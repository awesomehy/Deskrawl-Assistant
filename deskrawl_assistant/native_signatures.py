"""Normalize relocation operands, retaining branches, constants and field offsets.

Only file bytes are inspected; no DLL load, native calls or game mutations.
"""
from bisect import bisect_right
from collections import defaultdict
import hashlib
import json
import re
import struct

from .il2cpp_metadata import MetadataInspector


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=True,separators=(',',':'),sort_keys=True).encode()).hexdigest()


class Signatures:
    def __init__(self, inspector, aliases=None):
        from capstone import Cs,CS_ARCH_X86,CS_MODE_64
        self.i=inspector;self.pe=inspector.pe
        self.dis=Cs(CS_ARCH_X86,CS_MODE_64);self.dis.detail=True
        self.aliases={'gj':'gj',**(aliases or {})}
        self.defs={('.'.join(filter(None,(d['namespace'],d['name'])))):n for n,d in enumerate(inspector.defs)}
        self.shape_cache={};self.method_ids={};self.pointer_ids=defaultdict(list);self.method_pointers={}
        self.exports=self.pe.exports();self.ends=self.pe.unwind_functions()
        self.imports=self.pe.imports()
        self.modules={}
        for image in inspector.images():
            module=self.find_module(image['name']) if image['name'] in ('Assembly-CSharp.dll','ACTk.Runtime.dll') else None
            if module:self.modules[image['name']]=module
            for type_index in range(image['start'],image['start']+image['count']):
                counts=defaultdict(int)
                cls=self.type_name(self.full_name(type_index))
                for order_in_type,m in enumerate(inspector.methods(type_index)):
                    shape=(m['isStatic'],self.type_name(m['returnType']),tuple(self.type_name(p['resolvedType']) for p in m['parameters']))
                    order=counts[shape];counts[shape]+=1
                    # Stable named callbacks/constructors retain their names.
                    stable='' if re.fullmatch('[a-z]{2,4}',m['name']) else m['name']
                    key=image['name']+'|'+cls+'|'+digest((shape,order,stable))
                    rid=int(m['token'],16)&0xffffff
                    if module and not 1<=rid<=module['count']:raise ValueError('Method token outside code module')
                    ptr=self.pe.unpack('<Q',module['methods']+(rid-1)*8)[0] if module else 0
                    self.method_ids[(type_index,rid)]=key
                    global_index=inspector.defs[type_index]['firstMethod']+order_in_type
                    self.method_ids[global_index]=key
                    if ptr:
                        self.pe.raw(ptr)
                        self.pointer_ids[ptr].append(key);self.method_pointers[key]=ptr
        for name,ptr in self.exports.items():self.pointer_ids[ptr].append('export:'+name)
        self.starts=sorted(set(self.pointer_ids)|set(self.ends))
        self.body_cache={}
        self.helpers={}
        self.bss={}

    def full_name(self,index):
        d=self.i.defs[index]
        return '.'.join(filter(None,(d['namespace'],d['name'])))

    def type_name(self,name,stack=()):
        if self.aliases.get(name)=='gj':return 'gj'
        # Type/method obfuscation can rename short internal classes as well.
        def replace(m):
            raw=m.group()
            if self.aliases.get(raw)=='gj':return 'gj'
            if raw in self.defs and re.fullmatch('[a-z]{1,4}',raw):
                if raw in stack:return 'cycle'
                if len(stack)>6:raise ValueError('Internal type shape exceeds supported depth')
                if raw not in self.shape_cache:
                    idx=self.defs[raw];d=self.i.defs[idx]
                    shape=[(f['rawRegistrationOffset'],f['isStatic'],f['typeKind'],
                        self.type_name(f['resolvedType'],stack+(raw,))) for f in self.i.fields(idx)]
                    self.shape_cache[raw]='internal:'+digest((d['isValueType'],shape))
                return self.shape_cache[raw]
            return raw
        return re.sub(r'[A-Za-z_][A-Za-z_0-9.`]*',replace,name)

    def find_module(self,name):
        needle=name.encode()+b'\0';found=[];pos=0
        while (pos:=self.pe.data.find(needle,pos))>=0:
            addr=self.pe.va(pos);ref=0
            while (ref:=self.pe.data.find(struct.pack('<Q',addr),ref))>=0:
                if ref+24<=len(self.pe.data):
                    _,count,methods=struct.unpack_from('<QQQ',self.pe.data,ref)
                    try:
                        if not 0<count<200000:raise ValueError('Not a module')
                        self.pe.raw(methods,count*8)
                        found.append({'name':name,'count':count,'methods':methods})
                    except ValueError:pass
                ref+=1
            pos+=1
        if len(found)!=1:raise ValueError('Code module is missing or ambiguous: '+name)
        return found[0]

    def reference(self,address,size):
        """Resolve metadata uses semantically; never erase numeric constants."""
        if address in self.imports:return ('import',self.imports[address])
        try:at=self.pe.raw(address,max(size,8))
        except ValueError:
            if self.pe.contains(address,size):return ('bss',)
            raise ValueError('Native reference outside image: '+hex(address-self.pe.base)+' / '+str(size))
        value=struct.unpack_from('<Q',self.pe.data,at)[0]
        # Numeric constants can resemble encoded metadata. Only recognize an
        # encoded reference when its index belongs to that particular table.
        kind,index=value>>29,(value&0x1fffffff)>>1
        limits={1:self.i.registration[6],2:self.i.registration[6],3:self.i.method_count,
                4:self.i.section(272,6)[2],5:self.i.section(8,4)[2]-1,6:self.i.registration[8]}
        if size==8 and address%8==0 and value<=0xffffffff and value&1 and 0<=index<limits.get(kind,0):
            if kind in (1,2):return ('type',self.type_name(self.i.type_at(index)['resolvedType']))
            if kind==3:
                if index not in self.method_ids:raise ValueError('Method reference outside verified modules')
                return ('method',self.method_ids[index])
            if kind==5:return ('literal',hashlib.sha256(self.i.literal(index)).hexdigest())
            if kind==4:
                off,_,count=self.i.section(272,6)
                if not 0<=index<count:raise ValueError('Invalid field reference')
                ti,fi=struct.unpack_from('<Hi',self.i.meta,off+index*6)
                name=self.i.type_at(ti)['resolvedType']
                type_pointer=self.i.type_pointers[ti]
                data,bits=self.pe.unpack('<QI',type_pointer)
                if bits>>16&255==21:
                    base_pointer=self.pe.unpack('<Q',data)[0]
                    data,bits=self.pe.unpack('<QI',base_pointer)
                if bits>>16&255 not in (17,18) or data>=len(self.i.defs):raise ValueError('Unsupported field declaration')
                fields=self.i.fields(data)
                if not 0<=fi<len(fields):raise ValueError('Invalid referenced field')
                f=fields[fi]
                return ('field',self.type_name(name),fi,f['rawRegistrationOffset'],self.type_name(f['resolvedType']))
            if kind==6:
                if index>=self.i.registration[8]:raise ValueError('Invalid generic method reference')
                method,cls,args=self.pe.unpack('<iii',self.i.registration[9]+index*12)
                def inst(n):
                    if n==-1:return ()
                    if not 0<=n<self.i.registration[2]:raise ValueError('Invalid generic instantiation')
                    obj=self.pe.unpack('<Q',self.i.registration[3]+n*8)[0]
                    count,ptr=self.pe.unpack('<QQ',obj)
                    if not 1<=count<=64:raise ValueError('Invalid generic argument count')
                    return tuple(self.type_name(self.i.type_name(p)) for p in self.pe.unpack(f'<{count}Q',ptr))
                if method not in self.method_ids:raise ValueError('External generic method reference needs maintenance')
                return ('generic',self.method_ids[method],inst(cls),inst(args))
        return ('data',self.pe.data[at:at+size].hex())

    def instructions(self,pointer):
        if pointer in self.body_cache:return self.body_cache[pointer]
        at=self.pe.raw(pointer)
        next_index=bisect_right(self.starts,pointer)
        end=self.ends.get(pointer,self.starts[next_index] if next_index<len(self.starts) else pointer+4096)
        if not pointer<end<=pointer+128000:raise ValueError('Unsupported native method extent')
        rows=list(self.dis.disasm(self.pe.data[at:at+end-pointer],pointer))
        by_address={r.address:r for r in rows};pending=[pointer];reachable={}
        while pending:
            pc=pending.pop()
            while pc in by_address and pc not in reachable:
                ins=by_address[pc]
                reachable[pc]=ins
                if ins.mnemonic=='int3':break
                if ins.mnemonic.startswith('ret'):break
                if ins.mnemonic.startswith('j') and ins.op_str.startswith('0x'):
                    target=int(ins.op_str,16)
                    if pointer<=target<end:pending.append(target)
                    if ins.mnemonic=='jmp':break
                pc+=ins.size
        if not reachable:raise ValueError('No native code found')
        self.body_cache[pointer]=list(reachable[k] for k in sorted(reachable))
        return self.body_cache[pointer]

    def body(self,pointer):
        from capstone.x86 import X86_OP_MEM,X86_OP_IMM,X86_REG_RIP
        rows=self.instructions(pointer);indices={ins.address:n for n,ins in enumerate(rows)}
        anonymous={};normalized=[]
        for ins in rows:
            refs=[];operands=ins.op_str
            for op in ins.operands:
                if op.type==X86_OP_MEM and op.mem.base==X86_REG_RIP:
                    addr=ins.address+ins.size+op.mem.disp
                    reference=self.reference(addr,op.size)
                    if reference==('bss',):
                        self.bss.setdefault(addr,len(self.bss));reference=('bss',self.bss[addr])
                    refs.append(reference)
                    operands=re.sub(r'rip [+-] (?:0x[0-9a-f]+|[0-9]+)','rip + <reference>',operands)
                if op.type==X86_OP_IMM and ins.mnemonic in ('call','jmp','je','jne','ja','jae','jb','jbe','jg','jge','jl','jle','js','jns','jo','jno','jp','jnp','jrcxz'):
                    addr=op.imm
                    reference=('branch',indices[addr]) if addr in indices else ('call',tuple(sorted(self.pointer_ids.get(addr,()))))
                    if reference==('call',()):reference=('native',self.shallow_body(addr))
                    refs.append(reference);operands='<target>'
                elif op.type==X86_OP_IMM and self.pe.contains(op.imm) and ins.mnemonic in ('mov','movabs','lea'):
                    refs.append(self.reference(op.imm,8));operands=re.sub(r'0x[0-9a-f]+','<reference>',operands)
            if ins.mnemonic!='nop':normalized.append((ins.mnemonic,operands,refs))
        return digest(normalized)

    def shallow_body(self,pointer):
        """Stable traversal IDs preserve helper call topology without recursion."""
        self.helpers.setdefault(pointer,len(self.helpers))
        if len(self.helpers)>100000:raise ValueError('Native graph exceeds supported size')
        return self.helpers[pointer]

    def singleton_rva(self,name):
        idx=self.defs[name]
        candidates=[m for m in self.i.methods(idx) if m['isStatic'] and m['returnType']==name and not m['parameters']]
        if len(candidates)!=1:raise ValueError('Singleton getter is not unique: '+name)
        m=candidates[0];module=self.modules['Assembly-CSharp.dll']
        ptr=self.pe.unpack('<Q',module['methods']+((int(m['token'],16)&0xffffff)-1)*8)[0]
        from capstone.x86 import X86_OP_MEM,X86_REG_RIP
        locations=set()
        for ins in self.instructions(ptr):
            for op in ins.operands:
                if op.type==X86_OP_MEM and op.mem.base==X86_REG_RIP:
                    addr=ins.address+ins.size+op.mem.disp
                    if self.reference(addr,8)==('type',name):locations.add(addr-self.pe.base)
        if len(locations)!=1:raise ValueError('Singleton type reference is not unique: '+name)
        return locations.pop()

    def type_info_rva(self,name):
        """Find the unique TypeInfo referenced by this static class's methods."""
        from capstone.x86 import X86_OP_MEM,X86_REG_RIP
        locations=set();module=self.modules['Assembly-CSharp.dll']
        for m in self.i.methods(self.defs[name]):
            if not m['isStatic']:continue
            ptr=self.pe.unpack('<Q',module['methods']+((int(m['token'],16)&0xffffff)-1)*8)[0]
            if not ptr:continue
            for ins in self.instructions(ptr):
                for op in ins.operands:
                    if op.type==X86_OP_MEM and op.mem.base==X86_REG_RIP and op.size==8:
                        addr=ins.address+ins.size+op.mem.disp
                        if self.reference(addr,8)==('type',self.type_name(name)):locations.add(addr-self.pe.base)
        if len(locations)!=1:raise ValueError('Static type reference is not unique: '+name)
        return locations.pop()

    def native_layout(self):
        from capstone.x86 import X86_OP_MEM,X86_REG_RIP
        rows=self.instructions(self.exports['il2cpp_gc_wbarrier_set_field'])
        mode=[];bitmap=[]
        for ins in rows:
            for op in ins.operands:
                if op.type!=X86_OP_MEM or op.mem.base!=X86_REG_RIP:continue
                addr=ins.address+ins.size+op.mem.disp-self.pe.base
                if ins.mnemonic=='cmp' and op.size==4:mode.append(addr)
                if ins.mnemonic=='lea':bitmap.append(addr)
        if len(set(mode))!=1 or len(set(bitmap))!=1:raise ValueError('GC barrier layout needs maintenance')
        mode,bitmap=mode[0],bitmap[0]
        if not self.pe.contains(self.pe.base+mode,4) or not self.pe.contains(self.pe.base+bitmap,262144) or bitmap%8:
            raise ValueError('GC barrier data range changed')
        result={'game_manager_rva':self.singleton_rva('GameManager'),'cloud_client_rva':self.singleton_rva('CloudClient'),
                'gc_mode_rva':mode,'gc_bitmap_rva':bitmap}
        for name,key in (('PlayerData','player_data_rva'),('PlayerAbilityBook','ability_book_rva'),
                         ('EquipmentManager','equipment_manager_rva'),('RuneManager','rune_manager_rva'),
                         ('PlayerTalentBook','talent_book_rva'),('GameAreaManager','game_area_manager_rva')):
            result[key]=self.singleton_rva(name)
        difficulty=next((name for name in self.defs if self.aliases.get(name,name)=='ey'),None)
        if difficulty is None:raise ValueError('Difficulty state type is missing')
        result['difficulty_rva']=self.type_info_rva(difficulty)
        return result
