"""Material cleanup uses fresh identities and never falls back to disposal."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from deskrawl_assistant.managed_mode import ManagedMode, validate_settings, storage_material_kind
from deskrawl_assistant.models import ValidationError
from tests.test_managed_mode import equipment, loot, observation, settings, verified
from tests import test_managed_service as service_fixture


class MaterialStorageTests(unittest.TestCase):
    def test_upgrade_preserves_existing_policies_and_defaults_materials_to_page_five(self):
        original = settings(equipment_action='salvage', equipment_pages=[2, 3])
        original['auto_equip']['slot_locks'] = {'8': 'LegendaryNeck3'}
        for key in ('gold_bar', 'legendary_rein'):
            del original['cleanup'][key+'_action']; del original['cleanup'][key+'_pages']
        upgraded = validate_settings(original)
        self.assertEqual(upgraded['auto_equip'], original['auto_equip'])
        self.assertEqual(upgraded['cleanup']['equipment_action'], 'salvage')
        for key in ('gold_bar', 'legendary_rein'):
            self.assertEqual(upgraded['cleanup'][key+'_action'], 'store')
            self.assertEqual(upgraded['cleanup'][key+'_pages'], [5])
        upgraded['cleanup']['gold_bar_pages'].append(2)
        self.assertEqual(upgraded['cleanup']['legendary_rein_pages'], [5])
        self.assertEqual(original['cleanup']['equipment_pages'], [2, 3])

    def test_only_gold_and_legendary_reins_match(self):
        self.assertEqual(storage_material_kind(loot('gold', 'material', 'GoldBar')), 'gold_bar')
        self.assertEqual(storage_material_kind(loot('rein', 'material', 'Rein_BeachCrabBoss', base_rarity=3)), 'legendary_rein')
        for rarity in (None, 0, 1, 2, 4, True, '3'):
            self.assertIsNone(storage_material_kind(loot('rein', 'material', 'Rein_BeachCrabBoss', rarity=rarity)))
        for key in ('Gold', 'GoldPotion1', 'GoldBarFake', 'TreasureChest2'):
            self.assertIsNone(storage_material_kind(loot('other', 'material', key, rarity=3)))
        self.assertIsNone(storage_material_kind(equipment('not-material', internal_name='GoldBar')))

    def test_pages_and_disposition_validate_and_roundtrip(self):
        config = settings(gold_bar_action='keep', gold_bar_pages=[5], legendary_rein_pages=[4])
        result = validate_settings(config)
        self.assertEqual(result['cleanup']['gold_bar_action'],'store')
        self.assertEqual(validate_settings(deepcopy(result)), result)
        for changes in ({'gold_bar_action': 'sell'}, {'legendary_rein_action': 'salvage'},
                        {'gold_bar_action': 'store', 'gold_bar_pages': []}, {'legendary_rein_pages': [True]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_settings(settings(**changes))

    def test_materials_free_a_full_bag_before_entering_the_next_map(self):
        gold = loot('gold', 'material', 'GoldBar')
        rein = loot('rein', 'material', 'Rein_BeachCrabBoss', slot=1, base_rarity=3)
        rein['locked'] = True
        config = settings(gold_bar_pages=[2], legendary_rein_pages=[3])
        config['farming']['free_threshold'] = 1
        bag = [gold, rein]
        engine = ManagedMode(config); engine.start(observation(bag, town=True, capacity=2))
        stored = []
        for row, group, page in ((gold, 'gold_bar', 2), (rein, 'legendary_rein', 3)):
            action = engine.next_action(observation(bag + stored, town=True, capacity=2))
            self.assertEqual((action['kind'], action['storage_group']), ('transfer', group))
            self.assertEqual(action['target_indices'], list(range((page-1)*50, page*50)))
            verified(engine, action)
            bag.remove(row); moved = deepcopy(row); moved.update(container='storage', slot_index=(page-1)*50)
            stored.append(moved)
        self.assertTrue(stored[1]['locked'])
        self.assertEqual(engine.next_action(observation(stored, town=True, capacity=2))['kind'], 'enter_dungeon')

    def test_legacy_keep_migrates_to_storage_and_empty_pages_require_selection(self):
        rows = [loot('gold', 'material', 'GoldBar'),
                loot('rein', 'material', 'Rein_BeachCrabBoss', slot=1, rarity=3)]
        config = settings(equipment_action='salvage', gold_bar_action='keep', gold_bar_pages=[5],
                          legendary_rein_action='keep', legendary_rein_pages=[5])
        engine = ManagedMode(config); world = observation(rows, town=True, allowed=range(250))
        engine.start(world)
        action=engine.next_action(world)
        self.assertEqual((action['kind'],action['target']),('transfer','storage'))
        config['cleanup']['gold_bar_pages']=[]
        with self.assertRaises(ValidationError):validate_settings(config)
        migrated=validate_settings(config,allow_empty_storage_pages=True)
        self.assertEqual(migrated['cleanup']['gold_bar_pages'],[])
        self.assertEqual(migrated['cleanup']['gold_bar_action'],'store')

    def test_full_or_locked_material_pages_pause_without_disposing_anything(self):
        for group, key in (('gold_bar', 'GoldBar'), ('legendary_rein', 'Rein_BeachCrabBoss')):
            for allowed, full in (([], False), ([50], True)):
                with self.subTest(group=group, allowed=allowed):
                    rows = [loot('material', 'material', key, rarity=3)]
                    if full:rows.append(equipment('stored', slot=50, container='storage', locked=True))
                    config = settings(**{group+'_pages': [2]})
                    engine = ManagedMode(config); world = observation(rows, town=True, allowed=allowed)
                    engine.start(world)
                    self.assertIsNone(engine.next_action(world))
                    self.assertEqual(engine.phase, 'paused')
                    self.assertIn('物品', engine.status)

    def test_all_reward_locks_precede_material_storage_and_junk_disposal(self):
        protected = equipment('protected', matches=True)
        gold = loot('gold', 'material', 'GoldBar', slot=1)
        junk = equipment('junk', slot=2)
        rows = [gold, junk, protected]
        engine = ManagedMode(settings(equipment_action='sell'))
        world = observation(rows, town=True); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'lock')
        verified(engine, action); protected['locked'] = True
        action = engine.next_action(observation(rows, town=True))
        self.assertEqual(action['kind'], 'transfer')
        self.assertEqual(action['item']['item_uid'], 'protected')


class MaterialServiceTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp
    settings = service_fixture.ManagedServiceTests.settings

    def prepare(self, key='GoldBar', page=5):
        self.client.items = [service_fixture.equipment(key=key, item_uid=None, is_equipment=False,
            item_class='ItemData', item_type=7 if key == 'GoldBar' else 2, modifiers=[])]
        config = self.settings(farming=True)
        config['cleanup'].update(gold_bar_pages=[page], legendary_rein_pages=[page])
        self.service.save_managed(config); self.service.set_managed_running(True)
        self.service._publish(self.client.snapshot())
        world = self.service._managed_observation()
        action = self.service.managed.next_action(world)
        self.assertEqual(action['kind'], 'transfer')
        return action, world

    def test_real_itemdata_shape_moves_to_the_selected_open_page(self):
        with patch('deskrawl_assistant.managed_service.record'):
            for key in ('GoldBar', 'Rein_BeachCrabBoss'):
                action, world = self.prepare(key)
                self.assertTrue(self.service._managed_execute(action, world)['verified'])
                self.assertEqual(self.client.items[0]['container'], 'storage')
                self.assertEqual(self.client.items[0]['slot_index'], 200)
                self.service.managed.stop(); self.service.managed.pending = None

    def test_legacy_keep_cannot_bypass_forced_storage_and_invalid_target_is_rejected(self):
        action, world = self.prepare()
        self.service.managed.settings['cleanup']['gold_bar_action'] = 'keep'
        with patch('deskrawl_assistant.managed_service.record'):
            result = self.service._managed_execute(action, world)
        self.assertTrue(result['verified'])
        self.assertEqual(self.client.items[0]['container'],'storage')
        self.service.managed.stop(); self.service.managed.pending=None
        self.client.actions=[]
        action,world=self.prepare()
        action['target_indices'] = [0]
        with self.assertRaises(ValidationError):self.service._managed_execute(action, world)
        self.assertEqual(self.client.actions, [])


if __name__ == '__main__':unittest.main()
