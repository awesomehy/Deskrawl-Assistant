"""Proxy bypass tests use fake WinINet; never alter the machine proxy."""
import ctypes
from ctypes import wintypes as w
import os
import threading
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.steam_native_proxy import (
    CommunityProxyBypass, WindowsProxyOptions, ConnectionOptions,
    WindowsProxyBypassLease, ProxyConfigurationError, proxy_worker,
)

MODULE = 'deskrawl_assistant.steam_native_proxy'
NONCE = 'a' * 32


class MemoryOptions:
    def __init__(self, flags=3, bypass='localhost;127.*;<local>'):
        self.flags, self.bypass, self.writes = flags, bypass, []

    def read(self): return self.flags, self.bypass

    def write_bypass(self, value):
        self.writes.append(value)
        self.bypass = value


class BypassTests(unittest.TestCase):
    def test_only_exact_host_is_added_and_original_is_restored(self):
        for original in ('', 'localhost;127.*;<local>', 'localhost;'):
            with self.subTest(original=original):
                options = MemoryOptions(bypass=original)
                lease = CommunityProxyBypass(options)
                lease.start()
                self.assertEqual(options.bypass.count('steamcommunity.com'), 1)
                self.assertNotIn('*steamcommunity', options.bypass)
                lease.close()
                self.assertEqual((options.flags, options.bypass), (3, original))

    def test_existing_exception_is_not_owned_or_removed(self):
        options = MemoryOptions(bypass='localhost; STEAMCOMMUNITY.COM ;example.org')
        lease = CommunityProxyBypass(options)
        lease.start(); lease.close()
        self.assertEqual(options.writes, [])

    def test_external_edits_and_changed_proxy_flags_are_preserved(self):
        options = MemoryOptions()
        lease = CommunityProxyBypass(options)
        lease.start()
        options.flags = 5
        options.bypass += ';new.example;10.*'
        lease.close()
        self.assertEqual((options.flags, options.bypass), (5, 'localhost;127.*;<local>;new.example;10.*'))

    def test_external_removal_is_not_replaced_with_old_settings(self):
        options = MemoryOptions()
        lease = CommunityProxyBypass(options)
        lease.start()
        options.bypass = 'different.example'
        lease.close()
        self.assertEqual(options.bypass, 'different.example')
        self.assertEqual(len(options.writes), 1)

    def test_enabled_pac_is_not_overwritten_or_reported_successful(self):
        options = MemoryOptions(flags=7)
        lease = CommunityProxyBypass(options)
        with self.assertRaises(ProxyConfigurationError): lease.start()
        lease.close()
        self.assertEqual(options.writes, [])

    def test_notification_failure_after_write_still_restores(self):
        options = MemoryOptions()
        real_write = options.write_bypass
        def failing_write(value):
            real_write(value)
            if len(options.writes) == 1: raise OSError('fixture notification')
        options.write_bypass = failing_write
        lease = CommunityProxyBypass(options)
        with self.assertRaises(OSError): lease.start()
        lease.close()
        self.assertEqual(options.bypass, 'localhost;127.*;<local>')

    def test_race_before_write_does_not_remove_external_rule(self):
        options = Mock()
        options.read.side_effect = [(3, 'localhost'), (3, 'localhost;steamcommunity.com')]
        lease = CommunityProxyBypass(options)
        with self.assertRaises(ProxyConfigurationError): lease.start()
        lease.close()
        options.write_bypass.assert_not_called()


class WindowsApiTests(unittest.TestCase):
    def test_supported_api_sets_only_bypass_option_and_not_proxy_flags_or_server(self):
        options = WindowsProxyOptions.__new__(WindowsProxyOptions)
        options.api = Mock()
        writes = []
        def set_option(_handle, flag, data, size):
            if flag == 75:
                values = ctypes.cast(data, ctypes.POINTER(ConnectionOptions)).contents
                self.assertEqual((values.connection, values.count), (None, 1))
                item = values.options[0]
                writes.append((item.option, ctypes.wstring_at(item.value.string)))
            else:
                self.assertIn(flag, (39, 37))
                self.assertIsNone(data)
                self.assertEqual(size, 0)
            return True
        options.api.InternetSetOptionW.side_effect = set_option
        options.write_bypass('localhost;steamcommunity.com')
        self.assertEqual(writes, [(3, 'localhost;steamcommunity.com')])
        self.assertEqual(options.api.InternetSetOptionW.call_count, 3)

    def test_read_frees_wininet_string_on_success_and_failure(self):
        for success in (True, False):
            options = WindowsProxyOptions.__new__(WindowsProxyOptions)
            options.api, options.free = Mock(), Mock()
            text = ctypes.create_unicode_buffer('localhost')
            address = ctypes.cast(text, ctypes.c_void_p).value
            def query(_handle, _flag, data, _size):
                values = ctypes.cast(data, ctypes.POINTER(ConnectionOptions)).contents
                self.assertEqual([item.option for item in values.options[:values.count]], [10, 3])
                values.options[0].value.number = 3
                values.options[1].value.string = address
                return success
            options.api.InternetQueryOptionW.side_effect = query
            if success: self.assertEqual(options.read(), (3, 'localhost'))
            else:
                with self.assertRaises(ProxyConfigurationError): options.read()
            options.free.assert_called_once_with(address)


