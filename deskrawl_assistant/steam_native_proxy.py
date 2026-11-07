"""Temporary, exact-host bypass for native Steam's Windows system proxy.

HTTP CONNECT proxies resolve the Community host remotely and bypass hosts.
Use WinINet's supported API to add just this host to the LAN bypass list.
A non-elevated watcher owns restoration, including after desktop exit.
No proxy server, PAC URL, credentials, connection flags or Steam files change.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .steam_native_hosts import HOST, BEGIN, _kernel, _created, _owner_sid, _hosts_path, _event_name, hosts_edit


class ProxyConfigurationError(OSError):
    pass


class OptionValue(ctypes.Union):
    _fields_ = [('number', w.DWORD), ('string', ctypes.c_void_p), ('time', w.FILETIME)]


class ConnectionOption(ctypes.Structure):
    _fields_ = [('option', w.DWORD), ('value', OptionValue)]


class ConnectionOptions(ctypes.Structure):
    _fields_ = [('size', w.DWORD), ('connection', w.LPWSTR), ('count', w.DWORD),
                ('error', w.DWORD), ('options', ctypes.POINTER(ConnectionOption))]


class WindowsProxyOptions:
    def __init__(self):
        self.api = ctypes.WinDLL('wininet', use_last_error=True)
        self.api.InternetQueryOptionW.argtypes = (ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.POINTER(w.DWORD))
        self.api.InternetQueryOptionW.restype = w.BOOL
        self.api.InternetSetOptionW.argtypes = (ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD)
        self.api.InternetSetOptionW.restype = w.BOOL
        self.free = ctypes.WinDLL('kernel32', use_last_error=True).GlobalFree
        self.free.argtypes = (ctypes.c_void_p,)
        self.free.restype = ctypes.c_void_p

    def read(self):
        options = (ConnectionOption * 2)()
        options[0].option, options[1].option = 10, 3  # FLAGS_UI, PROXY_BYPASS
        values = ConnectionOptions(ctypes.sizeof(ConnectionOptions), None, 2, 0, options)
        size = w.DWORD(ctypes.sizeof(values))
        try:
            if not self.api.InternetQueryOptionW(None, 75, ctypes.byref(values), ctypes.byref(size)):
                raise ProxyConfigurationError('无法读取本机 Steam 所需的系统代理例外。')
            pointer = options[1].value.string
            return options[0].value.number, ctypes.wstring_at(pointer) if pointer else ''
        finally:
            if options[1].value.string: self.free(options[1].value.string)

    def write_bypass(self, value):
        if not isinstance(value, str) or len(value) > 65536 or '\x00' in value:
            raise ValueError('Invalid proxy bypass')
        text = ctypes.create_unicode_buffer(value)
        option = ConnectionOption(3, OptionValue(string=ctypes.cast(text, ctypes.c_void_p)))
        values = ConnectionOptions(ctypes.sizeof(ConnectionOptions), None, 1, 0, ctypes.pointer(option))
        if not self.api.InternetSetOptionW(None, 75, ctypes.byref(values), ctypes.sizeof(values)):
            raise ProxyConfigurationError('无法设置本机 Steam 的社区代理例外。')
        # Notify existing browsers; never disable or replace the user's proxy.
        for flag in (39, 37):
            if not self.api.InternetSetOptionW(None, flag, None, 0):
                raise ProxyConfigurationError('本机 Steam 的社区代理例外刷新失败。')


def _rules(value):
    if not isinstance(value, str) or len(value) > 65536 or '\x00' in value:
        raise ValueError('Invalid proxy bypass')
    return value.split(';')


class CommunityProxyBypass:
    def __init__(self, options=None):
        self.options = options or WindowsProxyOptions()
        self.original = self.applied = None

    def start(self):
        flags, original = self.options.read()
        _rules(original)
        # PAC decides routes independently of the manual bypass list. Do not
        # overwrite its script or misleadingly report that it was accelerated.
        if flags & 4:
            raise ProxyConfigurationError('系统使用自动代理脚本，无法仅用社区代理例外启用本机加速。')
        if any(rule.strip().lower() == HOST for rule in _rules(original)): return
        applied = original + (';' if original and not original.endswith(';') else '') + HOST
        if self.options.read()[1] != original:
            raise ProxyConfigurationError('系统代理例外正在变化，未覆盖。')
        # Record ownership before writing so a notification/verification failure
        # still restores a change which WinINet may already have applied.
        self.original, self.applied = original, applied
        self.options.write_bypass(self.applied)
        if self.options.read()[1] != self.applied:
            raise ProxyConfigurationError('系统代理例外未能确认，已取消本机加速。')

    def close(self):
        if self.applied is None: return
        _, current = self.options.read()
        if current == self.applied:
            restored = self.original
        else:
            # Preserve edits made by the user/another proxy while enabled.
            rules = _rules(current)
            matches = [i for i, rule in enumerate(rules) if rule.strip().lower() == HOST]
            if not matches:
                self.applied = self.original = None
                return
            del rules[matches[-1]]
            restored = ';'.join(rules)
        self.options.write_bypass(restored)
        if self.options.read()[1] != restored:
            raise ProxyConfigurationError('社区代理例外尚未恢复。')
        self.applied = self.original = None


def _hosts_active(nonce):
    path = _hosts_path()
    if path.stat().st_size > 1024 * 1024: return False
    raw = path.read_bytes()
    return BEGIN + nonce.encode() in raw and hosts_edit(raw, nonce, True) == raw


def proxy_worker(pid, created, nonce):
    """Fixed-host, same-user worker; accepts no proxy server, URL or paths."""
    if (os.name != 'nt' or not re.fullmatch('[a-f0-9]{32}', nonce)
            or not 1 <= pid <= 0xffffffff or not 1 <= created < 1 << 64): return 1
    kernel = _kernel()
    handles, events, bypass = [], {}, None
    acquired = False
    try:
        for suffix in ('ready', 'stop', 'done'):
            event = kernel.OpenEventW(0x100002, False, _event_name(nonce, 'proxy.' + suffix))
            if not event: return 1
            handles.append(event); events[suffix] = event
        owner = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not owner: return 1
        handles.append(owner)
        if _created(kernel, owner) != created: return 1
        this = kernel.OpenProcess(0x1000, False, os.getpid())
        if not this: return 1
        handles.append(this)
        sid = _owner_sid(this)
        if _owner_sid(owner) != sid: return 1
        mutex = kernel.CreateMutexW(None, False, 'Local\\Deskrawl.SteamCommunity.ProxyLease.' + sid)
        if not mutex: return 1
        handles.append(mutex)
        if kernel.WaitForSingleObject(mutex, 0) not in (0, 0x80): return 1
        acquired = True
        if kernel.WaitForSingleObject(owner, 0) == 0 or kernel.WaitForSingleObject(events['stop'], 0) == 0:
            return 1
        if not _hosts_active(nonce): return 1
        bypass = CommunityProxyBypass()
        bypass.start()
        kernel.SetEvent(events['ready'])
        last_check = time.monotonic()
        while kernel.WaitForSingleObject(owner, 200) != 0:
            if kernel.WaitForSingleObject(events['stop'], 0) == 0: break
            # Also releases a supplement attached to an already running older
            # helper when that helper switches its acceleration setting off.
            if time.monotonic() - last_check >= 1:
                if not _hosts_active(nonce): break
                last_check = time.monotonic()
        return 0
    except Exception:
        return 1  # Never log proxy configuration, credentials or private URLs.
    finally:
        cleaned = bypass is None
        for attempt in range(6):
            try:
                if bypass is not None: bypass.close()
                cleaned = True; break
            except Exception:
                if attempt < 5: time.sleep(min(.2 * 2 ** attempt, 2))
        if events.get('done') and cleaned: kernel.SetEvent(events['done'])
        if acquired: kernel.ReleaseMutex(mutex)
        for handle in reversed(handles): kernel.CloseHandle(handle)


class WindowsProxyBypassLease:
    def __init__(self):
        self.events, self.process = {}, None

    def start(self, cancelled, *, nonce, owner_pid=None):
        if not re.fullmatch('[a-f0-9]{32}', nonce): raise ValueError('Invalid lease')
        self.kernel = _kernel()
        for key in ('ready', 'stop', 'done'):
            ctypes.set_last_error(0)
            handle = self.kernel.CreateEventW(None, True, False, _event_name(nonce, 'proxy.' + key))
            if handle: self.events[key] = handle
            if not handle or ctypes.get_last_error() == 183:
                # Do not signal an existing watchdog's stop event or mistake
                # its ready event for acknowledgement by a newly spawned one.
                self.detach()
                raise ProxyConfigurationError('社区代理例外已由其他通道持有或监护事件不可用。')
        pid = os.getpid() if owner_pid is None else owner_pid
        owner = self.kernel.OpenProcess(0x1000, False, pid)
        try: created = _created(self.kernel, owner)
        finally:
            if owner: self.kernel.CloseHandle(owner)
        args = [sys.executable]
        if not getattr(sys, 'frozen', False):
            args.append(str(Path(__file__).resolve().parents[1] / 'run_assistant.pyw'))
        args += ['--steam-community-proxy', str(pid), str(created), nonce]
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.monotonic() + 10
        while not cancelled.is_set() and time.monotonic() < deadline:
            if self.kernel.WaitForSingleObject(self.events['done'], 0) == 0 or self.process.poll() is not None:
                raise ProxyConfigurationError('社区代理例外未启用；请检查系统代理脚本或网络设置权限。')
            if self.kernel.WaitForSingleObject(self.events['ready'], 100) == 0: return
        raise ProxyConfigurationError('社区代理例外启用已取消或超时。')

    def close(self):
        if not self.events: return
        if self.events.get('stop'):
            self.kernel.SetEvent(self.events['stop'])
            if self.process is not None and self.kernel.WaitForSingleObject(self.events.get('done'), 3000) != 0:
                raise ProxyConfigurationError('社区代理例外尚未清理。')
        self.detach()

    def detach(self):
        """Release caller handles; the watchdog retains its owner/hosts lease."""
        for handle in self.events.values():
            if handle: self.kernel.CloseHandle(handle)
        self.events = {}
