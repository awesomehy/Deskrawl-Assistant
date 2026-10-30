"""Read existing game loot accounting; no hooks, invocations or memory writes.

The cumulative counters cover all equipment. The bounded queue retains only
the latest 100 detailed CarriageRarityAction receipts for rarity >= Legendary.
Low-grade outcomes therefore cannot be presented as confirmed receipts.
"""
import hashlib
import struct

from .native_memory import MemoryReadError
from .recommendation_reader import _Copy


def _receipt_cursor(previous, source, counts, size, version):
    """Assign stable indices despite the game's dequeue/enqueue rotation.

    Enqueue and dequeue each increment Queue._version. Together with the size
    delta this gives the number of enqueues since the previous guarded read.
    Counters decreasing or an impossible delta means the ledger was cleared.
    """
    end = size
    if previous and previous['source'] == source:
        delta = (version - previous['version']) & 0xffffffff
        change = size - previous['size']
        numerator = delta + change
        if (all(a >= b for a, b in zip(counts, previous['counts']))
                and numerator >= 0 and not numerator % 2
                and 0 <= numerator // 2 <= 20000000
                and numerator // 2 >= max(change, 0)):
            end = previous['end'] + numerator // 2
    return {'source': source, 'counts': tuple(counts), 'size': size,
            'version': version, 'end': end}


def read_loot_ledger(reader, action_read, manager):
    copy = _Copy(reader)
    wave = copy.verify(copy.ptr(manager + 104), 'WaveManager')
    plan = copy.ptr(wave + 88)
    if not plan:
        return {'available': True, 'complete': True, 'id': None, 'counts': {}, 'receipts': [], 'policy': {}}
    copy.verify(plan, 'RunPlan')
    raw_id = reader.string(copy.ptr(plan + 16)) or ''
    if not raw_id: raise MemoryReadError('掉落统计关卡标识未确认。')
    counter_array = copy.ptr(wave + 160)
    counts = [struct.unpack('<i', raw)[0] for raw in copy.array(counter_array, 4, 'Int32', limit=16)]
    if len(counts) != 6 or any(not 0 <= n <= 10000000 for n in counts):
        raise MemoryReadError('游戏掉落计数格式未确认。')
    receipts = []
    receipt_list = copy.ptr(wave + 176)
    receipt_array = copy.ptr(receipt_list + 16)
    if receipt_array:
        tuple_type = copy.ptr(copy.ptr(receipt_array) + 64)
        expected = {'Item1':16, 'Item2':24, 'Item3':28, 'Item4':29}
        if {f['name']:f['offset'] for f in reader.fields(tuple_type)} != expected:
            raise MemoryReadError('马车处置记录字段布局未确认。')
    records = copy.queue(receipt_list, 16, 'ValueTuple`4', limit=100)
    version = struct.unpack('<I', copy.read(receipt_list + 36, 4))[0] if receipt_list else 0
    source = f'{reader.session_id}:{wave}:{counter_array}:{receipt_list}'
    cursor = _receipt_cursor(reader.__dict__.get('_loot_receipt_cursor'), source,
                             counts, len(records), version)
    for index, raw in enumerate(records, cursor['end'] - len(records)):
        item, action, ancient, mist = struct.unpack('<QiBB2x', raw)
        if action not in (0, 1, 2) or ancient not in (0, 1) or mist not in (0, 1):
            raise MemoryReadError('马车处置记录格式未确认。')
        # base_item verifies the ItemData hierarchy against the shipped layout.
        row = reader.base_item(item)
        copy.read(item + 32, 8)
        if not row.get('is_equipment'): continue
        row.update(kind='equipment', count=1, is_ancient=bool(ancient), is_black_mist=bool(mist))
        receipts.append({'index': index, 'action': {0:'pickup', 1:'sell', 2:'salvage'}[action], 'item': row})
    automation = action_read.singleton('CarriageAutomation')
    policy_obj = copy.ptr(automation + 32)
    copy.layout(policy_obj, 'Dictionary`2', {'_entries':24, '_count':32, '_freeCount':40, '_version':44})
    array, count, _, free, _ = struct.unpack('<Qiiii', copy.read(policy_obj + 24, 24))
    if not 0 <= free <= count <= 16: raise MemoryReadError('马车策略条目数无效。')
    if array:
        entry_type = copy.ptr(copy.ptr(array) + 64)
        if {f['name']:f['offset'] for f in reader.fields(entry_type)} != {'hashCode':16,'next':20,'key':24,'value':28}:
            raise MemoryReadError('马车策略条目布局未确认。')
    policy = {}
    for raw in copy.array(array, 16, count=count, limit=32):
        _, next_, key, value = struct.unpack('<Iiii', raw)
        if next_ < -1: continue
        if key in policy: raise MemoryReadError('马车策略含重复品质。')
        policy[key] = value
    if len(policy) != count-free: raise MemoryReadError('马车策略有效条目数不一致。')
    if any(key not in range(6) or value not in (0, 1, 2) for key, value in policy.items()):
        raise MemoryReadError('马车自动处理策略未确认。')
    copy.finish()
    # A rejected snapshot must never consume receipt indices.
    reader._loot_receipt_cursor = cursor
    # Built-in repeat creates a new RunPlan GUID while preserving these
    # cumulative counters/list. A round GUID is not an accounting reset.
    return {'available': True, 'complete': True, 'id': hashlib.sha256(source.encode()).hexdigest(),
            'run_id':hashlib.sha256(raw_id.encode()).hexdigest(),
            'counts': {str(n):count for n,count in enumerate(counts)},
            'receipts': receipts, 'policy': {str(n):v for n,v in policy.items()},
            'source': 'WaveManager.ess'}
