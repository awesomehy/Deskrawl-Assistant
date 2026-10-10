"""Extract a build-gated, read-only map/enemy catalog from shipped Unity assets.

Install UnityPy into a D:\\Download cache target, pass --unitypy-path; this tool
never opens a player process, loads GameAssembly, or changes installation files.
"""
from __future__ import annotations
import argparse
import csv
import io
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
METADATA_HASH = '57627070d0fe8a00f82e373100768f529c4829f02d6aeb8ac3057aaab13cacfc'
ASSEMBLY_HASH = 'd46f0a321e8e07646cca37c7460e58fbfecc47ade5027ea2e00fc71f0fec0565'
CONFIG = {'XpPerBaseHealth': 1.0, 'XpPerLevelScale': 0.10000000149011612,
          'XpZeroUnderLevel': 6, 'MaxPlayerLevel': 70,
          'health_growth': [0.11999999731779099, 0.07999999821186066, 0.05999999865889549],
          'damage_growth': [0.10000000149011612, 0.03999999910593033, 0.029999999329447746],
          'EliteHealthMultiplier': 3.0, 'EliteDamageMultiplier': 1.5}
DIFFICULTIES = {'Normal': {'value': 0, 'health_multiplier': 1.0, 'damage_multiplier': 1.0, 'xp_multiplier': 1.0},
                'Nightmare': {'value': 1, 'health_multiplier': 3.5, 'damage_multiplier': 1.350000023841858, 'xp_multiplier': 1.5},
                'Inferno': {'value': 2, 'health_multiplier': 6.0, 'damage_multiplier': 1.7999999523162842, 'xp_multiplier': 2.0}}


def file_hash(path):
    with Path(path).open('rb') as handle:
        digest=hashlib.sha256()
        for chunk in iter(lambda:handle.read(4*1024*1024),b''):digest.update(chunk)
        return digest.hexdigest()


