"""Reusable exact-host Steam TLS leaf; private material uses current-user DPAPI.

The leaf cannot issue certificates. Only its exact DER is trusted, independently
of the verified upstream Steam connection. No certificate-store writes occur
until the desktop explicitly enables its native Community bridge.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import ssl
import tempfile
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .paths import runtime_dir
from .steam_acceleration import STEAM_HOST
from .steam_session import _dpapi

PREFIX = 'Deskrawl Steam Community v1 '
LIFETIME = timedelta(days=365)
RENEW_BEFORE = timedelta(days=7)
MAX_FILE = 64 * 1024


def validate_leaf(der):
    """Validate ownership/constraints even for expired leaves being retired."""
    cert = x509.load_der_x509_certificate(der)
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    key = cert.public_key()
    if (cert.subject != cert.issuer or len(cert.subject) != 1 or len(names) != 1
            or not names[0].value.startswith(PREFIX)
            or len(names[0].value[len(PREFIX):]) != 32
            or any(c not in '0123456789abcdef' for c in names[0].value[len(PREFIX):])
            or not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048
            or cert.signature_algorithm_oid != x509.SignatureAlgorithmOID.RSA_WITH_SHA256
            or len(cert.extensions) != 4):
        raise ValueError('Invalid native Steam certificate')
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    if (not basic.critical or basic.value.ca or basic.value.path_length is not None
            or not usage.critical or not usage.value.digital_signature or not usage.value.key_encipherment
            or usage.value.content_commitment or usage.value.data_encipherment or usage.value.key_agreement
            or usage.value.key_cert_sign or usage.value.crl_sign
            or not eku.critical or list(eku.value) != [ExtendedKeyUsageOID.SERVER_AUTH]
            or san.critical or list(san.value) != [x509.DNSName(STEAM_HOST)]
            or not timedelta(0) < cert.not_valid_after_utc - cert.not_valid_before_utc <= LIFETIME + timedelta(minutes=5)):
        raise ValueError('Invalid native Steam certificate constraints')
    key.verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
    return cert


def _new_leaf(now):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, PREFIX + secrets.token_hex(16))])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + LIFETIME)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(STEAM_HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER), key


def _context(der, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(['http/1.1'])
    def sni(_socket, hostname, _context):
        if hostname != STEAM_HOST: return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
    context.set_servername_callback(sni)
    password = secrets.token_bytes(32)
    # OpenSSL needs files; even this short-lived scratch key is encrypted with
    # a random process-only password. Never write an unencrypted private key.
    with tempfile.TemporaryDirectory(prefix='deskrawl-steam-tls-') as folder:
        public, private = Path(folder) / 'leaf.pem', Path(folder) / 'key.pem'
        public.write_bytes(x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password)))
        context.load_cert_chain(public, private, password)
    return context


@dataclass(frozen=True)
class CertificateMaterial:
    context: ssl.SSLContext
    der: bytes
    check_after: float


class PersistentCertificateStore:
    def __init__(self, path=None, *, protector=_dpapi, clock=None):
        self.path = Path(path) if path is not None else runtime_dir() / 'steam-community-certificate.dpapi'
        self.protector = protector
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.guard = threading.Lock()

    def _load(self):
        try:
            with self.path.open('rb') as stream: raw = stream.read(MAX_FILE + 1)
        except FileNotFoundError:
            return None
        if len(raw) > MAX_FILE: return None
        try:
            decoded = self.protector(raw, decrypt=True)
            if len(decoded) > MAX_FILE: return None
            data = json.loads(decoded)
            if not isinstance(data, dict) or data.get('version') != 1: return None
            der = base64.b64decode(data['certificate'], validate=True)
            validate_leaf(der)
            key = serialization.load_der_private_key(base64.b64decode(data['key'], validate=True), password=None)
            if (not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048
                    or key.public_key().public_numbers() != validate_leaf(der).public_key().public_numbers()):
                return None
            retired = data.get('retired', [])
            if not isinstance(retired, list) or len(retired) > 4: return None
            retired = [base64.b64decode(value, validate=True) for value in retired]
            for value in retired: validate_leaf(value)
            return der, key, retired
        except Exception:
            # Corruption or another Windows user's DPAPI data is not reusable.
            # Generate a new leaf; never fall back to an unprotected key file.
            return None

    def _save(self, der, key, retired):
        data = {'version': 1, 'certificate': base64.b64encode(der).decode('ascii'),
                'key': base64.b64encode(key.private_bytes(serialization.Encoding.DER,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode('ascii'),
                'retired': [base64.b64encode(value).decode('ascii') for value in retired]}
        raw = self.protector(json.dumps(data, separators=(',', ':')).encode('ascii'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A unique sibling plus replace leaves the previous encrypted bundle
        # intact on failure. No shared .tmp name or plaintext fallback.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=self.path.name + '.',
                                             suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)

    def prepare(self, trust, *, activate=None):
        with self.guard:
            try:
                now, stored = self.clock(), self._load()
                retired = stored[2] if stored is not None else []
                if stored is not None:
                    cert = validate_leaf(stored[0])
                    valid = cert.not_valid_before_utc <= now < cert.not_valid_after_utc - RENEW_BEFORE
                else: valid = False
                if valid:
                    der, key = stored[:2]
                else:
                    if stored is not None:
                        retired = list(dict.fromkeys([*retired, stored[0]]))
                    der, key = _new_leaf(now)
                    # Save before asking for trust. A rejected system prompt
                    # does not cause another key to be generated next time.
                    self._save(der, key, retired)
                context = _context(der, key)
                trust.ensure(der)
                # During renewal publish the new TLS context before retiring
                # old trust. Existing handshakes can finish on the old leaf.
                if activate is not None: activate(context, der)
                for previous in retired:
                    if previous != der: trust.retire(previous)
                if retired: self._save(der, key, [])
                until_renewal = (validate_leaf(der).not_valid_after_utc - RENEW_BEFORE - now).total_seconds()
                return CertificateMaterial(context, der, max(1, min(24 * 3600, until_renewal)))
            except Exception:
                raise OSError('本机 Steam 专用证书无法安全保存或信任；请检查当前 Windows 用户权限和证书授权。') from None
