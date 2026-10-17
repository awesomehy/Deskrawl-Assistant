"""Partition migration/persistence with temporary files and no game or workers."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.automation import DEFAULT_SETTINGS as AUTOMATION_DEFAULTS
from deskrawl_assistant.managed_mode import DEFAULT_SETTINGS as MANAGED_DEFAULTS, validate_settings as validate_managed
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.web_service import AssistantService
from test_automation import AutomationClient, gem


class SharedStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        threads = patch('deskrawl_assistant.web_service.threading.Thread')
        threads.start()
        self.addCleanup(threads.stop)
        logging = patch('deskrawl_assistant.web_service.record')
        logging.start()
        self.addCleanup(logging.stop)

    def write(self, name, value):
        (self.directory/name).write_text(json.dumps(value), encoding='utf-8')

    def service(self):
        service = AssistantService(AutomationClient([]), self.directory/'rules.json')
        self.addCleanup(service.close)
        service._publish(service.client.snapshot())
        return service

    def assert_pages(self, service, rune, gem_pages):
        self.assertEqual(service.managed.settings['cleanup']['rune_pages'], rune)
        self.assertEqual(service.automation_settings['runes']['pages'], rune)
        self.assertEqual(service.managed.settings['cleanup']['gem_pages'], gem_pages)
        self.assertEqual(service.automation_settings['gems']['pages'], gem_pages)

    def test_upgrade_uses_managed_pages_and_preserves_other_settings_and_original_files(self):
        auto = deepcopy(AUTOMATION_DEFAULTS)
        auto['runes'].update(enabled=True, pages=[4])
        auto['gems'].update(enabled=True, pages=[5])
        auto['pressure'].update(pages=[2], threshold=3)
        managed = deepcopy(MANAGED_DEFAULTS)
        managed['cleanup'].update(rune_pages=[3,4], gem_pages=[2,5], rune_action='sell')
        managed['auto_equip'].update(disabled_slots=[5])
        managed['background']['rule_monitoring'] = False
        self.write('automation.json', auto)
        self.write('managed-mode.json', managed)
        before = {path.name:path.read_bytes() for path in self.directory.iterdir()}
        service = self.service()
        self.assert_pages(service, [3,4], [2,5])
        self.assertEqual(service.managed.settings, validate_managed(managed))
        for group in auto:
            actual = deepcopy(service.automation_settings[group])
            if group in ('runes','gems'): actual['pages'] = auto[group]['pages']
            self.assertEqual(actual, auto[group])
        self.assertEqual({name:(self.directory/name).read_bytes() for name in before}, before)
        self.assertFalse(service.automation_running)
        self.assertFalse(service.managed.running)
        self.assertEqual(service.client.events, [])

    def test_both_save_directions_persist_without_revalidating_unrelated_merchant_unlock(self):
        service = self.service()
        managed = deepcopy(service.managed.settings)
        managed['cleanup'].update(rune_pages=[2,4], gem_pages=[3], rune_action='sell')
        managed['auto_equip']['disabled_slots'] = [5]
        service.save_managed(managed)
        self.assert_pages(service, [2,4], [3])
        # This saved preference can exist while the game is disconnected. A page
        # edit must not ask to unlock the merchant or reset its ratios/presets.
        service.managed.settings['merchant']['enabled'] = True
        service.managed.settings['merchant']['weights']['2'] = 1
        service.client.connected = False
        old = deepcopy(service.managed.settings)
        auto = deepcopy(service.automation_settings)
        auto['runes']['pages'] = [1,5]
        auto['gems']['pages'] = [5]
        auto['carriage'].update(enabled=True, types=['gem'])
        service.save_automation(auto)
        old['cleanup'].update(rune_pages=[1,5], gem_pages=[5])
        self.assertEqual(service.managed.settings, old)
        self.assertEqual(json.loads(service.managed_config.read_text(encoding='utf-8')), old)
        self.assertEqual(json.loads(service.automation_config.read_text(encoding='utf-8')), auto)
        restarted = self.service()
        self.assert_pages(restarted, [1,5], [5])
        self.assertEqual(restarted.automation_settings, auto)
        self.assertEqual(restarted.managed.settings, old)

    def test_organizer_only_installation_keeps_its_pages_and_equipment_pages_stay_independent(self):
        auto = deepcopy(AUTOMATION_DEFAULTS)
        auto['runes']['pages'] = [2,4]
        auto['gems']['pages'] = [1]
        auto['pressure']['pages'] = [5]
        self.write('automation.json', auto)
        service = self.service()
        self.assert_pages(service, [2,4], [1])
        self.assertFalse(service.managed_config.exists())
        managed = deepcopy(service.managed.settings)
        managed['cleanup'].update(equipment_pages=[3], gem_pages=[2,5])
        service.save_managed(managed)
        self.assertEqual(service.automation_settings['pressure']['pages'], [5])
        self.assert_pages(self.service(), [2,4], [2,5])

    def test_failed_second_file_replace_restores_disk_and_does_not_publish_new_pages(self):
        service = self.service()
        service.save_managed(deepcopy(service.managed.settings))
        paths = [service.managed_config, service.automation_config]
        before = {path:path.read_bytes() for path in paths}
        old_auto, old_managed = deepcopy(service.automation_settings), deepcopy(service.managed.settings)
        requested = deepcopy(old_auto)
        requested['runes']['pages'] = [1]
        replace = Path.replace
        def fail_managed_once(source, target):
            if target == service.managed_config:
                raise PermissionError('offline simulated denied replace')
            return replace(source, target)
        with patch.object(Path, 'replace', fail_managed_once):
            with self.assertRaises(PermissionError): service.save_automation(requested)
        self.assertEqual({path:path.read_bytes() for path in paths}, before)
        self.assertEqual(service.automation_settings, old_auto)
        self.assertEqual(service.managed.settings, old_managed)
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_invalid_managed_file_is_preserved_until_explicit_managed_save(self):
        path = self.directory/'managed-mode.json'
        path.write_text('{broken', encoding='utf-8')
        service = self.service()
        with self.assertRaisesRegex(ValidationError, '托管配置未能载入'):
            service.save_automation(deepcopy(service.automation_settings))
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')
        service.save_managed(deepcopy(service.managed.settings))
        self.assertFalse(service.managed_config_load_failed)
        service.save_automation(deepcopy(service.automation_settings))

    def test_managed_save_preserves_corrupt_organizer_file_instead_of_defaulting_its_switches(self):
        path = self.directory/'automation.json'
        path.write_text('{broken', encoding='utf-8')
        service = self.service()
        managed = deepcopy(service.managed.settings)
        managed['cleanup'].update(rune_pages=[2], gem_pages=[3])
        service.save_managed(managed)
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')
        self.assert_pages(self.service(), [2], [3])

    def test_shared_destinations_cannot_be_cleared_when_either_mode_needs_storage(self):
        service = self.service()
        auto = deepcopy(service.automation_settings)
        auto['runes']['pages'] = []
        with self.assertRaisesRegex(ValidationError, '符文共用'): service.save_automation(auto)
        auto = deepcopy(service.automation_settings)
        auto['gems']['enabled'] = True
        service.save_automation(auto)
        managed = deepcopy(service.managed.settings)
        managed['cleanup'].update(gem_action='sell', gem_pages=[])
        with self.assertRaisesRegex(ValidationError, '请勾选用于存放物品的仓库页'): service.save_managed(managed)
        self.assert_pages(service, [4], [5])

    def test_organizer_actually_moves_to_new_managed_pages_without_using_other_pages(self):
        service = self.service()
        auto = deepcopy(service.automation_settings)
        auto['gems']['enabled'] = True
        service.save_automation(auto)
        managed = deepcopy(service.managed.settings)
        managed['cleanup']['gem_pages'] = [3]
        service.save_managed(managed)
        service.client.items = [gem(0)]
        service._publish(service.client.snapshot())
        service.set_automation_running(True)
        service._automation_once()
        self.assertEqual(service.client.destinations, [100])


if __name__ == '__main__': unittest.main()
