"""Narrow Windows hosts lease. The elevated child cannot accept paths or IPs.

It only maps Steam Community to the local TLS bridge, and restores that entry
when the owning process exits or signals stop. No Steam process is restarted.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

HOST = 'steamcommunity.com'
BEGIN = b'# Deskrawl Steam Community temporary acceleration '
END = b'# End Deskrawl Steam Community temporary acceleration'


class NativeAccelerationError(OSError):
    """Allowlisted diagnostics; never expose system file or credential contents."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def recover_orphaned_hosts(raw, nonce, lease_exists):
    """Called only while the elevated watcher owns the global hosts mutex.

    An exact old assistant block with no surviving lease events can be replaced.
    Foreign mappings, altered blocks and live/starting leases remain untouched.
    The returned baseline deliberately excludes the abandoned loopback mapping,
    so shutdown cannot restore a route to a listener which no longer exists.
    """
    starts = re.findall(rb'^' + re.escape(BEGIN) + rb'([a-f0-9]{32})\r?$', raw, re.M)
    if len(starts) != 1 or starts[0].decode() == nonce:
        return raw
    previous = starts[0].decode()
    # Validate the entire owned block before considering recovery.
    clean = hosts_edit(raw, previous, False)
    if lease_exists(previous):
        raise NativeAccelerationError('hosts_conflict', '另一份助手仍在使用本机 Steam 加速，未覆盖其配置。')
    return clean


def hosts_edit(raw, nonce, enabled):
    if not re.fullmatch('[a-f0-9]{32}', nonce): raise ValueError('Invalid lease')
    if b'\x00' in raw: raise ValueError('hosts 格式无法确认，未修改。')
    marker = BEGIN + nonce.encode()
    lines = raw.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip(b'\r\n').startswith(BEGIN)]
    ends = [i for i, line in enumerate(lines) if line.rstrip(b'\r\n') == END]
    if starts or ends:
        if (len(starts) != 1 or len(ends) != 1 or ends[0] != starts[0] + 2
                or lines[starts[0]].rstrip(b'\r\n') != marker
                or lines[starts[0] + 1].strip() != b'127.0.0.1 steamcommunity.com'):
            raise ValueError('Steam 社区已有其他加速配置，未覆盖。')
        if enabled: return raw
        return b''.join(lines[:starts[0]] + lines[ends[0] + 1:])
    if not enabled: return raw
    for line in lines:
        fields = line.split(b'#', 1)[0].lower().split()
        if HOST.encode() in [field.rstrip(b'.') for field in fields[1:]]:
            raise ValueError('Steam 社区已有 hosts 映射，未覆盖。')
    newline = b'\r\n' if b'\r\n' in raw or not raw else b'\n'
    return raw + (newline if raw and not raw.endswith((b'\r', b'\n')) else b'') + marker + newline + b'127.0.0.1 steamcommunity.com' + newline + END + newline


def _kernel():
    dll = ctypes.WinDLL('kernel32', use_last_error=True)
    dll.CreateEventW.argtypes = (ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR)
    dll.CreateEventW.restype = w.HANDLE
    dll.OpenEventW.argtypes = (w.DWORD, w.BOOL, w.LPCWSTR)
    dll.OpenEventW.restype = w.HANDLE
    dll.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    dll.OpenProcess.restype = w.HANDLE
    dll.CreateMutexW.argtypes = (ctypes.c_void_p, w.BOOL, w.LPCWSTR)
    dll.CreateMutexW.restype = w.HANDLE
    dll.WaitForSingleObject.argtypes = (w.HANDLE, w.DWORD)
    dll.WaitForSingleObject.restype = w.DWORD
    dll.SetEvent.argtypes = (w.HANDLE,)
    dll.CloseHandle.argtypes = (w.HANDLE,)
    dll.ReleaseMutex.argtypes = (w.HANDLE,)
    dll.GetProcessTimes.argtypes = (w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
                                   ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME))
    return dll


