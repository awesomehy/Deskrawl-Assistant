"""Immediate repeats use original settlement buttons, never skip loot or rotate early."""
from contextlib import ExitStack
from copy import deepcopy
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions, _Read, normalise_run
from deskrawl_assistant.managed_mode import ManagedMode
from deskrawl_assistant.mythic import floor_id
from deskrawl_assistant.native_dispatcher import compile_mailbox, load_profile
from deskrawl_assistant.native_memory import GameBusyError, SnapshotChangedError
from deskrawl_assistant.runtime_client import RuntimeClient
from test_managed_mode import settings, after_rewards, verified, equipment, loot
from test_farming_cycles import scene
import test_managed_service as service_fixture
from test_campaign_repeat_counter import CampaignCopy
from test_progression import state as growth_state
from test_world_boss import options as boss_options


def fixture(mythic=False, *, counter=None, town=False):
    key, difficulty = (floor_id(22), 'Mythic') if mythic else ('a', 'Normal')
    cfg = settings()
    cfg['farming']['repeat_maps'] = [{'dungeon_id':key, 'difficulty':difficulty, 'runs':5}]
    world = scene(key, difficulty, run_id='first', town=town)
    world['scene'].update(in_dungeon=not town, scene_changing=False, teleporting=False,
        world_difficulty='Inferno' if mythic else 'Normal',
        maps_by_difficulty={difficulty:[{'map_id':key, 'can_enter':True, 'unlocked':True}]},
        completion_visible={})
    if counter is not None and not town:
        world['scene']['run']['completion_counter'] = {'id':'totals', 'count':counter}
    engine = ManagedMode(cfg)
    engine.start(world)
    return engine, world


def win(world, mythic=False, run_id='first'):
    world['scene']['run'].update(id=run_id, status='success')
    world['scene']['completion_visible'] = {'MythicCompletionUI' if mythic else 'CombatMapCompletionUI':True}
    return world


class CompletionReplayTests(unittest.TestCase):
    def test_ordinary_and_rift_repeat_current_target_without_town_or_countdown(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic):
                engine, world = fixture(mythic)
                action = after_rewards(engine, win(world, mythic))
                self.assertEqual((action['kind'], action['reason_code']), ('enter_dungeon', 'completion_replay'))
                self.assertEqual(action['dungeon_id'], world['scene']['dungeon_id'])
                self.assertEqual(action['source_run_id'], 'first')
                self.assertTrue(action['protect_completion_rewards'])
                verified(engine, action)
                for _ in range(3):
                    self.assertIsNone(engine.next_action(world))
                self.assertEqual(engine.repeat_completed, 1)
                world['scene']['run'].update(id='second', status='running')
                world['scene']['completion_visible'] = {}
                self.assertIsNone(engine.next_action(world))
                self.assertEqual(after_rewards(engine, win(world, mythic, 'second'))['reason_code'], 'completion_replay')
                self.assertEqual(engine.repeat_completed, 2)

    def test_native_totals_and_delayed_counter_recovery_are_not_counted_twice(self):
        for mythic in (False, True):
            for delayed in (False, True):
                with self.subTest(mythic=mythic, delayed=delayed):
                    engine, world = fixture(mythic, counter=7)
                    if delayed:
                        world['scene']['run'].pop('completion_counter')
                    else:
                        world['scene']['run']['completion_counter']['count'] = 8
                    action = after_rewards(engine, win(world, mythic))
                    engine.expect_run_entry(action, world)  # service dispatch
                    verified(engine, action)  # engine acknowledgement
                    world['scene']['run'].update(id='second', status='running',
                        completion_counter={'id':'totals', 'count':8})
                    world['scene']['completion_visible'] = {}
                    engine.next_action(world)
                    self.assertEqual(engine.repeat_completed, 1)
                    world['scene']['run']['completion_counter']['count'] = 9
                    after_rewards(engine, win(world, mythic, 'second'))
                    self.assertEqual(engine.repeat_completed, 2)

    def test_original_town_entry_keeps_fallback_receipts_until_native_totals_appear(self):
        engine, world = fixture()
        original = {'kind':'enter_dungeon', 'action_id':100, 'dungeon_id':'a',
                    'difficulty':'Normal', 'source_run_id':'previous-visit'}
        engine.expect_run_entry(original)
        action = after_rewards(engine, win(world))
        verified(engine, action)
        self.assertEqual(engine.run_entry['action_id'], 100)
        self.assertEqual(engine.run_entry['fallback_wins'], {'first'})
        world['scene']['run'].update(id='second', status='running', completion_counter={'id':'totals','count':1})
        world['scene']['completion_visible'] = {}
        engine.next_action(world)
        self.assertEqual(engine.repeat_completed, 1)

    def test_rotation_failed_run_materials_and_return_conditions_keep_town_path(self):
        for mythic in (False, True):
            for change in ('quota', 'failed', 'materials', 'slots', 'chests', 'merchant', 'stop', 'push'):
                if mythic and change == 'push':
                    continue  # The push route only supports ordinary campaign maps.
                with self.subTest(mythic=mythic, change=change):
                    engine, world = fixture(mythic)
                    win(world, mythic)
                    if change == 'quota':
                        engine.repeat_maps[0]['runs'] = 1
                        engine.repeat_maps.append({'dungeon_id':'b', 'difficulty':engine.target_difficulty, 'runs':2})
                    elif change == 'failed': world['scene']['run']['status'] = 'failed'
                    elif change == 'materials':
                        world['scene']['maps_by_difficulty'][engine.target_difficulty][0]['can_enter'] = False
                    elif change == 'slots': world['counts']['inventory']['occupied'] = 38
                    elif change == 'chests': world['items'] = [loot('boxes', 'chest', 'TreasureChest3', count=30)]
                    elif change == 'merchant':
                        engine.settings['merchant']['enabled'] = True
                        world['progression'] = growth_state()
                        world['progression']['soul_shards'].update(current=100, maximum=100)
                    elif change == 'stop': engine.finishing = True
                    else: engine.settings['farming']['mode'] = 'push'
                    self.assertEqual(after_rewards(engine, world)['kind'], 'return_town')

    def test_late_gold_equipment_and_chests_are_collected_before_replay(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic):
                engine, world = fixture(mythic)
                win(world, mythic)
                self.assertIsNone(engine.next_action(world))
                rows = [equipment('gear', container='ground'),
                        loot('box', 'chest', 'TreasureChest3', container='ground'),
                        loot('gold', 'currency', 'GoldDrop', container='ground', item_type=1)]
                for expected in ('gold', 'gear', 'box'):
                    world['ground']['items'] = deepcopy(rows)
                    action = engine.next_action(world)
                    self.assertEqual((action['kind'], action['item']['selection_id']), ('collect_ground', expected))
                    verified(engine, action)
                    row = next(row for row in rows if row['selection_id'] == expected)
                    rows.remove(row)
                    if expected != 'gold':
                        world['items'].append(dict(row, container='inventory'))
                        world['counts']['inventory']['occupied'] += 1
                world['ground']['items'] = []
                self.assertEqual(after_rewards(engine, world)['reason_code'], 'completion_replay')

    def test_missing_popup_incomplete_ground_full_bag_and_retained_run_do_not_replay(self):
        for change in ('popup', 'ground', 'full', 'retained'):
            with self.subTest(change=change):
                engine, world = fixture(True)
                win(world, True)
                if change == 'popup': world['scene']['completion_visible'] = {}
                elif change == 'ground': world['ground']['complete'] = False
                elif change == 'retained': world['scene']['run']['retained_terminal'] = True
                else:
                    world['counts']['inventory']['occupied'] = 40
                    world['ground']['items'] = [equipment('leftover', container='ground')]
                action = after_rewards(engine, world)
                self.assertNotEqual((action or {}).get('reason_code'), 'completion_replay')
                if change == 'full': self.assertEqual(engine.phase, 'paused')

    def test_daily_boss_reset_keeps_priority_after_finishing_current_repeat(self):
        for mythic in (False, True):
            for remaining in (0, 100):
                with self.subTest(mythic=mythic, remaining=remaining):
                    engine, world = fixture(mythic)
                    engine.settings['pumpkin']['enabled'] = True
                    engine.pumpkin_difficulty = 'Normal'
                    rows = world['scene']['maps_by_difficulty'].setdefault('Normal', [])
                    rows.extend(boss_options(remaining)['Normal'])
                    action = after_rewards(engine, win(world, mythic))
                    self.assertEqual(action['kind'], 'return_town' if remaining else 'enter_dungeon')


class CompletionReplayServiceTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp

    def test_fresh_preflight_rechecks_settings_target_materials_drops_and_run(self):
        for mythic in (False, True):
            for change in ('none', 'mode', 'disabled', 'target', 'materials', 'slots', 'chests',
                           'merchant', 'stop', 'late_drop', 'source'):
                with self.subTest(mythic=mythic, change=change):
                    engine, world = fixture(mythic)
                    action = after_rewards(engine, win(world, mythic))
                    self.service.managed = engine
                    if change == 'mode': engine.settings['farming']['mode'] = 'push'
                    elif change == 'disabled': engine.settings['farming']['enabled'] = False
                    elif change == 'target': engine.target_dungeon = 'b'
                    elif change == 'materials': world['scene']['maps_by_difficulty'][engine.target_difficulty][0]['can_enter'] = False
                    elif change == 'slots': world['counts']['inventory']['occupied'] = 40
                    elif change == 'chests': world['items'] = [loot('boxes', 'chest', 'TreasureChest3', count=30)]
                    elif change == 'merchant': engine.merchant_pending = engine.settings['merchant']['enabled'] = True
                    elif change == 'stop': engine.finishing = True
                    elif change == 'late_drop': world['ground']['items'] = [equipment('late', container='ground')]
                    elif change == 'source': world['scene']['run'].update(id='native-next', status='running')
                    self.client.actions.clear()
                    with patch.object(self.service, '_managed_observation', return_value=world):
                        result = self.service._managed_execute(action, world)
                    if change == 'none':
                        self.assertTrue(result['verified'])
                        self.assertEqual(self.client.actions[0][0], 'enter_map')
                    else:
                        self.assertTrue(result['retryable'])
                        self.assertEqual(self.client.actions, [])

    def test_each_source_run_has_separate_retry_budget(self):
        engine, world = fixture()
        action = after_rewards(engine, win(world))
        self.service.managed = engine
        self.service.managed_retry = self.service.managed_scene_wait = None
        for run_id, now in (('first', 100), ('second', 233)):
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=now):
                self.service._managed_defer(dict(action, source_run_id=run_id), '等待结算窗口', 'game_busy')
        self.assertTrue(engine.running)
        self.assertEqual(self.service.managed_retry['started'], 233)
        self.assertEqual(self.service.managed_retry['attempts'], 1)


class CompletionReplayPollingTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp

    def prepare(self, mythic=False):
        engine, world = fixture(mythic)
        world['scene']['dead'] = False
        self.service.managed = engine
        self.service.managed_replay_poll = None
        return engine, world

    def test_serial_worker_wakes_once_then_uses_short_wait_through_settlement(self):
        for mythic, signal in ((False, 'completion_confirmed'), (True, 'completion_confirmed'),
                               (False, 'final_kill_settled'), (True, 'final_kill_settled')):
            with self.subTest(mythic=mythic, signal=signal):
                engine, world = self.prepare(mythic)
                world['scene']['run'].update(status='settling', **{signal:True})
                with patch.object(self.service, '_managed_observation', return_value=world), \
                        patch.object(self.service, '_managed_execute', return_value={'verified':True}) as execute:
                    for now in (0, .2, .4, .6, .8, 1.0, 1.2):
                        if now == .2: win(world, mythic)
                        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=now):
                            self.service._managed_once()
                            if now < 1.2:
                                execute.assert_not_called()
                                self.assertEqual(self.service._managed_poll_interval(), .05)
                            self.assertEqual(self.service.managed_fast_followup, now == 0)
                    execute.assert_called_once()
                    self.assertEqual(execute.call_args.args[0]['reason_code'], 'completion_replay')
                    self.assertEqual(engine.repeat_completed, 1)

    def test_short_polling_expires_without_renewal_on_stuck_or_changing_reads(self):
        _, world = self.prepare()
        win(world)
        for now, complete, expected in ((100, True, .05), (101, False, .05),
                                        (102, True, .05), (108, True, 2), (300, True, 2)):
            world['complete'] = complete
            self.service.managed_fast_followup = False
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=now):
                self.service._managed_update_replay_poll(world)
                self.assertEqual(self.service._managed_poll_interval(), expected)
            self.assertEqual(self.service.managed_fast_followup, now == 100)
            self.assertEqual(self.service.managed_replay_poll['until'], 108)
        world['scene']['run'].update(id='next', status='running', completion_confirmed=False)
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=301):
            self.service._managed_update_replay_poll(world)
            self.assertEqual(self.service._managed_poll_interval(), 2)
        win(world, run_id='next')
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=302):
            self.service._managed_update_replay_poll(world)
            self.assertEqual(self.service._managed_poll_interval(), .05)
            self.assertEqual(self.service.managed_replay_poll['until'], 310)

    def test_combat_unknown_states_and_other_work_keep_normal_polling(self):
        changes = ('combat', 'settling_unknown', 'failed', 'retained', 'incomplete', 'unavailable',
                   'town', 'dead', 'unknown_dead', 'scene_changing', 'teleporting',
                   'map', 'difficulty', 'push', 'disabled', 'finish', 'pumpkin', 'paused')
        for change in changes:
            with self.subTest(change=change):
                engine, world = self.prepare()
                win(world)
                if change in ('combat', 'settling_unknown', 'failed'):
                    world['scene']['run']['status'] = {'combat':'running', 'settling_unknown':'settling', 'failed':'failed'}[change]
                elif change == 'retained': world['scene']['run']['retained_terminal'] = True
                elif change == 'incomplete': world['complete'] = False
                elif change == 'unavailable': world['scene']['available'] = False
                elif change == 'town': world['scene'].update(in_town=True, in_dungeon=False)
                elif change == 'unknown_dead': world['scene']['dead'] = None
                elif change in ('dead', 'scene_changing', 'teleporting', 'online_saving'): world['scene'][change] = True
                elif change == 'map': world['scene']['dungeon_id'] = 'b'
                elif change == 'difficulty': world['scene']['difficulty'] = 'Nightmare'
                elif change == 'push': engine.settings['farming']['mode'] = 'push'
                elif change == 'disabled': engine.settings['farming']['enabled'] = False
                elif change == 'finish': engine.finishing = True
                elif change == 'pumpkin': engine.pumpkin_active = True
                else: engine.running = False
                with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                    self.service._managed_update_replay_poll(world)
                    self.assertEqual(self.service._managed_poll_interval(), 2)
        for attribute in ('managed_retry', 'managed_transition', 'managed_synthesis_wait', 'managed_auction_wait'):
            with self.subTest(receipt=attribute):
                _, world = self.prepare()
                with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                    self.service._managed_update_replay_poll(win(world))
                    setattr(self.service, attribute, {'in_flight':True})
                    self.assertEqual(self.service._managed_poll_interval(), 2)
                    setattr(self.service, attribute, None)

    def test_first_settlement_wakes_worker_without_creating_an_immediate_poll_loop(self):
        _, world = self.prepare()
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        with patch.object(self.service, '_managed_observation', return_value=world), \
                patch.object(self.service, '_refresh_trade_tracking_if_needed'):
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                self.service._work('managed', {})
            self.assertTrue(self.service.monitor_wake.is_set())
            self.service.monitor_wake.clear()
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100.2):
                self.service._work('managed', {})
                self.assertEqual(self.service._managed_poll_interval(), .05)
            self.assertFalse(self.service.monitor_wake.is_set())
        self.assertEqual(self.client.actions, [])

    def test_short_wait_is_used_by_existing_serial_monitor(self):
        _, world = self.prepare()
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            self.service._managed_update_replay_poll(win(world))
            with patch.object(self.service, 'monitor_wake') as wake, patch.object(self.service, 'closed') as closed, \
                    patch('deskrawl_assistant.web_service.threading.Thread') as thread:
                closed.is_set.side_effect = [False] * 6 + [True]
                self.service._monitor_loop()
                self.assertEqual(wake.wait.call_args_list, [unittest.mock.call(.05)] * 2)
                thread.return_value.start.assert_called_once()

    def test_fast_samples_keep_full_quiet_period_after_late_boss_drop(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic):
                engine, world = fixture(mythic)
                win(world, mythic)
                for now in (0, .2, .4, .6):
                    with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=now):
                        self.assertIsNone(engine.next_action(world))
                world['ground']['items'] = [equipment('late-boss', container='ground')]
                with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=.8):
                    action = engine.next_action(world)
                    self.assertEqual(action['kind'], 'collect_ground')
                    verified(engine, action)
                world['ground']['items'] = []
                for now in (1.0, 1.2, 1.4, 1.6, 1.8):
                    with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=now):
                        self.assertIsNone(engine.next_action(world))
                with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=2.0):
                    self.assertEqual(engine.next_action(world)['reason_code'], 'completion_replay')


class CompletionReplayLatencyTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp

    def captured(self, world):
        bag = world['counts']['inventory']
        snap = {'complete':world['complete'], 'bridge_session':world['session'],
                'containers':{'inventory':{'slot_count':bag['capacity'], 'occupied_count':bag['occupied'],
                    'slots':[r for r in world['items'] if r.get('container') == 'inventory']}}}
        control = {'available':True, 'character_id':'hero', 'scene':deepcopy(world['scene']),
                   'ground':deepcopy(world['ground'])}
        return {'snapshot':snap, 'control':control, 'session':world['session'],
                'progression':deepcopy(world.get('progression', {}))}

    def test_narrow_preflight_keeps_fresh_replay_gates_without_display_or_rule_scan(self):
        for mythic in (False, True):
            for change in ('none', 'incomplete', 'unavailable', 'session', 'connection', 'character',
                           'unknown_character', 'source', 'slots', 'chests', 'materials', 'late_drop', 'merchant'):
                with self.subTest(mythic=mythic, change=change):
                    engine, world = fixture(mythic)
                    action = after_rewards(engine, win(world, mythic))
                    engine.character_id = 'hero'
                    self.service.managed = engine
                    self.client.actions.clear()
                    if change == 'slots': world['counts']['inventory']['occupied'] = 40
                    elif change == 'chests': world['items'] = [loot('boxes', 'chest', 'TreasureChest3', count=30)]
                    elif change == 'materials': world['scene']['maps_by_difficulty'][engine.target_difficulty][0]['can_enter'] = False
                    elif change == 'late_drop': world['ground']['items'] = [equipment('late', container='ground')]
                    elif change == 'source': world['scene']['run'].update(id='native-next', status='running')
                    elif change == 'merchant': engine.settings['merchant']['enabled'] = engine.merchant_pending = True
                    value = self.captured(world)
                    if change == 'incomplete': value['snapshot']['complete'] = False
                    elif change == 'unavailable': value['control']['available'] = False
                    elif change == 'session': value['snapshot']['bridge_session'] = value['session'] = 'other'
                    elif change == 'connection': value['session'] = 'other'
                    elif change == 'character': value['control']['character_id'] = 'other'
                    elif change == 'unknown_character': value['control'].pop('character_id')
                    with patch.object(self.client, 'completion_replay_snapshot', create=True, return_value=value) as capture, \
                            patch.object(self.service, '_managed_observation', side_effect=AssertionError('full scan')), \
                            patch.object(self.service, 'managed_page', side_effect=AssertionError('settings projection')), \
                            patch.object(self.service, '_publish', side_effect=AssertionError('display projection')):
                        if change in ('incomplete', 'unavailable', 'session', 'connection', 'character', 'unknown_character'):
                            with self.assertRaises(SnapshotChangedError): self.service._managed_execute(action, world)
                        else:
                            result = self.service._managed_execute(action, world)
                            self.assertEqual(result.get('verified'), change == 'none')
                        capture.assert_called_once_with(merchant=engine.settings['merchant']['enabled'])
                    self.assertEqual(len(self.client.actions), 1 if change == 'none' else 0)

    def test_control_starts_quiet_time_and_fast_poll_before_optional_growth_read(self):
        engine, world = fixture()
        win(world)
        world['scene']['dead'] = False
        self.service.managed = engine
        value = self.captured(world)
        def growth(**kwargs):
            self.assertEqual(self.service._managed_poll_interval(), .05)
            self.assertEqual(engine.completion_rewards.empty_since, 100)
            raise SnapshotChangedError('growth changed')
        with patch.object(self.client, 'control_snapshot', return_value=value['control']), \
                patch.object(self.client, 'progression_snapshot', create=True, side_effect=growth), \
                patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            with self.assertRaises(SnapshotChangedError):
                self.service._managed_refresh_control(reward_snapshot=value['snapshot'])
        self.assertTrue(self.service.managed_replay_poll['active'])
        self.assertEqual(engine.completion_rewards.empty_reads, 1)
        self.assertEqual(self.client.actions, [])

    def test_early_control_cannot_clear_rewards_from_incomplete_or_nonempty_ground(self):
        for change in ('inventory', 'ground', 'late_drop'):
            with self.subTest(change=change):
                engine, world = fixture()
                win(world)
                world['scene']['dead'] = False
                self.service.managed = engine
                self.service.managed_replay_poll = None
                value = self.captured(world)
                if change == 'inventory': value['snapshot']['complete'] = False
                elif change == 'ground': value['control']['ground']['complete'] = False
                else: value['control']['ground']['items'] = [equipment('late', container='ground')]
                with patch.object(self.client, 'control_snapshot', return_value=value['control']):
                    self.service._managed_refresh_control(reward_snapshot=value['snapshot'])
                self.assertIsNone(engine.completion_rewards.empty_since)
                self.assertEqual(self.client.actions, [])

    def test_only_same_run_unsubmitted_online_wait_keeps_fast_polling(self):
        engine, world = fixture()
        win(world)
        world['scene']['dead'] = False
        self.service.managed = engine
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            self.service._managed_update_replay_poll(world)
            retry = {'key':('online_sync_pending',), 'action_kind':'enter_dungeon',
                     'reason_code':'completion_replay', 'source_run_id':'first'}
            self.service.managed_retry = retry
            self.assertEqual(self.service._managed_poll_interval(), .05)
            for key, value in (('source_run_id','next'), ('action_kind','return_town'),
                               ('reason_code','pumpkin_replay'), ('key',('game_busy',))):
                self.service.managed_retry = {**retry, key:value}
                self.assertEqual(self.service._managed_poll_interval(), 2)
            self.service.managed_retry = retry
            self.service.managed_transition = {'submitted':True}
            self.assertEqual(self.service._managed_poll_interval(), 2)

    def test_online_saving_does_not_hide_kill_signal_or_authorize_leaving_loot(self):
        engine, world = fixture()
        win(world)
        world['scene'].update(dead=False, online_saving=True)
        self.service.managed = engine
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            self.service._managed_update_replay_poll(world)
            self.assertEqual(self.service._managed_poll_interval(), .05)
            self.assertFalse(engine.completion_rewards.ready(world))
            self.assertIsNone(engine.completion_rewards.empty_since)
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=108):
            self.assertEqual(self.service._managed_poll_interval(), 2)

    def test_runtime_replay_capture_is_read_only_and_uses_one_connection_lock(self):
        client = RuntimeClient()
        client._reader = SimpleNamespace(session_id='connection')
        actions = Mock()
        client._actions = actions
        def read(value):
            def capture(*args, **kwargs):
                self.assertTrue(client._guard._is_owned())
                return value
            return capture
        actions.control_snapshot.side_effect = read({'available':True})
        actions.progression_snapshot.side_effect = read({'available':True})
        with patch.object(client, 'snapshot', side_effect=read({'bridge_session':'connection'})) as snap:
            for merchant in (False, True):
                actions.reset_mock()
                result = client.completion_replay_snapshot(merchant=merchant)
                snap.assert_called_with(save=False)
                self.assertEqual(result['session'], 'connection')
                self.assertEqual(actions.method_calls, [unittest.mock.call.control_snapshot()] +
                    ([unittest.mock.call.progression_snapshot(merchant=True)] if merchant else []))


class CompletionReplaySignalTests(unittest.TestCase):
    def test_signal_is_guarded_hint_and_never_installs_or_invokes_dispatcher(self):
        for case in ('running', 'kill', 'finished', 'popup', 'town', 'changing'):
            with self.subTest(case=case):
                game = GameActions(Mock(), dispatcher=Mock())
                read = Mock(); read.guards = []
                read.ptr.return_value = 2000
                read.boolean.side_effect = lambda address: (case == 'finished' and address == 2248
                    or case == 'kill' and address == 2249)
                copy = Mock(); copy.guards = [(2000, b'type')]
                copy.verify.return_value = 2000
                game._context = Mock(return_value=(read, 1000, 3000, 1 if case == 'town' else 0))
                game._completion_state = Mock(return_value={'CombatMapCompletionUI':case == 'popup'})
                if case == 'changing': read.finish.side_effect = SnapshotChangedError('changed')
                with patch('deskrawl_assistant.recommendation_reader._Copy', return_value=copy):
                    if case == 'changing':
                        with self.assertRaises(SnapshotChangedError): game.completion_replay_signal()
                    else:
                        self.assertEqual(game.completion_replay_signal(), case in ('kill', 'finished', 'popup'))
                game._context.assert_called_once_with(allow_busy=True)
                self.assertIn(copy.guards[0], read.guards)
                read.finish.assert_called_once()
                game.dispatcher.invoke.assert_not_called()
                game.dispatcher.install.assert_not_called()


class CompletionReplaySerialPollTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp
    captured = CompletionReplayLatencyTests.captured

    def prepare_fast_tick(self, mythic=False):
        engine, world = fixture(mythic)
        win(world, mythic)
        world.update(character_id='hero')
        world['scene'].update(dead=False, online_saving=False, online_pending_requests=0)
        engine.character_id = 'hero'
        self.service.managed = engine
        self.service.managed_transition = self.service.managed_retry = None
        self.service._managed_active_rules = list(self.service.rules)
        self.service._managed_active_automation = deepcopy(self.service.automation_settings)
        value = self.captured(world)
        value['character'] = {'available':True, 'session':world['session'], 'character_id':'hero',
                              'equipment':[], 'minion_equipment':[]}
        value['control']['carriage'] = {'available':True, 'complete':True, 'items':[]}
        self.service.managed_control = deepcopy(value['control'])
        self.service.managed_replay_poll = {'active':True, 'key':(world['session'],'first'), 'until':108}
        self.client.completion_replay_signal = Mock(return_value=True)
        self.client.completion_replay_snapshot = Mock(side_effect=lambda **kwargs: deepcopy(value))
        return engine, world, value

    def test_entire_settlement_tick_avoids_slow_full_projection_and_submits_once(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic):
                engine, world, value = self.prepare_fast_tick(mythic)
                self.client.actions.clear()
                self.client.native_handler = lambda *args: {'transition_requested':True, 'effect_confirmed':False}
                with patch.object(self.service, '_managed_observation', side_effect=AssertionError('slow full projection')), \
                        patch.object(self.service, '_publish', side_effect=AssertionError('display projection')), \
                        patch('deskrawl_assistant.managed_service.time.monotonic') as clock:
                    for now in (100, 100.2, 100.4, 100.6, 100.8):
                        clock.return_value = now
                        self.service._managed_once()
                        self.assertEqual(self.client.actions, [])
                    clock.return_value = 101.01
                    self.service._managed_once()
                    self.assertEqual(len(self.client.actions), 1)
                    self.assertEqual(self.client.actions[0][1]['reason_code'], 'completion_replay')
                    self.assertIsNotNone(self.service.managed_transition)
                self.assertEqual(engine.repeat_completed, 1)

    def test_online_request_can_overlap_empty_ground_wait_but_cannot_authorize_replay(self):
        engine, world, value = self.prepare_fast_tick()
        value['control']['scene']['online_pending_requests'] = 1
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            self.assertIsNone(engine.next_completion_replay(self.service._managed_completion_observation()))
            self.assertEqual(engine.completion_rewards.empty_since, 100)
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=101.1):
            self.assertIsNone(engine.next_completion_replay(self.service._managed_completion_observation()))
        value['control']['scene']['online_pending_requests'] = 0
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=101.2):
            action = engine.next_completion_replay(self.service._managed_completion_observation())
            self.assertEqual(action['reason_code'], 'completion_replay')
        self.assertEqual(self.client.actions, [])

    def test_changed_character_or_partial_inventory_cannot_use_fast_path(self):
        for change in ('character', 'session', 'partial'):
            with self.subTest(change=change):
                engine, world, value = self.prepare_fast_tick()
                if change == 'character': value['character']['character_id'] = 'other'
                elif change == 'session': value['character']['session'] = 'other'
                else: value['snapshot']['complete'] = False
                with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                    if change == 'partial':
                        self.assertIsNone(self.service._managed_completion_observation())
                    else:
                        with self.assertRaises(SnapshotChangedError):
                            self.service._managed_completion_observation()
                self.assertEqual(self.client.actions, [])

    def test_fast_read_keeps_metrics_equivalent_to_full_view_and_late_drop_blocks_replay(self):
        engine, world, value = self.prepare_fast_tick()
        raw = equipment('new', container='inventory', rarity=3, is_black_mist=True)
        raw.pop('rarity')
        raw.update(base_rarity=3)
        value['snapshot']['containers']['inventory']['slots'] = [raw]
        value['snapshot']['containers']['inventory']['occupied_count'] = 1
        value['control']['gold_ledger'] = {'available':True, 'id':'gold', 'total':100}
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            observation = self.service._managed_completion_observation()
            self.assertIsNotNone(observation)
            engine.next_completion_replay(observation)
            self.assertEqual(engine.metrics.state()['equipment']['black_mist']['count'], 1)
            self.assertEqual(engine.repeat_completed, 1)
        value['control']['ground'] = {'complete':True, 'items':[equipment('late', container='ground')]}
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=101.2):
            observation = self.service._managed_completion_observation()
            action = engine.next_completion_replay(observation)
            self.assertEqual(action['kind'], 'collect_ground')
            self.assertIsNone(engine.completion_rewards.empty_since)
        engine.pending = None
        value['control']['ground']['items'] = []
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=101.4):
            self.assertIsNone(engine.next_completion_replay(self.service._managed_completion_observation()))
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=102.41):
            self.assertEqual(engine.next_completion_replay(self.service._managed_completion_observation())['reason_code'], 'completion_replay')

    def test_capacity_rotation_and_merchant_return_conditions_fall_back_to_full_planner(self):
        for change in ('slots', 'chests', 'materials', 'rotation', 'merchant', 'carriage'):
            with self.subTest(change=change):
                engine, world, value = self.prepare_fast_tick()
                if change == 'slots': value['snapshot']['containers']['inventory']['occupied_count'] = 40
                elif change == 'chests': value['snapshot']['containers']['inventory']['slots'] = [loot('boxes','chest','TreasureChest3',count=30)]
                elif change == 'materials': value['control']['scene']['maps_by_difficulty'][engine.target_difficulty][0]['can_enter'] = False
                elif change == 'rotation':
                    engine.repeat_maps[0]['runs'] = 1
                    engine.repeat_maps.append({'dungeon_id':'b', 'difficulty':engine.target_difficulty, 'runs':2})
                elif change == 'merchant': engine.settings['merchant']['enabled'] = engine.merchant_pending = True
                else: value['control']['carriage']['complete'] = False
                with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                    self.assertIsNone(self.service._managed_completion_observation())
                self.assertEqual(self.client.actions, [])

    def test_config_stop_receipt_and_pause_disable_lightweight_path(self):
        for change in ('config', 'rules', 'organizer', 'stop', 'paused', 'receipt', 'pending', 'retry', 'transition'):
            with self.subTest(change=change):
                engine, world, value = self.prepare_fast_tick()
                if change == 'config': self.service.managed_settings_pending = deepcopy(engine.settings)
                elif change == 'rules': self.service._managed_active_rules = [object()]
                elif change == 'organizer': self.service._managed_active_automation = {}
                elif change == 'stop': engine.finishing = True
                elif change == 'paused': engine.running = False
                elif change == 'receipt': engine.await_effect = {'kind':'equip'}
                elif change == 'pending': engine.pending = {'kind':'enter_dungeon'}
                elif change == 'retry': self.service.managed_retry = {'key':('game_busy',)}
                else: self.service.managed_transition = {'action':{'kind':'enter_dungeon'}}
                with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                    self.assertFalse(self.service._managed_replay_probe_enabled())
                    self.assertIsNone(self.service._managed_completion_observation())
                self.client.completion_replay_snapshot.assert_not_called()
                self.service.managed_settings_pending = None
                self.service.managed_retry = self.service.managed_transition = None

    def test_between_full_reads_combat_only_reads_signal_and_stuck_popup_does_not_spin_full_scan(self):
        for signal, expired in ((False, False), (True, True)):
            with self.subTest(signal=signal, expired=expired):
                engine, world, value = self.prepare_fast_tick()
                self.service.managed_replay_poll = ({'active':True, 'key':(world['session'],'first'), 'until':99}
                                                   if expired else None)
                self.service.managed_full_read_due = 102
                self.client.completion_replay_signal.return_value = signal
                with patch.object(self.service, '_managed_observation', side_effect=AssertionError('early full read')), \
                        patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                    self.service._managed_once()
                self.client.completion_replay_signal.assert_called_once()
                self.client.completion_replay_snapshot.assert_not_called()
                self.assertEqual(self.client.actions, [])

    def test_boss_signal_wakes_authoritative_read_before_regular_poll_is_due(self):
        engine, world, value = self.prepare_fast_tick()
        self.service.managed_replay_poll = None
        self.service.managed_full_read_due = 102
        with patch.object(self.service, '_managed_observation', side_effect=AssertionError('slow full read')), \
                patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            self.service._managed_once()
        self.client.completion_replay_snapshot.assert_called_once_with(merchant=False, statistics=True)
        self.assertTrue(self.service.managed_replay_poll['active'])
        self.assertEqual(self.client.actions, [])

    def prepare_kill(self, mythic=False):
        engine, world, value = self.prepare_fast_tick(mythic)
        value['control']['scene']['completion_visible'] = {}
        value['control']['scene']['run'].update(status='settling', completion_confirmed=True,
            final_kill_settled=True, finished=False, wave_active=False, enemy_count=0,
            wave_index=5, wave_count=5)
        return engine, world, value

    def test_pre_popup_reward_check_overlaps_settlement_and_replays_on_first_popup_sample(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic):
                engine, world, value = self.prepare_kill(mythic)
                self.client.actions.clear()
                self.client.native_handler = lambda *args: {'transition_requested':True, 'effect_confirmed':False}
                with patch.object(self.service, '_managed_observation', side_effect=AssertionError('slow full read')), \
                        patch('deskrawl_assistant.managed_service.time.monotonic') as clock:
                    for now in (100, 100.25, 100.5, 101.1, 102.5):
                        clock.return_value = now
                        self.service._managed_once()
                        self.assertFalse(engine.completion_rewards.settled)
                        self.assertEqual(self.client.actions, [])
                    self.assertEqual(engine.completion_rewards.empty_since, 100)
                    win({'scene':value['control']['scene']}, mythic)
                    value['control']['scene']['run']['finished'] = True
                    clock.return_value = 102.55
                    self.service._managed_once()
                    self.assertEqual(len(self.client.actions), 1)
                    self.assertEqual(self.client.actions[0][1]['reason_code'], 'completion_replay')
                    clock.return_value = 102.6
                    # An accepted native transition disables the fast path.
                    self.assertFalse(self.service._managed_replay_probe_enabled())
                self.assertEqual(engine.repeat_completed, 1)

    def test_pre_popup_late_drop_is_picked_up_and_requires_new_quiet_interval(self):
        engine, world, value = self.prepare_kill()
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            self.service._managed_once()
        drop = equipment('late-boss', container='ground')
        value['control']['ground']['items'] = [drop]
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=101.2):
            observed = self.service._managed_completion_observation()
            action = engine.next_completion_replay(observed)
        self.assertEqual((action['kind'], action['item']['item_uid']), ('collect_ground','late-boss'))
        self.assertIsNone(engine.completion_rewards.empty_since)
        verified(engine, action)
        value['control']['ground']['items'] = []
        value['snapshot']['containers']['inventory']['slots'] = [dict(drop, container='inventory')]
        value['snapshot']['containers']['inventory']['occupied_count'] = 1
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=101.3):
            fresh = self.service._managed_replay_observation(statistics=True)
            self.assertTrue(engine._effect_visible(fresh, fresh['items']))
            self.assertIsNone(engine.next_completion_replay(self.service._managed_completion_observation()))
        win({'scene':value['control']['scene']})
        for now in (101.4, 102.29):
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=now):
                self.assertIsNone(engine.next_completion_replay(self.service._managed_completion_observation()))
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=102.31):
            self.assertEqual(engine.next_completion_replay(self.service._managed_completion_observation())['reason_code'],
                             'completion_replay')
        self.assertEqual(self.client.actions, [])

    def test_failed_read_discards_prepared_empty_interval(self):
        engine, world, value = self.prepare_kill()
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
            self.service._managed_once()
        self.assertEqual(engine.completion_rewards.empty_since, 100)
        self.client.completion_replay_snapshot.side_effect = SnapshotChangedError('loot changed')
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=101), \
                self.assertRaises(SnapshotChangedError):
            self.service._managed_completion_observation()
        self.assertIsNone(engine.completion_rewards.empty_since)
        self.assertEqual(self.client.actions, [])

    def test_native_finished_proof_dispatches_at_first_popup_tick_after_late_drops(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic):
                engine, world, value = self.prepare_kill(mythic)
                value['control']['scene']['run'].update(status='success', finished=True,
                    final_kill_settled=False, rewards_settled=True,
                    reward_evidence='verified_WaveManager_finished_and_LootManager_idle')
                win({'scene':value['control']['scene']}, mythic)
                self.client.actions.clear()
                self.client.native_handler = lambda *args: {'transition_requested':True, 'effect_confirmed':False}
                with patch.object(self.service, '_managed_observation', side_effect=AssertionError('slow full read')), \
                        patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                    self.service._managed_once()
                self.assertEqual(len(self.client.actions), 1)
                self.assertEqual(self.client.actions[0][1]['reason_code'], 'completion_replay')
                self.assertEqual(engine.repeat_completed, 1)
                self.assertFalse(self.service._managed_replay_probe_enabled())


