"""Export shipped gem/rune sprites and serialized effects for the supported build."""
from pathlib import Path
import hashlib
import json
import math
import struct
import sys
import UnityPy
from UnityPy.classes import PPtr

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from deskrawl_assistant.native_reader import decode_obscured_float
from deskrawl_assistant.stat_display import is_percent_stat
from extract_equipment_ui import enum_values


class Cursor:
    def __init__(self, data, pos=0): self.data, self.pos = data, pos
    def take(self, fmt):
        value = struct.unpack_from('<'+fmt, self.data, self.pos)
        self.pos += struct.calcsize('<'+fmt)
        return value[0] if len(value)==1 else value
    def count(self):
        n = self.take('i')
        if not 0 <= n <= 128: raise ValueError('Invalid serialized array count')
        return n
    def obscured(self):
        value = decode_obscured_float(self.data[self.pos:self.pos+20])
        if self.data[self.pos+20:self.pos+28] != b'\0'*8: raise ValueError('Unexpected legacy float layout')
        self.pos += 28
        return value
    def done(self):
        if self.pos != len(self.data): raise ValueError(f'Unconsumed asset bytes: {self.pos}/{len(self.data)}')


def payload(obj):
    data = obj.get_raw_data()
    n = struct.unpack_from('<i',data,28)[0]
    if not 1 <= n <= 256: raise ValueError('Invalid asset name')
    return data[32:32+n].decode(), data[(32+n+3)&~3:]


