"""Real ground/carriage readers agree on UID-less drop identity; no game access."""
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.carriage_transfer import read_carriage
from deskrawl_assistant.game_actions import GameActions
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


if __name__=='__main__': unittest.main()
