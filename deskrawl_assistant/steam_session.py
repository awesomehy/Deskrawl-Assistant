"""Local Steam Community login, with no credential bridge to the assistant UI.

Manually captured cookies use current-user Windows DPAPI. An optional native
Steam provider supplies active-account cookies in memory only. Both sources
are verified before use and sent only to the exact HTTPS Community origin.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse, unquote, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler

from .steam_market import APP_ID, CURRENCIES

ORIGIN = 'https://steamcommunity.com'
LOGIN_URL = ORIGIN + '/login/home/?goto=market%2F'
MAX_RESPONSE = 4 * 1024 * 1024
COOKIE_NAMES = frozenset(('steamLoginSecure', 'sessionid'))


class SteamSessionError(RuntimeError):
    def __init__(self, message, *, status=0, retry_after=0, auth_expired=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.auth_expired = auth_expired


def _dpapi(raw, *, decrypt=False):
    if os.name != 'nt':
        raise SteamSessionError('Steam 绑定仅支持 Windows 本机用户加密存储。')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, result = Blob(len(raw), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = ([ctypes.POINTER(Blob), ctypes.c_void_p] if decrypt else
                   [ctypes.POINTER(Blob), wintypes.LPCWSTR]) + [ctypes.c_void_p] * 3 + [wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None if decrypt else 'Deskrawl Steam session',
              None, None, None, 1, ctypes.byref(result)):
        raise SteamSessionError('Windows 无法读写 Steam 绑定，请使用原 Windows 账号。')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def _atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _validated_values(values):
    if not isinstance(values, dict) or set(values) != COOKIE_NAMES:
        raise SteamSessionError('Steam 登录 Cookie 格式无效。')
    for value in values.values():
        if not isinstance(value, str) or not 0 < len(value) <= 8192 or re.search(r'[\r\n;]', value):
            raise SteamSessionError('Steam 登录 Cookie 格式无效。')
    if not re.fullmatch(r'[0-9a-fA-F]{16,64}', values['sessionid']):
        raise SteamSessionError('Steam 登录会话格式已变化。')
    steamid = unquote(values['steamLoginSecure']).split('||', 1)[0]
    if not re.fullmatch(r'7656\d{13}', steamid):
        raise SteamSessionError('Steam 登录身份格式已变化。')
    return values


def normalise_cookies(cookies):
    """Accept native get_cookies() objects, never arbitrary frontend payloads."""
    result = {}
    if not isinstance(cookies, (list, tuple)):
        raise SteamSessionError('Steam 登录 Cookie 来源不受支持。')
    for cookie in cookies:
        if not isinstance(cookie, SimpleCookie):
            continue
        for name, morsel in cookie.items():
            if name not in COOKIE_NAMES:
                continue
            domain = str(morsel['domain']).lower().lstrip('.')
            if domain != 'steamcommunity.com':
                continue
            value = morsel.value
            if not isinstance(value, str) or not 0 < len(value) <= 8192 or re.search(r'[\r\n;]', value):
                raise SteamSessionError('Steam 登录 Cookie 格式无效。')
            if name == 'steamLoginSecure' and not morsel['secure']:
                raise SteamSessionError('Steam 登录缺少安全 Cookie。')
            if name in result and result[name] != value:
                raise SteamSessionError('Steam 返回重复登录身份。')
            result[name] = value
    if set(result) != COOKIE_NAMES:
        raise SteamSessionError('请先在 Steam 官方页面完成登录。')
    return _validated_values(result)


def _assignment(document, name):
    match = re.search(r'(?:var\s+)?' + re.escape(name) + r'\s*=\s*', document)
    if not match:
        return None
    try:
        return json.JSONDecoder().raw_decode(document[match.end():])[0]
    except ValueError:
        raise SteamSessionError('Steam 钱包页面结构已变化。') from None


def parse_wallet(document, steamid):
    """Fail closed if the logged-in wallet or account cannot be verified."""
    if re.search(r'\bg_steamID\s*=\s*(?:false|null|0|[\'\"]{2})\s*;', document):
        raise SteamSessionError('Steam 登录已过期，请重新登录。', status=401, auth_expired=True)
    identity = re.search(r'(?:var\s+)?g_steamID\s*=\s*[\'\"](7656\d{13})[\'\"]', document)
    wallet = _assignment(document, 'g_rgWalletInfo')
    apps = _assignment(document, 'g_rgAppContextData') or {}
    if identity and identity.group(1) != steamid:
        raise SteamSessionError('Steam 登录身份已变化，请重新登录。', status=401, auth_expired=True)
    if not identity or not isinstance(wallet, dict):
        raise SteamSessionError('Steam 登录或钱包页面暂无法核验，稍后重试。')
    currency = wallet.get('wallet_currency')
    try:
        currency = int(currency)
    except (ValueError, TypeError):
        currency = 0
    if currency not in CURRENCIES:
        raise SteamSessionError('Steam 钱包币种尚未确定，请先在 Steam 中确认钱包。')
    needed = ('wallet_market_minimum', 'wallet_currency_increment', 'wallet_fee_percent',
              'wallet_trade_max_balance')
    missing = [key for key in needed if key not in wallet]
    if missing:
        raise SteamSessionError('Steam 钱包费用参数缺失（' + '、'.join(missing) + '），暂不出售。')
    app = apps.get(str(APP_ID), {}) if isinstance(apps, dict) else {}
    if not isinstance(app, dict):
        raise SteamSessionError('Steam 游戏费用参数格式无效，暂不出售。')
    # Steam's GetPublisherFeeRate uses the app fee when present, then the
    # wallet-supplied default. Never copy its hard-coded final fallback: a fee
    # must come from the currently verified account's official page.
    publisher_fee = app.get('market_pubfee_rate')
    publisher_source = 'app_context' if publisher_fee is not None else None
    if publisher_fee is None:
        publisher_fee = wallet.get('wallet_publisher_fee_percent_default')
        publisher_source = 'wallet_default' if publisher_fee is not None else None
    if publisher_fee is not None:
        try:
            rate = Decimal(str(publisher_fee))
        except (InvalidOperation, ValueError):
            raise SteamSessionError('Steam 游戏手续费率无效，暂不出售。') from None
        if not rate.is_finite() or not 0 <= rate <= 1:
            raise SteamSessionError('Steam 游戏手续费率无效，暂不出售。')
    parameters = {key: wallet[key] for key in needed}
    if 'wallet_publisher_fee_percent_default' in wallet:
        parameters['wallet_publisher_fee_percent_default'] = wallet['wallet_publisher_fee_percent_default']
    return {'steamid': steamid, 'currency_id': currency, 'currency': CURRENCIES[currency],
            'wallet': parameters, 'publisher_fee': publisher_fee,
            'publisher_fee_source': publisher_source, 'verified_at': time.time()}


class _NoRedirect(HTTPRedirectHandler):
    # Steam checks market eligibility on a same-origin GET redirect before
    # returning the wallet page. Never replay an economic POST through it.
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() != 'GET' or req.data is not None:
            raise SteamSessionError('Steam 写入请求发生重定向，结果待核验；不会重复提交。')
        try:
            target = urlparse(newurl)
            secure = (target.scheme == 'https' and target.port in (None, 443)
                      and not target.username and not target.password
                      and not re.search(r'[\\\x00-\x20\x7f]', newurl))
            safe = secure and target.hostname == 'steamcommunity.com'
            if (secure and target.hostname == 'login.steampowered.com'
                    and target.path in ('/jwt/refresh', '/jwt/refresh/') and not target.fragment):
                returns = parse_qs(target.query, max_num_fields=32).get('redir', [])
                if len(returns) == 1:
                    source, back = urlparse(req.full_url), urlparse(returns[0])
                    if (source.scheme == back.scheme == 'https'
                            and source.hostname == back.hostname == 'steamcommunity.com'
                            and source.port in (None, 443) and back.port in (None, 443)
                            and not back.username and not back.password and not back.fragment
                            and not re.search(r'[\\\x00-\x20\x7f]', returns[0])
                            and (back.path, back.query) == (source.path, source.query)):
                        # Our saved Community access cookie cannot satisfy this
                        # request. Renew it through the official login window;
                        # never forward cookies to the other origin or follow.
                        raise SteamSessionError('Steam 登录凭证已过期，请重新登录。',
                                                status=401, auth_expired=True)
        except ValueError:
            safe = False
        if not safe:
            raise SteamSessionError('Steam 返回不支持的跳转地址，已停止核验。')
        if target.path == '/login' or target.path.startswith('/login/'):
            raise SteamSessionError('Steam 登录已过期，请重新登录。', status=401, auth_expired=True)
        allowed = ('/market/', '/market/eligibilitycheck/')
        if urlparse(req.full_url).path not in allowed or target.path not in allowed:
            raise SteamSessionError('Steam 返回不支持的页面跳转，已停止核验。')
        if sum(getattr(req, 'redirect_dict', {}).values()) >= self.max_redirections:
            raise SteamSessionError('Steam 市场资格检查跳转次数过多，暂不继续核验。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SteamSession:
    def __init__(self, path, *, transport=None, login_window_factory=None, protector=_dpapi, clock=time.time,
                 local_provider=None, local_verify_timeout=30):
        self.path = Path(path)
        self.transport = transport
        self.login_window_factory = login_window_factory
        self.protector = protector
        self.clock = clock
        self.guard = threading.RLock()
        self._cookies = {}
        self._wallet = None
        self._expired = False
        self._persisted_fingerprint = None
        self._window = None
        self._login_busy = False
        self._generation = 0
        self._window_epoch = 0
        self._closed = False
        self.local_provider = local_provider
        self._local_verify_timeout = local_verify_timeout
        self._local_request = None
        self._local_lock = threading.Lock()
        self._local_check_after = 0
        self._local_rejected = None
        self._local_rejected_until = 0
        self._local_renew_after = 0
        self._renewing = False
        self._local_suppressed = False
        self._local_identity = None
        self._local_status = 'pending' if local_provider else 'unavailable'
        self._local_message = ''
        self._source = 'none'
        self._message = '未绑定 Steam；绑定后仍可能需要手机确认。'
        if self.path.exists():
            try:
                if self.path.stat().st_size > 65536:
                    raise ValueError()
                data = json.loads(self.protector(self.path.read_bytes(), decrypt=True))
                values = _validated_values(data.get('cookies'))
                # Saved data is never considered verified until Steam is read.
                self._cookies = values
                self._source = 'saved_login'
                self._persisted_fingerprint = self._cookie_fingerprint(values)
                self._message = '已保存 Steam 绑定，等待核验登录状态。'
            except Exception:
                self._message = 'Steam 绑定无法解密；请用原 Windows 用户重新登录。'

    def state(self):
        with self.guard:
            wallet = self._wallet or {}
            # Temporary native renewal is not a user logout. Keep the binding
            # fence alive; verified=False and request() prohibit market writes.
            return {'bound': bool(self._cookies) and not self._expired,
                    'verified': bool(self._wallet) and not self._expired and not self._renewing,
                    'expired': self._expired, 'renewing': self._renewing,
                    'steamid': wallet.get('steamid'), 'wallet_currency': wallet.get('currency'),
                    'wallet_currency_id': wallet.get('currency_id'), 'login_open': self._login_busy,
                    'binding_generation': self._generation,
                    'message': self._message, 'live_verified': False,
                    'source': self._source, 'local_status': self._local_status,
                    'local_message': self._local_message}

    def refresh_local(self, *, force=False):
        """Worker-only discovery. Verify a candidate before replacing any saved login.

        Reads never create a session on disk and a rejected native cookie never
        deletes the user's manually saved login. No economic request is issued.
        """
        if not self.local_provider or not self._local_lock.acquire(blocking=False):
            return
        try:
            with self.guard:
                if (self._closed or self._local_suppressed or self._login_busy
                        or not force and self.clock() < self._local_check_after):
                    return
                self._local_check_after = self.clock() + 60
                generation = self._generation
            candidate = None
            renewal_failure = None
            try:
                from .steam_local_renewal import cookie_expiry
                try:
                    snapshot = self.local_provider.snapshot()
                except Exception:
                    # A cold/locked CEF cache is not a reason to require a new
                    # login when the active Steam client can renew over IPC.
                    identity = self.local_provider.current_identity()
                    snapshot = self._renew_local(identity, generation)
                values = _validated_values(dict(snapshot.cookies))
                expiry = cookie_expiry(values)
                if expiry is not None and expiry < self.clock() + 600 and self.clock() >= self._local_renew_after:
                    try:
                        snapshot = self._renew_local(snapshot.identity, generation)
                        values = _validated_values(dict(snapshot.cookies))
                    except Exception:
                        if expiry <= self.clock(): raise
                        # Still-valid cookies remain usable if an early renewal
                        # is delayed by an offline Steam CM connection.
                candidate = (snapshot.identity, self._cookie_fingerprint(values))
                with self.guard:
                    if (not force and candidate == self._local_rejected and self.clock() < self._local_rejected_until
                            and self.clock() < self._local_renew_after):
                        return  # Do not repeatedly delay a working manual fallback.
                steamid = unquote(values['steamLoginSecure']).split('||', 1)[0]
                try:
                    wallet = self._verify_native_wallet(values, steamid)
                except SteamSessionError as exc:
                    if not exc.auth_expired: raise
                    with self.guard:
                        self._local_rejected, self._local_rejected_until = candidate, self.clock() + 300
                        if (self._source == 'local_steam' and self._cookies == values
                                and generation == self._generation):
                            self._expire(generation)
                    # Authentication-only recovery. Never replay a failed POST;
                    # the next worker pass reconciles its existing transaction.
                    try:
                        snapshot = self._renew_local(snapshot.identity, generation)
                        values = _validated_values(dict(snapshot.cookies))
                    except Exception as failure:
                        renewal_failure = failure
                        raise exc from None
                    candidate = (snapshot.identity, self._cookie_fingerprint(values))
                    steamid = unquote(values['steamLoginSecure']).split('||', 1)[0]
                    wallet = self._verify_native_wallet(values, steamid)
                if self.local_provider.current_identity() != snapshot.identity:
                    raise SteamSessionError('本机 Steam 账号正在切换，暂不采用该会话。')
            except Exception as exc:
                from .steam_local_session import LocalSessionUnavailable
                with self.guard:
                    if generation != self._generation or self._closed:
                        return
                    if candidate and isinstance(exc, SteamSessionError) and exc.auth_expired:
                        self._local_rejected, self._local_rejected_until = candidate, self.clock() + 300
                    self._local_status = ('rejected' if isinstance(exc, SteamSessionError)
                                          and exc.auth_expired else 'unavailable')
                    self._local_message = (str(exc) if isinstance(exc, LocalSessionUnavailable) else
                        '本机 Steam 社区凭证已过期，请在 Steam 中打开社区续期，或使用助手登录。'
                        if isinstance(exc, SteamSessionError) and exc.auth_expired else
                        '本机 Steam 社区核验暂未完成，将自动重试；保留原有登录方式。')
                    if (self._source == 'local_steam' and isinstance(exc, SteamSessionError)
                            and exc.auth_expired):
                        self._expire(generation)
                    if self._renewing:
                        self._local_status = 'renewing'
                        self._message = '本机 Steam 社区凭证正在自动续期，期间暂停市场操作；无需重复登录。'
                        # A real account switch/logout is different from an
                        # expired web cookie. It revokes prepared transactions.
                        try: self._assert_local_identity()
                        except SteamSessionError: pass
                    failure = renewal_failure or exc
                    if (self._source == 'local_steam' and isinstance(failure, LocalSessionUnavailable)
                            and failure.retryable is False):
                        self._expired, self._renewing, self._wallet = True, False, None
                        self._local_status = 'unsupported'
                        self._message = self._local_message = str(failure)
                    # A changed native identity is revoked at every request
                    # boundary as well; do not silently switch an existing sale.
                return
            with self.guard:
                if (generation != self._generation or self._closed or self._local_suppressed
                        or self._login_busy):
                    return
                changed = (self._source != 'local_steam' or self._cookies != values
                           or self._local_identity != snapshot.identity or self._expired or self._renewing)
                if changed:
                    self._generation += 1
                self._cookies, self._local_identity = values, snapshot.identity
                self._source, self._expired, self._renewing = 'local_steam', False, False
                wallet['verified_at'], wallet['binding_generation'] = self.clock(), self._generation
                self._wallet = wallet
                self._local_rejected, self._local_rejected_until = None, 0
                self._local_status, self._local_message = 'verified', '已核验本机 Steam 社区账号与钱包。'
                self._message = '正在使用本机 Steam 登录态；市场操作使用助手配置的社区线路。'
        finally:
            self._local_lock.release()

    def _renew_local(self, identity, generation):
        from .steam_local_session import LocalSessionUnavailable
        with self.guard:
            if (generation != self._generation or self._closed or self._local_suppressed or self._login_busy
                    or identity.status != 'online' or self.clock() < self._local_renew_after):
                raise LocalSessionUnavailable('本机 Steam 自动续期等待重试，期间暂停市场操作。')
            # Bounded single-flight renewal; even force-refresh cannot spam the
            # CM auth service. Normal wallet GETs retain their own cooldowns.
            self._local_renew_after = self.clock() + 300
        snapshot = self.local_provider.renew(identity)
        if self.local_provider.current_identity() != identity:
            raise LocalSessionUnavailable('本机 Steam 账号已变化，未采用续期凭证。')
        return snapshot

    def _verify_native_wallet(self, values, steamid):
        """Bound discovery latency; late read replies cannot adopt or persist a login.

        urllib socket timeouts alone do not bound multiple redirects/body reads.
        At most one discovery GET may remain in flight after timeout. No economic
        request is sent here, and completed abandoned results are never reused.
        """
        if self._local_request and self._local_request[0].is_alive():
            raise SteamSessionError('本机 Steam 社区核验仍在结束中，暂用原登录方式。')
        completed, result = threading.Event(), {}
        def read():
            try:
                prepare = getattr(type(self.transport), 'prepare_local_verification', None)
                if prepare is not None:
                    prepare(self.transport)
                result['wallet'] = parse_wallet(self._request('GET', '/market/', cookies=values), steamid)
            except Exception as exc:
                result['error'] = exc
            finally:
                completed.set()
        thread = threading.Thread(target=read, name='Steam-native-wallet-read', daemon=True)
        self._local_request = (thread, completed)
        thread.start()
        if not completed.wait(self._local_verify_timeout):
            raise SteamSessionError('本机 Steam 社区核验超时，暂用原登录方式。')
        self._local_request = None
        if 'error' in result:
            raise result['error']
        return result['wallet']

    def _assert_local_identity(self):
        if self._source != 'local_steam' or self._expired:
            return
        try:
            same = self.local_provider.current_identity() == self._local_identity
        except Exception:
            same = False
        if not same:
            self._generation += 1
            self._expired, self._wallet = True, None
            self._renewing = False
            self._local_status = 'changed'
            self._message = '本机 Steam 已退出或切换账号，市场操作已暂停；等待重新核验。'
            raise SteamSessionError(self._message, status=401, auth_expired=True)

    def peek_state(self):
        # A sell POST pins this lock across submission to prevent account
        # switching. Status polling must not wait behind that network request.
        if not self.guard.acquire(blocking=False):
            return None
        try:
            return self.state()
        finally:
            self.guard.release()

    def logout(self):
        with self.guard:
            self._local_suppressed = True
            self._local_identity = None
            self._source = 'none'
            self._generation += 1
            self._window_epoch += 1
            window = self._window
            self._window = None
            self._login_busy = False
            self._cookies.clear()
            self._wallet = None
            self._expired = False
            self._renewing = False
            self._persisted_fingerprint = None
            self._message = '已解除本机 Steam 绑定。'
            self.path.unlink(missing_ok=True)
        if window:
            try:
                window.destroy()
            except Exception:
                pass
        return self.state()

    def request(self, method, path, data=None):
        with self.guard:
            self._assert_local_identity()
            if self._renewing:
                raise SteamSessionError('本机 Steam 社区凭证正在自动续期，暂不提交市场操作。')
            if self._expired:
                raise SteamSessionError('Steam 登录已过期，请重新登录。', status=401, auth_expired=True)
            if set(self._cookies) != COOKIE_NAMES:
                raise SteamSessionError('请先绑定 Steam 登录。', status=401)
            generation = self._generation
            cookies = deepcopy(self._cookies)
        try:
            return self._request(method, path, data, cookies=cookies)
        except SteamSessionError as exc:
            # Only Steam's own authentication rejection invalidates credentials;
            # local account/generation guards and network failures do not.
            if exc.status == 401 or exc.auth_expired:
                self._expire(generation)
            raise

    @staticmethod
    def _cookie_fingerprint(cookies):
        return sha256(json.dumps(cookies, sort_keys=True).encode('utf-8')).digest()

    def _expire(self, generation):
        with self.guard:
            if generation != self._generation:
                return
            self._renewing = self._source == 'local_steam' and self.local_provider is not None
            self._expired = not self._renewing
            self._wallet = None
            self._message = ('本机 Steam 社区凭证正在自动续期，期间暂停市场操作；无需重复登录。'
                if self._renewing else 'Steam 登录已过期，请重新登录；自动出售已取消，游戏拍卖行上传不受影响。')
            if self._renewing:
                self._local_check_after = 0
                self._local_status = 'renewing'
            if self._source != 'local_steam' and self._persisted_fingerprint == self._cookie_fingerprint(self._cookies):
                try:
                    self.path.unlink(missing_ok=True)
                    self._persisted_fingerprint = None
                except OSError:
                    pass  # Never mask the authentication result with a disk error.

    def _request(self, method, path, data=None, *, cookies):
        if (method not in ('GET', 'POST') or not path.startswith('/') or path.startswith('//')
                or '\\' in path or '\r' in path or '\n' in path):
            raise SteamSessionError('Steam 请求地址无效。')
        if self.transport:
            return self.transport(method, path, data, cookies)
        headers = {'User-Agent': 'Mozilla/5.0 DeskrawlAssistant',
                   'Referer': ORIGIN + '/market/', 'Origin': ORIGIN, 'Accept': 'application/json,text/html'}
        # Qualification cookies are received only in this request's temporary
        # jar. Authentication is still persisted solely by the native login
        # window, and every redirect is checked before cookies can be sent.
        jar = CookieJar()
        for name, value in cookies.items():
            jar.set_cookie(Cookie(0, name, value, None, False, 'steamcommunity.com',
                                  False, False, '/', True, True, None, True, None, None, {}))
        body = urlencode(data).encode('utf-8') if data is not None else None
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
        request = Request(ORIGIN + path, body, headers, method=method)
        try:
            # Match the official login window's configured system proxy. A
            # direct-only connection can time out even after browser login
            # succeeds. HTTPS certificate checks and the fixed-origin,
            # redirect policy still apply to authenticated requests.
            with build_opener(ProxyHandler(), _NoRedirect(), HTTPCookieProcessor(jar)).open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            retry = exc.headers.get('Retry-After', '')
            raise SteamSessionError(f'Steam 请求被限制（HTTP {exc.code}），请稍后重试。' if exc.code in (403, 429) else
                                    f'Steam 请求失败（HTTP {exc.code}），请重新核对登录状态。', status=exc.code,
                                    retry_after=min(3600, int(retry)) if retry.isdigit() else 0) from None
        except SteamSessionError:
            raise
        except Exception:
            raise SteamSessionError('Steam 网络请求未确认。') from None
        if len(raw) > MAX_RESPONSE:
            raise SteamSessionError('Steam 响应过大，暂不继续操作。')
        try:
            return raw.decode('utf-8')
        except UnicodeError:
            raise SteamSessionError('Steam 响应格式已变化。') from None

    def post(self, path, data, *, expected_steamid=None, expected_generation=None):
        with self.guard:
            self._assert_local_identity()
            if expected_generation is not None and expected_generation != self._generation:
                raise SteamSessionError('Steam 登录会话已变化，未继续提交。', status=401)
            steamid = unquote(self._cookies.get('steamLoginSecure', '')).split('||', 1)[0]
            if expected_steamid is not None and steamid != expected_steamid:
                raise SteamSessionError('Steam 登录账号已变化，未继续提交。', status=401)
            sessionid = self._cookies.get('sessionid')
            # Keep identity and cookies together until the request completes.
            # Login capture/logout cannot switch the account between validation
            # and a sell request.
            return self.request('POST', path, {**data, 'sessionid': sessionid})

    def verify(self, *, force=False):
        with self.guard:
            self._assert_local_identity()
            if self._wallet and not force and self.clock() - self._wallet['verified_at'] < 30:
                return deepcopy(self._wallet)
            cookie = self._cookies.get('steamLoginSecure', '')
            generation = self._generation
        steamid = unquote(cookie).split('||', 1)[0]
        if not re.fullmatch(r'7656\d{13}', steamid):
            raise SteamSessionError('请先绑定 Steam 登录。', status=401)
        try:
            wallet = parse_wallet(self.request('GET', '/market/'), steamid)
        except SteamSessionError as exc:
            if exc.auth_expired:
                self._expire(generation)
            with self.guard:
                if not self._expired and generation == self._generation and self._cookies.get('steamLoginSecure') == cookie:
                    self._wallet = None
                    self._message = str(exc)
            raise
        wallet['verified_at'] = self.clock()
        wallet['binding_generation'] = generation
        with self.guard:
            if generation != self._generation or self._cookies.get('steamLoginSecure') != cookie:
                raise SteamSessionError('Steam 登录绑定已变化，请重新核验。', status=401)
            self._wallet = wallet
            self._message = ('正在使用本机 Steam 登录态；市场操作使用助手配置的社区线路。'
                if self._source == 'local_steam' else 'Steam 登录和钱包已核验；实机出售尚待验证。')
        return deepcopy(wallet)

    def capture_session(self, cookies, *, window_epoch=None, verify_now=True):
        values = normalise_cookies(cookies)
        with self.guard:
            if (self._closed or window_epoch is not None
                    and (window_epoch != self._window_epoch or not self._login_busy)):
                raise SteamSessionError('Steam 登录窗口已关闭或取消，未保存绑定。')
            previous = self._cookies
            previous_expired = self._expired
            previous_renewing = self._renewing
            previous_source = self._source
            previous_suppressed = self._local_suppressed
            self._local_suppressed = True
            self._source = 'saved_login'
            self._generation += 1
            generation = self._generation
            self._cookies = values
            self._wallet = None
            self._expired = False
            self._renewing = False
        try:
            if verify_now:
                self.verify(force=True)
            encrypted = self.protector(json.dumps({'cookies': values}, ensure_ascii=False).encode('utf-8'))
            with self.guard:
                if (generation != self._generation or self._closed or window_epoch is not None
                        and (window_epoch != self._window_epoch or not self._login_busy)):
                    raise SteamSessionError('Steam 绑定已取消，请重新登录。')
                _atomic_write(self.path, encrypted)
                self._persisted_fingerprint = self._cookie_fingerprint(values)
                if not verify_now:
                    self._message = 'Steam 登录凭证已加密保存，正在后台核验账号与钱包；无需保持登录窗口打开。'
        except Exception:
            with self.guard:
                if generation == self._generation:
                    self._cookies = previous
                    self._wallet = None
                    self._expired = previous_expired
                    self._renewing = previous_renewing
                    self._source = previous_source
                    self._local_suppressed = previous_suppressed
                    if previous and not previous_expired:
                        self._message = '新登录暂未核验，已保留原绑定，等待重新核验。'
                elif self._cookies is values:
                    # Cancellation may change the generation while encryption
                    # is in progress. Discard only this uncommitted capture;
                    # never erase a newer concurrent binding.
                    self._cookies = {}
                    self._wallet = None
            raise
        return self.state()

    def login_window(self):
        """Called by the native desktop host; never expose capture to HTTP/JS."""
        if os.name != 'nt':
            raise SteamSessionError('Steam 登录绑定需要 Windows 独立窗口。')
        import webview
        with self.guard:
            if self._closed:
                raise SteamSessionError('助手正在退出，不能再打开 Steam 登录。')
            if self._login_busy:
                if self._window:
                    self._window.show()
                return self.state()
            self._login_busy = True
            self._window_epoch += 1
            epoch = self._window_epoch
        try:
            factory = self.login_window_factory or webview.create_window
            window = factory('绑定 Steam · 仅在官方页面登录', LOGIN_URL,
                                          js_api=None, width=1000, height=760,
                                          min_size=(740, 580), text_select=True)
            if window is None:
                raise SteamSessionError('Steam 登录窗口未打开。')
            with self.guard:
                obsolete = epoch != self._window_epoch or self._closed or not self._login_busy
                if not obsolete:
                    self._window = window
            if obsolete:
                window.destroy()
                return self.state()
            ended = threading.Event()
            def closed():
                ended.set()
                with self.guard:
                    if self._window is window:
                        self._generation += 1
                        # Closing also happens after a successful capture.
                        # The cached wallet belongs to the old generation;
                        # re-verify before preparing a new economic request.
                        self._wallet = None
                        self._window_epoch += 1
                        self._login_busy = False
                        self._window = None
            window.events.closed += closed
            def observe():
                next_attempt = 0
                while not ended.wait(2):
                    try:
                        with self.guard:
                            if self._window is not window or epoch != self._window_epoch or self._closed:
                                return
                        current = urlparse(window.get_current_url() or '')
                        if (current.scheme != 'https' or current.hostname != 'steamcommunity.com'
                                or current.port not in (None, 443) or current.username or current.password):
                            continue
                        if (current.path.startswith('/login')
                                and getattr(window, 'login_complete', False) is not True):
                            continue
                        cookies = window.get_cookies()
                        try:
                            normalise_cookies(cookies)
                        except SteamSessionError:
                            continue
                        if self.clock() < next_attempt:
                            continue
                        next_attempt = self.clock() + 30
                        # Saving an official login is separate from network
                        # verification. The Steam worker verifies the saved
                        # session after this window closes; no sale can proceed
                        # until account identity and wallet are verified.
                        self.capture_session(cookies, window_epoch=epoch, verify_now=False)
                        window.destroy()
                        return
                    except Exception as exc:
                        if isinstance(exc, SteamSessionError) and exc.status in (403, 429):
                            next_attempt = self.clock() + max(300, min(3600, exc.retry_after))
                        with self.guard:
                            if self._window is not window or epoch != self._window_epoch or self._closed:
                                return
                            self._message = (str(exc) if isinstance(exc, SteamSessionError) else
                                             'Steam 登录凭证未保存，请重新打开登录窗口。')
                        # A wallet may not be ready immediately after login.
                        # Keep observing until the window closes, without busy retries.
                        continue
            threading.Thread(target=observe, name='steam-native-login', daemon=True).start()
        except Exception:
            with self.guard:
                if epoch == self._window_epoch:
                    self._login_busy = False
            raise SteamSessionError('Steam 登录窗口无法打开，请在独立窗口助手中重试。') from None
        return self.state()

    def close(self):
        with self.guard:
            self._closed = True
            self._generation += 1
            self._window_epoch += 1
            window = self._window
            self._window = None
            self._login_busy = False
        if window:
            try:
                window.destroy()
            except Exception:
                pass
