"""Local Steam Community login, with no credential bridge to the assistant UI.

Cookies are captured only by the native login window, stored with current-user
Windows DPAPI, and sent only to the exact HTTPS Steam Community origin.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler

from .steam_market import APP_ID, CURRENCIES

ORIGIN = 'https://steamcommunity.com'
LOGIN_URL = ORIGIN + '/login/home/?goto=market%2F'
MAX_RESPONSE = 4 * 1024 * 1024
COOKIE_NAMES = frozenset(('steamLoginSecure', 'sessionid'))


class SteamSessionError(RuntimeError):
    def __init__(self, message, *, status=0, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


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
    identity = re.search(r'(?:var\s+)?g_steamID\s*=\s*[\'\"](7656\d{13})[\'\"]', document)
    wallet = _assignment(document, 'g_rgWalletInfo')
    apps = _assignment(document, 'g_rgAppContextData') or {}
    if not identity or identity.group(1) != steamid or not isinstance(wallet, dict):
        raise SteamSessionError('Steam 登录已失效或钱包身份无法确认，请重新登录。', status=401)
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
            safe = (target.scheme == 'https' and target.hostname == 'steamcommunity.com'
                    and target.port in (None, 443) and not target.username and not target.password
                    and not re.search(r'[\\\x00-\x20\x7f]', newurl))
        except ValueError:
            safe = False
        if not safe:
            raise SteamSessionError('Steam 返回不支持的跳转地址，已停止核验。')
        if target.path == '/login' or target.path.startswith('/login/'):
            raise SteamSessionError('Steam 登录已失效，请重新登录。', status=401)
        allowed = ('/market/', '/market/eligibilitycheck/')
        if urlparse(req.full_url).path not in allowed or target.path not in allowed:
            raise SteamSessionError('Steam 返回不支持的页面跳转，已停止核验。')
        if sum(getattr(req, 'redirect_dict', {}).values()) >= self.max_redirections:
            raise SteamSessionError('Steam 市场资格检查跳转次数过多，暂不继续核验。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SteamSession:
    def __init__(self, path, *, transport=None, protector=_dpapi, clock=time.time):
        self.path = Path(path)
        self.transport = transport
        self.protector = protector
        self.clock = clock
        self.guard = threading.RLock()
        self._cookies = {}
        self._wallet = None
        self._window = None
        self._login_busy = False
        self._generation = 0
        self._window_epoch = 0
        self._closed = False
        self._message = '未绑定 Steam；绑定后仍可能需要手机确认。'
        if self.path.exists():
            try:
                if self.path.stat().st_size > 65536:
                    raise ValueError()
                data = json.loads(self.protector(self.path.read_bytes(), decrypt=True))
                values = _validated_values(data.get('cookies'))
                # Saved data is never considered verified until Steam is read.
                self._cookies = values
                self._message = '已保存 Steam 绑定，等待核验登录状态。'
            except Exception:
                self._message = 'Steam 绑定无法解密；请用原 Windows 用户重新登录。'

    def state(self):
        with self.guard:
            wallet = self._wallet or {}
            return {'bound': bool(self._cookies), 'verified': bool(self._wallet),
                    'steamid': wallet.get('steamid'), 'wallet_currency': wallet.get('currency'),
                    'wallet_currency_id': wallet.get('currency_id'), 'login_open': self._login_busy,
                    'binding_generation': self._generation,
                    'message': self._message, 'live_verified': False}

    def logout(self):
        with self.guard:
            self._generation += 1
            self._window_epoch += 1
            window = self._window
            self._window = None
            self._login_busy = False
            self._cookies.clear()
            self._wallet = None
            self._message = '已解除本机 Steam 绑定。'
            self.path.unlink(missing_ok=True)
        if window:
            try:
                window.destroy()
            except Exception:
                pass
        return self.state()

    def request(self, method, path, data=None):
        if (method not in ('GET', 'POST') or not path.startswith('/') or path.startswith('//')
                or '\\' in path or '\r' in path or '\n' in path):
            raise SteamSessionError('Steam 请求地址无效。')
        with self.guard:
            if set(self._cookies) != COOKIE_NAMES:
                raise SteamSessionError('请先绑定 Steam 登录。', status=401)
            cookies = deepcopy(self._cookies)
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
            with self.guard:
                if generation == self._generation and self._cookies.get('steamLoginSecure') == cookie:
                    self._wallet = None
                    self._message = str(exc)
            raise
        wallet['verified_at'] = self.clock()
        wallet['binding_generation'] = generation
        with self.guard:
            if generation != self._generation or self._cookies.get('steamLoginSecure') != cookie:
                raise SteamSessionError('Steam 登录绑定已变化，请重新核验。', status=401)
            self._wallet = wallet
            self._message = 'Steam 登录和钱包已核验；实机出售尚待验证。'
        return deepcopy(wallet)

    def capture_session(self, cookies, *, window_epoch=None, verify_now=True):
        values = normalise_cookies(cookies)
        with self.guard:
            if (self._closed or window_epoch is not None
                    and (window_epoch != self._window_epoch or not self._login_busy)):
                raise SteamSessionError('Steam 登录窗口已关闭或取消，未保存绑定。')
            previous = self._cookies
            self._generation += 1
            generation = self._generation
            self._cookies = values
            self._wallet = None
        try:
            if verify_now:
                self.verify(force=True)
            encrypted = self.protector(json.dumps({'cookies': values}, ensure_ascii=False).encode('utf-8'))
            with self.guard:
                if (generation != self._generation or self._closed or window_epoch is not None
                        and (window_epoch != self._window_epoch or not self._login_busy)):
                    raise SteamSessionError('Steam 绑定已取消，请重新登录。')
                _atomic_write(self.path, encrypted)
                if not verify_now:
                    self._message = 'Steam 登录凭证已加密保存，正在后台核验账号与钱包；无需保持登录窗口打开。'
        except Exception:
            with self.guard:
                if generation == self._generation:
                    self._cookies = previous
                    self._wallet = None
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
            window = webview.create_window('绑定 Steam · 仅在官方页面登录', LOGIN_URL,
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
                        if current.path.startswith('/login'):
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
