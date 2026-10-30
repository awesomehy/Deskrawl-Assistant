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
    def test_retired_matched_choices_never_override_fixed_protection(self):
        for hit in ('store', 'sell', 'salvage'):
            for miss in ('store', 'sell', 'salvage'):
                for rarity in (2, 3):
                    with self.subTest(hit=hit, miss=miss, rarity=rarity):
                        cfg = settings(minion_equipment_policies={
                            'legendary_match':hit, 'legendary':miss, 'rare':'store'})
                        before = deepcopy(cfg)
                        world = observation([minion(rarity=rarity, base_rarity=rarity,
                            matches=True, rule_status='match')], town=True)
                        engine = ManagedMode(cfg); engine.start(world)
                        action = engine.next_action(world)
                        self.assertEqual(action['kind'], 'lock')
                        self.assertEqual(engine.settings['cleanup']['minion_equipment_policies'],
                                         {'legendary':miss, 'rare':'store'})
                        self.assertFalse(engine._disposable(world['items'][0]))
                        verified(engine, action); world['items'][0]['locked'] = True
                        self.assertEqual(engine.next_action(world)['target'], 'storage')
                        self.assertEqual(cfg, before)

    def test_matched_disposal_does_not_override_existing_locks_mist_or_uncertainty(self):
        cfg = settings(minion_equipment_policies={'legendary_match':'sell', 'legendary':'salvage'})
        for extra in ({'locked':True}, {'is_black_mist':True}, {'review':True, 'rule_status':'review'}):
            with self.subTest(extra=extra):
                world = observation([minion(rarity=3, base_rarity=3, matches=True, **extra)], town=True)
                engine = ManagedMode(cfg); engine.start(world)
                action = engine.next_action(world)
                self.assertTrue(action is None or action['kind'] in ('lock', 'transfer'))

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
        world = observation([minion(review=False, rule_status='no_rule', rules_known=True)], town=True)
        engine = ManagedMode(cfg); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual((action['kind'], action['target']), ('transfer', 'storage'))
        self.assertEqual(set(action['target_indices']), set(range(50, 100)))

    def test_matched_legendary_is_locked_then_stored_even_when_misses_are_sold(self):
        cfg = settings(minion_equipment_policies={'legendary':'sell', 'rare':'salvage'},
                       minion_equipment_pages=[2])
        world = observation([minion(rarity=3, base_rarity=3, matches=True, rule_status='match')], town=True)
        engine = ManagedMode(cfg); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'lock')
        verified(engine, action); world['items'][0]['locked'] = True
        action = engine.next_action(world)
        self.assertEqual((action['kind'], action['target']), ('transfer', 'storage'))
        self.assertEqual(set(action['target_indices']), set(range(50, 100)))

    def test_unresolved_minion_rules_hold_cleanup_without_sale_or_salvage(self):
        for fields in ({'review':True}, {'rule_status':'review'}, {'rule_status':'unknown'},
                       {'rules_known':False, 'review':True}, {'matches':None}):
            with self.subTest(fields=fields):
                world = observation([minion(rarity=3, base_rarity=3, **fields)], town=True)
                engine = ManagedMode(settings(minion_equipment_action='sell')); engine.start(world)
                for _ in range(5):
                    self.assertIsNone(engine.next_action(world))
                self.assertEqual(engine.phase, 'paused')
                self.assertIn('待核验', engine.status)

    def test_hidden_affixes_do_not_block_black_mist_minion_protection(self):
        world = observation([minion(rarity=3, base_rarity=3, is_black_mist=True,
                                    review=True, rule_status='review')], town=True)
        engine = ManagedMode(settings(minion_equipment_action='sell')); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'lock')
        verified(engine, action); world['items'][0]['locked'] = True
        self.assertEqual(engine.next_action(world)['kind'], 'transfer')

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

    def set_rule(self, *, stat='Stats.CooldownReduction', enabled=True):
        self.service.save_rule({'id':'minion-rule', 'name':'仆从冷却规则',
            'equipment_keys':['LegendaryMinionGear1'], 'enabled':enabled, 'group_mode':'all',
            'groups':{'primary':{'selected_stats':[stat], 'operator':'>=', 'count':1}}})

    def legendary(self, index=0, **extra):
        return service_fixture.equipment(index, key='LegendaryMinionGear1', equip_slot=12,
                                         rarity=3, base_rarity=3, locked=False, **extra)

    def prepare_disposal(self):
        cfg = self.settings(farming=True)
        cfg['cleanup'].update(minion_equipment_policies={'legendary':'sell', 'rare':'salvage'},
                              minion_equipment_pages=[5])
        self.service._publish(self.client.snapshot())
        self.service.save_managed(cfg)
        self.service.set_managed_running(True)

    def test_retired_matched_disposition_is_removed_on_save_and_reload(self):
        for disposition in ('sell', 'salvage'):
            with self.subTest(disposition=disposition):
                self.service.managed.stop()
                self.client.actions.clear()
                self.client.items = [self.legendary()]
                self.set_rule()
                cfg = self.settings(farming=True)
                cfg['cleanup'].update(minion_equipment_policies={
                    'legendary_match':disposition, 'legendary':'sell', 'rare':'salvage'},
                    minion_equipment_pages=[5])
                self.service._publish(self.client.snapshot()); self.service.save_managed(cfg)
                self.service._init_managed()
                self.assertEqual(self.service.managed.settings['cleanup']['minion_equipment_policies'],
                                 {'legendary':'sell', 'rare':'salvage'})
                self.assertEqual(self.service.managed.settings['cleanup']['minion_equipment_pages'], [5])
                disk = json.loads(self.service.managed_config.read_text(encoding='utf-8'))
                self.assertNotIn('legendary_match', disk['cleanup']['minion_equipment_policies'])
                self.service._managed_refresh_control()
                self.service.set_managed_running(True)
                world = self.service._managed_observation()
                action = self.service.managed.next_action(world)
                self.assertEqual(action['kind'], 'lock')
                self.assertTrue(self.service._managed_execute(action, world)['verified'])
                self.assertEqual(self.client.actions, [('lock', world['items'][0]['item_uid'])])

    def test_matched_character_and_minion_use_separate_pages_without_duplicate_moves(self):
        self.client.items = [service_fixture.equipment(0, locked=True), self.legendary(1)]
        self.client.items[1]['locked'] = True
        self.set_rule()
        self.service.save_rule({'id':'character-rule', 'name':'角色装备规则',
            'equipment_keys':['LegendaryBelt3'], 'enabled':True, 'group_mode':'all',
            'groups':{'primary':{'selected_stats':['Stats.CooldownReduction'], 'operator':'>=', 'count':1}}})
        cfg = self.settings(farming=True)
        cfg['cleanup'].update(equipment_pages=[1], minion_equipment_pages=[5],
                              minion_equipment_policies={'legendary':'sell', 'rare':'salvage'})
        self.service._publish(self.client.snapshot()); self.service.save_managed(cfg)
        self.service.set_managed_running(True)
        moved = []
        for _ in range(2):
            world = self.service._managed_observation()
            action = self.service.managed.next_action(world)
            self.assertEqual((action['kind'], action['target']), ('transfer', 'storage'))
            row = action['item']
            self.assertTrue(row['matches'])
            expected_page = 5 if is_minion_equipment(row) else 1
            self.assertEqual(set(action['target_indices']), set(range((expected_page-1)*50, expected_page*50)))
            result = self.service._managed_execute(action, world)
            self.assertTrue(result['verified'])
            self.service.managed.complete(action['action_id'], result=result)
            moved.append(row['item_uid'])
        self.assertEqual(len(set(moved)), 2)
        self.assertTrue(all(row['container'] == 'storage' for row in self.client.items))

    def test_stale_in_memory_matched_disposition_cannot_authorize_sale(self):
        self.client.items = [self.legendary()]
        self.set_rule(); self.prepare_disposal()
        self.service.managed.settings['cleanup']['minion_equipment_policies']['legendary_match'] = 'sell'
        world = self.service._managed_observation()
        action = self.service.managed.next_action(world)
        self.assertEqual(action['kind'], 'lock')
        result = self.service._managed_execute({'kind':'sell', 'item':world['items'][0]}, world)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions, [])

    def test_real_rule_evaluation_reaches_inventory_and_managed_payloads(self):
        self.client.items = [self.legendary()]
        self.set_rule()
        self.service._publish(self.client.snapshot())
        row = self.service.state()['items'][0]
        self.assertTrue(row['matches'])
        self.assertFalse(row['review'])
        self.assertEqual(row['matched_combinations'], ['仆从冷却规则'])
        self.assertEqual([s['key'] for s in row['groups']['primary'] if s['matched']],
                         ['Stats.CooldownReduction'])
        self.assertEqual(self.service.state()['counts']['inventory']['matches'], 1)
        managed = self.service._managed_observation()['items'][0]
        self.assertEqual((managed['matches'], managed['review'], managed['rule_status']),
                         (True, False, 'match'))
        self.assertEqual(self.client.actions, [])

    def test_disabled_rules_do_not_match_or_highlight_and_are_not_pending(self):
        self.client.items = [self.legendary()]
        self.set_rule(enabled=False)
        self.service._publish(self.client.snapshot())
        row = self.service.state()['items'][0]
        self.assertFalse(row['matches'])
        self.assertEqual(row['matched_combinations'], [])
        self.assertFalse(any(s['matched'] for group in row['groups'].values() for s in group))
        row = self.service._managed_observation()['items'][0]
        self.assertEqual(row['rule_status'], 'no_rule')
        self.assertFalse(self.service.managed._reward_pending(row))

    def test_managed_real_match_is_locked_and_stored_in_minion_pages(self):
        self.client.items = [self.legendary()]
        self.set_rule(); self.prepare_disposal()
        for expected in ('lock', 'transfer'):
            world = self.service._managed_observation()
            action = self.service.managed.next_action(world)
            self.assertEqual(action['kind'], expected)
            result = self.service._managed_execute(action, world)
            self.assertTrue(result['verified'])
            self.service.managed.complete(action['action_id'], result=result)
        self.assertEqual(self.client.items[0]['container'], 'storage')
        self.assertEqual(self.client.items[0]['slot_index'], 200)
        self.assertTrue(self.client.items[0]['locked'])
        self.assertEqual([a[0] for a in self.client.actions], ['lock', 'transfer'])

    def test_real_negative_rule_uses_saved_unmatched_disposition(self):
        self.client.items = [self.legendary()]
        self.set_rule(stat='Stats.Strength'); self.prepare_disposal()
        world = self.service._managed_observation()
        self.assertEqual(world['items'][0]['rule_status'], 'miss')
        action = self.service.managed.next_action(world)
        self.assertEqual(action['kind'], 'sell')
        self.assertTrue(self.service._managed_execute(action, world)['verified'])
        self.assertEqual([a[0] for a in self.client.actions], ['sell'])

    def test_incomplete_or_unreadable_rules_never_authorize_minion_disposal(self):
        self.client.items = [self.legendary()]
        self.set_rule(stat='Stats.Strength'); self.prepare_disposal()
        self.client.items[0]['modifiers_complete'] = False
        self.client.items[0]['read_evidence']['modifiers_complete'] = False
        world = self.service._managed_observation()
        self.assertTrue(self.service.managed._reward_pending(world['items'][0]))
        self.assertIsNone(self.service.managed.next_action(world))
        self.client.items[0]['modifiers_complete'] = True
        self.client.items[0]['read_evidence']['modifiers_complete'] = True
        self.service.config_error = '模拟规则文件不可读'
        world = self.service._managed_observation()
        self.assertEqual(world['items'][0]['rule_status'], 'review')
        self.assertIsNone(self.service.managed.next_action(world))
        self.assertEqual(self.client.actions, [])

    def test_fresh_matching_affixes_invalidate_a_previously_planned_sale(self):
        self.client.items = [self.legendary()]
        modifiers = deepcopy(self.client.items[0]['modifiers'])
        self.client.items[0]['modifiers'] = []
        self.set_rule(); self.prepare_disposal()
        world = self.service._managed_observation()
        action = self.service.managed.next_action(world)
        self.assertEqual(action['kind'], 'sell')
        self.client.items[0]['modifiers'] = modifiers
        result = self.service._managed_execute(action, world)
        self.assertTrue(result['retryable'])
        self.assertFalse(result['verified'])
        self.assertEqual(self.client.actions, [])

    def test_manual_rule_lock_revalidates_minion_affixes_before_writing(self):
        self.client.items = [self.legendary()]
        self.set_rule()
        snapshot = self.client.snapshot()
        candidates = self.service._rule_candidates(snapshot, self.client.items)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(self.service._apply(candidates, by_rules=True), 1)
        self.client.items[0]['locked'] = False
        candidates = self.service._rule_candidates(self.client.snapshot(), self.client.items)
        self.set_rule(stat='Stats.Strength')
        with self.assertRaisesRegex(ValidationError, '锁定被取消'):
            self.service._apply(candidates, by_rules=True)
        self.assertEqual([a[0] for a in self.client.actions], ['lock'])

    def test_monitor_locks_only_new_matching_minions_in_bag_and_warehouse(self):
        self.client.items = [self.legendary()]
        self.set_rule(); self.service.monitoring = True
        self.service._lock_monitor_once()
        self.assertEqual(self.client.actions, [])
        self.client.items.extend([self.legendary(1), self.legendary(200, container='storage')])
        self.service._lock_monitor_once()
        self.assertEqual(self.client.actions, [('lock', 'offline-inventory-1'),
                                                ('lock', 'offline-storage-200')])
        self.assertFalse(self.client.items[0]['locked'])
        self.assertTrue(all(row['locked'] for row in self.client.items[1:]))
        self.service._lock_monitor_once()
        self.assertEqual(len(self.client.actions), 2)

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
