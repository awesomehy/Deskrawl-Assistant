"""App-scoped anonymous Steam market transport; no system network mutations."""
from __future__ import annotations

from email.utils import parsedate_to_datetime
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
from pathlib import Path
import secrets
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler, ProxyHandler, Request, build_opener, getproxies,
)

from deskrawl_assistant.steam_market import (
    APP_ID, MAX_DOCUMENT_BYTES, MarketCatalog, MarketDataError, MarketItem,
    parse_listing,
)


ROOT = Path(__file__).resolve().parents[1]
STEAM_HOST = 'steamcommunity.com'
# Candidate addresses from Pixiv-Nginx/conf/site-steam.conf, not relay servers.
# IP ownership alone is never trusted: the actual Steam certificate is required.
CDN_CANDIDATES = ('104.91.87.202', '23.1.179.144')
HEADERS = {
    'Host': STEAM_HOST,
    'User-Agent': 'Deskrawl-Assistant public market quotes/1.0',
    'Accept': 'text/html',
    'Accept-Language': 'en-US,en;q=0.8',
    'Accept-Encoding': 'identity',
    'Connection': 'close',
}


class ProbeFailure(Exception):
    """Static diagnostic codes deliberately exclude credentials and page data."""
    def __init__(self, code, *, kind='network', status=None, retry_after=0):
        super().__init__(code)
        self.code = code
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    def as_dict(self):
        return {'code': self.code, 'kind': self.kind, 'http_status': self.status,
                'retry_after_seconds': self.retry_after}


def listing_path(item):
    parsed = urlsplit(item.market_url)
    if (parsed.scheme != 'https' or parsed.netloc != STEAM_HOST
            or not parsed.path.startswith(f'/market/listings/{APP_ID}/')
            or parsed.query or parsed.fragment):
        raise ProbeFailure('invalid_market_target', kind='config')
    return parsed.path + '?l=english'


class RequestBudget:
    """One serial remote request stream across both routes, with durable backoff."""
    def __init__(self, cooldown_file=None, *, interval=15, clock=time.monotonic,
                 wall=time.time, sleep=time.sleep):
        self.path = Path(cooldown_file) if cooldown_file else None
        self.interval = max(15, interval)
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.lock = threading.Lock()

    def _load(self):
        if self.path and self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                blocked, last = float(value['blocked_until']), float(value['last_request'])
                if not math.isfinite(blocked) or not math.isfinite(last):
                    raise ValueError('invalid timestamps')
                self.blocked_until = max(self.blocked_until, blocked)
                # Also preserve request spacing when the CLI is restarted.
                delay = min(self.interval, max(0, last + self.interval - self.wall()))
                self.next_request = max(self.next_request, self.clock() + delay)
            except (OSError, ValueError, TypeError, KeyError):
                raise ProbeFailure('invalid_backoff_file', kind='config') from None

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'blocked_until': self.blocked_until,
                                             'last_request': self.wall()}), encoding='utf-8')
            temporary.replace(self.path)

    def before_request(self):
        with self.lock:
            self._load()
            remaining = self.blocked_until - self.wall()
            if remaining > 0:
                raise ProbeFailure('steam_backoff_active', kind='cooldown', retry_after=int(remaining) + 1)
            delay = self.next_request - self.clock()
            if delay > 0:
                self.sleep(delay)
            self.next_request = self.clock() + self.interval
            self._save()

    def check_backoff(self):
        """Authenticated operations keep their own pacing but share HTTP backoff semantics."""
        with self.lock:
            self._load()
            remaining = self.blocked_until - self.wall()
            if remaining > 0:
                raise ProbeFailure('steam_backoff_active', kind='cooldown', retry_after=int(remaining) + 1)

    def observe(self, status, retry_after=None):
        if status not in (403, 429):
            return
        seconds = 300
        if retry_after:
            try:
                seconds = max(seconds, int(retry_after))
            except (ValueError, TypeError):
                try:
                    seconds = max(seconds, int(parsedate_to_datetime(retry_after).timestamp() - self.wall()))
                except (ValueError, TypeError, OverflowError):
                    pass
        seconds = min(3600, seconds)
        with self.lock:
            self.blocked_until = max(self.blocked_until, self.wall() + seconds)
            self._save()


