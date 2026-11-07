"""Window-scoped HTTPS for Steam Community login; no TLS interception proxy.

WebView2 still owns redirects, origins and cookies. Only Community requests use
the certificate-checked CDN connection; all other hosts retain browser TLS.
Request bodies/cookies live in memory only and are never logged or replayed.
"""
from __future__ import annotations

import http.client
from queue import Empty, Queue
import threading
import time
from urllib.parse import urljoin, urlsplit

from .steam_acceleration import CDN_CANDIDATES, STEAM_HOST, ProbeFailure, SteamCdnConnection
from .steam_network import login_host_allowed, network_failure

MAX_LOGIN_BODY = 2 * 1024 * 1024
MAX_LOGIN_RESPONSE = 16 * 1024 * 1024
# The current official login UI falls back from WebAPI to these same-origin UM
# methods. They are authentication only; never allow arbitrary /um/ mutations.
LOGIN_AUTH_PATHS = frozenset('/um/Authentication/' + method + '/' for method in (
    'BeginAuthSessionViaQR', 'BeginAuthSessionViaCredentials',
    'GetPasswordRSAPublicKey', 'PollAuthSessionStatus',
    'UpdateAuthSessionWithSteamGuardCode', 'GenerateAccessTokenForApp',
))
_HOP = frozenset(('connection', 'proxy-connection', 'keep-alive', 'transfer-encoding',
                  'upgrade', 'te', 'trailer', 'proxy-authenticate', 'proxy-authorization'))