def _created(kernel, handle):
    values = [w.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)): raise OSError()
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def _event_name(nonce, suffix):
    return 'Local\\Deskrawl.SteamCommunity.' + nonce + '.' + suffix


def _lease_exists(kernel, nonce):
    for suffix in ('ready', 'stop', 'done'):
        handle = kernel.OpenEventW(0x100000, False, _event_name(nonce, suffix))
        if handle:
            kernel.CloseHandle(handle)
            return True
        if ctypes.get_last_error() != 2:  # Access denied/unknown is not proof of absence.
            return True
    return False


def _owner_sid(process):
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
    api.GetTokenInformation.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD))
    api.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(w.LPWSTR))
    token, size, result = w.HANDLE(), w.DWORD(), w.LPWSTR()
    if not api.OpenProcessToken(process, 8, ctypes.byref(token)): raise OSError()
    try:
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 1 <= size.value <= 65536: raise OSError()
        raw = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, raw, size, ctypes.byref(size)): raise OSError()
        sid = ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p))[0]
        if not api.ConvertSidToStringSidW(sid, ctypes.byref(result)): raise OSError()
        return result.value
    finally:
        kernel = _kernel()
        kernel.CloseHandle(token)
        if result:
            kernel.LocalFree.argtypes = (ctypes.c_void_p,)
            kernel.LocalFree(ctypes.cast(result, ctypes.c_void_p))


def _hosts_path():
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)): raise OSError()
    return Path(buffer.value) / 'drivers/etc/hosts'


def _flush():
    ctypes.windll.dnsapi.DnsFlushResolverCache()


def hosts_worker(pid, created, nonce, fingerprint):
    """Dedicated, hidden elevated entry; no general command or file endpoint."""
    if (os.name != 'nt' or not re.fullmatch('[a-f0-9]{32}', nonce)
            or not re.fullmatch('[a-f0-9]{64}', fingerprint)
            or not 1 <= pid <= 0xffffffff or not 1 <= created < 1 << 64): return 1
    kernel = _kernel()
    handles, events, owner, mutex, acquired = [], {}, None, None, False
    original = applied = owner_sid = None
    write_started = False
    try:
        for suffix in ('ready', 'stop', 'done'):
            events[suffix] = kernel.OpenEventW(0x100002, False, _event_name(nonce, suffix))
            if not events[suffix]: return 1
            handles.append(events[suffix])
        owner = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not owner: return 1
        handles.append(owner)
        if _created(kernel, owner) != created: return 1
        owner_sid = _owner_sid(owner)
        mutex = kernel.CreateMutexW(None, False, 'Global\\Deskrawl.SteamCommunity.HostsLease')
        if not mutex: return 1
        handles.append(mutex)
        if kernel.WaitForSingleObject(mutex, 0) not in (0, 0x80): return 1
        acquired = True
        if kernel.WaitForSingleObject(owner, 0) == 0 or kernel.WaitForSingleObject(events['stop'], 0) == 0:
            return 1
        path = _hosts_path()
        if path.is_symlink() or path.stat().st_size > 1024 * 1024: return 1
        observed = path.read_bytes()
        original = recover_orphaned_hosts(observed, nonce, lambda old: _lease_exists(kernel, old))
        applied = hosts_edit(original, nonce, True)
        # Preserve the existing file's ACL. Never replace the system file with
        # a user-writable temporary file or accept a caller-selected path.
        with path.open('r+b') as stream:
            if stream.read() != observed: return 1
            if kernel.WaitForSingleObject(events['stop'], 0) == 0: return 1
            write_started = True
            stream.seek(0); stream.write(applied); stream.truncate(); stream.flush(); os.fsync(stream.fileno())
        _flush()
        kernel.SetEvent(events['ready'])
        while kernel.WaitForSingleObject(owner, 200) != 0:
            if kernel.WaitForSingleObject(events['stop'], 0) == 0: break
        return 0
    except Exception:
        return 1  # No host file contents, paths or exception text are logged.
    finally:
        # Antivirus/editor locks can briefly prevent restoration. The narrow
        # elevated watcher may finish cleanup even after its owner has exited.
        # Keep temporary trust until hosts no longer points at the bridge.
        # The certificate cleanup routine retains reusable DPAPI-backed leaves.
        for attempt in range(6):
            cleaned = True
            if write_started:
                try:
                    path = _hosts_path()
                    with path.open('r+b') as stream:
                        current = stream.read()
                        restored = original if current == applied else hosts_edit(current, nonce, False)
                        stream.seek(0); stream.write(restored); stream.truncate(); stream.flush(); os.fsync(stream.fileno())
                    _flush()
                except Exception: cleaned = False
            if cleaned and owner_sid is not None:
                try:
                    from .steam_native_acceleration import cleanup_owned_leaf
                    cleanup_owned_leaf(fingerprint, owner_sid)
                except Exception: cleaned = False
            if cleaned: break
            if attempt < 5: time.sleep(min(.2 * 2 ** attempt, 2))
        if events.get('done') and cleaned: kernel.SetEvent(events['done'])
        if acquired: kernel.ReleaseMutex(mutex)
        for handle in reversed(handles): kernel.CloseHandle(handle)


