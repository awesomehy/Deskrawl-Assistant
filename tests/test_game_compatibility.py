"""Ordinary changes may pass; resource/layout/logic changes must fail closed."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from deskrawl_assistant.game_compatibility import (CompatibilityError, bindings,
    check_assets, field_shape, inspect_changes, match_profiles, native_rva)
from deskrawl_assistant.il2cpp_metadata import MetadataInspector, PE
from deskrawl_assistant.native_reader import NativeReader
from deskrawl_assistant.native_signatures import Signatures


def field(name='old', offset=92, typ='System.Boolean'):
    return {'name':name,'rawRegistrationOffset':offset,'resolvedType':typ,'typeKind':2,
        'isStatic':False,'isLiteral':False,'isInitOnly':False,'token':'0x4000001'}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.original={'runtime-type-hints.json':{'types':[{'name':'SaveSystem','namespace':'',
            'fields':[field()],'methods':[]}]}}
        self.fields=[field('new')]
        self.i=SimpleNamespace(defs=[{'name':'SaveSystem','namespace':''}],
            fields=lambda n:self.fields,methods=lambda n:[],meta=b'meta',pe=SimpleNamespace(data=b'dll'))

    def test_rename_and_tokens_are_rediscovered_without_mutating_baseline(self):
        self.fields[0]['token']='0x4000088'
        result,_=match_profiles(self.i,self.original)
        t=result['runtime-type-hints.json']['types'][0]
        self.assertEqual(t['fields'][0]['name'],'new')
        self.assertEqual(t['fields'][0]['token'],'0x4000088')
        self.assertEqual(self.original['runtime-type-hints.json']['types'][0]['fields'][0]['name'],'old')

    def test_layout_type_added_field_and_ambiguous_type_are_rejected(self):
        for change in (lambda f:f[0].update(rawRegistrationOffset=93),
                       lambda f:f[0].update(resolvedType='System.Int32'),
                       lambda f:f.append(field('extra',94))):
            self.fields=[field()];change(self.fields)
            with self.assertRaises(CompatibilityError):match_profiles(self.i,self.original)
        self.i.defs.append(deepcopy(self.i.defs[0]))
        with self.assertRaises(CompatibilityError):match_profiles(self.i,self.original)

    def test_extra_readers_receive_current_hashes_names_and_tokens(self):
        original=deepcopy(self.original)
        original['experience-types.json']={'metadataSha256':'old','gameAssemblySha256':'old','types':deepcopy(original['runtime-type-hints.json']['types'])}
        result,_=match_profiles(self.i,original)
        self.assertEqual(result['experience-types.json']['metadataSha256'],result['runtime-type-hints.json']['metadataSha256'])
        self.assertEqual(result['experience-types.json']['gameAssemblySha256'],result['runtime-type-hints.json']['gameAssemblySha256'])
        self.assertEqual(result['experience-types.json']['types'][0]['fields'][0]['name'],'new')

    def test_registry_rename_needs_a_unique_complete_shape(self):
        self.original['runtime-type-hints.json']['types'][0]['name']='gj'
        self.i.defs[0]['name']='zz'
        result,aliases=match_profiles(self.i,self.original)
        self.assertEqual(aliases,{'zz':'gj'})
        self.assertEqual(result['runtime-type-hints.json']['types'][0]['logicalName'],'gj')
        self.i.defs.append({'name':'aa','namespace':''})
        with self.assertRaises(CompatibilityError):match_profiles(self.i,self.original)

    def test_recommendation_internal_type_rename_retains_its_own_identity(self):
        self.original['runtime-type-hints.json']['types'][0]['name']='ey'
        self.i.defs[0]['name']='xy'
        result,aliases=match_profiles(self.i,self.original)
        self.assertEqual(aliases,{'xy':'ey'})
        self.assertEqual(result['runtime-type-hints.json']['types'][0]['logicalName'],'ey')

    def test_enum_change_stops_before_native_analysis(self):
        with patch('deskrawl_assistant.game_compatibility.match_profiles',return_value=({},{})), \
             patch('deskrawl_assistant.game_compatibility.enum_contract',return_value={'StatType':[['A',2]]}), \
             patch('deskrawl_assistant.game_compatibility.native_contract') as native:
            with self.assertRaisesRegex(CompatibilityError,'枚举改变'):
                inspect_changes(self.i,{'enums':{'StatType':[['A',1]]}},self.original)
            native.assert_not_called()

    def test_native_logic_change_is_rejected(self):
        with patch('deskrawl_assistant.game_compatibility.match_profiles',return_value=({},{})), \
             patch('deskrawl_assistant.game_compatibility.enum_contract',return_value={}), \
             patch('deskrawl_assistant.game_compatibility.native_contract',return_value=({'fn':'changed'},{})):
            with self.assertRaisesRegex(CompatibilityError,'代码特征不一致'):
                inspect_changes(self.i,{'enums':{},'native_methods':{'fn':'old'}},self.original)

    def test_bindings_follow_field_names_at_verified_offsets(self):
        p={'types':[{'name':'SaveSystem','fields':[field('zzz')]},
            {'name':'GeneratedItemData','fields':[field('abc',73)]}]}
        self.assertEqual(bindings(p),{'save_dirty_field':'zzz','lock_field':'abc'})


class AssetAndSessionTests(unittest.TestCase):
    def test_new_resource_or_changed_data_is_not_automatically_accepted(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            game=Path(folder);root=game/'Deskrawl_Data';root.mkdir()
            p=root/'resources.assets';p.write_bytes(b'known')
            baseline={'assets':{'resources.assets':hashlib.sha256(b'known').hexdigest()}}
            check_assets(game,baseline)
            p.write_bytes(b'other')
            with self.assertRaisesRegex(CompatibilityError,'资源已变化'):check_assets(game,baseline)
            p.write_bytes(b'known');(root/'new.assets').write_bytes(b'new')
            with self.assertRaisesRegex(CompatibilityError,'资源文件有增减'):check_assets(game,baseline)

    def test_connected_file_update_blocks_and_reports_before_operations(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'Deskrawl_Data').mkdir();p=root/'GameAssembly.dll';p.write_bytes(b'old')
            r=object.__new__(NativeReader);events=[]
            r.memory=SimpleNamespace(path=root/'Deskrawl.exe')
            r.on_compatibility=events.append
            stat=p.stat();r.file_stamps={str(p):(stat.st_size,stat.st_mtime_ns)}
            r.check_game_files();p.write_bytes(b'updated')
            with self.assertRaises(CompatibilityError):r.check_game_files()
            self.assertEqual(events[-1]['phase'],'changed')

    def test_native_addresses_are_per_connection(self):
        r=SimpleNamespace(native_layout={'game_manager_rva':0x123456})
        self.assertEqual(native_rva(r,'game_manager_rva',99),0x123456)
        self.assertEqual(native_rva(SimpleNamespace(),'game_manager_rva',99),99)

    def test_unrecognized_metadata_and_invalid_pe_are_rejected(self):
        for value in (b'', b'\x00'*512):
            with self.assertRaises(ValueError):MetadataInspector(value,value)
            with self.assertRaises(ValueError):PE(value)


class NativeCodeTests(unittest.TestCase):
    def test_obfuscated_static_state_rename_keeps_native_type_signature(self):
        def signature(name,aliases):
            s=object.__new__(Signatures);s.aliases={'gj':'gj',**aliases};s.defs={name:0};s.shape_cache={}
            s.i=SimpleNamespace(defs=[{'isValueType':False}],fields=lambda _: [field('difficulty',0,'WorldDifficulty')])
            return s.type_name(name)
        self.assertEqual(signature('ey',{}),signature('zz',{'zz':'ey'}))

    def signature(self,code,rva=0,data=b'',data_rva=100):
        s=object.__new__(Signatures)
        base=0x180000000;content=bytearray(512)
        content[rva:rva+len(code)]=code;content[data_rva:data_rva+len(data)]=data
        s.pe=SimpleNamespace(base=base,data=bytes(content),
            raw=lambda addr,length=1:addr-base,contains=lambda addr,size=1:base<=addr<base+512)
        s.i=SimpleNamespace(registration=[0]*16,method_count=0,section=lambda *args:(0,0,0))
        s.dis=Cs(CS_ARCH_X86,CS_MODE_64);s.dis.detail=True
        s.starts=[base+rva,base+rva+len(code)];s.ends={base+rva:base+rva+len(code)}
        s.body_cache={};s.pointer_ids={};s.helpers={};s.bss={};s.imports={}
        return s.body(base+rva)

    def test_code_relocation_passes_but_immediate_mask_change_does_not(self):
        old=b'\xb8\x34\x12\x00\x00\xc3'
        self.assertEqual(self.signature(old),self.signature(old,rva=32))
        self.assertNotEqual(self.signature(old),self.signature(b'\xb8\x35\x12\x00\x00\xc3'))

    def test_rip_relocation_retains_the_referenced_constant(self):
        def code(rva,data_rva):return b'\x8b\x05'+struct.pack('<i',data_rva-rva-6)+b'\xc3'
        a=self.signature(code(0,100),data=struct.pack('<I',123))
        b=self.signature(code(32,200),rva=32,data_rva=200,data=struct.pack('<I',123))
        c=self.signature(code(32,200),rva=32,data_rva=200,data=struct.pack('<I',124))
        self.assertEqual(a,b);self.assertNotEqual(a,c)

    def test_branch_condition_and_destination_are_preserved(self):
        a=b'\x74\x05\xb8\x01\x00\x00\x00\xc3'
        b=b'\x75'+a[1:]
        self.assertNotEqual(self.signature(a),self.signature(b))


if __name__=='__main__':unittest.main()
