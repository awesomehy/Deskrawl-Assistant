from copy import deepcopy
import unittest

from deskrawl_assistant.inventory_categories import inventory_category, GEMS


class InventoryCategoryTests(unittest.TestCase):
    def test_all_gem_tiers_are_separate_from_native_rarity(self):
        for tier, label in enumerate(GEMS, 1):
            row = {'internal_name': f'GemRuby{tier}', 'rarity': 0}
            self.assertEqual(inventory_category(row, 'gem', {})['quality_key'], f'gem-{tier}')
            self.assertEqual(inventory_category(row, 'gem', {})['rarity_label'], label)

    def test_runes_use_all_four_game_qualities(self):
        for key, quality in [('UncommonRune_1', 'uncommon'), ('RareRune_1', 'rare'),
                             ('LegendaryRune_1', 'grand'), ('HunterSetRune1', 'set')]:
            self.assertEqual(inventory_category({'internal_name': key}, 'rune', {})['quality_key'], 'rune-' + quality)

    def test_other_subtypes_and_catalog_rarity_do_not_change_native_kind_or_input(self):
        for key, category in [('GoldBar', 'gold_bar'), ('UpgradeCommodity1', 'consecration_stone'),
                              ('Rein_VolcanoCinderlash', 'rein'), ('RareFish1', 'fish')]:
            row = {'internal_name': key, 'is_equipment': False, 'kind': 'material'}
            before = deepcopy(row)
            result = inventory_category(row, 'material', {'rarity': 3})
            self.assertEqual(result['item_category'], category)
            self.assertEqual(result['rarity'], 3)
            self.assertEqual(row, before)
        self.assertIsNone(inventory_category({'internal_name': 'unknown'}, 'material', {})['rarity'])

    def test_explicit_unknown_rarity_is_not_replaced_by_a_catalog_guess(self):
        result = inventory_category({'internal_name': 'GoldBar', 'base_rarity': None, 'rarity': 0},
                                    'material', {'rarity': 3})
        self.assertIsNone(result['rarity'])
