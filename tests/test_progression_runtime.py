"""Native signatures and receipts using fake snapshots; never attach to a game."""
from contextlib import nullcontext
from copy import deepcopy
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.native_dispatcher import compile_mailbox, load_profile, NativeActionError
from deskrawl_assistant.progression import catalog
from deskrawl_assistant.progression_runtime import ProgressionRuntime, _GrowthCopy
from deskrawl_assistant.recommendation_reader import _Copy
from deskrawl_assistant.native_memory import SnapshotChangedError, MemoryReadError, GameBusyError
from test_progression import state


class PreviewReadTests(unittest.TestCase):
    def test_runtime_quote_reads_wrapped_queue_head_and_rejects_corrupt_cursors(self):
        profile=load_profile();reader=Mock();reader.module={'base':0};copy=Mock()
        runtime=ProgressionRuntime(SimpleNamespace(reader=reader,profile=profile))
        pointers={profile['statics']['merchant_stock_type']:1000,1184:2000,2000:3000,
                  4000:4001,6000:6001,9000:9001,10000:9001,4032:6000,4024:5000,9016:5001,10016:5002}
        copy.copied_only.side_effect=lambda:nullcontext()
        copy.ptr.side_effect=pointers.get;copy.dictionary.return_value={2:4000}
        copy.verify.side_effect=lambda obj,name:obj;copy.array.return_value=[bytes(8)]*4
        copy.integer.side_effect=lambda address:{9068:21,10068:21,4016:70}[address]
        copy.read.side_effect=lambda address,size:struct.pack('<Qiiii',7000,3,1,2,9) if address==6016 else struct.pack('<Q',9000 if address==7056 else 10000)
        reader.string.side_effect=lambda pointer:{5000:'Inferno1',5001:'server-offer',5002:'next-offer'}[pointer]
        quotes=runtime._merchant_quotes(copy)
        self.assertEqual(quotes['helm'],{'price':21,'prices':[21,21],'level':70,'difficulty':'Inferno1','instance_id':'server-offer'})
        copy.read.assert_any_call(7056,8)
        copy.read.assert_any_call(7032,8)
        copy.read.side_effect=lambda address,size:struct.pack('<Qiiii',7000,4,1,2,9)
        from deskrawl_assistant.native_memory import MemoryReadError
        with self.assertRaisesRegex(MemoryReadError,'队列'):runtime._merchant_quotes(copy)

    def test_uninitialized_runtime_stock_type_waits_without_invalid_pointer_reads(self):
        profile=load_profile();reader=Mock();reader.module={'base':0};copy=Mock()
        copy.ptr.return_value=profile['uninitialized_type_tokens']['merchant_stock_type']
        runtime=ProgressionRuntime(SimpleNamespace(reader=reader,profile=profile))
        self.assertEqual(runtime._merchant_quotes(copy),{})
        copy.klass.assert_not_called();copy.dictionary.assert_not_called()

    def test_merchant_scan_skips_non_objects_with_invalid_text_before_valid_npc(self):
        profile=load_profile();reader=Mock();reader.singleton.return_value=0x20000
        reader.memory.regions.return_value=[SimpleNamespace(kind=0x20000,base=0x20000,size=512),
                                           SimpleNamespace(kind=0x20000,base=0x30000,size=2*1024**3)]
        objects=bytearray(512);struct.pack_into('<Q',objects,344,2);struct.pack_into('<Q',objects,408,2)
        reader.memory.read.return_value=bytes(objects)
        read=Mock();read.guards=[];read.klass.side_effect=lambda obj,name:obj
        def verify(obj,name):
            read.guards.append((obj,b'candidate'))
            if obj==0x20000:raise UnicodeDecodeError('utf-8',b'\xb0',0,1,'invalid')
            return obj
        read.verify.side_effect=verify
        read.ptr.return_value=2
        stock=list(range(100,110));read.pointers.return_value=stock
        reader.asset_name.side_effect=lambda obj:'SoulShard' if obj==2 else catalog()['shop'][obj-100]['id']
        runtime=ProgressionRuntime(SimpleNamespace(reader=reader,profile=profile))
        self.assertEqual(runtime._merchant_npc(read,2),0x20040)
        self.assertNotIn((0x20000,b'candidate'),read.guards)
        reader.memory.read.assert_called_once_with(0x20000,512)

    def lookup_fixture(self, regions, npc):
        reader=Mock();read=Mock();read.guards=[]
        currency=0x70000000;stock=list(range(100,110))
        reader.singleton.return_value=npc
        reader.memory.regions.return_value=regions
        def block(address,size):
            data=bytearray(size)
            if address<=npc+344<address+size:
                struct.pack_into('<Q',data,npc+344-address,currency)
            return bytes(data)
        reader.memory.read.side_effect=block
        def verify(obj,name):
            read.guards.append((obj,b'checked'))
            if name=='DialogueInteractable' and obj!=npc:raise MemoryReadError('not a merchant')
            return obj
        read.verify.side_effect=verify
        read.ptr.side_effect=lambda address:currency if address==npc+344 else 1
        read.pointers.return_value=stock
        reader.asset_name.side_effect=lambda obj:'SoulShard' if obj==currency else catalog()['shop'][obj-100]['id']
        runtime=ProgressionRuntime(SimpleNamespace(reader=reader,profile=load_profile()))
        return runtime,read,reader,currency

    def test_lookup_skips_gigabytes_of_earlier_graphics_buffers(self):
        regions=[SimpleNamespace(kind=0x20000,base=0x10000,size=2*1024**3),
                 SimpleNamespace(kind=0x20000,base=0x90000000,size=4096)]
        runtime,read,reader,currency=self.lookup_fixture(regions,0x90000040)
        self.assertEqual(runtime._merchant_npc(read,currency),0x90000040)
        reader.memory.read.assert_called_once_with(0x90000000,4096)

    def test_lookup_starts_near_live_root_inside_large_heap(self):
        base=0x10000000;npc=base+1500*1024**2+64
        runtime,read,reader,currency=self.lookup_fixture(
            [SimpleNamespace(kind=0x20000,base=base,size=2*1024**3)],npc)
        self.assertEqual(runtime._merchant_npc(read,currency),npc)
        reader.memory.read.assert_called_once_with(npc-64,1024**2)

    def test_lookup_budget_retries_continue_at_unvisited_chunks(self):
        base=0x10000000;chunk=1024**2;npc=base+2*chunk+64
        runtime,read,reader,currency=self.lookup_fixture(
            [SimpleNamespace(kind=0x20000,base=base,size=3*chunk)],npc)
        reader.singleton.return_value=base
        with patch('deskrawl_assistant.progression_runtime.MERCHANT_SCAN_BYTES',chunk):
            for _ in range(2):
                with self.assertRaisesRegex(GameBusyError,'自动继续'):
                    runtime._merchant_npc(read,currency)
            self.assertEqual(runtime._merchant_npc(read,currency),npc)
        self.assertEqual([c.args[0] for c in reader.memory.read.call_args_list],
                         [base,base+chunk,base+2*chunk])

    def test_invalid_cached_object_leaves_no_stale_guards(self):
        npc=0x10040
        runtime,read,reader,currency=self.lookup_fixture(
            [SimpleNamespace(kind=0x20000,base=0x10000,size=4096)],npc)
        runtime.npc=0x20000
        self.assertEqual(runtime._merchant_npc(read,currency),npc)
        self.assertNotIn((0x20000,b'checked'),read.guards)

    def test_missing_merchant_waits_without_invoking_or_reusing_old_scan(self):
        runtime,read,reader,currency=self.lookup_fixture(
            [SimpleNamespace(kind=0x20000,base=0x10000,size=4096)],0x20000)
        for _ in range(2):
            with self.assertRaisesRegex(GameBusyError,'暂未加载'):
                runtime._merchant_npc(read,currency)
            self.assertEqual(runtime._merchant_scanned,set())
        self.assertEqual(reader.memory.read.call_count,2)

    def test_cancelled_or_invalidated_lookup_does_not_scan(self):
        runtime,read,reader,currency=self.lookup_fixture(
            [SimpleNamespace(kind=0x20000,base=0x10000,size=4096)],0x10040)
        for kwargs in ({'cancel_event':SimpleNamespace(is_set=lambda:True)}, {'validate':lambda:False}):
            with self.assertRaisesRegex(NativeActionError,'已取消'):
                runtime._merchant_npc(read,currency,**kwargs)
        reader.memory.read.assert_not_called()

    def test_lookup_wall_clock_budget_defers_before_memory_scan(self):
        runtime,read,reader,currency=self.lookup_fixture(
            [SimpleNamespace(kind=0x20000,base=0x10000,size=4096)],0x10040)
        with patch('deskrawl_assistant.progression_runtime.time.monotonic',side_effect=[0,4]):
            with self.assertRaisesRegex(GameBusyError,'自动继续'):
                runtime._merchant_npc(read,currency)
        reader.memory.read.assert_not_called()

    def test_lookup_budget_includes_region_enumeration(self):
        region=SimpleNamespace(kind=0x20000,base=0x10000,size=4096)
        runtime,read,reader,currency=self.lookup_fixture([region],0x10040)
        clock=[0]
        def regions():
            yield region
            clock[0]=4
            yield SimpleNamespace(kind=0x20000,base=0x20000,size=4096)
        reader.memory.regions.side_effect=regions
        with patch('deskrawl_assistant.progression_runtime.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(GameBusyError,'自动继续'):
                runtime._merchant_npc(read,currency)
        reader.memory.read.assert_not_called()

    def test_lookup_yields_between_candidates_and_resumes_inside_the_same_chunk(self):
        base=0x10000;npc=base+64
        runtime,read,reader,currency=self.lookup_fixture(
            [SimpleNamespace(kind=0x20000,base=base,size=4096)],npc)
        block=bytearray(4096)
        struct.pack_into('<Q',block,344,currency)
        struct.pack_into('<Q',block,408,currency)
        reader.memory.read.side_effect=lambda address,size:bytes(block)
        clock=[0];checked=[];verify=read.verify.side_effect
        def slow_invalid_candidate(obj,name):
            if name=='DialogueInteractable':
                checked.append(obj)
                if obj==base:clock[0]+=4
            return verify(obj,name)
        read.verify.side_effect=slow_invalid_candidate
        with patch('deskrawl_assistant.progression_runtime.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(GameBusyError,'自动继续'):
                runtime._merchant_npc(read,currency)
            self.assertEqual(checked,[base])
            self.assertNotIn((base,b'checked'),read.guards)
            clock[0]=10
            self.assertEqual(runtime._merchant_npc(read,currency),npc)
        self.assertEqual(checked,[base,npc])
        self.assertEqual(reader.memory.read.call_count,2)

    def test_currency_reference_alone_is_not_a_valid_shop(self):
        runtime,read,reader,currency=self.lookup_fixture(
            [SimpleNamespace(kind=0x20000,base=0x10000,size=4096)],0x10040)
        read.pointers.return_value=list(range(100,109))  # Incomplete original stock.
        with self.assertRaises(GameBusyError):runtime._merchant_npc(read,currency)
        self.assertIsNone(runtime.npc)
        self.assertNotIn((0x10040,b'checked'),read.guards)

    def test_cached_merchant_lookup_uses_real_profile_for_currency_and_stock(self):
        from deskrawl_assistant.game_actions import _Read
        profile=load_profile();reader=Mock();reader.module={'base':0}
        classes={name:(i+1)*10000 for i,name in enumerate(profile['types'])}
        npc,currency,array=1000,2000,3000
        stock=[4000+i*1000 for i in range(10)]
        pointers={npc:classes['DialogueInteractable'],npc+16:1,npc+344:currency,npc+336:array,
                  currency:classes['ItemData'],**{obj:classes['RandomEquipment'] for obj in stock}}
        reader.memory.read.side_effect=lambda address,size:struct.pack('<Q',pointers[address])
        names={value:key for key,value in classes.items()}
        reader.class_name.side_effect=names.get
        reader.fields.side_effect=lambda klass:[{'name':f['name'],'offset':f['rawRegistrationOffset'],'token':int(f['token'],16)} for f in profile['types'][names[klass]]['fields']]
        reader.array.return_value=([struct.pack('<Q',obj) for obj in stock],b''.join(struct.pack('<Q',obj) for obj in stock))
        assets={currency:'SoulShard',**{obj:row['id'] for obj,row in zip(stock,catalog()['shop'])}}
        reader.asset_name.side_effect=assets.get
        runtime=ProgressionRuntime(SimpleNamespace(reader=reader,profile=profile));runtime.npc=npc
        self.assertEqual(runtime._merchant_npc(_Read(reader,profile),currency),npc)
        reader.array.assert_called_once_with(array,'ItemData',stride=8,limit=32)

    def snapshot_fixture(self, *, ability=500, scene=2):
        copy,read,reader=Mock(),Mock(),Mock();data=catalog()
        copy.copied_only.side_effect=nullcontext
        singletons={name:(index+1)*10000 for index,name in enumerate(
            ['PlayerData','PlayerTalentBook','PlayerLifeSkillBook','PlayerAbilityBook','Inventory','SaveSystem','MerchantUI'])}
        manager,hero,combat,player,soul=100,200,300,400,600
        items={'SoulShard':soul,**{row['id']:1000+index*1000 for index,row in enumerate(data['shop'])}}
        pointers={manager+56:hero,manager+64:combat,combat+32:player,combat+112:ability,
                  singletons['SaveSystem']+112:90000,singletons['Inventory']+64:90001}
        copy.ptr.side_effect=lambda address:pointers.get(address,0)
        def verify(obj,name):
            if not obj: raise MemoryReadError(name+' 尚未加载。')
            return obj
        copy.verify.side_effect=verify
        copy.dictionary.side_effect=lambda obj,*args:({index:obj for index,obj in enumerate(items.values())} if obj==90000 else {soul:100} if obj==90001 else {})
        copy.list.return_value=[];copy.array.return_value=[];copy.boolean.return_value=False
        integers={hero+48:4,soul+140:2000,singletons['SaveSystem']+32:1,
                  **{items[row['id']]+216:row['slot'] for row in data['shop']}}
        copy.integer.side_effect=lambda address:integers.get(address,0)
        reader.string.side_effect=lambda key:list(items)[key]
        names={hero:data['heroes']['4']['id'],**{obj:key for key,obj in items.items()}}
        reader.asset_name.side_effect=names.get
        actions=SimpleNamespace(reader=reader,profile=load_profile(),dispatcher=Mock(),
            _context=Mock(return_value=(read,manager,0,scene)),_difficulty_state=Mock(return_value=('Normal',[])))
        runtime=ProgressionRuntime(actions);runtime._copy=Mock(return_value=copy)
        runtime._merchant_quotes=Mock(return_value={})
        runtime._merchant_unlock=Mock(return_value={'unlock_known':True,'unlocked':True})
        runtime._singleton=Mock(side_effect=lambda copy,name:singletons[name]);runtime._ranks=Mock(return_value=({},{}))
        return runtime,actions,copy

    def test_combat_preview_reads_cached_prices_without_opening_shop_or_exposing_purchases(self):
        runtime,actions,copy=self.snapshot_fixture()
        with patch('deskrawl_assistant.progression_runtime.decode_obscured_int',return_value=70):
            result=runtime.snapshot(merchant=True)
        self.assertTrue(result['available']);self.assertFalse(result['merchant']['available'])
        self.assertEqual(result['merchant']['stock'],[]);self.assertEqual(len(result['merchant']['preview']),10)
        self.assertTrue(all(row['price_reference'] for row in result['merchant']['preview']))
        self.assertNotIn('MerchantUI',[call.args[1] for call in runtime._singleton.call_args_list])
        actions.dispatcher.invoke.assert_not_called()

    def test_town_without_skill_controller_still_reads_books_shards_and_real_merchant_unlock(self):
        runtime,actions,copy=self.snapshot_fixture(ability=0,scene=1)
        with patch('deskrawl_assistant.progression_runtime.decode_obscured_int',return_value=70):
            result=runtime.snapshot(merchant=True)
        self.assertTrue(result['available'])
        self.assertFalse(result['skills']['readable'])
        self.assertIsNone(result['skills']['equipped'])
        self.assertIn('PlayerAbilityController',result['skills']['reason'])
        self.assertEqual(result['soul_shards'],{'current':100,'maximum':2000})
        self.assertEqual(result['talents'],{'ranks':{},'unspent':0})
        self.assertEqual(result['life_skills'],{'ranks':{}})
        self.assertTrue(result['merchant']['unlock_known'])
        self.assertTrue(result['merchant']['unlocked'])
        copy.finish.assert_called_once();actions._context.return_value[0].finish.assert_called_once()
        actions.dispatcher.invoke.assert_not_called()
        runtime,actions,copy=self.snapshot_fixture(ability=500,scene=1)
        with patch('deskrawl_assistant.progression_runtime.decode_obscured_int',return_value=70):
            restored=runtime.snapshot(merchant=True)
        self.assertTrue(restored['skills']['readable'])
        self.assertEqual(restored['skills']['equipped'],[None]*4)

    def test_optional_skills_do_not_swallow_changing_roots_or_skip_final_consistency(self):
        runtime,actions,copy=self.snapshot_fixture(ability=0)
        with patch.object(runtime,'_skills',side_effect=SnapshotChangedError('技能根已变化')), \
                patch('deskrawl_assistant.progression_runtime.decode_obscured_int',return_value=70):
            with self.assertRaises(SnapshotChangedError):runtime.snapshot(merchant=True)
        runtime,actions,copy=self.snapshot_fixture(ability=0)
        copy.finish.side_effect=SnapshotChangedError('读取期间根已变化')
        with patch('deskrawl_assistant.progression_runtime.decode_obscured_int',return_value=70):
            with self.assertRaises(SnapshotChangedError):runtime.snapshot(merchant=True)
        actions.dispatcher.invoke.assert_not_called()

    def test_saved_unlock_flag_controls_access_and_corrupt_collection_is_unknown(self):
        profile=load_profile();reader=Mock();reader.module={'base':0};actions=SimpleNamespace(reader=reader,profile=profile)
        runtime=ProgressionRuntime(actions);copy=Mock();copy.klass.side_effect=lambda obj,name:obj
        pointers={profile['statics']['merchant_unlocks_type']:100,284:200,200:300,324:400}
        copy.ptr.side_effect=pointers.get;copy.read.side_effect=lambda address,size:struct.pack('<ii',1,1) if address==332 else bytes(size)
        copy.array.return_value=[struct.pack('<iiQ',1,-1,500)]
        reader.string.return_value='MysteriousMerchantArea';self.assertTrue(runtime._merchant_unlock(copy)['unlocked'])
        reader.string.return_value='MerchantArea';value=runtime._merchant_unlock(copy)
        self.assertTrue(value['unlock_known']);self.assertFalse(value['unlocked'])
        reader.string.return_value=None;value=runtime._merchant_unlock(copy)
        self.assertFalse(value['unlock_known']);self.assertFalse(value['unlocked'])


class RuntimeBoundaryTests(unittest.TestCase):
    def fixture(self, before, after):
        before = dict(before, _guards=[], _item_map={'SoulShard':123, **before.get('_item_map',{})})
        profile = load_profile()
        reader, read, copy = Mock(), Mock(), Mock()
        read.guards = [(10, b'\0' * 8)]
        read.boolean.return_value = False
        read.singleton.return_value = 900
        copy.action_guards = []
        copy.boolean.return_value = False
        copy.copied_only.side_effect = nullcontext
        copy.verify.side_effect = lambda obj, name: obj
        copy.ptr.side_effect = lambda address: {1056:2000,1064:3000,3032:4000,3112:5000,2080:6000}.get(address, 7000)
        copy.integer.side_effect = lambda address: before['merchant']['stock'][0]['slot'] if address==1215 else before['class_mask']
        skills = catalog()['heroes'][str(before['class_mask'])]['abilities']
        names = {101 + n:s['id'] for n,s in enumerate(skills)}
        copy.list.return_value = [struct.pack('<Q', ptr) for ptr in names]
        reader.asset_name.side_effect = names.get
        reader.snapshot.return_value = {'complete':True,'containers':{'inventory':{'occupied_count':0,'slot_count':40,'slots':[]}}}
        dispatcher = Mock()
        def invoke(calls, guards, **kwargs):
            # Real encoder verifies static/instance distinction, argument counts,
            # bool result predicates and the 16 KiB mailbox limit on every path.
            compile_mailbox(0x100000, 0x180000000, profile, 1, 2, 3, calls, guards)
            return {'invoked':True,'completed':True}
        dispatcher.invoke.side_effect = invoke
        actions = SimpleNamespace(reader=reader, profile=profile, dispatcher=dispatcher,
                                  _context=Mock(return_value=(read,1000,8000,1)), _require_empty_town=Mock(),
                                  _online_state=Mock(return_value={'online_saving':False,'online_pending_requests':0}))
        runtime = ProgressionRuntime(actions)
        runtime.snapshot = Mock(side_effect=[before, after])
        runtime._copy = Mock(return_value=copy)
        runtime._singleton = Mock(return_value=900)
        runtime._asset = Mock(return_value=777)
        runtime._ranks = Mock(return_value=({}, {}))
        runtime._merchant_npc = Mock(return_value=888)
        return runtime, actions, copy

    def test_talent_rank_receipt_survives_leveling_awarding_another_point(self):
        before = state(); after = deepcopy(before)
        key = catalog()['heroes']['4']['talents'][0]['id']
        after['talents']['ranks'][key] = 1
        after['talents']['unspent'] = before['talents']['unspent']
        runtime, actions, _ = self.fixture(before, after)
        result = runtime.execute('allocate_talent', {'class_mask':4,'node_id':key,'expected_rank':0})
        self.assertTrue(result['effect_confirmed'])
        self.assertEqual([c.method for c in actions.dispatcher.invoke.call_args.args[0]],
                         ['PlayerTalentBook.fod','PlayerTalentBook.fom'])
        self.assertTrue(runtime.snapshot.call_args.kwargs['after_action'])

    def test_life_receipt_survives_simultaneous_gold_drop_and_keeps_original_cost_check(self):
        before = state(gold=1000); after = deepcopy(before)
        key = catalog()['life_skills'][0]['id']; after['life_skills']['ranks'][key] = 1
        cost = catalog()['life_skills'][0]['gold_costs'][0]; after['gold'] = 1000-cost+20
        runtime, actions, copy = self.fixture(before, after)
        with patch('deskrawl_assistant.progression_runtime.decode_obscured_int', return_value=1000):
            result = runtime.execute('learn_life_skill', {'class_mask':4,'node_id':key,'expected_rank':0,
                                    'gold_cost':cost,'gold_reserve':100})
        calls = actions.dispatcher.invoke.call_args.args[0]
        self.assertTrue(all(c.require_true for c in calls))
        self.assertEqual(result['gold_cost'], cost)
        self.assertTrue(result['effect_confirmed'])

    def test_insufficient_gold_reserve_stops_before_native_submission(self):
        before = state(gold=100); runtime, actions, _ = self.fixture(before, before)
        key = catalog()['life_skills'][0]['id']; cost = catalog()['life_skills'][0]['gold_costs'][0]
        with patch('deskrawl_assistant.progression_runtime.decode_obscured_int', return_value=100), \
                self.assertRaises(SnapshotChangedError):
            runtime.execute('learn_life_skill', {'class_mask':4,'node_id':key,'expected_rank':0,
                            'gold_cost':cost,'gold_reserve':100})
        actions.dispatcher.invoke.assert_not_called()

    def test_all_four_classes_use_static_category_guard_and_precheck_all_slots(self):
        for mask in (1,2,4,8):
            before = state(mask); after = deepcopy(before)
            abilities = catalog()['heroes'][str(mask)]['abilities']
            special = [s['id'] for s in abilities if 2 in s['slots']]
            target = [next(s['id'] for s in abilities if 0 in s['slots']),
                      next(s['id'] for s in abilities if 1 in s['slots']), *special[:2]]
            after['skills']['equipped'] = target
            runtime, actions, _ = self.fixture(before, after)
            self.assertTrue(runtime.execute('set_skill', {'class_mask':mask,'skill_slots':target})['effect_confirmed'])
            calls = actions.dispatcher.invoke.call_args.args[0]
            self.assertEqual(len(calls),12)
            self.assertTrue(all(c.require_true for c in calls[:8]))
            self.assertTrue(all(c.instance == 0 for c in calls if c.method.endswith('.bdi')))
            self.assertTrue(all(c.method.endswith('.bdg') for c in calls[8:]))

    def test_locked_skill_or_changed_class_never_submits(self):
        before = state(); key = before['skills']['available'].pop()
        runtime, actions, _ = self.fixture(before, before)
        with self.assertRaises(NativeActionError):
            runtime.execute('set_skill', {'class_mask':4,'skill_slots':[None,None,key,None]})
        actions.dispatcher.invoke.assert_not_called()
        with self.assertRaises(NativeActionError):
            runtime.execute('allocate_talent', {'class_mask':8})
        actions.dispatcher.invoke.assert_not_called()

    def test_missing_skill_snapshot_never_submits_a_skill_change(self):
        before=state();before['skills']={'readable':False,'equipped':None,'available':[],
            'reason':'PlayerAbilityController 尚未加载。'}
        runtime,actions,_=self.fixture(before,deepcopy(before))
        with self.assertRaisesRegex(NativeActionError,'技能读取未确认'):
            runtime.execute('set_skill',{'class_mask':4,'skill_slots':[None]*4})
        actions.dispatcher.invoke.assert_not_called()

    def test_locked_merchant_never_submits_native_open_or_purchase(self):
        for kind in ('merchant_open','merchant_buy'):
            before=state();before['merchant']['unlocked']=False
            runtime,actions,_=self.fixture(before,before)
            with self.assertRaisesRegex(NativeActionError,'尚未解锁'):runtime.execute(kind,{'class_mask':4})
            actions.dispatcher.invoke.assert_not_called()

    def test_merchant_open_and_close_encode_original_panel_entries(self):
        for kind, method in [('merchant_open','DialogueInteractable.clw'),('merchant_close','PanelBase.Close')]:
            before = state(); after = deepcopy(before)
            before['merchant']['open'] = kind == 'merchant_close'
            before['merchant']['online'] = True
            after['merchant']['open'] = kind == 'merchant_open'
            runtime, actions, _ = self.fixture(before, after)
            self.assertTrue(runtime.execute(kind, {'class_mask':4})['effect_confirmed'])
            calls = actions.dispatcher.invoke.call_args.args[0]
            self.assertEqual(calls[0].method, method)
            self.assertEqual(len(calls),1)
            if kind == 'merchant_open': actions._require_empty_town.assert_called_once()
            else: actions._require_empty_town.assert_not_called()

    def test_live_open_soul_shop_skips_lookup_and_reopening_even_with_stale_plan(self):
        for snapshot_open in (True,False):
            before=state();before['merchant']['open']=snapshot_open
            runtime,actions,copy=self.fixture(before,before)
            copy.boolean.return_value=True
            copy.ptr.side_effect=lambda address:123 if address==900+192 else 7000
            result=runtime.execute('merchant_open',{'class_mask':4})
            self.assertTrue(result['effect_confirmed'])
            runtime._merchant_npc.assert_not_called()
            actions.dispatcher.invoke.assert_not_called()
            copy.finish.assert_called_once();actions._context.return_value[0].finish.assert_called_once()

    def test_other_currency_shop_still_requires_verified_soul_shard_merchant(self):
        before=state();after=deepcopy(before);after['merchant']['open']=True
        runtime,actions,copy=self.fixture(before,after)
        copy.boolean.return_value=True
        self.assertTrue(runtime.execute('merchant_open',{'class_mask':4})['effect_confirmed'])
        self.assertEqual(runtime._merchant_npc.call_args.args[1],123)
        self.assertEqual(actions.dispatcher.invoke.call_args.args[0][0].method,'DialogueInteractable.clw')

    def test_open_shop_noop_does_not_confirm_a_panel_that_changed_during_read(self):
        before=state();runtime,actions,copy=self.fixture(before,before)
        copy.boolean.return_value=True
        copy.ptr.side_effect=lambda address:123 if address==900+192 else 7000
        copy.finish.side_effect=SnapshotChangedError('商人窗口或货币已变化')
        with self.assertRaises(SnapshotChangedError):
            runtime.execute('merchant_open',{'class_mask':4})
        runtime._merchant_npc.assert_not_called()
        actions.dispatcher.invoke.assert_not_called()

    def test_lookup_wait_happens_before_any_purchase_or_native_submission(self):
        before=state();runtime,actions,_=self.fixture(before,before)
        runtime._merchant_npc.side_effect=GameBusyError('定位仍在进行')
        with self.assertRaises(GameBusyError):runtime.execute('merchant_open',{'class_mask':4})
        actions.dispatcher.invoke.assert_not_called()

    def test_merchant_purchase_requires_new_uid_correct_slot_and_exact_currency_drop(self):
        before = state(); quote = before['merchant']['stock'][0]
        before['skills']={'readable':False,'equipped':None,'available':[],
            'reason':'PlayerAbilityController 尚未加载。'}
        before['_item_map'] = {quote['id']:999}; after = deepcopy(before)
        after['soul_shards']['current'] -= quote['price']
        runtime, actions, _ = self.fixture(before, after)
        empty = {'complete':True,'containers':{'inventory':{'occupied_count':0,'slot_count':40,'slots':[]}}}
        filled = deepcopy(empty);filled['containers']['inventory']['slots']=[{'item_uid':'received','equip_slot':quote['slot']}]
        actions.reader.snapshot.side_effect = [empty,filled]
        result = runtime.execute('merchant_buy', {'class_mask':4,'stock_id':quote['id'],'shop_slot':quote['slot'],
                                'expected_price':quote['price'],'before_shards':before['soul_shards']['current']})
        self.assertEqual(result['received_uid'],'received')
        self.assertEqual(actions.dispatcher.invoke.call_count,1)
        self.assertEqual(actions.dispatcher.invoke.call_args_list[0].args[0][0].method,'MerchantUI.gzr')
        self.assertTrue(result['merchant_open'])

    def test_refill_uses_original_empty_stock_sync_and_does_not_debit_currency(self):
        before=state();after=deepcopy(before)
        before['merchant']['stock']=[r for r in before['merchant']['stock'] if r['slot']!=7]
        runtime,actions,_=self.fixture(before,after)
        result=runtime.execute('merchant_refresh',{'class_mask':4,'shop_slots':[7]})
        self.assertTrue(result['effect_confirmed'])
        calls=actions.dispatcher.invoke.call_args.args[0]
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].method,'SaveSystem.egx')
        self.assertEqual(calls[0].arguments[0].value,0)
        self.assertEqual(result['before_shards'],after['soul_shards']['current'])

    def test_pending_online_request_defers_purchase_and_refill_before_submission(self):
        for kind in ('merchant_buy', 'merchant_refresh'):
            for sync in ({'online_saving':False,'online_pending_requests':1},
                         {'online_saving':True,'online_pending_requests':0}):
                with self.subTest(kind=kind, sync=sync):
                    before=state(); runtime,actions,_=self.fixture(before,deepcopy(before))
                    actions._online_state.return_value=sync
                    with self.assertRaisesRegex(GameBusyError,'等待在线同步'):
                        runtime.execute(kind,{'class_mask':4})
                    actions.dispatcher.invoke.assert_not_called()
                    actions.reader.snapshot.assert_not_called()

    def test_drained_online_queue_allows_one_purchase_and_keeps_submission_guards(self):
        before=state(); quote=before['merchant']['stock'][0]
        before['_item_map']={quote['id']:999}; before['_guards']=[]; after=deepcopy(before)
        after['soul_shards']['current']-=quote['price']
        runtime,actions,_=self.fixture(before,after)
        runtime.snapshot.side_effect=[before,before,after]
        read=actions._context.return_value[0]
        def online_state(current_read):
            self.assertIs(current_read,read)
            pending=1 if actions._online_state.call_count==1 else 0
            current_read.guards.append((50,struct.pack('<i',pending)))
            return {'online_saving':False,'online_pending_requests':pending}
        actions._online_state.side_effect=online_state
        empty={'complete':True,'containers':{'inventory':{'occupied_count':0,'slot_count':40,'slots':[]}}}
        filled=deepcopy(empty)
        filled['containers']['inventory']['slots']=[{'item_uid':'received','equip_slot':quote['slot']}]
        actions.reader.snapshot.side_effect=[empty,filled]
        payload={'class_mask':4,'stock_id':quote['id'],'shop_slot':quote['slot'],
                 'expected_price':quote['price'],'before_shards':before['soul_shards']['current']}
        with self.assertRaises(GameBusyError):runtime.execute('merchant_buy',payload)
        actions.dispatcher.invoke.assert_not_called()
        # Each real context read has fresh guards; discard the first rejected read.
        read.guards=[(10,b'\0'*8)]
        result=runtime.execute('merchant_buy',payload)
        self.assertTrue(result['effect_confirmed'])
        self.assertEqual(actions.dispatcher.invoke.call_count,1)
        self.assertIn((50,struct.pack('<i',0)),actions.dispatcher.invoke.call_args.args[1])

    def test_temporary_receipt_read_failure_rechecks_without_another_purchase(self):
        before=state(); quote=before['merchant']['stock'][0]
        before['_item_map']={quote['id']:999}; before['_guards']=[]; after=deepcopy(before)
        after['soul_shards']['current']-=quote['price']
        runtime,actions,_=self.fixture(before,after)
        runtime.snapshot.side_effect=[before,SnapshotChangedError('库存刷新'),after]
        empty={'complete':True,'containers':{'inventory':{'occupied_count':0,'slot_count':40,'slots':[]}}}
        filled=deepcopy(empty)
        filled['containers']['inventory']['slots']=[{'item_uid':'received','equip_slot':quote['slot']}]
        actions.reader.snapshot.side_effect=[empty,filled]
        with patch('deskrawl_assistant.progression_runtime.time.sleep'):
            result=runtime.execute('merchant_buy',{'class_mask':4,'stock_id':quote['id'],'shop_slot':quote['slot'],
                'expected_price':quote['price'],'before_shards':before['soul_shards']['current']})
        self.assertTrue(result['effect_confirmed'])
        self.assertEqual(actions.dispatcher.invoke.call_count,1)

    def test_batch_uses_ten_original_single_purchases_and_confirms_all_uids_and_debit(self):
        before=state();quote=before['merchant']['stock'][0];quote['available_count']=10
        before['_item_map']={quote['id']:999};after=deepcopy(before)
        after['soul_shards']['current']-=quote['price']*10
        runtime,actions,_=self.fixture(before,after)
        empty={'complete':True,'containers':{'inventory':{'occupied_count':0,'slot_count':40,'slots':[]}}}
        filled=deepcopy(empty);filled['containers']['inventory']['slots']=[{'item_uid':str(n),'equip_slot':quote['slot']} for n in range(10)]
        actions.reader.snapshot.side_effect=[empty,filled]
        result=runtime.execute('merchant_buy',{'class_mask':4,'stock_id':quote['id'],'shop_slot':quote['slot'],
            'expected_price':quote['price'],'before_shards':before['soul_shards']['current'],'quantity':10})
        calls=actions.dispatcher.invoke.call_args.args[0]
        self.assertEqual(len(calls),10);self.assertTrue(all(c.method=='MerchantUI.gzr' and c.arguments[1].raw==struct.pack('<i',1) for c in calls))
        self.assertEqual(result['quantity'],10);self.assertEqual(len(result['received_uids']),10)

    def test_batch_rejects_insufficient_stock_or_space_before_submission(self):
        for stock,space in ((1,40),(10,3)):
            before=state();quote=before['merchant']['stock'][0];quote['available_count']=stock
            before['_item_map']={quote['id']:999};runtime,actions,_=self.fixture(before,before)
            actions.reader.snapshot.return_value={'complete':True,'containers':{'inventory':{'occupied_count':0,'slot_count':space,'slots':[]}}}
            with self.assertRaises((SnapshotChangedError,NativeActionError)):
                runtime.execute('merchant_buy',{'class_mask':4,'stock_id':quote['id'],'shop_slot':quote['slot'],
                    'expected_price':quote['price'],'before_shards':before['soul_shards']['current'],'quantity':10})
            actions.dispatcher.invoke.assert_not_called()

    def test_partial_batch_receipt_stops_without_repeating_any_purchase(self):
        before=state();quote=before['merchant']['stock'][0];quote['available_count']=10
        before['_item_map']={quote['id']:999};after=deepcopy(before)
        after['soul_shards']['current']-=quote['price']*9
        runtime,actions,_=self.fixture(before,after)
        empty={'complete':True,'containers':{'inventory':{'occupied_count':0,'slot_count':40,'slots':[]}}}
        partial=deepcopy(empty);partial['containers']['inventory']['slots']=[{'item_uid':str(n),'equip_slot':quote['slot']} for n in range(9)]
        actions.reader.snapshot.side_effect=[empty,partial]
        with patch('deskrawl_assistant.progression_runtime.time.monotonic',side_effect=[0,0,13]), \
                patch('deskrawl_assistant.progression_runtime.time.sleep'), \
                patch('deskrawl_assistant.progression_runtime.record') as recorded, \
                self.assertRaisesRegex(NativeActionError,'神秘商人购买.*预计 10 件，读到 9 件') as caught:
            runtime.execute('merchant_buy',{'class_mask':4,'stock_id':quote['id'],'shop_slot':quote['slot'],
                'expected_price':quote['price'],'before_shards':before['soul_shards']['current'],'quantity':10})
        self.assertTrue(caught.exception.uncertain);self.assertEqual(actions.dispatcher.invoke.call_count,1)
        failure=next(c.kwargs for c in recorded.call_args_list if c.args[0]=='growth_action_unconfirmed')
        self.assertEqual(failure['quantity'],10)
        self.assertEqual(failure['expected_price'],quote['price'])
        self.assertEqual(failure['observed']['received_count'],9)
        self.assertEqual(failure['observed']['after_shards'],after['soul_shards']['current'])

    def test_unreadable_purchase_receipt_logs_last_error_and_never_repeats(self):
        before=state(); quote=before['merchant']['stock'][0]
        before['_item_map']={quote['id']:999}; before['_guards']=[]
        runtime,actions,_=self.fixture(before,before)
        runtime.snapshot.side_effect=[before,MemoryReadError('物品注册表读取不完整')]
        with patch('deskrawl_assistant.progression_runtime.time.monotonic',side_effect=[0,0,13]), \
                patch('deskrawl_assistant.progression_runtime.time.sleep'), \
                patch('deskrawl_assistant.progression_runtime.record') as recorded, \
                self.assertRaises(NativeActionError) as caught:
            runtime.execute('merchant_buy',{'class_mask':4,'stock_id':quote['id'],'shop_slot':quote['slot'],
                'expected_price':quote['price'],'before_shards':before['soul_shards']['current']})
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(actions.dispatcher.invoke.call_count,1)
        failure=next(c.kwargs for c in recorded.call_args_list if c.args[0]=='growth_action_unconfirmed')
        self.assertEqual(failure['last_read_error'],'MemoryReadError: 物品注册表读取不完整')
        self.assertEqual(failure['attempts'],1)

    def test_compact_guards_keep_dictionary_versions_and_full_copy_consistency(self):
        copy = object.__new__(_GrowthCopy)
        copy.guards = []; copy.action_guards = []; copy.guarding = True
        memory = Mock(); memory.read.side_effect = lambda address, size: bytes(size)
        copy.memory = memory
        def large_dictionary(*args, **kwargs):
            copy.read(3000,24*573)  # immutable lookup plus many life ranks
            return {1:10}
        with patch.object(_Copy,'dictionary',side_effect=large_dictionary):
            self.assertEqual(copy.dictionary(1000),{1:10})
        self.assertIn((3000,bytes(24*573)),copy.guards)
        self.assertNotIn((3000,bytes(24*573)),copy.action_guards)
        self.assertIn((1024,bytes(24)),copy.action_guards)
        memory.read.side_effect = lambda address,size: bytes([1])*size if address==3000 else bytes(size)
        with self.assertRaises(SnapshotChangedError):copy.finish()


if __name__ == '__main__': unittest.main()
