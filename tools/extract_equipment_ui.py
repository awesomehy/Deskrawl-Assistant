"""Export local equipment icons and verified serialized slot/class filters."""
from pathlib import Path
import json
import struct
import sys
import hashlib
import UnityPy

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'tools'))
from inspect_affix_groups import compressed_integer, METADATA_HASH


def enum_values(metadata, wanted):
    data = metadata.read_bytes()
    if hashlib.sha256(data).hexdigest() != METADATA_HASH:
        raise ValueError('Unsupported game metadata')
    so, ss, _ = struct.unpack_from('<III', data, 32)
    fo, _, _ = struct.unpack_from('<III', data, 140)
    to, _, tc = struct.unpack_from('<III', data, 236)
    do, _, dc = struct.unpack_from('<III', data, 92)
    vo, _, _ = struct.unpack_from('<III', data, 104)
    def string(i):
        if not 0 <= i < ss: raise ValueError('Invalid string index')
        return data[so+i:data.index(b'\0', so+i)].decode()
    defaults = {}
    for i in range(dc):
        field, _, value = struct.unpack_from('<iHi', data, do + i*10)
        defaults[field] = value
    result = {}
    for i in range(tc):
        p = to + i*76
        name = string(struct.unpack_from('<i', data, p)[0])
        if name not in wanted or string(struct.unpack_from('<i', data, p+4)[0]): continue
        if not struct.unpack_from('<I', data, p+68)[0] & 2: raise ValueError('Expected enum')
        start = struct.unpack_from('<i', data, p+20)[0]
        count = struct.unpack_from('<H', data, p+56)[0]
        result[name] = {}
        for f in range(start, start+count):
            label = string(struct.unpack_from('<i', data, fo+f*10)[0])
            if label == 'value__': continue
            result[name][label] = compressed_integer(data, vo+defaults[f])[0]
    return result


def main():
    root = Path('G:/SteamLibrary/steamapps/common/Deskrawl/Deskrawl_Data')
    enums = enum_values(root / 'il2cpp_data/Metadata/global-metadata.dat', {'HeroClass','EquipSlotType','ItemRarity'})
    scripts = UnityPy.load(str(root / 'globalgamemanagers.assets'))
    script_ids = {o.path_id for o in scripts.objects if o.type.name == 'MonoScript' and o.read().m_ClassName == 'ItemData'}
    if len(script_ids) != 1: raise ValueError('ItemData script not unique')
    env = UnityPy.load(str(root / 'resources.assets'))
    assets = next(iter(env.files.values()))
    source = json.loads((BASE / 'data/game-catalog.json').read_text(encoding='utf-8'))
    names = set(source['indexes']['equipment_root_key_groups']['equipment_names'])
    rows = {}
    icons = BASE / 'deskrawl_assistant/web/icons'
    icons.mkdir(parents=True, exist_ok=True)
    slots = {v:k for k,v in enums['EquipSlotType'].items()}
    shared = env.load_file(str(root / 'sharedassets0.assets'), is_dependency=True)
    for o in list(assets.objects.values()) + list(shared.objects.values()):
        if o.type.name != 'MonoBehaviour': continue
        data = o.get_raw_data()
        if len(data) < 32: continue
        file_id, script_id = struct.unpack_from('<iq', data, 16)
        if file_id != 1 or script_id not in script_ids: continue
        n = struct.unpack_from('<i', data, 28)[0]
        if not 1 <= n <= 256: continue
        name = data[32:32+n].decode('utf-8')
        if name not in names: continue
        p = (32+n+3)&~3
        if len(data) < p+144: raise ValueError('Truncated equipment')
        icon_file, icon_id = struct.unpack_from('<iq', data, p)
        slot = struct.unpack_from('<i', data, p+132)[0]
        classes = struct.unpack_from('<i', data, p+140)[0]
        rarity = struct.unpack_from('<i', data, p+16)[0]
        if slot not in slots or not 0 <= classes <= 15 or name in rows: raise ValueError('Invalid equipment fields')
        if icon_file == 0:
            icon_assets = o.assets_file
        elif icon_file == 2 and o.assets_file is assets:
            icon_assets = shared
        else: raise ValueError(f'Unexpected icon file: {name}')
        icon_object = icon_assets.objects[icon_id]
        if icon_object.type.name != 'Sprite': raise ValueError(f'Unexpected icon reference: {name}')
        sprite = icon_object.read()
        image = sprite.image
        image.thumbnail((112,112))
        image.save(icons / (name+'.png'))
        rows[name] = {'slot':slots[slot], 'slot_value':slot, 'class_mask':classes, 'rarity_value':rarity,
                      'icon':'/icons/'+name+'.png','source_file':Path(o.assets_file.name).name,
                      'source_path_id':o.path_id,'sprite_path_id':icon_id,'sprite_name':sprite.m_Name}
    if set(rows) != names: raise ValueError('Missing equipment: '+str(names-set(rows)))
    output = {'schema_version':1,'source':'Shipped ItemData assets; class bitmask and slot constants decoded from verified metadata.',
              'metadata_sha256':METADATA_HASH,'resources_sha256':source['source'].get('sha256'),
              'sharedassets_sha256':'e138dbd25c30e7b9e6ada16e8398e6b80e6d8bfade84c84fc49d638ea04cce06',
              'enums':enums,'equipment':rows}
    (BASE / 'data/equipment-ui.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'equipment':len(rows),'legendary':sum(k.startswith('Legendary') for k in rows),'enums':enums},ensure_ascii=False))


if __name__ == '__main__': main()
