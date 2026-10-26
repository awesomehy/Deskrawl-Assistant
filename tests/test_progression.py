"""Offline policy, resource and native-boundary checks. Never open the game."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.progression import catalog, growth_action, next_purchase, NEW_ACTIONS, validate_growth, merchant_prices
from deskrawl_assistant.managed_mode import DEFAULT_SETTINGS, ManagedMode, validate_settings
from deskrawl_assistant.managed_service import required_capabilities
from deskrawl_assistant.game_actions import GameActions
from deskrawl_assistant.native_dispatcher import NativeActionError, load_profile
from deskrawl_assistant.models import ValidationError
from test_managed_mode import observation, equipment, loot, settings as managed_settings, verified


def settings(*args, **kwargs):
    cfg = managed_settings(*args, **kwargs)
    # These scenarios model an explicit saved plan that buys every slot.
    cfg['merchant']['weights'] = {str(n): 1 for n in range(1, 11)}
    return cfg


def state(mask=4, cap=2000, shards=1900, gold=1000000):
    hero = catalog()['heroes'][str(mask)]
    return {'available':True, 'class_mask':mask, 'level':70, 'gold':gold,
        'talents':{'ranks':{},'unspent':50}, 'life_skills':{'ranks':{}},
        'skills':{'equipped':[None]*4, 'available':[s['id'] for s in hero['abilities']]},
        'soul_shards':{'current':shards,'maximum':cap},
        'merchant':{'available':True,'open':True,'unlock_known':True,'unlocked':True,'stock':[dict(r,price=r['base_price']*7) for r in catalog()['shop']]}}


class GrowthPolicyTests(unittest.TestCase):
    def test_default_merchant_thresholds_and_saved_custom_percentages_survive_reload(self):
        cfg=validate_settings(deepcopy(DEFAULT_SETTINGS))
        self.assertEqual((cfg['merchant']['trigger_percent'],cfg['merchant']['spend_to_percent']),(85,0))
        cfg['merchant'].update(trigger_percent=93,spend_to_percent=17)
        import json
        reloaded=validate_settings(json.loads(json.dumps(cfg)))
        self.assertEqual((reloaded['merchant']['trigger_percent'],reloaded['merchant']['spend_to_percent']),(93,17))
    def test_merchant_junk_disposed_directly_with_space_while_ordinary_store_policy_survives(self):
        for primary,fallback in (('store','sell'),('store','salvage'),('sell','salvage'),('salvage','sell')):
            cfg=settings(equipment_action=primary,equipment_fallback=fallback)
            world=observation([equipment('bought')],town=True,merchant_uids=['bought'])
            engine=ManagedMode(cfg);engine.start(world)
            self.assertEqual(engine.next_action(world)['kind'],fallback if primary=='store' else primary)
            world=observation([equipment('ordinary')],town=True)
            engine=ManagedMode(cfg);engine.start(world)
            self.assertEqual(engine.next_action(world)['kind'],'transfer' if primary=='store' else primary)

    def test_merchant_protection_still_locks_before_any_junk_disposal(self):
        cfg=settings(equipment_action='store',equipment_fallback='salvage')
        for keep in (equipment('keep',matches=True),equipment('keep',rarity=4),equipment('keep',is_black_mist=True)):
            world=observation([equipment('junk'),keep],town=True,merchant_uids=['junk','keep'])
            engine=ManagedMode(cfg);engine.start(world)
            lock=engine.next_action(world);self.assertEqual(lock['kind'],'lock');self.assertEqual(lock['item']['item_uid'],'keep')

    def test_batch_purchase_keeps_slot_order_and_limits_stock_capacity_budget(self):
        cfg=settings()['merchant'];cfg.update(spend_to_percent=0,weights={str(n):0 for n in range(1,11)})
        cfg['weights'].update({'2':1,'7':10});current=state()
        for quote in current['merchant']['stock']:quote['available_count']=10
        self.assertEqual(next_purchase(cfg,current,{},max_quantity=10)['shop_slot'],2)
        counts={'2':1};action=next_purchase(cfg,current,counts,max_quantity=10)
        self.assertEqual((action['shop_slot'],action['quantity']),(7,10))
        self.assertEqual(next_purchase(cfg,current,counts,max_quantity=3)['quantity'],3)
        current['soul_shards']['current']=23
        self.assertIsNone(next_purchase(cfg,current,counts,max_quantity=10))

    def test_batch_quote_stops_before_price_change_and_rejects_inconsistent_head(self):
        quotes={'ring':{'price':28,'prices':[28,28,35],'level':70,'difficulty':'Inferno1'}}
        stock,_=merchant_prices(catalog()['shop'],quotes,70,'Inferno',True)
        self.assertEqual(stock[0]['available_count'],2)
        quotes['ring']['prices']=[35,28]
        with self.assertRaises(ValidationError):merchant_prices(catalog()['shop'],quotes,70,'Inferno',True)

    def test_purchase_fills_bag_before_closing_then_locks_before_disposal(self):
        cfg=settings(equipment_action='salvage');cfg['merchant'].update(enabled=True,spend_to_percent=0)
        cfg['merchant']['weights']={str(n):0 for n in range(1,11)};cfg['merchant']['weights']['2']=10
        current=state();current['merchant']['stock'][1]['available_count']=10
        world=observation([],town=True,capacity=3,progression=current);engine=ManagedMode(cfg);engine.start(world)
        buy=engine.next_action(world);self.assertEqual(buy['quantity'],3)
        bag=[equipment(str(n),n,matches=n==0) for n in range(3)]
        engine.complete(buy['action_id'],result={'verified':True,'merchant_open':True,'received_uids':[r['item_uid'] for r in bag]})
        current['soul_shards']['current']-=3*buy['expected_price']
        world=observation(bag,town=True,capacity=3,progression=current)
        close=engine.next_action(world);self.assertEqual(close['kind'],'merchant_close');verified(engine,close)
        current['merchant']['open']=False
        world=observation(bag,town=True,capacity=3,progression=current)
        lock=engine.next_action(world);self.assertEqual(lock['kind'],'lock');self.assertEqual(lock['item']['item_uid'],'0')

    def test_owned_purchase_can_continue_with_free_space_but_unknown_bag_cannot(self):
        cfg=settings();cfg['merchant']['enabled']=True;current=state()
        world=observation([],town=True,progression=current);engine=ManagedMode(cfg);engine.start(world)
        buy=engine.next_action(world)
        engine.complete(buy['action_id'],result={'verified':True,'merchant_open':True,'received_uids':['bought']})
        current['soul_shards']['current']-=buy['expected_price']
        bag=[equipment('bought',matches=True)]
        world=observation(bag,town=True,progression=current)
        self.assertEqual(engine.next_action(world)['kind'],'merchant_buy')
        self.assertFalse(engine.merchant_batch_ready(observation([equipment('unknown')],town=True,progression=current)))
        self.assertFalse(engine.merchant_batch_ready(observation([equipment('bought',rarity=4)],town=True,progression=current)))

    def test_retained_potions_allow_first_purchase_and_batch_refill_in_actual_free_slots(self):
        cfg=settings();cfg['merchant']['enabled']=True
        cfg['merchant']['weights']={str(n):0 for n in range(1,11)}
        cfg['merchant']['weights'].update({'2':1,'7':10})
        current=state()
        for quote in current['merchant']['stock']:quote['available_count']=10
        potions=[loot('potion-'+str(n),'material',name,n,count=14,
                      item_type=2,item_class='ItemData',item_uid='',identity_verified=False)
                 for n,name in enumerate(('MovementPotion1','ExpPotion1'))]
        original=deepcopy(potions)
        world=observation(potions,town=True,capacity=5,progression=current)
        engine=ManagedMode(cfg);engine.start(world)
        self.assertTrue(engine.merchant_batch_ready(world))
        buy=engine.next_action(world)
        self.assertEqual((buy['kind'],buy['shop_slot'],buy.get('quantity',1)),('merchant_buy',2,1))
        verified_receipt={'verified':True,'merchant_open':True,'received_uids':['bought']}
        engine.complete(buy['action_id'],result=verified_receipt)
        current['soul_shards']['current']-=buy['expected_price']
        world=observation(potions+[equipment('bought',2)],town=True,capacity=5,progression=current)
        buy=engine.next_action(world)
        self.assertEqual((buy['kind'],buy['shop_slot'],buy['quantity']),('merchant_buy',7,2))
        self.assertEqual(potions,original)

    def test_retained_potions_do_not_bypass_reward_review_collection_or_capacity(self):
        engine=ManagedMode(settings());engine.merchant_received_uids={'bought'}
        potion=loot('potion','material','MovementPotion1',item_type=2,item_class='ItemData',count=14,item_uid='')
        self.assertTrue(engine.merchant_batch_ready(observation([potion],town=True)))
        for row in (equipment('unknown'),equipment('bought',review=True),equipment('bought',rarity=4),
                    equipment('bought',is_black_mist=True),
                    loot('chest','chest','TreasureChest1',item_type=2,item_class='ItemData'),
                    loot('gem','gem','GemSapphire4'),loot('rune','rune','LegendaryRune_A'),
                    dict(potion,kind='equipment',is_equipment=True),
                    dict(potion,item_class='RuneData'),dict(potion,issues=['unreadable']),
                    dict(potion,item_type=3),dict(potion,count=0)):
            with self.subTest(row=row):
                self.assertFalse(engine.merchant_batch_ready(observation([potion,row],town=True)))
        for key in ('ground','carriage'):
            world=observation([potion],town=True);world[key]['items']=[equipment('outside')]
            self.assertFalse(engine.merchant_batch_ready(world))
            world[key].update(items=[],complete=False)
            self.assertFalse(engine.merchant_batch_ready(world))
        self.assertFalse(engine.merchant_batch_ready(observation([potion],town=True,capacity=1)))
        cfg=settings();cfg['merchant']['enabled']=True
        engine=ManagedMode(cfg);world=observation([potion],town=True,capacity=1,progression=state())
        engine.start(world);self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.phase,'paused');self.assertIn('背包没有空格',engine.status)

    def test_new_merchant_plan_skips_all_slots_but_preserves_saved_weights(self):
        self.assertEqual(set(DEFAULT_SETTINGS['merchant']['weights'].values()), {0})
        cfg = deepcopy(DEFAULT_SETTINGS)
        cfg['merchant']['weights'].update({'2':10,'3':1})
        self.assertEqual(validate_settings(cfg)['merchant']['weights'], cfg['merchant']['weights'])

    def test_each_class_plan_cannot_exceed_seventy_even_with_feature_disabled(self):
        for mask, hero in catalog()['heroes'].items():
            cfg = deepcopy(DEFAULT_SETTINGS)
            nodes = {n['id']:n for n in hero['talents']}
            ranks = {}
            from deskrawl_assistant.progression import talent_allowed
            for _ in range(70):
                key = next(k for k,n in nodes.items() if talent_allowed(n,nodes,ranks))
                ranks[key] = ranks.get(key,0) + 1
            cfg['progression']['talent_targets'][mask] = deepcopy(ranks)
            self.assertEqual(sum(validate_settings(cfg)['progression']['talent_targets'][mask].values()),70)
            key = next(k for k,n in nodes.items() if talent_allowed(n,nodes,ranks))
            cfg['progression']['talent_targets'][mask][key] = ranks.get(key,0) + 1
            with self.assertRaisesRegex(ValidationError,'最多 70 点'): validate_settings(cfg)

    def test_old_preferences_survive_with_all_new_features_off(self):
        old = deepcopy(DEFAULT_SETTINGS)
        for key in ('progression','life_skills','merchant'): old.pop(key)
        old['cleanup']['gem_pages']=[2]; old['background']['rule_monitoring']=False
        new=validate_settings(old)
        self.assertEqual(new['cleanup']['gem_pages'],[2])
        self.assertFalse(new['background']['rule_monitoring'])
        self.assertFalse(new['life_skills']['enabled']);self.assertFalse(new['merchant']['enabled'])
        self.assertFalse(new['progression']['talents_enabled']);self.assertFalse(new['progression']['skills_enabled'])
        self.assertNotIn('life_skills',old)

    def test_catalog_has_four_complete_distinct_pages_and_all_icons(self):
        data=catalog();self.assertEqual(set(data['heroes']),{'1','2','4','8'})
        self.assertEqual(sum(len(h['talents']) for h in data['heroes'].values()),168)
        self.assertEqual(len(data['life_skills']),85)
        rows = data['life_skills']+[r for h in data['heroes'].values() for group in ('talents','abilities') for r in h[group]]
        for row in rows:
            icon=Path(__file__).resolve().parents[1]/'deskrawl_assistant/web'/row['icon'].lstrip('/')
            self.assertTrue(icon.is_file(),row['id']);self.assertTrue(row['name'])
        self.assertEqual(data['soul_maximum']+sum(n['soul_capacity_per_rank']*n['max_points'] for n in data['life_skills']),3000)
        for h in data['heroes'].values(): self.assertEqual(len(h['abilities']),14)

    def test_priority_life_node_includes_its_whole_prerequisite_chain(self):
        cfg=deepcopy(DEFAULT_SETTINGS);cfg['life_skills'].update(enabled=True,priorities=['LifeSkill 66'])
        current=state();steps=[]
        for _ in range(10):
            action,_=growth_action(cfg,current)
            steps.append(action['node_id']);current['life_skills']['ranks'][action['node_id']]=1
            if action['node_id']=='LifeSkill 66':break
        self.assertEqual(steps,['LifeSkill','LifeSkill 2','LifeSkill 8','LifeSkill 31','LifeSkill 44','LifeSkill 66'])
        self.assertEqual(sum(n['soul_capacity_per_rank'] for n in catalog()['life_skills'] if n['id'] in steps),1000)

    def test_priority_does_not_spend_reserve_on_other_cheaper_nodes(self):
        cfg=deepcopy(DEFAULT_SETTINGS);cfg['life_skills'].update(enabled=True,priorities=['LifeSkill 66'],gold_reserve=1000)
        current=state(gold=1010)
        action,reason=growth_action(cfg,current)
        self.assertIsNone(action);self.assertIn('等待金币',reason)

    def test_life_plan_eventually_learns_every_node_without_refunds(self):
        cfg=deepcopy(DEFAULT_SETTINGS);cfg['life_skills']['enabled']=True;current=state(gold=100000000)
        for _ in range(86):
            action,_=growth_action(cfg,current)
            if not action:break
            current['life_skills']['ranks'][action['node_id']]=action['expected_rank']+1
            current['gold']-=action['gold_cost']
        self.assertEqual(len(current['life_skills']['ranks']),85)

    def test_busy_game_never_allocates_or_submits_another_purchase(self):
        cfg=settings();cfg['life_skills']['enabled']=True;cfg['merchant']['enabled']=True
        current=state();current['busy']=True
        self.assertIsNone(growth_action(cfg,current)[0])
        engine=ManagedMode(cfg);world=observation([],town=True,progression=current);engine.start(world)
        self.assertIsNone(engine.next_action(world));self.assertTrue(engine.running)

    def test_talent_plans_are_per_class_and_do_not_spend_unavailable_points(self):
        cfg=deepcopy(DEFAULT_SETTINGS);cfg['progression']['talents_enabled']=True
        for mask in (1,2,4,8):
            node=next(n for n in catalog()['heroes'][str(mask)]['talents'] if n['point_requirement']==0 and not n['prerequisite'])
            cfg['progression']['talent_targets'][str(mask)]={node['id']:1}
            current=state(mask);action,_=growth_action(cfg,current)
            self.assertEqual(action['node_id'],node['id'])
            current['talents']['unspent']=0;self.assertIsNone(growth_action(cfg,current)[0])

    def test_unreachable_parent_gate_and_mutually_exclusive_targets_rejected(self):
        cfg=deepcopy(DEFAULT_SETTINGS);hero=catalog()['heroes']['4']
        gated=next(n for n in hero['talents'] if n['point_requirement']>0)
        cfg['progression']['talent_targets']['4']={gated['id']:1}
        with self.assertRaises(ValidationError):validate_settings(cfg)
        synthetic=deepcopy(catalog());nodes=synthetic['heroes']['4']['talents'];nodes[1]['prerequisite']=nodes[0]['id']
        cfg['progression']['talent_targets']['4']={nodes[1]['id']:1}
        with self.assertRaises(ValidationError):validate_growth(cfg,synthetic)
        groups={}
        for n in hero['talents']:
            if n['exclusive_group']:groups.setdefault(n['exclusive_group'],[]).append(n)
        peers=next(rows for rows in groups.values() if len(rows)>1)
        cfg['progression']['talent_targets']['4']={n['id']:n['max_points'] for n in hero['talents']}
        with self.assertRaises(ValidationError):validate_settings(cfg)
        self.assertGreater(len(peers),1)

    def test_skill_stages_preserve_earlier_slots_and_wait_for_unlock(self):
        cfg=deepcopy(DEFAULT_SETTINGS);cfg['progression']['skills_enabled']=True
        skills=catalog()['heroes']['4']['abilities'];basic=next(s for s in skills if 0 in s['slots']);strong=next(s for s in skills if 1 in s['slots'])
        cfg['progression']['skill_stages']['4']=[{'level':1,'slots':[basic['id'],None,None,None]},
                                                {'level':20,'slots':[None,strong['id'],None,None]}]
        current=state();current['skills']['available'].remove(strong['id'])
        action,_=growth_action(cfg,current);self.assertEqual(action['skill_slots'],[basic['id'],None,None,None])
        current['skills']['equipped']=action['skill_slots'];current['skills']['available'].append(strong['id'])
        self.assertEqual(growth_action(cfg,current)[0]['skill_slots'],[basic['id'],strong['id'],None,None])

    def test_icon_preset_changes_each_slot_only_after_its_native_unlock_level(self):
        for mask in (1,2,4,8):
            with self.subTest(mask=mask):
                cfg=deepcopy(DEFAULT_SETTINGS);cfg['progression']['skills_enabled']=True
                skills=catalog()['heroes'][str(mask)]['abilities']
                basic=next(s for s in skills if 0 in s['slots'] and s['level_requirement']==1)
                future=next(s for s in skills if 0 in s['slots'] and s['level_requirement']>1)
                strong=next(s for s in skills if 1 in s['slots'])
                cfg['progression']['skill_stages'][str(mask)]=[{'level':1,'slots':[future['id'],strong['id'],None,None]}]
                current=state(mask);current['level']=strong['level_requirement']
                current['skills']['equipped']=[basic['id'],None,None,None]
                action,_=growth_action(cfg,current)
                self.assertEqual(action['skill_slots'],[basic['id'],strong['id'],None,None])
                current['skills']['equipped']=action['skill_slots']
                current['level']=future['level_requirement']-1
                self.assertIsNone(growth_action(cfg,current)[0])
                current['level']=future['level_requirement']
                self.assertEqual(growth_action(cfg,current)[0]['skill_slots'][0],future['id'])

    def test_special_slot_swap_is_one_atomic_planned_group(self):
        cfg=deepcopy(DEFAULT_SETTINGS);cfg['progression']['skills_enabled']=True
        a,b=[s['id'] for s in catalog()['heroes']['8']['abilities'] if 2 in s['slots']][:2]
        cfg['progression']['skill_stages']['8']=[{'level':1,'slots':[None,None,b,a]}]
        current=state(8);current['skills']['equipped']=[None,None,a,b]
        self.assertEqual(growth_action(cfg,current)[0]['skill_slots'],[None,None,b,a])

    def test_inactive_prototypes_and_duplicate_inherited_skills_are_not_targets(self):
        cfg=deepcopy(DEFAULT_SETTINGS)
        cfg['progression']['talent_targets']['1']={'WarriorTalent32':1}
        with self.assertRaisesRegex(ValidationError,'未在游戏中开放'):validate_settings(cfg)
        cfg['progression']['talent_targets']={}
        a,b=[s['id'] for s in catalog()['heroes']['8']['abilities'] if 2 in s['slots']][:2]
        cfg['progression']['skill_stages']['8']=[{'level':10,'slots':[None,None,a,b]},
                                               {'level':20,'slots':[None,None,b,None]}]
        with self.assertRaisesRegex(ValidationError,'重复技能'):validate_settings(cfg)

    def test_malformed_growth_settings_and_unassigned_skill_categories_rejected(self):
        for edit in (lambda c:c['life_skills'].update(priorities=[[]]),
                     lambda c:c['merchant'].update(trigger_percent=10,spend_to_percent=10),
                     lambda c:c['merchant'].update(enabled=True,weights={str(n):0 for n in range(1,11)}),
                     lambda c:c['progression']['skill_stages'].update({'4':[{'level':1,'slots':['MonkSpecial1',None,None,None]}]})):
            cfg=deepcopy(DEFAULT_SETTINGS);edit(cfg)
            with self.assertRaises(ValidationError):validate_settings(cfg)


class MerchantPolicyTests(unittest.TestCase):
    def test_unreadable_town_skills_allow_farming_to_load_combat_controller(self):
        cfg=settings();cfg['progression']['skills_enabled']=True
        current=state(shards=100)
        current['skills']={'readable':False,'equipped':None,'available':[],
            'reason':'PlayerAbilityController 尚未加载。'}
        current['merchant'].update(available=False,open=False)
        world=observation([],town=True,progression=current)
        engine=ManagedMode(cfg);engine.start(world)
        for _ in range(10):
            action=engine.next_action(world)
            if action is not None:break
        else:self.fail('Unreadable town skills prevented farming from starting')
        self.assertEqual(action['kind'],'enter_dungeon')
        self.assertTrue(engine.running)

    def test_unreadable_skills_wait_without_fake_equipping_and_do_not_block_other_growth(self):
        cfg=settings();cfg['progression']['skills_enabled']=True
        current=state();current['skills']={'readable':False,'equipped':None,'available':[],
            'reason':'PlayerAbilityController 尚未加载。'}
        action,reason=growth_action(cfg,current)
        self.assertIsNone(action);self.assertIn('技能读取',reason)
        cfg['life_skills']['enabled']=True
        action,_=growth_action(cfg,current)
        self.assertEqual(action['kind'],'learn_life_skill')
        cfg['life_skills']['enabled']=False;cfg['progression']['talents_enabled']=True
        cfg['progression']['talent_targets']={'4':{'HunterTalent0':1}}
        action,_=growth_action(cfg,current)
        self.assertEqual(action['kind'],'allocate_talent')

    def test_dynamic_capacity_avoids_early_return_after_life_upgrade(self):
        cfg=settings();cfg['merchant']['enabled']=True
        for cap,expected in ((2000,'return_town'),(3000,None)):
            engine=ManagedMode(cfg);world=observation(progression=state(cap=cap));engine.start(world)
            action=engine.next_action(world);self.assertEqual(action['kind'] if action else None,expected)

    def test_weighted_counts_follow_configured_ratios_without_cheaper_substitutions(self):
        cfg=deepcopy(DEFAULT_SETTINGS)['merchant'];cfg.update(spend_to_percent=10,weights={str(n):0 for n in range(1,11)});cfg['weights'].update({'1':2,'2':1})
        current=state(shards=2000);counts={}
        for _ in range(30):
            action=next_purchase(cfg,current,counts);key=str(action['shop_slot']);counts[key]=counts.get(key,0)+1
        self.assertEqual(counts,{'1':20,'2':10})
        current['soul_shards']['current']=201;self.assertIsNone(next_purchase(cfg,current,{}))

    def test_locked_merchant_cannot_open_or_buy_even_at_high_level_with_enough_currency(self):
        cfg=settings();cfg['merchant']['enabled']=True;current=state(gold=1000000)
        current['merchant']['unlocked']=False
        engine=ManagedMode(cfg);world=observation([],town=True,progression=current);engine.start(world)
        self.assertIsNone(engine.next_action(world));self.assertEqual(engine.phase,'paused')
        self.assertIn('尚未解锁',engine.status)

    def test_merchant_reference_prices_never_become_online_purchase_quotes(self):
        entries=catalog()['shop'];stock,preview=merchant_prices(entries,{},70,'Normal',True)
        self.assertEqual(stock,[]);self.assertEqual(len(preview),10)
        self.assertTrue(all(row['price_reference'] for row in preview))
        helm=next(row for row in preview if row['slot']==2);self.assertEqual(helm['price'],21)
        quotes={'helm':{'price':25,'level':70,'difficulty':'Normal'}}
        stock,preview=merchant_prices(entries,quotes,70,'Normal',True)
        self.assertEqual([(row['slot'],row['price']) for row in stock],[(2,25)])
        self.assertFalse(next(row for row in preview if row['slot']==2)['price_reference'])
        for level,difficulty in [(71,'Normal'),(70,'Nightmare')]:
            stock,preview=merchant_prices(entries,quotes,level,difficulty,True)
            self.assertEqual(stock,[]);self.assertTrue(all(row['price_reference'] for row in preview))
        stock,preview=merchant_prices(entries,{},9,'Normal',False)
        self.assertEqual(len(stock),10);self.assertTrue(all(not row['price_reference'] for row in preview))
        self.assertEqual(stock[0]['price'],entries[0]['base_price'])

    def test_level_prices_follow_native_integer_division_at_level_boundaries(self):
        entries=catalog()['shop']
        for level,multiplier in [(1,1),(9,1),(10,1),(19,1),(20,2),(69,6),(70,7),(79,7),(80,8)]:
            stock,preview=merchant_prices(entries,{},level,'Normal',True)
            self.assertEqual(stock,[])
            self.assertEqual([r['price'] for r in preview],[r['base_price']*multiplier for r in entries])
            self.assertTrue(all(r['price_source']=='level' for r in preview))

    def test_live_inferno1_quote_matches_normalized_inferno_difficulty_only(self):
        quote={'helm':{'price':21,'level':70,'difficulty':'Inferno1'}}
        stock,_=merchant_prices(catalog()['shop'],quote,70,'Inferno',True)
        self.assertEqual([(r['slot'],r['price']) for r in stock],[(2,21)])
        for difficulty in ('Normal','Nightmare'):
            self.assertEqual(merchant_prices(catalog()['shop'],quote,70,difficulty,True)[0],[])

    def test_zero_reserve_finishes_shopping_with_unspendable_remainder_and_resumes_map(self):
        cfg=settings();cfg['merchant'].update(enabled=True,spend_to_percent=0)
        cfg['merchant']['weights']={str(n):0 for n in range(1,11)}
        cfg['merchant']['weights']['2']=1
        current=state(shards=1900);world=observation([],town=True,progression=current)
        engine=ManagedMode(cfg);engine.start(world)
        for _ in range(400):
            action=engine.next_action(observation([],town=True,progression=current))
            if action is None: continue
            if action['kind']=='enter_dungeon': break
            self.assertIn(action['kind'],('merchant_buy','merchant_open','merchant_close'))
            if action['kind']=='merchant_buy': current['soul_shards']['current']-=action['expected_price']
            else: current['merchant']['open']=action['kind']=='merchant_open'
            engine.complete(action['action_id'],result={'verified':True,'merchant_open':current['merchant']['open']})
        else: self.fail('Shopping did not finish with an unspendable remainder')
        self.assertEqual(action['kind'],'enter_dungeon')
        self.assertEqual(current['soul_shards']['current'],10)
        self.assertEqual(engine.merchant_counts,{'2':90})
        self.assertFalse(engine.merchant_pending);self.assertTrue(engine.running)

    def test_merchant_buys_ten_helmets_then_one_chest_in_exact_cycles(self):
        cfg=deepcopy(DEFAULT_SETTINGS)['merchant'];cfg['weights']={str(n):0 for n in range(1,11)}
        cfg['weights'].update({'2':10,'3':1});current=state();counts={};sequence=[]
        for _ in range(97):
            action=next_purchase(cfg,current,counts);key=str(action['shop_slot'])
            # Reading/planning again before a verified purchase changes nothing.
            self.assertEqual(next_purchase(cfg,current,counts)['shop_slot'],action['shop_slot'])
            sequence.append(action['shop_slot']);counts[key]=counts.get(key,0)+1
        self.assertEqual(sequence,[2]*10+[3]+[2]*10+[3]+([2]*10+[3])*6+[2]*9)
        self.assertEqual(counts,{'2':89,'3':8})

    def test_empty_cached_slot_refills_without_advancing_the_purchase_cycle(self):
        cfg=settings();cfg['merchant']['enabled']=True
        cfg['merchant']['weights']={str(n):0 for n in range(1,11)};cfg['merchant']['weights'].update({'2':1,'7':10})
        current=state();current['merchant']['stock']=[r for r in current['merchant']['stock'] if r['slot']!=7]
        world=observation([],town=True,progression=current);engine=ManagedMode(cfg);engine.start(world)
        action=engine.next_action(world)
        self.assertEqual(action['kind'],'merchant_refresh');self.assertEqual(action['shop_slots'],[7])
        self.assertEqual(engine.merchant_counts,{})
        verified(engine,action)
        current['merchant']['stock']=state()['merchant']['stock']
        buy=engine.next_action(observation([],town=True,progression=current))
        self.assertEqual(buy['kind'],'merchant_buy');self.assertEqual(buy['shop_slot'],2)

    def test_merchant_partial_batch_waits_for_funds_without_buying_cheaper_slot(self):
        cfg=deepcopy(DEFAULT_SETTINGS)['merchant'];cfg['weights']={str(n):0 for n in range(1,11)}
        cfg['spend_to_percent']=10
        cfg['weights'].update({'1':2,'2':1});current=state(shards=221);counts={'1':1}
        self.assertIsNone(next_purchase(cfg,current,counts));self.assertEqual(counts,{'1':1})
        current['soul_shards']['current']=500
        self.assertEqual(next_purchase(cfg,current,counts)['shop_slot'],1)
        counts['1']=2;self.assertEqual(next_purchase(cfg,current,counts)['shop_slot'],2)

    def test_deferred_refill_keeps_farming_until_stock_recovers_even_with_full_shards(self):
        cfg=settings();cfg['merchant']['enabled']=True
        cfg['farming']['enabled']=False
        cfg['merchant']['weights']={str(n):int(n==7) for n in range(1,11)}
        current=state(shards=2000)
        current['merchant']['stock']=[r for r in current['merchant']['stock'] if r['slot']!=7]
        world=observation([],town=True,progression=current)
        engine=ManagedMode(cfg);engine.start(world)
        engine.merchant_refresh_deferred=True
        engine.merchant_last_balance=1900;engine.merchant_last_capacity=2000
        for _ in range(8):
            self.assertIsNone(engine.next_action(world))
            self.assertTrue(engine.running);self.assertFalse(engine.merchant_pending)
        current['merchant']['stock']=state()['merchant']['stock']
        action=engine.next_action(observation([],town=True,progression=current))
        self.assertEqual(action['kind'],'merchant_buy')
        self.assertEqual(action['shop_slot'],7)
        self.assertFalse(engine.merchant_refresh_deferred)

    def test_deferred_refill_cannot_reenter_the_batch_purchase_fast_path(self):
        cfg=settings();cfg['merchant']['enabled']=True
        current=state();current['merchant']['stock']=[]
        world=observation([],town=True,progression=current)
        engine=ManagedMode(cfg);engine.start(world);engine.merchant_refresh_deferred=True
        self.assertIsNone(engine._merchant_purchase(world))
        self.assertIsNone(engine.pending)

    def test_bought_divine_and_matched_gear_locked_and_stored_before_next_purchase(self):
        for row in (equipment('bought',rarity=4),equipment('bought',matches=True)):
            cfg=settings(equipment_action='salvage');cfg['merchant']['enabled']=True
            current=state();world=observation([],town=True,progression=current);engine=ManagedMode(cfg);engine.start(world)
            buy=engine.next_action(world);self.assertEqual(buy['kind'],'merchant_buy');verified(engine,buy)
            current['merchant']['open']=False  # Receipt reports that the purchase window closed.
            current['soul_shards']['current']-=buy['expected_price'];world=observation([row],town=True,progression=current)
            lock=engine.next_action(world);self.assertEqual(lock['kind'],'lock');verified(engine,lock)
            row['locked']=True;world=observation([row],town=True,progression=current)
            move=engine.next_action(world);self.assertEqual(move['kind'],'transfer');self.assertEqual(move['target'],'storage')

    def test_new_purchase_starts_whole_batch_rule_barrier(self):
        cfg=settings(equipment_action='salvage');cfg['merchant']['enabled']=True;current=state()
        world=observation([],town=True,progression=current);engine=ManagedMode(cfg);engine.start(world)
        buy=engine.next_action(world);verified(engine,buy);current['soul_shards']['current']-=buy['expected_price']
        current['merchant']['open']=False
        fresh=equipment('fresh',review=True,rule_status='review');world=observation([fresh],town=True,progression=current)
        self.assertIsNone(engine.next_action(world));self.assertIn('暂不出售或分解',engine.status)

    def test_confirmed_purchase_waits_for_sync_then_closes_before_protecting_gear(self):
        cfg=settings();cfg['merchant']['enabled']=True;current=state()
        world=observation([],town=True,progression=current);engine=ManagedMode(cfg);engine.start(world)
        buy=engine.next_action(world)
        engine.complete(buy['action_id'],result={'verified':True,'merchant_open':True})
        current['soul_shards']['current']-=buy['expected_price'];current['busy']=True
        item=equipment('purchase',rarity=4);world=observation([item],town=True,progression=current)
        self.assertIsNone(engine.next_action(world));self.assertTrue(engine.running)
        current['busy']=False
        close=engine.next_action(world);self.assertEqual(close['kind'],'merchant_close');verified(engine,close)
        current['merchant']['open']=False
        lock=engine.next_action(world);self.assertEqual(lock['kind'],'lock')

    def test_stop_never_buys_or_allocates_more_points(self):
        cfg=settings();cfg['merchant']['enabled']=True;cfg['life_skills']['enabled']=True
        world=observation([],town=True,progression=state());engine=ManagedMode(cfg);engine.start(world);engine.request_finish()
        self.assertIsNone(engine.next_action(world));self.assertIsNone(engine.next_action(world));self.assertFalse(engine.running)

    def test_absent_growth_state_stops_shard_spending_and_map_entry(self):
        cfg=settings();cfg['merchant']['enabled']=True;world=observation([],town=True)
        engine=ManagedMode(cfg);engine.start(world);self.assertIsNone(engine.next_action(world))
        self.assertIn('容量',engine.status)

    def test_unverified_growth_actions_are_blocked_before_process_reads_without_opt_in(self):
        reader,dispatcher=Mock(),Mock()
        profile=load_profile();profile.pop('enabled_kinds',None)
        with patch('deskrawl_assistant.game_actions.load_profile',return_value=profile):
            engine=GameActions(reader,dispatcher=dispatcher)
        for kind in ('learn_life_skill', 'set_skill'):
            with self.assertRaises(NativeActionError):engine.execute(kind,{})
        reader.check_game_files.assert_not_called();dispatcher.invoke.assert_not_called()

    def test_life_and_talents_require_individual_native_capabilities(self):
        cfg=deepcopy(DEFAULT_SETTINGS);cfg['life_skills']['enabled']=True;cfg['progression']['talents_enabled']=True
        keys=required_capabilities(cfg);self.assertIn('life_skills',keys);self.assertIn('talents',keys);self.assertNotIn('merchant',keys)


if __name__=='__main__':unittest.main()