def read_response(response, budget):
    status = response.status if hasattr(response, 'status') else response.code
    budget.observe(status, response.headers.get('Retry-After'))
    if status != 200:
        raise ProbeFailure('steam_http_error', kind='http', status=status,
                           retry_after=max(0, int(budget.blocked_until - budget.wall())))
    if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
        raise ProbeFailure('unexpected_content_encoding', kind='content')
    raw = response.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ProbeFailure('market_document_too_large', kind='content')
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        raise ProbeFailure('invalid_market_encoding', kind='content') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ProbeFailure('market_redirect_blocked', kind='http')


class ExistingRoute:
    def __init__(self, budget, timeout=8, *, proxies=None):
        self.budget, self.timeout = budget, timeout
        # Snapshot only; no install_opener(), environment or registry writes.
        self.proxies = dict(getproxies() if proxies is None else proxies)
        self.name = 'existing_proxy' if self.proxies.get('https') else 'existing_direct'
        self.opener = build_opener(ProxyHandler(self.proxies), NoRedirect())

    def get(self, item):
        path = listing_path(item)
        self.budget.before_request()
        request = Request('https://' + STEAM_HOST + path, headers=HEADERS)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return read_response(response, self.budget)
        except HTTPError as exc:
            with exc:
                return read_response(exc, self.budget)
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise ProbeFailure('existing_certificate_rejected', kind='security') from None
            raise ProbeFailure('existing_connection_failed') from None
        except ssl.SSLCertVerificationError:
            raise ProbeFailure('existing_certificate_rejected', kind='security') from None
        except (OSError, http.client.HTTPException):
            raise ProbeFailure('existing_connection_failed') from None


def verify_steam_hostname(certificate):
    # This fixed apex hostname needs an exact DNS SAN. Never accept a CN-only
    # certificate, a CDN alias, or '*.steamcommunity.com' for the apex domain.
    names = certificate.get('subjectAltName', ())
    if not any(kind == 'DNS' and name.lower() == STEAM_HOST for kind, name in names):
        raise ProbeFailure('cdn_steam_hostname_rejected', kind='security')


def upstream_tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Suppress SNI only. Chain/expiry validation stays on; fixed DNS SAN is
    # checked explicitly before any HTTP bytes leave the socket.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_default_certs()
    context.set_alpn_protocols(['http/1.1'])
    return context


class SteamCdnConnection(http.client.HTTPSConnection):
    def __init__(self, address, timeout):
        ipaddress.ip_address(address)
        super().__init__(STEAM_HOST, timeout=timeout, context=upstream_tls_context())
        self.address = address

    def connect(self):
        plain = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(plain, server_hostname=None)
            verify_steam_hostname(self.sock.getpeercert())
        except BaseException:
            if self.sock:
                self.sock.close()
            plain.close()
            raise


class CdnRoute:
    def __init__(self, budget, timeout=8, *, addresses=CDN_CANDIDATES):
        self.budget, self.timeout, self.addresses = budget, timeout, tuple(addresses)
        self.address_used = None

    def get(self, item):
        path = listing_path(item)
        for address in self.addresses:
            self.budget.before_request()
            connection = SteamCdnConnection(address, self.timeout)
            try:
                connection.request('GET', path, headers=HEADERS)
                with connection.getresponse() as response:
                    document = read_response(response, self.budget)
                    self.address_used = address
                    return document
            except ssl.SSLCertVerificationError:
                raise ProbeFailure('cdn_certificate_rejected', kind='security') from None
            except (OSError, http.client.HTTPException):
                # Only a connection failure can select another candidate.
                continue
            finally:
                connection.close()
        raise ProbeFailure('cdn_connection_failed')