def community_request(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname == STEAM_HOST
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        return False


class CommunityLoginTransport:
    def __init__(self, *, timeout=8, addresses=CDN_CANDIDATES, connection_factory=SteamCdnConnection):
        self.timeout, self.addresses, self.connection_factory = timeout, tuple(addresses), connection_factory
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.connections = set()
        self.preferred = None

    def prepare(self):
        """Choose a responsive edge with disposable, unauthenticated GETs.

        A TLS handshake alone can succeed on an edge whose HTTP responses stall.
        Probe the public login page before the user signs in, never an auth POST
        or a browser request carrying cookies. Losing probes cannot change the
        chosen route or install their cookies in the browser.
        """
        if self.closed.is_set() or len(self.addresses) < 2:
            return False
        results = Queue()
        timeout = min(self.timeout, 3)

        def probe(address):
            connection = None
            success = False
            try:
                connection = self.connection_factory(address, timeout)
                with self.lock:
                    if self.closed.is_set(): return
                    self.connections.add(connection)
                connection.connect()
                if self.closed.is_set(): return
                connection.request('GET', '/login/home/?_cdn=akamai', headers={
                    'Host': STEAM_HOST, 'User-Agent': 'Mozilla/5.0',
                    'Accept': 'text/html', 'Accept-Encoding': 'identity', 'Connection': 'close'})
                response = connection.getresponse()
                success = response.status == 200 and bool(response.read(1))
            except Exception:
                pass  # No authentication data exists; the real request still checks TLS.
            finally:
                if connection is not None:
                    with self.lock: self.connections.discard(connection)
                    connection.close()
                results.put(address if success else None)

        for address in self.addresses:
            threading.Thread(target=probe, args=(address,),
                             name='steam-login-route-probe', daemon=True).start()
        deadline = time.monotonic() + timeout * 3 + .5
        remaining = len(self.addresses)
        while remaining and not self.closed.is_set():
            wait = min(.1, deadline - time.monotonic())
            if wait <= 0: break
            try: address = results.get(timeout=wait)
            except Empty: continue
            remaining -= 1
            if address is not None:
                with self.lock:
                    if self.closed.is_set(): return False
                    self.preferred = address
                return True
        return False

    def method_allowed(self, method, path):
        return (method in ('GET', 'HEAD', 'POST', 'OPTIONS')
                and (method != 'POST' or path.startswith(('/login/', '/jwt/'))
                     or path in LOGIN_AUTH_PATHS))

    def request(self, url, method, headers, body=b''):
        if not community_request(url):
            raise ProbeFailure('login_target_rejected', kind='security')
        parsed = urlsplit(url)
        if not self.method_allowed(method, parsed.path) or len(body) > MAX_LOGIN_BODY:
            raise ProbeFailure('login_request_rejected', kind='security')
        clean = {}
        for key, value in headers:
            if (not key or any(c in key + value for c in '\r\n\x00')
                    or len(key) + len(value) > 65536):
                raise ProbeFailure('login_headers_rejected', kind='security')
            if key.lower() not in _HOP | {'host', 'content-length', 'accept-encoding'}:
                clean[key] = value
        clean.update({'Host': STEAM_HOST, 'Accept-Encoding': 'identity', 'Connection': 'close'})
        target = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
        with self.lock:
            preferred = self.preferred
        addresses = sorted(self.addresses, key=lambda address: address != preferred)
        for index, address in enumerate(addresses):
            if self.closed.is_set(): raise ProbeFailure('login_window_closed', kind='network')
            connection = self.connection_factory(address, self.timeout)
            with self.lock:
                if self.closed.is_set(): raise ProbeFailure('login_window_closed', kind='network')
                self.connections.add(connection)
            try:
                # A route may change only before ANY HTTP headers/body are sent.
                # connect() checks both the certificate chain and exact Steam SAN.
                try:
                    connection.connect()
                except (OSError, http.client.HTTPException, ProbeFailure) as error:
                    failure = error if isinstance(error, ProbeFailure) else network_failure(error)
                    if failure.kind == 'network' and index + 1 < len(addresses): continue
                    raise failure from None
                if self.closed.is_set(): raise ProbeFailure('login_window_closed', kind='network')
                try:
                    connection.request(method, target, body=body or None, headers=clean)
                    response = connection.getresponse()
                    raw = response.read(MAX_LOGIN_RESPONSE + 1)
                    if len(raw) > MAX_LOGIN_RESPONSE:
                        raise ProbeFailure('login_response_too_large', kind='content')
                    received = response.getheaders()
                    response_headers = []
                    for key, value in received:
                        if any(c in key + value for c in '\r\n\x00'):
                            raise ProbeFailure('login_response_headers_rejected', kind='security')
                        if key.lower() == 'location':
                            redirect = urlsplit(urljoin(url, value))
                            if (redirect.scheme != 'https' or not login_host_allowed(redirect.hostname or '')
                                    or redirect.port not in (None, 443) or redirect.username or redirect.password):
                                raise ProbeFailure('login_redirect_rejected', kind='security')
                        if key.lower() not in _HOP | {'content-length'}:
                            response_headers.append((key, value))
                    response_headers.append(('Content-Length', str(len(raw))))
                    with self.lock: self.preferred = address
                    return int(response.status), response_headers, raw
                except (OSError, http.client.HTTPException) as error:
                    # Never fall back/replay after a request has started, even GET.
                    raise network_failure(error) from None
            finally:
                with self.lock: self.connections.discard(connection)
                connection.close()
        raise ProbeFailure('login_connection_failed', kind='network')

    def close(self):
        self.closed.set()
        with self.lock:
            for connection in self.connections: connection.close()


def attach_community_transport(window, transport):
    """Install before first navigation; WebView2 handles Cookie/Set-Cookie normally."""
    from System import Array, Byte
    from System.IO import MemoryStream
    from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
    core = window.browser.CoreWebView2
    slots = threading.BoundedSemaphore(8)

    def diagnose(url, error):
        diagnostics = getattr(window, 'diagnostics', None)
        if diagnostics is None: return
        from .steam_login_diagnostics import diagnostic_target
        # Exception text may include cookies or proxy details. Retain only a
        # fixed category, plus the allowlisted host/path without query strings.
        kind = getattr(error, 'kind', '')
        code = {'network': 'login_transport_connection_failed',
                'security': 'login_transport_security_rejected',
                'content': 'login_transport_response_rejected'}.get(kind, 'login_transport_failed')
        try: diagnostics.record('transport_error', diagnostic_target(url), code=code)
        except Exception: pass

    def respond(event, status, headers, body):
        reason = http.client.responses.get(status, 'Steam response')
        event.Response = core.Environment.CreateWebResourceResponse(
            MemoryStream(Array[Byte](body)), status, reason,
            ''.join(key + ': ' + value + '\r\n' for key, value in headers))

    def requested(_, event):
        request = event.Request
        if not community_request(str(request.Uri)): return
        deferral = event.GetDeferral()
        acquired = False
        try:
            acquired = slots.acquire(blocking=False)
            if not acquired: raise ProbeFailure('login_requests_busy', kind='network')
            headers = [(str(pair.Key), str(pair.Value)) for pair in request.Headers]
            body = bytearray()
            if request.Content is not None:
                buffer = Array.CreateInstance(Byte, 65536)
                while True:
                    length = request.Content.Read(buffer, 0, len(buffer))
                    if not length: break
                    body.extend(bytes(buffer)[:length])
                    if len(body) > MAX_LOGIN_BODY:
                        raise ProbeFailure('login_body_too_large', kind='security')
            url, method, payload = str(request.Uri), str(request.Method), bytes(body)
        except Exception as error:
            diagnose(str(request.Uri), error)
            if acquired: slots.release()
            respond(event, 503, [('Content-Type', 'text/plain; charset=utf-8')],
                    'Steam 登录请求暂未完成，请关闭登录窗口后重试。'.encode())
            deferral.Complete()
            return

        def fetch():
            try:
                try:
                    result = transport.request(url, method, headers, payload)
                except Exception as error:
                    diagnose(url, error)
                    result = (502, [('Content-Type', 'text/plain; charset=utf-8'), ('Cache-Control', 'no-store')],
                        'Steam 登录通道连接失败，请检查网络或稍后重试。'.encode())
                def complete():
                    try:
                        if window.ready: respond(event, *result)
                    finally: deferral.Complete()
                try: window._invoke(complete)
                except Exception: pass  # Closing the WebView cancels its pending requests.
            finally:
                slots.release()
        threading.Thread(target=fetch, name='steam-login-community', daemon=True).start()

    core.AddWebResourceRequestedFilter('https://steamcommunity.com/*', CoreWebView2WebResourceContext.All)
    core.AddWebResourceRequestedFilter('https://steamcommunity.com:443/*', CoreWebView2WebResourceContext.All)
    core.WebResourceRequested += requested
