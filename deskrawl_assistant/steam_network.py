"""Authenticated Steam routes. Only reads may fail over; writes are never replayed."""
from __future__ import annotations

import base64
import http.client
from http.cookiejar import Cookie, CookieJar
import select
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, unquote
from urllib.request import Request, ProxyHandler, HTTPSHandler, HTTPCookieProcessor, build_opener, getproxies

from .steam_acceleration import (
    CDN_CANDIDATES, STEAM_HOST, ProbeFailure, RequestBudget, SteamCdnConnection,
)
from .steam_session import MAX_RESPONSE, ORIGIN, SteamSessionError, _NoRedirect


def network_failure(error):
    reason = error.reason if isinstance(error, URLError) else error
    return ProbeFailure('steam_certificate_rejected', kind='security') if isinstance(
        reason, ssl.SSLCertVerificationError) else ProbeFailure('steam_connection_failed')


class AuthenticatedSteamTransport:
    def __init__(self, enabled, backoff_file=None, *, timeout=15):
        self.enabled, self.timeout = enabled, timeout
        self.budget = RequestBudget(backoff_file)
        self.local = threading.local()

    def _read(self, response):
        status = response.status if hasattr(response, 'status') else response.code
        try: self.budget.observe(status, response.headers.get('Retry-After'))
        except OSError:
            # A failed cooldown write must not turn an HTTP rejection into
            # a connection failure and thereby authorize another route.
            raise SteamSessionError('Steam 退避状态无法保存，已暂停请求。',
                                    status=status, retry_after=300) from None
        if status != 200:
            raise SteamSessionError('Steam 页面跳转暂无法核验。' if 300 <= status < 400 else
                'Steam 请求被限制，请稍后重试。' if status in (403, 429) else 'Steam 请求未成功。',
                status=status,
                retry_after=max(0, int(self.budget.blocked_until - self.budget.wall())))
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise SteamSessionError('Steam 响应编码无法确认。')
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE: raise SteamSessionError('Steam 响应过大，暂不继续操作。')
        try: return raw.decode('utf-8')
        except UnicodeError: raise SteamSessionError('Steam 响应格式已变化。') from None

    def _send(self, route, method, path, body, headers, proxies):
        connections = []
        try:
            # Preserve the official market eligibility GET redirects and their
            # temporary qualification cookies on both routes. The existing
            # redirect policy rejects POST replay and all foreign origins.
            jar = CookieJar()
            headers = dict(headers)
            raw_cookie = headers.pop('Cookie')
            for pair in raw_cookie.split('; '):
                name, value = pair.split('=', 1)
                jar.set_cookie(Cookie(0, name, value, None, False, STEAM_HOST,
                    False, False, '/', True, True, None, True, None, None, {}))
            handlers = [ProxyHandler(proxies if route is None else {}),
                        _NoRedirect(), HTTPCookieProcessor(jar)]
            if route is not None:
                class CdnHandler(HTTPSHandler):
                    def https_open(handler, request):
                        def connect(host, **options):
                            if host not in (STEAM_HOST, STEAM_HOST + ':443'):
                                raise SteamSessionError('Steam 请求地址无效。')
                            connection = SteamCdnConnection(route, self.timeout)
                            connections.append(connection)
                            return connection
                        return handler.do_open(connect, request)
                handlers.append(CdnHandler())
            request = Request(ORIGIN + path, body, headers, method=method)
            try:
                with build_opener(*handlers).open(request, timeout=self.timeout) as response:
                    return self._read(response)
            except HTTPError as error:
                with error: return self._read(error)
        except (URLError, OSError, http.client.HTTPException) as error:
            raise network_failure(error) from None
        except (ValueError, UnicodeError):
            raise ProbeFailure('steam_connection_config_invalid', kind='config') from None
        finally:
            for connection in connections: connection.close()

    def __call__(self, method, path, data, cookies):
        if (method not in ('GET', 'POST') or not path.startswith('/') or path.startswith('//')
                or any(c in path for c in ('\\', '\r', '\n', '#'))
                or (method == 'POST' and path != '/market/sellitem/')):
            raise SteamSessionError('Steam 请求地址无效。')
        headers = {'Host': STEAM_HOST, 'User-Agent': 'Mozilla/5.0 DeskrawlAssistant',
            'Cookie': '; '.join(name + '=' + value for name, value in sorted(cookies.items())),
            'Referer': ORIGIN + '/market/', 'Origin': ORIGIN,
            'Accept': 'application/json,text/html', 'Accept-Encoding': 'identity', 'Connection': 'close'}
        body = urlencode(data).encode('utf-8') if data is not None else None
        if body is not None: headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
        enabled = bool(self.enabled())
        # Disabling acceleration retains the release client's system proxy
        # route; it only disables CDN fallback and the private login tunnel.
        try: proxies = dict(getproxies())
        except Exception: raise SteamSessionError('Steam 连接设置无法读取。') from None
        signature = (enabled, tuple(sorted(proxies.items())))
        selected = getattr(self.local, 'route', (None, None))
        routes = [selected[1] if selected[0] == signature else None] if method == 'POST' else [None]
        if enabled and method == 'GET': routes += list(CDN_CANDIDATES)
        try:
            self.budget.check_backoff()
            for index, route in enumerate(routes):
                try:
                    result = self._send(route, method, path, body, headers, proxies)
                    if method == 'GET': self.local.route = (signature, route)
                    return result
                except ProbeFailure as error:
                    if error.kind != 'network' or index == len(routes) - 1: raise
        except ProbeFailure as error:
            message = ('Steam 证书验证失败，已停止请求。' if error.kind == 'security' else
                'Steam 访问正在退避等待，请稍后重试。' if error.kind == 'cooldown' else
                'Steam 连接设置无法读取。' if error.kind == 'config' else 'Steam 网络请求未确认。')
            raise SteamSessionError(message, status=429 if error.kind == 'cooldown' else 0,
                                    retry_after=error.retry_after) from None


