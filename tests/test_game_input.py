"""Test input sequencing with a fake OS; never send a real key or mouse input."""
import ctypes as C
from ctypes import wintypes as W
import unittest
from unittest.mock import patch
from deskrawl_assistant import game_input


class FakeUser:
    def __init__(self):
        self.pid = 77
        self.held = set()
        self.accept_down = True
        self.events = []

    def GetForegroundWindow(self):
        return 123

    def GetWindowThreadProcessId(self, window, result):
        C.cast(result, C.POINTER(W.DWORD)).contents.value = self.pid
        return 1

    def GetAsyncKeyState(self, key):
        return 0x8000 if key in self.held else 0

    def SendInput(self, count, pointer, size):
        event = C.cast(pointer, C.POINTER(game_input.INPUT)).contents
        self.events.append((event.ki.wVk, event.ki.dwFlags))
        return 0 if event.ki.dwFlags == 0 and not self.accept_down else 1


class GameInputTests(unittest.TestCase):
    def test_one_keydown_and_release_with_frame_interval(self):
        fake = FakeUser()
        with patch.object(game_input, 'U', fake), patch.object(game_input.time, 'sleep') as pause:
            game_input.GameInput(77).press_l()
        self.assertEqual(fake.events, [(0x4c, 0), (0x4c, 2)])
        pause.assert_called_once_with(0.12)

    def test_rejected_keydown_is_not_retried(self):
        fake = FakeUser()
        fake.accept_down = False
        with patch.object(game_input, 'U', fake):
            with self.assertRaises(RuntimeError):
                game_input.GameInput(77).press_l()
        self.assertEqual(fake.events, [(0x4c, 0)])

    def test_release_happens_even_if_wait_is_interrupted(self):
        fake = FakeUser()
        with patch.object(game_input, 'U', fake), patch.object(game_input.time, 'sleep', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                game_input.GameInput(77).press_l()
        self.assertEqual(fake.events, [(0x4c, 0), (0x4c, 2)])

    def test_held_key_does_not_send_another_press(self):
        fake = FakeUser()
        fake.held.add(0x4c)
        with patch.object(game_input, 'U', fake):
            with self.assertRaises(RuntimeError):
                game_input.GameInput(77).press_l()
        self.assertFalse(fake.events)

    def test_other_foreground_app_receives_no_input(self):
        fake = FakeUser()
        fake.pid = 88
        with patch.object(game_input, 'U', fake):
            with self.assertRaises(RuntimeError):
                game_input.GameInput(77).press_l()
        self.assertFalse(fake.events)
