"""Read-only export of exact-build equipment stat effects used by previews."""
from pathlib import Path
import hashlib
import json
import math
import struct
import sys

import UnityPy
from UnityPy.classes import PPtr
from extract_item_ui import Cursor, payload

ROOT = Path(__file__).resolve().parents[1]


def parse_effect(kind, name, data):
    cursor = Cursor(data)
    if kind == 'EquipmentEffect_StatModifier':
        modifiers = []
        for _ in range(cursor.count()):
            stat, modifier = cursor.take('ii')
            value = cursor.obscured()
            if stat not in range(66) or modifier not in (0, 1) or not math.isfinite(value):
                raise ValueError('Unsupported equipment modifier: ' + name)
            modifiers.append({'stat':stat, 'type':modifier, 'value':value})
        required_status = cursor.take('iq')[1] != 0
        condition = cursor.take('i')
        cursor.done()
        if condition not in range(-1, 6):
            raise ValueError('Unsupported equipment condition: ' + name)
        return {'id':name, 'class':kind, 'condition':condition,
                'required_status':required_status, 'modifiers':modifiers}
    if kind == 'EquipmentEffect_StatConversion':
        source, target, fraction = cursor.take('iif')
        cursor.done()
        if source not in range(66) or target not in range(66) or not math.isfinite(fraction):
            raise ValueError('Unsupported equipment conversion: ' + name)
        return {'id':name, 'class':kind, 'source':source, 'target':target, 'fraction':fraction}
    return None


def main():
    game = Path(sys.argv[1])
    data_root = game/'Deskrawl_Data'
    actions = json.loads((ROOT/'data/game-actions-profile.json').read_text(encoding='utf-8'))
    for relative, expected in actions['sha256'].items():
        if hashlib.sha256((game/relative).read_bytes()).hexdigest() != expected:
            raise ValueError('Unverified game build: ' + relative)
    scripts = UnityPy.load(str(data_root/'globalgamemanagers.assets'))
    classes = {o.path_id:o.read().m_ClassName for o in scripts.objects if o.type.name == 'MonoScript'}
    env = UnityPy.load(str(data_root/'resources.assets'))
    assets = next(iter(env.files.values()))
    shared = env.load_file(str(data_root/'sharedassets0.assets'), is_dependency=True)
    exported = {}
    for obj in list(assets.objects.values()) + list(shared.objects.values()):
        if obj.type.name != 'MonoBehaviour': continue
        script_file, script_id = struct.unpack_from('<iq', obj.get_raw_data(), 16)
        if script_file != 1 or classes.get(script_id) != 'ItemData': continue
        name, data = payload(obj)
        if struct.unpack_from('<i', data, 12)[0] != 3: continue
        cursor = Cursor(data, 168)
        effects = []
        for _ in range(cursor.count()):
            file_id, path_id = cursor.take('iq')
            ref = PPtr(m_FileID=file_id, m_PathID=path_id, assetsfile=obj.assets_file).deref()
            script_file, script_id = struct.unpack_from('<iq', ref.get_raw_data(), 16)
            kind = classes.get(script_id) if script_file == 1 else None
            if kind not in ('EquipmentEffect_StatModifier', 'EquipmentEffect_StatConversion'): continue
            effect_name, effect_data = payload(ref)
            effects.append(parse_effect(kind, effect_name, effect_data))
        if effects: exported[name] = effects
    output = ROOT/'data/loot-item-ui.json'
    catalog = json.loads(output.read_text(encoding='utf-8'))
    if not set(exported) <= set(catalog['items']): raise ValueError('Unknown equipment IDs')
    catalog['comparison_effects_schema'] = 1
    catalog['comparison_game_assembly_sha256'] = actions['sha256']['GameAssembly.dll']
    for name, row in catalog['items'].items():
        row.pop('comparison_effects', None)
        if name in exported: row['comparison_effects'] = exported[name]
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'equipment':len(exported), 'effects':sum(map(len, exported.values()))}))


if __name__ == '__main__': main()