def main():
    root = Path('G:/SteamLibrary/steamapps/common/Deskrawl/Deskrawl_Data')
    catalog = json.loads((BASE/'data/game-catalog.json').read_text(encoding='utf-8'))
    groups = catalog['indexes']['equipment_root_key_groups']
    wanted = set().union(*(groups[k] for k in ('gem_names','generic_rune_names','skill_rune_names','set_rune_piece_names')))
    enums = enum_values(root/'il2cpp_data/Metadata/global-metadata.dat', {'StatType','ModifierType'})
    stats = {v:k for k,v in enums['StatType'].items()}
    entries = {k:v for t in catalog['tables'].values() for k,v in t['entries'].items()}
    def label(key):
        v = entries.get(key,{})
        return v.get('Chinese (Simplified)') or v.get('English') or key
    def text_value(stat, modifier, value):
        percent = modifier != 0 or is_percent_stat(stat)
        value = value*100 if percent else value
        return f'{value:.3f}'.rstrip('0').rstrip('.') + ('%' if percent else '')
    def attribute(c, leveled=False, plain=False):
        stat, modifier = c.take('ii')
        if stat not in stats or modifier not in enums['ModifierType'].values(): raise ValueError('Unknown stat/modifier')
        value = c.take('f') if plain else c.obscured()
        if not math.isfinite(value): raise ValueError('Nonfinite effect')
        result = {'stat':stat,'key':'Stats.'+stats[stat], 'name':label('Stats.'+stats[stat]),
                  'modifier':modifier,'value':value,'display_value':'+'+text_value(stat,modifier,value)}
        if leveled:
            growth = c.obscured()
            result.update(per_level=growth,growth='每级 +'+text_value(stat,modifier,growth))
        return result
    env = UnityPy.load(str(root/'resources.assets'))
    assets = next(iter(env.files.values()))
    shared = env.load_file(str(root/'sharedassets0.assets'),is_dependency=True)
    objects = list(assets.objects.values())+list(shared.objects.values())
    def ref(obj, value):
        file_id,path_id = value
        return PPtr(m_FileID=file_id,m_PathID=path_id,assetsfile=obj.assets_file).deref() if path_id else None
    def effects(c,obj,require_text=True):
        result=[]
        for _ in range(c.count()):
            effect = ref(obj,c.take('iq'))
            if not effect: raise ValueError('Empty effect reference')
            name,_ = payload(effect)
            description = entries.get(name+'.Desc',{}) or entries.get(name.replace('_Effect','Effect')+'.Desc',{})
            text = description.get('Chinese (Simplified)') or description.get('English')
            if not text and require_text: raise ValueError('Missing effect description: '+name)
            result.append({'name':label(name),'description':text or ''})
        return result
    def grants(c,obj):
        result=[]
        for _ in range(c.count()):
            all_abilities = c.take('i')
            ability = ref(obj,c.take('iq'))
            levels,every = c.take('ii')
            if all_abilities not in (0,1) or levels<0 or every<0: raise ValueError('Invalid skill grant')
            name = '全部技能' if all_abilities else label(payload(ability)[0])
            result.append({'name':name,'display_value':f'+{levels} 技能等级',
                           'growth':f'成长间隔：每 {every} 级符文' if every else ''})
        return result
    def rune_set(obj):
        name,data=payload(obj);c=Cursor(data);bonuses=[]
        for index in range(c.count()):
            required=c.take('i')
            attributes=[attribute(c) for _ in range(c.count())]
            descriptions=effects(c,obj,require_text=False)
            skills=grants(c,obj)
            entry=entries.get(name+f'Effect{index+1}.Desc',{})
            text=entry.get('Chinese (Simplified)') or entry.get('English')
            if not text and any(not v['description'] for v in descriptions): raise ValueError('Missing set bonus text: '+name)
            bonuses.append({'title':f'{required} 件套效果','values':[] if text else attributes+skills,
                            'texts':[text] if text else [v['description'] for v in descriptions]})
        c.done()
        return label(name),bonuses
    rows={}
    collectibles={}
    icons=BASE/'deskrawl_assistant/web/icons'
    scripts=UnityPy.load(str(root/'globalgamemanagers.assets'))
    script_ids={o.path_id:o.read().m_ClassName for o in scripts.objects if o.type.name=='MonoScript'}
    for obj in objects:
        if obj.type.name!='MonoBehaviour': continue
        raw=obj.get_raw_data()
        script_file,script_id=struct.unpack_from('<iq',raw,16)
        kind=script_ids.get(script_id) if script_file==1 else None
        if kind not in ('ItemData','GemData','RuneData'): continue
        name,data=payload(obj)
        item_type=struct.unpack_from('<i',data,12)[0]
        if name in entries and '.' not in name and item_type!=1:
            item_kind='gem' if kind=='GemData' else 'rune' if kind=='RuneData' else 'equipment' if name in groups['equipment_names'] else 'chest' if name.startswith('TreasureChest') else 'material'
            collectibles[name]={'key':name,'name':label(name),'kind':item_kind,'item_type':item_type}
        if name not in wanted: continue
        c=Cursor(data)
        sprite=ref(obj,c.take('iq'))
        if not sprite or sprite.type.name!='Sprite': raise ValueError('Missing sprite: '+name)
        c.pos=168
        inherited_effects=effects(c,obj)
        abilities=grants(c,obj)
        fixed=[attribute(c) for _ in range(c.count())]
        c.take('iii')  # Steam definition, retired aligned bool, Black Mist definition
        effect_groups=[]
        if kind=='GemData':
            for title in ('镶嵌于护甲','镶嵌于饰品','镶嵌于武器'):
                values=[attribute(c,plain=True) for _ in range(c.count())]
                effect_groups.append({'title':title,'values':values,'texts':[]})
        else:
            attributes=[attribute(c,leveled=True) for _ in range(c.count())]
            descriptions=effects(c,obj)
            set_obj=ref(obj,c.take('iq'))
            duplicate=c.take('i')
            if duplicate not in (0,1): raise ValueError('Unexpected duplicate flag')
            if attributes: effect_groups.append({'title':'属性加成 · 1级基础值','values':attributes,'texts':[]})
            if abilities: effect_groups.append({'title':'技能加成','values':abilities,'texts':[]})
            if descriptions or inherited_effects: effect_groups.append({'title':'符文效果','values':[],'texts':[v['description'] for v in descriptions+inherited_effects]})
            if fixed: effect_groups.append({'title':'固定属性','values':fixed,'texts':[]})
            if set_obj:
                set_name,bonuses=rune_set(set_obj)
                effect_groups.append({'title':'符文套装 · '+set_name,'values':[],'texts':['集齐对应数量的套装符文后生效。']})
                effect_groups.extend(bonuses)
        c.done()
        image=sprite.read().image
        image.save(icons/(name+'.png'))
        if name in rows or not effect_groups: raise ValueError('Duplicate or empty item: '+name)
        rows[name]={'name':label(name),'kind':'gem' if kind=='GemData' else 'rune',
                    'icon':'/icons/'+name+'.png','effect_groups':effect_groups,
                    'effect_note':'符文属性随等级成长；此处显示1级基础值和每级成长，套装效果需满足件数。' if kind=='RuneData' else '',
                    'source_file':Path(obj.assets_file.name).name,'source_path_id':obj.path_id}
    # These two legacy localization roots have no corresponding shipped asset.
    absent={'UncommonRune1','UncommonRune2'}
    if set(rows)!=wanted-absent: raise ValueError('Missing items: '+str(wanted-set(rows)-absent))
    output={'schema_version':1,'items':rows,'collectibles':collectibles,'unused_localization_roots':sorted(absent),
            'source':'Shipped GemData and RuneData assets; verified stat units and protected float checksum.',
            'resources_sha256':hashlib.sha256((root/'resources.assets').read_bytes()).hexdigest()}
    (BASE/'data/item-ui.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'items':len(rows),'gems':sum(v['kind']=='gem' for v in rows.values()),'runes':sum(v['kind']=='rune' for v in rows.values())}))


if __name__=='__main__': main()
