"""Servant gear stays separate from character gear throughout managed workflows."""
from copy import deepcopy
import json
import unittest

from deskrawl_assistant.auction import DEFAULT_AUCTION, allows, eligible
from deskrawl_assistant.item_protection import is_minion_equipment
from deskrawl_assistant.managed_mode import ManagedMode, ManagedMetrics, validate_settings, equipment_actions
from deskrawl_assistant.models import ValidationError
from test_managed_mode import equipment, observation, settings, verified
import test_managed_service as service_fixture


def minion(uid='minion', **extra):
    return equipment(uid, **{'name_key':'RareMinionGear1', 'internal_name':'RareMinionGear1',
        'equip_slot':12, 'rarity':2, 'base_rarity':2, **extra})


class MinionPolicyTests(unittest.TestCase):
    def test_old_config_inherits_saved_equipment_pages_without_inheriting_sale(self):
        old = settings(equipment_action='salvage', equipment_pages=[2, 4])
        old['cleanup'].pop('minion_equipment_action')
        old['cleanup'].pop('minion_equipment_policies')
        old['cleanup'].pop('minion_equipment_pages')
        before = deepcopy(old)
        migrated = validate_settings(old)
        self.assertEqual(migrated['cleanup']['minion_equipment_action'], 'store')
        self.assertEqual(migrated['cleanup']['minion_equipment_policies'], {'legendary':'store', 'rare':'store'})
        self.assertEqual(migrated['cleanup']['minion_equipment_pages'], [2, 4])
        self.assertNotIn('minion_equipment', migrated['auction']['categories'])
        self.assertEqual(validate_settings(migrated), migrated)
        self.assertEqual(old, before)

    def test_default_storage_uses_own_pages_even_without_character_affix_rules(self):
        cfg = settings(equipment_action='sell', minion_equipment_pages=[2])
        world = observation([minion(review=True, rule_status='unknown')], town=True)
        engine = ManagedMode(cfg); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual((action['kind'], action['target']), ('transfer', 'storage'))
        self.assertEqual(set(action['target_indices']), set(range(50, 100)))

    def test_explicit_sale_or_salvage_ignores_character_quality_policy(self):
        for disposition in ('sell', 'salvage'):
            with self.subTest(disposition=disposition):
                cfg = settings(minion_equipment_action=disposition)
                world = observation([minion()], town=True)
                engine = ManagedMode(cfg); engine.start(world)
                self.assertEqual(engine.next_action(world)['kind'], disposition)

    def test_mist_and_locked_minion_gear_are_stored_despite_sale_setting(self):
        for row in (minion(locked=True), minion(is_black_mist=True)):
            with self.subTest(row=row):
                cfg = settings(minion_equipment_action='sell', minion_equipment_pages=[2])
                world = observation([row], town=True)
                engine = ManagedMode(cfg); engine.start(world)
                action = engine.next_action(world)
                if row['locked'] is not True:
                    self.assertEqual(action['kind'], 'lock'); verified(engine, action)
                    world['items'][0]['locked'] = True
                    action = engine.next_action(world)
                self.assertEqual((action['kind'], action['target']), ('transfer', 'storage'))
                self.assertEqual(min(action['target_indices']), 50)

    def test_full_minion_destination_keeps_item_without_evicting_character_gear(self):
        cfg = settings(minion_equipment_pages=[2])
        world = observation([minion(), equipment('junk', slot=50, container='storage')],
                            town=True, allowed=[50])
        before = deepcopy(world)
        engine = ManagedMode(cfg); engine.start(world)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.phase, 'paused')
        self.assertIn('仆从装备', engine.status)
        self.assertEqual(world, before)

    def test_minion_storage_cannot_be_unconfigured_or_only_locked_pages(self):
        for pages in ([], [7]):
            with self.subTest(pages=pages), self.assertRaisesRegex(ValidationError, '仆从|仓库页'):
                validate_settings(settings(minion_equipment_pages=pages), available_pages={1, 5})

    def test_legacy_disposition_migrates_both_qualities_and_explicit_quality_wins(self):
        for action in ('store', 'sell', 'salvage'):
            old = settings(minion_equipment_action=action, minion_equipment_pages=[2, 4])
            old['cleanup'].pop('minion_equipment_policies')
            before = deepcopy(old)
            migrated = validate_settings(old)
            self.assertEqual(migrated['cleanup']['minion_equipment_policies'], {'legendary':action, 'rare':action})
            migrated['cleanup']['minion_equipment_policies']['rare'] = 'store'
            self.assertEqual(validate_settings(migrated), migrated)
            self.assertEqual(old, before)

    def test_quality_policies_apply_independently_of_character_and_legacy_policies(self):
        policies = {'legendary':'sell', 'rare':'salvage'}
        for rarity, bucket in ((2, 'rare'), (3, 'legendary')):
            cfg = settings(minion_equipment_action='store', equipment_action='store', minion_equipment_policies=policies)
            world = observation([minion(rarity=rarity, base_rarity=rarity)], town=True)
            engine = ManagedMode(cfg); engine.start(world)
            self.assertEqual(engine.next_action(world)['kind'], policies[bucket])
        cfg = settings(minion_equipment_action='sell', equipment_fallback='salvage',
            minion_equipment_policies={'legendary':'store', 'rare':'store'})
        cfg['cleanup']['equipment_policies'] = {key:{'action':'store', 'fallback':'salvage'}
            for key in ('ancient', 'legendary', 'rare', 'uncommon', 'normal')}
        self.assertNotIn('sell', equipment_actions(validate_settings(cfg)['cleanup']))

    def test_new_policies_cannot_authorize_disposal_of_black_mist_or_locked_minions(self):
        cfg = settings(minion_equipment_policies={'legendary':'sell', 'rare':'salvage'})
        for row in (minion(rarity=3, base_rarity=3, is_black_mist=True), minion(locked=True)):
            with self.subTest(row=row):
                world = observation([row], town=True)
                engine = ManagedMode(cfg); engine.start(world)
                action = engine.next_action(world)
                if action['kind'] == 'lock':
                    verified(engine, action); world['items'][0]['locked'] = True
                    action = engine.next_action(world)
                self.assertEqual((action['kind'], action['target']), ('transfer', 'storage'))

    def test_minion_policy_schema_rejects_protected_overrides_and_invalid_actions(self):
        for policies in (None, [], {'black_mist':'sell'}, {'rare':'discard'}, {'legendary':True}):
            with self.subTest(policies=policies), self.assertRaisesRegex(ValidationError, '仆从装备品质'):
                validate_settings(settings(minion_equipment_policies=policies))

    def test_upload_category_does_not_inherit_black_mist_permission_and_exclusion_wins(self):
        row = minion(is_black_mist=True, auction_candidate=True, identity_verified=True)
        config = {**DEFAULT_AUCTION, 'upload_enabled': True, 'categories': ['black_mist_equipment']}
        self.assertFalse(allows(config, row))
        config['categories'] = ['minion_equipment']
        self.assertTrue(eligible(row, config))
        config['excluded_keys'] = ['RareMinionGear1']
        self.assertFalse(eligible(row, config))
        config['excluded_keys'] = []
        self.assertFalse(eligible({**row, 'auction_candidate': False}, config))


