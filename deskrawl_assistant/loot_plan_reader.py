"""Atomic external reads of server-planned wave loot for the active online run.

The preview is never an inventory observation or proof of settlement. No game
function, save operation, network request or process write is performed here.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import hashlib
import json
import math
import struct
from types import SimpleNamespace

from .game_compatibility import CompatibilityError, assert_current
from .native_memory import MemoryReadError, SnapshotChangedError
from .paths import RESOURCE_ROOT
from .recommendation_reader import _Copy
from .stat_display import preview_value
from .mythic import MAP_ID as MYTHIC_MAP, MYTHIC, floor_id


MAX_WAVES = 512
MAX_ITEMS_PER_WAVE = 256
MAX_ITEMS = 4096
MAX_MODIFIERS = 64
MAX_QUANTITY = 1_000_000
SLOT_LABELS = {'Weapon': '武器', 'Helm': '头盔', 'Chest': '胸甲', 'Pants': '裤子',
               'Boots': '靴子', 'Belt': '腰带', 'Ring': '戒指', 'Necklace': '项链',
               'Gloves': '手套', 'Shoulder': '护肩', 'Back': '披风'}
RARITY_LABELS = {0: '普通', 1: '优秀', 2: '稀有', 3: '传说', 4: '神圣', 5: '符文套装'}
DIFFICULTY_LABELS = {'Normal': '普通', 'Nightmare': '噩梦', 'Inferno': '炼狱', 'Inferno1': '炼狱'}
PLAN_NOTE = '波次掉落计划；实际获得以通关结算为准。宝箱内容和寻宝、采集收益不计入波次合计。'


@lru_cache(maxsize=1)
def _profile():
    return json.loads((RESOURCE_ROOT / 'data/loot-plan-types.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def _labels():
    def load(name):
        return json.loads((RESOURCE_ROOT / 'data' / name).read_text(encoding='utf-8'))
    catalog, equipment, items = load('game-catalog.json'), load('equipment-ui.json'), load('item-ui.json')
    loot_items = load('loot-item-ui.json')['items']
    enums = load('affix-groups-static.json')['enumConstants']
    return {'names': {key: value for table in catalog['tables'].values()
                      for key, value in table['entries'].items()},
            'stats': catalog['tables']['Stats']['entries'],
            'stat_enums': {entry['name']: entry['value'] for entry in enums['StatType']},
            'modifier_enums': {entry['name']: entry['value'] for entry in enums['ModifierType']},
            'rarity_enums': equipment['enums']['ItemRarity'],
            'equipment': equipment['equipment'], 'items': {**items['items'], **loot_items},
            'collectibles': {**items['collectibles'], **_profile().get('materials', {})},
            'maps': {m['map_id']: m['stage'] for m in load('recommendation-catalog.json')['maps']}}


def empty_preview(phase='waiting', reason='进入在线关卡后自动显示本次掉落计划。'):
    return {'available': False, 'phase': phase, 'reason': reason, 'run': None,
            'totals': {'items': 0, 'equipment': 0, 'gold': 0, 'potion_charges': 0},
            'waves': [], 'warnings': []}


class _LootCopy(_Copy):
    def __init__(self, reader):
        # Connection hashes were verified by NativeReader, whose file stamps
        # are checked before and after copying. Do not rehash the DLL per poll.
        assert_current(reader)
        profile = _profile()
        if any(reader.profile.get(key) != profile[key]
               for key in ('metadataSha256', 'gameAssemblySha256')):
            raise CompatibilityError('本版本的关卡掉落计划尚未适配')
        super().__init__(reader)
        self.types = {entry['name']: entry for entry in profile['types']}

    def string(self, obj, *, limit=256):
        if not obj:
            return ''
        if self.reader.class_name(self.ptr(obj)) != 'String':
            raise MemoryReadError('掉落计划字符串类型不一致。')
        count = self.integer(obj + 16)
        if not 0 <= count <= limit:
            raise MemoryReadError('掉落计划字符串超出读取上限。')
        return self.read(obj + 20, count * 2).decode('utf-16-le') if count else ''

    def text(self, obj, offset):
        return self.string(self.ptr(obj + offset))

    def asset_name(self, obj):
        native = self.ptr(obj + 16)
        if not native:
            raise SnapshotChangedError('当前关卡资源正在切换。')
        raw = self.read(native + 56, 32)
        try:
            value = raw.split(b'\0', 1)[0].decode('ascii')
        except UnicodeError:
            value = ''
        valid = lambda s: bool(s) and all(c.isalnum() or c in '_-.' for c in s)
        if valid(value):
            return value
        pointer = struct.unpack_from('<Q', raw)[0]
        if not pointer:
            raise MemoryReadError('当前关卡资源名称无法确认。')
        parts = []
        for index in range(128):
            byte = self.read(pointer + index, 1)
            if byte == b'\0':
                value = b''.join(parts).decode('ascii')
                if valid(value):
                    return value
                break
            parts.append(byte)
        raise MemoryReadError('当前关卡资源名称无法确认。')

    def finish(self):
        super().finish()
        assert_current(self.reader)


def _bounded(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise MemoryReadError(label + '超出有效范围。')
    return value


def _enum(value, enums):
    if value in enums:
        return enums[value]
    # The game's Enum.TryParse also accepts a known numeric constant.
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if str(number) == value and number in enums.values() else None


def _modifier(raw, labels, warnings, index):
    stat, kind, value = raw['stat'], raw['type'], raw['value']
    if not math.isfinite(value) or abs(value) > 1e18:
        raise MemoryReadError('掉落计划词条数值无效。')
    stat_id, kind_id = _enum(stat, labels['stat_enums']), _enum(kind, labels['modifier_enums'])
    if stat_id is None or kind_id is None:
        warnings.append('存在未知词条，已保留原始字段并标为未确认。')
        return {**raw, 'text': f'未确认词条：{stat or "空属性"} / {kind or "空类型"} / {value:g}'}
    name = next(name for name, number in labels['stat_enums'].items() if number == stat_id)
    entry = labels['stats'].get('Stats.' + name, {})
    label = entry.get('Chinese (Simplified)') or entry.get('English') or name
    if kind_id == 0:
        display = preview_value(SimpleNamespace(value=value, stat_value=stat_id,
                                  record_index=index, confidence=1),
                                {'upgrade_level': 0, 'rarity': 0, 'modifiers': []})['display_value']
        suffix = ''
    else:
        display = format((Decimal(str(value)) * 100).quantize(Decimal('.1'), rounding=ROUND_HALF_UP), 'f').rstrip('0').rstrip('.') + '%'
        suffix = '（独立乘算）' if kind_id == 2 else '（加算）'
    sign = '+' if value >= 0 else ''
    return {**raw, 'key':'Stats.'+name, 'name':label, 'stat_id':stat_id, 'type_id':kind_id,
            'display_value':display+suffix, 'text': f'{label} {sign}{display}{suffix}'}


def describe_item(raw, labels=None, warnings=None):
    """Translate a copied PlanItem without guessing unknown IDs or stat units."""
    labels = labels or _labels()
    warnings = [] if warnings is None else warnings
    key = raw['base_item_id']
    equipment = labels['equipment'].get(key)
    other = {**labels['collectibles'].get(key, {}), **labels['items'].get(key, {})}
    name_row = labels['names'].get(key, {})
    known = bool(equipment or other)
    kind = 'equipment' if equipment else other.get('kind', 'material') if known else 'unknown'
    if not known:
        warnings.append('存在本地词典未收录的物品，名称和类型未确认。')
    name = name_row.get('Chinese (Simplified)') or other.get('name') or name_row.get('English')
    if not name:
        name = f'未知物品（{key or "空标识"}）'
    rarity = _enum(raw['rarity'], labels['rarity_enums'])
    rarity_source = 'plan'
    # Non-equipment has no GeneratedItemData rarity override: the tooltip
    # uses ItemData.Rarity. A generic Common protocol placeholder must not
    # turn a RuneSet into an ordinary rune. Retain the wire value separately.
    # For equipment, never hide an unknown explicit generated rarity.
    if (kind != 'equipment' or not raw['rarity']) and type(other.get('rarity')) is int:
        rarity = other['rarity']
        rarity_source = 'base_item'
    if raw['rarity'] and rarity is None:
        warnings.append('存在未知品质，已保留原始品质标识。')
    quantity = raw['qty']
    # Verified LootManager.duw -> dwa -> LootDrop.cwc: FixedQuantity is used
    # only by Material (ItemType=1), with a native minimum of one. Equipment,
    # gems, runes and consumables are one drop instance per plan record.
    # Keep raw_qty rather than pretending that this protocol field is total
    # count for every kind of item. Commodity is also a one-instance drop.
    if known and other.get('item_type') == 1:
        quantity = max(quantity, 1)
    elif kind in ('equipment', 'gem', 'rune', 'chest') or other.get('item_type') in (2, 3, 4, 5, 6, 7):
        quantity = 1
    else:
        quantity = None
        warnings.append('存在类型未确认的条目，实际数量未推测。')
    slot = raw['slot'] or (equipment or {}).get('slot', '')
    if kind == 'equipment' and slot not in SLOT_LABELS:
        warnings.append('存在未知装备部位，已标为未确认。')
    rarity_label = RARITY_LABELS.get(rarity, f'未知品质（{raw["rarity"]}）') if rarity is not None else '未指定品质' if not raw['rarity'] else f'未知品质（{raw["rarity"]}）'
    if kind == 'rune' and rarity in (3,4): rarity_label = '宏伟符文'
    elif kind == 'rune' and rarity in (0,1,2): rarity_label += '符文'
    return {'item_key': key, 'name': name, 'icon': (equipment or other).get('icon'),
            'kind': kind, 'rarity': rarity,
            'rarity_label':rarity_label, 'rarity_source':rarity_source, 'plan_rarity':raw['rarity'], 'slot':slot,
            'effect_groups':other.get('effect_groups',[]), 'effect_note':other.get('effect_note',''),
            'level': raw['level'], 'quantity': quantity, 'raw_qty': raw['qty'],
            'sockets': raw['sockets'], 'ancient': raw['ancient'], 'black_mist': raw['black_mist'],
            'chest_gold': raw['chest_gold'],
            'slot_label': SLOT_LABELS.get(slot, '未确认部位') if kind == 'equipment' else
                          {'gem': '宝石', 'rune': '符文', 'chest': '宝箱', 'material': '材料 / 其他'}.get(kind, '未知物品'),
            'modifiers': [_modifier(m, labels, warnings, index) for index, m in enumerate(raw['modifiers'])]}


def _plan_item(copy, obj):
    copy.verify(obj, 'PlanItem')
    # Identifiers stay out of the public preview. An explicitly enabled local
    # research capture can retain them via the private raw_sink.
    drop_id = copy.text(obj, 16)
    raw = {'base_item_id': copy.text(obj, 24), 'slot': copy.text(obj, 32),
           'rarity': copy.text(obj, 40), 'level': _bounded(copy.integer(obj + 48), 0, 10000, '物品等级'),
           'ancient': copy.boolean(obj + 52), 'black_mist': copy.boolean(obj + 53),
           'sockets': _bounded(copy.integer(obj + 64), 0, 64, '物品孔数'),
           'qty': _bounded(copy.integer(obj + 68), 0, MAX_QUANTITY, '物品数量'),
           'chest_gold': _bounded(copy.integer(obj + 72), 0, 2_147_483_647, '箱内金币'), 'modifiers': []}
    if not raw['base_item_id']:
        raise MemoryReadError('掉落计划物品缺少基础标识。')
    for element in copy.array(copy.ptr(obj + 56), 8, 'PlanModifier', limit=MAX_MODIFIERS):
        modifier = copy.verify(struct.unpack('<Q', element)[0], 'PlanModifier')
        raw['modifiers'].append({'stat': copy.text(modifier, 16), 'type': copy.text(modifier, 24),
                                 'value': copy.number(modifier + 32)})
    return raw, drop_id


def _snapshot(reader, raw_sink=None):
    copy = _LootCopy(reader)
    manager = copy.singleton(0x3A17CD8, 'GameManager')
    if copy.integer(manager + 168) != 1:
        copy.finish()
        return empty_preview(reason='进入角色后可显示本次在线关卡掉落计划。')
    area = copy.singleton(0x3A1BAA0, 'GameAreaManager')
    if copy.integer(area + 120) != 0:
        copy.finish()
        return empty_preview(reason='当前不在战斗关卡，进入在线关卡后自动刷新。')
    save = copy.verify(reader.singleton('SaveSystem'), 'SaveSystem')
    mode = copy.integer(save + 32)
    if mode not in (0, 1):
        raise MemoryReadError('当前角色模式尚未确认。')
    if mode == 0:
        copy.finish()
        return empty_preview(reason='离线模式没有在线关卡掉落计划。')
    wave_manager = copy.verify(copy.ptr(manager + 104), 'WaveManager')
    active_map, plan = copy.ptr(manager + 112), copy.ptr(manager + 120)
    wave_map, wave_plan = copy.ptr(wave_manager + 40), copy.ptr(wave_manager + 88)
    enabled = copy.boolean(wave_manager + 96)
    if not plan or not wave_plan or not active_map or not wave_map or not enabled:
        copy.finish()
        return empty_preview(reason='正在等待本次在线关卡掉落计划。')
    if plan != wave_plan or active_map != wave_map:
        raise SnapshotChangedError('关卡身份正在切换，等待下次稳定读取。')
    copy.verify(plan, 'RunPlan')
    copy.verify(active_map, 'MapData')
    run_id, map_id, difficulty = copy.text(plan, 16), copy.text(plan, 24), copy.text(plan, 48)
    if not run_id or not map_id or copy.asset_name(active_map) != map_id:
        raise SnapshotChangedError('本次掉落计划与活动关卡尚未一致。')
    raw_map_id = map_id
    world_difficulty = 'Inferno' if difficulty == 'Inferno1' else difficulty
    mythic_floor = None
    if copy.integer(active_map + 32) == 1:
        mythic_floor = copy.integer(plan + 64)
        if map_id != MYTHIC_MAP or mythic_floor != copy.integer(wave_manager + 304):
            raise SnapshotChangedError('神话裂隙掉落计划与当前层数尚未一致。')
        map_id = floor_id(mythic_floor)
    current_wave = copy.integer(wave_manager + 48)
    wave_objects = copy.array(copy.ptr(plan + 88), 8, 'RunWave', limit=MAX_WAVES)
    if not wave_objects or not 0 <= current_wave <= len(wave_objects):
        raise MemoryReadError('本次掉落计划波次数无效。')
    finished = copy.boolean(wave_manager + 248)
    # At clear-end the preview remains valid only while this same run is the
    # active combat area; town is already handled above.
    labels, warnings, waves, drop_ids = _labels(), [PLAN_NOTE], [], set()
    count = 0
    raw_waves = []
    raw_run = None
    if raw_sink is not None:
        raw_run = {'runId':run_id, 'mapId':raw_map_id, 'difficulty':difficulty,
            'dataVersion':copy.text(plan,32), 'playerLevel':copy.integer(plan+40),
            'mythicTier':copy.integer(plan+64), 'waves':raw_waves}
        for name, offset in (('enemyHealthMultiplier',72), ('enemyDamageMultiplier',80)):
            value = struct.unpack('<d',copy.read(plan+offset,8))[0]
            if not math.isfinite(value): raise MemoryReadError('掉落计划环境数值无效。')
            raw_run[name] = value
    for index, element in enumerate(wave_objects, 1):
        wave = copy.verify(struct.unpack('<Q', element)[0], 'RunWave')
        if copy.integer(wave + 16) != index:
            raise MemoryReadError('掉落计划波次编号不连续。')
        loot = copy.ptr(wave + 48)
        row = {'number': index, 'state': 'completed' if finished or index < current_wave else
               'current' if index == current_wave else 'upcoming',
               'gold': 0, 'potion_charges': 0, 'items': []}
        raw_wave = {'wave':index, 'type':copy.text(wave,24), 'level':copy.integer(wave+32),
                    'loot':None} if raw_sink is not None else None
        if loot:
            copy.verify(loot, 'WaveLoot')
            row['gold'] = _bounded(copy.integer(loot + 16), 0, 2_147_483_647, '波次金币')
            row['potion_charges'] = _bounded(copy.integer(loot + 20), 0, MAX_QUANTITY, '药水充能')
            if raw_wave is not None:
                raw_wave['loot'] = {'gold':row['gold'], 'potionCharges':row['potion_charges'], 'items':[]}
            for item_element in copy.array(copy.ptr(loot + 24), 8, 'PlanItem', limit=MAX_ITEMS_PER_WAVE):
                count += 1
                if count > MAX_ITEMS:
                    raise MemoryReadError('本次掉落条目超出读取上限。')
                raw, drop_id = _plan_item(copy, struct.unpack('<Q', item_element)[0])
                if drop_id:
                    if drop_id in drop_ids:
                        raise MemoryReadError('掉落计划含重复条目标识。')
                    drop_ids.add(drop_id)
                row['items'].append(describe_item(raw, labels, warnings))
                if raw_wave is not None:
                    raw_wave['loot']['items'].append({'dropId':drop_id, 'baseItemId':raw['base_item_id'],
                        'slot':raw['slot'], 'rarity':raw['rarity'], 'itemLevel':raw['level'],
                        'ancient':raw['ancient'], 'blackMist':raw['black_mist'], 'modifiers':raw['modifiers'],
                        'sockets':raw['sockets'], 'qty':raw['qty'], 'chestGold':raw['chest_gold']})
        waves.append(row)
        if raw_wave is not None: raw_waves.append(raw_wave)
    copy.finish()
    if raw_sink is not None:
        raw_sink.update(raw_run)
    if difficulty not in DIFFICULTY_LABELS:
        warnings.append('难度名称未在本地词典中确认，已显示原始名称。')
    return {'available': True, 'phase': 'active', 'reason': '',
            'run': {'key': hashlib.sha256((reader.session_id + '\0' + run_id).encode()).hexdigest(),
                    'id': hashlib.sha256(run_id.encode()).hexdigest(), 'difficulty': MYTHIC if mythic_floor else world_difficulty,
                    'world_difficulty': world_difficulty, 'mythic_floor': mythic_floor,
                    'map_id': map_id, 'map_name': f'神话裂隙 · 层级 {mythic_floor}' if mythic_floor else labels['maps'].get(map_id, map_id),
                    'difficulty_label': '神话裂隙' if mythic_floor else DIFFICULTY_LABELS.get(difficulty, difficulty or '未确认难度'),
                    'current_wave': current_wave, 'total_waves': len(waves)},
            'totals': {'items': count, 'equipment': sum(item['kind'] == 'equipment' for w in waves for item in w['items']),
                       'gold': sum(w['gold'] for w in waves), 'potion_charges': sum(w['potion_charges'] for w in waves)},
            'waves': waves, 'warnings': list(dict.fromkeys(warnings))}


def read_loot_plan(reader, *, raw_sink=None):
    """Return an empty payload on every transition/failure; no stale fallback."""
    try:
        if raw_sink is not None: raw_sink.clear()
        for attempt in range(3):
            try:
                return _snapshot(reader) if raw_sink is None else _snapshot(reader, raw_sink)
            except SnapshotChangedError:
                if attempt == 2:
                    raise
    except CompatibilityError:
        return empty_preview('unsupported', '当前游戏版本的关卡掉落预览尚未适配，请更新助手后重连。')
    except (MemoryReadError, OSError, ValueError, UnicodeError, KeyError, struct.error, AttributeError) as exc:
        reason = str(exc)
        # Do not expose address-bearing lower-level diagnostics in this view.
        if '0x' in reason or isinstance(exc, (OSError, KeyError, AttributeError, struct.error)):
            reason = '当前掉落计划读取未完成，等待下次刷新。'
        return empty_preview('unavailable', reason)
