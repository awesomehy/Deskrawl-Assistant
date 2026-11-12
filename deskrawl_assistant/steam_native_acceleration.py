"""Opt-in local Steam Community HTTPS bridge, owned by the desktop lifecycle.

No proxy server, Steam files, login tokens or process memory are modified.
Only steamcommunity.com is mapped to loopback. A reusable, non-CA leaf is
trusted for this Windows user; its key is persisted only with current-user
DPAPI protection. A temporary exact-host Windows proxy bypass prevents
CONNECT from bypassing loopback. Upstream Steam TLS is independently verified.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import hashlib
from pathlib import Path
import secrets
import socket
import ssl
import tempfile
import threading
import time

from .steam_acceleration import STEAM_HOST
from .steam_login_transport import CommunityLoginTransport, MAX_LOGIN_BODY
from .steam_native_hosts import WindowsHostsLease, NativeAccelerationError
from .steam_native_proxy import WindowsProxyBypassLease, ProxyConfigurationError
from .steam_native_certificate import PersistentCertificateStore, PREFIX as PERSISTENT_PREFIX, validate_leaf


def temporary_certificate(*, key=None):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    if key is None: key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                     'Deskrawl Steam Community temporary ' + secrets.token_hex(8))])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(hours=24))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(STEAM_HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(['http/1.1'])
    def sni(_socket, hostname, _context):
        if hostname != STEAM_HOST: return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
    context.set_servername_callback(sni)
    password = secrets.token_bytes(32)
    # Neither an unencrypted key nor a reusable CA key is stored on disk.
    with tempfile.TemporaryDirectory(prefix='deskrawl-steam-tls-') as folder:
        public, private = Path(folder) / 'leaf.pem', Path(folder) / 'key.pem'
        public.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password)))
        context.load_cert_chain(public, private, password)
    return context, cert.public_bytes(serialization.Encoding.DER)


class WindowsLeafTrust:
    """Trust exact DER; keep reusable leaves and clean up temporary leaves."""
    def __init__(self):
        self.certificates = set()

    def _api(self):
        dll = ctypes.WinDLL('crypt32', use_last_error=True)
        dll.CertOpenSystemStoreW.argtypes = (ctypes.c_void_p, w.LPCWSTR)
        dll.CertOpenSystemStoreW.restype = ctypes.c_void_p
        dll.CertAddEncodedCertificateToStore.argtypes = (ctypes.c_void_p, w.DWORD,
            ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p)
        dll.CertAddEncodedCertificateToStore.restype = w.BOOL
        dll.CertEnumCertificatesInStore.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        dll.CertEnumCertificatesInStore.restype = ctypes.c_void_p
        dll.CertDeleteCertificateFromStore.argtypes = (ctypes.c_void_p,)
        dll.CertDeleteCertificateFromStore.restype = w.BOOL
        dll.CertFreeCertificateContext.argtypes = (ctypes.c_void_p,)
        dll.CertCloseStore.argtypes = (ctypes.c_void_p, w.DWORD)
        dll.CertOpenStore.argtypes = (ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, w.LPCWSTR)
        dll.CertOpenStore.restype = ctypes.c_void_p
        return dll

    def add(self, der):
        dll = self._api()
        store = dll.CertOpenSystemStoreW(None, 'ROOT')
        if not store: raise OSError('本机 Steam 临时证书存储不可用。')
        try:
            raw = ctypes.create_string_buffer(der)
            if not dll.CertAddEncodedCertificateToStore(store, 1, raw, len(der), 1, None):
                raise OSError('本机 Steam 临时证书未获系统信任；未修改域名配置。')
            self.certificates.add(der)
        finally: dll.CertCloseStore(store, 0)

    def contains(self, der):
        class Context(ctypes.Structure):
            _fields_ = [('encoding', w.DWORD), ('raw', ctypes.c_void_p), ('size', w.DWORD),
                        ('info', ctypes.c_void_p), ('store', ctypes.c_void_p)]
        dll = self._api()
        store = dll.CertOpenSystemStoreW(None, 'ROOT')
        if not store: raise OSError('本机 Steam 证书存储不可用。')
        current = None
        try:
            while True:
                current = dll.CertEnumCertificatesInStore(store, current)
                if not current: return False
                item = ctypes.cast(current, ctypes.POINTER(Context)).contents
                if item.size == len(der) and ctypes.string_at(item.raw, item.size) == der: return True
        finally:
            if current: dll.CertFreeCertificateContext(current)
            dll.CertCloseStore(store, 0)

    def ensure(self, der):
        validate_leaf(der)
        if not self.contains(der): self.add(der)
        # Persistent trust is intentional. Ordinary shutdown must not cause
        # another certificate-installation prompt on the next launch.
        self.certificates.discard(der)

    def retire(self, der):
        validate_leaf(der)
        self.certificates.add(der)
        self.remove(der)

    def remove(self, der):
        if der not in self.certificates: return
        class Context(ctypes.Structure):
            _fields_ = [('encoding', w.DWORD), ('raw', ctypes.c_void_p), ('size', w.DWORD),
                        ('info', ctypes.c_void_p), ('store', ctypes.c_void_p)]
        dll = self._api()
        store = dll.CertOpenSystemStoreW(None, 'ROOT')
        if not store: raise OSError('本机 Steam 临时证书清理失败。')
        current = None
        try:
            while True:
                current = dll.CertEnumCertificatesInStore(store, current)
                if not current: break
                item = ctypes.cast(current, ctypes.POINTER(Context)).contents
                if item.size == len(der) and ctypes.string_at(item.raw, item.size) == der:
                    removed = dll.CertDeleteCertificateFromStore(current)
                    current = None
                    if not removed: raise OSError('本机 Steam 临时证书清理失败。')
                    break
            self.certificates.discard(der)
        finally:
            if current: dll.CertFreeCertificateContext(current)
            dll.CertCloseStore(store, 0)

    def close(self):
        for der in tuple(self.certificates): self.remove(der)


def cleanup_owned_leaf(fingerprint, user_sid):
    """Crash cleanup by the hosts watcher, in the ORIGINAL user's root store.

    A different administrator may approve UAC. Never delete a certificate in
    that administrator's own store, and never accept a registry/file path.
    """
    import re
    from cryptography import x509
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import NameOID
    if not re.fullmatch('[a-f0-9]{64}', fingerprint) or not re.fullmatch(r'S-1-(?:\d+-){1,14}\d+', user_sid):
        raise ValueError('Invalid certificate owner')
    dll = WindowsLeafTrust()._api()
    store = dll.CertOpenStore(ctypes.c_void_p(10), 0, None, 0x64000, user_sid + '\\Root')
    if not store: raise OSError('Certificate cleanup unavailable')
    class Context(ctypes.Structure):
        _fields_ = [('encoding', w.DWORD), ('raw', ctypes.c_void_p), ('size', w.DWORD),
                    ('info', ctypes.c_void_p), ('store', ctypes.c_void_p)]
    current = None
    try:
        while True:
            current = dll.CertEnumCertificatesInStore(store, current)
            if not current: break
            item = ctypes.cast(current, ctypes.POINTER(Context)).contents
            if not 1 <= item.size <= 8192: continue
            raw = ctypes.string_at(item.raw, item.size)
            try:
                cert = x509.load_der_x509_certificate(raw)
                public_key = cert.public_key().public_bytes(serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo)
            except (ValueError, UnsupportedAlgorithm):
                continue  # An unrelated legacy root cannot block our cleanup.
            if hashlib.sha256(public_key).hexdigest() != fingerprint: continue
            names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            if len(names) == 1 and names[0].value.startswith(PERSISTENT_PREFIX):
                validate_leaf(raw)
                # Reusable trust survives normal exits and crashes. Only the
                # ephemeral mapping/proxy lease belongs to this watcher.
                continue
            if (cert.issuer != cert.subject or len(names) != 1
                    or not names[0].value.startswith('Deskrawl Steam Community temporary ')
                    or cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                    or cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) != [STEAM_HOST]):
                raise ValueError('Not an owned temporary leaf')
            removed = dll.CertDeleteCertificateFromStore(current)
            current = None
            if not removed: raise OSError('Certificate cleanup failed')
            # Renewal leaves at most two matching leaves. Deleting the context
            # invalidates it; restart enumeration to remove every owned leaf.
    finally:
        if current: dll.CertFreeCertificateContext(current)
        dll.CertCloseStore(store, 0)


class CommunityRelayTransport(CommunityLoginTransport):
    # Login interception stays restricted to authentication endpoints. Only
    # this explicit native-browser relay supports other Community actions.
    def method_allowed(self, method, path):
        return (method in ('GET', 'HEAD', 'POST', 'OPTIONS') and path.startswith('/')
                and not path.startswith('//') and not any(c in path for c in '\r\n\\\x00'))


class NativeCommunityBridge:
    def __init__(self, *, transport=None, lease=None, trust=None, proxy_lease=None,
                 certificate_factory=None, certificate_store=None, port=443):
        self.transport = transport or CommunityRelayTransport(timeout=8)
        self.lease = lease or WindowsHostsLease()
        self.trust = trust or WindowsLeafTrust()
        self.proxy_lease = proxy_lease or WindowsProxyBypassLease()
        self.certificate_factory, self.port = certificate_factory, port
        self.certificate_store = (certificate_store or PersistentCertificateStore()) if certificate_factory is None else None
        self.server = self.thread = None
        self.key = None
        self.context = self.der = self.retired_der = None
        self.stopped = threading.Event()
        self.connections = set()
        self.guard = threading.Lock()
        self.slots = threading.BoundedSemaphore(16)

    def start(self, cancelled):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        owner = self
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False
            def get_request(self):
                stream, address = super().get_request()
                stream.settimeout(8)
                return stream, address
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def handle(self):
                if not owner.slots.acquire(blocking=False):
                    self.request.close(); return
                stream = None
                try:
                    with owner.guard:
                        if owner.stopped.is_set(): return
                        owner.connections.add(self.request)
                    self.rfile.close(); self.wfile.close()
                    with owner.guard: current_context = owner.context
                    stream = current_context.wrap_socket(self.request, server_side=True)
                    with owner.guard:
                        owner.connections.discard(self.request)
                        owner.connections.add(stream)
                    self.request = stream
                    self.setup()
                    # One request per connection bounds buffers and idle slots;
                    # Steam may reconnect normally, without replaying a request.
                    super().handle()
                except (OSError, ValueError): pass
                finally:
                    with owner.guard:
                        owner.connections.discard(self.request)
                        if stream is not None: owner.connections.discard(stream)
                    if stream is not None: stream.close()
                    owner.slots.release()
            def _relay(self):
                self.close_connection = True
                try:
                    if (self.headers.get('Host', '').lower() not in (STEAM_HOST, STEAM_HOST + ':443')
                            or self.headers.get('Transfer-Encoding')
                            or self.headers.get('Proxy-Authorization')
                            or self.requestline.split(' ')[1].startswith('//')
                            or not owner.transport.method_allowed(self.command, self.path)):
                        self.send_error(403); return
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) > 1: self.send_error(400); return
                    length = int(lengths[0]) if lengths else 0
                    if not 0 <= length <= MAX_LOGIN_BODY: self.send_error(413); return
                    body = self.rfile.read(length)
                    if len(body) != length: return
                    status, headers, raw = owner.transport.request(
                        'https://' + STEAM_HOST + self.path, self.command, list(self.headers.items()), body)
                    self.send_response_only(status)
                    for name, value in headers:
                        if name.lower() not in ('connection', 'keep-alive'):
                            self.send_header(name, value)
                    self.send_header('Connection', 'close'); self.end_headers()
                    if self.command != 'HEAD': self.wfile.write(raw)
                except Exception:
                    # No headers, cookies, page bodies or exception text go to logs.
                    try: self.send_error(502, 'Steam Community connection unavailable')
                    except OSError: pass
            do_GET = do_HEAD = do_POST = do_OPTIONS = _relay
        stage = 'listener'
        try:
            self.server = Server(('127.0.0.1', self.port), Handler)
            stage = 'upstream'
            self.transport.prepare()
            if cancelled.is_set(): raise OSError('本机 Steam 加速已取消。')
            stage = 'certificate'
            self._prepare_certificate(initial=True)
            if cancelled.is_set(): raise OSError('本机 Steam 加速已取消。')
            der = self.der
            public_key = x509.load_der_x509_certificate(der).public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            self.lease.certificate_fingerprint = hashlib.sha256(public_key).hexdigest()
            self.thread = threading.Thread(target=self.server.serve_forever,
                kwargs={'poll_interval': .1}, name='Steam-native-community-bridge', daemon=True)
            self.thread.start()
            stage = 'hosts'
            self.lease.start(cancelled)
            stage = 'proxy'
            self.proxy_lease.start(cancelled, nonce=self.lease.nonce)
            if cancelled.is_set(): raise OSError('本机 Steam 加速已取消。')
            return self
        except Exception as exc:
            self.close()
            if isinstance(exc, (NativeAccelerationError, ProxyConfigurationError)):
                raise
            messages = {
                'listener': '本机 Steam 加速监听端口 443 无法启用，可能被其他程序占用或系统禁止。',
                'upstream': '本机 Steam 加速无法连接 Steam 上游线路，请稍后重试。',
                'certificate': '本机 Steam 专用证书无法准备或获得系统信任；助手内部加速仍可使用。',
                'hosts': '本机 Steam 域名配置未启用，请检查 Windows 授权及已有社区映射。',
                'proxy': '本机 Steam 代理绕行配置未启用；助手内部加速仍可使用。',
            }
            raise NativeAccelerationError(stage + '_unavailable', messages[stage]) from None

    def _prepare_certificate(self, *, initial=False):
        if self.certificate_store is not None:
            def activate(context, der):
                with self.guard: self.context, self.der = context, der
            material = self.certificate_store.prepare(self.trust, activate=activate)
            context, der, delay = material.context, material.der, material.check_after
        else:
            # Short-lived leaves remain available only for isolated fixtures
            # and for testing the pre-persistence cleanup protocol.
            if initial and self.certificate_factory is temporary_certificate:
                from cryptography.hazmat.primitives.asymmetric import rsa
                self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if self.certificate_factory is temporary_certificate:
                if self.key is None: raise OSError('本机 Steam 临时证书无法续期。')
                context, der = self.certificate_factory(key=self.key)
            else:
                context, der = self.certificate_factory()
            self.trust.add(der)
            delay = 12 * 3600
        with self.guard:
            previous = self.der
            self.context, self.der = context, der
        if self.certificate_store is None:
            if self.retired_der is not None: self.trust.remove(self.retired_der)
            self.retired_der = previous
        self.renew_after = time.monotonic() + delay

    def renew(self):
        """Check reusable trust daily; rotate only near expiry or on invalid data."""
        if self.stopped.is_set(): raise OSError('本机 Steam 证书无法续期。')
        self._prepare_certificate()

    def close(self):
        self.stopped.set()
        self.proxy_lease.close()
        self.lease.close()
        if self.server is not None:
            if self.thread is not None: self.server.shutdown()
            self.server.server_close()
        with self.guard:
            for stream in self.connections:
                try: stream.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                stream.close()
        if self.thread is not None: self.thread.join(2)
        self.transport.close()
        self.trust.close()
        self.key = None


class NativeSteamAcceleration:
    """One setting, serialized apply/rollback; network work never holds the UI lock."""
    def __init__(self, *, bridge_factory=NativeCommunityBridge, supported=None):
        self.factory = bridge_factory
        self.supported = os.name == 'nt' if supported is None else supported
        self.guard = threading.Lock()
        self.wake, self.closed, self.cancel = threading.Event(), threading.Event(), threading.Event()
        self.enabled = False
        self.state = {'status': 'disabled', 'message': '本机 Steam 社区加速已关闭。'}
        self.thread = threading.Thread(target=self._run, name='Steam-native-acceleration', daemon=True)
        self.thread.start()

    def configure(self, enabled):
        with self.guard:
            if self.closed.is_set() or enabled == self.enabled: return
            self.enabled = enabled
            self.cancel.set()
            self.wake.set()

    def snapshot(self):
        with self.guard: return dict(self.state)

    def _run(self):
        bridge = None
        timeout = None
        while True:
            expired = not self.wake.wait(timeout)
            self.wake.clear()
            timeout = None
            if expired and bridge is not None and not self.closed.is_set():
                try:
                    bridge.renew()
                    timeout = max(0, bridge.renew_after - time.monotonic())
                    continue
                except Exception:
                    pass  # Restore DNS/trust rather than serving an expired leaf.
            if bridge is not None:
                try: bridge.close()
                except Exception:
                    with self.guard:
                        self.state = {'status': 'error', 'message': '本机 Steam 加速清理未完成，请检查系统授权。'}
                    # Do not start another bridge while a previous lease may remain.
                    if self.closed.is_set(): break
                    continue
                bridge = None
            with self.guard:
                if self.closed.is_set(): break
                if expired:
                    self.state = {'status': 'expired', 'message':
                        '本机 Steam 专用证书核验或更新失败，本机通道已关闭；关闭再开启可重试，助手内部加速仍可使用。'}
                    continue
                enabled = self.enabled
                self.cancel = threading.Event()
                cancel = self.cancel
                self.state = {'status': 'starting' if enabled else 'disabled', 'message':
                    '正在启用本机 Steam 社区加速；请处理 Windows 系统授权。' if enabled else
                    '本机 Steam 社区加速已关闭。'}
            if not enabled: continue
            if not self.supported:
                with self.guard:
                    self.state = {'status': 'unavailable', 'message': '本机 Steam 加速仅支持 Windows 独立窗口。'}
                continue
            try:
                bridge = self.factory()
                bridge.start(cancel)
                with self.guard:
                    if not cancel.is_set():
                        self.state = {'status': 'active', 'message':
                            '本机 Steam 社区通道已开启；重新打开 Steam 社区页面可使用新线路。'}
                        renew_after = getattr(bridge, 'renew_after', None)
                        if isinstance(renew_after, (int, float)):
                            timeout = max(0, renew_after - time.monotonic())
            except Exception as exc:
                cleanup_failed = False
                if bridge is not None:
                    try: bridge.close()
                    except Exception: cleanup_failed = True
                if not cleanup_failed: bridge = None
                with self.guard:
                    if cleanup_failed:
                        self.state = {'status': 'error', 'message': '本机 Steam 加速清理尚未完成，请检查 Windows 授权或已有社区映射。'}
                    elif not cancel.is_set():
                        self.state = {'status': 'error', 'message':
                            (str(exc) if isinstance(exc, (ProxyConfigurationError, NativeAccelerationError)) else
                             '本机 Steam 加速未启用。请检查系统授权、443 端口及已有社区 hosts 映射；助手内部加速仍可使用。'),
                            'error_code': exc.code if isinstance(exc, NativeAccelerationError) else 'setup_failed'}

    def close(self):
        with self.guard:
            self.closed.set(); self.cancel.set(); self.wake.set()
        if self.thread is not threading.current_thread(): self.thread.join(15)
