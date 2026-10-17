"""Offline policy and accounting tests: never invoke a live game."""
from copy import deepcopy
import unittest

from deskrawl_assistant.synthesis import next_action, validate_settings as validate_synthesis, DEFAULT_SYNTHESIS
from deskrawl_assistant.managed_mode import ManagedMode, ManagedMetrics, validate_settings
from deskrawl_assistant.models import ValidationError
from tests.test_managed_mode import loot, equipment, observation, settings


def gem(count=10, slot=0, grade=1, container='inventory', **extra):
    row = loot(f'{container}-{slot}', 'gem', f'GemRuby{grade}', slot=slot, container=container,
        count=count, item_class='GemData', item_type=4, slot_locked=False)
    row.update(extra)
    return row


def rune(number, quality='uncommon', container='inventory', key=None, **extra):
    prefix = {'uncommon':'UncommonRune_', 'rare':'RareRune_', 'grand':'LegendaryRune_', 'set':'WarriorSetRune1'}[quality]
    row = loot(f'rune{number}', 'rune', key or prefix+'Test', slot=number, container=container,
        item_uid=f'uid{number}', item_class='RuneData', slot_locked=False,
        rarity={'uncommon':1,'rare':2,'grand':3,'set':5}[quality])
    row.update(extra)
    return row


def config(gems=True, runes=False, farm=False, **cleanup):
    result = settings(farm=farm, **cleanup)
    result['synthesis']['gems']['enabled'] = gems
    result['synthesis']['runes']['enabled'] = runes
    result['synthesis']['runes']['max_quality'] = 'set'
    return result


def world(rows=(), **kwargs):
    result = observation(rows, town=True, **kwargs)
    result['character_id'] = '0:0:offline-character'
    result['synthesis'] = {
        'gems':{'available':True, 'gold':1000000, 'recipes':[
            {'recipe_id':str(n), 'input_key':f'GemRuby{n}', 'output_key':f'GemRuby{n+1}',
             'input_quality':n, 'output_quality':n+1, 'input_count':10, 'output_count':1,
             'gold_cost':300, 'output_max_stack':999}
            for n in range(1,6)]},
        'runes':{'available':True, 'unlocked':True, 'sequence':1, 'recipes':[
            {'recipe_id':a, 'input_quality':a, 'output_quality':b, 'input_count':9, 'output_count':1, 'chance':p}
            for a,b,p in [('uncommon','rare',1),('rare','grand',.15),('grand','set',.5)]]}}
    return result


def drop_count(metrics):
    state=metrics.state()
    return sum(row['count'] for row in state['equipment'].values()) + sum(
        row['count'] for group in ('runes','gems','others') for row in state[group])


