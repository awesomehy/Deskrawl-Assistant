"""Build-gated, external read-only experience snapshots.

Game fields remain untouched. CurrentXP is level-local; total_xp reconstructs
completed levels using the verified native requirement formula. This module
reuses an already connected NativeReader and needs no second memory scan.
"""
from __future__ import annotations

from functools import lru_cache
import json
import hashlib
import math
import struct

from .native_memory import MemoryReadError, SnapshotChangedError
from .paths import RESOURCE_ROOT

GAME_MANAGER_RVA = 0x3A18BF8
PLAYER_DATA_RVA = 0x3A1CBB8


def _hash32(raw):
    value = 0x811C9DC6
    for byte in raw:
        value = ((value ^ byte) * 0x1000192) & 0xFFFFFFFF
    return value


def decode_obscured_int(raw):
    if len(raw) != 16:
        raise MemoryReadError('等级数值包装长度不一致。')
    saved, hidden, key, _ = struct.unpack('<IIII', raw)
    if saved == hidden == key == 0:
        return 0
    bits = (((hidden - key) & 0xFFFFFFFF) ^ key)
    if (_hash32(struct.pack('<I', bits)) | 1) != saved:
        raise MemoryReadError('等级数值完整性不一致。')
    return bits if bits < 0x80000000 else bits - 0x100000000


def decode_obscured_long(raw):
    if len(raw) != 32:
        raise MemoryReadError('经验数值包装长度不一致。')
    saved = struct.unpack_from('<I', raw)[0]
    hidden, key = struct.unpack_from('<QQ', raw, 8)
    if saved == hidden == key == 0:
        return 0
    bits = (((hidden - key) & 0xFFFFFFFFFFFFFFFF) ^ key)
    plain = struct.pack('<Q', bits)
    low = _hash32(plain[:4])
    signed_low = low if low < 0x80000000 else low - 0x100000000
    expected = ((signed_low >> 2) & 0xFFFFFFFF) ^ (_hash32(plain[4:]) | 1)
    if expected != saved:
        raise MemoryReadError('经验数值完整性不一致。')
    return bits if bits < 0x8000000000000000 else bits - 0x10000000000000000


def _float32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def xp_requirement(level, config):
    """Translate Player.fki (RVA 0x517E60), including float32 operations.

    The game uses geometric interpolation between its mob-count anchors and
    nearest-even rounding, first for per-mob XP and then for level XP.
    """
    maximum = config['MaxPlayerLevel']
    if not 1 <= level <= maximum:
        raise ValueError('等级超出已验证范围。')
    if level == maximum:
        return 0
    anchors = [(1, config['MobsToLevelAtLevel1'])]
    for at in (5, 15, 50):
        if maximum > at:
            anchors.append((at, config[f'MobsToLevelAtLevel{at}']))
    anchors.append((maximum, config['MobsToLevelAtMaxLevel']))
    for (left, start), (right, end) in zip(anchors, anchors[1:]):
        if level <= right:
            proportion = min(1.0, max(0.0, (level - left) / (right - left)))
            mobs = start * math.pow(end / start, proportion)
            break
    scale = _float32(_float32(level - 1) * _float32(config['XpPerLevelScale']))
    base = _float32(_float32(config['XpPerBaseHealth']) * _float32(100.0))
    per_mob = max(1, round(base * (1.0 + scale)))
    return max(1, round(mobs * per_mob))


def cumulative_xp(level, current_xp, config):
    if type(current_xp) is not int or current_xp < 0:
        raise ValueError('当前经验无效。')
    maximum = config['MaxPlayerLevel']
    if type(level) is not int or not 1 <= level < maximum:
        raise ValueError('满级后使用巅峰经验，当前路径暂不计量。')
    needed = xp_requirement(level, config)
    if current_xp >= needed:
        raise SnapshotChangedError('角色正在升级，等待下一次稳定读取。')
    return sum(xp_requirement(at, config) for at in range(1, level)) + current_xp


@lru_cache(maxsize=1)
def _profile():
    return json.loads((RESOURCE_ROOT / 'data/experience-types.json').read_text(encoding='utf-8'))


