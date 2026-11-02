"""One native Minion pool, conservative legacy rules, and shared action routes.

All actions go to FakeManagedClient; no game process is opened or modified.
"""
import json
import unittest

from deskrawl_assistant.loot_preview import decorate_equipment
from deskrawl_assistant.models import Rule, ValidationError
from deskrawl_assistant.rules import evaluate, load_rules, primary_perfect_rules, rules_from_dict
import test_managed_service as fixture


KEY = 'LegendaryMinionGear1'
PRIMARY = ['Stats.Strength', 'Stats.CritDamage', 'Stats.AttackSpeed']
SECONDARY = ['Stats.LifeOnHit', 'Stats.LifeOnKill', 'Stats.ManaOnKill']


def group(keys, count=1, operator='>='):
    return {'selected_stats': keys, 'operator': operator, 'count': count}


def rule(keys=SECONDARY, count=3, **extra):
    return {'id': 'minion', 'name': '随机词条组合', 'equipment_keys': [KEY],
            'enabled': True, 'groups': {'random': group(keys, count)}, **extra}


class MinionRandomAffixTests(unittest.TestCase):
    setUp = fixture.ManagedServiceTests.setUp
    settings = fixture.ManagedServiceTests.settings

    def row(self, keys=SECONDARY, index=0, **extra):
        ids = {f'Stats.{name}': number for number, name in self.service.adapter.stat_names.items()}
        records = [{'stat': ids[key], 'type': 0, 'value': 1,
                    'source': 'generated_modifiers'} for key in keys]
        return fixture.equipment(index, **{'key': KEY, 'equip_slot': 12, 'modifiers': records, **extra})

    def observation(self, row):
        return self.service.adapter.adapt_item(row, session_id='999', snapshot_token='offline').observation

    def result(self, raw, row):
        return evaluate(Rule.from_dict(raw, self.service.catalog), self.observation(row), self.service.catalog).status

    def test_all_secondary_types_are_valid_random_affixes(self):
        row = self.row()
        self.assertEqual(self.result(rule(), row), 'match')
        adapted = self.service.adapter.adapt_item(row)
        self.assertEqual([m.group for m in adapted.display_modifiers], ['random'] * 3)
        self.assertTrue(adapted.observation.groups_complete['random'])
        self.assertTrue(all(value.group == 'secondary'
            for values in adapted.observation.affixes.values() for value in values))

    def test_editor_pool_merges_native_pools_after_each_class_filter(self):
        payload = self.service.catalog_payload()
        pool = payload['pools']['Minion']
        self.assertEqual(len(pool['random']), 55)
        self.assertEqual(set(pool['random']), set(pool['primary']) | set(pool['secondary']))
        for value, views in payload['class_pools'].items():
            merged = views['Minion']
            self.assertEqual(merged['random'], merged['primary'] + merged['secondary'])
            self.assertEqual(merged['secondary'], pool['secondary'])
            excluded = self.service.affix_class_filters['primary_exclusion_masks']
            self.assertFalse(any(int(value) & excluded.get(key, 0) for key in merged['primary']))

    def test_mixed_pool_counts_types_once_and_respects_threshold(self):
        keys = PRIMARY[:2] + SECONDARY[:1]
        row = self.row(keys)
        self.assertEqual(self.result(rule(keys), row), 'match')
        self.assertEqual(self.result(rule(keys), self.row(keys[:2] + keys[:1])), 'miss')
        self.assertEqual(self.result(rule(keys, count=2), self.row(keys[:2])), 'match')

    def test_secondary_cannot_substitute_for_primary_in_legacy_and_rule(self):
        old = rule(groups={'primary': group(PRIMARY, 1), 'secondary': group(SECONDARY, 1)})
        for keys, expected in [(PRIMARY, 'miss'), (SECONDARY, 'miss'),
                               (PRIMARY[:2] + SECONDARY[:1], 'match')]:
            with self.subTest(keys=keys):
                self.assertEqual(self.result(old, self.row(keys)), expected)
        # A simple union >=2 would incorrectly match both of the first cases.
        self.assertEqual(self.result(rule(PRIMARY + SECONDARY, 2), self.row(PRIMARY)), 'match')

    def test_primary_only_legacy_is_equivalent_to_single_random_group(self):
        old = rule(groups={'primary': group(PRIMARY, 1, '>')})
        merged = rule(PRIMARY, 2)
        for keys in [PRIMARY, PRIMARY[:1] + SECONDARY[:2], SECONDARY,
                     PRIMARY[:2] + SECONDARY[:1]]:
            self.assertEqual(self.result(old, self.row(keys)), self.result(merged, self.row(keys)))

    def test_unverified_sources_partial_reads_and_unknown_stats_stay_uncertain(self):
        for flag in ['slot_verified', 'snapshot_consistent', 'modifiers_complete',
                     'native_affixes_verified', 'enum_values_verified']:
            with self.subTest(flag=flag):
                row = self.row()
                row['read_evidence'][flag] = False
                self.assertEqual(self.result(rule(), row), 'uncertain')
        row = self.row()
        row['modifiers'].append({'stat': 99999, 'type': 0, 'value': 1, 'source': 'generated_modifiers'})
        self.assertEqual(self.result(rule(), row), 'uncertain')

    def test_fixed_gem_upgrade_and_special_effects_do_not_inflate_matches(self):
        for origin in ['fixed', 'gem', 'upgrade', 'legendary', 'black_mist', 'inherent']:
            with self.subTest(origin=origin):
                row = self.row()
                row['read_evidence']['provenance_verified'] = True
                row['modifiers'][2]['source'] = origin
                self.assertEqual(self.result(rule(), row), 'miss')

    def test_random_rule_does_not_match_character_equipment(self):
        row = self.row(equip_slot=6)
        raw = rule(equipment_keys=['LegendaryBelt3'])
        row.update(name_key='LegendaryBelt3', internal_name='LegendaryBelt3')
        self.assertEqual(self.result(raw, row), 'uncertain')
        with self.assertRaisesRegex(ValidationError, '只有仆从装备'):
            self.service.save_rule(raw)

    def test_perfect_means_all_three_random_affixes_in_one_matching_rule(self):
        obs = self.observation(self.row())
        good = Rule.from_dict(rule(), self.service.catalog)
        self.assertEqual(primary_perfect_rules(obs, [good], self.service.catalog), [good])
        for raw in [rule(SECONDARY[:2], 2), rule(enabled=False),
                    rule(groups={'primary': group(PRIMARY, 1), 'secondary': group(SECONDARY, 1)})]:
            self.assertFalse(primary_perfect_rules(obs, [Rule.from_dict(raw, self.service.catalog)], self.service.catalog))
        self.assertFalse(primary_perfect_rules(self.observation(self.row(SECONDARY[:2])), [good], self.service.catalog))

    def test_v2_v3_mixed_rules_roundtrip_reload_import_copy_without_weakening(self):
        old = rule(id='old', groups={'primary': group(PRIMARY, 2), 'secondary': group(SECONDARY, 1)})
        self.service.save_rule(old)
        self.service.save_rule(rule())
        before = [r.to_dict() for r in self.service.rules]
        self.assertEqual([r.to_dict() for r in load_rules(self.service.config, self.service.catalog)], before)
        exported = self.service.export_rules()
        self.assertIsInstance(exported, list)
        self.assertEqual([r.to_dict() for r in rules_from_dict(exported, self.service.catalog)], before)
        self.assertEqual(json.loads(self.service.config.read_text(encoding='utf-8')), before)
        self.service.copy_rules(KEY, 'RareMinionGear1')
        self.assertEqual([r.groups for r in self.service.rules[:2]], [r.groups for r in self.service.rules[2:]])
        self.service.import_rules(exported, True)
        self.assertEqual([r.groups for r in self.service.rules[:2]], [r.groups for r in self.service.rules[-2:]])
        self.assertEqual(self.result(old, self.row(SECONDARY)), 'miss')
        self.assertFalse(self.client.actions)

    def test_v3_schema_roundtrip_and_invalid_mixed_groups_are_rejected(self):
        self.service.save_rule(rule())
        self.assertEqual(self.service.export_rules()['version'], 3)
        self.assertEqual(load_rules(self.service.config, self.service.catalog)[0].groups,
                         self.service.rules[0].groups)
        for raw in [rule(PRIMARY + SECONDARY, 4),
                    rule(groups={'random': group(SECONDARY), 'primary': group(PRIMARY)})]:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                self.service.save_rule(raw)

    def test_inventory_and_managed_payloads_share_random_match_and_perfect_state(self):
        self.client.items = [self.row()]
        self.service.save_rule(rule())
        self.service._publish(self.client.snapshot())
        item = self.service.state()['items'][0]
        self.assertEqual((item['matches'], item['review'], item['primary_perfect']), (True, False, True))
        self.assertEqual([m['key'] for m in item['groups']['random'] if m['matched']], SECONDARY)
        self.assertEqual(item['groups']['primary'], [])
        self.assertEqual(item['groups']['secondary'], [])
        managed = self.service._managed_observation()['items'][0]
        self.assertEqual((managed['matches'], managed['rule_status']), (True, 'match'))

    def test_manual_rule_lock_and_new_loot_monitor_use_single_random_pool(self):
        self.service.save_rule(rule())
        self.client.items = [self.row()]
        snapshot = self.client.snapshot()
        candidates = self.service._rule_candidates(snapshot, self.client.items)
        self.assertEqual(self.service._apply(candidates, by_rules=True), 1)
        self.client.actions.clear()
        self.service._publish(self.client.snapshot())
        self.service.set_monitoring(True)
        self.service._monitor_once()
        self.assertFalse(self.client.actions)
        self.client.items.append(self.row(index=1))
        self.service._monitor_once()
        self.service._monitor_once()
        self.assertEqual(self.client.actions, [('lock', 'offline-inventory-1')])

    def test_managed_locks_and_stores_random_match_but_sells_miss(self):
        self.service.save_rule(rule())
        self.client.items = [self.row(), self.row(PRIMARY, index=1)]
        self.service._publish(self.client.snapshot())
        cfg = self.settings(farming=True)
        cfg['cleanup'].update(minion_equipment_policies={'legendary': 'sell', 'rare': 'salvage'},
                              minion_equipment_pages=[5])
        self.service.save_managed(cfg)
        self.service.set_managed_running(True)
        def native(kind, payload):
            self.assertEqual(kind, 'sell')
            self.client.items = [r for r in self.client.items if r['item_uid'] != payload['item']['item_uid']]
            return {'effect_confirmed': True, 'quantity': 1}
        self.client.native_handler = native
        for _ in range(3):
            world = self.service._managed_observation()
            action = self.service.managed.next_action(world)
            self.assertIsNotNone(action)
            result = self.service._managed_execute(action, world)
            self.assertTrue(result['verified'])
            self.service.managed.complete(action['action_id'], result=result)
        self.assertEqual([a[0] for a in self.client.actions], ['lock', 'transfer', 'sell'])
        self.assertEqual(len(self.client.items), 1)
        self.assertEqual((self.client.items[0]['container'], self.client.items[0]['locked']), ('storage', True))
        self.assertEqual(self.client.items[0]['slot_index'], 200)

    def test_loot_preview_uses_random_pool_without_exporting_action_identity(self):
        row = self.row()
        item = {'kind': 'equipment', 'slot': 'Minion', 'item_key': KEY,
                'modifiers': [{'stat_id': m['stat'], 'type_id': m['type'], 'value': m['value']}
                              for m in row['modifiers']]}
        decorate_equipment(item, [Rule.from_dict(rule(), self.service.catalog)],
                           self.service.adapter, self.service.catalog)
        self.assertTrue(item['matches'])
        self.assertTrue(item['primary_perfect'])
        self.assertEqual(len(item['groups']['random']), 3)
        self.assertNotIn('selection_id', item)
        self.assertNotIn('instance_id', item)


if __name__ == '__main__':
    unittest.main()
