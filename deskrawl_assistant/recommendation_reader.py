"""Version-gated external read-only combat and run snapshots.

Pure Python translations of verified stat getters operate on copied caches.
This module never calls game functions or writes game memory or save files.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import math
import struct

from .experience_reader import decode_obscured_int, decode_obscured_long
from .native_memory import MemoryReadError, SnapshotChangedError
from .native_reader import decode_obscured_float
from .paths import RESOURCE_ROOT
from .game_compatibility import native_rva, resolved_profile

MANAGER_RVA = 0x3A17CD8
DATA_RVA = 0x3A1BA98
BOOK_RVA = 0x3A17D08
DIFFICULTY_RVA = 0x3A1E438


def decode_obscured_double(raw):
    """Decode copied ACTk v39 ObscuredDouble and verify its native checksum."""
    if len(raw) != 40:
        raise MemoryReadError('属性数值包装长度不一致。')
    saved = struct.unpack_from('<I', raw)[0]
    hidden, key = struct.unpack_from('<QQ', raw, 8)
    if saved == hidden == key == 0:
        return 0.0
    encoded = bytearray(struct.pack('<Q', hidden))
    encoded[0], encoded[1] = encoded[1], encoded[0]
    encoded[4], encoded[7], encoded[5] = encoded[7], encoded[5], encoded[4]
    bits = int.from_bytes(encoded, 'little') ^ key
    signed = bits if bits < 2 ** 63 else bits - 2 ** 64
    expected = (((((signed >> 32) ^ signed) & 0xFFFFFFFF) * 31)
                + (signed >> 16) + 0x7E3779B9) & 0xFFFFFFFF
    if expected != saved:
        raise MemoryReadError('属性数值完整性不一致。')
    value = struct.unpack('<d', struct.pack('<Q', bits))[0]
    if not math.isfinite(value):
        raise MemoryReadError('属性数值不是有限数。')
    return value


def compose_stats(base, caches, status=None, conversions=()):
    """Translate Player.bju/StatusEffectController.stat for unconditional data.

    caches use native Player offsets; conversion tuples are (source, target,
    coefficient) already filtered for their active status condition. Recursion
    cycles are rejected instead of guessed. Returned stats retain native units.
    """
    status = status or ({}, {}, {})
    flat, percent, product = status
    result, visiting = {}, set()

    def stat(key):
        if key in result:
            return result[key]
        if key in visiting:
            raise MemoryReadError('属性转换出现循环，暂停当前推荐。')
        visiting.add(key)
        value = base.get(key, 0.0)
        value += sum(caches.get(off, {}).get(key, 0.0) for off in (536, 568, 632, 648))
        if key == 8:
            value += stat(0)
        elif key == 9:
            value += stat(2)
        elif key == 60:
            dexterity = max(0.0, stat(1))
            value += dexterity / (dexterity + 1500.0) if dexterity else 0.0
        value += sum(stat(source) * coefficient for source, target, coefficient in conversions if target == key)
        value *= 1.0 + sum(caches.get(off, {}).get(key, 0.0) for off in (544, 576, 640))
        if key in (7, 8, 9, 10):
            value *= 1.0 + stat({7: 42, 8: 44, 9: 59, 10: 57}[key])
        value = (value + flat.get(key, 0.0)) * (1.0 + percent.get(key, 0.0)) * product.get(key, 1.0)
        if key in (4, 13, 22, 37):
            value = min(value, 0.85)
        elif key == 60:
            value = min(value, 1.0)
        if not math.isfinite(value) or abs(value) > 1e18:
            raise MemoryReadError('最终属性超出有效范围。')
        visiting.remove(key)
        result[key] = value
        return value

    for key in range(66):
        stat(key)
    return result


def leveled_float(raw, level):
    if len(raw) != 40 or type(level) is not int or not 1 <= level <= 10000:
        raise MemoryReadError('技能等级或技能数值无效。')
    return decode_obscured_float(raw[:20]) + decode_obscured_float(raw[20:]) * max(0, level - 1)


def fingerprint(character_id, permanent_stats, skills, extra=None):
    """Exclude current HP, temporary buffs, run ID and character level."""
    payload = {'character': character_id, 'stats': permanent_stats,
               'skills': [{'slot': s['slot'], 'id': s.get('ability_id'), 'level': s.get('level')} for s in skills],
               'extra': extra or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def _hints():
    return json.loads((RESOURCE_ROOT / 'data/recommendation-types.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def _catalog():
    return json.loads((RESOURCE_ROOT / 'data/recommendation-catalog.json').read_text(encoding='utf-8'))


class _Copy:
    def __init__(self, reader):
        self.reader, self.memory, self.guards = reader, reader.memory, []
        hints = resolved_profile(reader, 'recommendation-types.json')
        self.types = {entry.get('logicalName',entry['name']): entry for entry in hints['types']}
        self.verified = set()

    def read(self, address, size, stable=True):
        raw = self.memory.read(address, size)
        if stable:
            self.guards.append((address, raw))
        return raw

    def ptr(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def integer(self, address):
        return struct.unpack('<i', self.read(address, 4))[0]

    def number(self, address):
        value = struct.unpack('<f', self.read(address, 4))[0]
        if not math.isfinite(value):
            raise MemoryReadError('计时或技能参数不是有限数。')
        return value

    def boolean(self, address):
        raw = self.read(address, 1)[0]
        if raw not in (0, 1):
            raise MemoryReadError('关卡状态布尔值无效。')
        return bool(raw)

    def klass(self, klass, name):
        if (klass, name) not in self.verified:
            expected = [{'name': f['name'], 'offset': f['rawRegistrationOffset'], 'token': int(f['token'], 16)}
                        for f in self.types[name]['fields']]
            if not klass or self.reader.class_name(klass) != self.types[name]['name'] or self.reader.fields(klass) != expected:
                raise MemoryReadError(f'{name} 的推荐字段与已验证版本不一致。')
            self.verified.add((klass, name))
        return klass

    def verify(self, obj, name):
        if not obj:
            raise MemoryReadError(f'{name} 尚未加载。')
        self.klass(self.ptr(obj), name)
        return obj

    def singleton(self, rva, name):
        key={'GameManager':'game_manager_rva','PlayerData':'player_data_rva','PlayerAbilityBook':'ability_book_rva',
             'EquipmentManager':'equipment_manager_rva','RuneManager':'rune_manager_rva','PlayerTalentBook':'talent_book_rva'}.get(name)
        if key:rva=native_rva(self.reader,key,rva)
        klass = self.klass(self.ptr(self.reader.module['base'] + rva), name)
        return self.verify(self.ptr(self.ptr(klass + 184)), name)

    def layout(self, obj, name, expected):
        klass = self.ptr(obj)
        if self.reader.class_name(klass) != name:
            raise MemoryReadError('集合类型与推荐布局不一致。')
        fields = {f['name']: f['offset'] for f in self.reader.fields(klass)}
        if any(fields.get(k) != v for k, v in expected.items()):
            raise MemoryReadError('集合字段与推荐布局不一致。')
        return klass

    def array(self, obj, stride, expected_name=None, count=None, limit=4096):
        if not obj:
            if count:
                raise MemoryReadError('推荐集合缺少数组。')
            return []
        klass = self.ptr(obj)
        element = self.ptr(klass + 64)
        if self.integer(klass + 0x104) != stride:
            raise MemoryReadError('推荐数组元素长度不一致。')
        if expected_name and self.reader.class_name(element) != expected_name:
            raise MemoryReadError('推荐数组元素类型不一致。')
        length = struct.unpack('<Q', self.read(obj + 24, 8))[0]
        count = length if count is None else count
        if not 0 <= count <= length <= limit:
            raise MemoryReadError('推荐数组数量异常。')
        raw = self.read(obj + 32, count * stride) if count else b''
        return [raw[i:i + stride] for i in range(0, len(raw), stride)]

    def list(self, obj, stride=8, element=None, limit=4096):
        if not obj:
            return []
        self.layout(obj, 'List`1', {'_items': 16, '_size': 24, '_version': 28})
        header = self.read(obj + 16, 16)
        array, size, _ = struct.unpack('<Qii', header)
        return self.array(array, stride, element, size, limit)

    def dictionary(self, obj, kind='obscured', key_kind='stat', limit=4096):
        if not obj:
            return {}
        self.layout(obj, 'Dictionary`2', {'_entries': 24, '_count': 32, '_freeCount': 40, '_version': 44})
        array, count, _, free, _ = struct.unpack('<Qiiii', self.read(obj + 24, 24))
        if not 0 <= free <= count <= limit:
            raise MemoryReadError('推荐属性字典数量异常。')
        stride = 56 if kind == 'obscured' else 24
        entries = self.array(array, stride, count=count, limit=limit)
        if array:
            element = self.ptr(self.ptr(array) + 64)
            fields = {f['name']: f['offset'] for f in self.reader.fields(element)}
            if fields != {'hashCode': 16, 'next': 20, 'key': 24, 'value': 32}:
                raise MemoryReadError('推荐属性字典元素布局不一致。')
        out = {}
        for raw in entries:
            _, next_ = struct.unpack_from('<Ii', raw)
            if next_ < -1:
                continue
            key = struct.unpack_from('<i' if key_kind == 'stat' else '<Q', raw, 8)[0]
            if key in out:
                raise MemoryReadError('推荐属性字典含重复键。')
            if kind == 'obscured':
                value = decode_obscured_double(raw[16:56])
            elif kind == 'double':
                value = struct.unpack_from('<d', raw, 16)[0]
            elif kind == 'pointer':
                value = struct.unpack_from('<Q', raw, 16)[0]
            elif kind == 'float':
                value = struct.unpack_from('<f', raw, 16)[0]
            else:
                value = struct.unpack_from('<i', raw, 16)[0]
            if not math.isfinite(value):
                raise MemoryReadError('推荐属性字典数值无效。')
            out[key] = value
        if len(out) != count - free:
            raise MemoryReadError('推荐属性字典有效元素数不一致。')
        return out

    def completed(self, save):
        static = self.ptr(self.ptr(save) + 184)
        obj = self.ptr(static + 16)
        self.layout(obj, 'HashSet`1', {'_slots': 24, '_count': 32, '_lastIndex': 36, '_version': 56})
        array = self.ptr(obj + 24)
        count, last = struct.unpack('<ii', self.read(obj + 32, 8))
        self.read(obj + 56, 4)
        if not 0 <= count <= last <= 4096:
            raise MemoryReadError('已完成地图集合数量异常。')
        out = []
        for raw in self.array(array, 16, count=last):
            hash_code, _, value = struct.unpack('<iiQ', raw)
            if hash_code >= 0 and value:
                out.append(self.reader.string(value))
        if len(out) != count or any(not value for value in out):
            raise MemoryReadError('已完成地图集合完整性不一致。')
        return out

    def finish(self):
        if any(self.memory.read(address, len(raw)) != raw for address, raw in self.guards):
            raise SnapshotChangedError('角色属性或关卡正在变化，等待下次稳定读取。')


def _effects(copy, obj, level, stats, depth=0, seen=None):
    if depth > 8:
        return [{'type': 'unknown', 'reason': '技能效果嵌套过深'}]
    seen = set() if seen is None else seen
    out = []
    for raw in copy.list(obj, element='AbilityEffect', limit=256):
        effect = struct.unpack('<Q', raw)[0]
        if not effect or effect in seen:
            continue
        nested_seen = seen | {effect}
        name = copy.reader.class_name(copy.ptr(effect))
        if name not in copy.types:
            out.append({'type': name, 'unsupported': True})
            continue
        copy.verify(effect, name)
        row = {'type': name}
        if name == 'DamageEffect':
            base = struct.unpack('<q', copy.read(effect + 48, 8))[0]
            scale = leveled_float(copy.read(effect + 56, 40), level)
            damage_type = copy.integer(effect + 100)
            type_stat = 23 + damage_type if 0 <= damage_type <= 5 else 23
            attack = max(0, int(stats[20])) + max(0, stats[3])
            pre_type = int((base + scale * attack) * (1 + max(0, stats.get('_primary', 0)) / 100))
            row.update(base_damage=base, attack_scaling=scale, damage_type=damage_type,
                       can_crit=copy.boolean(effect + 96),
                       damage=max(0, int(pre_type * (1 + stats[type_stat] + stats[65]))))
        elif name == 'SpawnProjectileEffect':
            row.update(projectiles=max(1, copy.integer(effect + 92)), pierce=copy.boolean(effect + 69),
                       max_pierce=copy.integer(effect + 72), splash_radius=copy.number(effect + 152),
                       rehit_interval=copy.number(effect + 144))
            row['on_hit'] = _effects(copy, copy.ptr(effect + 160), level, stats, depth + 1, nested_seen)
            row['on_expire'] = _effects(copy, copy.ptr(effect + 168), level, stats, depth + 1, nested_seen)
        elif name == 'EveryNthCastEffect':
            row.update(interval=max(1, copy.integer(effect + 48)), replaces=copy.boolean(effect + 64))
            row['triggered'] = _effects(copy, copy.ptr(effect + 56), level, stats, depth + 1, nested_seen)
        elif name == 'SpawnParticleAoEEffect':
            row.update(lifetime=copy.number(effect + 76), rehit_interval=copy.number(effect + 80))
            row['on_hit'] = _effects(copy, copy.ptr(effect + 88), level, stats, depth + 1, nested_seen)
        else:
            row['unsupported'] = True
        out.append(row)
    return out


def _skills(copy, controller, player, stats, warnings):
    book = copy.singleton(BOOK_RVA, 'PlayerAbilityBook')
    levels = copy.dictionary(copy.ptr(book + 40), 'int', 'object')
    bonuses = copy.dictionary(copy.ptr(player + 1208), 'int', 'object')
    global_levels = copy.integer(player + 1216)
    cooldown_tags = copy.dictionary(copy.ptr(player + 904), 'float')
    cooldown_abilities = copy.dictionary(copy.ptr(player + 1040), 'float', 'object')
    global_cooldown = max(0, copy.number(controller + 64))
    frequency = max(0.001, (stats[21] if stats[21] > 0 else 1.0) * (1 + max(0, stats[6])))
    slots = []
    for slot in range(4):
        ability = copy.ptr(controller + 32 + slot * 8)
        if not ability:
            slots.append({'slot': slot + 1, 'empty': True})
            continue
        copy.verify(ability, 'AbilityData')
        level = max(1, levels.get(ability, 1) + bonuses.get(ability, 0) + global_levels)
        if level > 10000:
            raise MemoryReadError('当前技能等级异常。')
        tags = [struct.unpack('<i', raw)[0] for raw in copy.list(copy.ptr(ability + 40), 4, 'AbilityTag', 128)]
        base_cooldown = leveled_float(copy.read(ability + 164, 40), level)
        reduction = min(0.85, stats[13] + sum(cooldown_tags.get(tag, 0) for tag in tags) + cooldown_abilities.get(ability, 0))
        cooldown = 1 / frequency if 0 in tags else max(0, base_cooldown * (1 - reduction))
        row = {'slot': slot + 1, 'ability_id': copy.reader.asset_name(ability), 'level': level,
               'tags': tags, 'cooldown': cooldown, 'cooldown_base': base_cooldown,
               'global_cooldown': global_cooldown,
               'mana_cost': leveled_float(copy.read(ability + 124, 40), level),
               'mana_gain': copy.number(ability + 120), 'range': leveled_float(copy.read(ability + 64, 40), level),
               'aoe_radius': copy.number(ability + 104), 'target_count': max(1, copy.integer(ability + 116)),
               'standing_time': copy.number(ability + 284), 'channel_duration': copy.number(ability + 208),
               'effects': _effects(copy, copy.ptr(ability + 336), level, stats),
               'source': 'verified_game_memory_and_pure_calculation'}
        slots.append(row)
    warnings.append('技能命中、穿透、控制与目标条件伤害由时间模型估算，并在正常刷图时校准。')
    return slots, frequency


def _run(copy, manager, wave, catalog, difficulty, *, player_level, xp_gain_multiplier):
    from .recommendation import difficulty_id, enemy_health, enemy_xp
    config = catalog['config']
    plan = copy.ptr(manager + 120)
    active_map = copy.ptr(manager + 112)
    if not plan or not active_map:
        return None
    copy.verify(active_map, 'MapData')
    copy.verify(plan, 'RunPlan')
    if copy.ptr(wave + 88) != plan:
        raise SnapshotChangedError('当前关卡计划正在切换。')
    raw_id = copy.reader.string(copy.ptr(plan + 16)) or ''
    map_id = copy.reader.string(copy.ptr(plan + 24)) or ''
    plan_difficulty = difficulty_id(copy.reader.string(copy.ptr(plan + 48)) or '')
    if not raw_id or map_id != copy.reader.asset_name(active_map) or plan_difficulty != difficulty:
        raise SnapshotChangedError('关卡身份尚未稳定。')
    wave_index = copy.integer(wave + 48)
    wave_active = copy.boolean(wave + 64)
    finished, final_kill = copy.boolean(wave + 248), copy.boolean(wave + 249)
    health_multiplier = struct.unpack('<d', copy.read(plan + 72, 8))[0]
    if not math.isfinite(health_multiplier) or not 0 <= health_multiplier <= 1e12:
        raise MemoryReadError('关卡生命倍率无效。')
    difficulty_config = dict(catalog['difficulties'][difficulty])
    if health_multiplier > 0:
        difficulty_config['health_multiplier'] = health_multiplier
    normal_health = boss_health = total_xp = 0
    xp_fallback = False
    normal_waves = boss_waves = 0
    phase = 'preparing' if wave_index == 0 else 'normal'
    waves = copy.array(copy.ptr(plan + 88), 8, 'RunWave', limit=512)
    for index, raw in enumerate(waves, 1):
        current = copy.verify(struct.unpack('<Q', raw)[0], 'RunWave')
        wave_type = copy.reader.string(copy.ptr(current + 24)) or ''
        is_boss_wave = wave_type.casefold() == 'boss'
        if is_boss_wave:
            boss_waves += 1
        else:
            normal_waves += 1
        if index == wave_index:
            phase = 'boss' if is_boss_wave else 'normal'
        level = copy.integer(current + 32)
        if not 1 <= level <= 200:
            raise MemoryReadError('规划敌人等级无效。')
        subtotal = 0
        for enemy_raw in copy.array(copy.ptr(current + 40), 8, 'PlanEnemy', limit=1024):
            enemy = copy.verify(struct.unpack('<Q', enemy_raw)[0], 'PlanEnemy')
            count = copy.integer(enemy + 24)
            xp = struct.unpack('<q', copy.read(enemy + 32, 8))[0]
            base_health = struct.unpack('<q', copy.read(enemy + 48, 8))[0]
            enemy_id = copy.reader.string(copy.ptr(enemy + 16)) or ''
            resource = catalog['enemies'].get(enemy_id)
            if base_health <= 0 and resource:
                base_health = resource['base_health']
                if difficulty != 'Normal' and level == 70 and resource.get('high_difficulty_health', 0) > 0:
                    base_health = resource['high_difficulty_health']
            if not 0 <= count <= 10000 or not -1 <= xp <= 1e15 or not 0 < base_health <= 1e15:
                raise MemoryReadError('规划敌人收益无效。')
            subtotal += count * enemy_health(base_health, level, difficulty_config, config, elite=wave_type.casefold() == 'elite')
            if xp <= 0:
                xp = enemy_xp(base_health, level, player_level, xp_gain_multiplier, difficulty_config['xp_multiplier'], config)
                xp_fallback = True
            total_xp += count * xp
        if is_boss_wave:
            boss_health += subtotal
        else:
            normal_health += subtotal
    enemies = copy.list(copy.ptr(manager + 192), element='Enemy', limit=4096)
    alive = 0
    for raw in enemies:
        enemy = struct.unpack('<Q', raw)[0]
        if enemy:
            copy.verify(enemy, 'Enemy')
            if not copy.boolean(enemy + 296):
                alive += 1
    if finished:
        phase = 'finished'
    return {'id': hashlib.sha256(raw_id.encode()).hexdigest(), 'map_id': map_id, 'difficulty': difficulty,
            'phase': phase, 'wave_index': wave_index, 'wave_active': wave_active, 'enemy_count': alive,
            'planned_normal_health': normal_health, 'planned_boss_health': boss_health, 'planned_xp': total_xp,
            'normal_waves': normal_waves, 'boss_waves': boss_waves, 'wave_count': len(waves),
            'started': True, 'finished': finished, 'final_kill_settled': final_kill,
            'at_start': wave_index == 0 and not wave_active and not finished and not final_kill,
            'start_observable': True, 'xp_source': 'static_formula_fallback' if xp_fallback else 'RunPlan_enemy_xp',
            'status_source': 'verified_WaveManager_state'}



def _loadout_identity(copy):
    """Permanent asset/UID identity; no snapshot scan or native calls."""
    equipment = copy.singleton(0x3A19650, 'EquipmentManager')
    items = copy.dictionary(copy.ptr(equipment + 40), 'pointer')
    item_uids = copy.dictionary(copy.ptr(equipment + 48), 'pointer')
    equipped = [{'slot': slot, 'asset_id': copy.reader.asset_name(item),
                 'uid': copy.reader.string(item_uids.get(slot, 0)) or ''}
                for slot, item in sorted(items.items()) if item]
    rune_manager = copy.singleton(0x3A1BA88, 'RuneManager')
    rune_objects = copy.array(copy.ptr(rune_manager + 48), 8, 'RuneData', limit=64)
    rune_uids = copy.array(copy.ptr(rune_manager + 56), 8, 'String', limit=64)
    if len(rune_objects) != len(rune_uids):
        raise MemoryReadError('符文槽位标识数量不一致。')
    runes = []
    for slot, (obj_raw, uid_raw) in enumerate(zip(rune_objects, rune_uids)):
        rune, uid = struct.unpack('<Q', obj_raw)[0], struct.unpack('<Q', uid_raw)[0]
        if rune:
            copy.verify(rune, 'RuneData')
            runes.append({'slot': slot, 'asset_id': copy.reader.asset_name(rune),
                          'uid': copy.reader.string(uid) or ''})
    talent_book = copy.singleton(0x3A1BA80, 'PlayerTalentBook')
    talents = []
    for talent, points in copy.dictionary(copy.ptr(talent_book + 40), 'int', 'object').items():
        if points:
            copy.verify(talent, 'TalentData')
            talents.append({'asset_id': copy.reader.asset_name(talent), 'points': points})
    return {'equipment': equipped, 'runes': runes,
            'talents': sorted(talents, key=lambda value: value['asset_id'])}

def _snapshot(reader):
    copy = _Copy(reader)
    manager = copy.singleton(MANAGER_RVA, 'GameManager')
    if copy.integer(manager + 168) != 1:
        raise MemoryReadError('请先进入角色，再读取当前角色推荐。')
    data = copy.singleton(DATA_RVA, 'PlayerData')
    combat = copy.verify(copy.ptr(manager + 64), 'PlayerCombatController')
    player = copy.verify(copy.ptr(combat + 32), 'Player')
    copy.klass(copy.ptr(copy.ptr(player) + 88), 'Character')
    controller = copy.verify(copy.ptr(combat + 112), 'PlayerAbilityController')
    wave = copy.verify(copy.ptr(manager + 104), 'WaveManager')
    config_obj = copy.verify(copy.ptr(manager + 32), 'GameConfig')
    hero = copy.verify(copy.ptr(manager + 56), 'HeroData')
    save = copy.verify(reader.singleton('SaveSystem'), 'SaveSystem')
    mode, slot = struct.unpack('<ii', copy.read(save + 32, 8))
    level = decode_obscured_int(copy.read(data + 64, 16))
    if mode not in (0, 1) or not 0 <= slot < 100 or not 1 <= level <= 200:
        raise MemoryReadError('当前角色标识或等级无效。')
    label = reader.string(copy.ptr(data + 40)) or ''
    character = f'{mode}:{slot}:{label}'
    hero_id = reader.asset_name(hero)
    character_id = character
    primary = copy.integer(hero + 44)
    if primary not in (0, 1, 2):
        raise MemoryReadError('角色主属性类型无效。')
    base = {7: decode_obscured_long(copy.read(player + 48, 32)),
            3: decode_obscured_long(copy.read(player + 80, 32)),
            primary: decode_obscured_float(copy.read(player + 416, 20))}
    for stat, offset in ((10, 112), (4, 132), (5, 152), (6, 172), (22, 192), (11, 436), (12, 456), (13, 476)):
        base[stat] = decode_obscured_float(copy.read(player + offset, 20))
    caches = {off: copy.dictionary(copy.ptr(player + off)) for off in (536, 544, 568, 576, 632, 640, 648)}
    status_obj = copy.verify(copy.ptr(player + 304), 'StatusEffectController')
    status = tuple(copy.dictionary(copy.ptr(status_obj + off), 'double') for off in (56, 64, 72))
    warnings, conversions = [], []
    for off in (792, 808):
        for raw in copy.list(copy.ptr(player + off), 24, 'ValueTuple`4'):
            source, target, coefficient, condition = struct.unpack('<iidQ', raw)
            if source not in range(66) or target not in range(66) or not math.isfinite(coefficient):
                raise MemoryReadError('属性转换参数无效。')
            if not condition:
                conversions.append((source, target, coefficient))
            else:
                warnings.append('带状态条件的属性转换未计入参考属性。')
    conditional = any(copy.integer(copy.ptr(player + off) + 24) > 0 for off in (816, 856, 880))
    if conditional:
        warnings.append('当前配装含额外条件属性，参考属性尚未计入该条件部分。')
    conditional = conditional or any('属性转换' in message for message in warnings)
    stats = compose_stats(base, caches, status, conversions)
    stats['_primary'] = stats[primary]
    skills, attack_speed = _skills(copy, controller, player, stats, warnings)
    hp_max, hp_current = struct.unpack('<qq', copy.read(data + 112, 16, stable=False))
    if not 0 <= hp_current <= hp_max <= 1e15:
        raise MemoryReadError('当前生命数值无效。')
    dead = copy.boolean(player + 296)
    diff_klass = copy.klass(copy.ptr(reader.module['base'] + native_rva(reader,'difficulty_rva',DIFFICULTY_RVA)), 'ey')
    diff_enum = copy.integer(copy.ptr(diff_klass + 184))
    difficulty = {0: 'Normal', 1: 'Nightmare', 2: 'Inferno'}.get(diff_enum)
    if not difficulty:
        raise MemoryReadError('当前难度不在已验证范围。')
    catalog = _catalog()
    completed = copy.completed(save)
    is_demo = copy.boolean(config_obj + 48)
    entry_items = {}
    inventory = copy.verify(reader.singleton('Inventory'), 'Inventory')
    for item, amount in copy.dictionary(copy.ptr(inventory + 64), 'int', 'object').items():
        if amount > 0:
            entry_items[reader.asset_name(item)] = amount
    unlocked = [m['map_id'] for m in catalog['maps']
                if not m.get('unavailable') and not (is_demo and m.get('locked_in_demo'))
                and (not m.get('prerequisite') or m['prerequisite'] in completed)]
    permanent = {off: caches[off] for off in (536, 544, 568, 576, 648)}
    loadout_identity = _loadout_identity(copy)
    loadout = fingerprint(character_id, permanent, skills, {'conversions': conversions, 'hero_id': hero_id, 'life_skill_stats': caches[632], 'identity': loadout_identity})
    damage = max(0, (int(stats[20]) + max(0, stats[3])) * (1 + max(0, stats[primary]) / 100) * (1 + stats[65]))
    combat_stats = {'damage': damage, 'weapon_damage': max(0, int(stats[20])), 'attack_speed': attack_speed,
                    'attack_speed_bonus': stats[6], 'weapon_speed': stats[21],
                    'crit_chance': max(0, stats[4]), 'crit_multiplier': max(1, stats[5]),
                    'normal_dps': None, 'boss_dps': None, 'health_max': hp_max, 'health_current': hp_current,
                    'armor': max(0, stats[8]), 'magic_resist': max(0, stats[9]), 'move_speed': max(0, stats[10]),
                    'cooldown_reduction': stats[13], 'primary_stat': stats[primary],
                    'damage_percent': stats[65], 'damage_type_bonuses': {str(n - 23): stats[n] for n in range(23, 29)}}
    units = {'damage': 'reference_damage_per_attack', 'weapon_damage': 'damage', 'attack_speed': 'attacks_per_second',
             'attack_speed_bonus': 'bonus_fraction', 'weapon_speed': 'attacks_per_second', 'crit_chance': 'chance_fraction',
             'crit_multiplier': 'multiplier', 'armor': 'rating', 'magic_resist': 'rating', 'move_speed': 'world_units_per_second',
             'cooldown_reduction': 'reduction_fraction', 'primary_stat': 'attribute_points', 'damage_percent': 'bonus_fraction',
             'damage_type_bonuses': 'bonus_fraction'}
    provenance = {k: {'unit': units.get(k, 'native_value'),
                     'source': 'verified_cache_pure_calculation', 'quality': 'partial' if conditional else 'verified'}
                  for k, v in combat_stats.items() if v is not None}
    for key in ('health_max', 'health_current'):
        provenance[key] = {'unit': 'HP', 'source': 'PlayerData_live_final_field', 'quality': 'verified'}
    for key in ('normal_dps', 'boss_dps'):
        provenance[key] = {'unit': 'damage_per_second', 'source': 'model_required', 'quality': 'theoretical'}
    run = _run(copy, manager, wave, catalog, difficulty, player_level=level, xp_gain_multiplier=max(0,1+stats[41]))
    if run:
        run['dead'] = dead
        if dead:
            run['phase'] = 'dead'
    timing = {'time_between_waves': max(0, copy.number(wave + 32)), 'initial_spawn_delay': max(0, copy.number(wave + 36))}
    copy.finish()
    return {'available': True, 'session': f'{reader.session_id}:{data:x}', 'profile': {
        'character_id': character_id, 'character': character, 'label': label, 'hero_id': hero_id, 'level': level,
        'fingerprint': loadout, 'loadout_fingerprint': loadout, 'loadout_identity': loadout_identity, 'difficulty': difficulty,
        'xp_gain_multiplier': max(0, 1 + stats[41]), 'xp_gain_provenance': 'Player.final_XPGain_bonus_plus_one',
        'combat': combat_stats, 'combat_provenance': provenance, 'skills': skills,
        'timing': timing,
        'unlocked_maps': {difficulty: unlocked}, 'completed_maps': {difficulty: completed},
        'entry_items': entry_items, 'is_demo': is_demo}, 'run': run, 'warnings': list(dict.fromkeys(warnings)),
        'reason': '', 'source': 'verified_game_memory'}


def read_recommendation(reader):
    try:
        for attempt in range(3):
            try:
                return _snapshot(reader)
            except SnapshotChangedError:
                if attempt == 2:
                    raise
    except (MemoryReadError, OSError, ValueError, UnicodeError, KeyError, struct.error) as exc:
        return {'available': False, 'profile': None, 'run': None, 'warnings': [], 'reason': str(exc),
                'source': 'verified_game_memory'}
