"""Guarded, external reads of equipped gear and current character stat layers."""
from datetime import datetime, timezone
import math
import struct
from .character import final_stats, panel_attack, boss_distance, PREVIEW_NOTE
from .character_positions import Positions
from .native_reader import decode_obscured_float
from .native_memory import MemoryReadError, SnapshotChangedError
from .recommendation_reader import _Copy, decode_obscured_int, decode_obscured_long


def _conditional(copy, player, manager, status_obj, hp, maximum, warnings, fixed_distance=None):
    stacks = {}
    for raw in ([] if fixed_distance is not None else copy.list(copy.ptr(status_obj + 32), element=copy.types['bk']['name'], limit=512)):
        obj = copy.verify(struct.unpack('<Q', raw)[0], 'bk')
        key, count = copy.ptr(obj + 16), copy.integer(obj + 40)
        if count < 0 or count > 100000:
            raise MemoryReadError('状态层数无效。')
        # Native big uses List.Find: keep the first matching status instance.
        stacks.setdefault(key, count)
    active = set(stacks)
    battle = True if fixed_distance is not None else copy.boolean(copy.verify(copy.ptr(manager + 64), 'PlayerCombatController') + 80)
    area = None
    layers = ({}, {})
    conversions = []
    alive_enemies = None
    positions = None

    def enemies():
        nonlocal alive_enemies
        if alive_enemies is None:
            alive_enemies = []
            for raw in copy.list(copy.ptr(manager + 192), element='Enemy', limit=2048):
                obj = struct.unpack('<Q', raw)[0]
                if obj:
                    copy.verify(obj, 'Enemy')
                    if not copy.boolean(obj + 296):
                        alive_enemies.append(obj)
        return alive_enemies

    def condition(kind, radius):
        if fixed_distance is not None:
            if kind in (-1, 1, 3, 4):
                return True
            if kind in (0, 5):
                return False
            if kind == 2 and math.isfinite(radius) and radius >= 0:
                return fixed_distance > radius
            raise MemoryReadError('固定 Boss 场景条件尚未适配。')
        if kind == -1:
            return True
        if kind in (0, 1):
            ratio = hp / maximum if maximum else 0
            return ratio < .3 if kind == 0 else ratio > .8
        if kind == 3:
            return battle and not special_area()
        if kind == 4:
            return not copy.boolean(player + 296) and battle and not special_area() and len(enemies()) == 1
        if kind in (2, 5):
            nonlocal positions
            if not enemies():
                return kind == 2
            if positions is None:
                positions = Positions(copy)
            count = positions.nearby_count(player, enemies(), radius, 1 if kind == 2 else 3)
            return count == 0 if kind == 2 else count >= 3
        raise MemoryReadError('角色条件类型尚未适配。')

    def special_area():
        nonlocal area
        if area is None:
            obj = copy.singleton(0x3A1BAA0, 'GameAreaManager')
            area = copy.integer(obj + 120) > 0
        return area

    def add(stat, kind, value):
        if stat not in range(66) or kind not in (0, 1) or not math.isfinite(value):
            raise MemoryReadError('条件属性参数无效。')
        layers[kind][stat] = layers[kind].get(stat, 0) + value

    for off in (792, 808):
        for raw in copy.list(copy.ptr(player + off), 24, 'ValueTuple`4'):
            source, target, coef, status = struct.unpack('<iidQ', raw)
            if source not in range(66) or target not in range(66) or not math.isfinite(coef):
                raise MemoryReadError('角色转换参数无效。')
            if not status or status in active:
                conversions.append((source, target, coef))
    for raw in copy.list(copy.ptr(player + 816), 24, 'ValueTuple`3'):
        status, stat, coef = struct.unpack('<Qi4xd', raw)
        add(stat, 0, coef * stacks.get(status, 0))
    for raw in copy.list(copy.ptr(player + 880), 24, 'ValueTuple`4'):
        status, stat, kind, coef = struct.unpack('<Qiid', raw)
        add(stat, kind, coef * stacks.get(status, 0))
    for raw in copy.list(copy.ptr(player + 856), 32, 'ValueTuple`6'):
        cond, radius, stat, kind, value, ability = struct.unpack('<ifiidQ', raw)
        if ability and fixed_distance is None:
            warnings.append('施放指定技能时的条件属性未计入，相关属性为参考值。')
        elif not ability and condition(cond, radius):
            add(stat, kind, value)
    for raw in copy.list(copy.ptr(player + 864), 32, 'ValueTuple`5'):
        cond, status, stat, kind, value = struct.unpack('<i4xQiid', raw)
        if (not status or status in active) and condition(cond, 6):
            add(stat, kind, value)
    return layers, conversions


