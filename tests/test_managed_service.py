"""Managed application bridge tests with no native client, windows or server.

Thread.start is mocked before service construction, so background polling cannot
run. Every snapshot, capability and commit below belongs to this synthetic game.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.character import final_stats, panel_attack
from deskrawl_assistant.managed_mode import DEFAULT_SETTINGS, identity
from deskrawl_assistant.managed_service import progression_from_catalog, required_capabilities, campaign_route
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.native_memory import GameBusyError, SnapshotChangedError, OnlineSyncBusyError
from deskrawl_assistant.native_dispatcher import NativeActionError, REJECTED, GUARD_FAILED
from deskrawl_assistant.web_service import AssistantService


FIXTURE = json.loads((Path(__file__).parent/'fixtures/service-snapshot.json').read_text(encoding='utf-8'))
TEMPLATE = FIXTURE['containers']['inventory']['slots'][0]


def equipment(index=0, *, container='inventory', key='LegendaryBelt3', **extra):
    value = deepcopy(TEMPLATE)
    value.update(item_level=1,base_rarity=3,slot_index=index,container=container,name_key=key,internal_name=key,
                 item_uid=f'offline-{container}-{index}',instance_id=f'offline-{container}-{index}',
                 transfer_key=f'transfer-{container}-{index}',is_black_mist=False,is_ancient=False,issues=[])
    value.update(extra)
    return value


def potion(index=0, *, key='MovementPotion1', count=14):
    return equipment(index,key=key,is_equipment=False,item_type=2,item_class='ItemData',
        kind='material',item_uid='',instance_id='',identity_verified=False,
        base_rarity=0,count=count)


class FakeManagedClient:
    def __init__(self, items=()):
        self.connected = True
        self.pid = 999
        self.items = deepcopy(list(items))
        self.carriage = []
        self.ground = []
        self.actions = []
        self.captures = 0
        self.storage_indices = list(range(50))+list(range(200,250))
        self.capabilities = {key:{'available':True,'native_verified':True}
                             for key in ('equip','travel','difficulty','sell','salvage','chests','ground_pickup')}
        self.maps = [{'id':'offline-map','name':'离线测试地图','unlocked':True,'can_enter':True,'supported':True}]
        self.in_town = True
        self.current_map = None
        self.run_status = 'running'
        self.difficulty = 'Normal'
        self.difficulties = [{'id':key,'unlocked':True} for key in ('Normal','Nightmare','Inferno')]
        self.native_handler = None
        self.carriage_corrupt_on_collect = False
        self.lock_commits_visible = True

    def snapshot(self):
        self.captures += 1
        snap = deepcopy(FIXTURE)
        snap.update(bridge_session='offline-session',snapshot_id=f'offline-{self.captures}',pid=self.pid)
        snap['transfer'] = {'available':True,'page_size':50,'storage_indices':list(self.storage_indices)}
        for container, details in snap['containers'].items():
            details['slots'] = deepcopy([row for row in self.items if row['container']==container])
            details['occupied_count'] = len(details['slots'])
        return snap

    def carriage_snapshot(self):
        return {'available':True,'complete':True,'count':len(self.carriage),'items':deepcopy(self.carriage)}

    def control_snapshot(self):
        return {'available':True,'scene':{'available':True,'in_town':self.in_town,'in_dungeon':not self.in_town,'dungeon_id':self.current_map},
                'scene_changing':False,'teleporting':False,'online_saving':False,'online_pending_requests':0,
                'difficulty':self.difficulty,'difficulties':deepcopy(self.difficulties),
                'maps_by_difficulty':{key:deepcopy(self.maps) for key in ('Normal','Nightmare','Inferno')},
                'run':None if self.in_town else {'id':'offline-run','status':self.run_status,'dungeon_id':self.current_map,'difficulty':self.difficulty},
                'ground':{'available':True,'complete':True,'items':deepcopy(self.ground)},'maps':deepcopy(self.maps)}

    def managed_capabilities(self):
        return deepcopy(self.capabilities)

    def lock_equipment(self, source, validate):
        actual = next(row for row in self.items if identity(row)==identity(source))
        if not validate(actual): raise ValidationError('模拟锁定被取消。')
        self.actions.append(('lock',actual['item_uid']))
        if self.lock_commits_visible: actual['locked'] = True
        return {'status':'locked'}

    def move_items(self, sources, target, validate, *, target_indices=None):
        if not validate(): raise ValidationError('模拟转移被取消。')
        actual = next(row for row in self.items if identity(row)==identity(sources[0]))
        candidates = target_indices if target=='storage' else list(range(40))
        used = {row['slot_index'] for row in self.items if row['container']==target}
        destination = next(index for index in candidates if index not in used)
        self.actions.append(('transfer',actual['item_uid'],target,destination))
        actual.update(container=target,slot_index=destination)
        return {'moved':1}

    def collect_carriage(self, selection_id, validate):
        if not validate(): raise ValidationError('模拟收取被取消。')
        actual = next(row for row in self.carriage if row['selection_id']==selection_id)
        self.carriage.remove(actual)
        occupied = {r['slot_index'] for r in self.items if r['container']=='inventory'}
        actual.update(container='inventory',slot_index=next(n for n in range(40) if n not in occupied))
        if self.carriage_corrupt_on_collect:
            actual['modifiers_complete'] = False
            actual['read_evidence']['modifiers_complete'] = False
        self.items.append(actual)
        self.actions.append(('collect_carriage',actual['item_uid']))
        return {'moved':1}

    def execute_game_action(self, kind, payload, cancelled, *, validate):
        if cancelled.is_set() or not validate(): raise ValidationError('模拟原生动作被取消。')
        self.actions.append((kind,deepcopy(payload)))
        if self.native_handler: return self.native_handler(kind,payload)
        return {'bridge_verified':True,'effect_confirmed':True,'quantity':(payload.get('item') or {}).get('count',1)}

    def close(self): self.connected = False


class ManagedServiceTests(unittest.TestCase):
    def start_delayed_merchant_refill(self, *, farming=False):
        from test_progression import state
        self.client.items=[]
        growth=state();growth['busy']=False
        growth['merchant']['stock']=[r for r in growth['merchant']['stock'] if r['slot']!=7]
        self.client.progression_snapshot=Mock(side_effect=lambda **kw:deepcopy(growth))
        self.client.capabilities['merchant']={'available':True,'native_verified':True}
        self.service._managed_refresh_control()
        cfg=self.settings(farming=farming);cfg['merchant']['enabled']=True
        cfg['merchant']['weights'].update({'2':10,'7':10})
        self.service.save_managed(cfg);self.service.set_managed_running(True)
        def native(kind,payload):
            if kind=='merchant_refresh':
                return {'completed':True,'effect_confirmed':False,'merchant_refresh_pending':True,
                        'before_shards':growth['soul_shards']['current']}
            if kind=='merchant_close':growth['merchant']['open']=False
            if kind=='enter_map':
                self.client.in_town=False;self.client.current_map=payload['map_id']
            return {'effect_confirmed':True}
        self.client.native_handler=native
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            for _ in range(5):
                self.service._managed_once()
                if self.service.managed.merchant_refresh_wait:break
        self.assertIsNotNone(self.service.managed.merchant_refresh_wait)
        self.assertEqual([a[0] for a in self.client.actions],['merchant_refresh'])
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed.await_effect)
        return growth

    def test_delayed_refill_polls_without_resubmit_and_preserves_purchase_ratio(self):
        from test_progression import state
        growth=self.start_delayed_merchant_refill()
        self.service.managed.merchant_counts={'2':6,'7':2}
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=180):
            for _ in range(8):self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions],['merchant_refresh'])
        self.assertTrue(self.service.managed.running)
        growth['merchant']['stock']=state()['merchant']['stock']
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=181):
            self.service._managed_once()
        self.assertIsNone(self.service.managed.merchant_refresh_wait)
        self.assertEqual(self.service.managed.merchant_counts,{'2':6,'7':2})
        action=self.service.managed.next_action(self.service._managed_observation())
        self.assertEqual(action['kind'],'merchant_buy');self.assertEqual(action['shop_slot'],2)

    def test_missing_refill_stock_skips_shopping_and_returns_to_farming(self):
        self.start_delayed_merchant_refill(farming=True)
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
            self.service._managed_once()
            self.assertTrue(self.service.managed.merchant_refresh_deferred)
            for _ in range(8):self.service._managed_once()
        self.assertTrue(self.service.managed.running,self.service.managed.status)
        kinds=[a[0] for a in self.client.actions]
        self.assertEqual(kinds.count('merchant_refresh'),1)
        self.assertNotIn('merchant_buy',kinds)
        self.assertIn('merchant_close',kinds)
        self.assertIn('enter_map',kinds)

    def test_refill_timeout_cleans_confirmed_gear_before_leaving(self):
        self.start_delayed_merchant_refill(farming=True)
        self.client.items=[equipment(item_uid='bought-before-refill',is_black_mist=True),
                           equipment(1,item_uid='bought-junk-before-refill')]
        self.service.managed.merchant_received_uids.update(('bought-before-refill','bought-junk-before-refill'))
        self.service.managed_merchant_uids.update(('bought-before-refill','bought-junk-before-refill'))
        self.service.managed.merchant_cleanup_pending=True
        self.service.managed.settings['cleanup'].update(equipment_action='store',equipment_fallback='salvage')
        original=self.client.native_handler
        def native(kind,payload):
            if kind in ('sell','salvage'):
                self.assertEqual(payload['item']['item_uid'],'bought-junk-before-refill')
                self.client.items=[r for r in self.client.items if r['item_uid']!='bought-junk-before-refill']
                return {'effect_confirmed':True,'quantity':1}
            return original(kind,payload)
        self.client.native_handler=native
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
            for _ in range(12):self.service._managed_once()
        kinds=[a[0] for a in self.client.actions]
        self.assertLess(kinds.index('merchant_close'),kinds.index('lock'))
        self.assertLess(kinds.index('lock'),kinds.index('transfer'))
        self.assertLess(kinds.index('transfer'),kinds.index('enter_map'))
        disposal=next(kind for kind in kinds if kind in ('sell','salvage'))
        self.assertLess(kinds.index('lock'),kinds.index(disposal))
        self.assertLess(kinds.index(disposal),kinds.index('enter_map'))
        self.assertEqual(kinds.count('merchant_refresh'),1)
        self.assertTrue(self.service.managed.running,self.service.managed.status)

    def test_refill_timeout_with_unreadable_state_pauses_with_original_fence(self):
        growth=self.start_delayed_merchant_refill()
        growth['available']=False
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
            self.service._managed_once()
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertTrue(self.service.managed_can_resume)
        self.assertIsNotNone(self.service.managed.merchant_refresh_wait)
        self.assertEqual([a[0] for a in self.client.actions],['merchant_refresh'])

    def test_refill_cannot_confirm_while_online_requests_are_pending(self):
        from test_progression import state
        growth=self.start_delayed_merchant_refill()
        growth['merchant']['stock']=state()['merchant']['stock']
        control=self.client.control_snapshot
        self.client.control_snapshot=lambda:dict(control(),online_pending_requests=1)
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=150):
            self.service._managed_once()
        self.assertIsNotNone(self.service.managed.merchant_refresh_wait)
        self.assertEqual([a[0] for a in self.client.actions],['merchant_refresh'])

    def test_refill_fence_survives_pause_resume_but_stop_clears_it(self):
        from test_progression import state
        growth=self.start_delayed_merchant_refill()
        self.service.managed.pause()
        self.service.managed.resume(self.service._managed_observation())
        growth['merchant']['stock']=state()['merchant']['stock']
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=150):
            self.service._managed_once()
        self.assertIsNone(self.service.managed.merchant_refresh_wait)
        self.assertTrue(self.service.managed.running)
        self.assertEqual([a[0] for a in self.client.actions],['merchant_refresh'])
        self.service.managed.merchant_refresh_wait={'action':{}}
        self.service.managed.stop()
        self.assertIsNone(self.service.managed.merchant_refresh_wait)

    def test_refill_currency_change_never_silently_authorizes_another_purchase(self):
        growth=self.start_delayed_merchant_refill()
        growth['soul_shards']['current']-=21
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=150):
            self.service._managed_once()
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertIsNotNone(self.service.managed.merchant_refresh_wait)
        self.assertEqual([a[0] for a in self.client.actions],['merchant_refresh'])

    def test_finish_request_settles_refill_then_cleans_without_new_purchases(self):
        from test_progression import state
        growth=self.start_delayed_merchant_refill()
        self.service.managed.request_finish()
        growth['merchant']['stock']=state()['merchant']['stock']
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=150):
            for _ in range(8):self.service._managed_once()
        self.assertFalse(self.service.managed.running,self.service.managed.status)
        kinds=[a[0] for a in self.client.actions]
        self.assertEqual(kinds.count('merchant_refresh'),1)
        self.assertNotIn('merchant_buy',kinds)
        self.assertNotIn('merchant_open',kinds)

    def test_merchant_and_farming_start_without_unrelated_skill_controller(self):
        from test_progression import state
        growth=state();growth['merchant'].update(unlock_known=True,unlocked=True)
        growth['skills']={'readable':False,'equipped':None,'available':[],
            'reason':'PlayerAbilityController 尚未加载。'}
        self.client.progression_snapshot=Mock(return_value=growth)
        for key in ('merchant','skills'):
            self.client.capabilities[key]={'available':True,'native_verified':True}
        self.service._managed_refresh_control()
        cfg=self.settings(farming=True);cfg['merchant']['enabled']=True
        cfg['merchant']['weights']['2']=1
        self.service.save_managed(cfg)
        self.assertTrue(self.service.managed_page()['can_start'])
        self.service.set_managed_running(True)
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.client.actions,[])
        self.service.stop_managed()
        cfg['progression']['skills_enabled']=True
        self.service.save_managed(cfg)
        page=self.service.managed_page()
        self.assertTrue(page['can_start'])
        self.assertEqual(page['blocked_reasons'],[])
        self.service.set_managed_running(True)
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.client.actions,[])

    def test_failed_growth_does_not_falsely_report_an_unread_merchant_as_locked(self):
        cfg=self.settings(farming=True);cfg['merchant']['enabled']=True;cfg['merchant']['weights']['2']=1
        self.service.managed.settings=cfg
        self.client.capabilities['merchant']={'available':True,'native_verified':True}
        self.service.managed_capabilities=self.client.managed_capabilities()
        self.service.managed_growth={'available':False,'reason':'PlayerAbilityController 尚未加载。'}
        self.assertEqual(self.service.managed_page()['blocked_reasons'],
            ['成长读取：PlayerAbilityController 尚未加载。'])

    def test_merchant_lookup_then_buys_cleans_and_refills_with_uidless_potions_left_in_bag(self):
        from test_progression import state
        from deskrawl_assistant.native_memory import GameBusyError
        kept=potion();self.client.items=[deepcopy(kept)]
        snapshot=self.client.snapshot
        def small_bag():
            value=snapshot();value['containers']['inventory']['slot_count']=4
            return value
        self.client.snapshot=small_bag
        growth=state();growth['merchant']['stock'][1].update(price=200,available_count=10)
        growth['merchant']['open']=False
        self.client.progression_snapshot=Mock(side_effect=lambda **kw:deepcopy(growth))
        self.client.capabilities['merchant']={'available':True,'native_verified':True}
        self.service._managed_refresh_control()
        cfg=self.settings();cfg['merchant']['enabled']=True;cfg['merchant']['weights']['2']=10
        cfg['background'].update(rule_monitoring=False,inventory_automation=False)
        cfg['cleanup'].update(equipment_action='store',equipment_fallback='salvage')
        self.service.save_managed(cfg);self.service.set_managed_running(True)
        bought=[];lookup_deferred=[]
        def native(kind,payload):
            if kind=='merchant_open' and not lookup_deferred:
                lookup_deferred.append(True)
                raise GameBusyError('神秘商人定位仍在进行，稍后自动继续；尚未提交购买。')
            if kind=='merchant_buy':
                self.assertTrue(growth['merchant']['open'])
                quantity=payload.get('quantity',1)
                occupied={r['slot_index'] for r in self.client.items if r['container']=='inventory'}
                free=[n for n in range(4) if n not in occupied]
                self.assertEqual(quantity,len(free))
                uids=[]
                for slot in free[:quantity]:
                    uid='purchase-'+str(len(bought));bought.append(uid);uids.append(uid)
                    self.client.items.append(equipment(slot,item_uid=uid,instance_id=uid,
                        is_black_mist=len(bought)==1))
                growth['soul_shards']['current']-=payload['expected_price']*quantity
                return {'effect_confirmed':True,'merchant_open':True,'received_uids':uids,'quantity':quantity}
            if kind in ('merchant_close','merchant_open'):
                growth['merchant']['open']=kind=='merchant_open'
            else:
                self.assertEqual(kind,'salvage')
                self.assertFalse(growth['merchant']['open'])
                self.assertTrue(payload['item']['is_equipment'])
                self.assertFalse(payload['item']['is_black_mist'])
                self.client.items=[r for r in self.client.items if identity(r)!=identity(payload['item'])]
            return {'effect_confirmed':True,'quantity':1}
        self.client.native_handler=native
        for tick in range(50):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100+3*tick):
                self.service._work('managed',{})
            self.assertTrue(self.service.managed.running,self.service.managed.status)
            self.assertEqual(self.client.items[0],kept)
            if tick==0:
                self.assertEqual(self.service.managed_retry['action_kind'],'merchant_open')
                self.assertEqual(bought,[])
            if not self.service.managed.merchant_pending:break
        self.assertEqual(lookup_deferred,[True])
        self.assertFalse(self.service.managed.merchant_pending,self.service.managed.status)
        self.assertEqual(growth['soul_shards']['current'],100)  # Unspendable remainder at a 0% target.
        kinds=[a[0] for a in self.client.actions]
        self.assertEqual(kinds.count('merchant_buy'),3)
        self.assertEqual(kinds.count('salvage'),8)
        self.assertLess(kinds.index('lock'),kinds.index('salvage'))
        self.assertEqual([r for r in self.client.items if r['container']=='inventory'],[kept])
        stored=[r for r in self.client.items if r['container']=='storage']
        self.assertEqual(len(stored),1);self.assertTrue(stored[0]['locked']);self.assertTrue(stored[0]['is_black_mist'])

    def test_merchant_fresh_preflight_still_blocks_new_gear_and_uncollected_rewards(self):
        from test_progression import state
        growth=state();self.client.items=[potion()]
        self.client.progression_snapshot=Mock(side_effect=lambda **kw:deepcopy(growth))
        self.client.capabilities['merchant']={'available':True,'native_verified':True}
        self.service._managed_refresh_control()
        cfg=self.settings();cfg['merchant']['enabled']=True;cfg['merchant']['weights']['2']=1
        self.service.save_managed(cfg);self.service.set_managed_running(True)
        world=self.service._managed_observation();action=self.service.managed.next_action(world)
        self.assertEqual(action['kind'],'merchant_buy')
        self.client.items.append(equipment(1,modifiers_complete=False))
        result=self.service._managed_execute(action,world)
        self.assertTrue(result['retryable']);self.assertEqual(self.client.actions,[])
        self.client.items=[]
        self.client.ground=[equipment(2,container='ground',selection_id='new-drop')]
        result=self.service._managed_execute(action,world)
        self.assertTrue(result['retryable']);self.assertEqual(self.client.actions,[])

    def test_merchant_lookup_wait_recovers_without_stopping_or_purchasing(self):
        from test_progression import state
        from deskrawl_assistant.native_memory import GameBusyError
        self.client.items=[]
        growth=state();growth['merchant']['open']=False
        self.client.progression_snapshot=Mock(side_effect=lambda **kw:deepcopy(growth))
        self.client.capabilities['merchant']={'available':True,'native_verified':True}
        self.service._managed_refresh_control()
        cfg=self.settings();cfg['merchant']['enabled']=True;cfg['merchant']['weights']['2']=1
        self.service.save_managed(cfg);self.service.set_managed_running(True)
        attempts=[]
        def native(kind,payload):
            self.assertEqual(kind,'merchant_open')
            attempts.append(kind)
            if len(attempts)==1:
                raise GameBusyError('神秘商人定位仍在进行，稍后自动继续；尚未提交购买。')
            growth['merchant']['open']=True
            return {'effect_confirmed':True}
        self.client.native_handler=native
        stats=deepcopy(self.service.managed.metrics.state())
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.error,'')
        self.assertIn('自动继续',self.service.managed.status)
        self.assertIsNone(self.service.managed.pending)
        self.assertEqual(self.service.managed_retry['action_kind'],'merchant_open')
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=101):
            self.service._work('managed',{})
        self.assertEqual(len(attempts),1)
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=103):
            self.service._work('managed',{})
        self.assertEqual(attempts,['merchant_open','merchant_open'])
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_retry)
        self.assertEqual(self.service.managed.metrics.state(),stats)
        self.assertNotIn('merchant_buy',[kind for kind,_ in self.client.actions])

    def test_disposal_reuses_receipt_and_wakes_next_step_with_fresh_inventory_and_scene(self):
        self.client.items=[equipment(0),equipment(1)]
        self.client.character_snapshot=Mock(return_value={'available':False,'equipment':[]})
        config=self.settings(farming=True,auto_equip=False);config['cleanup']['equipment_action']='sell'
        self.service.save_managed(config);self.service.set_managed_running(True)
        self.client.action_context_snapshot=Mock(return_value={'available':True,'scene':{'available':True,'in_town':True}})
        self.client.control_snapshot=Mock(wraps=self.client.control_snapshot)
        self.client.character_snapshot.reset_mock();self.client.captures=0
        def native(kind,payload):
            self.assertIn(kind,('sell','salvage'))
            self.client.items=[r for r in self.client.items if identity(r)!=identity(payload['item'])]
            return {'effect_confirmed':True,'quantity':1,'_receipt_snapshot':self.client.snapshot()}
        self.client.native_handler=native
        self.service._work('managed',{})
        self.assertEqual(self.client.captures,3)  # planning, fresh preflight, actual receipt
        self.assertEqual(self.client.character_snapshot.call_count,1)
        self.client.control_snapshot.assert_called_once()
        self.client.action_context_snapshot.assert_called_once()
        self.assertTrue(self.service.monitor_wake.is_set())
        self.assertEqual(len(self.service.state()['items']),1)
        event=next(r for r in self.service.journal.page(limit=50)['entries'] if r['event']=='managed_action')
        self.assertNotIn('_receipt_snapshot',event['context']['result'])

    def test_light_preflight_scene_change_does_not_dispose_outside_town(self):
        self.ready(farming=True,auto_equip=False)
        before=self.service._managed_observation();item=before['items'][0]
        self.client.action_context_snapshot=Mock(return_value={'available':True,'scene':{'available':True,'in_town':False,'in_dungeon':True}})
        result=self.service._managed_execute({'kind':'sell','item':item},before)
        self.assertTrue(result.get('retryable'));self.assertEqual(self.client.actions,[])

    def test_cancel_during_disposal_receipt_never_schedules_another_write(self):
        config=self.settings(farming=True,auto_equip=False);config['cleanup']['equipment_action']='sell'
        self.service.save_managed(config);self.service.set_managed_running(True)
        def native(kind,payload):
            self.service.stop_managed(pause=True)
            self.client.items=[]
            return {'effect_confirmed':True,'quantity':1,'_receipt_snapshot':self.client.snapshot()}
        self.client.native_handler=native
        self.service._work('managed',{})
        self.assertEqual(len(self.client.actions),1)
        self.assertFalse(self.service.managed.running)
        self.assertFalse(self.service.monitor_wake.is_set())

    def test_merchant_junk_never_uses_free_storage_and_receipt_survives_service_reload(self):
        item=equipment(0);self.client.items=[item]
        self.service.journal.append('system','managed_action','bought',context={
            'action':{'kind':'merchant_buy'},'result':{'verified':True,'received_uid':item['item_uid']}})
        self.service._init_managed()
        self.service._managed_refresh_control()
        cfg=self.settings(farming=True,auto_equip=False)
        cfg['cleanup'].update(equipment_action='store',equipment_fallback='salvage')
        self.service.save_managed(cfg);self.service.set_managed_running(True)
        def native(kind,payload):
            self.assertEqual(kind,'salvage');self.client.items=[]
            return {'effect_confirmed':True,'quantity':1}
        self.client.native_handler=native
        self.service._work('managed',{})
        self.assertEqual([a[0] for a in self.client.actions],['salvage'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.no_threads = patch('deskrawl_assistant.web_service.threading.Thread')
        self.no_threads.start()
        self.addCleanup(self.no_threads.stop)
        record = patch('deskrawl_assistant.web_service.record')
        record.start()
        self.addCleanup(record.stop)
        self.client = FakeManagedClient([equipment()])
        self.service = AssistantService(self.client,Path(self.tmp.name)/'rules.json')
        self.addCleanup(self.service.close)
        self.service._publish(self.client.snapshot())
        self.service._managed_refresh_control()

    def settings(self, *, farming=False, auto_equip=False):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings['auto_equip']['enabled'] = auto_equip
        settings['farming'].update(enabled=farming,dungeon_id='offline-map' if farming else None)
        # The synthetic player has pages 1 and 5 unlocked, but not default page 4.
        settings['cleanup']['rune_pages'] = [1]
        return settings

    def ready(self, *, farming=False, auto_equip=True):
        self.service.save_managed(self.settings(farming=farming,auto_equip=auto_equip))
        self.service.set_managed_running(True)

    def test_native_gold_and_other_drops_reach_page_stats_without_counting_potions(self):
        raw = self.client.control_snapshot()
        raw['gold_ledger'] = {'available': True, 'id': 'native-player-stats', 'total': 5000}
        with patch.object(self.client, 'control_snapshot', side_effect=lambda: deepcopy(raw)):
            self.service.managed.metrics.observe(self.service._managed_observation())
            self.client.items.extend([potion(1), potion(2, key='GoldBar', count=2)])
            raw['gold_ledger']['total'] += 321
            self.service.managed.metrics.observe(self.service._managed_observation())
        values = self.service.managed_page()['stats']
        self.assertEqual(values['gold']['count'], 321)
        self.assertEqual([(r['name_key'], r['count']) for r in values['others']], [('GoldBar', 2)])
        self.assertEqual(self.client.items[1]['count'], 14)
        self.assertEqual(self.client.actions, [])

    def test_other_drop_names_are_localized_including_previously_cached_labels(self):
        self.client.items = []
        self.service.managed.metrics.observe(self.service._managed_observation())
        counts = {'Rein_VolcanoCinderlash':6, 'Rein_VolcanoEmberBat':10, 'Rein_VolcanoMagmaGolem':13}
        names = {'Rein_VolcanoCinderlash':'焰鞭魔', 'Rein_VolcanoEmberBat':'余烬蝠',
                 'Rein_VolcanoMagmaGolem':'熔岩魔像'}
        self.client.items = [potion(index, key=key, count=count)
                             for index,(key,count) in enumerate(counts.items())]
        self.service.managed.metrics.observe(self.service._managed_observation())
        # Reproduce metrics cached before these labels were mapped.
        for metric_key,row in self.service.managed.metrics.rows.items():
            row['name'] = metric_key[1]
        rows = self.service.managed_page()['stats']['others']
        self.assertEqual({r['name_key']:r['name'] for r in rows}, names)
        self.assertEqual({r['name_key']:r['count'] for r in rows}, counts)
        self.assertTrue(all(r['sold']==0 and r['stored']==0 for r in rows))
        self.assertEqual(self.client.actions, [])

    def returning_world(self):
        self.client.items=[];self.client.in_town=False;self.client.current_map='offline-map'
        self.client.run_status='success'
        self.ready(farming=True,auto_equip=False)
        with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=0):
            self.service._managed_once()  # First empty reward confirmation.

    def test_return_preflight_replans_a_boss_win_or_drop_after_planning(self):
        self.client.items=[];self.client.in_town=False;self.client.current_map='offline-map'
        self.ready(farming=True,auto_equip=False)
        world=self.service._managed_observation()
        action=self.service.managed._action('return_town',reason='test pending return')
        self.client.run_status='success'
        with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=100):
            result=self.service._managed_execute(action,world)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])
        self.service.managed.pending=None
        world=self.service._managed_observation()
        with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=100):
            self.service.managed.next_action(world)
        with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=102):
            action=self.service.managed.next_action(world)
        self.assertEqual(action['kind'],'return_town')
        self.client.ground=[equipment(0,container='ground',selection_id='late-boss')]
        result=self.service._managed_execute(action,world)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_completed_run_pickup_uses_receipt_before_returning(self):
        self.client.items=[];self.client.in_town=False;self.client.current_map='offline-map'
        self.client.run_status='success'
        self.client.ground=[equipment(0,container='ground',selection_id='boss-drop')]
        self.ready(farming=True,auto_equip=False)
        def native(kind,payload):
            if kind=='collect_ground':
                row=self.client.ground.pop();row.update(container='inventory',slot_index=0)
                self.client.items.append(row)
                return {'effect_confirmed':True,'quantity':1}
            self.assertEqual(kind,'return_town')
            return {'transition_requested':True}
        self.client.native_handler=native
        with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=100):
            self.service._managed_once()
            self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions],['collect_ground'])
        with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=102):
            self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions],['collect_ground','return_town'])
        self.assertTrue(self.client.actions[-1][1]['protect_completion_rewards'])

    def test_death_flag_overrides_stale_running_plan_and_native_revive_resumes_farming(self):
        self.client.items=[];self.client.in_town=False;self.client.current_map='offline-map'
        raw_control=self.client.control_snapshot
        dead={'value':True}
        def control():
            value=raw_control();value['scene']['dead']=dead['value'];return value
        self.client.control_snapshot=control
        self.ready(farming=True,auto_equip=False)
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        self.service._managed_once()
        self.assertEqual(self.client.actions,[])
        self.assertIsNone(self.service.managed_transition)
        self.assertNotIn('offline-run',self.service.managed.handled_runs)
        self.assertIsNone(self.service.managed.last_success)
        for _ in range(3):self.service._managed_once()
        self.assertEqual(self.client.native_handler.call_count,0)
        dead['value']=False
        self.service._managed_once()
        self.assertIsNone(self.service.managed_transition);self.assertTrue(self.service.managed.running)
        self.assertEqual(self.client.actions,[])
        self.service._managed_once()
        self.assertIsNone(self.service.managed_transition);self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.managed.phase,'farming')

    def test_rejected_return_waits_then_requests_once_and_confirms_scene(self):
        self.returning_world()
        self.client.native_handler=Mock(side_effect=[NativeActionError('原生拒绝',state=REJECTED),
            {'transition_requested':True}])
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running);self.assertFalse(self.service.error)
        self.assertIsNone(self.service.managed.pending)
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=101):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=103):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,2)
        self.assertIsNotNone(self.service.managed_transition);self.assertIsNone(self.service.managed_retry)
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=104):
            self.service._work('managed',{})  # Await the accepted transition, without another request.
        self.assertEqual(self.client.native_handler.call_count,2)
        self.client.in_town=True
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=105):
            self.service._work('managed',{})
        self.assertIsNone(self.service.managed_transition)
        self.assertTrue(self.service.managed.running)

    def test_external_return_during_backoff_is_detected_without_replaying(self):
        self.returning_world()
        self.client.native_handler=Mock(side_effect=NativeActionError('原生拒绝',state=REJECTED))
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        self.client.in_town=True;self.client.items=[equipment(locked=True)]
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=101):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)
        self.assertIsNone(self.service.managed_retry)
        self.assertTrue(self.service.managed.running)

    def test_online_queue_waits_without_submitting_a_return_request(self):
        self.returning_world()
        raw=self.client.control_snapshot();raw['online_pending_requests']=2
        with patch.object(self.client,'control_snapshot',return_value=raw):
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running);self.assertFalse(self.service.error)
        self.assertEqual(self.client.actions,[])
        self.assertIn('待处理请求 2',self.service.managed.status)

    def test_online_queue_can_recover_after_two_minutes_without_restarting_managed(self):
        self.returning_world()
        raw = self.client.control_snapshot(); raw['online_pending_requests'] = 1
        self.client.native_handler = Mock(return_value={'transition_requested': True})
        initial_metrics = self.service.managed.metrics.state()
        with patch.object(self.client,'control_snapshot',side_effect=lambda:deepcopy(raw)):
            for now in (100, 221, 600):
                with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                    self.service._work('managed',{})
                self.assertTrue(self.service.managed.running)
                self.assertIsNone(self.service.managed_transition)
                self.assertIsNone(self.service.managed_return_recovery)
                self.assertIsNone(self.service.managed.pending)
                self.assertEqual(self.client.actions,[])
            self.assertIn('已等待 500 秒',self.service.managed.status)
            self.assertEqual(self.service.managed_retry['key'][0],'online_sync_pending')
            raw['online_pending_requests'] = 0
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=601):
                self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)
        self.assertIsNotNone(self.service.managed_transition)
        self.assertIsNone(self.service.managed_retry)
        self.assertEqual(self.service.managed.metrics.state(),initial_metrics)

    def test_sync_recovery_replans_when_native_game_already_returned_to_town(self):
        self.returning_world()
        raw = self.client.control_snapshot(); raw['online_pending_requests'] = 1
        with patch.object(self.client,'control_snapshot',return_value=raw):
            for now in (100, 221):
                with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                    self.service._work('managed',{})
        self.client.in_town = True
        self.client.items = [equipment(locked=False, is_black_mist=True)]
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=222):
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_retry)
        self.assertEqual([action[0] for action in self.client.actions],['lock'])

    def test_pending_queue_wait_does_not_bypass_interrupted_return_deadline(self):
        self.returning_world()
        self.service.managed_return_recovery = {'started':100, 'interruptions':1}
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
            self.service._managed_defer({'kind':'return_town'},'在线请求尚未完成','online_sync_pending')
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertEqual(self.client.actions,[])

    def test_native_sync_gate_is_retryable_but_generic_busy_keeps_timeout(self):
        self.returning_world()
        with patch.object(self.client,'execute_game_action',side_effect=OnlineSyncBusyError('等待在线同步')):
            for now in (100, 221):
                with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                    self.service._work('managed',{})
        self.assertTrue(self.service.managed.running)
        self.service.managed_retry = None
        with patch.object(self.client,'execute_game_action',side_effect=GameBusyError('传送状态异常')):
            for now in (300, 421):
                with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                    self.service._work('managed',{})
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertEqual(self.client.actions,[])

    def test_user_stop_during_online_wait_does_not_submit_travel(self):
        self.returning_world()
        raw = self.client.control_snapshot(); raw['online_pending_requests'] = 1
        with patch.object(self.client,'control_snapshot',return_value=raw):
            self.service._work('managed',{})
        self.service.stop_managed(pause=True)
        self.service._work('managed',{})
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.actions,[])

    def test_scene_transition_waits_without_duplicate_return_request(self):
        self.returning_world()
        raw=self.client.control_snapshot();raw['teleporting']=True
        with patch.object(self.client,'control_snapshot',return_value=raw):
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running);self.assertEqual(self.client.actions,[])

    def test_accepted_transition_can_wait_for_busy_game_but_never_replays(self):
        self.returning_world()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        raw=self.client.control_snapshot();raw['teleporting']=True
        with patch.object(self.client,'control_snapshot',return_value=raw):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=160):
                self.service._work('managed',{})
            self.assertTrue(self.service.managed.running)
            self.assertTrue(self.service.managed_transition['extended'])
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
                self.service._work('managed',{})
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.native_handler.call_count,1)

    def test_user_stop_cancels_deferred_return(self):
        self.returning_world()
        self.client.native_handler=Mock(side_effect=NativeActionError('原生拒绝',state=REJECTED))
        self.service._work('managed',{})
        self.assertIsNotNone(self.service.managed_retry)
        self.service.stop_managed()
        self.assertIsNone(self.service.managed_retry)
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.native_handler.call_count,1)

    def test_repeated_return_rejection_pauses_and_preserves_session_statistics(self):
        self.returning_world()
        self.client.items.append(equipment(9))
        self.client.native_handler=Mock(side_effect=NativeActionError('原生拒绝',state=REJECTED))
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        totals=deepcopy(self.service.managed.metrics.state())
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
            self.service._work('managed',{})
        self.assertFalse(self.service.managed.running);self.assertEqual(self.service.managed.phase,'paused')
        self.assertTrue(self.service.managed_can_resume)
        self.assertEqual(self.service.managed.metrics.state(),totals)
        self.assertIn('统计已保留',self.service.managed.status)

    def test_preinvoke_native_guard_failure_replans_and_rechecks_item_protection(self):
        cfg=self.settings(farming=True)
        cfg['cleanup']['equipment_policies']['legendary']={'action':'salvage','fallback':'salvage'}
        self.client.items[0]['locked']=False
        self.service.save_managed(cfg);self.service.set_managed_running(True)
        self.client.native_handler=Mock(side_effect=NativeActionError('指令未执行',state=GUARD_FAILED))
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running);self.assertIsNone(self.service.managed.pending)
        self.client.items[0]['base_rarity']=4
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=103):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)
        self.assertEqual(self.client.actions[-1][0],'lock')

    def test_uncertain_return_and_destructive_rejections_are_never_retried(self):
        for returning,uncertain in ((True,True),(False,False)):
            with self.subTest(returning=returning):
                self.service.stop_managed()
                self.client.in_town=not returning;self.client.items=[equipment(locked=False)]
                self.client.current_map='offline-map';self.client.run_status='success'
                cfg=self.settings(farming=True)
                cfg['cleanup']['equipment_policies']['legendary']={'action':'salvage','fallback':'salvage'}
                self.service.save_managed(cfg);self.service.set_managed_running(True)
                if returning:
                    with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=0):
                        self.service._managed_once()
                self.client.native_handler=Mock(side_effect=NativeActionError('拒绝',state=REJECTED,uncertain=uncertain))
                self.service._work('managed',{})
                self.assertFalse(self.service.managed.running)
                self.assertIsNone(self.service.managed_retry)
                self.assertEqual(self.client.native_handler.call_count,1)

    def test_transfer_prewrite_change_keeps_running_and_rechecks_protection(self):
        self.client.items[0]['locked'] = False
        self.ready(farming=True,auto_equip=False)
        def changed_before_write(*args,**kwargs):
            self.client.items[0].update(base_rarity=4)
            raise SnapshotChangedError('所选物品已变化或移动，请刷新后重新选择')
        with patch.object(self.client,'move_items',side_effect=changed_before_write) as move:
            self.service._work('managed',{})
        move.assert_called_once()
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed.pending)
        self.assertFalse(self.service.error)
        self.assertEqual(self.client.actions,[])
        self.service._work('managed',{})
        self.assertEqual(self.client.actions,[('lock',self.client.items[0]['item_uid'])])
        self.assertTrue(self.service.managed.running)

    def test_unconfirmed_transfer_error_still_stops_without_replay(self):
        self.client.items[0]['locked'] = False
        self.ready(farming=True,auto_equip=False)
        with patch.object(self.client,'move_items',side_effect=RuntimeError('转移回读不一致')) as move:
            self.service._work('managed',{})
        move.assert_called_once()
        self.assertFalse(self.service.managed.running)
        self.assertIn('转移回读不一致',self.service.error)
        self.assertEqual(self.client.actions,[])

    def test_slow_prewrite_transfer_retries_fresh_after_backoff(self):
        self.client.items[0]['locked'] = False
        self.ready(farming=True,auto_equip=False)
        with patch.object(self.client,'move_items',side_effect=GameBusyError('验证耗时异常，未移动物品')) as move:
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
                self.service._work('managed',{})
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=101):
                self.service._work('managed',{})
        move.assert_called_once()
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed.pending)
        self.assertIsNotNone(self.service.managed_retry)
        self.assertEqual(self.client.actions,[])
        self.assertEqual(self.client.items[0]['container'],'inventory')
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=103):
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_retry)
        self.assertEqual([a[0] for a in self.client.actions],['transfer'])
        self.assertEqual(self.client.items[0]['container'],'storage')

    def early_game(self, rarity=0, *, worn_key=None, restricted=True):
        """Exercise real adapter/preview/executor plumbing with synthetic gear."""
        candidate = equipment(key='CommonBelt1', rarity=rarity, equip_slot=6, class_mask=15,
            socket_count=0, socketed_gems=[], modifiers=[{'stat':1,'type':0,'value':50}])
        self.client.items = [candidate]
        worn = [equipment(container='equipped', key=worn_key, equipped_slot=5,
            equip_slot=6, class_mask=15, socket_count=0, socketed_gems=[],
            modifiers=[{'stat':1,'type':0,'value':10}])] if worn_key else []
        model = {'base':{1:100,4:.2,5:1.5}, 'caches':{536:{20:100,21:1,1:10 if worn else 0}},
                 'status':({}, {}, {}), 'conditional':({}, {}), 'conversions':[]}
        stats = final_stats(model)
        self.hero = {'available':True, 'session':'offline-session', 'captured_at':'synthetic',
            'level':1, 'max_level':100,
            'equipment':worn, 'class_mask':4, 'primary':1, 'stats':stats, 'model':model,
            'comparison_model':model, 'attack':panel_attack(stats,1)}
        self.client.character_snapshot = Mock(side_effect=lambda:deepcopy(self.hero))
        config = self.settings(auto_equip=True)
        if restricted: config['auto_equip']['slot_locks'] = {'5':'LegendaryBelt3'}
        self.service.save_managed(config)
        self.service.set_managed_running(True)
        return self.service._managed_observation()

    def test_early_game_qualities_are_read_scored_and_equipped_without_affix_rules(self):
        for rarity in (0,1,2):
            with self.subTest(rarity=rarity):
                observation = self.early_game(rarity,worn_key='CommonBelt2')
                row = observation['items'][0]
                self.assertEqual(row['rule_status'],'no_rule'); self.assertFalse(row['matches'])
                self.assertTrue(row['power']['available']); self.assertGreater(row['power']['delta'],0)
                action = self.service.managed.next_action(observation)
                self.assertEqual(action['kind'],'equip')
                result = self.service._managed_execute(action,observation)
                self.assertTrue(result['verified'])
                self.assertEqual(self.client.actions[-1][0],'equip')
                self.assertEqual(self.client.actions[-1][1]['expected_replaced_uid'],self.hero['equipment'][0]['item_uid'])
                self.service.stop_managed()

    def test_planned_transitional_equip_is_rejected_after_player_wears_target_legendary(self):
        observation = self.early_game(worn_key='CommonBelt2')
        action = self.service.managed.next_action(observation)
        self.assertEqual(action['kind'],'equip')
        self.hero['equipment'][0].update(name_key='LegendaryBelt3',internal_name='LegendaryBelt3',
                                       item_uid='newly-worn-legendary',instance_id='newly-worn-legendary',rarity=3)
        result = self.service._managed_execute(action,observation)
        self.assertFalse(result['verified']); self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_executor_rechecks_excluded_slot_before_submitting_native_equip(self):
        observation = self.early_game(worn_key='CommonBelt2')
        action = self.service.managed.next_action(observation)
        self.assertEqual(action['kind'],'equip')
        self.service.managed.settings['auto_equip']['disabled_slots'] = [5]
        result = self.service._managed_execute(action,observation)
        self.assertFalse(result['verified']); self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_fresh_divine_quality_rejects_an_already_planned_equip(self):
        observation = self.early_game(worn_key='CommonBelt2',restricted=False)
        action = self.service.managed.next_action(observation)
        self.assertEqual(action['kind'],'equip')
        self.client.items[0]['rarity'] = 4
        result = self.service._managed_execute(action,observation)
        self.assertFalse(result['verified']); self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_fresh_divine_quality_rejects_storage_candidate_preparation(self):
        self.early_game(worn_key='CommonBelt2',restricted=False)
        self.client.items[0]['container'] = 'storage'
        observation = self.service._managed_observation()
        action = self.service.managed.next_action(observation)
        self.assertEqual(action['kind'],'transfer'); self.assertEqual(action['reason_code'],'equip_prepare')
        self.client.items[0]['rarity'] = 4
        result = self.service._managed_execute(action,observation)
        self.assertFalse(result['verified']); self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_disabled_slot_settings_are_saved_and_loaded_with_existing_named_lock(self):
        config = self.settings(auto_equip=True)
        config['auto_equip'].update(disabled_slots=[5,6],slot_locks={'5':'LegendaryBelt3'})
        self.service.save_managed(config)
        saved = json.loads(self.service.managed_config.read_text(encoding='utf-8'))
        self.assertEqual(saved['auto_equip']['disabled_slots'],[5,6])
        self.assertEqual(saved['auto_equip']['slot_locks'],{'5':'LegendaryBelt3'})
        self.service._init_managed()
        self.assertEqual(self.service.managed.settings['auto_equip'],saved['auto_equip'])
        self.assertEqual(self.service.managed_config_error,'')
        self.assertEqual(self.client.actions,[])

    def test_planned_transitional_equip_waits_if_current_character_cannot_be_read(self):
        observation = self.early_game()
        action = self.service.managed.next_action(observation)
        self.assertEqual(action['kind'],'equip')
        self.hero = {'available':False,'equipment':[],'reason':'角色正在变化'}
        result = self.service._managed_execute(action,observation)
        self.assertFalse(result['verified']); self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_unrestricted_slot_can_execute_other_legendary_upgrade(self):
        observation = self.early_game(3,worn_key='LegendaryBelt2',restricted=False)
        self.client.items[0].update(name_key='LegendaryBelt1',internal_name='LegendaryBelt1')
        observation = self.service._managed_observation()
        action = self.service.managed.next_action(observation)
        self.assertEqual(action['kind'],'equip')
        self.assertTrue(self.service._managed_execute(action,observation)['verified'])
        self.assertEqual(self.client.actions[-1][1]['item']['name_key'],'LegendaryBelt1')

    def test_unverified_native_caps_reject_start_without_writing(self):
        self.service.save_managed(self.settings(auto_equip=True))
        for value in ({'available':False,'reason':'待验证'}, {'available':True,'native_verified':False}):
            self.client.capabilities['equip'] = value
            self.service._managed_refresh_control()
            with self.assertRaisesRegex(ValidationError,'尚未核验'):
                self.service.set_managed_running(True)
            self.assertFalse(self.service.managed.running)
            self.assertFalse(self.service.busy)
            self.assertEqual(self.client.actions,[])

    def test_unreadable_campaign_blocks_farming_and_exposes_reason(self):
        self.service.save_managed(self.settings(farming=True))
        with patch.object(self.client,'control_snapshot',return_value={'available':False,'maps':[],
                'reason':'关卡记录尚未加载。'}):
            self.service._managed_refresh_control()
            page=self.service.managed_page()
            self.assertFalse(page['can_start'])
            self.assertFalse(page['control_available'])
            self.assertEqual(page['control_error'],'关卡记录尚未加载。')
            with self.assertRaisesRegex(ValidationError,'关卡记录尚未加载'):
                self.service.set_managed_running(True)
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.actions,[])
        self.service._managed_refresh_control()
        page=self.service.managed_page()
        self.assertTrue(page['can_start'])
        self.assertTrue(page['maps'])
        self.assertEqual(page['control_error'],'')

    def test_refreshed_capability_revocation_rejects_start(self):
        self.service.save_managed(self.settings(auto_equip=True))
        self.assertTrue(self.service.managed_page()['can_start'])
        self.client.capabilities['equip'] = {'available':False,'native_verified':False,'reason':'版本变化'}
        with self.assertRaisesRegex(ValidationError,'能力发生变化'):
            self.service.set_managed_running(True)
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.actions,[])

    def test_explicit_local_test_capability_can_start_without_claiming_verified(self):
        self.client.capabilities['equip'] = {'available':True,'native_verified':False,'test_enabled':True}
        self.service._managed_refresh_control()
        self.ready(auto_equip=True)
        state = self.service.managed_page()
        self.assertTrue(state['running'])
        self.assertFalse(state['capabilities']['equip']['native_verified'])
        self.assertEqual(self.client.actions,[])

    def test_release_enabled_capability_can_start_without_test_mode_or_claiming_verified(self):
        self.client.capabilities['equip'] = {'available':True,'native_verified':False,
            'release_enabled':True,'test_enabled':False}
        self.service._managed_refresh_control()
        self.ready(auto_equip=True)
        state = self.service.managed_page()
        self.assertTrue(state['running'])
        self.assertFalse(state['capabilities']['equip']['native_verified'])
        self.assertFalse(state['capabilities']['equip']['test_enabled'])
        self.assertEqual(self.client.actions,[])

    def test_cancel_during_start_clears_pending_stop_before_new_start(self):
        self.service.save_managed(self.settings(auto_equip=True))
        original = self.service._managed_observation
        def cancelled_read():
            self.service.stop_managed()
            return original()
        with patch.object(self.service,'_managed_observation',side_effect=cancelled_read):
            with self.assertRaisesRegex(ValidationError,'启动已取消'):
                self.service.set_managed_running(True)
        self.assertFalse(self.service.busy)
        self.assertIsNone(self.service.operation_kind)
        self.assertIsNone(self.service.managed_stop_request)
        self.assertTrue(self.service.set_managed_running(True)['running'])

    def test_start_stops_old_writers_and_managed_rejects_manual_operations(self):
        self.service.monitoring = True
        self.service.automation_running = True
        self.ready()
        self.assertFalse(self.service.monitoring)
        self.assertFalse(self.service.automation_running)
        for operation in ('read','unlock_all','disconnect'):
            with self.subTest(operation=operation),self.assertRaisesRegex(ValidationError,'暂停托管'):
                self.service.start(operation)
        for start in (self.service.set_monitoring,self.service.set_automation_running):
            with self.assertRaisesRegex(ValidationError,'托管正在运行'): start(True)
        with self.assertRaisesRegex(ValidationError,'暂停托管'):
            self.service.save_managed(self.settings(auto_equip=True))

    def test_no_rule_is_disposable_only_after_complete_equipment_classification(self):
        row = self.service._managed_observation()['items'][0]
        self.assertEqual(row['rule_status'],'no_rule')
        self.assertFalse(row['review'])
        self.assertTrue(row['rules_known'])
        self.assertTrue(self.service.managed._disposable(row))
        self.client.items[0]['modifiers_complete'] = False
        self.client.items[0]['read_evidence']['modifiers_complete'] = False
        uncertain = self.service._managed_observation()['items'][0]
        self.assertEqual(uncertain['rule_status'],'review')
        self.assertTrue(uncertain['review'])
        self.assertFalse(self.service.managed._disposable(uncertain))

    def test_start_cannot_choose_locked_difficulty_or_wrong_tier_progress(self):
        settings = self.settings(farming=True); settings['farming']['difficulty']='Nightmare'
        self.client.difficulties[1]['unlocked'] = False
        self.service._managed_refresh_control(); self.service.save_managed(settings)
        with self.assertRaisesRegex(ValidationError,'目标难度尚未解锁'):
            self.service.set_managed_running(True)
        self.assertEqual(self.client.actions,[])
        self.client.difficulties[1]['unlocked'] = True
        raw = self.client.control_snapshot(); raw['maps_by_difficulty']['Nightmare'][0]['unlocked']=False
        with patch.object(self.client,'control_snapshot',return_value=raw):
            self.service._managed_refresh_control()
            with self.assertRaisesRegex(ValidationError,'目标关卡尚未解锁'):
                self.service.set_managed_running(True)
        self.assertEqual(self.client.actions,[])

    def test_campaign_route_follows_real_chain_then_next_tier_from_first_map(self):
        route = campaign_route(self.service.managed_catalog,'Volcano7','Normal')
        self.assertEqual(route[:2],[{'dungeon_id':'Volcano7','difficulty':'Normal'},
                                   {'dungeon_id':'Forest1','difficulty':'Nightmare'}])
        boundary = next(n for n,row in enumerate(route) if row['difficulty']=='Inferno')
        self.assertEqual(route[boundary-1],{'dungeon_id':'Volcano7','difficulty':'Nightmare'})
        self.assertEqual(route[boundary],{'dungeon_id':'Forest1','difficulty':'Inferno'})
        self.assertEqual(campaign_route(self.service.managed_catalog,'Volcano7','Inferno'),
                         [{'dungeon_id':'Volcano7','difficulty':'Inferno'}])

    def test_growth_presets_and_purchase_counts_survive_save_and_reload_for_all_classes(self):
        from deskrawl_assistant.progression import catalog
        settings=self.settings()
        for mask,hero in catalog()['heroes'].items():
            node=next(n for n in hero['talents'] if n['point_requirement']==0 and not n['prerequisite'])
            ability=next(n for n in hero['abilities'] if 0 in n['slots'])
            settings['progression']['talent_targets'][mask]={node['id']:2}
            settings['progression']['skill_stages'][mask]=[{'level':1,'slots':[ability['id'],None,None,None]}]
        settings['life_skills']['priorities']=['LifeSkill 66','LifeSkill 31']
        settings['merchant'].update(trigger_percent=93,spend_to_percent=17)
        settings['merchant']['weights'].update({'2':10,'3':1})
        saved=self.service.save_managed(settings)
        self.assertEqual(json.loads(self.service.managed_config.read_text(encoding='utf-8')),saved)
        self.service._init_managed()
        self.assertEqual(self.service.managed_config_error,'')
        self.assertEqual(self.service.managed.settings,saved)
        self.assertEqual((saved['merchant']['trigger_percent'],saved['merchant']['spend_to_percent']),(93,17))
        self.assertEqual(self.client.actions,[])

    def test_price_previews_are_read_even_when_automatic_purchases_are_disabled(self):
        self.assertFalse(self.service.managed.settings['merchant']['enabled'])
        self.client.progression_snapshot=Mock(return_value={'available':True,'merchant':{'preview':[]}})
        self.service._managed_refresh_control()
        self.client.progression_snapshot.assert_called_once_with(merchant=True)
        self.assertFalse(self.service.managed.settings['merchant']['enabled']);self.assertEqual(self.client.actions,[])

    def test_locked_or_unknown_merchant_cannot_be_enabled_and_legacy_config_cannot_start(self):
        settings=self.settings(auto_equip=True);settings['merchant']['enabled']=True
        settings['merchant']['weights']['2']=10
        for shop in ({},{'unlock_known':True,'unlocked':False},{'unlock_known':False,'unlocked':True}):
            self.service.managed_growth={'available':True,'merchant':shop}
            with self.assertRaisesRegex(ValidationError,'不能启用'):self.service.save_managed(settings)
        self.service.managed.settings=settings
        self.assertTrue(any('尚未解锁' in reason for reason in self.service.managed_page()['blocked_reasons']))
        self.assertEqual(self.client.actions,[])
        self.service.managed_growth={'available':True,'gold':0,'merchant':{'unlock_known':True,'unlocked':True}}
        # Already paid for the unlock: current gold no longer controls access.
        self.assertTrue(self.service.save_managed(settings)['merchant']['enabled'])

    def test_push_start_uses_highest_native_unlock_not_legacy_selected_map(self):
        self.client.maps=[{'id':key,'unlocked':True,'can_enter':True,'supported':True} for key in ('Forest1','Volcano7')]
        self.service._managed_refresh_control()
        settings=self.settings(farming=True);settings['farming'].update(mode='push',dungeon_id=None)
        self.service.save_managed(settings)
        self.assertEqual(self.service.managed_page()['automatic_start'],{'dungeon_id':'Volcano7','difficulty':'Inferno'})
        self.service.set_managed_running(True)
        self.assertEqual((self.service.managed.target_dungeon,self.service.managed.target_difficulty),('Volcano7','Inferno'))
        self.assertEqual(self.service.managed.settings['farming']['progression'][0],{'dungeon_id':'Forest1','difficulty':'Normal'})
        self.assertEqual(self.client.actions,[]) # Starting itself never executes a game action.

    def test_repeat_start_validates_both_maps_and_resolved_difficulty_duplicates(self):
        settings=self.settings(farming=True)
        settings['farming']['repeat_maps']=[{'dungeon_id':'offline-map','difficulty':'current','runs':2},
                                          {'dungeon_id':'offline-map','difficulty':'Nightmare','runs':3}]
        self.client.difficulties[1]['unlocked']=False;self.service._managed_refresh_control()
        self.service.save_managed(settings)
        with self.assertRaisesRegex(ValidationError,'目标难度尚未解锁'):self.service.set_managed_running(True)
        settings['farming']['repeat_maps'][1]['difficulty']='Normal'
        self.service.save_managed(settings)
        with self.assertRaisesRegex(ValidationError,'关卡不能相同'):self.service.set_managed_running(True)
        self.assertFalse(self.service.managed.running);self.assertEqual(self.client.actions,[])

    def test_resume_after_crossing_difficulty_keeps_original_route(self):
        self.client.maps = [{'id':key,'map_id':key,'unlocked':True,'can_enter':True,'supported':True}
                            for key in ('Volcano7','Forest1')]
        self.service._managed_refresh_control()
        settings = self.settings(farming=True); settings['farming'].update(mode='push',dungeon_id='Volcano7')
        self.service.save_managed(settings); self.service.set_managed_running(True)
        original = deepcopy(self.service.managed.settings['farming']['progression'])
        self.service.managed.target_dungeon='Forest1'; self.service.managed.target_difficulty='Nightmare'
        self.service.stop_managed(pause=True)
        self.client.difficulty='Nightmare'
        self.service.set_managed_running(True)
        self.assertEqual(self.service.managed.settings['farming']['progression'],original)
        self.assertEqual(self.service.managed.target_difficulty,'Nightmare')

    def test_native_town_without_run_is_normalised_before_planning(self):
        for scene in ('town',{'available':True,'in_town':True,'run':None}):
            with self.subTest(scene=scene):
                raw = self.client.control_snapshot()
                raw.update(scene=scene,run=None)
                with patch.object(self.client,'control_snapshot',return_value=raw):
                    self.service._managed_refresh_control()
                    self.assertEqual(self.service._managed_scene()['run'],{})
                    self.ready(farming=True,auto_equip=False)
                    self.service._managed_once()
                    self.assertTrue(self.service.managed.running)
                    self.service.stop_managed()

    def test_unexpected_worker_failure_retains_traceback_in_local_log(self):
        self.ready()
        with patch.object(self.service,'_managed_once',side_effect=AttributeError('synthetic failure')), \
                patch('deskrawl_assistant.web_service.record') as log:
            self.service._work('managed',{})
        failures = [call.kwargs for call in log.call_args_list if call.args[0]=='web_operation_failed']
        self.assertEqual(len(failures),1)
        self.assertEqual(failures[0]['error_type'],'AttributeError')
        self.assertIn('Traceback (most recent call last)',failures[0]['traceback'])
        self.assertIn('synthetic failure',failures[0]['traceback'])
        self.assertFalse(self.service.managed.running)

    def test_configuration_error_never_becomes_no_rule_disposal(self):
        self.service.config_error = '规则文件损坏'
        row = self.service._managed_observation()['items'][0]
        self.assertFalse(row['rules_known'])
        self.assertTrue(row['review'])
        self.assertEqual(row['rule_status'],'review')
        self.assertFalse(self.service.managed._disposable(row))

    def test_non_contiguous_warehouse_pages_report_true_capacity_and_free_space(self):
        self.client.items = [equipment(200,container='storage')]
        self.service._publish(self.client.snapshot())
        pages = self.service.managed_page()['warehouse_pages']
        self.assertEqual([page['index'] for page in pages],[1,5])
        self.assertEqual([page['capacity'] for page in pages],[50,50])
        self.assertEqual([page['free'] for page in pages],[50,49])
        observation = self.service._managed_observation()
        self.assertEqual(self.service.managed._targets(observation,[5]),list(range(200,250)))

    def test_locked_only_destinations_reject_save_and_block_old_config_start(self):
        settings = self.settings(farming=True)
        settings['cleanup']['equipment_pages'] = [2]
        with self.assertRaisesRegex(ValidationError, '装备.*已解锁'):
            self.service.save_managed(settings)
        # Loaded legacy selections stay visible so the player can correct them.
        self.service.managed.settings = settings
        page = self.service.managed_page()
        self.assertFalse(page['can_start'])
        self.assertIn('装备', '；'.join(page['blocked_reasons']))
        with self.assertRaisesRegex(ValidationError, '装备.*已解锁'):
            self.service.set_managed_running(True)
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.settings['cleanup']['equipment_pages'],[2])
        self.assertEqual(self.client.actions,[])

    def test_verified_repeat_without_sale_does_not_require_unverified_difficulty_switch(self):
        settings=self.settings(farming=True)
        settings['cleanup'].update(equipment_action='salvage',equipment_fallback='salvage')
        for key in ('sell','difficulty'):
            self.client.capabilities[key]={'available':False,'native_verified':False}
        self.service._managed_refresh_control()
        self.service.save_managed(settings)
        self.assertNotIn('difficulty',required_capabilities(self.service.managed.settings))
        self.service.set_managed_running(True)
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.client.actions,[])
        self.service.stop_managed()
        settings['farming']['difficulty']='Nightmare'
        self.service.save_managed(settings)
        with self.assertRaisesRegex(ValidationError,'difficulty'):
            self.service.set_managed_running(True)

    def test_slot_lock_requires_same_legendary_slot_with_independent_rings(self):
        settings = self.settings(auto_equip=True)
        necklaces = [key for key,meta in self.service.ui['equipment'].items() if key.startswith('Legendary') and meta['slot']=='Necklace']
        rings = [key for key,meta in self.service.ui['equipment'].items() if key.startswith('Legendary') and meta['slot']=='Ring']
        settings['auto_equip']['slot_locks'] = {'8':necklaces[0],'6':rings[0],'7':rings[1]}
        self.service.save_managed(settings)
        self.assertEqual(self.service.managed.settings['auto_equip']['slot_locks'],settings['auto_equip']['slot_locks'])
        settings['auto_equip']['slot_locks']['8'] = 'LegendaryBelt3'
        with self.assertRaisesRegex(ValidationError,'对应的穿戴部位'):
            self.service.save_managed(settings)

    def test_transfer_rejects_indices_outside_selected_warehouse_pages(self):
        self.ready()
        observation = self.service._managed_observation()
        row = observation['items'][0]
        action = {'kind':'transfer','item':row,'target':'storage','target_indices':[200]}
        with self.assertRaisesRegex(ValidationError,'目标仓库页'):
            self.service._managed_execute(action,observation)
        self.assertEqual(self.client.actions,[])

    def test_fresh_inventory_lock_prevents_stale_planned_disposal(self):
        self.ready()
        observation = self.service._managed_observation()
        row = observation['items'][0]
        self.assertTrue(self.service.managed._disposable(row))
        self.client.items[0]['locked'] = True
        self.service._publish(self.client.snapshot())
        with self.assertRaises(ValidationError):
            self.service._managed_execute({'kind':'sell','item':row},observation)
        self.assertEqual(self.client.actions,[])

    def test_fresh_quality_policy_rejects_stale_sale_and_confirms_new_plan(self):
        self.client.items = [equipment(key='CommonBelt1',rarity=0)]
        config = self.settings(farming=True)
        config['cleanup']['equipment_policies'] = {
            'normal':{'action':'sell','fallback':'sell'},
            'rare':{'action':'salvage','fallback':'salvage'}}
        self.service.save_managed(config)
        self.service.set_managed_running(True)
        old = self.service._managed_observation()
        row = old['items'][0]
        self.client.items[0]['rarity'] = 2
        result = self.service._managed_execute({'kind':'sell','item':row},old)
        self.assertTrue(result.get('retryable'))
        self.assertFalse(result['verified'])
        self.assertEqual(self.client.actions,[])
        fresh = self.service._managed_observation()
        result = self.service._managed_execute({'kind':'salvage','item':fresh['items'][0]},fresh)
        self.assertTrue(result['verified'])
        self.assertEqual(self.client.actions[0][0],'salvage')

    def test_warehouse_fallback_uses_quality_fallback_instead_of_primary_action(self):
        self.client.items = [equipment(key='CommonBelt1',rarity=2)]
        config = self.settings(farming=True)
        config['cleanup']['equipment_policies'] = {'rare':{'action':'store','fallback':'salvage'}}
        self.service.save_managed(config)
        self.service.set_managed_running(True)
        observation = self.service._managed_observation()
        row = observation['items'][0]
        result = self.service._managed_execute({'kind':'salvage','item':row},observation)
        self.assertTrue(result.get('retryable'))
        self.assertEqual(self.client.actions,[])
        result = self.service._managed_execute({'kind':'salvage','item':row,
            'reason_code':'equipment_fallback'},observation)
        self.assertTrue(result['verified'])
        self.assertEqual(self.client.actions[0][0],'salvage')

    def test_new_protected_pickup_is_a_lock_barrier_for_already_planned_sale(self):
        self.ready(farming=True,auto_equip=False)
        observation = self.service._managed_observation()
        row = observation['items'][0]
        self.client.items.append(equipment(1,is_black_mist=True))
        result = self.service._managed_execute({'kind':'sell','item':row},observation)
        self.assertTrue(result.get('retryable'))
        self.assertFalse(result.get('verified'))
        self.assertEqual(self.client.actions,[])

    def test_hidden_black_mist_is_locked_and_stored_without_affix_verification(self):
        hidden=equipment(0,is_black_mist=True,modifiers=[])
        hidden['read_evidence']['native_affixes_verified']=False
        self.client.items=[hidden,equipment(1)]
        config=self.settings(farming=True)
        config['cleanup']['equipment_action']='salvage'
        self.service.save_managed(config);self.service.set_managed_running(True)
        world=self.service._managed_observation()
        self.assertTrue(world['items'][0]['review'])
        self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions],['lock'])
        self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions],['lock','transfer'])
        self.assertTrue(self.client.items[0]['locked'])
        self.assertEqual(self.client.items[0]['container'],'storage')
        self.assertEqual(self.client.items[0]['modifiers'],[])
        self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions],['lock','transfer','salvage'])

    def test_locked_hidden_black_mist_does_not_block_safe_capacity_disposal(self):
        hidden=equipment(0,is_black_mist=True,locked=True,modifiers=[])
        hidden['read_evidence']['native_affixes_verified']=False
        self.client.items=[hidden,equipment(1)]
        config=self.settings(farming=True)
        config['cleanup']['equipment_action']='sell'
        self.service.save_managed(config);self.service.set_managed_running(True)
        world=self.service._managed_observation()
        result=self.service._managed_execute({'kind':'sell','item':world['items'][1]},world)
        self.assertTrue(result['verified'])
        self.assertEqual(self.client.actions[0][0],'sell')

    def test_mandatory_hidden_gear_with_unverified_identity_never_locks_or_disposes(self):
        hidden=equipment(0,is_black_mist=True,modifiers=[])
        hidden['read_evidence'].update(native_affixes_verified=False,identity_verified=False)
        self.client.items=[hidden,equipment(1)]
        self.ready(farming=True,auto_equip=False)
        self.service._managed_once()
        self.assertEqual(self.client.actions,[])
        self.assertEqual(self.service.managed.phase,'paused')

    def test_native_capability_revoked_before_action_never_submits_sale(self):
        self.ready(farming=True,auto_equip=False)
        observation = self.service._managed_observation()
        row = observation['items'][0]
        self.client.capabilities['sell'] = {'available':False,'native_verified':False,'reason':'运行中兼容状态改变'}
        try:
            result = self.service._managed_execute({'kind':'sell','item':row},observation)
        except ValidationError:
            pass
        else:
            self.assertFalse(result.get('verified'))
        self.assertEqual(self.client.actions,[])

    def test_scene_transition_waits_for_fresh_scene_and_never_resubmits(self):
        self.client.items = []
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        self.client.native_handler = lambda kind,payload:{'bridge_verified':True,'transition_requested':True}
        self.service._managed_once()
        self.assertEqual([action[0] for action in self.client.actions],['enter_map'])
        self.assertIsNotNone(self.service.managed_transition)
        self.assertEqual(self.service.managed.pending['kind'],'enter_dungeon')
        for _ in range(6): self.service._managed_once()
        self.assertEqual([action[0] for action in self.client.actions],['enter_map'])
        self.assertTrue(self.service.managed.running)
        self.client.in_town = False
        self.client.current_map = 'offline-map'
        self.service._managed_once()
        self.assertIsNone(self.service.managed_transition)
        self.assertIsNone(self.service.managed.pending)
        self.assertEqual(self.service.managed.phase,'farming')
        self.assertEqual(len(self.client.actions),1)

    def test_entry_snapshot_race_keeps_running_then_enters_with_fresh_state(self):
        self.client.items = []
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        with patch.object(self.client,'execute_game_action',side_effect=SnapshotChangedError(
                '角色、场景或掉落正在变化，等待下一轮')) as execute:
            self.service._work('managed',{})
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0],'enter_map')
        self.assertTrue(self.service.managed.running)
        self.assertFalse(self.service.error)
        self.assertIsNone(self.service.managed.pending)
        self.assertIsNone(self.service.managed_transition)
        self.assertEqual(self.client.actions,[])
        self.client.native_handler = lambda kind,payload:{'bridge_verified':True,'transition_requested':True}
        self.service._work('managed',{})
        self.assertEqual([a[0] for a in self.client.actions],['enter_map'])
        self.assertIsNotNone(self.service.managed_transition)
        self.client.in_town = False
        self.client.current_map = 'offline-map'
        self.service._work('managed',{})
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_transition)
        self.assertEqual(self.service.managed.phase,'farming')
        self.assertEqual(len(self.client.actions),1)

    def test_scene_transition_timeout_stops_without_second_entry_request(self):
        self.client.items = []
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        self.client.native_handler = lambda kind,payload:{'bridge_verified':True,'transition_requested':True}
        self.service._work('managed',{})
        self.assertIsNotNone(self.service.managed_transition)
        deadline = self.service.managed_transition['deadline']
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=deadline+1):
            self.service._work('managed',{})
        self.assertFalse(self.service.managed.running)
        self.assertIn('关卡切换结果未确认',self.service.managed.status)
        self.assertEqual([action[0] for action in self.client.actions],['enter_map'])

    def test_completed_run_waits_for_actual_town_before_cleaning_or_reentering(self):
        self.client.items=[];self.client.in_town=False;self.client.current_map='offline-map'
        self.client.run_status='success'
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        self.client.native_handler=lambda kind,payload:{'transition_requested':True}
        with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=0):
            self.service._managed_once()
        self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions],['return_town'])
        self.assertEqual(self.service.managed_transition['requested_scene']['run_status'],'success')
        for _ in range(3):self.service._managed_once()
        self.assertEqual(len(self.client.actions),1)
        self.assertIsNotNone(self.service.managed_transition)
        self.client.in_town=True;self.client.current_map=None
        self.service._managed_once()
        self.assertIsNone(self.service.managed_transition)
        self.assertEqual(len(self.client.actions),1)
        self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions],['return_town','enter_map'])

    def test_cancelled_return_replans_native_repeat_without_cleanup_or_false_arrival(self):
        self.client.items=[];self.client.in_town=False;self.client.current_map='offline-map'
        self.client.run_status='success'
        self.service._publish(self.client.snapshot());self.ready(farming=True,auto_equip=False)
        self.client.native_handler=lambda kind,payload:{'transition_requested':True}
        with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=0):
            self.service._managed_once()
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        # The repeat restarted after a cancelled portal. Discard stale return
        # intent and reconsider this live run; never pretend town was visited.
        self.client.run_status='running'
        for now in (101,109,112):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._work('managed',{})
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.managed.phase,'farming')
        self.assertIsNone(self.service.managed_transition)
        self.assertEqual([a[0] for a in self.client.actions],['return_town'])

    def test_interrupted_return_retries_then_late_arrival_unlocks_cleanup(self):
        self.returning_world()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        for now in (100,101,109):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)
        self.assertIsNone(self.service.managed_transition)
        self.assertIsNone(self.service.managed.pending)
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=112):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,2)
        self.client.in_town=True;self.client.current_map=None
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=113):
            self.service._work('managed',{})
        self.assertEqual(self.service.managed.phase,'cleanup')
        self.assertIsNone(self.service.managed_return_recovery)
        self.assertTrue(self.service.managed.running)

    def test_late_town_arrival_during_interruption_backoff_never_replays_return(self):
        self.returning_world()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        for now in (100,101,109):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._work('managed',{})
        self.client.in_town=True;self.client.current_map=None
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=110):
            self.service._work('managed',{})
        self.assertEqual([a[0] for a in self.client.actions].count('return_town'),1)
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_return_recovery)

    def test_three_interrupted_returns_pause_and_resume_keep_statistics(self):
        self.returning_world()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        for now in (100,101,109,112,113,121,124,125,133):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,3)
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertTrue(self.service.managed_can_resume)
        self.assertIsNone(self.service.managed.pending)
        self.assertIsNone(self.service.managed_transition)
        stats=deepcopy(self.service.managed.metrics.state())
        self.client.in_town=True;self.client.current_map=None
        self.service.set_managed_running(True)
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.managed.metrics.state(),stats)
        self.assertEqual(self.service.managed.phase,'cleanup')

    def test_unknown_portal_flags_pause_without_retrying_an_unconfirmed_return(self):
        self.returning_world()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        raw=self.client.control_snapshot();raw.pop('teleporting')
        with patch.object(self.client,'control_snapshot',return_value=raw):
            for now in (101,109,146):
                with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                    self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertTrue(self.service.managed_can_resume)

    def test_interrupted_return_budget_survives_busy_wait_and_native_acceptance(self):
        self.returning_world()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        for now in (100,101,109):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._work('managed',{})
        raw=self.client.control_snapshot();raw['online_saving']=True
        with patch.object(self.client,'control_snapshot',return_value=raw), \
                patch('deskrawl_assistant.managed_service.time.monotonic',return_value=112):
            self.service._work('managed',{})
        self.assertEqual(self.service.managed_return_recovery['started'],100)
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=115):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)  # Reconfirm empty after sync.
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=117):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,2)
        raw=self.client.control_snapshot();raw['teleporting']=True
        with patch.object(self.client,'control_snapshot',return_value=raw), \
                patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,2)
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertTrue(self.service.managed_can_resume)

    def test_return_pause_resume_waits_for_a_still_active_portal_before_replanning(self):
        self.returning_world()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        raw=self.client.control_snapshot();raw['teleporting']=True
        with patch.object(self.client,'control_snapshot',return_value=raw):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
                self.service._work('managed',{})
            self.service.set_managed_running(True)
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.client.native_handler.call_count,1)
        self.client.in_town=True;self.client.current_map=None
        self.service._work('managed',{})
        self.assertEqual([action[0] for action in self.client.actions].count('return_town'),1)

    def test_interrupted_return_then_persistent_sync_pauses_without_another_request(self):
        self.returning_world()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        for now in (100,101,109):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._work('managed',{})
        raw=self.client.control_snapshot();raw['online_saving']=True
        with patch.object(self.client,'control_snapshot',return_value=raw):
            for now in (112,221):
                with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                    self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertTrue(self.service.managed_can_resume)

    def test_interrupted_graceful_stop_still_returns_and_never_reenters_a_map(self):
        self.returning_world()
        self.service.finish_managed()
        self.client.native_handler=Mock(return_value={'transition_requested':True})
        for now in (100,101,109,112):
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                self.service._work('managed',{})
        self.assertTrue(self.service.managed.finishing)
        self.assertEqual(self.client.native_handler.call_count,2)
        self.client.in_town=True;self.client.current_map=None
        for _ in range(8):
            if not self.service.managed.running:break
            self.service._work('managed',{})
        self.assertEqual(self.service.managed.phase,'stopped')
        self.assertEqual([action[0] for action in self.client.actions],['return_town','return_town'])

    def test_scene_transition_without_run_waits_for_active_run(self):
        self.client.items = []
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        self.client.native_handler = lambda kind,payload:{'transition_requested':True}
        self.service._managed_once()
        self.client.in_town = False
        self.client.current_map = 'offline-map'
        raw = self.client.control_snapshot(); raw['run'] = None
        with patch.object(self.client,'control_snapshot',return_value=raw):
            self.service._managed_once()
            self.assertIsNotNone(self.service.managed_transition)
            self.assertTrue(self.service.managed.running)
            self.assertEqual([action[0] for action in self.client.actions],['enter_map'])
        self.service._managed_once()
        self.assertIsNone(self.service.managed_transition)
        self.assertTrue(self.service.managed.running)
        self.assertEqual([action[0] for action in self.client.actions],['enter_map'])

    def test_stopping_between_transition_polls_clears_old_transition(self):
        self.client.items = []
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        self.client.native_handler = lambda kind,payload:{'bridge_verified':True,'transition_requested':True}
        self.service._work('managed',{})
        self.assertIsNotNone(self.service.managed_transition)
        self.assertIsNone(self.service.operation_kind)
        self.service.stop_managed()
        self.assertIsNone(self.service.managed_transition)
        self.assertFalse(self.service.managed.running)

    def test_cancelled_inflight_sale_keeps_confirmed_receipt_and_statistics(self):
        self.client.items = []
        self.service._publish(self.client.snapshot())
        settings = self.settings(farming=True)
        settings['cleanup']['equipment_action'] = 'sell'
        self.service.save_managed(settings)
        self.service.set_managed_running(True)
        self.client.items.append(equipment(1))
        def sold_then_stopped(kind,payload):
            self.assertEqual(kind,'sell')
            self.client.items = [row for row in self.client.items if identity(row)!=identity(payload['item'])]
            self.service.stop_managed()
            return {'bridge_verified':True,'effect_confirmed':True,'quantity':1}
        self.client.native_handler = sold_then_stopped
        self.service._work('managed',{})
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase,'stopped')
        self.assertIsNone(self.service.managed_stop_request)
        totals = self.service.managed.metrics.state()['equipment']['legendary']
        self.assertEqual(totals['count'],1)
        self.assertEqual(totals['sold'],1)
        self.assertEqual([action[0] for action in self.client.actions],['sell'])
        self.assertTrue(any('已确认' in row['message'] for row in self.service.managed_history))

    def test_pause_and_resume_keep_metrics_and_reset_requires_stop(self):
        self.ready()
        self.service.managed.metrics.totals[('equipment','ancient')] = {'count':3,'sold':1,'salvaged':1,'stored':0}
        self.service.managed.metrics.rows[('equipment','ancient')] = equipment(is_ancient=True)
        self.service.stop_managed(pause=True)
        self.assertTrue(self.service.managed_page()['paused'])
        expected = self.service.managed.metrics.state()
        self.service.set_managed_running(True)
        self.assertEqual(self.service.managed.metrics.state(),expected)
        with self.assertRaisesRegex(ValidationError,'停止托管'):
            self.service.reset_managed()
        self.service.stop_managed()
        self.service.reset_managed()
        self.assertEqual(self.service.managed.metrics.state()['equipment']['ancient']['count'],0)

    def test_user_stop_finishes_collection_locks_and_cleanup_without_entering_again(self):
        self.client.items=[];self.client.in_town=False;self.client.current_map='offline-map'
        self.service._publish(self.client.snapshot())
        s=self.settings(farming=True);s['cleanup']['equipment_action']='salvage'
        self.service.save_managed(s);self.service.set_managed_running(True)
        self.client.items=[equipment(0,is_black_mist=True),equipment(1)]
        self.client.ground=[equipment(2,container='ground',selection_id='ground-new',is_black_mist=True)]
        def native(kind,payload):
            if kind=='return_town': return {'transition_requested':True}
            if kind=='salvage':
                self.client.items=[r for r in self.client.items if identity(r)!=identity(payload['item'])]
            if kind=='collect_ground':
                row=self.client.ground.pop(0)
                used={r['slot_index'] for r in self.client.items if r['container']=='inventory'}
                row.update(container='inventory',slot_index=next(n for n in range(40) if n not in used))
                self.client.items.append(row)
            return {'bridge_verified':True,'effect_confirmed':True,'quantity':1}
        self.client.native_handler=native
        packet=self.service.finish_managed();self.assertTrue(packet['running']);self.assertTrue(packet['finishing'])
        self.service._work('managed',{})
        for _ in range(2):self.service._work('managed',{})
        self.assertEqual([a[0] for a in self.client.actions],['return_town'])
        self.client.in_town=True;self.client.current_map=None
        for _ in range(25):
            if not self.service.managed.running:break
            self.service._work('managed',{})
        kinds=[a[0] for a in self.client.actions]
        self.assertNotIn('enter_map',kinds)
        self.assertLess(kinds.index('lock'),kinds.index('salvage'))
        self.assertIn('collect_ground',kinds)
        self.assertEqual(self.service.managed.phase,'stopped')
        self.assertTrue(all(r['locked'] and r['container']=='storage' for r in self.client.items))
        self.assertIsNone(self.service.managed_started_clock)

    def test_stop_during_inflight_action_retains_receipt_and_finishing_request(self):
        self.client.items=[];self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        self.client.items=[equipment(1)]
        self.service.managed.settings['cleanup']['equipment_policies']['legendary']['action']='sell'
        def native(kind,payload):
            self.service.finish_managed()
            self.client.items=[]
            return {'bridge_verified':True,'effect_confirmed':True,'quantity':1}
        self.client.native_handler=native
        self.service._work('managed',{})
        self.assertTrue(self.service.managed.finishing)
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_stop_request)
        self.assertEqual(self.service.managed.metrics.state()['equipment']['legendary']['sold'],1)
        self.service._work('managed',{});self.service._work('managed',{})
        self.assertEqual(self.service.managed.phase,'stopped')

    def test_finish_request_blocks_not_yet_submitted_entry_and_abort_remains_immediate(self):
        self.client.items=[];self.service._publish(self.client.snapshot());self.ready(farming=True,auto_equip=False)
        world=self.service._managed_observation();action=self.service.managed.next_action(world)
        self.service.finish_managed()
        result=self.service._managed_execute(action,world)
        self.assertTrue(result['retryable']);self.assertEqual(self.client.actions,[])
        self.service.managed.pending=None
        self.service.stop_managed()
        self.assertFalse(self.service.managed.finishing);self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.actions,[])

    def test_capacity_pause_can_resume_without_resetting_session_statistics(self):
        self.client.storage_indices=[0]
        self.client.items=[equipment(0,locked=True),equipment(0,container='storage',locked=True)]
        self.service._publish(self.client.snapshot())
        settings = self.settings(farming=True)
        for kind in ('equipment', 'rune', 'gem', 'gold_bar', 'legendary_rein'):
            settings['cleanup'][kind + '_pages'] = [1]
        self.service.save_managed(settings)
        self.service.set_managed_running(True)
        self.service.managed.metrics.totals[('equipment','ancient')]={'count':3,'sold':1,'salvaged':0,'stored':2}
        self.service.managed.metrics.rows[('equipment','ancient')]=equipment(is_ancient=True)
        self.service._work('managed',{})
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertTrue(self.service.managed_can_resume)
        expected=self.service.managed.metrics.state()
        self.client.items=[row for row in self.client.items if row['container']=='inventory']
        self.service.set_managed_running(True)
        self.assertEqual(self.service.managed.metrics.state(),expected)
        self.assertTrue(self.service.managed.running)

    def test_collected_rewards_are_rescanned_and_all_required_locks_precede_disposal(self):
        self.client.items = []
        self.client.carriage = [equipment(0,container='carriage',is_black_mist=True,selection_id='carriage-item')]
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        self.service._managed_once()
        self.assertEqual(self.client.actions[0][0],'collect_carriage')
        # A new unprotected reward arrives before the next read. The acquired
        # black-mist gear must be fully classified and locked before junk sells.
        self.client.items.append(equipment(1))
        self.service._managed_once()
        self.assertEqual([action[0] for action in self.client.actions],['collect_carriage','lock'])
        self.service._managed_once()
        self.assertEqual(self.client.actions[2][0],'transfer')
        self.assertEqual(self.client.actions[2][2],'storage')
        self.assertTrue(self.client.items[0]['locked'])

    def test_carriage_prewrite_change_discards_selection_and_replans_fresh(self):
        self.client.items = []
        self.client.carriage = [equipment(0,container='carriage',is_black_mist=True,selection_id='old-selection')]
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        def prewrite_change(*args,**kwargs):
            self.client.carriage[0]['selection_id'] = 'fresh-selection'
            raise GameBusyError('容器或唯一编号发生变化，未收取')
        with patch.object(self.client,'collect_carriage',side_effect=prewrite_change) as collect:
            for now in (100, 101):
                with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=now):
                    self.service._work('managed',{})
        collect.assert_called_once()
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed.pending)
        self.assertEqual(self.client.actions,[])
        with patch.object(self.client,'collect_carriage',wraps=self.client.collect_carriage) as collect:
            with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=103):
                self.service._work('managed',{})
        self.assertEqual(collect.call_args.args[0],'fresh-selection')
        self.assertEqual([action[0] for action in self.client.actions],['collect_carriage'])
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_retry)

    def test_carriage_postwrite_readback_mismatch_still_stops_without_replay(self):
        self.client.items = []
        self.client.carriage = [equipment(0,container='carriage',selection_id='item')]
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        with patch.object(self.client,'collect_carriage',side_effect=RuntimeError('马车收取后物品回读不一致')) as collect:
            self.service._work('managed',{})
            self.service._work('managed',{})
        collect.assert_called_once()
        self.assertFalse(self.service.managed.running)
        self.assertIn('马车收取后物品回读不一致',self.service.error)

    def test_collection_batch_reaches_executor_before_locks_and_disposal(self):
        self.client.items = []
        self.client.carriage = [equipment(0, container='carriage', is_black_mist=True,
            selection_id='carriage-keep'), equipment(1, container='carriage', selection_id='carriage-junk')]
        self.client.ground = [equipment(2, container='ground', is_black_mist=True,
            selection_id='ground-keep'), equipment(3, container='ground', selection_id='ground-junk')]
        self.service._publish(self.client.snapshot())
        config = self.settings(farming=True)
        config['cleanup']['equipment_action'] = 'salvage'
        self.service.save_managed(config); self.service.set_managed_running(True)
        self.service.finish_managed()
        def native(kind, payload):
            if kind == 'collect_ground':
                row = next(r for r in self.client.ground if r['selection_id'] == payload['selection_id'])
                self.client.ground.remove(row)
                used = {r['slot_index'] for r in self.client.items if r['container'] == 'inventory'}
                row.update(container='inventory', slot_index=next(n for n in range(40) if n not in used))
                self.client.items.append(row)
            elif kind == 'salvage':
                self.client.items = [r for r in self.client.items if identity(r) != identity(payload['item'])]
            return {'bridge_verified': True, 'effect_confirmed': True, 'quantity': 1}
        self.client.native_handler = native
        for _ in range(20):
            if not self.service.managed.running: break
            self.service._work('managed', {})
        kinds = [a[0] for a in self.client.actions]
        self.assertEqual(kinds[:4], ['collect_carriage', 'collect_carriage', 'collect_ground', 'collect_ground'])
        self.assertEqual(kinds.count('lock'), 2)
        self.assertEqual(kinds.count('salvage'), 2)
        self.assertLess(max(i for i, kind in enumerate(kinds) if kind == 'lock'), kinds.index('salvage'))
        self.assertTrue(all(r['locked'] and r['container'] == 'storage' for r in self.client.items))
        self.assertEqual(self.service.managed.phase, 'stopped')

    def test_verified_pickup_wakes_serial_worker_without_another_full_preflight(self):
        self.client.items = []
        self.client.carriage = [equipment(0, container='carriage', selection_id='fast-one'),
            equipment(1, container='carriage', selection_id='fast-two')]
        self.client.character_snapshot = Mock(return_value={'available': False, 'equipment': []})
        self.service._publish(self.client.snapshot()); self.ready(farming=True, auto_equip=False)
        self.client.character_snapshot.reset_mock()
        with patch.object(self.service, '_managed_observation', wraps=self.service._managed_observation) as read:
            self.service._work('managed', {})
            self.assertEqual(read.call_count, 1)
        self.assertEqual(self.client.character_snapshot.call_count, 1)
        self.assertEqual([a[0] for a in self.client.actions], ['collect_carriage'])
        self.assertTrue(self.service.monitor_wake.is_set())
        self.assertFalse(self.service.busy)
        self.service.monitor_wake.clear()
        self.service._work('managed', {})
        self.assertEqual([a[0] for a in self.client.actions], ['collect_carriage'] * 2)
        self.assertTrue(self.service.monitor_wake.is_set())
        self.service.monitor_wake.clear()
        self.service._work('managed', {})  # Disposal still uses a fresh inventory barrier.
        self.assertTrue(self.service.monitor_wake.is_set())

    def test_pickup_cancel_during_receipt_never_wakes_next_action(self):
        self.client.items = []
        self.client.ground = [equipment(0, container='ground', selection_id='cancel-pickup')]
        self.service._publish(self.client.snapshot()); self.ready(farming=True, auto_equip=False)
        def native(kind, payload):
            self.assertEqual(kind, 'collect_ground')
            self.service.stop_managed(pause=True)
            row = self.client.ground.pop(); row.update(container='inventory', slot_index=0)
            self.client.items.append(row)
            return {'bridge_verified': True, 'effect_confirmed': True, 'quantity': 1}
        self.client.native_handler = native
        self.service._work('managed', {})
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertFalse(self.service.monitor_wake.is_set())
        self.assertEqual([a[0] for a in self.client.actions], ['collect_ground'])

    def test_pickup_publication_never_keeps_character_from_another_session(self):
        self.service.character = {'available': True, 'session': 'another-character', 'equipment': []}
        self.client.character_snapshot = Mock()
        self.service._publish(self.client.snapshot(), refresh_character=False)
        self.assertFalse(self.service.character['available'])
        self.client.character_snapshot.assert_not_called()

    def test_monitor_loop_wait_accepts_wake_and_starts_one_serial_worker(self):
        with patch.object(self.service, 'monitor_wake') as wake, patch.object(self.service, 'closed') as closed, \
                patch('deskrawl_assistant.web_service.threading.Thread') as thread:
            wake.wait.return_value = True
            closed.is_set.side_effect = [False] * 6 + [True]
            self.service._monitor_loop()
            wake.wait.assert_any_call(2)
            self.assertEqual(wake.clear.call_count, 2)
            thread.return_value.start.assert_called_once()
            self.assertTrue(self.service.busy)

    def test_collection_with_incomplete_reward_read_never_disposes_junk(self):
        self.client.items = []
        self.client.carriage = [equipment(0,container='carriage',is_black_mist=True,selection_id='carriage-item')]
        self.client.carriage_corrupt_on_collect = True
        self.service._publish(self.client.snapshot())
        self.ready(farming=True,auto_equip=False)
        self.service._managed_once()
        self.client.items.append(equipment(1))
        for _ in range(5): self.service._managed_once()
        self.assertEqual([action[0] for action in self.client.actions],['collect_carriage'])
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase,'paused')


class ManagedBridgeHelpersTests(unittest.TestCase):
    def test_progression_follows_campaign_prerequisites_ignoring_side_maps(self):
        maps = [{'map_id':'a','prerequisite':None,'map_type':0},
                {'map_id':'c','prerequisite':'b','map_type':0},
                {'map_id':'rift','prerequisite':'a','map_type':1},
                {'map_id':'b','prerequisite':'a','map_type':0}]
        self.assertEqual(progression_from_catalog(maps,'a'),['a','b','c'])
        with self.assertRaises(ValidationError): progression_from_catalog(maps,'unknown')

    def test_fallback_disposal_required_even_when_new_junk_has_other_action(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        settings['auto_equip']['enabled'] = True
        self.assertEqual(required_capabilities(settings),{'transfer','equip','lock'})
        settings['farming']['enabled'] = True
        settings['cleanup'].update(equipment_action='sell',equipment_fallback='salvage')
        self.assertTrue({'sell','salvage','chests','ground_pickup','travel','lock'} <= required_capabilities(settings))
