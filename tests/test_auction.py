from copy import deepcopy
import unittest

from deskrawl_assistant.auction import DEFAULT_AUCTION, CATEGORIES, category, allows, validate_settings, next_action
from deskrawl_assistant.managed_mode import DEFAULT_SETTINGS, validate_settings as validate_managed
from deskrawl_assistant.models import ValidationError


def item(index=0, **extra):
    return dict(item_uid=f'uid-{index}', internal_name='DivineBoots1', name_key='DivineBoots1',
                identity_verified=True, auction_candidate=True, container='inventory', slot_index=index,
                locked=False, bound=False, issues=[], count=1, **extra)


def observation(items):
    return {'complete': True, 'scene': {'in_town': True}, 'items': items,
            'counts': {'inventory': {'capacity': 40, 'occupied': len(items)}},
            'auction': {'available': True, 'enabled': True, 'listing_count': 0, 'capacity': 8,
                        'steamid': '76561198000000001', 'steamid_verified': True},
            'storage': {'allowed_indices': list(range(50))}}


class AuctionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {**deepcopy(DEFAULT_AUCTION), 'upload_enabled': True, 'categories': ['divine_equipment']}

    def test_old_settings_migrate_off_and_preserve_other_preferences(self):
        old = deepcopy(DEFAULT_SETTINGS)
        old.pop('auction')
        old['cleanup']['equipment_action'] = 'salvage'
        new = validate_managed(old)
        self.assertEqual(new['auction'], DEFAULT_AUCTION)
        self.assertEqual(new['cleanup']['equipment_action'], 'salvage')
        self.assertNotIn('auction', old)

    def test_default_does_nothing(self):
        self.assertIsNone(next_action(DEFAULT_AUCTION, observation([item()])))

    def test_managed_loader_migrates_nested_development_fields_before_shape_validation(self):
        base = validate_managed(deepcopy(DEFAULT_SETTINGS))
        base['auto_equip'].update(enabled=True, minimum_delta=17, disabled_slots=[2, 6])
        base['cleanup']['rune_pages'] = [2, 4]
        base['merchant']['weights'].update({'2': 1, '3': 10})
        base['auction'].update(upload_enabled=True, sell_enabled=True,
            categories=['grand_rune'], excluded_keys=['LegendaryRune_HunterSpecial1'])
        cases = [
            {**base['auction'], 'allow_locked': True},
            {**base['auction'], 'retain_count': 3},
            {k: v for k, v in base['auction'].items() if k != 'excluded_keys'},
            {k: v for k, v in base['auction'].items() if k != 'categories'},
            {**{k: v for k, v in base['auction'].items() if k not in ('categories', 'excluded_keys')},
             'item_keys': ['GoldBar'], 'allow_locked': True, 'retain_count': 3},
        ]
        # Missing categories with no legacy permissions cannot authorize an
        # active upload. Model the old disabled default for that case.
        cases[3].update(upload_enabled=False, sell_enabled=False)
        for auction in cases:
            with self.subTest(fields=sorted(auction)):
                raw = {**deepcopy(base), 'auction': deepcopy(auction)}
                before = deepcopy(raw)
                migrated = validate_managed(raw)
                self.assertEqual(migrated['auction'], validate_settings(auction))
                self.assertEqual({k: v for k, v in migrated.items() if k != 'auction'},
                                 {k: v for k, v in base.items() if k != 'auction'})
                self.assertEqual(raw, before)
                self.assertEqual(validate_managed(migrated), migrated)

    def test_sale_only_settings_disable_sale_without_authorizing_upload_or_losing_policy(self):
        old = {**deepcopy(DEFAULT_AUCTION), 'sell_enabled': True,
               'categories': ['grand_rune'], 'excluded_keys': ['LegendaryRune_HunterSpecial1'],
               'price_rule': 'highest_buy'}
        new = validate_settings(old)
        self.assertFalse(new['upload_enabled'])
        self.assertFalse(new['sell_enabled'])
        self.assertEqual(new, {**old, 'sell_enabled': False})
        self.assertTrue(old['sell_enabled'])
        managed = validate_managed({**deepcopy(DEFAULT_SETTINGS), 'auction': old})
        self.assertEqual(managed['auction'], new)

    def test_categories_authorize_each_known_family_and_exact_exclusions_win(self):
        cases = [('DivineBoots1', {}, 'divine_equipment'),
                 ('LegendaryBoots1', {'is_black_mist': True}, 'black_mist_equipment'),
                 ('HunterSetRune1_1', {}, 'set_rune'),
                 ('LegendaryRune_HunterSpecial1', {}, 'grand_rune'),
                 ('Rein_VolcanoDragon', {'rarity': 3}, 'legendary_rein'),
                 ('UpgradeCommodity1', {}, 'consecration_stone'), ('GoldBar', {}, 'gold_bar')]
        for name, extra, group in cases:
            with self.subTest(name=name):
                row = {**item(), 'internal_name': name, **extra}
                config = {**self.config, 'item_keys': [], 'categories': [group]}
                self.assertEqual(category(row), group)
                self.assertIsNotNone(next_action(config, observation([row])))
                config['excluded_keys'] = [name]
                self.assertIsNone(next_action(config, observation([row])))
        self.assertEqual(set(group for _, _, group in cases), set(CATEGORIES))
        config = {**self.config, 'item_keys': [], 'categories': list(CATEGORIES)}
        for name, rarity in [('LegendaryBoots1', 3), ('RareRune_HunterSpecial1', 2),
                             ('MovementPotion1', 1), ('Rein_VolcanoDragon', 2)]:
            self.assertFalse(allows(config, {'internal_name': name, 'rarity': rarity}))

    def test_migration_does_not_expand_specific_items_into_whole_categories(self):
        old = deepcopy(self.config)
        old.pop('categories')
        old.update(item_keys=['DivineBoots1', 'retired-item'], sell_enabled=True, retain_count=3,
                   excluded_keys=['DivineBow1'])
        new = validate_settings(old)
        self.assertEqual(new['categories'], [])
        self.assertEqual(new['item_keys'], [])
        self.assertFalse(new['upload_enabled'])
        self.assertFalse(new['sell_enabled'])
        self.assertNotIn('retain_count', new)
        self.assertEqual(new['excluded_keys'], ['DivineBow1'])
        self.assertEqual(old['item_keys'], ['DivineBoots1', 'retired-item'])
        self.assertIsNone(next_action(new, observation([item()])))
        self.assertIsNone(next_action(new, observation([{**item(), 'internal_name': 'DivineBow1'}])))
        for categories in (['typo'], 'grand_rune', [None], ['grand_rune', 'grand_rune']):
            with self.assertRaises(ValidationError):
                validate_settings({**self.config, 'categories': categories})

    def test_legacy_keys_cannot_authorize_even_before_configuration_migration(self):
        old = {**self.config, 'categories': [], 'item_keys': ['DivineBoots1']}
        self.assertFalse(allows(old, item()))
        self.assertIsNone(next_action(old, observation([item()])))

    def test_migration_preserves_explicit_categories_and_removes_legacy_scope(self):
        old = {**self.config, 'item_keys': ['GoldBar'], 'sell_enabled': True,
               'excluded_keys': ['DivineBoots1'], 'retain_count': 2}
        new = validate_settings(old)
        self.assertEqual(new, {k: ([] if k == 'item_keys' else v) for k, v in old.items() if k != 'retain_count'})
        self.assertFalse(allows(new, {'internal_name': 'GoldBar'}))
        self.assertFalse(allows(new, item()))
        self.assertTrue(allows(new, {'internal_name': 'DivineBow1'}))

    def test_managed_configuration_load_migrates_legacy_permissions(self):
        old = deepcopy(DEFAULT_SETTINGS)
        old['auction'].update(upload_enabled=True, sell_enabled=True, item_keys=['GoldBar'])
        new = validate_managed(old)
        self.assertEqual(new['auction'], DEFAULT_AUCTION)

    def test_protected_whitelist_overrides_allowed_upload_types(self):
        self.config['excluded_keys'] = ['DivineBoots1']
        self.assertIsNone(next_action(self.config, observation([item()])))
        # Old development settings gain an empty protection list without
        # changing either automation switch or the selected categories.
        old = deepcopy(self.config)
        old.pop('excluded_keys')
        migrated = validate_settings(old)
        self.assertEqual(migrated['excluded_keys'], [])
        self.assertEqual(migrated['item_keys'], old['item_keys'])
        self.assertEqual(migrated['categories'], old['categories'])
        self.assertNotIn('excluded_keys', old)

    def test_selected_category_uploads_single_item(self):
        action = next_action(self.config, observation([item()]))
        self.assertEqual(action['kind'], 'auction_upload')
        self.assertEqual(action['quantity'], 1)
        self.assertNotIn('allow_locked', action)

    def test_retired_controls_are_removed_without_changing_scope_or_switches(self):
        for locked in (False, True):
            old = {**self.config, 'allow_locked': locked, 'retain_count': 100000,
                   'excluded_keys': ['DivineBow1']}
            new = validate_settings(old)
            self.assertNotIn('allow_locked', new)
            self.assertNotIn('retain_count', new)
            self.assertEqual(new, {**self.config, 'excluded_keys': ['DivineBow1']})
            self.assertEqual(old['retain_count'], 100000)
            self.assertEqual(next_action(new, observation([{**item(), 'locked': True}]))['kind'], 'auction_upload')

    def test_closed_ui_is_refreshed_before_upload_not_assumed_empty(self):
        value = observation([item()])
        value['auction'].update(enabled=None, listing_count=None, needs_refresh=True)
        self.assertEqual(next_action(self.config, value)['kind'], 'auction_open')

    def test_excluded_stock_does_not_open_the_auction_dialog(self):
        self.config['excluded_keys'] = ['DivineBoots1']
        value = observation([item()])
        value['auction'].update(enabled=None, listing_count=None, needs_refresh=True)
        self.assertIsNone(next_action(self.config, value))

    def test_same_kind_waits_for_previous_inventory_mapping_but_other_kind_can_progress(self):
        value = observation([item()])
        value['auction']['blocked_keys'] = ['DivineBoots1']
        self.assertIsNone(next_action(self.config, value))

    def test_received_history_does_not_use_an_active_upload_slot(self):
        value = observation([item()])
        value['auction'].update(listing_count=None,
            listings=[{'received': True}] * 20 + [{'received': False}] * 7)
        self.assertEqual(next_action(self.config, value)['kind'], 'auction_upload')
        value['auction']['listings'].append({'received': False})
        self.assertIsNone(next_action(self.config, value))

    def test_capacity_busy_offline_and_unreadable_state_skip(self):
        for patch in ({'listing_count':8}, {'listing_count':None}, {'capacity':9}, {'busy':True},
                      {'pending':True}, {'available':False}, {'enabled':False}, {'steamid_verified': False}):
            value = observation([item()]); value['auction'].update(patch)
            with self.subTest(patch=patch): self.assertIsNone(next_action(self.config, value))
        value = observation([item()]); value['scene']['in_town'] = False
        self.assertIsNone(next_action(self.config, value))

    def test_bound_uidless_and_untrusted_never_upload_while_locks_do_not_block(self):
        for patch in ({'bound':True}, {'item_uid':''},
                      {'identity_verified':False}, {'auction_candidate':False}, {'issues':['unknown']},
                      {'count':2}, {'container':'equipped'}):
            value = item(); value.update(patch)
            with self.subTest(patch=patch): self.assertIsNone(next_action(self.config, observation([value])))
        for locked in (False, True, None, 1):
            value = item(); value['locked'] = locked
            self.assertEqual(next_action(self.config, observation([value]))['kind'], 'auction_upload')
            self.config['excluded_keys'] = ['DivineBoots1']
            self.assertIsNone(next_action(self.config, observation([value])))
            self.config['excluded_keys'] = []

    def test_warehouse_prepare_respects_unlocks_and_potion_capacity(self):
        value = item(); value['container'] = 'storage'
        obs = observation([value])
        self.assertEqual(next_action(self.config, obs)['target'], 'inventory')
        obs['storage']['allowed_indices'] = []
        self.assertIsNone(next_action(self.config, obs))
        obs['storage']['allowed_indices'] = [0]
        obs['counts']['inventory']['occupied'] = 40
        self.assertIsNone(next_action(self.config, obs))

    def test_pending_identity_is_not_submitted_twice(self):
        value = observation([item()]); value['auction']['blocked_uids'] = ['uid-0']
        self.assertIsNone(next_action(self.config, value))

    def test_configuration_types_are_strict(self):
        for patch in ({'upload_enabled':1}, {'price_rule':'url'}, {'item_keys':['DivineBoots1','DivineBoots1']},
                      {'categories':[]}, {'item_keys':[{}]}):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                validate_settings({**self.config, **patch})
        # Retired catalog keys are harmless migration input, never permissions.
        self.assertEqual(validate_settings({**self.config, 'item_keys': ['retired-item']}, {'other'})['item_keys'], [])


if __name__ == '__main__': unittest.main()
