"""Synthetic executor checks. No threads, process reads or native dispatch."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.native_dispatcher import NativeActionError, REJECTED
from deskrawl_assistant.native_memory import GameBusyError
from tests import test_managed_service as service_tests
from tests.test_stargazing import plan


class StargazingServiceTests(unittest.TestCase):
    def setUp(self):
        service_tests.ManagedServiceTests.setUp(self)
        self.client.items=[]
        self.client.in_town=False;self.client.current_map='offline-map'
        self.client.maps.append({'id':'other-map','name':'另一张离线图','unlocked':True,'can_enter':True,'supported':True})
        self.run_id='offline-run';self.plan_items=[]
        original=self.client.control_snapshot
        def control():
            result=original()
            if result['run']:result['run']['id']=self.run_id
            result['carriage']={'complete':True,'items':deepcopy(self.client.carriage)}
            return result
        self.client.control_snapshot=control
        def preview():
            scene={'dungeon_id':self.client.current_map,'difficulty':self.client.difficulty,'run':{'id':self.run_id}}
            return plan({'scene':scene,'session':'offline-session'},self.plan_items)
        self.client.managed_loot_snapshot=Mock(side_effect=preview)
        cfg=service_tests.ManagedServiceTests.settings(self,farming=True)
        cfg['farming']['repeat_maps']=[{'dungeon_id':'offline-map','difficulty':'Normal','runs':3},
                                     {'dungeon_id':'other-map','difficulty':'Normal','runs':2}]
        cfg['farming']['stargazing']['enabled']=True
        self.service.save_managed(cfg)
        # Exercise the retained internal algorithm explicitly; the public
        # settings boundary now disables this hidden feature.
        self.service.managed.settings['farming']['stargazing']['enabled']=True
        self.service.set_managed_running(True)

    def test_combat_skip_rechecks_plan_then_requires_new_run_before_committing_target(self):
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        self.service._managed_once()
        self.assertEqual(self.client.actions[0][0],'enter_map')
        self.assertEqual(self.service.managed.target_dungeon,'offline-map')
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.client.current_map='other-map'
        self.service._managed_once()
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.assertEqual(len(self.client.actions),1)
        self.run_id='new-run'
        self.service._managed_once()
        self.assertEqual(self.service.managed.stargazing_state['skipped'],1)
        self.assertEqual(self.service.managed.target_dungeon,'other-map')

    def test_high_value_plan_appearing_between_plan_and_execute_prevents_entry(self):
        world=self.service._managed_observation()
        action=self.service.managed.next_action(world)
        self.plan_items=[{'kind':'gem','name':'一颗实际规划宝石'}]
        result=self.service._managed_execute(action,world)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])
        self.assertEqual(self.service.managed.target_dungeon,'offline-map')
        self.service.managed.pending=None
        self.assertIsNone(self.service.managed.next_action(self.service._managed_observation()))
        self.assertEqual(self.service.managed.stargazing_state['verdict'],'keep')

    def test_attack_observed_between_planning_and_dispatch_waits_without_native_request(self):
        world=self.service._managed_observation()
        action=self.service.managed.next_action(world)
        original=self.client.control_snapshot
        def attacking():
            state=original();state.update(travel_blocked=True,travel_wait_reason='正在攻击')
            return state
        self.client.control_snapshot=attacking
        with self.assertRaisesRegex(GameBusyError,'正在攻击'):
            self.service._managed_execute(action,world)
        self.assertEqual(self.client.actions,[])

    def test_native_completion_is_counted_even_during_retry_backoff(self):
        self.client.native_handler=Mock(side_effect=NativeActionError('明确拒绝',state=REJECTED))
        self.service._managed_once()
        original=self.client.control_snapshot
        def completed():
            state=original();state['run'].update(status='settling',completion_confirmed=True)
            return state
        self.client.control_snapshot=completed
        self.service._managed_once()
        self.assertEqual(self.service.managed.repeat_completed,1)
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.service._managed_once()
        self.assertEqual(self.service.managed.repeat_completed,1)
        self.assertEqual(len(self.client.actions),1)

    def test_temporary_native_refusal_defers_then_can_switch_without_returning_to_town(self):
        self.client.native_handler=Mock(side_effect=NativeActionError('明确拒绝',state=REJECTED))
        self.service._managed_once()
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed.stargazing_return)
        self.assertEqual(self.service.managed_retry['key'][0],'stargazing_entry_rejected')
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        self.service.managed_retry['next_at']=0
        self.service._managed_once()
        self.assertEqual([row[0] for row in self.client.actions],['enter_map','enter_map'])
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)

    def test_repeated_explicit_refusals_eventually_return_to_town(self):
        self.client.native_handler=Mock(side_effect=NativeActionError('明确拒绝',state=REJECTED))
        for _ in range(8):
            if self.service.managed_retry:self.service.managed_retry['next_at']=0
            self.service._managed_once()
        self.assertIsNotNone(self.service.managed.stargazing_return)
        self.assertIsNone(self.service.managed_retry)
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        self.service._managed_once()
        self.assertEqual(self.client.actions[-1][0],'return_town')
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)

    def test_final_wave_receipt_between_planning_and_dispatch_forbids_skip(self):
        world=self.service._managed_observation()
        action=self.service.managed.next_action(world)
        original=self.client.control_snapshot
        def ended():
            state=original();state['run']['completion_confirmed']=True
            return state
        self.client.control_snapshot=ended
        result=self.service._managed_execute(action,world)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_unconfirmed_dispatch_is_hard_failure(self):
        self.client.native_handler=Mock(side_effect=NativeActionError('结果不确定',state=REJECTED,uncertain=True))
        with self.assertRaises(NativeActionError):self.service._managed_once()
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.assertIsNone(self.service.managed.stargazing_return)

    def test_save_reload_preserves_disabled_options_and_restores_fixed_protection(self):
        self.service.stop_managed()
        cfg=deepcopy(self.service.managed.settings)
        cfg['farming']['stargazing']['rewards'].update(gem=False,divine_equipment=False)
        self.service.save_managed(cfg)
        self.assertFalse(self.service.managed.settings['farming']['stargazing']['enabled'])
        self.assertFalse(json.loads(self.service.managed_config.read_text(encoding='utf-8'))['farming']['stargazing']['enabled'])
        self.service._init_managed()
        self.assertFalse(self.service.managed.settings['farming']['stargazing']['enabled'])
        choices=self.service.managed.settings['farming']['stargazing']['rewards']
        self.assertFalse(choices['gem']);self.assertTrue(choices['divine_equipment'])

    def test_legacy_enabled_file_loads_disabled_without_skipping_or_changing_quotas(self):
        self.service.stop_managed()
        cfg=deepcopy(self.service.managed.settings)
        cfg['farming']['stargazing']['enabled']=True
        cfg['farming']['stargazing']['rewards']['gem']=False
        self.service.managed_config.write_text(json.dumps(cfg),encoding='utf-8')
        self.service._init_managed()
        self.service._managed_refresh_control()
        current=self.service.managed.settings['farming']
        self.assertFalse(current['stargazing']['enabled'])
        self.assertFalse(current['stargazing']['rewards']['gem'])
        self.assertEqual(current['repeat_maps'],cfg['farming']['repeat_maps'])
        self.service.set_managed_running(True)
        self.service._managed_once()
        self.assertEqual(self.client.actions,[])
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.managed.repeat_completed,0)
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)

    def request_town_for_stargazing(self):
        self.service.managed.stargazing_return={'source_run_id':self.run_id,'next_repeat_index':1}
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._managed_once()
        self.assertEqual(self.client.actions[-1][0],'return_town')

    def test_interrupted_skip_rechecks_new_high_value_run_without_returning_or_counting_a_skip(self):
        self.request_town_for_stargazing()
        self.run_id='valuable-new-run';self.plan_items=[{'kind':'gem','name':'新局宝石'}]
        for now in (101,109,112):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._managed_once()
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.managed.stargazing_state['verdict'],'keep')
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.assertEqual(self.service.managed.repeat_completed,0)
        self.assertEqual([row[0] for row in self.client.actions],['return_town'])

    def test_final_wave_during_return_wait_counts_completion_once_and_never_counts_skip(self):
        self.request_town_for_stargazing()
        original=self.client.control_snapshot
        def completed():
            result=original()
            if result['run']:result['run'].update(status='settling',completion_confirmed=True)
            return result
        self.client.control_snapshot=completed
        for now in (101,102):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._managed_once()
        self.assertEqual(self.service.managed.repeat_completed,1)
        self.client.in_town=True;self.client.current_map=None
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=103):
            self.service._managed_once()
        self.assertEqual(self.service.managed.repeat_completed,1)
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.assertEqual(self.service.managed.target_dungeon,'offline-map')
        self.assertEqual(self.service.managed.phase,'cleanup')

    def test_three_confirmed_cancelled_stargazing_returns_finish_current_run_without_a_skip(self):
        self.request_town_for_stargazing()
        for now in (101,109,112,113,121,124,125,133,140,300):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._managed_once()
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.managed.stargazing_state['verdict'],'keep')
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.assertEqual(self.service.managed.repeat_completed,0)
        self.assertEqual([row[0] for row in self.client.actions],['return_town']*3)
        self.assertIsNone(self.service.managed_transition)
        self.assertIsNone(self.service.managed_return_recovery)
        self.assertIsNone(self.service.managed.stargazing_return)
        self.client.run_status='success'
        self.service._managed_once()
        self.service._managed_once()
        self.assertEqual(self.service.managed.repeat_completed,1)
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)

    def test_stargazing_live_portal_still_pauses_without_repeated_dispatch(self):
        self.request_town_for_stargazing()
        original=self.client.control_snapshot
        def teleporting():
            result=original();result['teleporting']=True;return result
        self.client.control_snapshot=teleporting
        for now in (101,130,221):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._managed_once()
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.assertEqual([row[0] for row in self.client.actions],['return_town'])

    def test_town_arrival_after_new_run_does_not_commit_old_skip(self):
        self.request_town_for_stargazing()
        self.run_id='another-run'
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=101):
            self.service._managed_once()
        self.client.in_town=True;self.client.current_map=None
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=102):
            self.service._managed_once()
        self.assertEqual(self.service.managed.stargazing_state['skipped'],0)
        self.assertEqual(self.service.managed.repeat_completed,0)
        self.assertEqual(self.service.managed.target_dungeon,'offline-map')
        self.assertEqual(self.service.managed.phase,'cleanup')