def _snapshot(reader):
    profile = _profile()
    if any(reader.profile.get(key) != profile[key] for key in ('metadataSha256', 'gameAssemblySha256')):
        raise MemoryReadError('游戏版本与已验证经验结构不一致。')
    profiles = {entry['name']: entry for entry in profile['types']}
    memory = reader.memory
    guards = []

    def read(address, size):
        value = memory.read(address, size)
        guards.append((address, value))
        return value

    def pointer(address):
        return struct.unpack('<Q', read(address, 8))[0]

    def verify_class(klass, name):
        expected = [{'name': field['name'], 'offset': field['rawRegistrationOffset'],
                     'token': int(field['token'], 16)} for field in profiles[name]['fields']]
        if not klass or reader.class_name(klass) != name or reader.fields(klass) != expected:
            raise MemoryReadError(f'{name} 的经验读取字段与已验证版本不一致。')
        return klass

    def verify(obj, name):
        if not obj:
            raise MemoryReadError('角色或关卡尚未加载，请进入角色后重试。')
        verify_class(pointer(obj), name)
        return obj

    def singleton(rva, name):
        klass = verify_class(pointer(reader.module['base'] + rva), name)
        static = pointer(klass + 184)
        return verify(pointer(static), name)

    manager = singleton(GAME_MANAGER_RVA, 'GameManager')
    if struct.unpack('<i', read(manager + 168, 4))[0] != 1:
        raise MemoryReadError('游戏尚未进入角色，暂停读取经验。')
    player = singleton(PLAYER_DATA_RVA, 'PlayerData')
    config_obj = verify(pointer(manager + 32), 'GameConfig')
    save = verify(reader.singleton('SaveSystem'), 'SaveSystem')
    # Copy all related numeric fields as a single range; recheck after decoding.
    numeric = read(player + 64, 48)
    level = decode_obscured_int(numeric[:16])
    current_xp = decode_obscured_long(numeric[16:48])
    name = reader.string(pointer(player + 40)) or ''
    mode, slot = struct.unpack('<ii', read(save + 32, 8))
    if mode not in (0, 1) or not 0 <= slot < 100:
        raise MemoryReadError('角色槽位标识无效。')
    config_raw = read(config_obj + 244, 32)
    values = struct.unpack('<iiiiiiff', config_raw)
    names = ('MobsToLevelAtLevel1', 'MobsToLevelAtLevel5', 'MobsToLevelAtLevel15',
             'MobsToLevelAtLevel50', 'MobsToLevelAtMaxLevel', 'MaxPlayerLevel',
             'XpPerBaseHealth', 'XpPerLevelScale')
    config = dict(zip(names, values))
    if (not 2 <= config['MaxPlayerLevel'] <= 200 or
            any(not 1 <= value <= 1000000000 for value in values[:5]) or
            not math.isfinite(values[6]) or not 0 < values[6] <= 1000000 or
            not math.isfinite(values[7]) or not 0 <= values[7] <= 10000):
        raise MemoryReadError('升级经验配置无效。')
    total = cumulative_xp(level, current_xp, config)
    stage, difficulty, run_id = '', '', ''
    active_map = pointer(manager + 112)
    plan = pointer(manager + 120)
    if active_map and plan:
        verify(active_map, 'MapData')
        verify(plan, 'RunPlan')
        plan_header = read(plan + 16, 40)
        run_pointer, stage_pointer, _, _, _, difficulty_pointer = struct.unpack('<QQQiiQ', plan_header)
        stage = reader.string(stage_pointer) or ''
        difficulty = reader.string(difficulty_pointer) or ''
        run_id = reader.string(run_pointer) or ''
        if stage != reader.asset_name(active_map):
            raise SnapshotChangedError('关卡正在切换，等待下一次稳定读取。')
    if any(memory.read(address, len(value)) != value for address, value in guards):
        raise SnapshotChangedError('角色经验或关卡正在变化，等待下一次读取。')
    return {'available': True, 'total_xp': total, 'current_xp': current_xp,
            'level': level, 'next_level_xp': xp_requirement(level, config),
            'character': f'{mode}:{slot}:{name}',
            'session': f'{reader.session_id}:{player:x}', 'stage': stage,
            'difficulty': difficulty, 'run_id': hashlib.sha256(run_id.encode('utf-8')).hexdigest() if run_id else '',
            'source': 'verified_game_memory', 'xp_kind': 'normal'}


def read_experience(reader):
    """Return a public snapshot or an explicit unavailable state.

    Main-menu, mid-transition and unsupported max-level states must not become
    zero-XP observations. The RunPlan identifier is only used to suggest the
    stage for manual timing; this does not detect a completed run.
    """
    try:
        for attempt in range(3):
            try:
                return _snapshot(reader)
            except SnapshotChangedError:
                if attempt == 2:
                    raise
    except (MemoryReadError, ValueError, OSError, UnicodeError, KeyError, struct.error) as exc:
        return {'available': False, 'reason': str(exc), 'source': 'verified_game_memory'}