def _snapshot(reader):
    reader.check_game_files()
    copy = _Copy(reader)
    manager = copy.singleton(0x3A17CD8, 'GameManager')
    if copy.integer(manager + 168) != 1:
        raise MemoryReadError('进入角色后可读取当前属性与装备。')
    data = copy.singleton(0x3A1BA98, 'PlayerData')
    combat = copy.verify(copy.ptr(manager + 64), 'PlayerCombatController')
    player = copy.verify(copy.ptr(combat + 32), 'Player')
    copy.klass(copy.ptr(copy.ptr(player) + 88), 'Character')
    hero = copy.verify(copy.ptr(manager + 56), 'HeroData')
    primary, class_mask = copy.integer(hero + 44), copy.integer(hero + 48)
    if primary not in (0, 1, 2) or class_mask not in (1, 2, 4, 8):
        raise MemoryReadError('角色职业或主属性尚未适配。')
    save = copy.verify(reader.singleton('SaveSystem'), 'SaveSystem')
    mode, slot = struct.unpack('<ii', copy.read(save + 32, 8))
    label = reader.string(copy.ptr(data + 40)) or '未命名角色'
    level = decode_obscured_int(copy.read(data + 64, 16))
    if mode not in (0, 1) or not 0 <= slot < 100 or not 1 <= level <= 200:
        raise MemoryReadError('角色标识或等级无效。')
    hp_max, hp = struct.unpack('<qq', copy.read(data + 112, 16, stable=False))
    if not 0 <= hp <= hp_max <= 1e15:
        raise MemoryReadError('当前生命数值无效。')
    base = {7: decode_obscured_long(copy.read(player + 48, 32)),
            3: decode_obscured_long(copy.read(player + 80, 32)),
            14: decode_obscured_long(copy.read(player + 216, 32)),
            primary: decode_obscured_float(copy.read(player + 416, 20))}
    for stat, offset in ((10, 112), (4, 132), (5, 152), (6, 172), (22, 192), (11, 436), (12, 456), (13, 476)):
        base[stat] = decode_obscured_float(copy.read(player + offset, 20))
    caches = {off: copy.dictionary(copy.ptr(player + off)) for off in (536, 544, 568, 576, 632, 640, 648)}
    status_obj = copy.verify(copy.ptr(player + 304), 'StatusEffectController')
    status = tuple(copy.dictionary(copy.ptr(status_obj + off), 'double') for off in (56, 64, 72))
    warnings = []
    conditional, conversions = _conditional(copy, player, manager, status_obj, hp, hp_max, warnings)
    equipment_manager = copy.singleton(0x3A19650, 'EquipmentManager')
    equipped = copy.dictionary(copy.ptr(equipment_manager + 40), 'pointer')
    uids = copy.dictionary(copy.ptr(equipment_manager + 48), 'pointer')
    reader.snapshot_stamps = []
    registry, stamp = reader.registry()
    equipment = []
    for position, obj in sorted(equipped.items()):
        if not obj:
            continue
        if position not in range(12):
            raise MemoryReadError('穿戴部位尚未适配。')
        copy.verify(obj, 'ItemData')
        uid = reader.string(uids.get(position, 0))
        if not uid or uid not in registry:
            raise MemoryReadError('穿戴装备未在注册表中找到。')
        row = {**reader.base_item(obj), **reader.generated(registry[uid]),
               'item_uid': uid, 'equipped_slot': position, 'container': 'equipped', 'slot_index': position, 'count': 1}
        equipment.append(row)
    model = {'base': base, 'caches': caches, 'status': status,
             'conditional': conditional, 'conversions': conversions}
    stats = final_stats(model)
    # Rebuild from permanent layers, never freeze a combat-time sample. Status
    # stacks and status-dependent conversions are excluded by the fixed reader.
    distance = boss_distance(class_mask)
    fixed_conditional, fixed_conversions = _conditional(copy, player, manager, status_obj, 1, 1, [], distance)
    comparison_model = {'base': base, 'caches': caches, 'status': ({}, {}, {}),
                        'conditional': fixed_conditional, 'conversions': fixed_conversions}
    comparison_stats = final_stats(comparison_model)
    copy.finish()
    if reader.memory.read(stamp['object'] + 24, 24) != stamp['header'] or any(
            reader.memory.read(address, len(raw)) != raw for address, raw in reader.snapshot_stamps):
        raise SnapshotChangedError('穿戴装备在读取时变化，等待下一轮读取。')
    return {'available': True, 'session': reader.session_id, 'character_id': f'{mode}:{slot}:{label}',
        'name': label, 'level': level, 'class_mask': class_mask, 'primary': primary,
        'health': hp, 'health_max': hp_max, 'stats': stats, 'attack': panel_attack(comparison_stats, primary),
        'equipment': equipment, 'model': model, 'comparison_model': comparison_model,
        'comparison_stats': comparison_stats, 'comparison_distance': distance, 'comparison_note': PREVIEW_NOTE,
        'warnings': list(dict.fromkeys(warnings)),
        'captured_at': datetime.now(timezone.utc).isoformat(), 'source': 'verified_game_memory'}


def read_character(reader):
    try:
        for attempt in range(3):
            try:
                return _snapshot(reader)
            except SnapshotChangedError:
                if attempt == 2:
                    raise
    except (MemoryReadError, OSError, ValueError, UnicodeError, KeyError, struct.error, OverflowError) as exc:
        return {'available': False, 'reason': str(exc), 'equipment': [], 'warnings': []}