class LocalRelay:
    """A single-use, fixed-target reverse proxy, reachable only on IPv4 loopback."""
    def __init__(self, item, upstream):
        self.item, self.upstream = item, upstream
        self.token = secrets.token_hex(32)
        self.server = None
        self.thread = None
        self.used = False
        self.lock = threading.Lock()

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *args):
                pass

            def do_GET(self):
                valid = (self.path == '/listing'
                         and self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'
                         and not self.headers.get('Origin')
                         and secrets.compare_digest(self.headers.get('X-Deskrawl-Probe', ''), owner.token)
                         and not self.headers.get('Cookie')
                         and not self.headers.get('Authorization')
                         and not self.headers.get('Transfer-Encoding')
                         and self.headers.get('Content-Length', '0') == '0')
                if not valid:
                    self.send_error(403)
                    return
                with owner.lock:
                    if owner.used:
                        self.send_error(409)
                        return
                    owner.used = True
                try:
                    document = owner.upstream.get(owner.item)
                    if len(document.encode('utf-8')) > MAX_DOCUMENT_BYTES:
                        raise ProbeFailure('market_document_too_large', kind='content')
                    payload = {'ok': True, 'document': document}
                except ProbeFailure as exc:
                    payload = {'ok': False, 'error': exc.as_dict()}
                except Exception:
                    payload = {'ok': False, 'error': ProbeFailure('relay_upstream_failed').as_dict()}
                raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        return self

    @property
    def port(self):
        return self.server.server_port

    def get(self, item):
        if item != self.item:
            raise ProbeFailure('relay_target_mismatch', kind='config')
        # HTTPConnection deliberately bypasses all environment/system proxies.
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=65)
        try:
            connection.request('GET', '/listing', headers={'X-Deskrawl-Probe': self.token})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ProbeFailure('relay_access_rejected', kind='security', status=response.status)
                raw = response.read(MAX_DOCUMENT_BYTES * 6 + 1025)
                if len(raw) > MAX_DOCUMENT_BYTES * 6 + 1024:
                    raise ProbeFailure('relay_document_too_large', kind='content')
                value = json.loads(raw)
                if not value['ok']:
                    error = value['error']
                    raise ProbeFailure(error['code'], kind=error['kind'],
                                       status=error['http_status'], retry_after=error['retry_after_seconds'])
                return value['document']
        except (OSError, http.client.HTTPException):
            raise ProbeFailure('loopback_connection_failed') from None
        finally:
            connection.close()

    def __exit__(self, *args):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class MarketProbe:
    def __init__(self, preferred, upstream, *, relay_factory=LocalRelay):
        self.preferred, self.upstream = preferred, upstream
        self.relay_factory = relay_factory
        self.events = []
        self.route = None

    def fetch(self, item):
        listing_path(item)
        try:
            document = self.preferred.get(item)
            self.route = self.preferred.name
            self.events.append({'route': self.route, 'result': 'received'})
        except ProbeFailure as exc:
            self.events.append({'route': self.preferred.name, **exc.as_dict()})
            if exc.kind != 'network':
                raise
            self.route = 'local_reverse_proxy'
            try:
                with self.relay_factory(item, self.upstream) as relay:
                    document = relay.get(item)
                self.events.append({'route': self.route, 'result': 'received'})
            except ProbeFailure as fallback_error:
                self.events.append({'route': self.route, **fallback_error.as_dict()})
                raise
        return document

    def run(self, item):
        try:
            return parse_listing(self.fetch(item), item)
        except MarketDataError:
            raise ProbeFailure('steam_item_or_orderbook_unverified', kind='content') from None


class SteamMarketTransport:
    """Read the preference for each job; one budget survives route changes."""
    def __init__(self, enabled, cooldown_file, *, timeout=8):
        self.enabled = enabled
        self.budget = RequestBudget(cooldown_file)
        self.timeout = timeout

    def __call__(self, item):
        from .steam_market import MarketUnavailable
        try:
            preferred = ExistingRoute(self.budget, self.timeout)
            if self.enabled():
                return MarketProbe(preferred, CdnRoute(self.budget, self.timeout)).fetch(item)
            return preferred.get(item)
        except ProbeFailure as exc:
            messages = {
                'cooldown': 'Steam 市场暂时限制访问，请稍后重试',
                'security': 'Steam 证书验证失败，已停止读取市场',
                'content': 'Steam 市场响应格式无法验证',
                'config': 'Steam 市场连接设置无法读取',
            }
            message = messages.get(exc.kind, '无法读取 Steam 社区市场，托管不受影响')
            if exc.status in (403, 429): message = messages['cooldown']
            raise MarketUnavailable(message, status=exc.status,
                                    retry_after=exc.retry_after) from None
