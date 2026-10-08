"""Read live container and carriage state; never write to game memory."""
from __future__ import annotations
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from deskrawl_assistant import native_reader
from deskrawl_assistant.native_memory import game_pids, MemoryReadError


class TransferReader(native_reader.NativeReader):
    def _locate_classes(self):
        evidence = json.loads((ROOT/'data/transfer-extra-types.json').read_text(encoding='utf-8'))
        self.types.update({t['name']:t for t in evidence if t['name'] in {'HorseCarriage','LootDrop','GameManager','CarriageAutomation','LootManager'}})
        original = native_reader.REQUIRED_CLASSES
        native_reader.REQUIRED_CLASSES = original+('HorseCarriage','LootDrop','GameManager','CarriageAutomation','LootManager')
        try:
            super()._locate_classes()
        finally:
            native_reader.REQUIRED_CLASSES = original

    def list_objects(self,address,element):
        p=self.memory
        if not address or self.class_name(p.u64(address))!='List`1':
            raise MemoryReadError('列表类型未确认')
        fields={f['name']:f['offset'] for f in self.fields(p.u64(address))}
        if any(fields.get(k)!=v for k,v in {'_items':16,'_size':24,'_version':28}.items()):
            raise MemoryReadError('列表字段布局未确认')
        header=p.read(address+16,16)
        array,count,version=struct.unpack('<Qii',header)
        records,block=self.array(array,element,limit=8192,stride=8)
        if not 0<=count<=len(records): raise MemoryReadError('列表数量未确认')
        objects=[struct.unpack('<Q',b)[0] for b in records[:count]]
        if p.read(address+16,16)!=header or p.read(array+32,len(block))!=block:
            raise MemoryReadError('列表读取时发生变化')
        return objects,{'list':address,'array':array,'count':count,'version':version,'header':header.hex()}

    def transfer_state(self):
        p=self.memory
        snapshot=self.snapshot()
        manager=self.singleton('GameManager')
        carriage=p.u64(manager+88)
        if not carriage or p.u64(carriage)!=self.classes['HorseCarriage']:
            raise MemoryReadError('马车实例未确认')
        registry,_=self.registry()
        drops=[]
        objects,stamp=self.list_objects(p.u64(carriage+152),'LootDrop')
        for address in objects:
            if not address or p.u64(address)!=self.classes['LootDrop']:
                raise MemoryReadError('马车掉落实例类型未确认')
            header=p.read(address+120,96)
            item=struct.unpack_from('<Q',header)[0]
            row={'drop_address':address,'item_address':item,'field_128':struct.unpack_from('<i',header,8)[0],
                'uid_136':self.string(struct.unpack_from('<Q',header,16)[0]),
                'uid_152':self.string(struct.unpack_from('<Q',header,32)[0]),
                'flags':{'132':bool(header[12]),'133':bool(header[13]),'148':bool(header[28]),'149':bool(header[29]),
                         '184':bool(header[64]),'185':bool(header[65]),'208':bool(header[88]),'209':bool(header[89])}}
            if item:
                row.update(self.base_item(item))
                # Match LootDrop.cur: material count uses +164; other items
                # use +168 and an implicit minimum quantity of one.
                row['count']=max(1,struct.unpack_from('<i',header,44 if row['item_type']==1 else 48)[0])
            uid=row['uid_152'] or row['uid_136']
            if uid in registry: row.update(self.generated(registry[uid]))
            if p.read(address+120,96)!=header: raise MemoryReadError('马车物品读取时发生变化')
            drops.append(row)
        containers={}
        for name,key,offset in [('Inventory','inventory',56),('Storage','storage',48)]:
            obj=self.singleton(name)
            array=p.u64(obj+offset)
            records,_=self.array(array,'InventorySlot',limit=2048,stride=8)
            empty=[]
            for index,record in enumerate(records):
                slot=struct.unpack('<Q',record)[0]
                if slot and p.u64(slot)==self.classes['InventorySlot'] and p.u64(slot+16)==0:
                    empty.append(index)
            containers[key]={'root':obj,'array':array,'slot_count':len(records),
                'empty_count':len(empty),'empty_indices':empty,'state_32':p.i32(obj+32)}
        return {'pid':self.pid,'classes':self.classes,'containers':containers,'snapshot':snapshot,
            'game_manager':manager,'active_map':p.u64(manager+112),'game_state':p.i32(manager+168),
            'carriage':carriage,'carriage_capacity':p.i32(carriage+64),'carriage_list':stamp,'drops':drops}


def main():
    reader=TransferReader(game_pids()[0])
    try:
        result=reader.transfer_state()
        target=ROOT/'data/runtime/transfer-state.json'
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k not in {'classes','snapshot'}},ensure_ascii=False,indent=2))
        print('snapshot_complete',result['snapshot']['complete'])
    finally: reader.close()


if __name__=='__main__': main()