class PolicyTests(unittest.TestCase):
    def test_defaults_off_and_config_validation(self):
        self.assertFalse(any(v['enabled'] for v in DEFAULT_SYNTHESIS.values()))
        legacy = settings(); legacy.pop('synthesis')
        self.assertEqual(validate_settings(legacy)['synthesis'], DEFAULT_SYNTHESIS)
        for group, key, value in [('gems','max_quality',7),('gems','max_quality',True),
                                 ('runes','max_quality','normal'),('runes','excluded_keys',['a','a'])]:
            bad = deepcopy(DEFAULT_SYNTHESIS); bad[group][key] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):validate_synthesis(bad)

    def test_maximum_only_as_output_low_to_high_and_gold_batch_limit(self):
        cfg = config(); cfg['synthesis']['gems']['max_quality'] = 5
        state = world([gem(30, grade=4),gem(50,slot=1,grade=5),gem(20,slot=2,grade=1)])
        state['synthesis']['gems']['gold'] = 650
        action = next_action(cfg,state)
        self.assertEqual((action['recipe_id'],action['count']),('1',2))
        self.assertEqual(sum(r['quantity'] for r in action['inputs']),20)
        self.assertEqual(next_action(cfg,world([gem(100,grade=5)])),None)

    def test_insufficient_gold_or_unavailable_group_does_not_block_other_group(self):
        state = world([gem(100)]+[rune(n+1) for n in range(9)])
        state['synthesis']['gems']['gold'] = 299
        self.assertEqual(next_action(config(runes=True),state)['kind'],'synthesize_runes')
        state['synthesis']['gems'] = {'available':False, 'reason':'offline'}
        self.assertEqual(next_action(config(runes=True),state)['kind'],'synthesize_runes')

    def test_gem_locked_or_unknown_bag_stack_skips_recipe(self):
        for lock in (True,None):
            rows = [gem(10),gem(1,slot=1),gem(10,slot=2,grade=2)]
            rows[1]['locked'] = lock
            with self.subTest(lock=lock):
                self.assertEqual(next_action(config(),world(rows))['recipe_id'],'2')
        rows = [gem(10),gem(10,container='storage',slot=50)]
        rows[1]['locked']=True
        self.assertEqual(next_action(config(),world(rows))['count'],1)

    def test_cross_page_cross_stack_staging_uses_only_unlocked_pages(self):
        state = world([gem(3),gem(2,container='storage',slot=50),gem(5,container='storage',slot=100),
                       gem(100,container='storage',slot=200)])
        action = next_action(config(),state)
        self.assertEqual((action['kind'],action['item']['slot_index']),('transfer',50))
        self.assertEqual(action['reason_code'],'synthesis_prepare')
        state['storage']['allowed_indices'] = list(range(50))
        self.assertIsNone(next_action(config(),state))

    def test_full_bag_and_potions_leave_no_staging_loop(self):
        rows = [gem(9)]+[loot(f'p{n}','material','MovementPotion1',slot=n+1,item_class='ItemData',item_type=2) for n in range(2)]
        rows += [gem(1,container='storage',slot=50)]
        self.assertIsNone(next_action(config(),world(rows,capacity=3)))
        # Two source stacks and one output slot are reserved before withdrawing.
        self.assertIsNone(next_action(config(),world([gem(5,container='storage',slot=50),
             gem(5,container='storage',slot=51)],capacity=2)))

    def test_native_reverse_stack_consumption_is_preserved(self):
        rows = [gem(7),gem(7,slot=1),gem(7,slot=2)]
        action = next_action(config(),world(rows))
        self.assertEqual([(r['slot_index'],r['quantity']) for r in action['inputs']],[(2,7),(1,7),(0,6)])

    def test_runes_allow_experience_and_probability_but_respect_cap(self):
        cfg=config(gems=False,runes=True)
        rows=[rune(n,'rare',rune_level=20,rune_xp=999) for n in range(9)]
        cfg['synthesis']['runes']['max_quality']='grand'
        action=next_action(cfg,world(rows))
        self.assertEqual((action['kind'],len(action['input_uids'])),('synthesize_runes',9))
        self.assertEqual(action['recipe']['chance'],.15)
        cfg['synthesis']['runes']['max_quality']='rare'
        self.assertIsNone(next_action(cfg,world(rows)))
        self.assertIsNone(next_action(config(gems=False,runes=True),world([rune(n,'set') for n in range(9)])))

    def test_exclusion_applies_to_all_levels_pages_and_duplicate_uids(self):
        cfg=config(gems=False,runes=True)
        rows=[rune(n) for n in range(9)]
        cfg['synthesis']['runes']['excluded_keys']=[rows[0]['internal_name']]
        self.assertIsNone(next_action(cfg,world(rows)))
        rows[0]['container']='storage'; rows[0]['slot_index']=50
        self.assertIsNone(next_action(cfg,world(rows)))
        cfg['synthesis']['runes']['excluded_keys']=[]
        rows[0]['item_uid']=rows[1]['item_uid']
        self.assertIsNone(next_action(cfg,world(rows)))

    def test_locked_equipped_and_non_runes_cannot_fill_nine(self):
        for field,value in [('locked',True),('locked',None),('is_equipped',True),('issues',['stale'])]:
            rows=[rune(n) for n in range(9)];rows[0][field]=value
            with self.subTest(field=field,value=value):self.assertIsNone(next_action(config(gems=False,runes=True),world(rows)))
        rows=[rune(n) for n in range(8)]+[equipment('impostor',slot=20,rarity=1)]
        self.assertIsNone(next_action(config(gems=False,runes=True),world(rows)))

    def test_unlocked_recipes_and_town_only(self):
        state=world([rune(n,'grand') for n in range(9)])
        state['synthesis']['runes']['recipes'][2]['available']=False
        self.assertIsNone(next_action(config(gems=False,runes=True),state))
        state=world([gem()]);state['scene']['in_town']=False
        self.assertIsNone(next_action(config(),state))
        state['scene']['in_town']=True;state['scene']['online_saving']=True
        self.assertIsNone(next_action(config(),state))


