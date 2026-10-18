"""Real ground/carriage readers agree on UID-less drop identity; no game access."""
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.carriage_transfer import collect_item, read_carriage
from deskrawl_assistant.game_actions import GameActions
from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.paths import RESOURCE_ROOT


class Memory:
    def __init__(self): self.data = {}
    def put(self, address, block): self.data.update((address+i,b) for i,b in enumerate(block))
    def read(self, address, size): return bytes(self.data.get(address+i,0) for i in range(size))
    def u64(self, address): return struct.unpack('<Q',self.read(address,8))[0]


class CarriageDropIdentityTests(unittest.TestCase):
    def test_same_native_drop_keeps_identity_and_selection_when_loaded_onto_carriage(self):
        for uid in (None, '', 'stable-item-uid'):
            with self.subTest(uid=uid):
                memory = Memory()
                profiles = {row['name']:row for row in json.loads(
                    (RESOURCE_ROOT/'data/carriage-types.json').read_text(encoding='utf-8'))}
                classes = {'GameManager':0x1000, 'HorseCarriage':0x1100, 'LootDrop':0x1200, 'List`1':0x1300}
                names = {value:key for key,value in classes.items()}
                def fields(klass):
                    name = names[klass]
                    if name=='List`1': return [{'name':key,'offset':offset}
                        for key,offset in (('_items',16),('_size',24),('_version',28))]
                    return [{'name':f['name'],'offset':f['rawRegistrationOffset'],'token':int(f['token'],16)}
                            for f in profiles[name]['fields']]
                def pointer(address, value): memory.put(address,struct.pack('<Q',value))
                base, static, manager, carriage, drop, item = 0x3000,0x4000,0x5000,0x6000,0x7000,0x8000
                pointer(base,classes['GameManager']); pointer(classes['GameManager']+184,static)
                pointer(static,manager); pointer(manager,classes['GameManager'])
                pointer(manager+88,carriage); pointer(carriage,classes['HorseCarriage'])
                pointer(carriage+152,0xa000); pointer(manager+216,0xb000)
                for root,array,count in ((0xa000,0xc000,0),(0xb000,0xd000,1)):
                    pointer(root,classes['List`1'])
                    memory.put(root+16,struct.pack('<Qii',array,count,0)); pointer(array+32,drop)
                header = bytearray(216)
                struct.pack_into('<Q',header,0,classes['LootDrop'])
                struct.pack_into('<Q',header,16,0x9000)
                struct.pack_into('<Q',header,120,item)
                struct.pack_into('<Q',header,136,0xe000 if uid else 0)
                struct.pack_into('<i',header,168,1)
                memory.put(drop,header)
                reader = SimpleNamespace(memory=memory, session_id='drop-identity-fixture', module={'base':base},
                    classes={}, _carriage_profiles=profiles, fields=fields, class_name=lambda klass:names[klass],
                    string=lambda address:uid, base_item=lambda address:{'internal_name':'GoldBar','item_type':7},
                    array=lambda address,*args,**kwargs:([memory.read(address+32,8)],memory.read(address+32,8)))
                game = object.__new__(GameActions); game.reader = reader
                game._enrich_drops = lambda read,rows:rows
                read = Mock(); read.ptr.side_effect = memory.u64; read.list.return_value = [drop]; read.guards = []
                with patch('deskrawl_assistant.carriage_transfer.assert_current'), \
                        patch('deskrawl_assistant.carriage_transfer.native_rva',return_value=0):
                    ground = game._ground(read,manager)[0]
                    memory.put(0xa000+24,struct.pack('<i',1))
                    loaded = read_carriage(reader)['items'][0]
                    self.assertEqual(game._ground(read,manager), [])
                self.assertEqual(ground['item_uid'],uid or '')
                self.assertEqual(loaded['item_uid'],uid or '')
                self.assertEqual(ground['observation_id'],loaded['observation_id'])
                self.assertEqual(ground['selection_id'],loaded['selection_id'])


class CarriageCollectionReadbackTests(unittest.TestCase):
    def collect(self, drop_uid='', inventory_uid=None, count=1, change=None):
        amounts = [min(99, count-offset) for offset in range(0, count, 99)]
        slots = [dict(slot_index=index, internal_name='GemEmerald4', count=amount,
                      item_uid=inventory_uid) for index,amount in enumerate(amounts)]
        if change:
            change(slots)
        def snapshot(rows):
            return dict(complete=True, containers={'inventory':{'slots':rows}, 'storage':{'slots':[]}})
        addresses = [3000+1000*index for index in range(len(amounts))]
        pointers = {1056:2000, 816:0, **{address:77 for address in addresses}}
        reader = SimpleNamespace(snapshot=Mock(side_effect=[snapshot([]), snapshot(slots)]),
            memory=SimpleNamespace(u64=lambda address:pointers[address], read=lambda address,size:b'\0'*size),
            singleton=lambda name:1000,
            array=lambda *args,**kwargs:([struct.pack('<Q',address) for address in addresses], b'block'),
            snapshot_stamps=[], classes={'InventorySlot':77})
        drop = dict(internal_name='GemEmerald4', item_uid=drop_uid, count=count, max_stack=99,
                    item_class='GemData', selection_id='pick', observation_id='observe', collectable=True,
                    slot_index=5, _address=800, _header=b'x'*216)
        tx = Mock()
        tx.collect_many.return_value = {'save_requested':True}
        receipts = []
        with patch('deskrawl_assistant.carriage_transfer.read_carriage', return_value={'items':[drop], 'guards':[]}), \
                patch('deskrawl_assistant.carriage_transfer.FrozenCarriageTransaction', return_value=tx):
            try:
                result = collect_item(reader, 'pick', on_commit=receipts.append)
            finally:
                tx.collect_many.assert_called_once()
                tx.close.assert_called_once()
                self.assertEqual(len(receipts), 1)
        return result

    def test_uidless_readback_accepts_both_null_and_empty_uid(self):
        for drop_uid in ('', None):
            for inventory_uid in ('', None):
                with self.subTest(drop_uid=drop_uid, inventory_uid=inventory_uid):
                    result = self.collect(drop_uid, inventory_uid)
                    self.assertEqual(result['moved'], 1)
                    self.assertEqual(result['movements'][0]['identity'], '')

    def test_uidless_split_stacks_are_verified_and_receipted_once(self):
        result = self.collect(count=101)
        self.assertEqual(result['new_slots'], 2)
        self.assertEqual([move['count'] for move in result['movements']], [99, 2])

    def test_real_uid_still_requires_exact_match(self):
        self.assertEqual(self.collect('equipment-id', 'equipment-id')['moved'], 1)
        for drop_uid,inventory_uid in (('equipment-id', 'different-id'), ('equipment-id', None),
                                       ('equipment-id', ''), ('', 'unexpected-id')):
            with self.subTest(drop_uid=drop_uid, inventory_uid=inventory_uid):
                with self.assertRaisesRegex(MemoryReadError, '回读不一致'):
                    self.collect(drop_uid, inventory_uid)

    def test_name_count_and_destination_still_require_exact_match(self):
        for key,value in (('internal_name','GemSapphire4'), ('count',2), ('slot_index',1)):
            with self.subTest(field=key):
                with self.assertRaisesRegex(MemoryReadError, '回读不一致'):
                    self.collect(change=lambda slots:slots[0].update({key:value}))
        with self.assertRaisesRegex(MemoryReadError, '回读不一致'):
            self.collect(change=lambda slots:slots.clear())


if __name__=='__main__': unittest.main()
