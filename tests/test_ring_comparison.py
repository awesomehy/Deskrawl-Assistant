"""Ring swap regressions use anonymized stat layers, never game processes."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from deskrawl_assistant.character import final_stats, panel_attack, preview
from deskrawl_assistant.managed_mode import DEFAULT_SETTINGS, ManagedMode

DATA = json.loads((Path(__file__).parent/'fixtures/ring-comparison-cycle.json').read_text(encoding='utf-8'))


def state(key):
    value = deepcopy(DATA[key])
    model = value['comparison_model']
    model['base'] = {int(k):v for k,v in model['base'].items()}
    model['caches'] = {int(k):{int(s):v for s,v in row.items()} for k,row in model['caches'].items()}
    for name in ('conditional','status'):
        model[name] = tuple({int(k):v for k,v in row.items()} for row in model[name])
    return value


class RingSwapRegressionTests(unittest.TestCase):
    def test_recorded_baselines_reproduce_both_real_ring_states(self):
        for key,expected in (('wearing_a',81372),('wearing_b',68119)):
            with self.subTest(key=key):
                char=state(key)
                self.assertEqual(expected,panel_attack(final_stats(char['comparison_model']),char['primary']))

    def test_removing_boss_attack_speed_ring_drops_its_effect(self):
        result=preview(DATA['ring_b'],state('wearing_a'),{'items':{}})
        self.assertTrue(result['available'])
        change=next(c for c in result['comparisons'] if c['target']==6)
        self.assertEqual(68119,change['after'])
        self.assertLess(change['delta'],0)

    def test_equipping_boss_attack_speed_ring_adds_its_effect(self):
        result=preview(DATA['ring_a'],state('wearing_b'),{'items':{}})
        change=next(c for c in result['comparisons'] if c['target']==6)
        self.assertEqual(81372,change['after'])
        reverse=preview(DATA['ring_b'],state('wearing_a'),{'items':{}})['comparisons'][0]
        self.assertEqual(change['delta'],-reverse['delta'])

    def test_replacing_other_ring_preserves_existing_boss_bonus(self):
        result=preview(DATA['ring_b'],state('wearing_a'),{'items':{}})
        change=next(c for c in result['comparisons'] if c['target']==7)
        self.assertEqual(73459,change['after'])

    def test_same_ring_compared_to_its_own_slot_has_zero_delta(self):
        change=preview(DATA['ring_a'],state('wearing_a'),{'items':{}})['comparisons'][0]
        self.assertEqual(0,change['delta'])

    def test_bonus_follows_actual_target_when_rings_exchange_positions(self):
        char=state('wearing_a')
        for row in char['equipment']:row['equipped_slot']=13-row['equipped_slot']
        result=preview(DATA['ring_b'],char,{'items':{}})
        self.assertEqual(68119,next(c for c in result['comparisons'] if c['target']==7)['after'])
        self.assertEqual(73459,next(c for c in result['comparisons'] if c['target']==6)['after'])

    def test_managed_swaps_to_better_ring_once_then_stays(self):
        config=deepcopy(DEFAULT_SETTINGS)
        config['auto_equip']['enabled']=True
        config['background'].update(rule_monitoring=False,inventory_automation=False)
        engine=ManagedMode(config)
        def world(key,candidate):
            char=state(key);row=deepcopy(DATA[candidate])
            row['power']=preview(row,char,{'items':{}})
            return {'complete':True,'session':'offline','items':[row],'character':char,
                'scene':{'available':True,'in_town':False},'counts':{'inventory':{'capacity':40,'occupied':1}}}
        engine.start(world('wearing_b','ring_a'))
        action=engine.next_action(world('wearing_b','ring_a'))
        self.assertEqual('equip',action['kind']);self.assertEqual(6,action['target_slot'])
        engine.complete(action['action_id'],result={'verified':True})
        for _ in range(5):self.assertIsNone(engine.next_action(world('wearing_a','ring_b')))
        self.assertTrue(engine.running)