@unittest.skipUnless(os.name == 'nt', 'Fake Windows watchdog orchestration')
class WatcherTests(unittest.TestCase):
    def exercise(self, *, wrong_user=False, wrong_pid=False, hosts=True, cancel=False, setup_error=False, disappears=False):
        kernel = Mock()
        kernel.OpenEventW.side_effect = lambda _a, _b, name: {'ready': 1, 'stop': 2, 'done': 3}[name.rsplit('.', 1)[1]]
        kernel.OpenProcess.side_effect = [4, 6]
        kernel.CreateMutexW.return_value = 5
        ready = []
        kernel.SetEvent.side_effect = lambda event: ready.append(True) if event == 1 else None
        kernel.WaitForSingleObject.side_effect = lambda handle, delay: (
            0 if handle == 5 or handle == 4 and ready and not disappears or handle == 2 and cancel else 258)
        bypass = Mock()
        if setup_error: bypass.start.side_effect = OSError('fixture')
        with patch(MODULE + '._kernel', return_value=kernel), \
             patch(MODULE + '._created', return_value=43 if wrong_pid else 42), \
             patch(MODULE + '._owner_sid', side_effect=['S-1-5-21-123', 'S-1-5-21-456' if wrong_user else 'S-1-5-21-123']), \
             patch(MODULE + '._hosts_active', side_effect=[True, False] if disappears else lambda _: hosts), \
             patch(MODULE + '.time.monotonic', side_effect=range(1000)), \
             patch(MODULE + '.CommunityProxyBypass', return_value=bypass) as factory:
            result = proxy_worker(123, 42, NONCE)
        return result, kernel, bypass, factory

    def test_owner_exit_restores_bypass_before_done(self):
        result, kernel, bypass, _ = self.exercise()
        self.assertEqual(result, 0)
        bypass.start.assert_called_once(); bypass.close.assert_called_once()
        kernel.SetEvent.assert_any_call(3)

    def test_disabled_hosts_also_releases_companion_without_stopping_owner(self):
        result, _, bypass, _ = self.exercise(disappears=True)
        self.assertEqual(result, 0)
        bypass.close.assert_called_once()

    def test_wrong_user_reused_pid_missing_hosts_and_cancel_never_set_options(self):
        for kwargs in ({'wrong_user': True}, {'wrong_pid': True}, {'hosts': False}, {'cancel': True}):
            result, _, _, factory = self.exercise(**kwargs)
            self.assertEqual(result, 1)
            factory.assert_not_called()

    def test_partial_setup_failure_still_rolls_back(self):
        result, kernel, bypass, _ = self.exercise(setup_error=True)
        self.assertEqual(result, 1)
        bypass.close.assert_called_once()
        self.assertNotIn(unittest.mock.call(1), kernel.SetEvent.call_args_list)

    def test_bad_arguments_never_open_windows_or_proxy(self):
        with patch(MODULE + '._kernel') as kernel:
            self.assertEqual(proxy_worker(0, 42, NONCE), 1)
            self.assertEqual(proxy_worker(123, 0, NONCE), 1)
            self.assertEqual(proxy_worker(123, 42, '../hosts'), 1)
            kernel.assert_not_called()

    def test_existing_watchdog_is_not_signalled_or_mistaken_for_new_ready(self):
        kernel = Mock()
        def event(*args): ctypes.set_last_error(183); return 1
        kernel.CreateEventW.side_effect = event
        lease = WindowsProxyBypassLease()
        with patch(MODULE + '._kernel', return_value=kernel), patch(MODULE + '.subprocess.Popen') as popen:
            with self.assertRaises(ProxyConfigurationError): lease.start(threading.Event(), nonce=NONCE)
        kernel.SetEvent.assert_not_called()
        popen.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(1)


if __name__ == '__main__': unittest.main()
