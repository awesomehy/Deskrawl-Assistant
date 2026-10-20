from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.managed_mode import DEFAULT_SETTINGS, validate_settings
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.reconnect import ReconnectFlow, DEFAULT_RECONNECT
from deskrawl_assistant.reconnect_runtime import ReconnectRuntime, _notice_kind
from deskrawl_assistant.native_dispatcher import NativeActionError, GUARD_FAILED
from deskrawl_assistant.native_memory import MemoryReadError
import test_managed_service as service_tests


CHARACTER = '1:0:原角色'
CONFIG = {'enabled': True, 'character_index': 1}


def menu(**extra):
    return dict({'in_game': False, 'popup_visible': False, 'disconnected': False,
        'menu_ready': True, 'mode': 1, 'profiles_ready': True, 'busy': False,
        'selected_slot': 0, 'play_ready': True,
        'entries': [{'mode': 1, 'slot': 0, 'filled': True}, {'mode': 1, 'slot': 1, 'filled': False}]}, **extra)


def disconnect():
    return menu(popup_visible=True, disconnected=True, confirm_ready=True)


class ReconnectFlowTests(unittest.TestCase):
    def flow(self):
        flow = ReconnectFlow()
        flow.active, flow.started = True, 0
        flow.selection_confirmed = True
        return flow

    def test_old_configuration_migrates_disabled_without_replacing_other_preferences(self):
        cfg = deepcopy(DEFAULT_SETTINGS)
        del cfg['reconnect']
        cfg['auto_equip']['enabled'] = True
        value = validate_settings(cfg)
        self.assertEqual(value['reconnect'], DEFAULT_RECONNECT)
        self.assertTrue(value['auto_equip']['enabled'])
        self.assertNotIn('reconnect', cfg)

    def test_configuration_rejects_ambiguous_or_unbounded_indices(self):
        for value in (0, -1, True, 1.5, '1', 101, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                cfg = deepcopy(DEFAULT_SETTINGS)
                cfg['reconnect'] = {'enabled': True, 'character_index': value}
                validate_settings(cfg)

    def test_configuration_roundtrip(self):
        cfg = deepcopy(DEFAULT_SETTINGS)
        cfg['reconnect'] = {'enabled': True, 'character_index': 4}
        self.assertEqual(validate_settings(cfg)['reconnect'], cfg['reconnect'])

    def test_disabled_and_manual_menu_never_arm_login(self):
        self.assertEqual(ReconnectFlow().decide(disconnect(), DEFAULT_RECONNECT, CHARACTER, 0)['kind'], 'idle')
        self.assertEqual(ReconnectFlow().decide(menu(), CONFIG, CHARACTER, 0)['kind'], 'idle')

    def test_full_flow_requires_effect_and_respects_backoff(self):
        flow = ReconnectFlow()
        self.assertEqual(flow.decide(disconnect(), CONFIG, CHARACTER, 0)['kind'], 'confirm_disconnect')
        flow.submitted('confirm_disconnect', 0)
        self.assertEqual(flow.decide(disconnect(), CONFIG, CHARACTER, 1)['kind'], 'wait')
        self.assertEqual(flow.decide(menu(selected_slot=-1), CONFIG, CHARACTER, 5)['kind'], 'select_character')
        flow.submitted('select_character', 5)
        self.assertEqual(flow.decide(menu(), CONFIG, CHARACTER, 10)['kind'], 'play')
        flow.submitted('play', 10)
        self.assertEqual(flow.decide(menu(), CONFIG, CHARACTER, 15)['kind'], 'wait')
        self.assertEqual(flow.decide({'in_game': True}, CONFIG, CHARACTER, 16)['kind'], 'verify')

    def test_other_popups_are_never_acknowledged(self):
        for text in ('出售确认', '删除角色', '在线同步失败'):
            self.assertEqual(self.flow().decide(menu(popup_visible=True, text=text), CONFIG, CHARACTER, 5)['kind'], 'wait')

    def test_server_failure_only_retries_in_an_armed_flow_and_after_start_backoff(self):
        state = menu(popup_visible=True, retryable_network_error=True, confirm_ready=True)
        self.assertEqual(ReconnectFlow().decide(state, CONFIG, CHARACTER, 0)['kind'], 'idle')
        flow = self.flow()
        flow.submitted('play', 5)
        self.assertEqual(flow.decide(state, CONFIG, CHARACTER, 10)['kind'], 'wait')
        self.assertEqual(flow.decide(state, CONFIG, CHARACTER, 35)['kind'], 'confirm_network_error')
        self.assertEqual(flow.decide(dict(state, confirm_ready=False), CONFIG, CHARACTER, 35)['kind'], 'wait')
        flow.submitted('confirm_network_error', 35)
        self.assertEqual(flow.decide(menu(), CONFIG, CHARACTER, 36)['kind'], 'wait')
        self.assertEqual(flow.decide(menu(), CONFIG, CHARACTER, 40)['kind'], 'select_character')
        flow.submitted('select_character', 40)
        self.assertEqual(flow.decide(menu(), CONFIG, CHARACTER, 45)['kind'], 'play')

    def test_notice_allowlist_never_accepts_delegates_or_generic_errors(self):
        self.assertEqual(_notice_kind('连接已断开。\n(SteamID private)', False), 'disconnect')
        self.assertEqual(_notice_kind('无法连接服务器。\n(SteamID private)', False), 'server_unavailable')
        for text in ('无法连接服务器。', '连接已断开。', '删除角色'):
            self.assertIsNone(_notice_kind(text, True))
        for text in ('无法连接服务器。是否购买？', '删除角色', '', None):
            self.assertIsNone(_notice_kind(text, False))

    def test_missing_identity_and_offline_identity_pause(self):
        for character in (None, '', '0:0:离线角色'):
            self.assertEqual(self.flow().decide(menu(), CONFIG, character, 5)['kind'], 'pause')

    def test_empty_wrong_or_missing_position_never_plays(self):
        for config in ({'enabled': True, 'character_index': 2}, {'enabled': True, 'character_index': 3}):
            self.assertEqual(self.flow().decide(menu(), config, CHARACTER, 5)['kind'], 'pause')
        self.assertEqual(self.flow().decide(menu(entries=[{'mode': 1, 'slot': 1, 'filled': True}]), CONFIG, CHARACTER, 5)['kind'], 'pause')
        self.assertEqual(self.flow().decide(menu(entries=[{'mode': 0, 'slot': 0, 'filled': True}]), CONFIG, CHARACTER, 5)['kind'], 'pause')

    def test_list_position_uses_entry_slot_instead_of_assuming_index_equals_slot(self):
        state = menu(entries=[{'mode': 1, 'slot': 3, 'filled': True}], selected_slot=-1)
        self.assertEqual(self.flow().decide(state, CONFIG, '1:3:角色', 5)['kind'], 'select_character')

    def test_native_busy_loading_and_disabled_play_wait(self):
        for extra in ({'menu_ready': False}, {'profiles_ready': False}, {'busy': True}, {'play_ready': False}):
            self.assertEqual(self.flow().decide(menu(**extra), CONFIG, CHARACTER, 5)['kind'], 'wait')
        self.assertEqual(self.flow().decide(menu(mode=0), CONFIG, CHARACTER, 5)['kind'], 'online_tab')

    def test_timeout_is_explicit_and_does_not_replay(self):
        self.assertEqual(self.flow().decide(menu(), CONFIG, CHARACTER, 600)['kind'], 'pause')


class ReconnectRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.actions = Mock()
        self.runtime = ReconnectRuntime(self.actions)
        self.context = {'popup': 10, 'menu': 20, 'entries': [30, 40]}
        self.read = Mock(guards=[(123, b'guard')])

    def test_only_guarded_original_callbacks_are_invoked(self):
        for state, kind, method in [(disconnect(), 'confirm_disconnect', 'PopupPanel.confirm_disconnect'),
                (menu(popup_visible=True, retryable_network_error=True, confirm_ready=True),
                 'confirm_network_error', 'PopupPanel.confirm_disconnect'),
                (menu(selected_slot=-1), 'select_character', 'MainMenuUI.select_character'),
                (menu(), 'play', 'MainMenuUI.play'), (menu(mode=0), 'online_tab', 'MainMenuUI.online_tab')]:
            with patch.object(self.runtime, '_read', return_value=(state, self.context, self.read)):
                self.runtime.execute(kind, 1, CHARACTER, validate=lambda: True)
            calls, guards = self.actions.dispatcher.invoke.call_args.args
            self.assertEqual(calls[0].method, method)
            self.assertEqual(guards, self.read.guards)
            if kind == 'select_character': self.assertEqual(calls[0].arguments[0].value, 30)

    def test_changed_popup_empty_slot_and_cancel_never_invoke(self):
        for state, kind, validate in [(menu(popup_visible=True), 'confirm_disconnect', lambda: True),
                (menu(entries=[]), 'play', lambda: True), (menu(), 'play', lambda: False)]:
            with patch.object(self.runtime, '_read', return_value=(state, self.context, self.read)):
                with self.assertRaises(NativeActionError):
                    self.runtime.execute(kind, 1, CHARACTER, validate=validate)
        self.actions.dispatcher.invoke.assert_not_called()


class ManagedReconnectTests(unittest.TestCase):
    setUp = service_tests.ManagedServiceTests.setUp
    settings = service_tests.ManagedServiceTests.settings

    def ready(self):
        cfg = self.settings(auto_equip=True)
        cfg['reconnect'] = deepcopy(CONFIG)
        control = self.client.control_snapshot()
        control['character_id'] = CHARACTER
        self.client.control_snapshot = Mock(return_value=control)
        self.service.save_managed(cfg)
        self.service.set_managed_running(True)
        self.service.managed.character_id = CHARACTER
        self.client.reconnect_snapshot = Mock(return_value=disconnect())
        self.client.execute_reconnect_action = Mock(return_value={'submitted': True})

    def test_settings_are_saved_and_no_menu_inventory_is_read(self):
        self.ready()
        self.assertEqual(__import__('json').loads(self.service.managed_config.read_text(encoding='utf-8'))['reconnect'], CONFIG)
        captures = self.client.captures
        self.service._managed_once()
        self.assertEqual(self.client.captures, captures)
        self.assertEqual(self.service.managed.phase, 'reconnecting')
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.client.execute_reconnect_action.call_args.args[0], 'confirm_disconnect')
        self.assertEqual(self.client.actions, [])

    def test_pause_and_stop_cancel_auto_login(self):
        for pause in (True, False):
            self.ready()
            self.service.stop_managed(pause=pause)
            self.assertFalse(self.service._managed_reconnect_once())
            self.client.execute_reconnect_action.assert_not_called()

    def test_finish_during_reconnect_stops_without_more_login(self):
        self.ready()
        self.service._managed_once()
        self.service.finish_managed()
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.execute_reconnect_action.call_count, 1)

    def test_same_character_resumes_without_resetting_stats_or_quotas(self):
        self.ready()
        engine = self.service.managed
        engine.repeat_completed = 17
        metrics = engine.metrics
        self.service._managed_once()
        self.client.reconnect_snapshot.return_value = {'in_game': True}
        observation = self.service._managed_observation()
        observation['character_id'] = CHARACTER
        with patch.object(self.service, '_managed_observation', return_value=observation):
            self.service._managed_once()
        self.assertTrue(engine.running)
        self.assertFalse(self.service.managed_reconnect.active)
        self.assertIs(engine.metrics, metrics)
        self.assertEqual(engine.repeat_completed, 17)
        self.assertEqual(engine.character_id, CHARACTER)
        self.assertIn('重连成功', engine.status)
        self.assertEqual(self.client.actions, [])

    def test_other_character_cannot_resume_cleanup(self):
        self.ready()
        self.service._managed_once()
        self.client.reconnect_snapshot.return_value = {'in_game': True}
        observation = self.service._managed_observation()
        observation['character_id'] = '1:0:新角色'
        with patch.object(self.service, '_managed_observation', return_value=observation):
            self.service._managed_once()
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertEqual(self.client.actions, [])

    def test_uncertain_login_ack_pauses_without_retry(self):
        self.ready()
        self.client.execute_reconnect_action.side_effect = NativeActionError('未确认', uncertain=True)
        self.service._managed_once()
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertFalse(self.service._managed_reconnect_once())
        self.assertEqual(self.client.execute_reconnect_action.call_count, 1)

    def test_inventory_receipt_fence_survives_reconnect(self):
        self.ready()
        self.service.managed.await_effect = {'kind': 'synthesize_runes', 'receipt_id': 'old'}
        self.service._managed_once()
        self.assertEqual(self.service.managed.await_effect['receipt_id'], 'old')

    def test_guard_rejection_waits_and_does_not_repeat_each_tick(self):
        self.ready()
        self.client.execute_reconnect_action.side_effect = NativeActionError('guard', state=GUARD_FAILED)
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=10):
            self.service._managed_once()
            self.service._managed_once()
        self.assertEqual(self.client.execute_reconnect_action.call_count, 1)
        self.assertTrue(self.service.managed.running)

    def test_disconnect_between_preflight_and_inventory_read_keeps_process_connection(self):
        self.ready()
        self.client.reconnect_snapshot.side_effect = [menu(in_game=True), disconnect()]
        with patch.object(self.service, '_managed_observation', side_effect=MemoryReadError('角色已卸载')):
            self.service._work('managed', {})
        self.assertTrue(self.service.managed.running)
        self.assertTrue(self.client.connected)
        self.assertEqual(self.service.managed.phase, 'reconnecting')

    def test_unrelated_inventory_exception_does_not_arm_login(self):
        self.ready()
        self.client.reconnect_snapshot.return_value = menu()
        with patch.object(self.service, '_managed_observation', side_effect=MemoryReadError('其他错误')):
            self.service._work('managed', {})
        self.assertFalse(self.service.managed.running)
        self.client.execute_reconnect_action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