class SchedulerTests(unittest.TestCase):
    def test_craft_before_full_home_pause_and_synthesis_only_start(self):
        rows=[gem()]+[gem(1,slot=n,grade=6,container='storage',locked=True) for n in range(50)]
        engine=ManagedMode(config());state=world(rows);engine.start(state)
        self.assertEqual(engine.next_action(state)['kind'],'synthesize_gems')

    def test_equipment_protection_then_junk_cleanup_then_craft(self):
        rows=[gem(),equipment('keep',slot=1,matches=True),equipment('junk',slot=2)]
        engine=ManagedMode(config(equipment_action='sell'));state=world(rows);engine.start(state)
        action=engine.next_action(state);self.assertEqual(action['kind'],'lock')
        engine.complete(action['action_id'],result={'verified':True});rows[1]['locked']=True
        action=engine.next_action(world(rows));self.assertEqual((action['kind'],action['item']['item_uid']),('transfer','keep'))
        engine.complete(action['action_id'],result={'verified':True});rows[1].update(container='storage',slot_index=10)
        action=engine.next_action(world(rows));self.assertEqual((action['kind'],action['item']['item_uid']),('sell','junk'))
        engine.complete(action['action_id'],result={'verified':True});rows.pop()
        self.assertEqual(engine.next_action(world(rows))['kind'],'synthesize_gems')

    def test_finish_never_starts_new_craft_and_stores_locked_materials(self):
        rows=[gem(10,locked=True)]
        engine=ManagedMode(config());state=world(rows);engine.start(state);engine.finishing=True
        action=engine.next_action(state)
        self.assertEqual((action['kind'],action['target']),('transfer','storage'))

    def test_unverified_receipt_never_retries_consumption(self):
        engine=ManagedMode(config());state=world([gem()]);engine.start(state)
        action=engine.next_action(state)
        engine.complete(action['action_id'],result={'verified':True,'effect_confirmed':False})
        self.assertFalse(engine.running)
        self.assertIsNone(engine.next_action(state))

    def test_confirmed_receipt_waits_for_effect_and_then_plans_next_tier(self):
        engine=ManagedMode(config());state=world([gem(100)]);engine.start(state)
        action=engine.next_action(state)
        output=dict(gem(10,grade=2,slot=1),quantity=10)
        engine.complete(action['action_id'],result={'verified':True,'effect_confirmed':True,
            'inputs':action['inputs'],'outputs':[output],'receipt_id':'gem-confirmed'})
        self.assertIsNone(engine.next_action(state));self.assertTrue(engine.running)
        self.assertEqual(engine.next_action(world([gem(10,grade=2,slot=1)]))['recipe_id'],'2')
        self.assertEqual(drop_count(engine.metrics),0)

    def test_legacy_actions_migrate_to_store_without_losing_pages_or_other_settings(self):
        original=settings(rune_action='sell',gem_action='sell',rune_pages=[3],gem_pages=[2],
            gold_bar_action='keep',legendary_rein_action='keep',gold_bar_pages=[4])
        result=validate_settings(original)
        for group in ('rune','gem','gold_bar','legendary_rein'):
            self.assertEqual(result['cleanup'][group+'_action'],'store')
            self.assertEqual(result['cleanup'][group+'_pages'],original['cleanup'][group+'_pages'])
        self.assertEqual(result['auto_equip'],original['auto_equip'])


class MetricsTests(unittest.TestCase):
    def test_gem_outputs_and_merged_stacks_are_not_loot_true_drops_still_count(self):
        metrics=ManagedMetrics();input_row=gem(20);existing=gem(2,grade=2,slot=1)
        metrics.observe(world([input_row,existing]))
        metrics.synthesized('receipt',[dict(input_row,quantity=20)],[dict(existing,quantity=2)])
        metrics.synthesized('receipt',[dict(input_row,quantity=20)],[dict(existing,quantity=2)])
        metrics.observe(world([gem(4,grade=2,slot=1)]))
        self.assertEqual(drop_count(metrics),0)
        metrics.observe(world([gem(5,grade=2,slot=1)]))
        self.assertEqual(drop_count(metrics),1)

    def test_probability_failure_output_uid_is_not_new_loot_or_storage_outcome(self):
        metrics=ManagedMetrics();inputs=[rune(n,'rare') for n in range(9)]
        metrics.observe(world(inputs));output=rune(10,'rare')
        metrics.synthesized('failure',[dict(r,quantity=1) for r in inputs],[dict(output,quantity=1)])
        metrics.observe(world([output]));metrics.committed('store',output,'transfer')
        self.assertEqual(drop_count(metrics),0)
        fresh=rune(11,'grand');metrics.observe(world([output,fresh]))
        self.assertEqual(drop_count(metrics),1)

    def test_upgrade_stone_is_other_and_potions_remain_omitted(self):
        metrics=ManagedMetrics();metrics.observe(world())
        stone=loot('stone','material','UpgradeCommodity1',count=2,item_class='ItemData',item_type=7)
        potion=loot('potion','material','MovementPotion1',slot=1,count=14,item_class='ItemData',item_type=2)
        metrics.observe(world([stone,potion]))
        payload=metrics.state();self.assertEqual(drop_count(metrics),2)
        self.assertEqual(len(payload['others']),1)
        self.assertEqual(payload['others'][0]['count'],2)


if __name__=='__main__':unittest.main()
