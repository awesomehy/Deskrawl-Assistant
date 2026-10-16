"""Export every shipped loot ItemData, with exact IDs, sprites and descriptions.

This reads installation files only. Existing equipment and gem/rune exports
remain separate so this catalogue cannot change inventory automation policy.
"""
from pathlib import Path
import hashlib
import json
import struct
import sys

import UnityPy
from UnityPy.classes import PPtr
from extract_item_ui import Cursor, payload
from equipment_effect_text import render_equipment_effect

BASE = Path(__file__).resolve().parents[1]


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('G:/SteamLibrary/steamapps/common/Deskrawl/Deskrawl_Data')
    profile = json.loads((BASE / 'data/loot-plan-types.json').read_text(encoding='utf-8'))
    for path, expected in ((root/'il2cpp_data/Metadata/global-metadata.dat', profile['metadataSha256']),
                           (root.parent/'GameAssembly.dll', profile['gameAssemblySha256'])):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Unverified game build: ' + str(path))
    catalog = json.loads((BASE/'data/game-catalog.json').read_text(encoding='utf-8'))
    labels = {k:v for t in catalog['tables'].values() for k,v in t['entries'].items()}
    equipment = json.loads((BASE/'data/equipment-ui.json').read_text(encoding='utf-8'))['equipment']
    other = json.loads((BASE/'data/item-ui.json').read_text(encoding='utf-8'))['items']
    def text(key):
        value = labels.get(key, {})
        return value.get('Chinese (Simplified)') or value.get('English') or ''
    scripts = UnityPy.load(str(root/'globalgamemanagers.assets'))
    classes = {o.path_id:o.read().m_ClassName for o in scripts.objects if o.type.name == 'MonoScript'}
    env = UnityPy.load(str(root/'resources.assets'))
    assets = next(iter(env.files.values()))
    shared = env.load_file(str(root/'sharedassets0.assets'), is_dependency=True)
    icons = BASE/'deskrawl_assistant/web/icons'
    rows = {}
    colors = {}
    for obj in list(assets.objects.values()) + list(shared.objects.values()):
        if obj.type.name != 'MonoBehaviour': continue
        script_file, script_id = struct.unpack_from('<iq', obj.get_raw_data(), 16)
        cls = classes.get(script_id) if script_file == 1 else None
        if cls == 'GraphicsConfig':
            _, data = payload(obj)
            c = Cursor(data, 9*12+16+3*12)
            for _ in range(c.count()): c.take('iq')
            for _ in range(8): c.take('iq')
            for label in ('common','uncommon','rare','legendary','divine','rune_set','black_mist'):
                rgba = c.take('ffff')
                if any(not 0<=v<=1 for v in rgba): raise ValueError('Invalid rarity colour')
                colors[label] = '#'+''.join(f'{round(v*255):02x}' for v in rgba[:3])
            continue
        if cls not in ('ItemData', 'GemData', 'RuneData'): continue
        key, data = payload(obj)
        if key in rows: raise ValueError('Duplicate item ID: ' + key)
        sprite_file, sprite_id = struct.unpack_from('<iq', data)
        sprite = PPtr(m_FileID=sprite_file, m_PathID=sprite_id, assetsfile=obj.assets_file).deref() if sprite_id else None
        if not sprite or sprite.type.name != 'Sprite': raise ValueError('Missing item sprite: '+key)
        item_type, rarity = struct.unpack_from('<ii', data, 12)
        if item_type not in (1,2,3,5,6,7) or rarity not in range(6): raise ValueError('Unverified item enum: '+key)
        label_key = key
        # Resolve the serialized SlaveToUnlock reference rather than guessing
        # by prefix: legacy asset names can differ from the referenced servant.
        if not text(key) and item_type == 2:
            file_id, path_id = struct.unpack_from('<iq', data, 36)
            slave = PPtr(m_FileID=file_id, m_PathID=path_id, assetsfile=obj.assets_file).deref() if path_id else None
            if slave: label_key = payload(slave)[0]
        name = text(label_key)
        if not name: raise ValueError('Missing localisation: '+key)
        kind = ('equipment' if item_type == 3 else 'gem' if cls == 'GemData' else
                'rune' if cls == 'RuneData' else 'chest' if key.startswith('TreasureChest') else 'material')
        groups = list(other.get(key, {}).get('effect_groups', []))
        description = text(label_key+'.Desc')
        effect_ids = []
        if cls == 'ItemData':
            cursor = Cursor(data, 168)
            for _ in range(cursor.count()):
                file_id, path_id = cursor.take('iq')
                effect = PPtr(m_FileID=file_id, m_PathID=path_id, assetsfile=obj.assets_file).deref() if path_id else None
                if not effect: raise ValueError('Missing inherited effect: '+key)
                effect_key, effect_data = payload(effect)
                effect_ids.append(effect_key)
                effect_text = text(effect_key+'.Desc') or text(effect_key.replace('_Effect','Effect')+'.Desc')
                if not effect_text:
                    script_file, script_id = struct.unpack_from('<iq', effect.get_raw_data(), 16)
                    effect_class = classes.get(script_id) if script_file == 1 else None
                    effect_text = render_equipment_effect(effect_class, effect_data, text)
                # Some passive implementation assets have no individual text;
                # their equipment description is the game's user-facing text.
                if effect_text:
                    groups.append({'title':'物品效果', 'values':[], 'texts':[effect_text]})
        if description and not any(description in g.get('texts', []) for g in groups):
            groups.insert(0, {'title':'装备效果' if kind=='equipment' else '物品说明', 'values':[], 'texts':[description]})
        if kind == 'equipment' and not description:
            description = '\n'.join(t for g in groups for t in g.get('texts', []))
        note = other.get(key, {}).get('effect_note','')
        if not groups:
            if label_key != key and item_type == 2:
                if struct.unpack_from('<i', data, 32)[0] != 1: raise ValueError('Unverified servant effect')
                groups = [{'title':'物品效果', 'values':[], 'texts':['使用后解锁仆人：'+name+'。']}]
            elif kind == 'equipment' and not effect_ids:
                note = '此装备没有固定特殊效果，基础属性与随机词条见下方。'
            else:
                note = '游戏资源未提供独立效果说明。'
        file = icons/(key+'.png')
        # Equipment/gem/rune exports already refresh their sprites. Refresh
        # remaining items too, since a new build can change an existing icon.
        if not file.exists() or kind not in ('equipment', 'gem', 'rune'):
            image = sprite.read().image
            image.thumbnail((112,112))
            image.save(file)
        rows[key] = {'name':name, 'label_key':label_key, 'kind':kind, 'item_type':item_type,
            'rarity':rarity, 'icon':'/icons/'+key+'.png', 'description':description,
            'effect_groups':groups, 'effect_note':note,
            'source_effect_ids':effect_ids,
            'source_class':cls, 'source_file':Path(obj.assets_file.name).name, 'source_path_id':obj.path_id}
    if not set(equipment).union(other) <= set(rows): raise ValueError('Incomplete loot catalogue')
    if len(colors) != 7: raise ValueError('Missing shipped rarity palette')
    output = {'schema_version':1, 'metadata_sha256':profile['metadataSha256'],
        'resources_sha256':hashlib.sha256((root/'resources.assets').read_bytes()).hexdigest(),
        'source':'All shipped ItemData/GemData/RuneData assets; IDs are asset names, not inferred drop probabilities.',
        'rarity_colors':colors, 'items':dict(sorted(rows.items()))}
    (BASE/'data/loot-item-ui.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'items':len(rows),'runes':sum(r['kind']=='rune' for r in rows.values()),
        'icons':len(list(icons.glob('*.png'))),'descriptions':sum(bool(r['effect_groups']) for r in rows.values())}))


if __name__ == '__main__': main()