def login_host_allowed(host):
    return (host in (STEAM_HOST, 'store.steampowered.com', 'login.steampowered.com',
                    'api.steampowered.com', 'help.steampowered.com',
                    'www.google.com', 'www.gstatic.com', 'www.recaptcha.net')
            or any(host == suffix or host.endswith('.' + suffix)
                   for suffix in ('steamstatic.com', 'steamusercontent.com')))


class LoginTunnelConnector:
    """Probe only a TLS handshake before handing an opaque tunnel to WebView2."""
    def __init__(self, *, timeout=8): self.timeout = timeout

    def _open(self, host, proxy, address=None):
        stream = None
        try:
            if not proxy:
                return socket.create_connection((address or host, 443), timeout=self.timeout)
            parsed = urlsplit(proxy if '://' in proxy else 'http://' + proxy)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ProbeFailure('unsupported_proxy', kind='config')
            stream = socket.create_connection((parsed.hostname, parsed.port or
                (443 if parsed.scheme == 'https' else 80)), timeout=self.timeout)
            if parsed.scheme == 'https':
                stream = ssl.create_default_context().wrap_socket(stream, server_hostname=parsed.hostname)
            auth = ''
            if parsed.username is not None:
                value = unquote(parsed.username) + ':' + unquote(parsed.password or '')
                auth = 'Proxy-Authorization: Basic ' + base64.b64encode(value.encode()).decode('ascii') + '\r\n'
            stream.sendall((f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n' + auth + '\r\n').encode('ascii'))
            response = http.client.HTTPResponse(stream);response.begin()
            status = response.status;response.close()
            if status != 200: raise ProbeFailure('proxy_connect_rejected', kind='http', status=status)
            return stream
        except (ValueError, UnicodeError):
            if stream is not None: stream.close()
            raise ProbeFailure('unsupported_proxy', kind='config') from None
        except Exception:
            if stream is not None: stream.close()
            raise

    def connect(self, host):
        if not login_host_allowed(host): raise ProbeFailure('login_target_rejected', kind='security')
        try: proxies = getproxies()
        except Exception: raise ProbeFailure('proxy_config_unavailable', kind='config') from None
        proxy = proxies.get('https')
        candidates = [(proxy, None)]
        if host == STEAM_HOST: candidates += [(None, address) for address in CDN_CANDIDATES]
        elif proxy: candidates.append((None, None))
        for index, (candidate, address) in enumerate(candidates):
            try:
                # No browser HTTP bytes, login data or Cookie exist at this stage.
                stream = self._open(host, candidate, address)
                try:
                    with ssl.create_default_context().wrap_socket(stream, server_hostname=host): pass
                finally: stream.close()
                return self._open(host, candidate, address)
            except (OSError, URLError, http.client.HTTPException) as error:
                failure = network_failure(error)
                if failure.kind != 'network' or index == len(candidates) - 1: raise failure from None
        raise ProbeFailure('login_connection_failed')


class LoginTunnel:
    """Window-scoped loopback CONNECT proxy. Never decrypts HTTPS or retries its bytes."""
    def __init__(self, connector=None):
        self.connector = connector or LoginTunnelConnector()
        self.server = self.thread = None
        self.closed = threading.Event();self.guard = threading.Lock();self.streams = set()
        self.slots = threading.BoundedSemaphore(12)

    def start(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def setup(self):
                super().setup();self.connection.settimeout(3)
            def do_CONNECT(self):
                self.close_connection = True
                host, _, port = self.path.rpartition(':')
                if (port != '443' or not login_host_allowed(host) or self.headers.get('Origin')
                        or self.headers.get('Cookie') or self.headers.get('Authorization')
                        or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0'):
                    self.send_error(403);return
                if not owner.slots.acquire(blocking=False): self.send_error(503);return
                upstream = None
                try:
                    if owner.closed.is_set(): return
                    upstream = owner.connector.connect(host)
                    with owner.guard:
                        if owner.closed.is_set(): return
                        owner.streams.update((self.connection, upstream))
                    self.send_response(200, 'Connection Established');self.end_headers();self.wfile.flush()
                    # Once established, any disconnect closes this tunnel. No route replay.
                    deadline = time.monotonic() + 300
                    while not owner.closed.is_set() and time.monotonic() < deadline:
                        ready, _, _ = select.select([self.connection, upstream], [], [], .25)
                        for source in ready:
                            raw = source.recv(65536)
                            if not raw: return
                            (upstream if source is self.connection else self.connection).sendall(raw)
                except (OSError, ValueError, ProbeFailure, http.client.HTTPException):
                    if upstream is None:
                        try: self.send_error(502)
                        except OSError: pass
                finally:
                    with owner.guard:
                        owner.streams.discard(self.connection);owner.streams.discard(upstream)
                    if upstream is not None: upstream.close()
                    owner.slots.release()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler);self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.05},
                                       name='steam-login-tunnel', daemon=True)
        self.thread.start();return self

    @property
    def browser_arguments(self):
        return f'--proxy-server=http://127.0.0.1:{self.server.server_port} --disable-quic'

    def close(self):
        self.closed.set()
        with self.guard:
            for stream in self.streams:
                try: stream.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                stream.close()
        if self.server is not None:
            self.server.shutdown();self.server.server_close()
        if self.thread is not None: self.thread.join(2)