class MinionMetricsTests(unittest.TestCase):
    @staticmethod
    def ledger(count=0, receipts=None):
        return {'available': True, 'complete': True, 'id': 'same-ledger',
                'counts': {'2': count}, 'receipts': receipts or [], 'policy': {}}

    def test_ground_bag_storage_and_native_total_count_each_pickup_once(self):
        metric = ManagedMetrics(); metric.observe(observation(loot_ledger=self.ledger()))
        row = minion(container='ground')
        world = observation(loot_ledger=self.ledger()); world['ground']['items'] = [row]
        metric.observe(world)
        self.assertEqual(metric.state()['minion_equipment'][0]['count'], 1)
        self.assertEqual(metric.state()['equipment']['rare']['count'], 0)
        for container in ('inventory', 'storage'):
            row['container'] = container
            metric.observe(observation([row], loot_ledger=self.ledger(1)))
        character = equipment('character', rarity=2)
        metric.observe(observation([row, character], loot_ledger=self.ledger(2)))
        stats = metric.state()
        self.assertEqual(stats['minion_equipment'][0]['count'], 1)
        self.assertEqual(stats['equipment']['rare']['count'], 1)
        self.assertEqual(stats['others'], [])

    def test_native_disposal_without_instance_level_joins_same_item(self):
        metric = ManagedMetrics(); metric.observe(observation(loot_ledger=self.ledger()))
        row = minion(item_level=70)
        metric.observe(observation([row], loot_ledger=self.ledger(1)))
        receipt = {'index': 0, 'action': 'sell', 'item': minion(item_level=None)}
        metric.observe(observation(loot_ledger=self.ledger(1, [receipt])))
        metric.observe(observation(loot_ledger=self.ledger(1, [receipt])))
        stats = metric.state()
        self.assertEqual(len(stats['minion_equipment']), 1)
        self.assertEqual((stats['minion_equipment'][0]['count'], stats['minion_equipment'][0]['sold']), (1, 1))
        self.assertEqual(stats['equipment']['rare']['count'], 0)

    def test_unequipping_initial_minion_gear_is_not_a_drop(self):
        metric = ManagedMetrics()
        world = observation(); world['character']['minion_equipment'] = [minion()]
        metric.observe(world)
        metric.observe(observation([minion()]))
        self.assertEqual(metric.state()['minion_equipment'], [])


class MinionServiceTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp
    settings = service_fixture.ManagedServiceTests.settings

    def test_quality_settings_are_saved_and_loaded_without_losing_warehouse_pages(self):
        cfg = self.settings()
        cfg['cleanup'].update(minion_equipment_policies={'legendary':'salvage', 'rare':'sell'}, minion_equipment_pages=[5])
        self.service.save_managed(cfg)
        disk = json.loads(self.service.managed_config.read_text(encoding='utf-8'))
        self.service._init_managed()
        self.assertEqual(self.service.managed.settings, disk)
        self.assertEqual(disk['cleanup']['minion_equipment_policies'], {'legendary':'salvage', 'rare':'sell'})
        self.assertEqual(disk['cleanup']['minion_equipment_pages'], [5])

    def test_executor_rechecks_actual_quality_before_disposal(self):
        self.client.items = [service_fixture.equipment(key='RareMinionGear1', equip_slot=12, rarity=2, base_rarity=2, locked=False)]
        cfg = self.settings(farming=True)
        cfg['cleanup']['minion_equipment_policies'] = {'legendary':'sell', 'rare':'salvage'}
        self.service._publish(self.client.snapshot()); self.service.save_managed(cfg)
        self.service.set_managed_running(True)
        world = self.service._managed_observation(); row = world['items'][0]
        result = self.service._managed_execute({'kind':'sell', 'item':row}, world)
        self.assertTrue(result['retryable']); self.assertFalse(result['verified'])
        self.assertEqual(self.client.actions, [])

    def test_inventory_and_managed_payload_have_independent_minion_identity(self):
        self.client.items = [service_fixture.equipment(key='RareMinionGear1', equip_slot=12,
                            base_rarity=2, locked=False)]
        self.service._publish(self.client.snapshot())
        row = self.service.state()['items'][0]
        self.assertTrue(row['is_minion_equipment'])
        self.assertEqual((row['kind_label'], row['slot']), ('仆从装备', 'Minion'))
        self.assertFalse(row['matches'])
        self.assertEqual(self.service.state()['counts']['inventory']['minion_equipment'], 1)
        world = self.service._managed_observation()
        self.assertFalse(self.service.managed._reward_pending(world['items'][0]))
        self.assertTrue(is_minion_equipment(world['items'][0]))

    def test_server_checks_minion_destination_not_character_destination(self):
        self.client.items = [service_fixture.equipment(key='RareMinionGear1', equip_slot=12,
                            base_rarity=2, locked=False)]
        cfg = self.settings(farming=True)
        cfg['cleanup']['minion_equipment_pages'] = [5]
        self.service._publish(self.client.snapshot())
        self.service.save_managed(cfg)
        self.service.set_managed_running(True)
        world = self.service._managed_observation()
        row = world['items'][0]
        with self.assertRaisesRegex(ValidationError, '目标仓库页'):
            self.service._managed_execute({'kind': 'transfer', 'item': row,
                'target': 'storage', 'target_indices': [0]}, world)
        result = self.service._managed_execute({'kind': 'transfer', 'item': row,
            'target': 'storage', 'target_indices': [200]}, world)
        self.assertTrue(result['verified'])
