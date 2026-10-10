"""Counterfactual gear regression tests; no game methods or writes."""
from copy import deepcopy
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.character import (final_stats, panel_attack, item_bonuses, preview)
from deskrawl_assistant.character_reader import read_character
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.web_service import AssistantService
from test_headline_affixes import FixtureClient


def item(slot=3, mods=(), **extra):
    return {'is_equipment':True,'class_mask':15,'equip_slot':slot,'modifiers_complete':True,
        'modifiers':[{'stat':stat,'type':0,'value':value} for stat,value in mods],
        'upgrade_level':0,'rarity':3,'socket_count':0,'socketed_gems':[],**extra}


def character(primary=1, equipment=(), **extra):
    c = {'available':True,'class_mask':4,'primary':primary,'equipment':list(equipment),
        'model':{'base':{0:10,1:100,2:20,3:10.5,4:.2,5:1.5},
            'caches':{536:{20:100,21:1},544:{}},'status':({}, {}, {}),'conditional':({}, {}),'conversions':[]},**extra}
    c['comparison_model'] = c['model']
    return c


class CharacterMathTests(unittest.TestCase):
    def test_panel_uses_role_attribute_and_extra_crit_damage(self):
        c=character();s=final_stats(c['model'])
        # Weapon damage truncates, fixed damage remains double. Crit extra=150%.
        self.assertEqual(int(110.5*2*(1+.2*1.5)),panel_attack(s,1))
        self.assertEqual(int(110.5*1.1*(1+.2*1.5)),panel_attack(s,0))
        self.assertEqual(int(110.5*1.2*(1+.2*1.5)),panel_attack(s,2))

    def test_three_native_weight_groups_multiply_not_add(self):
        c=character();c['model']['caches'][536].update({65:1,23:1,58:1,33:1})
        s=final_stats(c['model'])
        self.assertAlmostEqual(panel_attack(s,1),int(110.5*2*1.3*2.4*1.5*1.5))

    def test_layers_recompute_primary_conversion_and_percent(self):
        c=character();c['model']['caches'][544]={1:.2};c['model']['conversions']=[(1,3,.5)]
        old=item(mods=[(1,20)],equipped_slot=2)
        c['equipment']=[old];c['model']['caches'][536][1]=20
        result=preview(item(mods=[(1,50)]),c,{'items':{}})
        delta=next(a for a in result['attributes'] if a['stat']==1)
        self.assertEqual(144,delta['before']);self.assertEqual(180,delta['after'])
        self.assertEqual(18,next(a for a in result['attributes'] if a['stat']==3)['after']-next(a for a in result['attributes'] if a['stat']==3)['before'])
        self.assertGreater(result['delta'],0)

    def test_caps_are_reapplied_after_swap(self):
        c=character();c['model']['caches'][536][4]=.7
        result=preview(item(mods=[(4,.2)]),c,{'items':{}})
        self.assertEqual(0,result['delta'])
        self.assertFalse(any(a['stat']==4 for a in result['attributes']))

    def test_two_ring_targets_have_distinct_deltas_and_select_best(self):
        c=character(equipment=[item(7,[(1,50)],equipped_slot=6,item_uid='r1'),item(7,[(1,10)],equipped_slot=7,item_uid='r2')])
        c['model']['caches'][536][1]=60
        result=preview(item(7,[(1,30)]),c,{'items':{}})
        self.assertEqual([6,7],[i['target'] for i in result['comparisons']])
        self.assertLess(result['comparisons'][0]['delta'],0)
        self.assertGreater(result['comparisons'][1]['delta'],0)
        self.assertEqual(7,result['target'])

    def test_empty_equipment_slot_and_zero_baseline_are_valid(self):
        c=character();c['model']['base'][3]=0;c['model']['caches'][536][20]=0
        result=preview(item(1,[(20,10),(21,1)]),c,{'items':{}})
        self.assertEqual(0,result['before']);self.assertGreater(result['delta'],0);self.assertIsNone(result['percent'])

    def test_preview_does_not_mutate_live_model(self):
        c=character();original=deepcopy(c)
        self.assertTrue(preview(item(mods=[(1,100)]),c,{'items':{}})['available'])
        self.assertEqual(original,c)

    def test_live_buffs_health_and_distance_do_not_change_fixed_preview(self):
        c=character();c['comparison_model']=deepcopy(c['model'])
        row=item(mods=[(1,50)])
        expected=preview(row,c,{'items':{}})
        # Battle-only changes are still displayed as current attributes, but
        # must not leak into the comparison model or reorder the inventory.
        c['model']['status']=({1:500,6:2},{3:4},{65:5})
        c['model']['conditional']=({65:8},{6:10})
        c.update(health=1,health_max=100,distance=2,stats=final_stats(c['model']))
        self.assertEqual(expected,preview(row,c,{'items':{}}))

    def test_permanent_changes_recalculate_instead_of_freezing_a_sample(self):
        c=character();row=item(mods=[(1,20)])
        before=preview(row,c,{'items':{}})
        c['comparison_model']['caches'][568]={1:50}
        after=preview(row,c,{'items':{}})
        self.assertGreater(after['before'],before['before'])
        self.assertGreater(after['after'],before['after'])

    def test_upgrade_distributes_before_headline_exclusion(self):
        row=item(9,[(8,100),(1,20),(4,.1),(5,1)],upgrade_level=5)
        bonuses=item_bonuses(row,{'items':{}})
        self.assertAlmostEqual(115,bonuses[0][8],places=4)
        self.assertAlmostEqual(23,bonuses[0][1],places=4)
        self.assertAlmostEqual(.11,bonuses[0][4],places=4)

    def test_gem_effects_depend_on_slot_and_do_not_receive_gear_upgrade(self):
        gem={'items':{'gem':{'effect_groups':[{'values':[{'stat':1,'modifier':0,'value':20}]},
            {'values':[{'stat':51,'modifier':0,'value':.1}]},{'values':[{'stat':23,'modifier':0,'value':.35}]}]}}}
        for slot,stat,value in ((1,23,.35),(7,51,.1),(8,51,.1),(3,1,20)):
            row=item(slot,[],socket_count=2,socketed_gems=['gem','gem'],upgrade_level=10)
            self.assertAlmostEqual(value*2,item_bonuses(row,gem)[0][stat])

    def test_remove_old_socketed_gem_before_adding_new_gear(self):
        ui={'items':{'gem':{'effect_groups':[{'values':[{'stat':1,'modifier':0,'value':20}]},{'values':[]},{'values':[]}]}}}
        old=item(mods=[(1,10)],equipped_slot=2,socket_count=1,socketed_gems=['gem'])
        c=character(equipment=[old]);c['model']['caches'][536][1]=30
        result=preview(item(mods=[(1,25)]),c,ui)
        self.assertLess(result['delta'],0)
        change=next(a for a in result['attributes'] if a['stat']==1)
        self.assertEqual(-5,change['after']-change['before'])

    def test_conditional_and_status_bonuses_remain_in_both_sides(self):
        c=character();c['model']['conditional']=({65:.75},{3:.2});c['model']['status']=({6:.3},{},{3:2})
        result=preview(item(mods=[(1,10)]),c,{'items':{}})
        self.assertTrue(result['estimated']);self.assertGreater(result['delta'],0)
        self.assertEqual(panel_attack(final_stats(c['model']),1),result['before'])

    def test_invalid_or_unknown_data_never_becomes_zero_gain(self):
        for row in (item(mods=[(1,math.nan)]),item(modifiers_complete=False),item(socket_count=1,socketed_gems=None),
                    item(socket_count=0,socketed_gems=['unknown']),item(class_mask=1),item(modifiers=[{'stat':1,'type':2,'value':1}])):
            result=preview(row,character(),{'items':{}})
            self.assertFalse(result['available']);self.assertNotIn('delta',result)

    def test_conversion_cycle_fails_closed(self):
        c=character();c['model']['conversions']=[(1,3,1),(3,1,1)]
        self.assertFalse(preview(item(mods=[]),c,{'items':{}})['available'])


class CharacterServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=FixtureClient();self.client.character_snapshot=Mock()
        self.c=character(name='测试猎人',level=70,session=self.client.data['bridge_session'],captured_at='2026-10-10T00:00:00Z',
            stats=final_stats(character()['model']),health=100,health_max=100,attack=287,warnings=[],character_id='test')
        self.client.character_snapshot.return_value=self.c
        self.service=AssistantService(self.client,Path(self.tmp.name)/'rules.json');self.addCleanup(self.service.close)

    def test_gear_tags_socket_gem_effects_and_upgrade_reach_browser(self):
        row=next(r for r in self.client.data['containers']['storage']['slots'] if r.get('is_equipment'))
        row.update(is_ancient=True,is_black_mist=True,rarity=4,socket_count=1,socketed_gems=['GemTopaz4'],upgrade_level=7)
        self.service._publish(self.client.snapshot());out=self.service.state()
        found=next(i for i in out['items'] if i['instance_id']==row['instance_id'])
        self.assertEqual(['远古','黑雾','神圣'],found['tags']);self.assertEqual(7,found['upgrade'])
        self.assertEqual(1,found['occupied_sockets']);self.assertTrue(found['gems'][0]['effects']['values'])
        self.assertEqual('测试猎人',out['character']['name']);self.assertNotIn('model',out['character'])
        self.assertFalse(self.client.actions)

    def test_stale_character_is_cleared_on_disconnect_failure_or_new_session(self):
        self.service._publish(self.client.snapshot());self.assertTrue(self.service.state()['character']['available'])
        self.client.character_snapshot.side_effect=MemoryReadError('角色正在变化')
        self.service._publish(self.client.snapshot());self.assertFalse(self.service.state()['character']['available'])
        self.client.character_snapshot.side_effect=None;self.client.character_snapshot.return_value={**self.c,'session':'other'}
        self.service._publish(self.client.snapshot());self.assertFalse(self.service.state()['character']['available'])
        self.service._publish(None);self.assertEqual([],self.service.state()['character']['equipment'])

    def test_worn_equipment_never_enters_lock_or_transfer_selection(self):
        row=deepcopy(next(r for r in self.client.data['containers']['storage']['slots'] if r.get('is_equipment')))
        row.update(equipped_slot=0,container='equipped',socket_count=0,socketed_gems=[])
        self.c['equipment']=[row]
        self.service._publish(self.client.snapshot());out=self.service.state()
        worn=out['character']['equipment'][0]
        self.assertEqual('equipped',worn['container']);self.assertFalse(worn['movable'])
        self.assertFalse(any(i['selection_id']==worn['selection_id'] for i in out['items']))
        self.assertTrue(any(worn['groups'].values()))
        self.assertFalse(self.client.actions)

    def test_reader_retries_torn_reads_but_discards_last_result_on_failure(self):
        with patch('deskrawl_assistant.character_reader._snapshot',side_effect=[SnapshotChangedError('变动'),{'available':True}]) as read:
            self.assertTrue(read_character(Mock())['available']);self.assertEqual(2,read.call_count)
        with patch('deskrawl_assistant.character_reader._snapshot',side_effect=MemoryReadError('字段变化')):
            self.assertFalse(read_character(Mock())['available'])


if __name__=='__main__':unittest.main()