class WindowsHostsLease:
    def __init__(self):
        self.kernel = _kernel()
        self.nonce = secrets.token_hex(16)
        self.launched = False
        self.events = {key: self.kernel.CreateEventW(None, True, False, _event_name(self.nonce, key))
                       for key in ('ready', 'stop', 'done')}
        if not all(self.events.values()):
            self.close(); raise OSError('本机 Steam 加速事件创建失败。')

    def start(self, cancelled):
        owner = self.kernel.OpenProcess(0x1000, False, os.getpid())
        try: created = _created(self.kernel, owner)
        finally: self.kernel.CloseHandle(owner)
        if not re.fullmatch('[a-f0-9]{64}', getattr(self, 'certificate_fingerprint', '')):
            raise ValueError('Missing certificate lease')
        args = ['--steam-community-hosts', str(os.getpid()), str(created), self.nonce, self.certificate_fingerprint]
        if not getattr(sys, 'frozen', False):
            args.insert(0, str(Path(__file__).resolve().parents[1] / 'run_assistant.pyw'))
        # Only this narrowly scoped watcher is elevated, not the helper/game.
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        shell.ShellExecuteW.argtypes = (w.HWND, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_int)
        shell.ShellExecuteW.restype = ctypes.c_ssize_t
        result = shell.ShellExecuteW(None, 'runas', sys.executable, subprocess.list2cmdline(args), None, 0)
        if result <= 32:
            raise NativeAccelerationError('authorization_denied',
                '未获得本机 Steam 加速所需的 Windows 授权；助手内部加速仍可使用。')
        self.launched = True
        deadline = time.monotonic() + 30
        while not cancelled.is_set() and time.monotonic() < deadline:
            if self.kernel.WaitForSingleObject(self.events['ready'], 100) == 0: return
            if self.kernel.WaitForSingleObject(self.events['done'], 0) == 0:
                raise NativeAccelerationError('hosts_unavailable',
                    '本机 Steam 域名配置未启用；请检查已有加速器、hosts 映射或系统权限。')
        raise NativeAccelerationError('authorization_timeout', '本机 Steam 加速授权已取消或启用超时。')

    def close(self):
        events = getattr(self, 'events', {})
        if events.get('stop'):
            self.kernel.SetEvent(events['stop'])
            # Keep TLS available while the owned hosts entry is being removed.
            if self.launched and self.kernel.WaitForSingleObject(events.get('done'), 3000) != 0:
                raise OSError('本机 Steam 域名配置尚未清理，请检查系统授权。')
        for handle in events.values():
            if handle: self.kernel.CloseHandle(handle)
        self.events = {}