def extract(game, unitypy_path=None):
    if unitypy_path:
        sys.path.insert(0, str(unitypy_path))
    import UnityPy
    sys.path.insert(0, str(ROOT))
    from deskrawl_assistant.experience_reader import decode_obscured_int, decode_obscured_long
    from deskrawl_assistant.native_reader import decode_obscured_float
    assets_root = game / 'Deskrawl_Data'
    if file_hash(assets_root/'il2cpp_data/Metadata/global-metadata.dat') != METADATA_HASH or file_hash(game/'GameAssembly.dll') != ASSEMBLY_HASH:
        raise ValueError('Game build differs; revalidate serialized layouts and native formulas.')
    scripts = UnityPy.load(str(assets_root/'globalgamemanagers.assets'))
    script_names = {o.path_id: o.read().m_ClassName for o in scripts.objects if o.type.name == 'MonoScript'}
    env = UnityPy.load(str(assets_root/'resources.assets'))
    main = next(iter(env.files.values()))
    shared = env.load_file(str(assets_root/'sharedassets0.assets'), is_dependency=True)
    files = {str(main.name): main, str(shared.name): shared}
    objects = list(main.objects.values()) + list(shared.objects.values())
    def header(o):
        raw = o.get_raw_data()
        if len(raw) < 32:
            return None
        fid, sid = struct.unpack_from('<iq', raw, 16)
        if fid != 1:
            return None
        size = struct.unpack_from('<i', raw, 28)[0]
        if not 0 <= size <= 256:
            raise ValueError('Invalid MonoBehaviour name')
        return script_names.get(sid), raw[32:32+size].decode(), (32+size+3)&~3, raw
    localization = {}
    for item in main.objects.values():
        if item.type.name == 'TextAsset':
            text = item.read()
            if text.m_Name in ('World', 'UI'):
                for row in csv.DictReader(io.StringIO(text.m_Script.lstrip('\ufeff'))):
                    localization[row['Key']] = row.get('Chinese (Simplified)') or row.get('English') or row['Key']
    names = {}
    for o in objects:
        if o.type.name == 'MonoBehaviour':
            h = header(o)
            if h:
                names[(str(o.assets_file.name), o.path_id)] = h[1]
    def ref_name(file, fid, pid):
        if not pid:
            return None
        target = file if fid == 0 else main if fid == 2 and file is shared else shared if fid == 2 and file is main else None
        if target is None or pid not in target.objects:
            raise ValueError(f'Unresolved object reference {fid}:{pid}')
        o = target.objects[pid]
        return o.read().m_Name if o.type.name == 'GameObject' else names.get((str(target.name), pid))
    maps, enemy_refs = [], set()
    for o in objects:
        if o.type.name != 'MonoBehaviour':
            continue
        h = header(o)
        if not h or h[0] != 'MapData':
            continue
        _, name, pos, raw = h
        def take(fmt):
            nonlocal pos
            values = struct.unpack_from(fmt, raw, pos)
            pos += struct.calcsize(fmt)
            return values[0] if len(values) == 1 else values
        def ptr():
            fid, pid = take('<iq')
            return ref_name(o.assets_file, fid, pid)
        def encounter():
            size = take('<i')
            if not 0 <= size <= 64:
                raise ValueError('Unexpected encounter array')
            entries = []
            for _ in range(size):
                enemy_id = ptr()
                count, row = take('<ii')
                if not enemy_id or not 0 < count <= 100 or not 0 <= row <= 20:
                    raise ValueError('Invalid enemy entry')
                enemy_refs.add(enemy_id)
                entries.append({'enemy_id': enemy_id, 'count': count, 'row': row})
            weight = take('<f')
            if not 0 <= weight <= 100:
                raise ValueError('Invalid encounter weight')
            return {'weight': weight, 'enemies': entries}
        prerequisite = ptr()
        typ, low, high = take('<iii')
        locked, unavailable = take('<ii')  # each serialized Boolean is aligned to four bytes
        requirements = []
        nreq = take('<i')
        if not 0 <= nreq <= 32:
            raise ValueError('Invalid entry requirement count')
        for _ in range(nreq):
            item = ptr()
            count, mask = take('<ii')
            requirements.append({'item_id': item, 'count': count, 'difficulty_mask': mask})
        waves = take('<i')
        elite = take('<f')
        npool = take('<i')
        if not 0 <= npool <= 64 or not 0 <= elite <= 1 or not 0 <= waves <= 10000:
            raise ValueError('Invalid map array/wave configuration')
        pool = [encounter() for _ in range(npool)]
        mini = take('<i')
        boss = encounter()
        if mini not in (0, 1):
            raise ValueError('Invalid mini-boss flag')
        supported = typ == 0 and waves > 0 and bool(pool)
        family = re.sub(r'\d+$', '', name)
        number = name[len(family):]
        stage = localization.get(family, family) + (' '+number if number else '')
        maps.append({'map_id': name, 'stage': stage, 'family': family,
                     'map_type': typ, 'min_level': low, 'max_level': high, 'waves': waves,
                     'prerequisite': prerequisite, 'requirements': requirements,
                     'locked_in_demo': bool(locked), 'unavailable': bool(unavailable),
                     'elite_chance': elite, 'normal_pool': pool, 'boss_encounter': boss,
                     'boss': bool(boss['enemies']), 'mini_boss': bool(mini),
                     'boss_replaces_last_wave': True, 'supported': supported,
                     'unsupported_reason': '' if supported else '特殊/神话关卡生成路径尚未验证',
                     'source_path_id': o.path_id})
    enemies = {}
    for o in objects:
        if o.type.name != 'GameObject':
            continue
        go = o.read()
        if go.m_Name not in enemy_refs:
            continue
        for component in go.m_Component:
            ref = component.component
            target = o.assets_file if ref.m_FileID == 0 else None
            if target is None:
                raise ValueError('Unexpected enemy component dependency')
            comp = target.objects[ref.m_PathID]
            if comp.type.name != 'MonoBehaviour':
                continue
            h = header(comp)
            if not h or h[0] != 'Enemy':
                continue
            _, _, start, b = h
            def integer(at):
                h, v, k = struct.unpack_from('<III', b, start+at)
                return decode_obscured_int(struct.pack('<IIII', h, v, k, 0))
            def long(at):
                h, v, k = struct.unpack_from('<IQQ', b, start+at)
                return decode_obscured_long(struct.pack('<I4xQQQ', h, v, k, 0))
            def single(at):
                h, v, k = struct.unpack_from('<III', b, start+at)
                return decode_obscured_float(struct.pack('<IIIfI', h, v, k, 0, 0))
            hp, damage = long(12), long(32)
            override_hp, override_damage = struct.unpack_from('<qq', b, start+244)
            if hp <= 0 or damage < 0 or override_hp < 0 or override_damage < 0:
                raise ValueError('Invalid enemy stats')
            enemies[go.m_Name] = {'enemy_id': go.m_Name, 'base_level': integer(0),
                                  'base_health': hp, 'base_damage': damage,
                                  'move_speed': single(52), 'crit_chance': single(80),
                                  'crit_multiplier': single(108), 'attack_speed': single(136),
                                  'high_difficulty_health': override_hp, 'high_difficulty_damage': override_damage,
                                  'source_path_id': comp.path_id}
    required_refs = {e['enemy_id'] for m in maps if m['supported'] for pool in m['normal_pool']+[m['boss_encounter']] for e in pool['enemies']}
    missing = required_refs - enemies.keys()
    if missing:
        raise ValueError('Missing enemy prefabs: '+', '.join(sorted(missing)))
    return {'schema_version': 1, 'metadata_sha256': METADATA_HASH, 'game_assembly_sha256': ASSEMBLY_HASH,
            'source': 'Verified shipped MapData and Enemy assets; integrity-checked Obscured values. No player/save data.',
            'asset_sha256': {p: file_hash(assets_root/p) for p in ['resources.assets', 'sharedassets0.assets', 'globalgamemanagers.assets']},
            'config': CONFIG, 'difficulties': DIFFICULTIES, 'difficulty_options': [{'id': key, 'label': localization.get('UI.Difficulty.'+('Inferno1' if key == 'Inferno' else key), key)} for key in DIFFICULTIES],
            'maps': sorted(maps, key=lambda m: m['map_id']), 'enemies': enemies,
            'evidence': {'xp': 'Enemy.cmg 0x73BC80; GameManager death 0x77DFC0; MapData.cha 0x711870',
                         'health': 'Enemy.bjv 0x744320; GameConfig.ccv 0x707D50',
                         'waves': 'WaveManager.esn 0x7F5420 replaces last wave; eso 0x7F59E0 rolls elite per non-boss wave; esp 0x7F5BF0 weights pools',
                         'levels': 'MapData.cgt/cgu 0x7114F0/0x711590: Normal uses asset range, higher difficulties use level 70'},
            'limitations': ['特殊/神话图未参与理论排序', '首轮时间为理论模型；真实刷图可被动校准', '未验证特殊掉落/召唤怪奖励，不计为固定额外经验']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path, required=True)
    parser.add_argument('--unitypy-path', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'data/recommendation-catalog.json')
    args = parser.parse_args()
    catalog = extract(args.game, args.unitypy_path)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'maps': len(catalog['maps']), 'supported': sum(m['supported'] for m in catalog['maps']), 'enemies': len(catalog['enemies']), 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
