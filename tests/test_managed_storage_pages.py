"""Destination permissions and overflow retention; no live game or workers."""
from copy import deepcopy
import unittest

from deskrawl_assistant.managed_mode import ManagedMode, validate_settings
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.storage_preferences import available_storage_pages, MANDATORY_STORAGE_GROUPS
from test_managed_mode import settings, observation, equipment, loot
import test_managed_service as service_fixture


class DestinationTests(unittest.TestCase):
    def test_each_required_group_needs_an_open_page_and_reserved_pages_are_preserved(self):
        for kind, label in MANDATORY_STORAGE_GROUPS:
            with self.subTest(kind=kind):
                cfg = settings()
                cfg['cleanup'][kind + '_pages'] = [8]
                before = deepcopy(cfg)
                with self.assertRaisesRegex(ValidationError, label + '.*已解锁'):
                    validate_settings(cfg, available_pages={1, 5})
                self.assertEqual(cfg, before)
                cfg['cleanup'][kind + '_pages'] = [8, 1]
                self.assertEqual(validate_settings(cfg, available_pages={1, 5})['cleanup'][kind + '_pages'], [8, 1])

    def test_unread_permissions_differ_from_no_unlocked_slots(self):
        cfg = settings()
        self.assertEqual(validate_settings(cfg)['cleanup'], validate_settings(cfg, available_pages=None)['cleanup'])
        with self.assertRaisesRegex(ValidationError, '装备、符文、宝石、金条、传说缰绳'):
            validate_settings(cfg, available_pages=set())
        snap = {'complete': True, 'transfer': {'available': True, 'page_size': 50, 'storage_indices': []}}
        self.assertEqual(available_storage_pages(snap, True), set())
        self.assertIsNone(available_storage_pages(snap, False))
        self.assertIsNone(available_storage_pages({**snap, 'complete': False}, True))

    def test_noncontiguous_and_partially_unlocked_full_pages_remain_valid(self):
        snap = {'complete': True, 'transfer': {'available': True, 'page_size': 50,
            'storage_indices': [0, 49, 200]}, 'containers': {'storage': {'slots': [{'slot_index': n} for n in (0, 49, 200)]}}}
        self.assertEqual(available_storage_pages(snap, True), {1, 5})
        validate_settings(settings(), available_pages=available_storage_pages(snap, True))

    def test_malformed_permissions_are_unread_instead_of_usable(self):
        for size, indices in ((True, [0]), (0, [0]), (50, [0, 0]), (50, [-1]), (50, [True]), (50, None)):
            with self.subTest(size=size, indices=indices):
                snap = {'complete': True, 'transfer': {'available': True, 'page_size': size, 'storage_indices': indices}}
                self.assertIsNone(available_storage_pages(snap, True))

    def test_full_required_destinations_keep_every_kind_in_bag_and_pause(self):
        rows = [equipment('divine', rarity=4, locked=True), equipment('mist', is_black_mist=True, locked=True),
            loot('rune', 'rune', 'Rune_Dexterity', rarity=2), loot('gem', 'gem', 'GemRuby4'),
            loot('gold', 'material', 'GoldBar'), loot('rein', 'material', 'Rein_BeachCrabBoss', rarity=3)]
        for row in rows:
            with self.subTest(row=row['internal_name']):
                cfg = settings(gold_bar_pages=[1], legendary_rein_pages=[1])
                full = equipment('occupied', container='storage', locked=True)
                world = observation([row, full], town=True, allowed=[0])
                before = deepcopy(world)
                engine = ManagedMode(cfg)
                engine.start(world)
                self.assertIsNone(engine.next_action(world))
                self.assertEqual(engine.phase, 'paused')
                self.assertIn('留在背包', engine.status)
                self.assertIsNone(engine.pending)
                self.assertEqual(world, before)


class DestinationServiceTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp
    settings = service_fixture.ManagedServiceTests.settings

    def test_rejected_save_preserves_both_files_and_published_choices(self):
        cfg = self.settings(farming=True)
        self.service.save_managed(cfg)
        paths = [self.service.managed_config, self.service.automation_config]
        before = {path: path.read_bytes() for path in paths}
        old = deepcopy(self.service.managed.settings)
        for kind, label in MANDATORY_STORAGE_GROUPS:
            with self.subTest(kind=kind):
                requested = deepcopy(old)
                requested['cleanup'][kind + '_pages'] = [2]
                with self.assertRaisesRegex(ValidationError, label + '.*已解锁'):
                    self.service.save_managed(requested)
                self.assertEqual(self.service.managed.settings, old)
                self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(self.client.actions, [])

    def test_shared_organizer_cannot_save_locked_only_rune_or_gem_destination(self):
        self.service.save_managed(self.settings())
        before = self.service.managed_config.read_bytes()
        for group, label in (('runes', '符文'), ('gems', '宝石')):
            auto = deepcopy(self.service.automation_settings)
            auto[group]['pages'] = [2]
            with self.assertRaisesRegex(ValidationError, label + '.*已解锁'):
                self.service.save_automation(auto)
            self.assertEqual(self.service.managed_config.read_bytes(), before)
        self.assertEqual(self.client.actions, [])

    def test_reserve_with_unlocked_page_saves_and_survives_start(self):
        cfg = self.settings(farming=True)
        for kind, _ in MANDATORY_STORAGE_GROUPS:
            cfg['cleanup'][kind + '_pages'] = [2, 1]
        self.service.save_managed(cfg)
        self.service.set_managed_running(True)
        self.assertTrue(self.service.managed.running)
        for kind, _ in MANDATORY_STORAGE_GROUPS:
            self.assertEqual(self.service.managed.settings['cleanup'][kind + '_pages'], [2, 1])
        self.assertEqual(self.client.actions, [])

    def test_permissions_are_rechecked_after_fresh_start_observation(self):
        cfg = self.settings(farming=True)
        cfg['cleanup']['equipment_pages'] = [1]
        self.service.save_managed(cfg)
        self.assertTrue(self.service.managed_page()['can_start'])
        self.client.storage_indices = list(range(200, 250))
        with self.assertRaisesRegex(ValidationError, '装备.*已解锁'):
            self.service.set_managed_running(True)
        self.assertFalse(self.service.managed.running)
        self.assertFalse(self.service.busy)
        self.assertEqual(self.client.actions, [])

    def test_offline_drafts_preserve_choices_then_require_correction_when_connected(self):
        self.client.connected = False
        cfg = self.settings(farming=True)
        cfg['cleanup']['rune_pages'] = [2]
        self.service.save_managed(cfg)
        saved = self.service.managed_config.read_bytes()
        self.client.connected = True
        self.service._publish(self.client.snapshot())
        page = self.service.managed_page()
        self.assertFalse(page['can_start'])
        self.assertIn('符文', '；'.join(page['blocked_reasons']))
        self.assertEqual(self.service.managed_config.read_bytes(), saved)

    def test_unread_permissions_block_save_and_start_with_an_explanation(self):
        self.service.snapshot['complete'] = False
        with self.assertRaisesRegex(ValidationError, '仓库开放格位尚未读取完整'):
            self.service.save_managed(self.settings())
        page = self.service.managed_page()
        self.assertFalse(page['can_start'])
        self.assertFalse(page['warehouse_ready'])
        self.assertIn('仓库开放格位尚未读取完整', '；'.join(page['blocked_reasons']))


if __name__ == '__main__': unittest.main()
