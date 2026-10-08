"""Normal keyboard/mouse input used by the user's desktop application.

This module is not used by investigation scripts. The application only sends L
after foreground, current hovered UID, instance identity and lock checks.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import time

U = C.WinDLL("user32", use_last_error=True)
U.GetForegroundWindow.restype = W.HWND
U.GetWindowThreadProcessId.argtypes = (W.HWND, C.POINTER(W.DWORD))
U.GetWindowThreadProcessId.restype = W.DWORD
U.GetCursorPos.argtypes = (C.POINTER(W.POINT),)
U.GetCursorPos.restype = W.BOOL
U.SetCursorPos.argtypes = (C.c_int, C.c_int)
U.SetCursorPos.restype = W.BOOL
U.GetWindowRect.argtypes = (W.HWND, C.POINTER(W.RECT))
U.GetWindowRect.restype = W.BOOL
U.GetAsyncKeyState.argtypes = (C.c_int,)
U.GetAsyncKeyState.restype = C.c_short
U.GetSystemMetrics.argtypes = (C.c_int,)
U.GetSystemMetrics.restype = C.c_int


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]


class INPUTUNION(C.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", W.DWORD), ("payload", INPUTUNION)]


U.SendInput.argtypes = (W.UINT, C.POINTER(INPUT), C.c_int)
U.SendInput.restype = W.UINT


class GameInput:
    def __init__(self, pid):
        self.pid = pid

    def foreground(self):
        window = U.GetForegroundWindow()
        pid = W.DWORD()
        U.GetWindowThreadProcessId(window, C.byref(pid))
        return window if pid.value == self.pid else None

    def bounds(self):
        window = self.foreground()
        if window is None:
            raise RuntimeError("请先切回 Deskrawl 游戏窗口")
        rect = W.RECT()
        if not U.GetWindowRect(window, C.byref(rect)):
            raise C.WinError(C.get_last_error())
        return (rect.left, rect.top, rect.right, rect.bottom)

    def position(self):
        point = W.POINT()
        if not U.GetCursorPos(C.byref(point)):
            raise C.WinError(C.get_last_error())
        return (point.x, point.y)

    def key_down(self, key):
        return bool(U.GetAsyncKeyState(key) & 0x8000)

    def move(self, point):
        if self.foreground() is None:
            raise RuntimeError("游戏已失去前台，停止锁定")
        left, top, width, height = (U.GetSystemMetrics(index) for index in (76, 77, 78, 79))
        if not 0 < width < 65536 or not 0 < height < 65536 or not left <= point[0] < left+width or not top <= point[1] < top+height:
            raise RuntimeError("目标鼠标位置不在当前桌面中，请重新校准")
        # Unity's input system may not produce hover events from SetCursorPos.
        # Use a normal absolute mouse event at the target pixel's center.
        x = min(65535, int((point[0] - left + 0.5) * 65536 / width))
        y = min(65535, int((point[1] - top + 0.5) * 65536 / height))
        event = INPUT()
        event.type = 0
        event.mi = MOUSEINPUT(x, y, 0, 0x0001 | 0x8000 | 0x4000, 0, 0)
        if U.SendInput(1, C.byref(event), C.sizeof(INPUT)) != 1:
            raise RuntimeError("鼠标移动未被系统接受，操作停止")

    def press_l(self):
        if self.foreground() is None:
            raise RuntimeError("游戏已失去前台，未发送 L")
        if any(self.key_down(key) for key in (0x10, 0x11, 0x12, 0x5b, 0x5c, 0x4c)):
            raise RuntimeError("有修饰键或 L 正在按住，停止锁定")
        events = (INPUT * 2)()
        events[0].type = events[1].type = 1
        events[0].ki = KEYBDINPUT(0x4c, 0, 0, 0, 0)
        events[1].ki = KEYBDINPUT(0x4c, 0, 2, 0, 0)
        sent = U.SendInput(1, C.byref(events[0]), C.sizeof(INPUT))
        if sent != 1:
            raise RuntimeError("L 按下未被系统接受；不会重按 L")
        try:
            # A down/up pair in one batch can fit between Unity input frames.
            # Hold through several frames; there is still only one keydown.
            time.sleep(0.12)
        finally:
            released = U.SendInput(1, C.byref(events[1]), C.sizeof(INPUT))
        if released != 1:
            raise RuntimeError("L 松开未被系统确认；操作停止，不会重按 L")