class CompletionReplayNativeTests(unittest.TestCase):
    def test_loot_spawner_counter_is_guarded_through_submission(self):
        for pending in (0, 1, 8192, -1, 8193):
            with self.subTest(pending=pending):
                reader, dispatcher = Mock(), Mock()
                memory = {3076:struct.pack('<i',pending)}
                reader.memory.read.side_effect = lambda address, size: memory[address]
                read = _Read(reader, load_profile())
                read.singleton = Mock(return_value=3000)
                game = GameActions(reader, dispatcher=dispatcher)
                if pending < 0 or pending > 8192:
                    with self.assertRaises(SnapshotChangedError): game._completion_loot_idle(read)
                else:
                    self.assertEqual(game._completion_loot_idle(read), pending == 0)
                    read.finish()
                    memory[3076]=struct.pack('<i',pending+1)
                    with self.assertRaises(SnapshotChangedError): read.finish()
                dispatcher.invoke.assert_not_called()

    def test_original_handlers_keep_native_busy_unlock_and_material_gates(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic), ExitStack() as stack:
                read, reader, dispatcher = Mock(), Mock(), Mock()
                read.guards = []
                read.boolean.return_value = False
                read.integer.return_value = 1
                read.singleton.return_value = 3
                dispatcher.invoke.return_value = {}
                game = GameActions(reader, dispatcher=dispatcher, allow_unverified=True)
                key, difficulty = (floor_id(22), 'Mythic') if mythic else ('a', 'Normal')
                control = {'available':True, 'difficulty':difficulty, 'map_id':key,
                           'maps':[{'map_id':key, 'can_enter':True}]}
                for name, value in (('capabilities', {'compatible':True}), ('_context', (read, 1, 2, 0)),
                                    ('control_snapshot', control), ('_map_objects', {'MythicDungeon' if mythic else key:1000})):
                    stack.enter_context(patch.object(game, name, return_value=value))
                empty = stack.enter_context(patch.object(game, '_require_empty_town'))
                guard = stack.enter_context(patch.object(game, '_require_completion_replay'))
                stack.enter_context(patch.object(game, '_mythic_replay_ui', return_value=2000))
                stack.enter_context(patch.object(game, '_pumpkin_replay_ui', return_value=2000))
                stack.enter_context(patch.object(game, '_require_travel_ready'))
                game.execute('enter_map', {'map_id':key, 'difficulty':difficulty,
                    'reason_code':'completion_replay', 'source_run_id':'first'})
                empty.assert_not_called()
                guard.assert_called_once()
                calls = dispatcher.invoke.call_args.args[0]
                expected = ['GameManager.travel_busy'] + (['SaveSystem.mythic_unlocked'] if mythic else [])
                expected += ['TownPortalSystem.entry_materials', ('Mythic' if mythic else 'CombatMap')+'CompletionUI.replay']
                self.assertEqual([c.method for c in calls], expected)
                self.assertTrue(calls[0].require_false)
                self.assertTrue(all(c.require_true and c.mutates is False for c in calls[1:-1]))
                self.assertEqual(calls[-1].instance, 2000)
                compile_mailbox(0x100000, 0x180000000, load_profile(), 0x300000, 0x400000, 100, calls, [])

    def test_guard_rejects_stale_floor_plan_popup_late_drop_and_native_replay_races(self):
        for mythic in (False, True):
            for change in ('none', 'plan', 'map', 'difficulty', 'dead', 'popup', 'busy', 'floor',
                           'unfinished', 'enemy', 'active', 'late_drop', 'spawning', 'unknown_spawner'):
                with self.subTest(mythic=mythic, change=change), ExitStack() as stack:
                    key, difficulty = (floor_id(22), 'Mythic') if mythic else ('a', 'Normal')
                    read, copy = Mock(), Mock()
                    read.guards = []
                    read.boolean.return_value = change == 'busy'
                    read.singleton.return_value = 3000
                    read.integer.return_value = None if change == 'unknown_spawner' else int(change == 'spawning')
                    copy.guards = [(100, b'guard')]
                    game = GameActions(Mock(), dispatcher=Mock())
                    state = {'map_id':key, 'difficulty':difficulty, 'world_difficulty':'Inferno' if mythic else 'Normal',
                             'run':{'id':'first', 'status':'success'}}
                    raw = {'id':'first', 'map_id':key, 'difficulty':difficulty, 'finished':True,
                           'final_kill_settled':False, 'enemy_count':0, 'wave_active':False}
                    if change == 'plan': raw['id'] = 'native-next'
                    elif change == 'map': state['map_id'] = 'b'
                    elif change == 'difficulty': state['difficulty'] = 'Nightmare'
                    elif change == 'dead': state['dead'] = True
                    elif change == 'floor': raw['map_id'] = floor_id(23)
                    elif change == 'unfinished': raw['finished'] = False
                    elif change == 'enemy': raw['enemy_count'] = 1
                    elif change == 'active': raw['wave_active'] = True
                    stack.enter_context(patch('deskrawl_assistant.recommendation_reader._Copy', return_value=copy))
                    campaign = stack.enter_context(patch('deskrawl_assistant.recommendation_reader._campaign_run', return_value=raw))
                    stack.enter_context(patch.object(game, '_completion_state', return_value={
                        ('Mythic' if mythic else 'CombatMap')+'CompletionUI':change != 'popup'}))
                    stack.enter_context(patch.object(game, '_ground', return_value=[{}] if change == 'late_drop' else []))
                    payload = {'map_id':key, 'difficulty':difficulty, 'source_run_id':'first'}
                    if change == 'none':
                        game._require_completion_replay(read, 1, 0, payload, state)
                        self.assertIn((100, b'guard'), read.guards)
                        campaign.assert_called_once_with(copy, 1, state['world_difficulty'], allow_settled_replay=True)
                    else:
                        with self.assertRaises((GameBusyError, SnapshotChangedError)):
                            game._require_completion_replay(read, 1, 0, payload, state)
                    game.dispatcher.invoke.assert_not_called()

    def test_rift_singleton_visibility_and_inflight_fence_are_guarded(self):
        for change in ('none', 'uninitialized', 'hidden', 'inflight', 'changed'):
            with self.subTest(change=change):
                reader = Mock()
                reader.module = {'base':0}
                game = GameActions(reader, dispatcher=Mock())
                root = game.profile['statics']['MythicCompletionUI']
                memory = {root:struct.pack('<Q',1000), 1184:struct.pack('<Q',2000),
                          2000:struct.pack('<Q',3000), 2008:b'\1', 3233:b'\0'}
                if change == 'uninitialized': memory[root] = struct.pack('<Q',game.profile['uninitialized_type_tokens']['MythicCompletionUI'])
                elif change == 'hidden': memory[2008] = b'\0'
                elif change == 'inflight': memory[3233] = b'\1'
                reader.memory.read.side_effect = lambda addr,size: memory[addr]
                read = _Read(reader, game.profile)
                read.klass = Mock(side_effect=lambda addr,name: addr)
                read.verify = Mock(side_effect=lambda addr,name: addr)
                if change in ('none', 'changed'):
                    self.assertEqual(game._mythic_replay_ui(read), 3000)
                    read.finish()
                    if change == 'changed':
                        memory[3233] = b'\1'
                        with self.assertRaises(SnapshotChangedError): read.finish()
                else:
                    with self.assertRaises(GameBusyError): game._mythic_replay_ui(read)


