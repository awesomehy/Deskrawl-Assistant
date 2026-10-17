"""Extract public market candidates from the shipped ItemData definitions.

Reads installation assets only; never reads a Steam account or inventory.
The generated names are candidates, not a claim that an individual item can
be sold. Runtime prices require an exact, marketable Steam description.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

import UnityPy

from extract_item_ui import Cursor, payload

BASE = Path(__file__).resolve().parents[1]


def extract(root: Path) -> dict:
    loot = json.loads((BASE / 'data/loot-item-ui.json').read_text(encoding='utf-8'))
    resources_hash = hashlib.sha256((root / 'resources.assets').read_bytes()).hexdigest()
    if resources_hash != loot['resources_sha256']:
        raise ValueError('Run the verified loot catalogue extraction for this resources.assets first')
    localization = json.loads((BASE / 'data/game-catalog.json').read_text(encoding='utf-8'))
    labels = {key: row for table in localization['tables'].values()
              for key, row in table['entries'].items()}
    scripts = UnityPy.load(str(root / 'globalgamemanagers.assets'))
    classes = {obj.path_id: obj.read().m_ClassName for obj in scripts.objects
               if obj.type.name == 'MonoScript'}
    env = UnityPy.load(str(root / 'resources.assets'))
    assets = next(iter(env.files.values()))
    shared = env.load_file(str(root / 'sharedassets0.assets'), is_dependency=True)
    result = {}
    for obj in list(assets.objects.values()) + list(shared.objects.values()):
        if obj.type.name != 'MonoBehaviour':
            continue
        file_id, script_id = struct.unpack_from('<iq', obj.get_raw_data(), 16)
        cls = classes.get(script_id) if file_id == 1 else None
        if cls not in ('ItemData', 'RuneData', 'GemData'):
            continue
        key, data = payload(obj)
        c = Cursor(data, 168)
        # EquipmentEffects, AbilityLevels and FixedStats precede the Steam IDs.
        for stride in (12, 24, 36):
            count = c.count()
            c.pos += count * stride
        definition_id, retired, mist_definition_id = c.take('iii')
        if retired not in (0, 1) or min(definition_id, mist_definition_id) < 0:
            raise ValueError('Invalid serialized Steam definition: ' + key)
        if not definition_id and not mist_definition_id:
            continue
        meta = loot['items'][key]
        english = labels.get(meta.get('label_key', key), {}).get('English')
        if not english:
            raise ValueError('Missing shipped English name: ' + key)
        if key.startswith('Rein_'):
            template = labels['Rein']['English']
            if template.count('{slave}') != 1:
                raise ValueError('Unverified rein localization template')
            english = template.replace('{slave}', english)
        entry = {'kind': meta['kind'], 'rarity': meta['rarity'],
                 'source_class': cls, 'source_path_id': obj.path_id,
                 'source_file': Path(obj.assets_file.name).name,
                 'retired': bool(retired)}
        if definition_id:
            entry['normal'] = {'definition_id': definition_id, 'market_hash_name': english}
        if mist_definition_id:
            entry['black_mist'] = {'definition_id': mist_definition_id,
                                  'market_hash_name': english + ' (Black Mist)'}
        if key in result:
            raise ValueError('Duplicate ItemData key: ' + key)
        result[key] = entry
    if result.get('GoldBar', {}).get('normal', {}).get('definition_id') != 5000:
        raise ValueError('Gold Bar Steam definition does not match the verified build')
    if result.get('UpgradeCommodity1', {}).get('normal', {}).get('definition_id') != 5001:
        raise ValueError('Consecration Stone Steam definition does not match the verified build')
    return {'schema_version': 1, 'appid': 4623570, 'resources_sha256': resources_hash,
            'source': 'Shipped ItemData SteamItemDefId/BlackMistSteamItemDefId and English localization; '
                      'names remain candidates until exact Steam market description validation.',
            'items': dict(sorted(result.items()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game_data', type=Path, nargs='?',
                        default=Path('G:/SteamLibrary/steamapps/common/Deskrawl/Deskrawl_Data'))
    parser.add_argument('--output', type=Path, default=BASE / 'data/steam-market-catalog.json')
    args = parser.parse_args()
    value = extract(args.game_data)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'items': len(value['items']), 'appid': value['appid']}))


if __name__ == '__main__':
    main()
