"""Exercise automatic adaptation on file copies, never the running game."""
import argparse
import json
import os
from pathlib import Path
import struct
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from deskrawl_assistant.game_compatibility import ASSET_FILES, resolve
from deskrawl_assistant.il2cpp_metadata import MetadataInspector
from deskrawl_assistant.native_signatures import Signatures


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--game-dir',type=Path,default=Path(r'G:\SteamLibrary\steamapps\common\Deskrawl'))
    args=parser.parse_args();source=args.game_dir
    folder=ROOT/'build'/'auto-compatibility-fixture';folder.mkdir(parents=True,exist_ok=True)
    game=folder/'Deskrawl';data=game/'Deskrawl_Data';metadata=data/'il2cpp_data/Metadata/global-metadata.dat'
    metadata.parent.mkdir(parents=True,exist_ok=True)
    for name in ASSET_FILES:
        target=data/name
        if not target.exists():os.link(source/'Deskrawl_Data'/name,target)
    i=MetadataInspector(source/'Deskrawl_Data/il2cpp_data/Metadata/global-metadata.dat',source/'GameAssembly.dll')
    meta=bytearray(i.meta)
    for old,new in (('gj','zz'),):
        index=next(n for n,d in enumerate(i.defs) if not d['namespace'] and d['name']==old)
        string_index=struct.unpack_from('<i',meta,i.type_offset+index*76)[0]
        position=i.string_offset+string_index
        assert meta[position:position+len(old)]==old.encode()
        meta[position:position+len(old)]=new.encode()
    save=next(n for n,d in enumerate(i.defs) if not d['namespace'] and d['name']=='SaveSystem')
    dirty=next(f for f in i.fields(save) if f['rawRegistrationOffset']==92 and f['resolvedType']=='System.Boolean')
    string_index=struct.unpack_from('<i',meta,i.field_offset+dirty['fieldIndex']*10)[0]
    position=i.string_offset+string_index
    assert meta[position:position+4]==b'nly\0'
    meta[position:position+3]=b'zzz'
    metadata.write_bytes(meta)
    s=Signatures(i);old_rva=s.singleton_rva('GameManager')
    idx=s.defs['GameManager'];getter=next(m for m in i.methods(idx) if m['isStatic'] and m['returnType']=='GameManager' and not m['parameters'])
    module=s.modules['Assembly-CSharp.dll']
    pointer=i.pe.unpack('<Q',module['methods']+((int(getter['token'],16)&0xffffff)-1)*8)[0]
    section=next(t for t in i.pe.sections if t[0]<=old_rva<t[0]+t[3])
    new_rva=(section[0]+section[3]-16)&~7
    binary=bytearray(i.pe.data);position=i.pe.raw(i.pe.base+new_rva,8)
    assert binary[position:position+8]==b'\0'*8 and new_rva!=old_rva
    old=i.pe.raw(i.pe.base+old_rva,8);binary[position:position+8]=binary[old:old+8]
    patches=0
    from capstone.x86 import X86_OP_MEM, X86_REG_RIP
    for ins in s.instructions(pointer):
        if any(op.type==X86_OP_MEM and op.mem.base==X86_REG_RIP and ins.address+ins.size+op.mem.disp==i.pe.base+old_rva for op in ins.operands):
            assert ins.disp_size==4
            position=i.pe.raw(ins.address)+ins.disp_offset
            struct.pack_into('<i',binary,position,i.pe.base+new_rva-ins.address-ins.size);patches+=1
    assert patches
    dll=game/'GameAssembly.dll';dll.write_bytes(binary)
    del s,i,binary,meta
    started=time.monotonic()
    with patch('deskrawl_assistant.game_compatibility.runtime_dir',return_value=folder/'runtime'):
        result=resolve(game,dll,progress=lambda state:print(state['message'],flush=True))
    assert result['status']['phase']=='adapted'
    assert result['bindings']['save_dirty_field']=='zzz'
    assert result['native_layout']['game_manager_rva']==new_rva
    registry=next(t for t in result['profiles']['runtime-type-hints.json']['types'] if t.get('logicalName')=='gj')
    assert registry['name']=='zz'
    report={'success':True,'seconds':round(time.monotonic()-started,2),'status':result['status'],
        'save_field':'nly → zzz','registry_type':'gj → zz',
        'old_game_manager_rva':hex(old_rva),'new_game_manager_rva':hex(new_rva),
        'original_game_files_modified':False,'game_writes':False}
    (folder/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
