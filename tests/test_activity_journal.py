"""Persistent event counts, identity-aware observation and transfer receipts."""
import copy
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.activity_journal import ActivityJournal, ItemActivityTracker
from deskrawl_assistant.carriage_transfer import collect_item
from deskrawl_assistant.container_transfer import plan_transfers
from deskrawl_assistant.native_memory import SnapshotChangedError


def row(index=0, count=1, container='inventory', key='GemEmerald4', uid=''):
    return dict(slot_index=index,count=count,container=container,internal_name=key,item_uid=uid)


def snapshot(items=(), session='session-1', complete=True):
    return dict(bridge_session=session,complete=complete,
        containers={c:{'slots':[copy.deepcopy(i) for i in items if i['container']==c]} for c in ('inventory','storage')})


def carriage(items=()):
    return dict(available=True,items=[dict(i,observation_id=i.get('observation_id','drop-1')) for i in items])


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'activity.sqlite3'
        self.journal=ActivityJournal(self.path)
        self.tracker=ItemActivityTracker(self.journal,lambda key:(key,'gem'))

    def events(self,event):
        return [r for r in self.journal.page(limit=200)['entries'] if r['event']==event]

    def test_existing_items_are_baseline_and_counts_are_persistent(self):
        self.tracker.observe(snapshot([row(count=5)]),carriage([row(count=3)]))
        self.assertFalse(self.events('item_acquired'));self.assertFalse(self.events('carriage_acquired'))
        self.tracker.observe(snapshot([row(count=8),row(2,count=4)]),carriage([row(count=7)]))
        got=self.events('item_acquired')
        self.assertEqual(sorted(r['quantity'] for r in got),[3,4])
        increased=next(r for r in got if r['quantity']==3)
        self.assertEqual(increased['destination'],dict(container='inventory',slot_index=0,count_before=5,count_after=8))
        self.assertEqual(self.events('carriage_acquired')[0]['quantity'],4)
        self.assertEqual(ActivityJournal(self.path).page()['total'],4)

    def test_sorting_and_manual_stack_merge_are_transfers_not_loot(self):
        self.tracker.observe(snapshot([row(0,5),row(3,90,'storage')]))
        self.tracker.observe(snapshot([row(3,95,'storage')]))
        self.assertFalse(self.events('item_acquired'))
        moved=self.events('game_item_moved')[0]
        self.assertEqual(moved['quantity'],5)
        self.assertEqual(moved['source']['count_after'],0)
        self.assertEqual(moved['destination']['count_before'],90)
        self.tracker.observe(snapshot([row(8,95,'storage')]))
        self.assertFalse(self.events('item_acquired'))

    def test_replacement_by_different_unique_equipment_is_new_loot(self):
        self.tracker.observe(snapshot([row(key='LegendaryBelt3',uid='old')]))
        self.tracker.observe(snapshot([row(key='LegendaryBelt3',uid='new')]))
        self.assertEqual(self.events('item_acquired')[0]['quantity'],1)

    def test_incomplete_snapshots_and_transient_carriage_do_not_reset_baseline(self):
        self.tracker.observe(snapshot([row(count=7)]),carriage([row(count=2)]))
        self.tracker.observe(snapshot([],complete=False),carriage([]))
        self.tracker.observe(snapshot([row(count=7)]),dict(available=False,complete=False))
        self.tracker.observe(snapshot([row(count=7)]),carriage([row(count=3)]))
        self.assertFalse(self.events('item_acquired'))
        self.assertEqual(self.events('carriage_acquired')[0]['quantity'],1)

    def test_new_game_session_is_a_new_baseline(self):
        self.tracker.observe(snapshot([row(count=2)]))
        self.tracker.observe(snapshot([row(count=99)],session='different'))
        self.assertFalse(self.events('item_acquired'))
        self.assertEqual(len(self.events('observation_baseline')),2)

    def test_confirmed_tool_moves_prevent_duplicate_acquisition_and_transfer(self):
        self.tracker.observe(snapshot([row(3,90,'storage')]),carriage([]))
        drop=row(2,count=5);drop['observation_id']='gem-drop'
        self.tracker.observe(snapshot([row(3,90,'storage')]),carriage([drop]))
        self.tracker.apply_movements([dict(name='GemEmerald4',identity=None,observation_id='gem-drop',
            source=dict(container='carriage',slot_index=2,count_before=5,count_after=0),
            destination=dict(container='inventory',slot_index=0,count_before=0,count_after=5))])
        self.tracker.observe(snapshot([row(0,5),row(3,90,'storage')]),carriage([]))
        self.tracker.apply_movements([dict(name='GemEmerald4',
            source=dict(container='inventory',slot_index=0,count_before=5,count_after=0),
            destination=dict(container='storage',slot_index=3,count_before=90,count_after=95))])
        self.tracker.observe(snapshot([row(3,95,'storage')]),carriage([]))
        self.assertFalse(self.events('item_acquired'));self.assertFalse(self.events('game_item_moved'))
        self.assertEqual(len(self.events('carriage_acquired')),1)

    def test_cursor_filter_literal_search_and_deduplication(self):
        for n in range(7):
            self.journal.append('lock','equipment_locked','锁定 '+str(n),item_name='50%_'+str(n),event_key=str(n))
        self.journal.append('lock','equipment_locked','重复',event_key='0')
        first=self.journal.page(category='lock',query='50%_',limit=3)
        self.assertEqual(first['total'],7)
        second=self.journal.page(category='lock',query='50%_',before=first['next_before'],limit=3)
        third=self.journal.page(category='lock',before=second['next_before'],limit=3)
        ids=[r['id'] for page in (first,second,third) for r in page['entries']]
        self.assertEqual(len(ids),7);self.assertEqual(len(set(ids)),7);self.assertIsNone(third['next_before'])

    def test_bad_page_or_quantity_arguments_are_rejected(self):
        for arguments in [dict(limit=True),dict(limit=201),dict(before=0),dict(category='bad'),dict(query='x'*201)]:
            with self.assertRaises(ValueError):self.journal.page(**arguments)
        for quantity in (True,-1,1.5):
            with self.assertRaises(ValueError):self.journal.append('loot','item_acquired','bad',quantity=quantity)

    def test_split_merge_receipts_conserve_counts_for_multiple_sources(self):
        def header(count,item=123):
            result=bytearray(48)
            if count:struct.pack_into('<Qi',result,16,item,count)
            return bytes(result)
        sources=[dict(address=n,slot_index=n,header=header(count),name='GemEmerald4',
                      stackable=True,max_stack=99,has_unique_uid=False) for n,count in ((1,10),(2,7))]
        targets=[dict(address=10,slot_index=105,header=header(95),has_unique_uid=False),
                 dict(address=11,slot_index=106,header=header(0),has_unique_uid=False)]
        plan=plan_transfers(sources,targets)
        moves=plan['movements']
        self.assertEqual([m['count'] for m in moves],[4,6,7])
        self.assertEqual(sum(m['count'] for m in moves),17)
        self.assertEqual(moves[0]['destination']['count_after'],99)
        self.assertEqual(moves[-1]['destination'],dict(slot_index=106,count_before=6,count_after=13))
        self.assertEqual(moves[1]['source'],dict(slot_index=1,count_before=6,count_after=0))

    def test_carriage_receipt_is_delivered_before_post_commit_snapshot_failure(self):
        drop=dict(internal_name='GemEmerald4',item_uid=None,count=101,max_stack=99,item_class='GemData',
                  selection_id='pick',observation_id='observe',collectable=True,slot_index=5,_address=800,_header=b'x'*216)
        pointers={1056:2000,3000:77,4000:77,816:0}
        reader=SimpleNamespace(snapshot=Mock(side_effect=[snapshot(),SnapshotChangedError('post-commit change')]),
            memory=SimpleNamespace(u64=lambda a:pointers[a],read=lambda a,n:b'\0'*n),
            singleton=lambda name:1000,array=lambda *args,**kwargs:([struct.pack('<Q',v) for v in (3000,4000)],b'block'),
            snapshot_stamps=[],classes={'InventorySlot':77})
        tx=Mock();tx.collect_many.return_value={'save_requested':True}
        received=[]
        with patch('deskrawl_assistant.carriage_transfer.read_carriage',return_value={'items':[drop],'guards':[]}), \
             patch('deskrawl_assistant.carriage_transfer.FrozenCarriageTransaction',return_value=tx):
            with self.assertRaises(SnapshotChangedError):collect_item(reader,'pick',on_commit=received.append)
        self.assertEqual(len(received),1)
        movements=received[0]['movements']
        self.assertEqual([m['count'] for m in movements],[99,2])
        self.assertEqual([m['destination']['slot_index'] for m in movements],[0,1])
        self.assertEqual(movements[-1]['source']['count_after'],0)
        tx.close.assert_called_once()


if __name__=='__main__':unittest.main()
