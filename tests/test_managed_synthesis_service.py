"""Serial synthesis orchestration and retention gates; no native writes."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from tests import test_managed_service as service_fixtures
from deskrawl_assistant.item_protection import disposal_protection
from deskrawl_assistant.managed_service import required_capabilities
from deskrawl_assistant.managed_mode import identity
from deskrawl_assistant.models import ValidationError


class ManagedSynthesisServiceTests(unittest.TestCase):
    setUp = service_fixtures.ManagedServiceTests.setUp

    def enable(self, *, gems=False, runes=False):
        if not hasattr(self.client, '_synthesis_control_snapshot'):
            self.client.character_id = '0:0:offline-character'
            self.client._synthesis_control_snapshot = self.client.control_snapshot
            self.client.control_snapshot = lambda: {**self.client._synthesis_control_snapshot(),
                'character_id':self.client.character_id}
        self.service.managed.settings['synthesis'] = {
            'gems': {'enabled': gems, 'max_quality': 4},
            'runes': {'enabled': runes, 'max_quality': 'rare', 'excluded_keys': []}}
        for capability in ('gem_synthesis', 'rune_synthesis'):
            self.client.capabilities[capability] = {'available': True, 'native_verified': True}
        self.service.managed.running = True
        self.service.managed.session = 'offline-session'
        self.service.managed.character_id = self.client.character_id

    def test_disabled_or_combat_synthesis_never_reads_optional_profiles(self):
        self.client.synthesis_snapshot = Mock(side_effect=AssertionError('unexpected optional read'))
        self.assertEqual(self.service._managed_observation()['synthesis'], {})
        self.enable(gems=True, runes=True)
        self.client.in_town = False
        self.assertEqual(self.service._managed_observation()['synthesis'], {})
        self.client.synthesis_snapshot.assert_not_called()

    def test_enabled_native_reads_only_selected_group(self):
        self.enable(gems=True)
        self.client.synthesis_snapshot = Mock(return_value={
            'gems': {'available': True, 'recipes': [], 'gold': 1000},
            'runes': {'available': False, 'reason': 'unrelated unsupported profile'}})
        world = self.service._managed_observation()
        self.client.synthesis_snapshot.assert_called_once_with(gems=True, runes=False)
        self.assertTrue(world['complete'])
        self.assertEqual(set(world['synthesis']), {'gems'})
        self.assertTrue(world['synthesis']['gems']['available'])

    def test_optional_read_failure_does_not_poison_normal_observation(self):
        self.enable(runes=True)
        self.client.synthesis_snapshot = Mock(side_effect=ValueError('unsupported rune recipe'))
        world = self.service._managed_observation()
        self.assertTrue(world['complete'])
        self.assertTrue(world['scene']['in_town'])
        self.assertFalse(world['synthesis']['runes']['available'])
        self.assertIn('unsupported rune recipe', world['synthesis']['runes']['reason'])
        self.assertEqual(self.client.actions, [])

    def test_raw_synthesis_identity_and_level_fields_survive_display_payload(self):
        # Planner safety must use original native facts, including explicit
        # False/zero values which presentation rows may otherwise omit.
        self.client.items = [service_fixtures.equipment(
            key='HunterRune1', is_equipment=False, kind='rune', item_class='RuneData',
            item_type=3, slot_locked=False, identity_verified=True,
            rune_level=17, rune_xp=12345)]
        world = self.service._managed_observation()
        actual = world['items'][0]
        for name in ('slot_locked', 'identity_verified', 'rune_xp', 'item_class', 'rune_level'):
            self.assertEqual(actual[name], self.client.items[0][name], name)
        self.client.items[0].update(slot_locked=True, identity_verified=False, rune_level=0, rune_xp=0)
        actual = self.service._managed_observation()['items'][0]
        for name in ('slot_locked', 'identity_verified', 'rune_xp', 'item_class', 'rune_level'):
            self.assertEqual(actual[name], self.client.items[0][name], name)

    def test_synthesis_capabilities_are_independent_and_opt_in(self):
        settings = deepcopy(self.service.managed.settings)
        self.assertFalse({'gem_synthesis', 'rune_synthesis'} & required_capabilities(settings))
        self.enable(gems=True)
        self.assertIn('gem_synthesis', required_capabilities(self.service.managed.settings))
        self.assertNotIn('rune_synthesis', required_capabilities(self.service.managed.settings))
        self.enable(runes=True)
        self.assertIn('rune_synthesis', required_capabilities(self.service.managed.settings))
        self.assertNotIn('gem_synthesis', required_capabilities(self.service.managed.settings))

    def test_synthesis_only_requires_town_cleanup_but_not_travel(self):
        self.enable(gems=True)
        settings = self.service.managed.settings
        settings['farming']['enabled'] = False
        settings['merchant']['enabled'] = False
        settings['cleanup']['equipment_policies']['normal'].update(action='sell', fallback='salvage')
        capabilities = required_capabilities(settings)
        self.assertTrue({'transfer', 'lock', 'chests', 'ground_pickup', 'gem_synthesis'} <= capabilities)
        self.assertTrue({'sell', 'salvage'} <= capabilities)
        self.assertFalse({'travel', 'difficulty', 'rune_synthesis'} & capabilities)

    def test_legacy_sell_keep_and_empty_pages_migrate_without_resetting_other_settings(self):
        legacy = deepcopy(self.service.managed.settings)
        legacy.pop('synthesis', None)
        legacy['auto_equip'].update(enabled=True, minimum_delta=23, disabled_slots=[6, 7])
        legacy['cleanup'].update(rune_action='sell', gem_action='sell', gold_bar_action='keep',
                                 legendary_rein_action='keep', rune_pages=[], gem_pages=[],
                                 gold_bar_pages=[], legendary_rein_pages=[])
        original = json.dumps(legacy, ensure_ascii=False)
        self.service.managed_config.write_text(original, encoding='utf-8')
        self.service._init_managed()
        self.assertFalse(self.service.managed_config_load_failed)
        current = self.service.managed.settings
        self.assertEqual(current['auto_equip'], legacy['auto_equip'])
        for group in ('rune', 'gem', 'gold_bar', 'legendary_rein'):
            self.assertEqual(current['cleanup'][group+'_action'], 'store')
            self.assertEqual(current['cleanup'][group+'_pages'], [])
        self.assertIn('符文、宝石、金条、传说缰绳', self.service.managed_config_error)
        self.assertEqual(self.service.managed_config.read_text(encoding='utf-8'), original)
        self.assertEqual(self.client.actions, [])

    def test_all_managed_materials_reject_forged_or_legacy_disposal(self):
        self.service.managed.running = True
        for key, kind, item_class, rarity in (
                ('HunterRune1', 'rune', 'RuneData', 2),
                ('GemRuby1', 'gem', 'GemData', 0),
                ('GoldBar', 'material', 'ItemData', 0),
                ('Rein_1', 'material', 'ItemData', 3)):
            raw = service_fixtures.equipment(key=key, is_equipment=False, kind=kind, item_class=item_class,
                            rarity=rarity, base_rarity=rarity)
            # Only managed automation receives the new retention requirement.
            self.assertEqual(disposal_protection(raw), '')
            self.client.items = [raw]
            world = self.service._managed_observation()
            for action in ('sell', 'salvage'):
                with self.subTest(key=key, action=action), self.assertRaisesRegex(ValidationError, '托管必须收纳'):
                    self.service._managed_execute({'kind': action, 'item': world['items'][0]}, world)
            self.assertEqual(self.client.actions, [])

    def test_new_pending_storage_material_blocks_stale_travel_or_merchant_open(self):
        self.client.items = []
        self.service.managed.running = True
        clean = self.service._managed_observation()
        for key, kind, item_class, rarity in (
                ('HunterRune1', 'rune', 'RuneData', 2), ('GemRuby1', 'gem', 'GemData', 0),
                ('GoldBar', 'material', 'ItemData', 0), ('Rein_1', 'material', 'ItemData', 3)):
            self.client.items = [service_fixtures.equipment(key=key, kind=kind, is_equipment=False,
                item_class=item_class, rarity=rarity, base_rarity=rarity)]
            for kind in ('enter_dungeon', 'switch_difficulty', 'merchant_open'):
                action = {'kind': kind, 'dungeon_id': 'offline-map', 'difficulty': 'Normal'}
                with self.subTest(key=key, action=kind):
                    result = self.service._managed_execute(action, clean)
                    self.assertTrue(result['retryable'])
                    self.assertFalse(result['verified'])
                    self.assertEqual(self.client.actions, [])

    def craft(self, kind='synthesize_gems'):
        return {'kind': kind, 'recipe_id': 'gem-0-1', 'recipe': {'input_count': 10},
                'inputs': [], 'input_keys': ['GemRuby1'], 'input_uids': [], 'count': 1,
                'max_quality': 4, 'reason': 'test synthesis'}

    def test_craft_replans_before_native_submit_and_preserves_verified_receipt(self):
        self.enable(gems=True)
        self.client.synthesis_snapshot = Mock(return_value={'gems': {'available': True, 'recipes': []}})
        action = self.craft()
        receipt = {'effect_confirmed': True, 'receipt_id': 'receipt-1',
                   'inputs': [{'internal_name': 'GemRuby1', 'quantity': 10}],
                   'outputs': [{'internal_name': 'GemRuby2', 'quantity': 1}]}
        self.client.native_handler = lambda kind, payload: deepcopy(receipt)
        before = self.service._managed_observation()
        with patch('deskrawl_assistant.synthesis.next_action', return_value=deepcopy(action)) as planner:
            result = self.service._managed_execute(action, before)
        planner.assert_called_once()
        self.assertTrue(result['verified'])
        self.assertEqual(result['receipt_id'], 'receipt-1')
        self.assertEqual(result['inputs'], receipt['inputs'])
        self.assertEqual(result['outputs'], receipt['outputs'])
        self.assertEqual([kind for kind, _ in self.client.actions], ['synthesize_gems'])

    def test_changed_cap_or_rune_exclusions_blocks_old_craft(self):
        self.enable(runes=True)
        self.client.synthesis_snapshot = Mock(return_value={'runes': {'available': True, 'recipes': []}})
        action = self.craft('synthesize_runes')
        action.update(excluded_keys=[], max_quality='legendary')
        before = self.service._managed_observation()
        for planned in (None, {**action, 'max_quality': 'rare'},
                        {**action, 'excluded_keys': ['HunterRune1']},
                        {**action, 'input_uids': ['changed-uid']}):
            with self.subTest(planned=planned), patch('deskrawl_assistant.synthesis.next_action', return_value=planned):
                result = self.service._managed_execute(action, before)
                self.assertTrue(result['retryable'])
                self.assertFalse(result['verified'])
        self.assertEqual(self.client.actions, [])

    def test_craft_only_runs_in_town_and_is_not_started_during_finish(self):
        self.enable(gems=True)
        self.client.in_town = False
        world = self.service._managed_observation()
        self.assertTrue(self.service._managed_execute(self.craft(), world)['retryable'])
        self.client.in_town = True
        self.service.managed.finishing = True
        self.assertTrue(self.service._managed_execute(self.craft(), world)['retryable'])
        self.assertEqual(self.client.actions, [])

    def test_material_preparation_replans_and_rejects_changed_stack(self):
        self.enable(gems=True)
        self.client.synthesis_snapshot = Mock(return_value={'gems': {'available': True, 'recipes': []}})
        self.client.items = [service_fixtures.equipment(container='storage', key='GemRuby1', is_equipment=False,
                                     item_class='GemData', kind='gem', count=10)]
        world = self.service._managed_observation()
        action = {'kind': 'transfer', 'reason_code': 'synthesis_prepare',
                  'item': world['items'][0], 'target': 'inventory'}
        self.client.items[0]['count'] = 9
        fresh = deepcopy(action)
        fresh['item']['count'] = 9
        with patch('deskrawl_assistant.synthesis.next_action', return_value=fresh):
            result = self.service._managed_execute(action, world)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions, [])

    def test_material_preparation_can_take_from_any_open_warehouse_page(self):
        self.enable(gems=True)
        self.client.synthesis_snapshot = Mock(return_value={'gems': {'available': True, 'recipes': []}})
        self.client.items = [service_fixtures.equipment(0, container='storage', key='GemRuby1', is_equipment=False,
                                     item_class='GemData', kind='gem', count=10)]
        world = self.service._managed_observation()
        action = {'kind': 'transfer', 'reason_code': 'synthesis_prepare',
                  'item': world['items'][0], 'target': 'inventory'}
        # Gem destination remains page five; ingredient is on open page one.
        self.assertEqual(self.service.managed.settings['cleanup']['gem_pages'], [5])
        with patch('deskrawl_assistant.synthesis.next_action', return_value=deepcopy(action)):
            result = self.service._managed_execute(action, world)
        self.assertTrue(result['verified'])
        self.assertEqual(self.client.items[0]['container'], 'inventory')

    def test_finish_blocks_already_planned_material_withdrawal(self):
        self.enable(gems=True)
        self.client.synthesis_snapshot = Mock(return_value={'gems': {'available': True, 'recipes': []}})
        self.client.items = [service_fixtures.equipment(0, container='storage', key='GemRuby1',
            is_equipment=False, item_class='GemData', kind='gem', count=10)]
        world = self.service._managed_observation()
        action = {'kind': 'transfer', 'reason_code': 'synthesis_prepare',
                  'item': world['items'][0], 'target': 'inventory'}
        self.service.managed.finishing = True
        result = self.service._managed_execute(action, world)
        self.assertTrue(result['retryable'])
        self.assertFalse(result['verified'])
        self.assertEqual(self.client.items[0]['container'], 'storage')
        self.assertEqual(self.client.actions, [])

    def test_finish_requested_during_fresh_read_blocks_material_withdrawal(self):
        self.enable(gems=True)
        self.client.synthesis_snapshot = Mock(return_value={'gems': {'available': True, 'recipes': []}})
        self.client.items = [service_fixtures.equipment(0, container='storage', key='GemRuby1',
            is_equipment=False, item_class='GemData', kind='gem', count=10)]
        world = self.service._managed_observation()
        action = {'kind': 'transfer', 'reason_code': 'synthesis_prepare',
                  'item': world['items'][0], 'target': 'inventory'}
        def finish_while_reading(**kwargs):
            self.service.managed.finishing = True
            return deepcopy(world)
        with patch.object(self.service, '_managed_observation', side_effect=finish_while_reading), \
                patch('deskrawl_assistant.synthesis.next_action', return_value=deepcopy(action)):
            result = self.service._managed_execute(action, world)
        self.assertTrue(result['retryable'])
        self.assertFalse(result['verified'])
        self.assertEqual(self.client.items[0]['container'], 'storage')
        self.assertEqual(self.client.actions, [])

    def blessing_stone(self, slot=0, **extra):
        return service_fixtures.equipment(slot, key='UpgradeCommodity1', is_equipment=False,
            item_class='ItemData', item_type=7, kind='material', item_uid='', instance_id='',
            identity_verified=False, count=4, base_rarity=0, rarity=0, **extra)

    def merchant_world(self, kept, *, capacity=4):
        from tests.test_progression import state
        self.client.items = deepcopy(kept)
        raw_snapshot = service_fixtures.FakeManagedClient.snapshot.__get__(self.client)
        def small_snapshot():
            value = raw_snapshot()
            value['containers']['inventory']['slot_count'] = capacity
            return value
        self.client.snapshot = small_snapshot
        growth = state()
        growth['merchant']['stock'][1].update(price=200, available_count=10)
        growth['merchant']['open'] = True
        self.client.progression_snapshot = Mock(side_effect=lambda **kw: deepcopy(growth))
        self.client.capabilities['merchant'] = {'available': True, 'native_verified': True}
        self.service._managed_refresh_control()
        config = service_fixtures.ManagedServiceTests.settings(self)
        config['merchant']['enabled'] = True
        config['merchant']['weights']['2'] = 10
        config['background'].update(rule_monitoring=False, inventory_automation=False)
        config['cleanup'].update(equipment_action='salvage', equipment_fallback='salvage')
        self.service.save_managed(config)
        self.service.set_managed_running(True)
        return growth

    def assert_retained_merchant_cycles(self, kept):
        growth = self.merchant_world(kept)
        purchases = []
        def native(kind, payload):
            if kind == 'merchant_buy':
                quantity = payload.get('quantity', 1)
                occupied = {r['slot_index'] for r in self.client.items if r['container'] == 'inventory'}
                slots = [n for n in range(4) if n not in occupied]
                self.assertLessEqual(quantity, len(slots))
                uids = []
                for slot in slots[:quantity]:
                    uid = 'merchant-test-' + str(len(purchases))
                    purchases.append(uid); uids.append(uid)
                    self.client.items.append(service_fixtures.equipment(slot, item_uid=uid, instance_id=uid))
                growth['soul_shards']['current'] -= quantity * payload['expected_price']
                return {'effect_confirmed': True, 'merchant_open': True, 'quantity': quantity, 'received_uids': uids}
            if kind in ('merchant_open', 'merchant_close'):
                growth['merchant']['open'] = kind == 'merchant_open'
            else:
                self.assertEqual(kind, 'salvage')
                self.client.items = [r for r in self.client.items if identity(r) != identity(payload['item'])]
            return {'effect_confirmed': True, 'quantity': 1}
        self.client.native_handler = native
        for _ in range(60):
            self.service._work('managed', {})
            self.assertTrue(self.service.managed.running, self.service.managed.status)
            if not self.service.managed.merchant_pending:
                break
        self.assertFalse(self.service.managed.merchant_pending, self.service.managed.status)
        self.assertEqual(growth['soul_shards']['current'], 100)
        self.assertEqual(len(purchases), 9)
        kinds = [a[0] for a in self.client.actions]
        self.assertGreaterEqual(kinds.count('merchant_buy'), 3)
        self.assertEqual(self.client.items, kept)

    def test_blessing_stone_retained_during_initial_and_later_merchant_batches(self):
        self.assert_retained_merchant_cycles([self.blessing_stone()])

    def test_blessing_stone_and_potion_can_share_bag_through_merchant_batches(self):
        self.assert_retained_merchant_cycles([self.blessing_stone(), service_fixtures.potion(1)])

    def test_retained_material_slots_still_prevent_over_capacity_purchases(self):
        self.merchant_world([self.blessing_stone(), service_fixtures.potion(1)], capacity=2)
        for _ in range(3):
            if not self.service.managed.running: break
            self.service._work('managed', {})
        self.assertNotIn('merchant_buy', [a[0] for a in self.client.actions])
        self.assertEqual(len(self.client.items), 2)

    def test_unverified_blessing_stone_is_not_whitelisted_as_a_consumable(self):
        from deskrawl_assistant.managed_mode import retained_merchant_item
        raw = self.blessing_stone()
        self.assertTrue(retained_merchant_item(raw))
        for key, value in (('item_type', 2), ('item_class', 'RuneData'), ('count', 0),
                           ('issues', ['身份未确认']), ('internal_name', 'UnknownCommodity')):
            changed = {**raw, key: value}
            if key == 'internal_name': changed['name_key'] = value
            self.assertFalse(retained_merchant_item(changed), (key, value))
