"""Real service/planner coordination with synthetic game and Steam adapters."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests import test_managed_service as fixtures
from tests.test_auction_coordinator_audit import TradingFake, STEAMID
from deskrawl_assistant.auction import DEFAULT_AUCTION, next_action
from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.managed_service import required_capabilities
from deskrawl_assistant.native_dispatcher import NativeActionError


class AuctionManagedBridgeTests(unittest.TestCase):
    setUp = fixtures.ManagedServiceTests.setUp

    def enable(self, *, sell=False):
        self.client.items = [fixtures.equipment(key='DivineHelm1', base_rarity=4, rarity=4,
            item_type=3, bound=False, socket_count=0, slot_locked=False, locked=True)]
        self.client.capabilities['auction_upload'] = {'available': True, 'native_verified': True}
        control = self.client.control_snapshot
        self.client.control_snapshot = lambda: {**control(), 'character_id': '0:0:offline-character'}
        self.native = {'available': True, 'enabled': True, 'busy': False,
            'listing_count': 0, 'listing_limit': 8, 'steamid': STEAMID, 'steamid_verified': True}
        self.client.auction_state = Mock(side_effect=lambda: deepcopy(self.native))
        self.identity = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                         'session': 'offline-session', 'character_id': '0:0:offline-character'}
        self.client.steam_identity_snapshot = Mock(side_effect=lambda: deepcopy(self.identity))
        self.service.managed.settings['auction'] = {**deepcopy(DEFAULT_AUCTION),
            'upload_enabled': True, 'sell_enabled': sell, 'categories': ['divine_equipment']}
        self.service.managed.running = True
        self.service.managed.session = 'offline-session'
        self.service.managed.character_id = '0:0:offline-character'
        self.trading = TradingFake()
        self.coordinator = AuctionCoordinator(Path(self.tmp.name), self.trading,
            self.service._auction_policy, start_worker=False)
        self.service.auction_coordinator = self.coordinator

    def plan(self):
        world = self.service._managed_observation()
        action = next_action(self.service.managed.settings, world)
        self.assertIsNotNone(action)
        return action, world

    def test_defaults_never_read_native_auction_or_construct_steam_worker(self):
        self.client.auction_state = Mock(side_effect=AssertionError('optional auction read'))
        self.assertEqual(self.service._managed_observation()['auction'], {})
        self.client.auction_state.assert_not_called()
        self.assertIsNone(getattr(self.service, 'auction_coordinator', None))
        self.assertNotIn('auction_upload', required_capabilities(self.service.managed.settings))

    def test_legacy_configuration_load_save_and_reload_preserve_existing_presets(self):
        from deskrawl_assistant.managed_mode import validate_settings
        from deskrawl_assistant.progression import catalog
        settings = fixtures.ManagedServiceTests.settings(self, farming=True, auto_equip=True)
        settings['auto_equip'].update(minimum_delta=17, disabled_slots=[2, 6])
        settings['farming']['repeat_maps'] = [
            {'dungeon_id': 'offline-map', 'difficulty': 'Normal', 'runs': 37},
            {'dungeon_id': 'second-map', 'difficulty': 'Inferno', 'runs': 19}]
        settings['cleanup'].update(equipment_action='salvage', rune_pages=[1], gem_pages=[5])
        settings['merchant'].update(trigger_percent=93, spend_to_percent=17)
        settings['merchant']['weights'].update({'2': 1, '3': 10})
        settings['reconnect'].update(enabled=True, character_index=3)
        settings['life_skills'].update(priorities=['LifeSkill 66', 'LifeSkill 31'], gold_reserve=777)
        settings['synthesis']['gems'].update(enabled=True, max_quality=5)
        settings['synthesis']['runes'].update(enabled=True, max_quality='grand',
            excluded_keys=['LegendaryRune_HunterSpecial1'])
        for mask, hero in catalog()['heroes'].items():
            node = next(n for n in hero['talents'] if n['point_requirement'] == 0 and not n['prerequisite'])
            ability = next(n for n in hero['abilities'] if 0 in n['slots'])
            settings['progression']['talent_targets'][mask] = {node['id']: 2}
            settings['progression']['skill_stages'][mask] = [{'level': 1, 'slots': [ability['id'], None, None, None]}]
        expected = validate_settings(settings)
        for schema in ('released_141', 'early_development'):
            with self.subTest(schema=schema):
                raw = deepcopy(expected)
                if schema == 'released_141':
                    raw.pop('auction')
                else:
                    raw['auction'].update(allow_locked=True, retain_count=3)
                    raw['auction'].pop('excluded_keys')
                    raw['auction'].pop('categories')
                original = json.dumps(raw).encode('utf-8')
                self.service.managed_config.write_bytes(original)
                self.service._init_managed()
                self.assertEqual(self.service.managed_config_error, '')
                self.assertFalse(self.service.managed_config_load_failed)
                self.assertEqual(self.service.managed.settings, expected)
                self.assertEqual(self.service.managed_config.read_bytes(), original)
                saved = self.service.save_managed(self.service.managed.settings)
                self.service._init_managed()
                self.assertEqual(self.service.managed.settings, expected)
                self.assertEqual(saved, expected)
                self.assertEqual(self.service.automation_settings['runes']['pages'], [1])
                self.assertEqual(self.service.automation_settings['gems']['pages'], [5])
        self.assertEqual(self.client.actions, [])

    def test_manual_entry_state_follows_read_worker_and_invalidates_failed_scene_reads(self):
        self.client.capabilities['auction_upload'] = {'available': True}
        raw = {**self.client.control_snapshot(), 'character_id': '1:0:test-character'}
        with patch.object(self.client, 'control_snapshot', return_value=raw):
            self.service._managed_refresh_control()
        self.assertTrue(self.service.state()['manual_auction_availability']['available'])
        raw['scene']['in_town'] = False
        with patch.object(self.client, 'control_snapshot', return_value=raw):
            self.service._managed_refresh_control()
        self.assertFalse(self.service.state()['manual_auction_availability']['available'])
        raw['scene']['in_town'] = True
        with patch.object(self.client, 'control_snapshot', return_value=raw):
            self.service._managed_refresh_control()
        from deskrawl_assistant.native_memory import SnapshotChangedError
        with patch.object(self.client, 'control_snapshot', side_effect=SnapshotChangedError('scene changed')):
            with self.assertRaises(SnapshotChangedError):
                self.service._managed_refresh_control()
        self.assertFalse(self.service.state()['manual_auction_availability']['available'])

    def test_background_inventory_monitor_keeps_manual_scene_gate_current(self):
        self.client.items = []
        self.client.capabilities['auction_upload'] = {'available': True}
        control = self.client.control_snapshot
        self.client.control_snapshot = lambda: {**control(), 'character_id': '1:0:test-character'}
        for feature in ('monitoring', 'automation_running'):
            with self.subTest(feature=feature):
                self.service.monitoring = feature == 'monitoring'
                self.service.automation_running = feature == 'automation_running'
                for in_town in (True, False, True):
                    self.client.in_town = in_town
                    self.service.managed_control_read_at = None
                    self.service.busy = True
                    self.service._work('monitor', {})
                    self.assertEqual(self.service.error, '')
                    self.assertEqual(self.service.state()['manual_auction_availability']['available'], in_town)
                self.assertTrue(getattr(self.service, feature))
        self.assertEqual(self.client.actions, [])

    def test_actual_reader_identity_evidence_is_usable_without_a_top_level_identity_flag(self):
        self.enable()
        self.assertNotIn('identity_verified', self.client.items[0])
        action, world = self.plan()
        self.assertTrue(world['items'][0]['auction_identity_verified'])
        self.assertTrue(world['items'][0]['auction_candidate'])
        self.assertTrue(world['items'][0]['locked'])
        self.assertNotIn('allow_locked', action)
        self.assertEqual(action['kind'], 'auction_upload')

    def test_explicit_negative_or_inconsistent_identity_never_becomes_a_candidate(self):
        self.enable()
        for patch in ({'identity_verified': False}, {'read_evidence': {'snapshot_consistent': False}}):
            original = deepcopy(self.client.items[0])
            self.client.items[0].update(patch)
            world = self.service._managed_observation()
            self.assertFalse(world['items'][0]['auction_candidate'])
            self.assertIsNone(next_action(self.service.managed.settings, world))
            self.client.items[0] = original

    def test_upload_then_exact_steam_mapping_and_sale_are_separate_serial_stages(self):
        self.enable(sell=True)
        action, world = self.plan()
        # Preparing the inventory baseline queues work without doing HTTP on
        # the game worker. The native action must not run until it is ready.
        deferred = self.service._managed_execute(action, world)
        self.assertTrue(deferred['retryable'])
        self.assertEqual(self.client.actions, [])
        self.assertEqual(self.trading.calls, [])
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[0][0], 'prepare')

        def commit(kind, payload):
            self.assertEqual(kind, 'auction_upload')
            self.assertEqual(payload['expected_steamid'], STEAMID)
            persisted = json.loads(self.coordinator.path.read_text(encoding='utf-8'))
            self.assertEqual(next(iter(persisted['intents'].values()))['phase'], 'submitted')
            self.client.items.clear()
            self.native['listing_count'] += 1
            return {'effect_confirmed': True, 'steam_pending': True, 'quantity': 1,
                    'uploaded_uid': payload['item']['item_uid'],
                    'uploaded_instance_id': payload['item']['instance_id'],
                    'uploaded_internal_name': payload['item']['internal_name']}
        self.client.native_handler = commit
        result = self.service._managed_execute(action, world)
        self.assertTrue(result['verified'])
        intent = next(iter(self.coordinator.intents.values()))
        self.assertEqual(intent['phase'], 'confirmed')
        self.assertEqual(len(self.client.actions), 1)
        self.coordinator.process_once()
        self.assertEqual(intent['phase'], 'resolved')
        self.assertEqual(self.trading.calls[-1][0], 'complete')
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1][0], 'sale')

    def test_proven_prewrite_rejection_skips_only_the_item_and_preserves_managed_running(self):
        self.enable()
        action, world = self.plan()
        def reject(kind, payload):
            raise NativeActionError('synthetic preflight refusal', mutation_started=False)
        self.client.native_handler = reject
        result = self.service._managed_execute(action, world)
        self.assertTrue(result['retryable'])
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.coordinator.intents, {})
        refreshed = self.service._managed_observation()
        self.assertIsNone(next_action(self.service.managed.settings, refreshed))

    def test_unknown_native_result_keeps_durable_fence_and_never_reuploads(self):
        self.enable()
        action, world = self.plan()
        def lost(kind, payload):
            raise NativeActionError('synthetic lost receipt', uncertain=True)
        self.client.native_handler = lost
        with self.assertRaises(NativeActionError):
            self.service._managed_execute(action, world)
        self.assertEqual(next(iter(self.coordinator.intents.values()))['phase'], 'submitted')
        self.assertIsNone(next_action(self.service.managed.settings, self.service._managed_observation()))
        self.assertEqual(len(self.client.actions), 1)

    def test_finishing_during_native_window_wait_prevents_upload(self):
        self.enable()
        action, world = self.plan()
        def waiting(kind, payload, cancelled, *, validate):
            self.service.managed.finishing = True
            self.assertFalse(validate())
            raise NativeActionError('synthetic stop before submission', mutation_started=False)
        self.client.execute_game_action = waiting
        self.assertTrue(self.service._managed_execute(action, world)['retryable'])
        self.assertEqual(self.coordinator.intents, {})

    def test_native_list_read_failure_skips_the_feature_without_poisoning_other_observation(self):
        self.enable()
        self.client.auction_state = Mock(side_effect=ValueError('unsupported native structure'))
        world = self.service._managed_observation()
        self.assertTrue(world['complete'])
        self.assertFalse(world['auction']['available'])
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.client.actions, [])

    def test_open_failure_defers_without_logging_exception_payload_or_creating_intent(self):
        self.enable()
        self.native.update(needs_refresh=True, enabled=None, listing_count=None)
        action, world = self.plan()
        self.assertEqual(action['kind'], 'auction_open')
        self.client.native_handler = Mock(side_effect=NativeActionError(
            'sensitive upstream payload', state=7, last_call=0, mutation_started=False))
        with patch('deskrawl_assistant.managed_service.record') as log:
            result = self.service._managed_execute(action, world)
        self.assertTrue(result['retryable'])
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.coordinator.intents, {})
        log.assert_called_with('managed_auction_open_deferred', error_type='NativeActionError',
                               state=7, last_call=0, uncertain=False)
        self.assertNotIn('sensitive', str(result))

    def test_warehouse_withdrawal_uses_full_character_read_and_precedes_upload(self):
        self.enable()
        self.client.items[0].update(container='storage')
        # The lightweight inventory context deliberately has no character ID.
        self.client.action_context_snapshot = Mock(return_value={
            'available': True, 'scene': {'available': True, 'in_town': True}})
        action, world = self.plan()
        self.assertEqual(action['kind'], 'transfer')
        self.assertEqual(action['reason_code'], 'auction_prepare')
        result = self.service._managed_execute(action, world)
        self.assertTrue(result['verified'])
        self.client.action_context_snapshot.assert_not_called()
        self.assertEqual(self.client.items[0]['container'], 'inventory')
        self.assertEqual(self.plan()[0]['kind'], 'auction_upload')

    def test_already_finishing_drops_stale_auction_plan_without_any_native_write(self):
        self.enable()
        action, world = self.plan()
        self.service.managed.finishing = True
        self.assertTrue(self.service._managed_execute(action, world)['retryable'])
        self.assertEqual(self.client.actions, [])
        self.assertEqual(self.coordinator.intents, {})

    def test_protection_list_changes_invalidate_stale_upload_before_native_execution(self):
        self.enable()
        action, world = self.plan()
        self.service.managed.settings['auction']['excluded_keys'] = ['DivineHelm1']
        self.assertTrue(self.service._managed_execute(action, world)['retryable'])
        self.assertEqual(self.client.actions, [])
        self.assertEqual(self.coordinator.intents, {})

    def test_cached_auction_account_cannot_authorize_a_sale_after_identity_is_unknown(self):
        self.enable(sell=True)
        self.plan()
        self.identity = {'available': False}
        running, _, account = self.service._auction_policy()
        self.assertFalse(running)
        self.assertIsNone(account)
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])

    def test_fresh_sale_identity_rejects_role_and_connection_changes_even_outside_town(self):
        self.enable(sell=True)
        self.plan()
        self.assertEqual(self.service._managed_auction_state({'in_town': False}), {})
        self.assertEqual(self.service.managed_auction, {})
        self.assertEqual(self.service._auction_policy()[2], STEAMID)
        for changes in ({'character_id': '1:1:other-role'}, {'session': 'reconnected'},
                        {'steamid_verified': False}):
            original = deepcopy(self.identity)
            self.identity.update(changes)
            self.assertFalse(self.service._auction_policy()[0])
            self.assertIsNone(self.service._auction_policy()[2])
            self.identity = original

    def test_role_changes_while_market_quote_is_read_cancel_the_submit_boundary(self):
        self.enable(sell=True)
        original = self.trading.sale_once
        def changed(config, steamid, *, validate=None):
            self.identity['character_id'] = '1:1:other-role'
            return original(config, steamid, validate=validate)
        self.trading.sale_once = changed
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])

    def test_fresh_identity_read_runs_without_holding_service_lock_and_rechecks_stop(self):
        self.enable(sell=True)
        def read():
            self.assertFalse(self.service.guard._is_owned())
            self.service.managed.finishing = True
            return deepcopy(self.identity)
        self.client.steam_identity_snapshot.side_effect = read
        self.assertFalse(self.service._auction_policy()[0])

    def test_defaults_and_upload_only_do_not_request_steam_account_reads(self):
        self.enable()
        self.service._auction_policy()
        self.client.steam_identity_snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
