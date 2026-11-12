"""Ask the active Steam client for a short-lived web session through its IPC.

No password or refresh secret is read. Native calls run in a disposable helper,
not in Steam/game processes or the UI. The private ABI is enabled only for an
exact DLL reviewed locally; unknown Steam builds retain the normal login path.
The result remains untrusted until SteamSession verifies the market account.
"""
from __future__ import annotations

import base64
import ctypes as C
import hashlib
import json
import math
import multiprocessing
import os
import secrets
import time
from urllib.parse import quote, unquote

from .steam_client_profile import STEAM_ID_BASE, WindowsSteamProfile
from .steam_session import _validated_values

# Reviewed 64-bit SteamClient020 / ClientEngine005 / IClientUser dispatch layout.
# Do not guess new vtable slots after a Steam update.
SUPPORTED_DLLS = frozenset({'253e0a11a4415b38c78a42f7bdd6ecc426455f06c1ba6453a48090df3142dbbc'})


class NativeRenewalUnavailable(RuntimeError):
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def cookie_expiry(cookies):
    """Scheduling hint only. JWT claims alone never grant authentication."""
    try:
        value = unquote(cookies['steamLoginSecure'])
        _, token = value.split('||', 1)
        if not 1 <= len(token) <= 8192 or len(token.split('.')) != 3:
            return None
        claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
        if not isinstance(claims, dict):
            return None
        expiry = claims.get('exp')
        value = float(expiry) if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) else None
        return value if value is not None and math.isfinite(value) else None
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError):
        return None


def web_cookies(raw, steamid, *, now=None):
    """GetCurrentWebAuthToken returns `steamid||JWT`, not just the JWT."""
    try:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 8192:
            raise ValueError()
        subject, token = raw.split('||')
        if subject != steamid or len(token.split('.')) != 3:
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
        audience = claims.get('aud')
        if (claims.get('sub') != steamid or not isinstance(audience, list)
                or 'web' not in audience or 'derive' in audience):
            raise ValueError()
        result = _validated_values({'steamLoginSecure': quote(raw, safe=''), 'sessionid': secrets.token_hex(12)})
        expiry = cookie_expiry(result)
        if expiry is None or expiry < (time.time() if now is None else now) + 120:
            raise ValueError()
        return result
    except Exception:
        raise NativeRenewalUnavailable('本机 Steam 尚未返回有效的社区凭证。') from None