class ClearedCompletionPlanTests(unittest.TestCase):
    def copied(self, mythic):
        copy = CampaignCopy()
        copy.values[copy.manager+128] = struct.pack('<Q', 0)
        copy.values[copy.wave+40] = struct.pack('<Q', copy.active_map)
        copy.values[copy.wave+248] = b'\1'
        copy.values[copy.wave+249] = b'\1'
        copy.integer_value(copy.wave+48, 2)
        if mythic:
            copy.map_name = copy.strings[0x9100] = 'MythicDungeon'
            copy.strings[0x9200] = 'Inferno1'
            copy.integer_value(copy.active_map+32, 1)
            copy.integer_value(copy.plan+72, 22)
            copy.integer_value(copy.wave+304, 22)
        return copy

    def recover(self, copy, mythic):
        from deskrawl_assistant.recommendation_reader import _campaign_run
        return _campaign_run(copy, copy.manager, 'Inferno' if mythic else 'Normal', allow_settled_replay=True)

    def test_live_cleared_plan_shape_reaches_replay_without_reviving_cached_terminal(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic):
                copy = self.copied(mythic)
                self.assertIsNone(copy.run('Inferno' if mythic else 'Normal'))
                recovered = self.recover(copy, mythic)
                copy.finish()
                run = normalise_run(recovered, {recovered['map_id']}, completion_visible={
                    ('Mythic' if mythic else 'CombatMap')+'CompletionUI': True})
                self.assertEqual(run['status'], 'success')
                self.assertNotIn('retained_terminal', run)
                engine, world = fixture(mythic)
                engine.target_dungeon = recovered['map_id']
                engine.repeat_maps[0]['dungeon_id'] = recovered['map_id']
                world['scene'].update(dungeon_id=recovered['map_id'], run=run,
                    completion_visible={('Mythic' if mythic else 'CombatMap')+'CompletionUI':True},
                    maps_by_difficulty={run['difficulty']:[{'map_id':run['map_id'], 'can_enter':True}]})
                action = after_rewards(engine, world)
                self.assertEqual(action['reason_code'], 'completion_replay')
                self.assertEqual(action['source_run_id'], recovered['id'])

    def test_recovery_rejects_unfinished_wrong_map_floor_or_active_enemies(self):
        for mythic in (False, True):
            for change in ('unfinished','active','wrong_map','no_plan','early_wave','enemy','wrong_type'):
                with self.subTest(mythic=mythic, change=change):
                    copy = self.copied(mythic)
                    if change == 'unfinished': copy.values[copy.wave+248] = b'\0'
                    elif change == 'active': copy.values[copy.wave+64] = b'\1'
                    elif change == 'wrong_map': copy.values[copy.wave+40] = struct.pack('<Q', 123)
                    elif change == 'no_plan': copy.values[copy.wave+88] = struct.pack('<Q', 0)
                    elif change == 'early_wave': copy.integer_value(copy.wave+48, 1)
                    elif change == 'wrong_type': copy.integer_value(copy.active_map+32, 2)
                    else:
                        copy.list = lambda *a, **k: [struct.pack('<Q', 0xA000)]
                        copy.values[0xA000+296] = b'\0'
                    self.assertIsNone(self.recover(copy, mythic))
            if mythic:
                copy = self.copied(mythic)
                copy.integer_value(copy.wave+304, 23)
                with self.assertRaises(SnapshotChangedError): self.recover(copy, True)

    def test_auto_replay_after_read_invalidates_plan_and_completion_guards(self):
        for mythic in (False, True):
            for field, raw in ((CampaignCopy.manager+128, struct.pack('<Q', CampaignCopy.plan)),
                               (CampaignCopy.wave+88, struct.pack('<Q', 0)),
                               (CampaignCopy.wave+248, b'\0'), (CampaignCopy.wave+64, b'\1')):
                with self.subTest(mythic=mythic, field=field):
                    copy = self.copied(mythic)
                    self.assertIsNotNone(self.recover(copy, mythic))
                    copy.finish()
                    copy.values[field] = raw
                    with self.assertRaises(SnapshotChangedError): copy.finish()


if __name__ == '__main__':
    unittest.main()
