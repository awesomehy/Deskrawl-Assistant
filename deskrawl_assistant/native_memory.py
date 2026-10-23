"""Windows external process reader. Requests read/query rights only, never writes."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
from pathlib import Path
import struct
import time

if os.name != "nt":
    raise ImportError("装备读取仅支持 Windows")

K = C.WinDLL("kernel32", use_last_error=True)
K.OpenProcess.argtypes = (W.DWORD, W.BOOL, W.DWORD)
K.OpenProcess.restype = W.HANDLE
K.CloseHandle.argtypes = (W.HANDLE,)
K.CloseHandle.restype = W.BOOL
K.ReadProcessMemory.argtypes = (W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t))
K.ReadProcessMemory.restype = W.BOOL
K.GetExitCodeProcess.argtypes = (W.HANDLE, C.POINTER(W.DWORD))
K.GetExitCodeProcess.restype = W.BOOL
K.QueryFullProcessImageNameW.argtypes = (W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
K.QueryFullProcessImageNameW.restype = W.BOOL
K.CreateToolhelp32Snapshot.argtypes = (W.DWORD, W.DWORD)
K.CreateToolhelp32Snapshot.restype = W.HANDLE
K.GetCurrentProcess.argtypes = ()
K.GetCurrentProcess.restype = W.HANDLE

A = C.WinDLL("advapi32", use_last_error=True)
A.OpenProcessToken.argtypes = (W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
A.OpenProcessToken.restype = W.BOOL
A.GetTokenInformation.argtypes = (W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
A.GetTokenInformation.restype = W.BOOL


class PROCESSENTRY32W(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
        ("th32DefaultHeapID", C.c_size_t), ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
        ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD), ("szExeFile", W.WCHAR * 260)]


class MODULEENTRY32W(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("th32ModuleID", W.DWORD), ("th32ProcessID", W.DWORD),
        ("GlblcntUsage", W.DWORD), ("ProccntUsage", W.DWORD), ("modBaseAddr", C.c_void_p),
        ("modBaseSize", W.DWORD), ("hModule", W.HMODULE), ("szModule", W.WCHAR * 256), ("szExePath", W.WCHAR * 260)]


class MEMORY_BASIC_INFORMATION(C.Structure):
    _fields_ = [("BaseAddress", C.c_void_p), ("AllocationBase", C.c_void_p), ("AllocationProtect", W.DWORD),
        ("PartitionId", W.WORD), ("RegionSize", C.c_size_t), ("State", W.DWORD), ("Protect", W.DWORD), ("Type", W.DWORD)]


K.Process32FirstW.argtypes = (W.HANDLE, C.POINTER(PROCESSENTRY32W))
K.Process32NextW.argtypes = K.Process32FirstW.argtypes
K.Module32FirstW.argtypes = (W.HANDLE, C.POINTER(MODULEENTRY32W))
K.Module32NextW.argtypes = K.Module32FirstW.argtypes
K.VirtualQueryEx.argtypes = (W.HANDLE, C.c_void_p, C.POINTER(MEMORY_BASIC_INFORMATION), C.c_size_t)
K.VirtualQueryEx.restype = C.c_size_t


@dataclass(frozen=True)
class Region:
    base: int
    size: int
    kind: int
    protection: int


class MemoryReadError(RuntimeError):
    pass


class SnapshotChangedError(MemoryReadError):
    """The live game changed a verified object while it was being copied."""


class GameBusyError(SnapshotChangedError):
    """A known temporary game gate blocked an action before any mutation."""


class OnlineSyncBusyError(GameBusyError):
    """Travel was not submitted because the game's online sync is still busy."""


class DeathRecoveryBusyError(GameBusyError):
    """A confirmed dead player is waiting for DeathUI before any submission."""


def _process_elevated(handle):
    """Best-effort read-only diagnosis; unknown privileges remain unknown."""
    token = W.HANDLE()
    if not A.OpenProcessToken(handle, 0x8, C.byref(token)):  # TOKEN_QUERY
        return None
    try:
        elevated, received = W.DWORD(), W.DWORD()
        if not A.GetTokenInformation(token, 20, C.byref(elevated), C.sizeof(elevated), C.byref(received)):
            return None
        if received.value != C.sizeof(elevated):
            return None
        return bool(elevated.value)
    finally:
        K.CloseHandle(token)


def process_access_diagnostics(pid):
    """Inspect elevation only; never request debug privileges or change access."""
    result = {'assistant_elevated': None, 'game_elevated': None}
    try:
        result['assistant_elevated'] = _process_elevated(K.GetCurrentProcess())
        handle = K.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if handle:
            try:
                result['game_elevated'] = _process_elevated(handle)
            finally:
                K.CloseHandle(handle)
    except (OSError, ValueError):
        pass
    return result


