"""Read the active Windows Steam Community session without changing Steam.

Only the two Community authentication cookies are decrypted, in memory. No
Steam password/refresh token, other account, process memory, proxy setting or
on-disk cookie is modified. A snapshot is NOT proof of a valid market login;
SteamSession must verify the account and wallet with Steam before adopting it.
"""
from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .steam_client_profile import WindowsSteamProfile, STEAM_ID_BASE
from .steam_session import _dpapi, _validated_values

MAX_DATABASE = 16 * 1024 * 1024


class LocalSessionUnavailable(RuntimeError):
    """A fixed diagnostic, never an underlying exception containing secrets."""


@dataclass(frozen=True)
class LocalSessionSnapshot:
    identity: object
    cookies: dict = field(repr=False)


def _cookie_values(raw, key, decrypt):
    if not raw.startswith(b'SQLite format 3\0') or len(raw) > MAX_DATABASE:
        raise LocalSessionUnavailable('本机 Steam 社区缓存格式暂不支持。')
    try:
        with closing(sqlite3.connect(':memory:')) as db:
            db.deserialize(raw)
            db.execute('PRAGMA query_only=ON')
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
            version = int(db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
            if not 1 <= version <= 24:
                raise ValueError()
            rows = db.execute("SELECT host_key,name,path,is_secure,encrypted_value FROM cookies "
                "WHERE host_key IN ('.steamcommunity.com','steamcommunity.com') "
                "AND name IN ('steamLoginSecure','sessionid') LIMIT 9").fetchall()
        result = {}
        for host, name, path, secure, encrypted in rows:
            if path != '/' or name == 'steamLoginSecure' and secure != 1:
                raise ValueError()
            if not isinstance(encrypted, bytes) or not 31 < len(encrypted) <= 8192:
                raise ValueError()
            value = decrypt(key, encrypted)
            if version >= 24:
                if value[:32] != hashlib.sha256(host.encode()).digest():
                    raise ValueError()
                value = value[32:]
            value = value.decode('utf-8')
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError()
            if name in result and result[name] != value:
                raise ValueError()
            result[name] = value
        return _validated_values(result)
    except Exception:
        raise LocalSessionUnavailable('本机 Steam 社区会话暂不可读，请在 Steam 中打开社区后重试。') from None


def _aes_gcm(key, value):
    """Windows CNG; authenticate the tag before returning any plaintext."""
    import ctypes as C
    from ctypes import wintypes as W
    if len(key) != 32 or value[:3] != b'v10' or len(value) <= 31:
        raise ValueError('Unsupported native cookie encryption')
    class Auth(C.Structure):
        _fields_ = [('size', W.ULONG), ('version', W.ULONG), ('nonce', C.c_void_p),
            ('nonce_size', W.ULONG), ('aad', C.c_void_p), ('aad_size', W.ULONG),
            ('tag', C.c_void_p), ('tag_size', W.ULONG), ('mac', C.c_void_p),
            ('mac_size', W.ULONG), ('aad_count', W.ULONG), ('data_count', C.c_uint64), ('flags', W.ULONG)]
    b = C.WinDLL('bcrypt')
    ptr = C.c_void_p
    b.BCryptOpenAlgorithmProvider.argtypes = [C.POINTER(ptr), W.LPCWSTR, W.LPCWSTR, W.ULONG]
    b.BCryptSetProperty.argtypes = [ptr, W.LPCWSTR, ptr, W.ULONG, W.ULONG]
    b.BCryptGenerateSymmetricKey.argtypes = [ptr, C.POINTER(ptr), ptr, W.ULONG, ptr, W.ULONG, W.ULONG]
    b.BCryptDecrypt.argtypes = [ptr, ptr, W.ULONG, ptr, ptr, W.ULONG, ptr, W.ULONG, C.POINTER(W.ULONG), W.ULONG]
    b.BCryptDestroyKey.argtypes = [ptr]
    b.BCryptCloseAlgorithmProvider.argtypes = [ptr, W.ULONG]
    algorithm, handle = ptr(), ptr()
    def check(status):
        if status:
            raise ValueError('Native cookie authentication failed')
    check(b.BCryptOpenAlgorithmProvider(C.byref(algorithm), 'AES', None, 0))
    try:
        mode = C.create_unicode_buffer('ChainingModeGCM')
        check(b.BCryptSetProperty(algorithm, 'ChainingMode', mode, C.sizeof(mode), 0))
        secret = C.create_string_buffer(key)
        check(b.BCryptGenerateSymmetricKey(algorithm, C.byref(handle), None, 0, secret, len(key), 0))
        try:
            nonce, tag = C.create_string_buffer(value[3:15]), C.create_string_buffer(value[-16:])
            source, output = C.create_string_buffer(value[15:-16]), C.create_string_buffer(len(value))
            count = W.ULONG()
            info = Auth()
            info.size, info.version = C.sizeof(info), 1
            info.nonce, info.nonce_size = C.cast(nonce, ptr), 12
            info.tag, info.tag_size = C.cast(tag, ptr), 16
            check(b.BCryptDecrypt(handle, source, len(value) - 31, C.byref(info), None, 0,
                                  output, len(output), C.byref(count), 0))
            return output.raw[:count.value]
        finally:
            b.BCryptDestroyKey(handle)
    finally:
        b.BCryptCloseAlgorithmProvider(algorithm, 0)


class WindowsCommunityCache:
    """Read a locked CEF file via a read-only mapping, never a shared file cursor.

    Duplicating an existing file handle avoids closing Steam or copying its
    credential database to disk. Handle discovery is restricted to verified
    Steam webhelpers descended from the active Steam process, and to one exact
    cache pathname. No process memory is read and no debug privilege is enabled.
    """
    def __init__(self, identity):
        self.identity = identity
        self.root = Path(os.environ['LOCALAPPDATA']) / 'Steam' / 'htmlcache'

    @staticmethod
    def _processes():
        import ctypes as C
        from .native_memory import K, PROCESSENTRY32W
        snapshot = K.CreateToolhelp32Snapshot(2, 0)
        if snapshot == C.c_void_p(-1).value:
            raise OSError()
        try:
            row, rows = PROCESSENTRY32W(), {}
            row.dwSize = C.sizeof(row)
            found = K.Process32FirstW(snapshot, C.byref(row))
            while found:
                rows[row.th32ProcessID] = (row.th32ParentProcessID, row.szExeFile)
                found = K.Process32NextW(snapshot, C.byref(row))
            return rows
        finally:
            K.CloseHandle(snapshot)

    def _helpers(self):
        rows = self._processes()
        for pid, (parent, name) in rows.items():
            if name.casefold() != 'steamwebhelper.exe':
                continue
            seen = {pid}
            while parent in rows and parent not in seen:
                if parent == self.identity.pid:
                    yield pid
                    break
                seen.add(parent)
                parent = rows[parent][0]

    def _snapshot(self, pid, target):
        import ctypes as C
        from ctypes import wintypes as W
        k, nt = C.WinDLL('kernel32', use_last_error=True), C.WinDLL('ntdll')
        k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
        k.CloseHandle.argtypes = [W.HANDLE]
        k.GetCurrentProcess.restype = W.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        k.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
        k.GetFileType.argtypes, k.GetFileType.restype = [W.HANDLE], W.DWORD
        k.GetFinalPathNameByHandleW.argtypes = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD]
        k.CreateFileMappingW.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, W.DWORD, W.DWORD, W.LPCWSTR]
        k.CreateFileMappingW.restype = W.HANDLE
        k.MapViewOfFile.argtypes, k.MapViewOfFile.restype = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t], C.c_void_p
        k.UnmapViewOfFile.argtypes = [C.c_void_p]
        k.GetFileSizeEx.argtypes = [W.HANDLE, C.POINTER(C.c_int64)]
        nt.NtQueryInformationProcess.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
        nt.NtQueryInformationProcess.restype = C.c_long
        class Entry(C.Structure):
            _fields_ = [('handle', W.HANDLE), ('count', C.c_size_t), ('pointers', C.c_size_t),
                ('access', W.DWORD), ('type', W.DWORD), ('attributes', W.DWORD), ('reserved', W.DWORD)]
        process = k.OpenProcess(0x440, False, pid)  # Query + duplicate file handle; no VM/debug access.
        if not process:
            return None
        try:
            size, name = W.DWORD(32768), C.create_unicode_buffer(32768)
            if not k.QueryFullProcessImageNameW(process, 0, name, C.byref(size)):
                return None
            executable = Path(name.value).resolve()
            if (executable.name.casefold() != 'steamwebhelper.exe'
                    or not executable.is_relative_to((self.identity.root / 'bin' / 'cef').resolve())):
                return None
            buffer, needed = C.create_string_buffer(4 * 1024 * 1024), W.DWORD()
            if nt.NtQueryInformationProcess(process, 51, buffer, len(buffer), C.byref(needed)) != 0:
                return None
            count, start = C.c_size_t.from_buffer(buffer).value, 2 * C.sizeof(C.c_size_t)
            if count > 50000 or start + count * C.sizeof(Entry) > len(buffer):
                return None
            for index in range(count):
                entry = Entry.from_buffer(buffer, start + index * C.sizeof(Entry))
                if not entry.access & 1:
                    continue
                handle = W.HANDLE()
                if not k.DuplicateHandle(process, entry.handle, k.GetCurrentProcess(), C.byref(handle), 0, False, 2):
                    continue
                try:
                    if k.GetFileType(handle) != 1:
                        continue
                    path = C.create_unicode_buffer(32768)
                    length = k.GetFinalPathNameByHandleW(handle, path, len(path), 0)
                    if not 0 < length < len(path) or path.value.removeprefix('\\\\?\\').casefold() != str(target).casefold():
                        continue
                    length = C.c_int64()
                    if not k.GetFileSizeEx(handle, C.byref(length)) or not 0 < length.value <= MAX_DATABASE:
                        return None
                    mapping = k.CreateFileMappingW(handle, None, 2, 0, 0, None)  # PAGE_READONLY.
                    if not mapping:
                        return None
                    try:
                        view = k.MapViewOfFile(mapping, 4, 0, 0, length.value)  # FILE_MAP_READ.
                        if not view:
                            return None
                        try:
                            first = C.string_at(view, length.value)
                            # Fail closed on writes during capture; never SQLite-open the live file.
                            return first if first == C.string_at(view, length.value) else None
                        finally:
                            k.UnmapViewOfFile(view)
                    finally:
                        k.CloseHandle(mapping)
                finally:
                    k.CloseHandle(handle)
        finally:
            k.CloseHandle(process)
        return None

    def read(self):
        target = (self.root / 'Default' / 'Network' / 'Cookies').resolve()
        def quiet():
            for suffix in ('-wal', '-journal'):
                side = Path(str(target) + suffix)
                if side.exists() and side.stat().st_size:
                    raise LocalSessionUnavailable('Steam 社区会话正在更新，稍后重新读取。')
        quiet()
        raw = None
        for pid in self._helpers():
            raw = self._snapshot(pid, target)
            if raw:
                break
        quiet()
        if not raw:
            raise LocalSessionUnavailable('本机 Steam 社区会话暂不可读。')
        with (self.root / 'Local State').open('rb') as source:
            state = source.read(1024 * 1024 + 1)
        if len(state) > 1024 * 1024:
            raise LocalSessionUnavailable('Steam 社区缓存配置格式暂不支持。')
        encrypted = base64.b64decode(json.loads(state)['os_crypt']['encrypted_key'], validate=True)
        if not encrypted.startswith(b'DPAPI'):
            raise LocalSessionUnavailable('Steam 社区缓存加密方式暂不支持。')
        key = _dpapi(encrypted[5:], decrypt=True)
        return _cookie_values(raw, key, _aes_gcm)


class LocalSteamSessionProvider:
    def __init__(self, backend=None, cache_factory=WindowsCommunityCache):
        self.backend = backend or WindowsSteamProfile()
        self.cache_factory = cache_factory

    def current_identity(self):
        return self.backend.identity()

    def snapshot(self):
        try:
            identity = self.current_identity()
            if identity.status != 'online':
                raise LocalSessionUnavailable('本机 Steam 未运行或未登录。')
            values = self.cache_factory(identity).read()
            from urllib.parse import unquote
            if unquote(values['steamLoginSecure']).split('||', 1)[0] != str(STEAM_ID_BASE + identity.account):
                raise LocalSessionUnavailable('本机 Steam 社区与当前账号不一致，等待 Steam 更新会话。')
            if self.current_identity() != identity:
                raise LocalSessionUnavailable('本机 Steam 账号正在切换，稍后重试。')
            return LocalSessionSnapshot(identity, values)
        except LocalSessionUnavailable:
            raise
        except Exception:
            raise LocalSessionUnavailable('本机 Steam 社区会话暂不可读。') from None
