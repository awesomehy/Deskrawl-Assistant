"""Offline UI-projection cache regressions; no workers, game or user data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.character import final_stats, panel_attack
from deskrawl_assistant.projection_cache import ProjectionCache
from deskrawl_assistant.rules import rules_from_dict
from deskrawl_assistant.web_service import AssistantService


SOURCE = json.loads((Path(__file__).parent / 'fixtures/service-snapshot.json').read_text(encoding='utf-8'))


def snapshot():
    value = deepcopy(SOURCE)
    value.update(snapshot_id='sample-1', captured_at='2026-11-06T00:00:00Z',
                 bridge_session='projection-test-session', issues=[])
    value['containers']['inventory']['slots'][0]['transfer_key'] = 'transfer-belt'
    value['transfer'] = {'available': True, 'storage_indices': list(range(50)), 'page_size': 50}
    return value


def character():
    model = {'base': {0: 10, 1: 100, 2: 20, 3: 10.5, 4: .2, 5: 1.5},
             'caches': {536: {20: 100, 21: 1}, 544: {}},
             'status': ({}, {}, {}), 'conditional': ({}, {}), 'conversions': []}
    stats = final_stats(model)
    worn = deepcopy(SOURCE['containers']['inventory']['slots'][0])
    worn.update(container='equipped', equipped_slot=5, item_uid='worn-belt',
                class_mask=15, socket_count=0, socketed_gems=[])
    return {'available': True, 'name': '合成角色', 'level': 70, 'max_level': 70,
            'class_mask': 4, 'primary': 1, 'health': 100, 'health_max': 100,
            'attack': panel_attack(stats, 1), 'stats': stats, 'model': model,
            'comparison_model': deepcopy(model), 'equipment': [worn], 'warnings': [],
            'captured_at': '2026-11-06T00:00:00Z', 'session': 'projection-test-session',
            'character_id': 'synthetic-character'}


class FakeClient:
    def __init__(self):
        self.connected = True
        self.pid = SOURCE['pid']
        self.compatibility = {'phase': 'ready', 'message': '合成测试'}
        self.character = character()

    def character_snapshot(self):
        return deepcopy(self.character)

    def close(self):
        self.connected = False


class ServiceProjectionCacheTests(unittest.TestCase):
    def test_failed_projection_is_not_cached_and_next_read_can_recover(self):
        cache = ProjectionCache()
        self.assertEqual(cache.get('old', lambda: {'name': 'old'}), {'name': 'old'})
        failing = Mock(side_effect=ValueError('synthetic incomplete projection'))
        with self.assertRaises(ValueError):
            cache.get('new', failing)
        self.assertEqual(cache.get('new', lambda: {'name': 'new'}), {'name': 'new'})
        self.assertEqual(cache.get('new', failing), {'name': 'new'})
        self.assertEqual(failing.call_count, 1)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Constructors may create Thread objects but cannot execute any targets.
        # join is paired with the disabled start for normal service cleanup.
        for target in ('threading.Thread.start', 'threading.Thread.join',
                       'deskrawl_assistant.web_service.record',
                       'deskrawl_assistant.managed_service.record'):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = FakeClient()
        self.lifecycle = Mock()
        self.lifecycle.state.return_value = {'status': 'running', 'running': True}
        self.profile = Mock()
        self.profile.state.return_value = {'status': 'online', 'nickname': '合成 Steam', 'avatar_url': ''}
        self.service = AssistantService(self.client, Path(self.tmp.name) / 'rules.json',
                                        game_lifecycle=self.lifecycle, steam_client_profile=self.profile)
        self.addCleanup(self.service.close)
        self.service.rules = rules_from_dict({'schema_version': 2, 'rules': [self.rule()]}, self.service.catalog)
        inventory_patch = patch.object(self.service, '_inventory_projection', wraps=self.service._inventory_projection)
        self.inventory = inventory_patch.start()
        self.addCleanup(inventory_patch.stop)
        character_patch = patch.object(self.service, '_character_payload', wraps=self.service._character_payload)
        self.character_payload = character_patch.start()
        self.addCleanup(character_patch.stop)
        self.install(snapshot(), character())

    @staticmethod
    def rule():
        return {'id': 'projection-rule', 'name': '腰带冷却规则', 'equipment_keys': ['LegendaryBelt3'],
                'enabled': True, 'group_mode': 'all',
                'groups': {'primary': {'selected_stats': ['Stats.CooldownReduction'], 'operator': '>=', 'count': 1}}}

    def install(self, snap, hero=None):
        if hero is not None:
            self.client.character = deepcopy(hero)
        self.service._publish(deepcopy(snap))

    def prime(self):
        self.service._inventory_projection_cache.clear()
        self.service._character_projection_cache.clear()
        self.inventory.reset_mock()
        self.character_payload.reset_mock()
        self.install(snapshot(), character())
        return self.service.state()

    def test_new_valid_snapshot_tokens_and_sample_times_reuse_projections_with_current_envelopes(self):
        before = self.service.state()
        for token in ('sample-2', 12, 'sample-3'):
            with self.subTest(token=token):
                snap, hero = snapshot(), character()
                snap.update(snapshot_id=token, captured_at=f'2026-11-06T00:00:{12 if token == 12 else 20}Z')
                hero['captured_at'] = '2026-11-06T00:05:00Z'
                self.install(snap, hero)
                after = self.service.state()
                self.assertEqual(before['items'], after['items'])
                self.assertEqual(before['counts'], after['counts'])
                self.assertEqual(after['captured_at'], snap['captured_at'])
                self.assertEqual(after['character']['captured_at'], hero['captured_at'])
                self.assertEqual(self.inventory.call_count, 1)
                self.assertEqual(self.character_payload.call_count, 1)
                self.assertEqual(self.service.snapshot['snapshot_id'], token, 'native evidence is never normalized in place')

    def test_invalid_or_missing_observation_tokens_never_reuse_valid_snapshot_projection(self):
        for token in (None, '', ' ', 0, -1, True):
            with self.subTest(token=token):
                self.prime()
                changed = snapshot()
                changed['snapshot_id'] = token
                self.install(changed)
                self.service.state()
                self.assertEqual(self.inventory.call_count, 2)
        self.prime()
        changed = snapshot()
        del changed['snapshot_id']
        self.install(changed)
        self.service.state()
        self.assertEqual(self.inventory.call_count, 2)

    def test_missing_and_empty_sample_times_do_not_reuse_valid_sample_projection(self):
        for stamp in (None, '', ' '):
            with self.subTest(stamp=stamp):
                self.prime()
                snap, hero = snapshot(), character()
                snap['captured_at'] = stamp
                hero['captured_at'] = stamp
                self.install(snap, hero)
                value = self.service.state()
                self.assertEqual(self.inventory.call_count, 2)
                self.assertEqual(self.character_payload.call_count, 2)
                self.assertEqual(value['captured_at'], stamp)
                self.assertEqual(value['character']['captured_at'], stamp)
        self.prime()
        snap = snapshot()
        del snap['captured_at']
        self.install(snap)
        self.assertIsNone(self.service.state()['captured_at'])
        self.assertEqual(self.inventory.call_count, 2)

    def test_completeness_errors_session_and_transfer_evidence_invalidate_inventory(self):
        changes = {
            'incomplete snapshot': lambda snap: snap.update(complete=False),
            'read errors': lambda snap: snap.update(errors=['synthetic read error']),
            'issues': lambda snap: snap.update(issues=['synthetic quality warning']),
            'other process': lambda snap: snap.update(pid=1001),
            'other session': lambda snap: snap.update(bridge_session='new-session'),
            'container incomplete': lambda snap: snap['containers']['inventory'].update(slots_complete=False),
            'identity unverified': lambda snap: snap['containers']['inventory']['slots'][0]['read_evidence'].update(identity_verified=False),
            'lock unverified': lambda snap: snap['containers']['inventory']['slots'][0]['read_evidence'].update(lock_verified=False),
            'transfer unavailable': lambda snap: snap['transfer'].update(available=False),
            'storage unlocks': lambda snap: snap['transfer'].update(storage_indices=[0, 1]),
        }
        for label, mutate in changes.items():
            with self.subTest(label=label):
                self.prime()
                changed = snapshot()
                mutate(changed)
                self.install(changed)
                value = self.service.state()
                self.assertEqual(self.inventory.call_count, 2)
                self.assertEqual(value['complete'], changed['complete'])
                self.assertEqual(value['transfer'], changed['transfer'])
                self.assertEqual(value['issues'], changed['issues'])

    def test_item_identity_lock_quantity_and_affixes_invalidate_and_update_display(self):
        changes = (
            ('item_uid', 'different-uid', 'item_uid'),
            ('instance_id', 'different-instance', 'instance_id'),
            ('transfer_key', 'different-transfer', 'selection_id'),
            ('locked', True, 'locked'),
            ('count', 2, 'count'),
            ('slot_index', 9, 'slot_index'),
            ('item_level', 71, 'level'),
        )
        for field, new_value, output_field in changes:
            with self.subTest(field=field):
                self.prime()
                # In-place changes must not change the cache's saved key by alias.
                self.service.snapshot['containers']['inventory']['slots'][0][field] = new_value
                value = self.service.state()
                self.assertEqual(self.inventory.call_count, 2)
                self.assertEqual(value['items'][0][output_field], new_value)
        before = self.prime()
        self.service.snapshot['containers']['inventory']['slots'][0]['modifiers'][0]['value'] = .25
        value = self.service.state()
        self.assertEqual(self.inventory.call_count, 2)
        self.assertNotEqual(before['items'][0]['groups'], value['items'][0]['groups'])

    def test_rule_changes_invalidate_inventory_but_not_unchanged_character(self):
        for field, new_value in (('enabled', False), ('name', '新的规则名称')):
            with self.subTest(field=field):
                self.prime()
                rule = self.rule()
                rule[field] = new_value
                self.service.rules = rules_from_dict({'schema_version': 2, 'rules': [rule]}, self.service.catalog)
                value = self.service.state()
                self.assertEqual(self.inventory.call_count, 2)
                self.assertEqual(self.character_payload.call_count, 1)
                self.assertEqual(value['rules'][0][field], new_value)
                self.service.rules = rules_from_dict({'schema_version': 2, 'rules': [self.rule()]}, self.service.catalog)

    def test_character_changes_invalidate_both_projections_and_refresh_the_payload(self):
        for field, new_value in (('available', False), ('name', '另一角色'), ('level', 71), ('health', 25),
                                 ('warnings', ['synthetic warning']), ('character_id', 'another-character')):
            with self.subTest(field=field):
                self.prime()
                self.service.character[field] = new_value
                value = self.service.state()
                self.assertEqual(self.inventory.call_count, 2)
                self.assertEqual(self.character_payload.call_count, 2)
                self.assertEqual(value['character'][field], new_value)
        self.prime()
        self.service.character['stats'][1] = 123
        value = self.service.state()
        self.assertEqual(self.inventory.call_count, 2)
        self.assertEqual(self.character_payload.call_count, 2)
        self.assertEqual(next(row['value'] for row in value['character']['attributes'] if row['stat'] == 1), 123)
        self.service.character['equipment'][0]['item_uid'] = 'new-equipped-belt'
        value = self.service.state()
        self.assertEqual(self.inventory.call_count, 3)
        self.assertEqual(self.character_payload.call_count, 3)
        self.assertEqual(value['character']['equipment'][0]['selection_id'], 'equipped:new-equipped-belt')

    def test_returned_rows_counts_and_character_cannot_pollute_subsequent_cached_responses(self):
        expected = self.service.state()
        result = self.service.state()
        result['items'][0]['name'] = 'MUTATED'
        result['items'][0]['groups']['primary'].append({'name': 'MUTATED'})
        result['counts']['inventory']['occupied'] = 999
        result['issues'].append('MUTATED')
        result['character']['attributes'][0]['name'] = 'MUTATED'
        result['character']['equipment'][0]['name'] = 'MUTATED'
        actual = self.service.state()
        for key in ('items', 'counts', 'issues', 'character'):
            self.assertEqual(actual[key], expected[key])
        self.assertEqual(self.inventory.call_count, 1)
        self.assertEqual(self.character_payload.call_count, 1)

    def test_dynamic_busy_managed_progress_and_external_status_do_not_use_cached_envelopes(self):
        self.service.state()
        self.service.busy = True
        self.service.message = 'synthetic busy'
        self.service.error = 'synthetic display error'
        self.service.progress = {'done': 2, 'total': 3}
        self.service.monitoring = True
        self.service.managed.running = True
        self.service.managed.phase = 'shopping'
        self.service.managed.status = 'synthetic managed status'
        self.client.compatibility = {'phase': 'changed', 'message': 'synthetic compatibility'}
        self.lifecycle.state.return_value = {'status': 'closing', 'running': True}
        self.profile.state.return_value = {'status': 'logged_out', 'nickname': '', 'avatar_url': ''}
        actual = self.service.state()
        self.assertTrue(actual['busy'])
        self.assertTrue(actual['monitoring'])
        self.assertEqual(actual['message'], 'synthetic busy')
        self.assertEqual(actual['error'], 'synthetic display error')
        self.assertEqual(actual['progress'], {'done': 2, 'total': 3})
        self.assertEqual(actual['managed'], {'running': True, 'phase': 'shopping', 'status': 'synthetic managed status'})
        self.assertEqual(actual['compatibility']['phase'], 'changed')
        self.assertEqual(actual['game']['status'], 'closing')
        self.assertEqual(actual['steam_client']['status'], 'logged_out')
        self.assertEqual(self.inventory.call_count, 1)
        self.assertEqual(self.character_payload.call_count, 1)
        self.service.busy = False
        self.service.updating = True
        self.assertTrue(self.service.state()['busy'])
        self.service.updating = False
        self.assertFalse(self.service.state()['busy'])

    def test_disconnect_discards_inventory_projection_and_never_returns_previous_character(self):
        self.service.state()
        self.client.connected = False
        value = self.service.state()
        self.assertFalse(value['connected'])
        self.assertEqual(value['items'], [])
        self.assertEqual(value['counts'], {})
        self.assertFalse(value['character']['available'])
        self.assertEqual(value['character']['equipment'], [])
        self.assertIsNone(self.service._inventory_projection_cache.value)
        self.assertIsNone(self.service._inventory_projection_cache.key)
        self.client.connected = True
        self.install(snapshot(), character())
        value = self.service.state()
        self.assertEqual(self.inventory.call_count, 2, 'even identical contents rebuild after disconnect')
        self.assertEqual(self.character_payload.call_count, 3)
        self.assertTrue(value['character']['available'])


class ProjectionCacheIsolationTests(unittest.TestCase):
    def test_cache_clones_nested_keys_and_results_and_retains_only_the_latest_value(self):
        cache = ProjectionCache()
        key = {'items': [{'locked': False}]}
        builder = Mock(return_value={'items': [{'name': 'original'}]})
        value = cache.get(key, builder)
        value['items'][0]['name'] = 'client mutation'
        self.assertEqual(cache.get(deepcopy(key), builder)['items'][0]['name'], 'original')
        self.assertEqual(builder.call_count, 1)
        key['items'][0]['locked'] = True
        cache.get(key, builder)
        self.assertEqual(builder.call_count, 2)
        cache.get({'items': [{'locked': False}]}, builder)
        self.assertEqual(builder.call_count, 3, 'cache retains one projection, not an unbounded history')
        cache.clear()
        self.assertIsNone(cache.key)
        self.assertIsNone(cache.value)


if __name__ == '__main__':
    unittest.main()