class ProcessAccessDeniedError(MemoryReadError):
    def __init__(self, pid, diagnostics):
        self.pid, self.winerror, self.diagnostics = pid, 5, diagnostics
        if diagnostics.get('game_elevated') is True and diagnostics.get('assistant_elevated') is False:
            help_text = '检测到游戏以管理员权限运行、助手未提权。请保存并退出助手，再右键以管理员身份运行助手后连接。'
        else:
            help_text = ('请确认 Steam、游戏和助手由同一 Windows 账户启动；若游戏以管理员身份运行，助手也需要相同权限。'
                         '权限一致仍失败时，请检查安全软件的拦截记录。')
        super().__init__(f'无法以只读方式打开游戏：[WinError 5] 拒绝访问（PID {pid}）。'
                         f'{help_text} 此错误发生在打开游戏进程时，与游戏文件的只读属性无关。')


def game_pids():
    snapshot = K.CreateToolhelp32Snapshot(2, 0)
    if snapshot == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        row = PROCESSENTRY32W()
        row.dwSize = C.sizeof(row)
        found = []
        success = K.Process32FirstW(snapshot, C.byref(row))
        while success:
            if row.szExeFile.casefold() == "deskrawl.exe":
                found.append(row.th32ProcessID)
            success = K.Process32NextW(snapshot, C.byref(row))
        error = C.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES is the only successful termination.
            raise C.WinError(error)
        return found
    finally:
        K.CloseHandle(snapshot)


class ProcessMemory:
    def __init__(self, pid: int):
        # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION | QUERY_LIMITED_INFORMATION.
        self.handle = K.OpenProcess(0x10 | 0x400 | 0x1000, False, pid)
        if not self.handle:
            error = C.get_last_error()
            if error == 5:
                raise ProcessAccessDeniedError(pid, process_access_diagnostics(pid))
            raise MemoryReadError(f"无法以只读方式打开游戏：{C.WinError(error)}（PID {pid}）")
        self.pid = pid
        try:
            length = W.DWORD(32768)
            value = C.create_unicode_buffer(length.value)
            if not K.QueryFullProcessImageNameW(self.handle, 0, value, C.byref(length)):
                raise C.WinError(C.get_last_error())
            self.path = Path(value.value)
            if self.path.name.casefold() != "deskrawl.exe":
                raise MemoryReadError("目标进程不是 Deskrawl")
            self.modules = self._modules()
        except Exception:
            self.close()
            raise

    def _modules(self):
        for attempt in range(3):
            snapshot = K.CreateToolhelp32Snapshot(0x8 | 0x10, self.pid)
            if snapshot != C.c_void_p(-1).value:
                break
            if C.get_last_error() != 24:
                raise C.WinError(C.get_last_error())
        if snapshot == C.c_void_p(-1).value:
            raise MemoryReadError("无法读取游戏模块列表")
        try:
            row = MODULEENTRY32W()
            row.dwSize = C.sizeof(row)
            found = {}
            success = K.Module32FirstW(snapshot, C.byref(row))
            while success:
                found[row.szModule.casefold()] = {"base": row.modBaseAddr, "size": row.modBaseSize, "path": row.szExePath}
                success = K.Module32NextW(snapshot, C.byref(row))
            return found
        finally:
            K.CloseHandle(snapshot)

    def is_running(self):
        code = W.DWORD()
        return bool(self.handle and K.GetExitCodeProcess(self.handle,C.byref(code)) and code.value==259)

    def read(self, address: int, size: int) -> bytes:
        if not self.handle or not isinstance(address, int) or not 0x10000 <= address < 0x7fffffffffff:
            raise MemoryReadError("无效或已断开的读取地址")
        if not isinstance(size, int) or not 0 <= size <= 64 * 1024 * 1024:
            raise MemoryReadError("读取长度超出限制")
        if not size:
            return b""
        output = C.create_string_buffer(size)
        received = C.c_size_t()
        if not K.ReadProcessMemory(self.handle, address, output, size, C.byref(received)) or received.value != size:
            raise MemoryReadError(f"游戏数据暂不可读，地址 0x{address:x}，系统错误 {C.get_last_error()}")
        return output.raw

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def i32(self, address):
        return struct.unpack("<i", self.read(address, 4))[0]

    def cstring(self, address, max_length=256):
        if not address:
            return ""
        # Avoid crossing an unmapped page merely to read a short metadata name.
        result = bytearray()
        pos = 0
        while pos < max_length:
            block = self.read(address + pos, min(32, max_length - pos, 4096 - ((address + pos) & 4095)))
            if b"\0" in block:
                result.extend(block.split(b"\0", 1)[0])
                return result.decode("utf-8")
            result.extend(block)
            pos += len(block)
        raise MemoryReadError("名称长度超出限制")

    def regions(self):
        address = 0
        while address < 0x7fffffffffff:
            row = MEMORY_BASIC_INFORMATION()
            size = K.VirtualQueryEx(self.handle, address, C.byref(row), C.sizeof(row))
            if not size or not row.RegionSize:
                break
            base = row.BaseAddress or 0
            if row.State == 0x1000 and not row.Protect & 0x100 and row.Protect & 0xff in {2, 4, 8, 0x20, 0x40, 0x80}:
                yield Region(base, row.RegionSize, row.Type, row.Protect)
            address = base + row.RegionSize

    def close(self):
        if self.handle:
            K.CloseHandle(self.handle)
            self.handle = None
