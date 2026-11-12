"""Warehouse page entitlement guards for the verified 1.1.11 layout."""
import json
from types import SimpleNamespace
import struct
import unittest

from deskrawl_assistant.container_transfer import access_state, GAME_MANAGER_RVA, CLOUD_CLIENT_RVA
from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.paths import RESOURCE_ROOT
from test_container_transfer_probe import FakeMemory


class StorageAccessTests(unittest.TestCase):
    def setUp(self):
        self.p = FakeMemory()
        self.base = 0x10000000
        self.objects = {name:0x200000+i*0x1000 for i,name in enumerate(
            ('GameManager','PlayerCombatController','Player','SaveSystem','CloudClient','DlcState','LocalDlcEntitlement'))}
        self.classes = {name:obj+0x100000 for name,obj in self.objects.items()}
        profiles = {t['name']:t for t in json.loads((RESOURCE_ROOT/'data/container-transfer-types.json').read_text(encoding='utf-8'))}
        fields = {self.classes[name]:[
            {'name':f['name'],'offset':f['rawRegistrationOffset'],'token':int(f['token'],16)}
            for f in profiles[name]['fields']] for name in self.objects if name in profiles}
        self.reader = SimpleNamespace(memory=self.p, module={'base':self.base},
            classes={'SaveSystem':self.classes['SaveSystem']}, singleton=lambda name:self.objects[name],
            fields=lambda klass:fields[klass], class_name=lambda klass:next(n for n,k in self.classes.items() if k==klass))
        for name,obj in self.objects.items():
            self.pointer(obj,self.classes[name])
            self.pointer(self.classes[name]+184,obj+0x200000)
            self.pointer(obj+0x200000,obj)
        for rva,name in ((GAME_MANAGER_RVA,'GameManager'),(CLOUD_CLIENT_RVA,'CloudClient')):
            self.pointer(self.base+rva,self.classes[name])
        self.pointer(self.objects['GameManager']+72,self.objects['PlayerCombatController'])
        self.pointer(self.objects['PlayerCombatController']+32,self.objects['Player'])
        self.integer(self.objects['Player']+616,3)
        self.integer(self.objects['SaveSystem']+32,1)
        self.pointer(self.objects['CloudClient']+424,0xDEAD0000)  # Previous build's field is not a DLC object.
        self.pointer(self.objects['CloudClient']+432,self.objects['DlcState'])
        self.p.put(self.objects['DlcState']+16,b'\1')
        self.pointer(self.objects['SaveSystem']+240,self.objects['LocalDlcEntitlement'])
        self.p.put(self.objects['LocalDlcEntitlement']+28,b'\1')

    def pointer(self,address,value): self.p.put(address,struct.pack('<Q',value))
    def integer(self,address,value): self.p.put(address,struct.pack('<i',value))

    def test_online_dlc_uses_new_field_and_guards_it(self):
        state,guards = access_state(self.reader)
        self.assertEqual(state['storage_indices'],list(range(250)))
        self.assertEqual(state['storage_pages'],5)
        observed = dict(guards)
        self.assertIn(self.objects['CloudClient']+432,observed)
        self.assertNotIn(self.objects['CloudClient']+424,observed)
        self.pointer(self.objects['CloudClient']+432,0)
        self.assertNotEqual(self.p.read(self.objects['CloudClient']+432,8),observed[self.objects['CloudClient']+432])

    def test_without_online_entitlement_only_open_normal_pages_are_allowed(self):
        self.integer(self.objects['Player']+616,1)
        self.p.put(self.objects['DlcState']+16,b'\0')
        state,_ = access_state(self.reader)
        self.assertEqual(state['storage_indices'],list(range(100)))
        self.assertEqual(state['storage_pages'],2)

    def test_offline_entitlement_keeps_its_verified_layout(self):
        self.integer(self.objects['SaveSystem']+32,0)
        self.integer(self.objects['Player']+616,0)
        state,_ = access_state(self.reader)
        self.assertEqual(state['storage_indices'],list(range(50))+list(range(200,250)))

    def test_invalid_entitlement_byte_stops_operation(self):
        self.p.put(self.objects['DlcState']+16,b'\2')
        with self.assertRaisesRegex(MemoryReadError,'状态异常'):
            access_state(self.reader)

    def test_both_dlc_entitlements_are_independent_online(self):
        for first in (0, 1):
            for pack in (0, 1):
                with self.subTest(first=first, pack=pack):
                    self.p.put(self.objects['DlcState']+16,bytes([first]))
                    self.p.put(self.objects['DlcState']+18,bytes([pack]))
                    state, guards = access_state(self.reader)
                    expected = list(range(200))
                    if first: expected += list(range(200,250))
                    if pack: expected += list(range(250,400))
                    self.assertEqual(state['storage_indices'],expected)
                    self.assertEqual(state['storage_pages'],4+first+3*pack)
                    self.assertIn(self.objects['DlcState']+18,dict(guards))

    def test_storage_pack_is_guarded_offline_and_invalid_flags_fail_closed(self):
        self.integer(self.objects['SaveSystem']+32,0)
        self.pointer(self.objects['SaveSystem']+248,self.objects['LocalDlcEntitlement'])
        state, guards = access_state(self.reader)
        self.assertEqual(state['storage_indices'],list(range(400)))
        self.assertIn(self.objects['SaveSystem']+248,dict(guards))
        self.assertIn(self.objects['LocalDlcEntitlement']+28,dict(guards))
        self.integer(self.objects['SaveSystem']+32,1)
        self.p.put(self.objects['DlcState']+18,b'\2')
        with self.assertRaisesRegex(MemoryReadError,'状态异常'):
            access_state(self.reader)


if __name__=='__main__': unittest.main()
