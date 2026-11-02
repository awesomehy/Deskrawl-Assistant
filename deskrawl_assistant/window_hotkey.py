"""Windows RegisterHotKey worker. No keyboard hooks or input interception."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import threading

from .assistant_settings import normalize_hotkey


def hotkey_codes(value):
    normalized = normalize_hotkey(value)
    if not normalized:
        return 0, 0
    parts = normalized.split('+')
    modifiers = sum({'Alt': 1, 'Ctrl': 2, 'Shift': 4}[p] for p in parts[:-1])
    key = parts[-1]
    vk = 0x70 + int(key[1:]) - 1 if key.startswith('F') and len(key) > 1 else ord(key)
    return modifiers | 0x4000, vk  # MOD_NOREPEAT


def hotkey_bindings(value):
    """Numeric shortcuts accept both top-row and Num Lock keypad digits."""
    modifiers, key = hotkey_codes(value)
    if not key:
        return ()
    bindings = ((modifiers, key),)
    if 0x30 <= key <= 0x39:
        bindings += ((modifiers, 0x60 + key - 0x30),)
    return bindings


class Win32HotkeyBackend:
    def __init__(self):
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                           wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.PeekMessageW.restype = wintypes.BOOL

    def register(self, identity, modifiers, key):
        return bool(self.user32.RegisterHotKey(None, identity, modifiers, key))

    def unregister(self, identity):
        self.user32.UnregisterHotKey(None, identity)

    def poll(self):
        message = wintypes.MSG()
        result = []
        while self.user32.PeekMessageW(ctypes.byref(message), None, 0x0312, 0x0312, 1):
            result.append(message.wParam)
        return result


class GlobalHotkey:
    def __init__(self, callback, *, backend_factory=None):
        self.callback = callback
        self.factory = backend_factory or Win32HotkeyBackend
        self.supported = os.name == 'nt' or backend_factory is not None
        self.commands = queue.Queue()
        self.shortcut = ''
        self.error = ''
        self.closed = False
        self.thread = None
        if self.supported:
            self.thread = threading.Thread(target=self._run, name='assistant-hotkey', daemon=True)
            self.thread.start()

    def change(self, shortcut, persist=lambda: None):
        shortcut = normalize_hotkey(shortcut)
        if self.closed or not self.supported:
            raise ValueError('当前窗口不支持全局老板键。')
        done, errors = threading.Event(), []
        self.commands.put((shortcut, persist, done, errors))
        # The transaction cannot time out and later unexpectedly commit.
        while not done.wait(0.1):
            if not self.thread.is_alive():
                raise ValueError(self.error or '老板键服务未能启动。')
        if errors:
            raise errors[0]

    def _run(self):
        identities = ()
        backend = None
        try:
            backend = self.factory()
            while True:
                try:
                    command = self.commands.get(timeout=0.04)
                except queue.Empty:
                    command = None
                if command is not None:
                    shortcut, persist, done, errors = command
                    if shortcut is None:
                        done.set()
                        break
                    new_identities = []
                    try:
                        if shortcut != self.shortcut and shortcut:
                            # Reserve a separate pair while the old shortcut remains
                            # active. A conflict on either digit must roll back both.
                            base_identity = 0x3212 if 0x3210 in identities else 0x3210
                            bindings = hotkey_bindings(shortcut)
                            for offset, (modifiers, key) in enumerate(bindings):
                                new_identity = base_identity + offset
                                if not backend.register(new_identity, modifiers, key):
                                    detail = '（含对应小键盘数字键）' if len(bindings) > 1 else ''
                                    raise ValueError(f'这个快捷键{detail}已被其他程序使用，请换一个组合。')
                                new_identities.append(new_identity)
                        persist()
                        if shortcut != self.shortcut:
                            for identity in identities:
                                backend.unregister(identity)
                            identities = tuple(new_identities)
                            self.shortcut = shortcut
                        self.error = ''
                    except Exception as exc:
                        for new_identity in new_identities:
                            backend.unregister(new_identity)
                        errors.append(exc)
                    finally:
                        done.set()
                for received in backend.poll():
                    if received in identities and not self.closed:
                        self.callback()
        except Exception:
            self.error = '全局老板键服务不可用，请重新打开助手后重试。'
        finally:
            if backend:
                for identity in identities:
                    backend.unregister(identity)

    def close(self):
        self.closed = True
        if self.thread and self.thread.is_alive():
            done = threading.Event()
            self.commands.put((None, None, done, []))
            self.thread.join(timeout=2)
