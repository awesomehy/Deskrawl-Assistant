"""Read the current character's native PlayerStatistics; never call game methods."""
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import struct

from .native_memory import MemoryReadError, SnapshotChangedError
from .recommendation_reader import _Copy, decode_obscured_int


def _enum_values(inspector, name):
    """Decode metadata v39 integer defaults, including obfuscated field tokens."""
    index = next(n for n, row in enumerate(inspector.defs)
                 if row['name'] == name and not row['namespace'])
    start, _, count = inspector.section(92, 10)
    data_start, data_size, _ = inspector.section(104)
    defaults = {field: (kind, offset) for field, kind, offset in
                (struct.unpack_from('<iHi', inspector.meta, start + n * 10) for n in range(count))}
    result = {}
    for field in inspector.fields(index):
        if not field['isLiteral']:
            continue
        kind, offset = defaults[field['fieldIndex']]
        if inspector.type_at(kind)['resolvedType'] != 'System.Int32' or not 0 <= offset < data_size:
            raise MemoryReadError('统计品质枚举结构已变化。')
        raw = inspector.meta[data_start + offset:data_start + data_size]
        first = raw[0]
        if first < 0x80:
            unsigned = first
        elif first == 0xF0 and len(raw) >= 5:
            unsigned = struct.unpack_from('<I', raw, 1)[0]
        elif first & 0xC0 == 0xC0 and first < 0xE0 and len(raw) >= 4:
            unsigned = ((first & 0x3F) << 24) | int.from_bytes(raw[1:4], 'big')
        elif first & 0xC0 == 0x80 and len(raw) >= 2:
            unsigned = ((first & 0x7F) << 8) | raw[1]
        else:
            raise MemoryReadError('统计品质枚举编码尚未适配。')
        result[field['name']] = -(unsigned // 2 + 1) if unsigned & 1 else unsigned // 2
    if len(set(result.values())) != len(result) or any(not 0 <= value < 32 for value in result.values()):
        raise MemoryReadError('统计品质枚举数值无效。')
    return result


def _layout(reader):
    if hasattr(reader, '_statistics_layout'):
        return reader._statistics_layout
    if hasattr(reader, '_statistics_layout_error'):
        raise MemoryReadError(reader._statistics_layout_error)
    try:
        from .il2cpp_metadata import MetadataInspector
        from .native_signatures import Signatures
        from capstone.x86 import X86_OP_MEM, X86_REG_RCX
        inspector = MetadataInspector(reader.metadata, Path(reader.module['path']))
        signatures = Signatures(inspector)
        index = signatures.defs['PlayerStatistics']
        fields = inspector.fields(index)
        select = lambda kind, static: [f for f in fields if f['resolvedType'] == kind and f['isStatic'] == static]
        singleton, duration, counts = select('PlayerStatistics', True), select('System.Double', False), select('System.Int32[]', False)
        if any(len(rows) != 1 for rows in (singleton, duration, counts)):
            raise MemoryReadError('游戏累计统计字段需要维护适配。')
        offsets = [rows[0]['rawRegistrationOffset'] for rows in (singleton, duration, counts)]
        if offsets[0] != 0 or any(not isinstance(off, int) or not 16 <= off < 4096 for off in offsets[1:]):
            raise MemoryReadError('游戏累计统计字段位置无效。')
        methods = inspector.methods(index)
        time_getters = [m for m in methods if not m['isStatic'] and m['returnType'] == 'System.Double' and not m['parameters']]
        count_getters = [m for m in methods if not m['isStatic'] and m['returnType'] == 'System.Int32'
                         and [p['resolvedType'] for p in m['parameters']] == ['ItemRarity']]
        if len(time_getters) != 1 or len(count_getters) != 1:
            raise MemoryReadError('游戏累计统计读取逻辑需要维护适配。')
        def instructions(method):
            rid = int(method['token'], 16) & 0xffffff
            pointer = signatures.pe.unpack('<Q', signatures.modules['Assembly-CSharp.dll']['methods'] + (rid - 1) * 8)[0]
            return signatures.instructions(pointer)
        def accesses(rows, offset):
            return any(op.type == X86_OP_MEM and op.mem.base == X86_REG_RCX and op.mem.disp == offset
                       for ins in rows for op in ins.operands)
        time_body, count_body = instructions(time_getters[0]), instructions(count_getters[0])
        if (not accesses(time_body, offsets[1]) or not accesses(count_body, offsets[2])
                or not any(op.type == X86_OP_MEM and op.mem.scale == 4 and op.mem.disp == 32
                           for ins in count_body for op in ins.operands)):
            raise MemoryReadError('游戏累计统计原生逻辑与字段不一致。')
        rarity = _enum_values(inspector, 'ItemRarity')
        if 'Divine' not in rarity or 'RuneSet' not in rarity:
            raise MemoryReadError('神圣装备或符文套装统计类别需要维护适配。')
        reader._statistics_layout = {
            'rva': signatures.singleton_rva('PlayerStatistics'), 'duration': offsets[1], 'counts': offsets[2],
            'rarity': rarity, 'fields': [{'name': f['name'], 'offset': f['rawRegistrationOffset'],
                                        'token': int(f['token'], 16)} for f in fields]}
        # Reuse this metadata inspection and cache a missing gold signature too.
        # Coin statistics are optional and must not block other game statistics.
        try:
            reader._statistics_layout['gold'] = _gold_pickup_offset(fields, methods, instructions)
        except MemoryReadError as exc:
            reader._statistics_layout['gold_reason'] = str(exc)
        return reader._statistics_layout
    except (KeyError, ValueError, StopIteration, OSError, MemoryReadError, struct.error) as exc:
        reader._statistics_layout_error = '游戏累计统计暂不可读：' + str(exc)
        raise MemoryReadError(reader._statistics_layout_error) from exc


def _statistics_object(reader, copy, layout):
    klass = copy.ptr(reader.module['base'] + layout['rva'])
    if (not klass or reader.class_name(klass) != 'PlayerStatistics'
            or reader.fields(klass) != layout['fields']):
        raise MemoryReadError('游戏累计统计的运行字段与已验证结构不一致。')
    static = copy.ptr(klass + 184)
    if not static:
        raise MemoryReadError('游戏累计统计尚未初始化。')
    obj = copy.ptr(static)
    if not obj or copy.ptr(obj) != klass:
        raise MemoryReadError('游戏累计统计当前实例尚未加载。')
    return obj


def _totals(reader, copy, layout):
    obj = _statistics_object(reader, copy, layout)
    # Native Update adds UnityEngine.Time.deltaTime to this Double: units are seconds.
    duration = struct.unpack('<d', copy.read(obj + layout['duration'], 8, stable=False))[0]
    if not math.isfinite(duration) or not 0 <= duration <= 10**10:
        raise MemoryReadError('游戏累计游玩时长无效。')
    array = copy.ptr(obj + layout['counts'])
    if not array:
        raise MemoryReadError('游戏累计掉落统计尚未加载。')
    rows = copy.array(array, 4, expected_name='Int32', limit=32)
    if len(rows) != max(layout['rarity'].values()) + 1:
        raise MemoryReadError('游戏累计掉落统计类别数量不一致。')
    counts = [struct.unpack('<i', row)[0] for row in rows]
    if any(value < 0 for value in counts):
        raise MemoryReadError('游戏累计掉落统计数值无效。')
    return {'play_seconds': int(duration), 'divine_count': counts[layout['rarity']['Divine']],
            'rune_set_count': counts[layout['rarity']['RuneSet']]}


def _gold_pickup_offset(fields, methods, instructions):
    """Find the Int64 incremented by the native positive Int32 pickup handler.

    In 1.0.3, LootDrop.cxi publishes the picked coin value to the +216 event;
    PlayerStatistics.fpn adds it at +48. PlayerData's balance-change event is
    +224 instead, so sales and spending do not enter this counter.
    """
    from capstone.x86 import X86_OP_MEM, X86_REG_RCX
    offsets = {f['rawRegistrationOffset'] for f in fields
               if not f['isStatic'] and f['resolvedType'] == 'System.Int64'
               and type(f['rawRegistrationOffset']) is int and 16 <= f['rawRegistrationOffset'] < 4096}
    increments = []
    for method in methods:
        if (method['isStatic'] or method['returnType'] != 'System.Void'
                or [p['resolvedType'] for p in method['parameters']] != ['System.Int32']):
            continue
        for ins in instructions(method):
            if ins.mnemonic != 'add' or not ins.operands: continue
            target = ins.operands[0]
            if (target.type == X86_OP_MEM and target.size == 8 and target.mem.base == X86_REG_RCX
                    and target.mem.disp in offsets):
                increments.append(target.mem.disp)
    if len(increments) != 1:
        raise MemoryReadError('游戏金币掉落统计逻辑需要维护适配。')
    return increments[0]


def read_gold_ledger(reader, character_id):
    """Read actual cumulative coin pickups, independently of automation gates."""
    try:
        reader.check_game_files()
        if not isinstance(character_id, str) or not character_id:
            raise MemoryReadError('金币掉落统计的角色身份尚未确认。')
        layout = _layout(reader)
        offset = layout.get('gold')
        if type(offset) is not int:
            raise MemoryReadError(layout.get('gold_reason') or '游戏金币掉落统计逻辑需要维护适配。')
        copy = _Copy(reader)
        obj = _statistics_object(reader, copy, layout)
        total = struct.unpack('<q', copy.read(obj + offset, 8))[0]
        if total < 0: raise MemoryReadError('游戏累计金币掉落数值无效。')
        copy.finish()
        source = hashlib.sha256(f'{reader.session_id}:{character_id}:{obj}'.encode()).hexdigest()
        return {'available': True, 'id': source, 'total': total, 'source': 'PlayerStatistics.coin_pickups'}
    except (MemoryReadError, OSError, ValueError, KeyError, UnicodeError, struct.error, OverflowError) as exc:
        return {'available': False, 'reason': str(exc)}


def _snapshot(reader, layout):
    copy = _Copy(reader)
    manager = copy.singleton(0x3A17CD8, 'GameManager')
    if copy.integer(manager + 176) != 1:
        raise MemoryReadError('进入角色后才能读取游戏累计统计。')
    data = copy.singleton(0x3A1BA98, 'PlayerData')
    hero = copy.verify(copy.ptr(manager + 56), 'HeroData')
    combat = copy.verify(copy.ptr(manager + 64), 'PlayerCombatController')
    player = copy.verify(copy.ptr(combat + 32), 'Player')
    save = copy.verify(reader.singleton('SaveSystem'), 'SaveSystem')
    mode, slot = struct.unpack('<ii', copy.read(save + 32, 8))
    # Guard character identity locally across the read; no name/slot goes into the report.
    copy.ptr(data + 40)
    class_mask = copy.integer(hero + 48)
    level = decode_obscured_int(copy.read(player + 32, 16))
    if mode not in (0, 1) or not 0 <= slot < 100 or class_mask not in (1, 2, 4, 8) or not 1 <= level <= 200:
        raise MemoryReadError('游戏统计的当前角色信息无效。')
    totals = _totals(reader, copy, layout)
    copy.finish()
    return {'available': True, 'session': reader.session_id, 'source': 'verified_game_statistics',
            'class_mask': class_mask, 'level': level, **totals,
            'captured_at': datetime.now(timezone.utc).isoformat()}


def read_player_statistics(reader):
    try:
        reader.check_game_files()
        layout = _layout(reader)
        for attempt in range(3):
            try:
                return _snapshot(reader, layout)
            except SnapshotChangedError:
                if attempt == 2:
                    raise
    except (MemoryReadError, OSError, ValueError, KeyError, UnicodeError, struct.error, OverflowError) as exc:
        return {'available': False, 'reason': str(exc), 'source': 'verified_game_statistics'}
