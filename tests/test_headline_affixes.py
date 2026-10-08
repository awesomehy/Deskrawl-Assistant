"""Regressions from real glove, bow and chest modifier lists (IDs anonymized)."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.models import Rule, ValidationError
from deskrawl_assistant.rules import evaluate
from deskrawl_assistant.web_service import AssistantService


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/headline-affix-snapshot.json').read_text(encoding='utf-8'))
SELECTED = ['Stats.Dexterity', 'Stats.CritChance', 'Stats.CritDamage',
            'Stats.AttackSpeed', 'Stats.Damage', 'Stats.CooldownReduction']


class FixtureClient:
    connected = True
    pid = 999

    def __init__(self):
        self.data = copy.deepcopy(FIXTURE)
        self.actions = []

    def snapshot(self):
        return copy.deepcopy(self.data)

    def lock_equipment(self, expected, validate):
        row = next(r for c in self.data['containers'].values() for r in c['slots']
                   if r['instance_id'] == expected['instance_id'])
        if not validate(copy.deepcopy(row)):
            raise ValueError('Fresh rule validation refused')
        row['locked'] = True
        self.actions.append(row['slot_index'])
        return {'status': 'locked'}

    def close(self):
        self.connected = False


class HeadlineAffixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FixtureClient()
        self.service = AssistantService(self.client, Path(self.tmp.name) / 'rules.json')
        self.addCleanup(self.service.close)
        self.service._publish(self.client.snapshot())
        self.log = patch('deskrawl_assistant.web_service.record')
        self.log.start()
        self.addCleanup(self.log.stop)

    def rule(self, key, stats=SELECTED, count=4):
        return {'id': key, 'name': key, 'enabled': True, 'equipment_keys': [key],
                'groups': {'primary': {'selected_stats': stats, 'operator': '>=', 'count': count}},
                'group_mode': 'all'}

    def adapted(self, index):
        return next(i for i in self.service.adapter.adapt_snapshot(self.client.snapshot()).items if i.index == index)

    def test_real_gloves_match_four_random_stats_despite_base_armor(self):
        for index in (189, 194, 240, 241):
            item = self.adapted(index)
            self.assertEqual(('Stats.Armor',), item.base_stats)
            self.assertNotIn('Stats.Armor', item.observation.affixes)
            self.assertTrue(item.observation.groups_complete['primary'])
            key = item.observation.equipment_key
            rule = Rule.from_dict(self.rule(key), self.service.catalog)
            self.assertEqual('match', evaluate(rule, item.observation, self.service.catalog).status)

    def test_rule_batch_locks_four_matching_gloves_and_leaves_bad_glove(self):
        for key in ('LegendaryGloves6', 'LegendaryGloves2', 'LegendaryGloves5'):
            self.service.save_rule(self.rule(key))
        self.service.start('lock_rules', {'scope': 'all'})
        self.service.worker.join(3)
        self.assertFalse(self.service.busy)
        self.assertFalse(self.service.error, self.service.error)
        self.assertEqual([189, 194, 240, 241], self.client.actions)
        # The real glove at 192 has only three selected primary types.
        self.assertFalse(next(r for r in self.client.data['containers']['storage']['slots'] if r['slot_index'] == 192)['locked'])

    def test_weapon_headline_does_not_block_random_affixes(self):
        item = self.adapted(34)
        self.assertEqual({'Stats.WeaponDamage', 'Stats.WeaponSpeed'}, set(item.base_stats))
        stats = ['Stats.DamagePhysical', 'Stats.DamageVsInjured', 'Stats.AttackSpeed', 'Stats.DamageArcane']
        rule = Rule.from_dict(self.rule('LegendaryBow1', stats), self.service.catalog)
        self.assertEqual('match', evaluate(rule, item.observation, self.service.catalog).status)

    def test_base_armor_never_inflates_primary_threshold(self):
        item = self.adapted(205)
        # An old imported config can contain Armor. It must not turn two
        # genuine selected affixes into three, even if it remains on disk.
        rule = Rule.from_dict(self.rule('LegendaryChestArmor2', ['Stats.Armor', 'Stats.MaxHealth', 'Stats.Thorns'], 3), self.service.catalog)
        result = evaluate(rule, item.observation, self.service.catalog)
        self.assertEqual('miss', result.status)
        self.assertIn('2/3', result.reasons[0])
        with self.assertRaises(ValidationError):
            self.service.save_rule(rule.to_dict())

    def test_unverified_headline_does_not_silently_disappear(self):
        row = copy.deepcopy(self.client.data['containers']['storage']['slots'][0])
        row['read_evidence']['native_affixes_verified'] = False
        item = self.service.adapter.adapt_item(row, snapshot_token='unverified', session_id='999')
        self.assertFalse(item.base_stats)
        self.assertIn('Stats.WeaponSpeed', item.observation.affixes)
        self.assertFalse(item.observation.groups_complete['primary'])

    def test_unexpected_armor_on_belt_and_unknown_types_still_block(self):
        row = copy.deepcopy(next(r for r in self.client.data['containers']['storage']['slots'] if r['slot_index'] == 205))
        row.update(name_key='LegendaryBelt1', internal_name='LegendaryBelt1', equip_slot=6)
        row['modifiers'] = [m for m in row['modifiers'] if m['stat_name'] == 'Armor']
        item = self.service.adapter.adapt_item(row, snapshot_token='belt', session_id='999')
        self.assertFalse(item.base_stats)
        self.assertEqual('unknown', item.observation.affixes['Stats.Armor'][0].group)
        self.assertFalse(item.observation.groups_complete['primary'])
        row['modifiers'].append({'stat': 99999, 'value': 1, 'source': 'generated_modifiers', 'type': 0})
        item = self.service.adapter.adapt_item(row, snapshot_token='unknown', session_id='999')
        self.assertFalse(item.observation.groups_complete['primary'])

    def test_state_separates_headline_and_lists_exact_matched_types(self):
        self.service.save_rule(self.rule('LegendaryGloves5'))
        state = self.service.state()
        glove = next(i for i in state['items'] if i['slot_index'] == 241)
        self.assertEqual(['Stats.Armor'], [s['key'] for s in glove['groups']['base']])
        self.assertFalse(glove['groups']['unknown'])
        self.assertTrue(glove['matches'])
        reason = glove['reasons'][0]
        for label in ('暴击伤害', '攻击速度', '冷却缩减', '暴击几率'):
            self.assertIn(label, reason)
        pools = self.service.catalog_payload()['pools']
        self.assertNotIn('Stats.Armor', pools['Chest']['primary'])
        self.assertNotIn('Stats.Armor', pools['Belt']['primary'])


if __name__ == '__main__':
    unittest.main()
