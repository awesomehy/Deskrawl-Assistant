"""Production transfers: whole stacks, capacity-limited prefixes and stale guards."""
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from deskrawl_assistant.container_transfer import FrozenSlotTransaction, move_items, plan_transfers, replace_fields
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError, GameBusyError
from test_container_transfer_probe import FakeMemory


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.p = FakeMemory()
        self.klass = 0x300000
        self.save = 0x400000
        self.reader = SimpleNamespace(memory=self.p,classes={'InventorySlot':self.klass},
            module={'base':0x10000000},singleton=lambda n:self.save,string=lambda uid:str(uid) if uid else None)
        self.tx = object.__new__(FrozenSlotTransaction)
        self.tx.reader,self.tx.handle,self.tx.write = self.reader,123,self.p.put
        self.suspend = patch('deskrawl_assistant.container_transfer.NT.NtSuspendProcess',return_value=0).start()
        self.resume = patch('deskrawl_assistant.container_transfer.NT.NtResumeProcess',return_value=0).start()
        self.addCleanup(patch.stopall)

    def pair(self,src=0x100000,dst=0x200000,count=36,uid=0,locked=0):
        header = bytearray(48)
        struct.pack_into('<Q',header,0,self.klass)
        empty = bytes(header)
        struct.pack_into('<Q',header,16,0x500000)
        struct.pack_into('<i',header,24,count)
        struct.pack_into('<Q',header,32,uid)
        header[40] = locked
        full = bytes(header)
        self.p.put(src,full); self.p.put(dst,empty)
        return src,dst,full,empty

    def test_stack_quantity_and_locked_generated_identity_survive(self):
        pairs = [self.pair(count=36),self.pair(0x110000,0x210000,1,0x600000,1)]
        result = self.tx.move_many(pairs,[])
        self.assertEqual(result['moved'],2)
        for src,dst,full,empty in pairs:
            self.assertEqual(self.p.read(dst,48),full)
            self.assertEqual(self.p.read(src,48),empty)
        self.assertEqual(self.p.read(self.save+92,1),b'\1')
        self.resume.assert_called_once()

    def test_failure_in_second_item_rolls_back_entire_batch_and_save(self):
        pairs = [self.pair(),self.pair(0x110000,0x210000)]
        def fail_once(address,data):
            self.p.put(address,data)
            if address==0x210000+24:
                self.tx.write = self.p.put
                raise RuntimeError('second-item failure')
        self.tx.write = fail_once
        with self.assertRaisesRegex(RuntimeError,'second-item failure'): self.tx.move_many(pairs,[])
        for src,dst,full,empty in pairs:
            self.assertEqual(self.p.read(src,48),full)
            self.assertEqual(self.p.read(dst,48),empty)

        self.assertEqual(self.p.read(self.save+92,1),b'\0')
        self.resume.assert_called_once()

    def test_game_item_gate_rejects_transfer_without_any_write(self):
        pair=self.pair()
        self.p.put(self.save+194,b'\1')
        with self.assertRaisesRegex(GameBusyError,'暂时禁止'):
            self.tx.move_many([pair],[])
        src,dst,full,empty=pair
        self.assertEqual(self.p.read(src,48),full)
        self.assertEqual(self.p.read(dst,48),empty)
        self.assertEqual(self.p.read(self.save+92,1),b'\0')
        self.resume.assert_called_once()

    def test_changed_permission_prevents_writes_and_resumes(self):
        pair = self.pair()
        with self.assertRaisesRegex(RuntimeError,'仓库权限发生变化'):
            self.tx.move_many([pair],[(0x700000,b'\1')])
        self.assertEqual(self.p.read(pair[0],48),pair[2])
        self.assertEqual(self.p.read(pair[1],48),pair[3])
        self.resume.assert_called_once()

    def test_slow_prewrite_verification_is_retryable_without_writing_and_resumes(self):
        pair = self.pair()
        with patch('deskrawl_assistant.container_transfer.time.monotonic', side_effect=[10, 11.01]), \
                patch.object(self.tx, 'write', wraps=self.tx.write) as write:
            with self.assertRaisesRegex(GameBusyError, '验证耗时异常，未移动物品'):
                self.tx.move_many([pair], [])
        write.assert_not_called()
        self.assertEqual(self.p.read(pair[0], 48), pair[2])
        self.assertEqual(self.p.read(pair[1], 48), pair[3])
        self.assertEqual(self.p.read(self.save + 92, 1), b'\0')
        self.resume.assert_called_once()
        self.assertEqual(self.tx.move_many([pair], [])['moved'], 1)

    def test_nonempty_target_is_never_overwritten(self):
        src,dst,full,_ = self.pair()
        self.p.put(dst,full)
        with self.assertRaisesRegex(RuntimeError,'完整空位'): self.tx.move(src,dst,full,full,[])
        self.suspend.assert_not_called()

    def test_resume_is_retried_after_failure(self):
        self.resume.side_effect = [-1,0]
        self.tx.move(*self.pair(),[])
        self.assertEqual(self.resume.call_count,2)

    def test_duplicate_slots_rejected(self):
        pair = self.pair()
        with self.assertRaisesRegex(RuntimeError,'重复'): self.tx.move_many([pair,pair],[])
        self.suspend.assert_not_called()

    def test_rollback_keeps_attempting_other_fields_after_one_restore_error(self):
        src,dst,full,empty = self.pair()
        attempted = []
        def fail(address,data):
            attempted.append(address)
            if address==src+16 or (address==dst+24 and data==b'\0'*4):
                raise RuntimeError('simulated failure')
            self.p.put(address,data)
        self.tx.write = fail
        with self.assertRaisesRegex(RuntimeError,'部分字段未能恢复'): self.tx.move(src,dst,full,empty,[])
        self.assertEqual(self.p.u64(dst+16),0)
        self.resume.assert_called_once()

    def fake_reader(self):
        inv,store = 0x800000,0x810000
        ia,sa = 0x820000,0x830000
        self.p.put(inv+56,struct.pack('<Q',ia));self.p.put(store+48,struct.pack('<Q',sa))
        self.reader.singleton = lambda name: inv if name=='Inventory' else store
        pair = self.pair()
        row = {'transfer_key':'token','slot_index':0,'internal_name':'GemEmerald4','count':36,'item_type':6}
        self.reader.snapshot = lambda:{'complete':True,'containers':{'storage':{'slots':[row]},'inventory':{'slots':[]}}}
        self.reader.array = lambda ptr,*a,**kw:([struct.pack('<Q',pair[0] if ptr==sa else pair[1])],struct.pack('<Q',pair[0] if ptr==sa else pair[1]))
        self.reader.transfer_key = lambda *args:'token'
        self.reader.snapshot_stamps = []
        return row,pair

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_full_destination_is_rejected_before_transaction(self,tx,access):
        row,(src,dst,full,empty) = self.fake_reader()
        self.p.put(dst,full)
        with self.assertRaisesRegex(RuntimeError,'空格不足'): move_items(self.reader,[row],'inventory')
        tx.assert_not_called()

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_stale_selection_is_rejected_before_transaction(self,tx,access):
        self.fake_reader()
        with self.assertRaisesRegex(SnapshotChangedError,'已变化或移动'):
            move_items(self.reader,[{'transfer_key':'stale'}],'inventory')
        tx.assert_not_called()

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_changed_source_header_replans_without_opening_transaction(self,tx,access):
        row,_ = self.fake_reader()
        self.reader.transfer_key = lambda *args:'changed-token'
        with self.assertRaisesRegex(SnapshotChangedError,'来源物品发生变化'):
            move_items(self.reader,[row],'inventory')
        tx.assert_not_called()

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_invalid_selection_is_not_a_retryable_snapshot_change(self,tx,access):
        row,_ = self.fake_reader()
        for expected in ([{}],[row,row]):
            with self.subTest(expected=expected):
                with self.assertRaises(MemoryReadError) as caught:
                    move_items(self.reader,expected,'inventory')
                self.assertNotIsInstance(caught.exception,SnapshotChangedError)
        tx.assert_not_called()

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_cancel_prevents_transaction(self,tx,access):
        row,pair = self.fake_reader()
        with self.assertRaisesRegex(RuntimeError,'已取消'): move_items(self.reader,[row],'inventory',lambda:False)
        tx.assert_not_called()

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_cleanup_context_is_attached_to_atomic_transaction_or_blocks_it(self,tx,access):
        row,_ = self.fake_reader()
        tx.return_value.apply_slots.return_value = {'moved':1}
        move_items(self.reader,[row],'inventory',context_guards=lambda:[(0x900000,b'town')])
        self.assertIn((0x900000,b'town'),tx.return_value.apply_slots.call_args.args[1])
        tx.reset_mock()
        def changed(): raise GameBusyError('town changed')
        with self.assertRaises(GameBusyError):
            move_items(self.reader,[row],'inventory',context_guards=changed)
        tx.assert_not_called()

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_target_restriction_rejects_closed_page_before_write(self,tx,access):
        row,pair = self.fake_reader()
        with self.assertRaisesRegex(RuntimeError,'尚未开放'):
            move_items(self.reader,[row],'storage',target_indices=[50])
        tx.assert_not_called()

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_target_restriction_rejects_invalid_and_out_of_range_indices(self,tx,access):
        row,pair = self.fake_reader()
        for restriction in ([],[True],[0,0],[-1]):
            with self.subTest(restriction=restriction), self.assertRaisesRegex(RuntimeError,'限制无效'):
                move_items(self.reader,[row],'inventory',target_indices=restriction)
        with self.assertRaisesRegex(RuntimeError,'超出容器'):
            move_items(self.reader,[row],'inventory',target_indices=[1])
        tx.assert_not_called()

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_only_selected_destination_receives_items(self,tx,access):
        row,(src,dst,full,empty) = self.fake_reader()
        another = dst+0x100
        self.p.put(another,empty)
        self.reader.array = lambda ptr,*a,**kw: ([struct.pack('<Q',src)],struct.pack('<Q',src)) if ptr==0x830000 else (
            [struct.pack('<Q',dst),struct.pack('<Q',another)],struct.pack('<QQ',dst,another))
        tx.return_value.apply_slots.return_value = {'moved':1}
        result = move_items(self.reader,[row],'inventory',target_indices=[1])
        changes = tx.return_value.apply_slots.call_args.args[0]
        self.assertEqual({a for a,_,_ in changes},{src,another})
        self.assertEqual(result['destinations'][0]['slot_index'],1)

    def test_manual_prefix_moves_in_requested_order_using_fresh_unlocked_capacity(self):
        for target in ('inventory','storage'):
            with self.subTest(target=target):
                self.fake_reader()
                source_name = 'storage' if target=='inventory' else 'inventory'
                sources = [self.stack(0x100000+n*0x1000,1,item=0x500000+n*0x1000,index=n,locked=1) for n in range(3)]
                # The cached snapshot claims plenty of space. Physical slots
                # have only one empty usable slot; a closed storage page does
                # not add capacity.
                targets = [self.stack(0x200000,0),self.stack(0x210000,1,item=0x600000,index=1)]
                if target=='storage': targets.append(self.stack(0x220000,0,index=2))
                rows = [dict(transfer_key=f'token-{n}',slot_index=n,internal_name=f'item-{n}',item_type=2)
                        for n in range(3)]
                self.reader.snapshot = lambda:{'complete':True,'containers':{
                    source_name:{'slots':rows},target:{'slots':[],'slot_count':250}}}
                self.reader.transfer_key = lambda container,index,address,head,row:row['transfer_key']
                arrays = {}
                for name,ptr in [('inventory',0x820000),('storage',0x830000)]:
                    entries = [struct.pack('<Q',s['address']) for s in (targets if name==target else sources)]
                    arrays[ptr] = entries,b''.join(entries)
                    self.p.put(ptr+32,arrays[ptr][1])
                self.reader.array = lambda ptr,*a,**kw:arrays[ptr]
                expected = [rows[2],rows[0],rows[1]]
                allowed = [0,1] if target=='storage' else [0,1,2]
                with patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':allowed},[])), \
                        patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction',return_value=self.tx), \
                        patch.object(self.tx,'close'), \
                        patch.object(self.tx,'apply_slots',wraps=self.tx.apply_slots) as apply, \
                        patch('deskrawl_assistant.container_transfer.operation_id',return_value='test-op'):
                    result = move_items(self.reader,expected,target,allow_partial=True)
                self.assertEqual((result['moved'],result['remaining']),(1,2))
                self.assertEqual(result['movements'][0]['source'],
                    {'container':source_name,'slot_index':2,'count_before':1,'count_after':0})
                self.assertEqual(self.p.u64(sources[2]['address']+16),0)
                self.assertEqual(self.p.read(targets[0]['address'],48),sources[2]['header'])
                for src in sources[:2]: self.assertEqual(self.p.read(src['address'],48),src['header'])
                self.assertEqual(apply.call_args.kwargs['moved'],1)
                if target=='storage': self.assertEqual(self.p.u64(targets[2]['address']+16),0)

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_manual_full_destination_returns_zero_without_transaction_or_commit(self,tx,access):
        row,(src,dst,full,empty) = self.fake_reader()
        self.p.put(dst,full)
        commits = []
        result = move_items(self.reader,[row],'inventory',allow_partial=True,on_commit=commits.append)
        self.assertEqual((result['moved'],result['remaining']),(0,1))
        self.assertEqual(result['movements'],[])
        tx.assert_not_called()
        self.assertEqual(commits,[])
        self.assertEqual(self.p.read(src,48),full)

    @patch('deskrawl_assistant.container_transfer.access_state',return_value=({'storage_indices':[0]},[]))
    @patch('deskrawl_assistant.container_transfer.FrozenSlotTransaction')
    def test_partial_mode_still_rejects_stale_selection_before_any_move(self,tx,access):
        row,_ = self.fake_reader()
        with self.assertRaises(SnapshotChangedError):
            move_items(self.reader,[row,{'transfer_key':'stale'}],'inventory',allow_partial=True)
        tx.assert_not_called()

    def stack(self,address,count,item=0x500000,index=0,uid=0,locked=0):
        head = bytearray(48)
        struct.pack_into('<Q',head,0,self.klass)
        struct.pack_into('<Q',head,16,item if count else 0)
        struct.pack_into('<i',head,24,count)
        struct.pack_into('<Q',head,32,uid)
        head[40] = locked
        self.p.put(address,head)
        return {'address':address,'header':bytes(head),'slot_index':index,
                'name':'gem','stackable':True,'max_stack':50}

    def test_full_warehouse_can_merge_without_empty_slot(self):
        source = self.stack(0x100000,1)
        target = self.stack(0x200000,39)
        plan = plan_transfers([source],[target])
        self.tx.apply_slots(plan['changes'],[],moved=1)
        self.assertEqual(self.p.i32(0x200000+24),40)
        self.assertEqual(self.p.u64(0x100000+16),0)
        self.assertEqual(plan['new_slots'],0)
        self.assertEqual(plan['stacked_count'],1)

    def test_merge_fills_to_limit_and_puts_remainder_in_empty_slot(self):
        source = self.stack(0x100000,36)
        targets = [self.stack(0x200000,38,index=0),self.stack(0x210000,0,index=1)]
        plan = plan_transfers([source],targets)
        self.tx.apply_slots(plan['changes'],[],moved=1)
        self.assertEqual(self.p.i32(0x200000+24),50)
        self.assertEqual(self.p.i32(0x210000+24),24)
        self.assertEqual(plan['stacked_count'],12)
        self.assertEqual(plan['new_slots'],1)

    def test_game_102_limit_99_is_used_instead_of_hardcoded_50(self):
        source=self.stack(0x100000,60);source['max_stack']=99
        targets=[self.stack(0x200000,70,index=0),self.stack(0x210000,0,index=1)]
        plan=plan_transfers([source],targets)
        self.tx.apply_slots(plan['changes'],[],moved=1)
        self.assertEqual(self.p.i32(0x200000+24),99)
        self.assertEqual(self.p.i32(0x210000+24),31)
        self.assertEqual(plan['stacked_count'],29)

    def test_same_name_with_different_grade_reference_does_not_merge(self):
        source = self.stack(0x100000,1,item=0x500000)
        other_grade = self.stack(0x200000,39,item=0x600000)
        empty = self.stack(0x210000,0,index=1)
        plan = plan_transfers([source],[other_grade,empty])
        self.tx.apply_slots(plan['changes'],[],moved=1)
        self.assertEqual(self.p.i32(0x200000+24),39)
        self.assertEqual(self.p.u64(0x210000+16),0x500000)
        self.assertEqual(plan['stacked_count'],0)

    def test_multiple_sources_share_one_stack_and_one_remainder_slot(self):
        sources = [self.stack(0x100000,10),self.stack(0x110000,20)]
        targets = [self.stack(0x200000,45),self.stack(0x210000,0,index=1)]
        plan = plan_transfers(sources,targets)
        self.tx.apply_slots(plan['changes'],[],moved=2)
        self.assertEqual(self.p.i32(0x200000+24),50)
        self.assertEqual(self.p.i32(0x210000+24),25)
        self.assertEqual(plan['new_slots'],1)
        self.assertEqual(self.p.i32(0x100000+24),0)
        self.assertEqual(self.p.i32(0x110000+24),0)

    def test_insufficient_overflow_space_makes_no_changes(self):
        source = self.stack(0x100000,36)
        target = self.stack(0x200000,38)
        with self.assertRaisesRegex(RuntimeError,'空格不足'):
            plan_transfers([source],[target])
        self.assertEqual(self.p.read(source['address'],48),source['header'])
        self.assertEqual(self.p.read(target['address'],48),target['header'])
        self.suspend.assert_not_called()

    def test_a_later_unstackable_item_blocks_the_whole_batch_when_full(self):
        gem = self.stack(0x100000,1)
        gear = self.stack(0x110000,1,item=0x600000,uid=0x900000)
        target = self.stack(0x200000,38)
        with self.assertRaisesRegex(RuntimeError,'空格不足'):
            plan_transfers([gem,gear],[target])
        self.assertEqual(self.p.i32(0x200000+24),38)
        self.assertEqual(self.p.i32(0x100000+24),1)

    def test_partial_allocation_undoes_tentative_merges_of_first_unfitting_stack(self):
        sources = [self.stack(0x100000,1),self.stack(0x110000,20),self.stack(0x120000,1)]
        target = self.stack(0x200000,38)
        plan = plan_transfers(sources,[target],allow_partial=True)
        self.assertEqual((plan['moved'],plan['remaining'],plan['stacked_count']),(1,2,1))
        self.tx.apply_slots(plan['changes'],[],moved=plan['moved'])
        self.assertEqual(self.p.i32(target['address']+24),39)
        self.assertEqual(self.p.u64(sources[0]['address']+16),0)
        self.assertEqual(self.p.read(sources[1]['address'],48),sources[1]['header'])
        self.assertEqual(self.p.read(sources[2]['address'],48),sources[2]['header'])
        self.assertEqual(len(plan['movements']),1)

    def test_partial_capacity_accounts_for_stacking_before_empty_slots(self):
        sources = [self.stack(0x100000,1),self.stack(0x110000,10),self.stack(0x120000,50)]
        targets = [self.stack(0x200000,49),self.stack(0x210000,0,index=1)]
        plan = plan_transfers(sources,targets,allow_partial=True)
        self.assertEqual((plan['moved'],plan['remaining'],plan['new_slots']),(2,1,1))
        self.tx.apply_slots(plan['changes'],[],moved=plan['moved'])
        self.assertEqual(self.p.i32(targets[0]['address']+24),50)
        self.assertEqual(self.p.i32(targets[1]['address']+24),10)
        self.assertEqual(self.p.read(sources[2]['address'],48),sources[2]['header'])

    def test_partial_capacity_does_not_skip_an_unfitting_first_row(self):
        sources = [self.stack(0x100000,1,item=0x600000),self.stack(0x110000,1)]
        plan = plan_transfers(sources,[self.stack(0x200000,49)],allow_partial=True)
        self.assertEqual((plan['moved'],plan['remaining']),(0,2))
        self.assertEqual(plan['changes'],[])
        self.assertEqual(plan['movements'],[])

    def test_partial_mode_validates_all_sources_even_if_capacity_would_stop_earlier(self):
        sources = [self.stack(0x100000,1),self.stack(0x110000,1),self.stack(0x120000,0)]
        with self.assertRaisesRegex(MemoryReadError,'来源物品或数量无效'):
            plan_transfers(sources,[self.stack(0x200000,0)],allow_partial=True)
        self.suspend.assert_not_called()

    def test_generated_instances_never_merge_even_with_the_same_base_item(self):
        source = self.stack(0x100000,1,uid=0x700000)
        target = self.stack(0x200000,1,uid=0x710000)
        empty = self.stack(0x210000,0,index=1)
        plan = plan_transfers([source],[target,empty])
        self.tx.apply_slots(plan['changes'],[],moved=1)
        self.assertEqual(self.p.u64(0x200000+32),0x710000)
        self.assertEqual(self.p.u64(0x210000+32),0x700000)
        self.assertEqual(plan['stacked_count'],0)

    def test_merge_retains_lock_if_either_source_or_target_is_locked(self):
        for source_locked,target_locked in [(0,1),(1,0),(1,1),(0,0)]:
            with self.subTest(source_locked=source_locked,target_locked=target_locked):
                source = self.stack(0x100000,1,locked=source_locked)
                target = self.stack(0x200000,39,locked=target_locked)
                plan = plan_transfers([source],[target])
                self.tx.apply_slots(plan['changes'],[],moved=1)
                self.assertEqual(self.p.read(0x200000+40,1),bytes([source_locked or target_locked]))
                self.assertEqual(self.p.read(0x100000+40,1),b'\0')

    def test_merge_write_failure_rolls_back_counts_and_source_identity(self):
        source = self.stack(0x100000,1)
        target = self.stack(0x200000,39)
        plan = plan_transfers([source],[target])
        def fail_once(address,data):
            self.p.put(address,data)
            if address==target['address']+24:
                self.tx.write = self.p.put
                raise RuntimeError('merge failure')
        self.tx.write = fail_once
        with self.assertRaisesRegex(RuntimeError,'merge failure'):
            self.tx.apply_slots(plan['changes'],[],moved=1)
        self.assertEqual(self.p.read(source['address'],48),source['header'])
        self.assertEqual(self.p.read(target['address'],48),target['header'])
        self.resume.assert_called_once()

    def test_changed_stack_prevents_merge_after_allocation(self):
        source = self.stack(0x100000,1)
        target = self.stack(0x200000,39)
        plan = plan_transfers([source],[target])
        self.p.put(target['address']+24,struct.pack('<i',40))
        with self.assertRaisesRegex(RuntimeError,'准备期间发生变化'):
            self.tx.apply_slots(plan['changes'],[],moved=1)
        self.assertEqual(self.p.i32(source['address']+24),1)
        self.assertEqual(self.p.i32(target['address']+24),40)

    def test_inconsistent_quantity_plan_is_rejected_before_freeze(self):
        source = self.stack(0x100000,1)
        after = replace_fields(source['header'],{24:struct.pack('<i',2)})
        with self.assertRaisesRegex(RuntimeError,'数量或物品编号不一致'):
            self.tx.apply_slots([(source['address'],source['header'],after)],[],moved=1)
        self.suspend.assert_not_called()

    def test_invalid_source_gem_quantity_or_stack_limit_is_rejected(self):
        for count,limit in [(51,50),(1,0),(1,-1)]:
            source = self.stack(0x100000,count)
            source['max_stack'] = limit
            with self.subTest(count=count,limit=limit),self.assertRaisesRegex(RuntimeError,'堆叠上限异常'):
                plan_transfers([source],[self.stack(0x200000,0)])

    def test_empty_string_uid_placeholder_does_not_block_stacking(self):
        empty_string = 0x900000
        self.reader.string = lambda uid:'' if uid==empty_string else str(uid) if uid else None
        for source_uid in (0,empty_string):
            with self.subTest(source_uid=source_uid):
                source = self.stack(0x100000,1,uid=source_uid)
                target = self.stack(0x200000,39,uid=empty_string)
                source['has_unique_uid'] = target['has_unique_uid'] = False
                plan = plan_transfers([source],[target])
                self.tx.apply_slots(plan['changes'],[],moved=1)
                self.assertEqual(self.p.i32(0x200000+24),40)
                self.assertEqual(self.p.u64(0x200000+32),empty_string)
                self.assertEqual(self.p.u64(0x100000+32),0)
                self.assertEqual(plan['new_slots'],0)

    def test_newly_placed_generated_item_cannot_receive_a_later_stack(self):
        source = self.stack(0x100000,1,uid=0x700000)
        second = self.stack(0x110000,1)
        empty = self.stack(0x200000,0)
        with self.assertRaisesRegex(RuntimeError,'空格不足'):
            plan_transfers([source,second],[empty])
