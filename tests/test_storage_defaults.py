"""Partition defaults and unlock transitions; all clients and actions are fake."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.automation import DEFAULT_SETTINGS as AUTO, validate_settings as validate_auto
from deskrawl_assistant.managed_mode import DEFAULT_SETTINGS as MANAGED, ManagedMode, validate_settings as validate_managed
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.web_service import AssistantService
import test_automation as automation_fakes
import test_managed_mode as planner_fakes
import test_managed_service as service_fakes


class StorageDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'rules.json'
        threads = patch('deskrawl_assistant.web_service.threading.Thread')
        threads.start(); self.addCleanup(threads.stop)
        log = patch('deskrawl_assistant.web_service.record')
        log.start(); self.addCleanup(log.stop)

    def service(self, client):
        service = AssistantService(client, self.config)
        self.addCleanup(service.close)
        service._publish(client.snapshot())
        return service

    def test_first_launch_uses_partition_preset_without_creating_preferences_or_starting(self):
        service = self.service(service_fakes.FakeManagedClient())
        self.assertEqual(service.automation_settings['pressure']['pages'], [1, 2, 3])
        self.assertEqual(service.automation_settings['runes']['pages'], [4])
        self.assertEqual(service.automation_settings['gems']['pages'], [5])
        cleanup = service.managed.settings['cleanup']
        self.assertEqual([cleanup[k + '_pages'] for k in ('equipment', 'rune', 'gem')], [[1, 2, 3], [4], [5]])
        self.assertFalse(service.automation_config.exists())
        self.assertFalse(service.managed_config.exists())
        self.assertFalse(service.automation_running)
        self.assertFalse(service.managed.running)

    def test_saved_local_partitions_survive_restart_and_permission_changes_without_file_rewrite(self):
        client = service_fakes.FakeManagedClient()
        # Save while page 2 is open; later permission loss must not rewrite
        # the existing selection or the reserved page 7.
        client.storage_indices += [50]
        service = self.service(client)
        auto = deepcopy(AUTO)
        auto['pressure']['pages'] = [5]
        auto['runes']['pages'] = [2, 7]
        auto['gems']['pages'] = [1]
        managed = deepcopy(MANAGED)
        managed['cleanup'].update(equipment_pages=[5], rune_pages=[2, 7], gem_pages=[1])
        service.save_automation(auto)
        service.save_managed(managed)
        saved_auto = service.automation_config.read_bytes()
        saved_managed = service.managed_config.read_bytes()
        client.storage_indices = list(range(5))
        restored = self.service(client)
        self.assertEqual(restored.automation_settings, auto)
        self.assertEqual(restored.managed.settings['cleanup']['rune_pages'], [2, 7])
        client.storage_indices += list(range(300, 307))
        restored._publish(client.snapshot())
        restored.managed_page()
        self.assertEqual(restored.automation_config.read_bytes(), saved_auto)
        self.assertEqual(restored.managed_config.read_bytes(), saved_managed)
        self.assertEqual(restored.managed.settings['cleanup']['rune_pages'], [2, 7])

    def test_offline_saved_defaults_wait_then_use_newly_unlocked_pages(self):
        client = automation_fakes.AutomationClient([
            automation_fakes.gem(0),
            dict(automation_fakes.gem(1, 'Rune3'), item_class='RuneData', item_type=7)])
        client.storage_indices = list(range(50))
        client.connected = False
        service = self.service(client)
        auto = deepcopy(AUTO)
        auto['gems']['enabled'] = auto['runes']['enabled'] = True
        service.save_automation(auto)
        client.connected = True
        service._publish(client.snapshot())
        service.set_automation_running(True)
        service._automation_once()
        self.assertEqual(client.events, [])
        self.assertTrue(all(row['container'] == 'inventory' for row in client.items))
        self.assertIn('尚未开放', service.automation_status)
        self.assertTrue(service.automation_running)
        client.storage_indices += list(range(150, 154)) + list(range(200, 202))
        service._automation_once()
        self.assertEqual(client.destinations, [200, 150])
        self.assertEqual(service.automation_settings, auto)

    def test_default_equipment_automation_only_uses_open_slots_even_with_reserved_pages(self):
        gear = automation_fakes.item(0)
        client = automation_fakes.AutomationClient([gear], capacity=4)
        client.storage_indices = [100, 101, 150, 200, 201]
        service = self.service(client)
        auto = deepcopy(AUTO)
        auto['pressure'].update(enabled=True, threshold=3, target_free=4)
        service.save_automation(auto)
        service.set_automation_running(True)
        service._automation_once()
        self.assertEqual(client.destinations, [100])
        self.assertEqual(service.automation_settings['pressure']['pages'], [1, 2, 3])

    def test_partial_and_non_contiguous_permissions_report_actual_capacity(self):
        client = service_fakes.FakeManagedClient([service_fakes.equipment(102, container='storage')])
        client.storage_indices = [0, 1, 100, 102, 104, 200]
        packet = self.service(client).managed_page()
        self.assertTrue(packet['warehouse_ready'])
        self.assertEqual([(row['index'], row['capacity'], row['free']) for row in packet['warehouse_pages']],
                         [(1, 2, 2), (3, 3, 2), (5, 1, 1)])

    def test_missing_or_invalid_permission_is_not_reported_as_unlocked(self):
        client = service_fakes.FakeManagedClient()
        service = self.service(client)
        for transfer in ({'available': False, 'page_size': 50, 'storage_indices': [0]},
                         {'available': True, 'page_size': 50, 'storage_indices': [0, 0]},
                         {'available': True, 'page_size': 50, 'storage_indices': [True]}, {}):
            with self.subTest(transfer=transfer):
                snapshot = client.snapshot(); snapshot['transfer'] = transfer
                service._publish(snapshot)
                packet = service.managed_page()
                self.assertFalse(packet['warehouse_ready'])
                self.assertEqual(packet['warehouse_pages'], [])
        client.connected = False
        service._publish(client.snapshot())
        self.assertFalse(service.managed_page()['warehouse_ready'])

    def test_valid_reserved_pages_can_be_saved_but_malformed_selection_cannot(self):
        auto = deepcopy(AUTO)
        managed = deepcopy(MANAGED)
        # Offline drafts preserve reserved choices. Connected mandatory
        # storage requires at least one open selected page per category.
        self.assertEqual(validate_auto(auto, {}, available_pages={1})['runes']['pages'], [4])
        self.assertEqual(validate_managed(managed)['cleanup']['gem_pages'], [5])
        with self.assertRaises(ValidationError):
            validate_managed(managed, available_pages={1})
        for kind in ('rune', 'gem', 'gold_bar', 'legendary_rein'):
            managed['cleanup'][kind+'_pages'].insert(0, 1)
        self.assertEqual(validate_managed(managed, available_pages={1})['cleanup']['gem_pages'], [1, 5])
        for invalid in ([0], [True], [2, 2], ['4']):
            with self.subTest(invalid=invalid):
                auto['runes']['pages'] = invalid
                managed['cleanup']['rune_pages'] = invalid
                with self.assertRaises(ValidationError): validate_auto(auto, {})
                with self.assertRaises(ValidationError): validate_managed(managed)


class ManagedUnlockTests(unittest.TestCase):
    def engine(self):
        config = deepcopy(MANAGED)
        config['farming'].update(enabled=True, dungeon_id='a')
        return ManagedMode(config)

    def test_closed_partition_pauses_retains_item_and_resumes_to_correct_page_after_unlock(self):
        for kind, name, page in [('rune', 'Rune3', 4), ('rune', 'SetRune1', 4), ('gem', 'GemEmerald4', 5)]:
            with self.subTest(kind=kind, name=name):
                row = planner_fakes.loot('reserved-item', kind, name)
                engine = self.engine()
                world = planner_fakes.observation([row], town=True, allowed=range(50))
                engine.start(world)
                self.assertIsNone(engine.next_action(world))
                self.assertEqual(engine.phase, 'paused')
                self.assertIn('尚未解锁', engine.status)
                self.assertIsNone(engine.pending)
                self.assertEqual(world['items'][0]['container'], 'inventory')
                targets = [50 * (page - 1), 50 * (page - 1) + 1]
                world = planner_fakes.observation([row], town=True, allowed=[0, *targets])
                engine.resume(world)
                action = engine.next_action(world)
                self.assertEqual(action['kind'], 'transfer')
                self.assertEqual(action['target_indices'], targets)
                self.assertEqual(engine.settings['cleanup'][kind + '_pages'], [page])

    def test_no_open_equipment_partition_never_invokes_full_page_disposal_fallback(self):
        row = planner_fakes.equipment('junk')
        engine = self.engine()
        world = planner_fakes.observation([row], town=True, allowed=range(200, 250))
        engine.start(world)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.phase, 'paused')
        self.assertIn('所选装备仓库页尚未解锁', engine.status)
        self.assertFalse(engine.running)
        self.assertIsNone(engine.pending)

    def test_some_reserved_pages_closed_still_allow_storage_in_open_selected_page(self):
        row = planner_fakes.equipment('junk')
        engine = self.engine()
        world = planner_fakes.observation([row], town=True, allowed=[0, 100, 101, 200])
        engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'transfer')
        self.assertEqual(action['target_indices'], [0, 100, 101])
        self.assertEqual(engine.settings['cleanup']['equipment_pages'], [1, 2, 3])

    def test_full_open_page_keeps_quality_fallback_without_using_closed_or_other_partitions(self):
        row = planner_fakes.equipment('junk')
        stored = planner_fakes.equipment('kept', 0, 'storage', locked=True)
        engine = self.engine()
        world = planner_fakes.observation([row, stored], town=True, allowed=[0, 200])
        engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'sell')
        self.assertEqual(action['reason_code'], 'equipment_fallback')
        self.assertEqual(action['item']['item_uid'], 'junk')


if __name__ == '__main__':
    unittest.main()