def _native_refresh(identity):
    if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
        raise NativeRenewalUnavailable('当前系统暂不支持本机 Steam 自动续期。', retryable=False)
    if WindowsSteamProfile().identity() != identity or identity.status != 'online':
        raise NativeRenewalUnavailable('本机 Steam 已退出或切换账号。')
    path = (identity.root / 'steamclient64.dll').resolve()
    if path.parent != identity.root.resolve() or not 0 < path.stat().st_size <= 128 * 1024 * 1024:
        raise NativeRenewalUnavailable('本机 Steam 接口暂无法核验。')
    with path.open('rb') as source:
        fingerprint = hashlib.file_digest(source, 'sha256').hexdigest()
    if fingerprint not in SUPPORTED_DLLS:
        raise NativeRenewalUnavailable('当前 Steam 版本的自动续期接口尚未适配，可使用助手登录。', retryable=False)
    dll = C.WinDLL(str(path), winmode=0x1100)  # Exact DLL + its directory + System32.
    dll.CreateInterface.argtypes, dll.CreateInterface.restype = (C.c_char_p, C.c_void_p), C.c_void_p
    client = dll.CreateInterface(b'SteamClient020', None)
    engine = dll.CreateInterface(b'CLIENTENGINE_INTERFACE_VERSION005', None)
    if not client or not engine:
        raise NativeRenewalUnavailable('本机 Steam 接口暂不可用。')

    def call(obj, slot, result, args, *values):
        if not obj:
            raise NativeRenewalUnavailable('本机 Steam 接口暂不可用。')
        table = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p))).contents
        return C.CFUNCTYPE(result, C.c_void_p, *args)(table[slot])(obj, *values)

    pipe = call(client, 0, C.c_int, ())
    user = 0
    try:
        if not pipe:
            raise NativeRenewalUnavailable('本机 Steam 尚未建立在线连接。')
        user = call(client, 2, C.c_int, (C.c_int,), pipe)
        public = call(client, 5, C.c_void_p, (C.c_int, C.c_int, C.c_char_p), user, pipe, b'SteamUser019')
        utils = call(client, 9, C.c_void_p, (C.c_int, C.c_char_p), pipe, b'SteamUtils009')
        if not user or not utils or not call(public, 1, C.c_bool, ()):
            raise NativeRenewalUnavailable('本机 Steam 暂未在线，将稍后重试续期。')
        native = call(engine, 8, C.c_void_p, (C.c_int, C.c_int), user, pipe)
        handle = call(native, 53, C.c_uint64, ())  # RequestWebAuthToken; auth only.
        if not handle:
            raise NativeRenewalUnavailable('本机 Steam 未接受续期请求。')
        deadline, failed = time.monotonic() + 10, C.c_bool()
        while not call(utils, 11, C.c_bool, (C.c_uint64, C.POINTER(C.c_bool)), handle, C.byref(failed)):
            if time.monotonic() >= deadline:
                raise NativeRenewalUnavailable('本机 Steam 续期暂未完成，将稍后重试。')
            time.sleep(.1)
        if failed.value:
            raise NativeRenewalUnavailable('本机 Steam 续期暂未完成，将稍后重试。')
        buffer = C.create_string_buffer(8192)
        while not call(native, 52, C.c_bool, (C.c_void_p, C.c_int), buffer, len(buffer)):
            if time.monotonic() >= deadline:
                raise NativeRenewalUnavailable('本机 Steam 社区凭证暂不可读。')
            time.sleep(.1)
        if WindowsSteamProfile().identity() != identity:
            raise NativeRenewalUnavailable('本机 Steam 账号已变化，未采用原凭证。')
        return web_cookies(buffer.value.decode('ascii'), str(STEAM_ID_BASE + identity.account))
    finally:
        if user:
            call(client, 4, None, (C.c_int, C.c_int), pipe, user)
        if pipe:
            call(client, 1, C.c_bool, (C.c_int,), pipe)


def _renew_child(connection, identity):
    try:
        if os.name == 'nt':
            C.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        connection.send({'cookies': _native_refresh(identity)})
    except NativeRenewalUnavailable as exc:
        connection.send({'message': str(exc), 'retryable': exc.retryable})
    except BaseException:
        # Native exceptions and paths must never reach logs/UI or a crash dialog.
        try: connection.send({'message': '本机 Steam 自动续期暂未完成，将稍后重试。'})
        except Exception: pass
    finally:
        connection.close()


def renew_native_web_session(identity, *, timeout=20):
    """Bound hangs/crashes to our own child; never terminate Steam or the game."""
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_renew_child, args=(send, identity), daemon=True,
                              name='Steam-web-session-renewal')
    try:
        process.start()
        send.close()
        if not receive.poll(timeout):
            raise NativeRenewalUnavailable('本机 Steam 续期超时，将稍后重试。')
        result = receive.recv()
        if not isinstance(result, dict):
            raise NativeRenewalUnavailable('本机 Steam 续期暂未完成。')
        if 'cookies' not in result:
            raise NativeRenewalUnavailable(result.get('message', '本机 Steam 续期暂未完成。'),
                                          retryable=result.get('retryable') is not False)
        values = _validated_values(result['cookies'])
        if unquote(values['steamLoginSecure']).split('||', 1)[0] != str(STEAM_ID_BASE + identity.account):
            raise NativeRenewalUnavailable('本机 Steam 续期账号不一致，未采用该凭证。')
        return values
    except NativeRenewalUnavailable:
        raise
    except Exception:
        raise NativeRenewalUnavailable('本机 Steam 自动续期暂未完成，将稍后重试。') from None
    finally:
        receive.close(); send.close()
        if process.pid:
            process.join(.2)
            if process.is_alive():
                process.terminate(); process.join(2)
            if not process.is_alive(): process.close()
