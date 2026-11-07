import json
import os
from pathlib import Path
import queue
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.assistant_settings import AssistantSettings, normalize_hotkey
from deskrawl_assistant.window_hotkey import GlobalHotkey, hotkey_codes, hotkey_bindings
from deskrawl_assistant.steam_acceleration import SteamMarketTransport, ProbeFailure
from deskrawl_assistant.steam_market import MarketUnavailable
from deskrawl_assistant.web_service import AssistantService
from tests.test_web_service import FakeClient


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'assistant-settings.json'
        self.settings = AssistantSettings(self.path)

    def test_acceleration_is_on_and_no_shortcut_registered_by_default(self):
        self.assertEqual(self.settings.snapshot(), {'steam_acceleration_enabled': True, 'boss_hotkey': ''})
        self.assertFalse(self.path.exists())

    def test_upgrade_preserves_explicit_acceleration_choice_and_file(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.path.write_text(json.dumps({'schema_version': 1,
                    'steam_acceleration_enabled': enabled, 'boss_hotkey': 'Ctrl+0'}), encoding='utf-8')
                original = self.path.read_bytes()
                loaded = AssistantSettings(self.path)
                self.assertIs(loaded.acceleration_enabled(), enabled)
                self.assertEqual(loaded.snapshot()['boss_hotkey'], 'Ctrl+0')
                self.assertFalse(loaded.load_error)
                self.assertEqual(self.path.read_bytes(), original)

    def test_save_roundtrip_and_no_location_migration_field(self):
        value = {'steam_acceleration_enabled': True, 'boss_hotkey': ''}
        self.settings.save(value)
        self.assertEqual(AssistantSettings(self.path).snapshot(), value)
        with self.assertRaises(ValueError): self.settings.save({**value, 'data_directory': 'C:/other'})
        self.assertEqual(AssistantSettings(self.path).snapshot(), value)

    def test_invalid_values_never_rewrite_settings(self):
        initial = self.settings.snapshot()
        self.settings.save(initial)
        original = self.path.read_bytes()
        for value in [True, 'false', 0, None]:
            if value is True: continue
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.settings.save({**initial, 'steam_acceleration_enabled': value})
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_file_is_retained_and_defaults_do_not_enable_acceleration(self):
        self.path.write_text('{broken', encoding='utf-8')
        settings = AssistantSettings(self.path)
        self.assertFalse(settings.acceleration_enabled())
        self.assertTrue(settings.load_error)
        self.assertEqual(self.path.read_text(), '{broken')

    def test_failed_write_preserves_persisted_and_in_memory_values(self):
        initial = self.settings.snapshot()
        self.settings.save(initial)
        original = self.path.read_bytes()
        with patch('pathlib.Path.replace', side_effect=OSError('read only')):
            with self.assertRaises(OSError): self.settings.save({**initial, 'steam_acceleration_enabled': False})
        self.assertEqual(self.settings.snapshot(), initial)
        self.assertEqual(self.path.read_bytes(), original)

    def test_shortcut_requires_desktop_transaction(self):
        with self.assertRaises(ValueError):
            self.settings.save({'steam_acceleration_enabled': True, 'boss_hotkey': 'Ctrl+Alt+H'})
        self.assertFalse(self.path.exists())

    def test_shortcut_normalization_and_reserved_combinations(self):
        self.assertEqual(normalize_hotkey('Alt+Ctrl+h'), 'Ctrl+Alt+H')
        self.assertEqual(hotkey_codes('Ctrl+Alt+F11'), (0x4003, 0x7A))
        for value in ['H', 'Shift+H', 'Win+H', 'Ctrl+F12', 'Alt+F4', 'Ctrl+Ctrl+H', 'Ctrl+Delete', None]:
            with self.subTest(value=value), self.assertRaises(ValueError): normalize_hotkey(value)

    def test_numeric_shortcuts_bind_both_keyboards_without_changing_saved_format(self):
        for digit in range(10):
            with self.subTest(digit=digit):
                self.assertEqual(hotkey_bindings(f'Ctrl+{digit}'),
                                 ((0x4002, 0x30 + digit), (0x4002, 0x60 + digit)))
        self.assertEqual(hotkey_bindings('Ctrl+Alt+I'), ((0x4003, 0x49),))
        self.assertEqual(hotkey_bindings('Ctrl+Alt+F11'), ((0x4003, 0x7A),))
        self.assertEqual(hotkey_bindings(''), ())

    def test_existing_numeric_preference_loads_unchanged_and_registers_both_keys(self):
        original = {'schema_version': 1, 'steam_acceleration_enabled': True, 'boss_hotkey': 'Ctrl+0'}
        self.path.write_text(json.dumps(original), encoding='utf-8')
        before = self.path.read_bytes()
        backend = FakeHotkeyBackend()
        manager = GlobalHotkey(lambda: None, backend_factory=lambda: backend)
        try:
            settings = AssistantSettings(self.path)
            manager.change(settings.snapshot()['boss_hotkey'])
            self.assertEqual(set(backend.active.values()), {(0x4002, 0x30), (0x4002, 0x60)})
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            manager.close()


class FakeHotkeyBackend:
    def __init__(self):
        self.active = {}
        self.reject = False
        self.reject_keys = set()
        self.events = queue.Queue()

    def register(self, identity, modifiers, key):
        if self.reject or key in self.reject_keys: return False
        self.active[identity] = (modifiers, key)
        return True

    def unregister(self, identity):
        self.active.pop(identity, None)

    def poll(self):
        result = []
        while not self.events.empty(): result.append(self.events.get_nowait())
        return result


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeHotkeyBackend()
        self.manager = GlobalHotkey(Mock(), backend_factory=lambda: self.backend)
        self.addCleanup(self.manager.close)

    def test_conflict_preserves_old_registration_and_does_not_save(self):
        self.manager.change('Ctrl+Alt+H')
        before = dict(self.backend.active)
        self.backend.reject = True
        persist = Mock()
        with self.assertRaisesRegex(ValueError, '其他程序'):
            self.manager.change('Ctrl+Alt+J', persist)
        persist.assert_not_called()
        self.assertEqual(self.manager.shortcut, 'Ctrl+Alt+H')
        self.assertEqual(self.backend.active, before)

    def test_save_failure_releases_new_registration_and_keeps_old(self):
        self.manager.change('Ctrl+Alt+H')
        before = dict(self.backend.active)
        with self.assertRaises(OSError):
            self.manager.change('Ctrl+Alt+J', Mock(side_effect=OSError('disk full')))
        self.assertEqual(self.backend.active, before)
        self.assertEqual(self.manager.shortcut, 'Ctrl+Alt+H')

    def test_success_save_precedes_releasing_old_key_and_clear_releases_all(self):
        self.manager.change('Ctrl+Alt+H')
        counts = []
        self.manager.change('Ctrl+Alt+J', lambda: counts.append(len(self.backend.active)))
        self.assertEqual(counts, [2])
        self.assertEqual(len(self.backend.active), 1)
        self.manager.change('')
        self.assertFalse(self.backend.active)

    def test_close_unregisters_and_joins_thread(self):
        self.manager.change('Ctrl+Alt+H')
        self.manager.close()
        self.assertFalse(self.manager.thread.is_alive())
        self.assertFalse(self.backend.active)

    def test_both_digit_messages_trigger_callback_and_close_releases_both(self):
        self.manager.change('Ctrl+0')
        self.assertEqual(set(self.backend.active.values()), {(0x4002, 0x30), (0x4002, 0x60)})
        for count, identity in enumerate(self.backend.active, 1):
            called = threading.Event()
            self.manager.callback.side_effect = called.set
            self.backend.events.put(identity)
            self.assertTrue(called.wait(2))
            self.assertEqual(self.manager.callback.call_count, count)
        self.manager.close()
        self.assertFalse(self.backend.active)

    def test_keypad_conflict_rolls_back_top_row_and_keeps_old_shortcut(self):
        self.manager.change('Ctrl+Alt+I')
        before = dict(self.backend.active)
        self.backend.reject_keys.add(0x60)
        persist = Mock()
        with self.assertRaisesRegex(ValueError, '小键盘'):
            self.manager.change('Ctrl+0', persist)
        persist.assert_not_called()
        self.assertEqual(self.backend.active, before)
        self.assertEqual(self.manager.shortcut, 'Ctrl+Alt+I')

    def test_numeric_save_failure_releases_both_new_keys(self):
        self.manager.change('Ctrl+Alt+I')
        before = dict(self.backend.active)
        with self.assertRaises(OSError):
            self.manager.change('Ctrl+0', Mock(side_effect=OSError('disk full')))
        self.assertEqual(self.backend.active, before)
        self.assertEqual(self.manager.shortcut, 'Ctrl+Alt+I')

    def test_replacing_numeric_shortcut_releases_both_old_keys_after_save(self):
        self.manager.change('Ctrl+0')
        registrations_at_save = []
        self.manager.change('Ctrl+1', lambda: registrations_at_save.append(set(self.backend.active.values())))
        self.assertEqual(registrations_at_save,
                         [{(0x4002, 0x30), (0x4002, 0x60), (0x4002, 0x31), (0x4002, 0x61)}])
        self.assertEqual(set(self.backend.active.values()), {(0x4002, 0x31), (0x4002, 0x61)})
        self.manager.change('Ctrl+Alt+I')
        self.assertEqual(set(self.backend.active.values()), {(0x4003, 0x49)})
        self.manager.change('')
        self.assertFalse(self.backend.active)

    @unittest.skipUnless(os.name == 'nt', 'Windows global hotkey API')
    def test_real_windows_conflict_detection_and_cleanup(self):
        first, second = GlobalHotkey(lambda: None), GlobalHotkey(lambda: None)
        try:
            try: first.change('Ctrl+Alt+Shift+F11')
            except ValueError: self.skipTest('Test shortcut already in use')
            with self.assertRaises(ValueError): second.change('Ctrl+Alt+Shift+F11')
            first.change('')
            second.change('Ctrl+Alt+Shift+F11')
            self.assertEqual(second.shortcut, 'Ctrl+Alt+Shift+F11')
        finally:
            first.close();second.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows message queue')
    def test_real_windows_hotkey_message_reaches_callback(self):
        import ctypes
        called=threading.Event();manager=GlobalHotkey(called.set)
        try:
            manager.change('Ctrl+Alt+Shift+F10')
            self.assertTrue(ctypes.windll.user32.PostThreadMessageW(manager.thread.native_id,0x312,0x3210,0))
            self.assertTrue(called.wait(2))
        finally: manager.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows global hotkey API')
    def test_real_windows_numeric_shortcut_owns_and_releases_both_keys(self):
        from deskrawl_assistant.window_hotkey import Win32HotkeyBackend
        manager = GlobalHotkey(lambda: None)
        probe = Win32HotkeyBackend()
        bindings = hotkey_bindings('Ctrl+Alt+Shift+0')
        try:
            try: manager.change('Ctrl+Alt+Shift+0')
            except ValueError: self.skipTest('Test shortcut already in use')
            for offset, (modifiers, key) in enumerate(bindings):
                self.assertFalse(probe.register(0x4310 + offset, modifiers, key))
            manager.close()
            for offset, (modifiers, key) in enumerate(bindings):
                self.assertTrue(probe.register(0x4310 + offset, modifiers, key))
        finally:
            manager.close()
            for offset in range(len(bindings)):
                probe.unregister(0x4310 + offset)


class SettingsServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        client = FakeClient([])
        client.connected = False
        self.service = AssistantService(client, Path(self.tmp.name) / 'rules.json')
        self.addCleanup(self.service.close)

    def test_settings_save_does_not_touch_game_or_market_until_requested(self):
        self.assertTrue(self.service.assistant_settings_info()['steam_acceleration_enabled'])
        self.service.save_assistant_settings({'steam_acceleration_enabled': False, 'boss_hotkey': ''})
        self.service.save_assistant_settings({'steam_acceleration_enabled': True, 'boss_hotkey': ''})
        self.assertTrue(self.service.assistant_preferences.acceleration_enabled())
        self.assertIsNone(self.service.market)
        self.assertFalse(self.service.client.actions)
        self.assertIsNone(self.service.native_steam_acceleration)

    def test_desktop_activation_uses_saved_switch_and_settings_control_same_manager(self):
        self.service.save_assistant_settings({'steam_acceleration_enabled': True, 'boss_hotkey': ''})
        with patch('deskrawl_assistant.steam_native_acceleration.NativeSteamAcceleration') as factory:
            manager = factory.return_value
            manager.snapshot.return_value = {'status': 'active', 'message': 'fixture active'}
            self.service.enable_native_steam_acceleration()
            self.service.enable_native_steam_acceleration()
            factory.assert_called_once_with()
            manager.configure.assert_called_once_with(True)
            self.assertEqual(self.service.assistant_settings_info()['native_steam_acceleration']['status'], 'active')
            self.service.save_assistant_settings({'steam_acceleration_enabled': False, 'boss_hotkey': ''})
            manager.configure.assert_called_with(False)
            self.service.close()
            manager.close.assert_called_once()
        self.assertFalse(self.service.client.actions)

    def test_open_uses_actual_data_path_and_accepts_no_caller_path(self):
        with patch('os.startfile', create=True) as open_folder:
            self.assertTrue(self.service.open_assistant_data_directory()['opened'])
        open_folder.assert_called_once_with(str(Path(self.tmp.name).resolve()))

    def test_config_change_refreshes_only_market_failures(self):
        self.service.market = Mock()
        self.service.save_assistant_settings({'steam_acceleration_enabled': False, 'boss_hotkey': ''})
        self.service.market.refresh_failed.assert_called_once()

    def test_reset_rules_backs_up_only_rules_and_keeps_other_files(self):
        self.service.config.write_bytes(b'{old broken rules')
        other=self.service.automation_config;other.write_bytes(b'{"unchanged":true}')
        self.service.config_error='broken'
        result=self.service.reset_assistant_configuration('equipment_rules')
        self.assertEqual((Path(result['backup_directory'])/self.service.config.name).read_bytes(),b'{old broken rules')
        self.assertEqual(json.loads(self.service.config.read_text())['rules'],[])
        self.assertEqual(other.read_bytes(),b'{"unchanged":true}')
        self.assertFalse(self.service.config_error);self.assertFalse(self.service.client.actions)

    def test_reset_automation_persists_shared_defaults_and_keeps_rules_and_preferences(self):
        from deskrawl_assistant.automation import DEFAULT_SETTINGS
        from deskrawl_assistant.managed_mode import DEFAULT_SETTINGS as MANAGED
        self.service.config.write_bytes(b'keep rules')
        self.service.assistant_preferences.save({'steam_acceleration_enabled':True,'boss_hotkey':''})
        preferences=self.service.assistant_preferences.path.read_bytes()
        self.service.automation_settings['gems']['enabled']=True
        self.service.reset_assistant_configuration('automation')
        self.assertEqual(self.service.automation_settings,DEFAULT_SETTINGS)
        self.assertEqual(self.service.managed.settings,MANAGED)
        self.assertEqual(json.loads(self.service.automation_config.read_text()),DEFAULT_SETTINGS)
        self.assertEqual(self.service.config.read_bytes(),b'keep rules')
        self.assertEqual(self.service.assistant_preferences.path.read_bytes(),preferences)

    def test_reset_rejects_active_work_and_arbitrary_categories(self):
        for field in ['busy','monitoring','automation_running']:
            setattr(self.service,field,True)
            with self.assertRaises(ValueError):self.service.reset_assistant_configuration('equipment_rules')
            setattr(self.service,field,False)
        for value in ['../config','all','steam_session',None]:
            with self.assertRaises(ValueError):self.service.reset_assistant_configuration(value)

    def test_reset_failed_disk_transaction_leaves_memory_and_originals(self):
        self.service.config.write_bytes(b'original')
        self.service.rules=['in memory']
        with patch('deskrawl_assistant.web_service.write_settings_files',side_effect=OSError('full')):
            with self.assertRaises(OSError):self.service.reset_assistant_configuration('equipment_rules')
        self.assertEqual(self.service.rules,['in memory'])
        self.assertEqual(self.service.config.read_bytes(),b'original')

    def test_browser_cache_clear_uses_native_profile_without_deleting_configuration(self):
        desktop=Mock();desktop.clear_browser_cache.return_value={'cleared':True}
        result=self.service.reset_assistant_configuration('browser_cache',desktop)
        desktop.clear_browser_cache.assert_called_once();self.assertTrue(result['cleared'])
        with self.assertRaises(ValueError):self.service.reset_assistant_configuration('browser_cache')


class AccelerationToggleTests(unittest.TestCase):
    def test_disabled_transport_never_starts_cdn_or_relay(self):
        transport = SteamMarketTransport(lambda: False, None)
        preferred = Mock()
        preferred.get.side_effect = ProbeFailure('offline')
        with patch('deskrawl_assistant.steam_acceleration.ExistingRoute', return_value=preferred), \
             patch('deskrawl_assistant.steam_acceleration.MarketProbe') as fallback:
            with self.assertRaises(MarketUnavailable): transport(Mock())
            fallback.assert_not_called()

    def test_enabled_transport_can_fallback_and_next_job_observes_disabled_state(self):
        value = {'enabled': True}
        transport = SteamMarketTransport(lambda: value['enabled'], None)
        preferred = Mock()
        preferred.get.return_value = 'ordinary'
        with patch('deskrawl_assistant.steam_acceleration.ExistingRoute', return_value=preferred), \
             patch('deskrawl_assistant.steam_acceleration.MarketProbe') as fallback:
            fallback.return_value.fetch.return_value = 'accelerated'
            self.assertEqual(transport(Mock()), 'accelerated')
            value['enabled'] = False
            self.assertEqual(transport(Mock()), 'ordinary')
            self.assertEqual(fallback.call_count, 1)

    def test_429_maps_to_existing_market_backoff_contract(self):
        transport = SteamMarketTransport(lambda: True, None)
        with patch('deskrawl_assistant.steam_acceleration.MarketProbe') as fallback:
            fallback.return_value.fetch.side_effect = ProbeFailure('limited', kind='http', status=429, retry_after=600)
            with self.assertRaises(MarketUnavailable) as caught: transport(Mock())
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, 600))


if __name__ == '__main__': unittest.main()
