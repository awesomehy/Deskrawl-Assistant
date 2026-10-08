"""Preview regressions from the user's chest and a verified +5 glove."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from deskrawl_assistant.models import Rule
from deskrawl_assistant.web_service import AssistantService
from deskrawl_assistant.stat_display import preview_value, upgraded_value
from test_headline_affixes import FixtureClient


CHEST = json.loads((Path(__file__).parent / 'fixtures/preview-chest.json').read_text(encoding='utf-8'))


class EquipmentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FixtureClient()
        self.client.data['containers']['storage']['slots'].append(copy.deepcopy(CHEST))
        self.service = AssistantService(self.client, Path(self.tmp.name) / 'rules.json')
        self.addCleanup(self.service.close)
        self.service._publish(self.client.snapshot())

    def rule(self, *, enabled=True, count=3, secondary=False):
        result = {'id': 'chest-preview', 'name': '不屈胸甲', 'enabled': enabled,
                  'equipment_keys': ['LegendaryChestArmor5'], 'group_mode': 'all',
                  'groups': {'primary': {'selected_stats': ['Stats.Dexterity', 'Stats.MaxHealth', 'Stats.Dodge',
                      'Stats.MaxHealthPercent', 'Stats.SpecialAbilityDamage', 'Stats.DamageVsHealthy', 'Stats.DamageVsInjured'],
                      'operator': '>=', 'count': count}}}
        if secondary:
            result['groups']['secondary'] = {'selected_stats': ['Stats.LifeOnHit'], 'operator': '>=', 'count': 1}
        return result

    def chest(self):
        return next(i for i in self.service.state()['items'] if i['instance_id'] == CHEST['instance_id'])

    def test_user_chest_displays_three_hits_and_real_percentages(self):
        self.service.save_rule(self.rule())
        item = self.chest()
        stats = {s['key']: s for g in item['groups'].values() for s in g}
        self.assertEqual('777', stats['Stats.Armor']['display_value'])
        self.assertEqual('124.4%', stats['Stats.SpecialAbilityDamage']['display_value'])
        self.assertEqual('155.5%', stats['Stats.DamageVsInjured']['display_value'])
        self.assertEqual('15.5%', stats['Stats.PhysicalDamageReduction']['display_value'])
        self.assertEqual('31', stats['Stats.LifeOnHit']['display_value'])
        self.assertEqual({'Stats.SpecialAbilityDamage', 'Stats.DamageVsInjured', 'Stats.DamageVsHealthy'},
                         {k for k, s in stats.items() if s['matched']})
        self.assertTrue(item['matches'])
        self.assertFalse(self.client.actions)

    def test_partial_hits_highlight_even_when_whole_item_fails_count(self):
        self.service.save_rule(self.rule(count=4))
        item = self.chest()
        self.assertFalse(item['matches'])
        self.assertEqual(3, sum(s['matched'] for s in item['groups']['primary']))

    def test_disabled_rules_and_disabled_secondary_never_highlight(self):
        self.service.save_rule(self.rule(enabled=False))
        self.assertFalse(any(s['matched'] for g in self.chest()['groups'].values() for s in g))
        self.service.save_rule(self.rule())
        self.assertFalse(any(s['matched'] for s in self.chest()['groups']['secondary']))
        self.service.save_rule(self.rule(secondary=True))
        self.assertTrue(self.chest()['groups']['secondary'][0]['matched'])

    def test_base_armor_does_not_highlight_even_in_old_imported_rule(self):
        rule = self.rule()
        rule['groups']['primary']['selected_stats'].append('Stats.Armor')
        self.service.rules = [Rule.from_dict(rule, self.service.catalog)]
        self.assertFalse(self.chest()['groups']['base'][0]['matched'])

    def test_plus_five_distribution_keeps_original_array_indexes(self):
        row = copy.deepcopy(CHEST)
        row.update(upgrade_level=5, rarity=3)
        raw = [774, 0.1550000011920929, 0.23199999332427979, 46, 0.12399999797344208, 15]
        stat_ids = [8, 13, 6, 3, 4, 35]
        names = [self.service.adapter.stat_names[n] for n in stat_ids]
        row['modifiers'] = [{'stat': n, 'stat_name': name, 'value': v, 'source': 'generated_modifiers', 'type': 0}
                            for n, name, v in zip(stat_ids, names, raw)]
        row.update(name_key='LegendaryGloves2', internal_name='LegendaryGloves2', equip_slot=9)
        adapted = self.service.adapter.adapt_item(row, snapshot_token='upgrade', session_id='999')
        self.assertEqual(['851', '17.1%', '25.5%', '51', '13%', '16'],
                         [preview_value(m, row)['display_value'] for m in adapted.display_modifiers])
        # Display scaling must never replace the original rule observation.
        self.assertEqual(str(raw[4]), str(adapted.observation.affixes['Stats.CritChance'][0].value))
        self.assertAlmostEqual(1.45, upgraded_value(1, 0, 6, 15, 3), places=6)

    def test_missing_value_is_not_displayed_as_zero_or_required_for_type_match(self):
        row = next(r for r in self.client.data['containers']['storage']['slots'] if r['instance_id'] == CHEST['instance_id'])
        row['modifiers'][1]['value'] = None
        self.service.save_rule(self.rule())
        self.service._publish(self.client.snapshot())
        item = self.chest()
        stat = next(s for s in item['groups']['primary'] if s['key'] == 'Stats.SpecialAbilityDamage')
        self.assertEqual('未读取', stat['display_value'])
        self.assertTrue(stat['matched'])
        self.assertTrue(item['matches'])

    def test_duplicate_rows_remain_visible_but_count_one_type(self):
        row = next(r for r in self.client.data['containers']['storage']['slots'] if r['instance_id'] == CHEST['instance_id'])
        row['modifiers'].append(copy.deepcopy(row['modifiers'][1]))
        self.service.save_rule(self.rule(count=4))
        self.service._publish(self.client.snapshot())
        item = self.chest()
        self.assertEqual(2, sum(s['key'] == 'Stats.SpecialAbilityDamage' for s in item['groups']['primary']))
        self.assertFalse(item['matches'])


if __name__ == '__main__':
    unittest.main()
