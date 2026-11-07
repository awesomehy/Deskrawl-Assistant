"""Read-only profile of the active local Steam client, independent of market auth.

Only PersonaName and a bounded cached PNG are exposed to the local UI. No login
credentials, account history, Steam IDs, or filesystem paths leave this module.
"""
from __future__ import annotations

import hashlib
import re
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

STEAM_ID_BASE = 76561197960265728
MAX_LOCAL_FILE = 1024 * 1024


@dataclass(frozen=True)
class ClientIdentity:
    status: str
    pid: int = 0
    created: int = 0
    account: int = 0
    root: Path | None = None


def _bounded_read(path):
    with path.open('rb') as source:
        data = source.read(MAX_LOCAL_FILE + 1)
    if len(data) > MAX_LOCAL_FILE:
        raise ValueError('Local Steam file too large')
    return data


def _keyvalues(text):
    """Strict, bounded parser for Steam's quoted loginusers.vdf format."""
    tokens = []
    i = 0
    while i < len(text):
        char = text[i]
        if char.isspace():
            i += 1
        elif text.startswith('//', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
        elif char in '{}':
            tokens.append((char, char))
            i += 1
        elif char == '"':
            i += 1
            value = []
            while i < len(text) and text[i] != '"':
                if text[i] == '\\' and i + 1 < len(text) and text[i + 1] in '\\"':
                    i += 1
                value.append(text[i])
                i += 1
            if i >= len(text):
                raise ValueError('Incomplete KeyValues string')
            tokens.append(('text', ''.join(value)))
            i += 1
        else:
            raise ValueError('Unsupported KeyValues token')
        if len(tokens) > 20000:
            raise ValueError('Too many KeyValues tokens')
    cursor = 0

    def pairs(depth=0):
        nonlocal cursor
        if depth > 8:
            raise ValueError('KeyValues nesting too deep')
        result = {}
        while cursor < len(tokens) and tokens[cursor][0] != '}':
            kind, key = tokens[cursor]
            cursor += 1
            if kind != 'text' or cursor == len(tokens):
                raise ValueError('Incomplete KeyValues pair')
            kind, value = tokens[cursor]
            cursor += 1
            if kind == '{':
                value = pairs(depth + 1)
                if cursor == len(tokens) or tokens[cursor][0] != '}':
                    raise ValueError('Incomplete KeyValues object')
                cursor += 1
            elif kind != 'text':
                raise ValueError('Invalid KeyValues value')
            key = key.casefold()
            if key in result:
                raise ValueError('Duplicate KeyValues key')
            result[key] = value
        return result

    result = pairs()
    if cursor != len(tokens):
        raise ValueError('Unexpected KeyValues closing brace')
    return result


class WindowsSteamProfile:
    @staticmethod
    def _active_process():
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess') as key:
                pid = winreg.QueryValueEx(key, 'pid')[0]
                try:
                    account = winreg.QueryValueEx(key, 'ActiveUser')[0]
                except FileNotFoundError:
                    account = None
        except FileNotFoundError:
            return 0, None
        return pid, account

    @staticmethod
    def _steam_running():
        import ctypes as C
        from .native_memory import K, PROCESSENTRY32W
        snapshot = K.CreateToolhelp32Snapshot(2, 0)
        if snapshot == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            row = PROCESSENTRY32W()
            row.dwSize = C.sizeof(row)
            found = K.Process32FirstW(snapshot, C.byref(row))
            while found:
                if row.szExeFile.casefold() == 'steam.exe':
                    return True
                found = K.Process32NextW(snapshot, C.byref(row))
            if C.get_last_error() != 18:
                raise C.WinError(C.get_last_error())
            return False
        finally:
            K.CloseHandle(snapshot)

    @staticmethod
    def _process(pid):
        import ctypes as C
        from ctypes import wintypes as W
        from .native_memory import K
        handle = K.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only.
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            size = W.DWORD(32768)
            name = C.create_unicode_buffer(size.value)
            if not K.QueryFullProcessImageNameW(handle, 0, name, C.byref(size)):
                raise C.WinError(C.get_last_error())
            path = Path(name.value)
            if path.name.casefold() != 'steam.exe' or str(path).startswith('\\\\'):
                raise ValueError('Active Steam process not verified')
            created, exited, kernel, user = (W.FILETIME() for _ in range(4))
            K.GetProcessTimes.argtypes = (W.HANDLE, *(C.POINTER(W.FILETIME),) * 4)
            K.GetProcessTimes.restype = W.BOOL
            if not K.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
                raise C.WinError(C.get_last_error())
            code = W.DWORD()
            if not K.GetExitCodeProcess(handle, C.byref(code)):
                raise C.WinError(C.get_last_error())
            if code.value != 259:
                raise C.WinError(1168)
            return path.parent, (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            K.CloseHandle(handle)

    def identity(self):
        pid, account = self._active_process()
        if type(pid) is not int or pid <= 0:
            return ClientIdentity('unknown' if self._steam_running() else 'not_running')
        try:
            root, created = self._process(pid)
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (6, 87, 1168):
                raise
            return ClientIdentity('unknown' if self._steam_running() else 'not_running')
        if type(account) is not int or not 0 <= account <= 0xffffffff:
            return ClientIdentity('unknown')
        return ClientIdentity('online' if account else 'logged_out', pid, created, account, root)

    def profile(self, identity):
        sid = str(STEAM_ID_BASE + identity.account)
        document = _keyvalues(_bounded_read(identity.root / 'config' / 'loginusers.vdf').decode('utf-8-sig'))
        users = document.get('users', {})
        user = users.get(sid, {}) if isinstance(users, dict) else {}
        # Never use MostRecent or AccountName: both may belong to a logged-out account.
        nickname = user.get('personaname', '') if isinstance(user, dict) else ''
        if not isinstance(nickname, str):
            nickname = ''
        nickname = ''.join(char for char in nickname if ord(char) >= 32 and ord(char) != 127).strip()[:512]
        avatar = b''
        try:
            data = _bounded_read(identity.root / 'config' / 'avatarcache' / (sid + '.png'))
            if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 33 and data[12:16] == b'IHDR':
                width, height = struct.unpack('>II', data[16:24])
                if 0 < width <= 1024 and 0 < height <= 1024:
                    avatar = data
        except (OSError, ValueError):
            pass  # An unavailable avatar must not hide the nickname.
        return nickname, avatar


class SteamClientProfile:
    def __init__(self, backend=None, clock=time.monotonic):
        self.backend = backend or WindowsSteamProfile()
        self.clock = clock
        self.guard = threading.Lock()
        self.checked = self.profile_checked = -float('inf')
        self.identity = None
        self.cached = {'status': 'loading', 'nickname': '', 'avatar_url': ''}
        self.image = b''
        self.image_version = ''

    def _clear(self, status):
        self.identity = None
        self.image = b''
        self.image_version = ''
        self.cached = {'status': status, 'nickname': '', 'avatar_url': ''}

    def state(self):
        with self.guard:
            now = self.clock()
            if now - self.checked < 2:
                return dict(self.cached)
            self.checked = now
            try:
                identity = self.backend.identity()
                if identity.status != 'online':
                    self._clear(identity.status)
                elif identity != self.identity or now - self.profile_checked >= 30:
                    try:
                        nickname, avatar = self.backend.profile(identity)
                    except (OSError, ValueError, UnicodeError):
                        nickname, avatar = '', b''
                    if self.backend.identity() != identity:
                        self._clear('unknown')  # Switched accounts while reading files.
                    else:
                        self.identity = identity
                        self.profile_checked = now
                        self.image = avatar
                        self.image_version = hashlib.sha256(avatar).hexdigest() if avatar else ''
                        self.cached = {'status': 'online' if nickname else 'profile_unavailable',
                            'nickname': nickname, 'avatar_url': '/api/steam-client/avatar?v=' + self.image_version if avatar else ''}
            except (OSError, ValueError, ImportError):
                self._clear('unknown')
            return dict(self.cached)

    def avatar(self, version):
        if not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{64}', version):
            return None
        self.state()
        with self.guard:
            return self.image if self.image and version == self.image_version else None
