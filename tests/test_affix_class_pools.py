"""Legendary candidates mirror 1.0.3's native primary-only class filter."""
import json
from pathlib import Path
import tempfile
import unittest

from deskrawl_assistant.affix_pools import editor_pools
from deskrawl_assistant.web_service import AssistantService, BASE
from test_web_service import FakeClient


class AffixClassPoolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads((BASE / 'data/affix-groups-static.json').read_text(encoding='utf-8'))
        cls.filters = json.loads((BASE / 'data/affix-class-filters.json').read_text(encoding='utf-8'))
        cls.broad, cls.by_class = editor_pools(cls.raw['slotPools'], cls.filters)

    def test_profile_matches_installed_pool_metadata_and_native_class_flags(self):
        self.assertEqual(self.raw['metadataSha256'], self.filters['source']['metadataSha256'])
        ui = json.loads((BASE / 'data/equipment-ui.json').read_text(encoding='utf-8'))
        self.assertEqual({key: ui['enums']['HeroClass'][key] for key in self.filters['classes']},
                         self.filters['classes'])
        self.assertEqual({'1', '2', '4', '8'}, set(self.by_class))

    def test_native_weapon_primary_counts_and_class_damage_types(self):
        expected = {
            '1': (17, {'Strength', 'DamagePhysical', 'DamageFire', 'DamageVsBleeding', 'DamageOverTime'}),
            '2': (19, {'Intelligence', 'DamageFire', 'DamageCold', 'DamageLightning', 'DamageVsDistant', 'DamageVsBurned', 'DamageOverTime'}),
            '4': (19, {'Dexterity', 'DamagePhysical', 'DamagePoison', 'DamageArcane', 'DamageVsDistant', 'DamageVsPoisoned', 'DamageOverTime'}),
            '8': (15, {'Dexterity', 'DamagePhysical', 'DamageLightning'}),
        }
        filtered = set(self.filters['primary_exclusion_masks'])
        for value, (count, specific) in expected.items():
            pool = set(self.by_class[value]['Weapon']['primary'])
            self.assertEqual(count, len(pool), value)
            self.assertEqual({'Stats.' + key for key in specific}, pool & filtered, value)

    def test_every_slot_has_complete_nonmutating_union_and_unchanged_secondary(self):
        original = json.loads(json.dumps(self.raw))
        for slot, pool in self.broad.items():
            union = set()
            for class_pool in self.by_class.values():
                self.assertEqual(pool['secondary'], class_pool[slot]['secondary'])
                self.assertLessEqual(set(class_pool[slot]['primary']), set(pool['primary']))
                union.update(class_pool[slot]['primary'])
            self.assertEqual(set(pool['primary']), union, slot)
        self.assertEqual(original, self.raw)

    def test_shoulders_keep_cooldown_but_no_divine_fixed_stats_are_added(self):
        for pool in self.by_class.values():
            self.assertIn('Stats.CooldownReduction', pool['Shoulder']['primary'])
            self.assertNotIn('Stats.MaxHealth', pool['Weapon']['primary'])
            self.assertNotIn('Stats.CooldownReduction', pool['Ring']['primary'])
            self.assertNotIn('Stats.ManaCostReduction', pool['Ring']['primary'])

    def test_headline_attributes_stay_out_of_class_candidates(self):
        for pool in self.by_class.values():
            for slot, groups in pool.items():
                for stats in groups.values():
                    self.assertNotIn('Stats.WeaponDamage', stats)
                    self.assertNotIn('Stats.WeaponSpeed', stats)
                    if slot in ('Helm', 'Chest', 'Pants', 'Boots', 'Gloves', 'Shoulder'):
                        self.assertNotIn('Stats.Armor', stats)

    def test_catalog_exposes_offline_class_views_without_migrating_existing_rules(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        config = Path(directory) / 'rules.json'
        service = AssistantService(FakeClient([]), config)
        self.addCleanup(service.close)
        # A preexisting cross-class rule remains valid, byte-identical on
        # opening the catalog, and retains both selections on saving.
        rule = {'id': 'mixed', 'name': '原组合', 'equipment_keys': ['LegendaryRing2'], 'enabled': True,
                'groups': {'primary': {'selected_stats': ['Stats.Strength', 'Stats.Intelligence'],
                                       'operator': '>=', 'count': 1}}, 'group_mode': 'all'}
        saved = service.save_rule(rule)
        before = config.read_bytes()
        payload = service.catalog_payload()
        self.assertEqual(self.broad, payload['pools'])
        self.assertEqual(self.by_class, payload['class_pools'])
        self.assertEqual(before, config.read_bytes())
        self.assertEqual(saved, service.save_rule(saved))
        self.assertEqual(before, config.read_bytes())
        self.assertEqual([], service.client.actions)


if __name__ == '__main__':
    unittest.main()
