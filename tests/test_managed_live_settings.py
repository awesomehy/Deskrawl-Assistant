"""Runtime edits use synthetic native actions only; submitted work stays fenced."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from deskrawl_assistant.managed_mode import ManagedMode
from deskrawl_assistant.models import ValidationError
import test_managed_service as service_fixtures
from test_managed_service import equipment
from test_managed_mode import observation, settings


class LiveSettingsTests(unittest.TestCase):
    setUp = service_fixtures.ManagedServiceTests.setUp
    settings = service_fixtures.ManagedServiceTests.settings
    ready = service_fixtures.ManagedServiceTests.ready

    def rule(self):
        return {'id': 'live', 'name': '运行中修改', 'enabled': True,
                'equipment_keys': ['LegendaryBelt3'], 'group_mode': 'all',
                'groups': {'primary': {'selected_stats': ['Stats.Dexterity'], 'operator': '>=', 'count': 1}}}

    def test_edit_during_native_lock_keeps_receipt_then_uses_new_storage_pages_and_rules(self):
        self.client.items = [equipment(is_black_mist=True)]
        self.ready(farming=True)
        old = deepcopy(self.service.managed.settings)
        new = deepcopy(old)
        new['cleanup']['equipment_pages'] = [5]
        lock = self.client.lock_equipment

        def save_during_lock(row, validate):
            self.service.save_managed(new)
            self.service.save_rule(self.rule())
            self.assertEqual(self.service.managed.settings, old)
            self.assertEqual(self.service.managed_page()['settings']['cleanup']['equipment_pages'], [5])
            self.assertEqual(json.loads(self.service.managed_config.read_text(encoding='utf-8'))['cleanup']['equipment_pages'], [5])
            self.assertEqual(len(self.service.state()['rules']), 1)
            self.assertEqual(self.service._managed_observation()['items'][0]['rule_status'], 'no_rule')
            return lock(row, validate)

        self.client.lock_equipment = save_during_lock
        for _ in range(4):
            self.service._managed_once()
            if self.client.actions: break
        self.assertTrue(self.client.items[0]['locked'])
        self.assertIsNotNone(self.service.managed.await_effect)
        self.service._managed_once()
        self.assertEqual(self.service.managed.settings['cleanup']['equipment_pages'], [5])
        self.assertEqual(len(self.service._managed_active_rules), 1)
        self.assertIsNone(self.service.managed.await_effect)
        self.assertEqual(len(self.client.actions), 1)  # No extra write while applying.
        self.service._managed_once()
        self.assertEqual(self.client.items[0]['container'], 'storage')
        self.assertIn(self.client.items[0]['slot_index'], range(200, 250))
        self.assertTrue(self.service.managed.running)

    def test_submitted_craft_and_upload_waits_finish_before_settings_change(self):
        self.ready()
        old = deepcopy(self.service.managed.settings)
        new = deepcopy(old)
        new['auto_equip']['minimum_delta'] = 100
        self.service.save_managed(new)
        self.service.managed_synthesis_wait = {'submitted': True}
        with patch.object(self.service, '_managed_wait_synthesis', return_value='wait'):
            self.service._managed_once()
        self.assertEqual(self.service.managed.settings, old)
        self.service.managed_synthesis_wait = None
        with patch.object(self.service, '_managed_wait_auction', return_value=True):
            self.service._managed_once()
        self.assertEqual(self.service.managed.settings, old)
        self.assertFalse(self.client.actions)

    def test_unverified_item_or_submitted_travel_does_not_apply_the_queued_config(self):
        self.ready()
        old = deepcopy(self.service.managed.settings)
        new = deepcopy(old)
        new['auto_equip']['minimum_delta'] = 100
        self.service.save_managed(new)
        world = self.service._managed_observation()
        self.service.managed_transition = {'submitted': True}
        self.assertFalse(self.service._managed_apply_configuration(world))
        self.service.managed_transition = None
        with patch.object(self.service.managed, '_effect_visible', return_value=False):
            self.assertTrue(self.service._managed_apply_configuration(world))
        self.assertEqual(self.service.managed.settings, old)
        self.assertIsNotNone(self.service.managed_settings_pending)
        self.assertFalse(self.client.actions)

    def test_invalid_edit_and_multiple_saves_preserve_statistics_and_pause_resume(self):
        self.ready()
        self.service.managed.metrics.gold = 123
        before = self.service.managed_config.read_bytes()
        bad = deepcopy(self.service.managed.settings)
        bad['cleanup']['rune_pages'] = []
        with self.assertRaises(ValidationError): self.service.save_managed(bad)
        self.assertEqual(self.service.managed_config.read_bytes(), before)
        for amount in (10, 20):
            new = deepcopy(self.service.managed_page()['settings'])
            new['auto_equip']['minimum_delta'] = amount
            self.service.save_managed(new)
        self.service.stop_managed(pause=True)
        self.assertTrue(self.service.managed_can_resume)
        self.service.set_managed_running(True)
        self.assertEqual(self.service.managed.settings['auto_equip']['minimum_delta'], 20)
        self.assertEqual(self.service.managed.metrics.gold, 123)

    def test_rule_import_copy_delete_and_export_are_available_during_managed(self):
        self.ready()
        rule = self.rule()
        self.service.save_rule(rule)
        rule['id'] = 'imported'
        self.service.import_rules({'version': 2, 'rules': [rule]}, True)
        self.service.copy_rules('LegendaryBelt3', 'LegendaryBelt1')
        self.assertEqual(len(self.service.export_rules()['rules']), 3)
        self.service.delete_rules(['live'], 'LegendaryBelt3')
        self.assertEqual(len(self.service.rules), 2)
        self.assertEqual(self.service._managed_active_rules, [])
        self.assertTrue(self.service.managed.running)

    def test_paused_organizer_save_preserves_pending_managed_changes_and_resume_totals(self):
        self.ready()
        self.service.managed.metrics.gold = 123
        new = deepcopy(self.service.managed.settings)
        new['auto_equip']['minimum_delta'] = 50
        self.service.save_managed(new)
        self.service.stop_managed(pause=True)
        auto = deepcopy(self.service.automation_settings)
        auto['runes']['pages'] = [5]
        self.service.save_automation(auto)
        saved = json.loads(self.service.managed_config.read_text(encoding='utf-8'))
        self.assertEqual(saved['auto_equip']['minimum_delta'], 50)
        self.assertEqual(saved['cleanup']['rune_pages'], [5])
        self.assertTrue(self.service.managed_can_resume)
        self.service.set_managed_running(True)
        self.assertEqual(self.service.managed.settings, saved)
        self.assertEqual(self.service.managed.metrics.gold, 123)

    def test_expired_login_cancels_queued_sale_without_losing_other_saved_edits(self):
        self.ready()
        new = deepcopy(self.service.managed.settings)
        new['auto_equip']['minimum_delta'] = 123
        new['auction'].update(upload_enabled=True, sell_enabled=True)
        self.service.managed_settings_pending = new
        self.service._disable_steam_sale()
        self.assertFalse(self.service.managed_settings_pending['auction']['sell_enabled'])
        saved = json.loads(self.service.managed_config.read_text(encoding='utf-8'))
        self.assertEqual(saved['auto_equip']['minimum_delta'], 123)
        self.assertTrue(saved['auction']['upload_enabled'])
        self.assertFalse(saved['auction']['sell_enabled'])


class LiveRouteTests(unittest.TestCase):
    def test_reorder_retains_matching_maps_counts_and_session_statistics(self):
        config = settings()
        config['farming'].update(mode='repeat', repeat_maps=[
            {'dungeon_id': 'a', 'difficulty': 'Normal', 'runs': 10},
            {'dungeon_id': 'b', 'difficulty': 'Normal', 'runs': 20}])
        world = observation(town=True)
        world['scene']['difficulty'] = 'Normal'
        engine = ManagedMode(config)
        engine.start(world)
        engine.repeat_completed = 4
        engine.repeat_counts = {0: 4, 1: 3}
        engine.handled_runs.add('completed-old')
        engine.metrics.gold = 321
        new = deepcopy(config)
        new['farming']['repeat_maps'].reverse()
        engine.update_settings(new, world)
        self.assertEqual(engine.repeat_index, 1)
        self.assertEqual(engine.repeat_completed, 4)
        self.assertEqual(engine.repeat_counts, {0: 3, 1: 4})
        self.assertEqual(engine.target_dungeon, 'a')
        self.assertIn('completed-old', engine.handled_runs)
        self.assertEqual(engine.metrics.gold, 321)
        self.assertTrue(engine.running)

    def test_lower_quota_rotates_without_crediting_old_wins_to_new_map(self):
        config = settings()
        config['farming'].update(mode='repeat', repeat_maps=[
            {'dungeon_id': 'a', 'difficulty': 'Normal', 'runs': 10},
            {'dungeon_id': 'b', 'difficulty': 'Normal', 'runs': 20}])
        world = observation(town=True)
        engine = ManagedMode(config)
        engine.start(world)
        engine.repeat_completed = 8
        new = deepcopy(config)
        new['farming']['repeat_maps'][0]['runs'] = 5
        engine.update_settings(new, world)
        self.assertEqual(engine.target_dungeon, 'b')
        self.assertEqual(engine.repeat_completed, 0)
