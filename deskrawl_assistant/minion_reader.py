"""Read 1.1.0 servant progression without invoking upgrades or spending items."""
from functools import lru_cache
import json
import struct

from .native_memory import MemoryReadError
from .paths import RESOURCE_ROOT
from .recommendation_reader import _Copy


@lru_cache(maxsize=1)
def _labels():
    data = json.loads((RESOURCE_ROOT / 'data/game-catalog.json').read_text(encoding='utf-8'))
    return {key: row.get('Chinese (Simplified)') or row.get('English') or key
            for table in data['tables'].values() for key, row in table['entries'].items()}


def read_minions(reader):
    copy = _Copy(reader)
    # This address and the full FieldInfo layouts are part of the connection's
    # reviewed metadata profile; an unknown layout must never become empty data.
    from .game_compatibility import resolved_profile
    profile = resolved_profile(reader, 'recommendation-types.json')
    manager = copy.singleton(profile['minion_manager_rva'], 'SlaveManager')
    game = copy.singleton(0, 'GameManager')
    config = copy.verify(copy.ptr(copy.verify(copy.ptr(game + 32), 'GameConfig') + 88), 'SlaveProgressionConfig')
    maximum = copy.integer(config + 24)
    if not 1 <= maximum <= 1000:
        raise MemoryReadError('仆从等级上限未确认。')
    progress = copy.dictionary(copy.ptr(manager + 80), 'pointer', 'object')
    owned = [struct.unpack('<Q', raw)[0] for raw in copy.list(copy.ptr(manager + 88), element='SlaveData', limit=1024)]
    if len(owned) != len(set(owned)):
        raise MemoryReadError('已解锁仆从列表重复。')
    active = copy.ptr(manager + 96)
    rows = []
    for slave in owned:
        copy.verify(slave, 'SlaveData')
        key = reader.asset_name(slave)
        if not key:
            raise MemoryReadError('仆从资源标识未确认。')
        level, xp, passive_levels, ability_levels = 1, 0, [], []
        obj = progress.get(slave)
        if obj:
            copy.verify(obj, 'iz')
            level = copy.integer(obj + 16)
            xp = struct.unpack('<q', copy.read(obj + 24, 8))[0]
            passive_levels = [struct.unpack('<i', r)[0] for r in copy.array(copy.ptr(obj + 32), 4, 'Int32', limit=128)]
            ability_levels = [struct.unpack('<i', r)[0] for r in copy.array(copy.ptr(obj + 40), 4, 'Int32', limit=128)]
        if not 1 <= level <= maximum or xp < 0 or any(not 0 <= n <= 10000 for n in passive_levels + ability_levels):
            raise MemoryReadError('仆从成长数值未确认。')
        rows.append({'id': key, 'name': _labels().get(key, key), 'active': slave == active,
                     'species': copy.integer(slave + 24), 'rarity': copy.integer(slave + 28),
                     'level': level, 'xp': xp, 'passive_levels': passive_levels,
                     'ability_levels': ability_levels})
    unlocked = [raw[0] for raw in copy.array(copy.ptr(manager + 128), 1, 'Boolean', limit=16)]
    if any(n not in (0, 1) for n in unlocked):
        raise MemoryReadError('仆从继承槽位状态未确认。')
    borrowed = copy.ptr(manager + 136)
    borrowed_key = reader.asset_name(copy.verify(borrowed, 'SlaveData')) if borrowed else None
    result = {'available': True, 'max_level': maximum, 'minions': rows,
              'slot_unlocks': [bool(n) for n in unlocked],
              'skill_unlock_level': copy.integer(config + 100),
              'equipment_unlock_level': copy.integer(config + 96),
              'borrowed_skill': {'minion_id': borrowed_key, 'kind': copy.integer(manager + 144),
                                 'index': copy.integer(manager + 148)}}
    copy.finish()
    return result
