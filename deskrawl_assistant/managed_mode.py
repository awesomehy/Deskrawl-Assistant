"""Pure, serial managed-mode planner. It never calls or writes to the game.

Every action needs a fresh observation and a verified receipt. The power score
is character.preview's fixed panel estimate, not damage per second: legendary
proc differences are not predicted. An executor must still recheck identities.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math

from .models import ValidationError
from .game_panel_log import DisposalLogCursor
from .item_protection import divine_equipment, mandatory_storage, disposal_protection, is_rune, town_rewards_clear
from .storage_preferences import DEFAULT_STORAGE_PAGES, validate_available_storage_pages
from .mythic import MYTHIC, floor_number
from .equipment_requirements import equip_eligibility
from .stargazing import DEFAULT_STARGAZING, validate_stargazing, enabled as stargazing_enabled, decide as stargazing_decide, combat_rewards_clear, combat_rewards_known
from .combat_rewards import CompletionRewards
from .synthesis import (DEFAULT_SYNTHESIS, enabled as synthesis_enabled,
                        validate_settings as validate_synthesis, next_action as synthesis_action, KINDS as SYNTHESIS_ACTIONS)
from .progression import (DEFAULT_PROGRESSION, DEFAULT_LIFE, DEFAULT_MERCHANT,
                          validate_growth, enabled as growth_enabled, growth_action, next_purchase, PURCHASE_BATCH_LIMIT)
from .reconnect import DEFAULT_RECONNECT, validate_settings as validate_reconnect


DEFAULT_SETTINGS = {
    'reconnect': deepcopy(DEFAULT_RECONNECT),
    'synthesis': deepcopy(DEFAULT_SYNTHESIS),
    'progression': deepcopy(DEFAULT_PROGRESSION), 'life_skills': deepcopy(DEFAULT_LIFE),
    'merchant': deepcopy(DEFAULT_MERCHANT),
    'background': {'rule_monitoring': True, 'inventory_automation': True},
    'auto_equip': {'enabled': False, 'minimum_delta': 1, 'slot_locks': {}, 'disabled_slots': []},
    'farming': {'enabled': False, 'mode': 'repeat', 'dungeon_id': None, 'difficulty':'current',
                'progression': [], 'repeat_maps': [], 'free_threshold': 4, 'stargazing': deepcopy(DEFAULT_STARGAZING)},
    'cleanup': {'equipment_action': 'store', 'equipment_fallback': 'sell', 'equipment_policies': {},
                'equipment_pages': list(DEFAULT_STORAGE_PAGES['equipment']), 'rune_action': 'store', 'rune_pages': list(DEFAULT_STORAGE_PAGES['rune']),
                'gem_action': 'store', 'gem_pages': list(DEFAULT_STORAGE_PAGES['gem']),
                'gold_bar_action': 'store', 'gold_bar_pages': list(DEFAULT_STORAGE_PAGES['gem']),
                'legendary_rein_action': 'store', 'legendary_rein_pages': list(DEFAULT_STORAGE_PAGES['gem'])},
}
STORAGE_MATERIALS = {'gold_bar': '金条', 'legendary_rein': '传说缰绳'}
EQUIPMENT_BUCKETS = ('divine', 'black_mist', 'ancient', 'legendary', 'rare', 'uncommon', 'normal')
BUCKET_LABELS = dict(zip(EQUIPMENT_BUCKETS, ('神圣', '黑雾传说', '远古传说', '普通传说', '稀有', '优秀', '普通')))
DISPOSABLE_BUCKETS = EQUIPMENT_BUCKETS[2:]
DIFFICULTIES = ('Normal','Nightmare','Inferno')
FARM_DIFFICULTIES = (*DIFFICULTIES,MYTHIC)
DIFFICULTY_LABELS = {**dict(zip(DIFFICULTIES,('普通','噩梦','炼狱'))),MYTHIC:'神话裂隙'}


def run_record(value):
    """The native reader returns None when no dungeon run is active."""
    if value is None:
        return {}
    if not isinstance(value,dict):
        raise ValidationError('关卡运行记录格式未确认。')
    return value


def validate_settings(raw, *, equipment_keys=None, available_pages=None, allow_empty_rune_pages=False,
                      allow_empty_storage_pages=False):
    """Validate choices and require open destinations when permissions are known."""
    if not isinstance(raw, dict):
        raise ValidationError('托管配置格式无效。')
    raw = deepcopy(raw)
    raw.setdefault('reconnect', deepcopy(DEFAULT_RECONNECT))
    raw.setdefault('background', deepcopy(DEFAULT_SETTINGS['background']))
    raw.setdefault('synthesis', deepcopy(DEFAULT_SYNTHESIS))
    for name in ('progression', 'life_skills', 'merchant'):
        raw.setdefault(name, deepcopy(DEFAULT_SETTINGS[name]))
    if set(raw) != set(DEFAULT_SETTINGS): raise ValidationError('托管配置格式无效。')
    if isinstance(raw.get('farming'),dict):
        raw['farming'].setdefault('difficulty','current')
        raw['farming'].setdefault('repeat_maps',[])
        raw['farming'].setdefault('stargazing', deepcopy(DEFAULT_STARGAZING))
    if isinstance(raw.get('auto_equip'),dict):
        raw['auto_equip'].setdefault('disabled_slots',[])
    if isinstance(raw.get('cleanup'),dict):
        raw['cleanup'].setdefault('equipment_policies',{})
        for kind in STORAGE_MATERIALS:
            raw['cleanup'].setdefault(kind+'_action', 'store')
            raw['cleanup'].setdefault(kind+'_pages', deepcopy(DEFAULT_SETTINGS['cleanup'][kind+'_pages']))
    result = deepcopy(raw)
    for group, defaults in DEFAULT_SETTINGS.items():
        if not isinstance(raw[group], dict) or set(raw[group]) != set(defaults):
            raise ValidationError('托管配置字段不完整：' + group)
    e, f, c = result['auto_equip'], result['farming'], result['cleanup']
    result['synthesis'] = validate_synthesis(result['synthesis'])
    result['reconnect'] = validate_reconnect(result['reconnect'])
    f['stargazing'] = validate_stargazing(f['stargazing'])
    if type(e['enabled']) is not bool or type(f['enabled']) is not bool:
        raise ValidationError('托管开关必须为布尔值。')
    if any(type(v) is not bool for v in result['background'].values()):
        raise ValidationError('托管监控和整理开关必须为布尔值。')
    delta = e['minimum_delta']
    if type(delta) not in (int, float) or not math.isfinite(delta) or delta < 1:
        raise ValidationError('换装最小提升必须是至少 1 的有限数值。')
    disabled = e['disabled_slots']
    if (not isinstance(disabled,list) or any(type(n) is not int or n not in range(12) for n in disabled)
            or len(set(disabled)) != len(disabled)):
        raise ValidationError('不参与自动换装的部位无效。')
    e['disabled_slots'] = sorted(disabled)
    locks = e['slot_locks']
    if not isinstance(locks, dict) or any(str(k) not in {str(n) for n in range(12)} for k in locks):
        raise ValidationError('暗金锁定部位无效。')
    e['slot_locks'] = {str(k): v for k, v in locks.items()}
    if len(e['slot_locks']) != len(locks):
        raise ValidationError('暗金锁定部位重复。')
    if any(not isinstance(v, str) or not v or (equipment_keys is not None and v not in equipment_keys)
           for v in locks.values()):
        raise ValidationError('指定暗金装备无效。')
    if f['mode'] not in ('repeat', 'push') or type(f['free_threshold']) is not int or not 0 <= f['free_threshold'] <= 2048:
        raise ValidationError('刷图模式或回城空格门槛无效。')
    if f['difficulty'] not in ('current',*FARM_DIFFICULTIES):
        raise ValidationError('托管难度无效。')
    if f['dungeon_id'] is not None and (type(f['dungeon_id']) not in (int, str) or not str(f['dungeon_id'])):
        raise ValidationError('关卡标识无效。')
    p = f['progression']
    if (not isinstance(p,list) or len(p)>4096 or any(not (type(i) in (int,str) or
            isinstance(i,dict) and set(i)=={'dungeon_id','difficulty'} and type(i['dungeon_id']) in (int,str)
            and i['difficulty'] in DIFFICULTIES) for i in p) or len({str(i) for i in p})!=len(p)):
        raise ValidationError('推进关卡顺序无效。')
    repeats = f['repeat_maps']
    if isinstance(repeats,list) and not repeats and f['dungeon_id'] is not None:
        repeats = [{'dungeon_id':f['dungeon_id'],'difficulty':f['difficulty'],'runs':1}]
    if (not isinstance(repeats,list) or len(repeats)>2 or any(
            not isinstance(row,dict) or set(row)!={'dungeon_id','difficulty','runs'}
            or type(row['dungeon_id']) not in (int,str) or not str(row['dungeon_id'])
            or row['difficulty'] not in ('current',*FARM_DIFFICULTIES)
            or type(row['runs']) is not int or not 1<=row['runs']<=10000 for row in repeats)):
        raise ValidationError('重复刷图需要一张或两张关卡，每张循环次数为 1 至 10000。')
    if any((row['difficulty']==MYTHIC) != (floor_number(row['dungeon_id']) is not None) for row in repeats):
        raise ValidationError('神话裂隙请选择已解锁的层数。')
    if len({(str(row['dungeon_id']),row['difficulty']) for row in repeats})!=len(repeats):
        raise ValidationError('重复刷图的两张关卡不能相同。')
    f['repeat_maps'] = deepcopy(repeats)
    if f['mode']=='repeat' and repeats:
        f['dungeon_id'],f['difficulty'] = repeats[0]['dungeon_id'],repeats[0]['difficulty']
    if f['enabled'] and f['mode']=='repeat' and not repeats:
        raise ValidationError('请先选择托管关卡。')
    if c['equipment_action'] not in ('sell', 'salvage', 'store') or c['equipment_fallback'] not in ('sell', 'salvage'):
        raise ValidationError('未命中装备处理方式无效。')
    policies = c['equipment_policies']
    if not isinstance(policies,dict) or not set(policies) <= set(DISPOSABLE_BUCKETS):
        raise ValidationError('装备品质处理配置无效；神圣和黑雾装备固定保留。')
    for bucket, policy in policies.items():
        if (not isinstance(policy,dict) or set(policy) != {'action','fallback'}
                or policy['action'] not in ('store','sell','salvage') or policy['fallback'] not in ('sell','salvage')):
            raise ValidationError('装备品质处理方式无效：'+BUCKET_LABELS[bucket])
    # Legacy global preferences become explicit per-quality preferences without
    # changing a saved user's disposal choices. The globals only aid migration.
    c['equipment_policies'] = {bucket:deepcopy(policies.get(bucket,
        {'action':c['equipment_action'],'fallback':c['equipment_fallback']})) for bucket in DISPOSABLE_BUCKETS}
    if c['rune_action'] not in ('sell', 'store') or c['gem_action'] not in ('sell', 'store'):
        raise ValidationError('符文或宝石处理方式无效。')
    c['rune_action'] = c['gem_action'] = 'store'
    for kind, label in STORAGE_MATERIALS.items():
        if c[kind+'_action'] not in ('store', 'keep'):
            raise ValidationError(label+'处理方式仅支持放入仓库或留在背包。')
        c[kind+'_action'] = 'store'
    for name in ('equipment_pages', 'rune_pages', 'gem_pages', *(kind+'_pages' for kind in STORAGE_MATERIALS)):
        pages = c[name]
        if (not isinstance(pages, list) or len(pages) > 256 or any(type(n) is not int or n < 1 for n in pages)
                or len(set(pages)) != len(pages)):
            raise ValidationError('托管仓库页选择无效：' + name)
        if not pages and not allow_empty_storage_pages and not (name=='rune_pages' and allow_empty_rune_pages) and (
                name in ('equipment_pages','rune_pages') or c[name.replace('_pages', '_action')] == 'store'):
            raise ValidationError('请勾选用于存放物品的仓库页。')
    validate_available_storage_pages(c, available_pages)
    validate_growth(result)
    return result


def item_kind(row):
    if row.get('is_equipment') is True or row.get('kind') == 'equipment':
        return 'equipment'
    if is_rune(row): return 'rune'
    if row.get('kind') in ('gem', 'rune', 'chest', 'material'):
        return row['kind']
    name = row.get('internal_name', row.get('name_key', ''))
    if row.get('item_class') == 'RuneData': return 'rune'
    if row.get('item_class') == 'GemData' or name.startswith('Gem'): return 'gem'
    if name.startswith('TreasureChest'): return 'chest'
    return 'material'


def storage_material_kind(row):
    """Only known physical gold bars and legendary servant reins use these policies."""
    if item_kind(row) != 'material': return None
    names = {row[key] for key in ('internal_name', 'name_key') if isinstance(row.get(key), str)}
    if 'GoldBar' in names: return 'gold_bar'
    rarity = row.get('rarity')
    if rarity is None: rarity = row.get('base_rarity')
    if type(rarity) is int and rarity == 3 and any(name.startswith('Rein_') for name in names):
        return 'legendary_rein'
    return None


def managed_required_storage(row):
    """Managed retention only; manual actions and high-value monitoring stay separate."""
    return (mandatory_storage(row) or {'rune': '符文', 'gem': '宝石'}.get(item_kind(row))
            or STORAGE_MATERIALS.get(storage_material_kind(row), ''))


def managed_town_rewards_clear(observation):
    return town_rewards_clear(observation) and not any(
        managed_required_storage(row) for row in observation.get('items', [])
        if row.get('container') == 'inventory')


def retained_potion(row):
    """Native consumable potions stay in the bag; they have no equipment UID."""
    name = row.get('internal_name') or row.get('name_key')
    return (item_kind(row) == 'material' and row.get('item_class') == 'ItemData'
            and row.get('item_type') == 2 and isinstance(name, str) and 'Potion' in name
            and not row.get('issues') and not mandatory_storage(row)
            and type(row.get('count')) is int and row['count'] > 0)


def retained_merchant_item(row):
    """Explicitly known player supplies may occupy bag slots during shopping."""
    if retained_potion(row):
        return True
    return (item_kind(row) == 'material' and row.get('item_class') == 'ItemData'
            and row.get('item_type') == 7
            and (row.get('internal_name') or row.get('name_key')) == 'UpgradeCommodity1'
            and not row.get('issues') and type(row.get('count')) is int and row['count'] > 0)


def equip_candidate_allowed(row, score, settings, character):
    """Use the same slot policy when planning and just before executing a swap."""
    if divine_equipment(row) or not isinstance(score, dict): return False
    target, delta = score.get('target'), score.get('delta')
    if not equip_eligibility(row, character, target_slot=target)['can_equip']: return False
    if (type(target) is not int or target not in range(12)
            or type(delta) not in (int, float) or not math.isfinite(delta)
            or delta < settings['minimum_delta']):
        return False
    if target in settings.get('disabled_slots',[]): return False
    wanted = settings['slot_locks'].get(str(target))
    if not wanted or row.get('name_key') == wanted:
        return True
    # A named legendary still excludes other legendary names. Early-game
    # ordinary/uncommon/rare upgrades may bridge the slot until it is worn.
    rarity = row.get('rarity')
    if type(rarity) is not int or rarity not in (0, 1, 2): return False
    if not isinstance(character, dict) or character.get('available') is not True: return False
    worn = character.get('equipment')
    if not isinstance(worn, list): return False
    slots = set()
    current = None
    for item in worn:
        if not isinstance(item, dict): return False
        slot = item.get('equipped_slot')
        if type(slot) is not int or slot not in range(12) or slot in slots: return False
        slots.add(slot)
        if slot == target: current = item
    if 'replaced_uid' not in score: return False
    if current is None:
        return score['replaced_uid'] is None
    key, uid = current.get('name_key'), current.get('item_uid')
    return (isinstance(key, str) and bool(key) and isinstance(uid, str) and bool(uid)
            and score['replaced_uid'] == uid and key != wanted)


def equipment_bucket(row):
    """One mutually exclusive label, even when the game sets overlapping tags."""
    if row.get('rarity') == 4 or row.get('is_divine') is True: return 'divine'
    if row.get('is_black_mist') is True: return 'black_mist'
    if row.get('is_ancient') is True: return 'ancient'
    return {0: 'normal', 1: 'uncommon', 2: 'rare', 3: 'legendary'}.get(row.get('rarity'), 'unknown')


def equipment_policy(cleanup, row, *, fallback=False):
    bucket = equipment_bucket(row)
    if bucket not in DISPOSABLE_BUCKETS:
        raise ValidationError('当前装备品质不能按垃圾策略处理。')
    field = 'fallback' if fallback else 'action'
    return cleanup.get('equipment_policies',{}).get(bucket,{}).get(field,
        cleanup['equipment_fallback' if fallback else 'equipment_action'])


def equipment_actions(cleanup):
    return {equipment_policy(cleanup,{'rarity':rarity,'is_ancient':ancient},fallback=fallback)
        for rarity,ancient in ((0,False),(1,False),(2,False),(3,False),(3,True)) for fallback in (False,True)}


def identity(row):
    uid = row.get('item_uid')
    if isinstance(uid, str) and uid:
        return 'uid:' + uid
    return row.get('observation_id') or row.get('selection_id') or row.get('transfer_key')


def _quantity(row):
    n = row.get('count', 1)
    if type(n) is not int or n < 1: raise ValidationError('物品数量尚未确认。')
    return n


def _metric_key(row):
    name = row.get('internal_name') or row.get('name_key') or row.get('name') or '未命名物品'
    # Coins have their own native pickup counter. Consumable potions and flask
    # refills are deliberately omitted from the cumulative item table.
    kind = item_kind(row)
    if name in ('GoldDrop', 'HealthPotionDrop') or kind not in ('equipment', 'rune', 'gem') and 'Potion' in name:
        return None
    if kind == 'equipment' and equipment_bucket(row) in EQUIPMENT_BUCKETS:
        return kind, equipment_bucket(row)
    # Native fungible stacks expose base_rarity; the managed view decorates
    # the same value as rarity. Receipts and observations must share one key.
    rarity = row.get('rarity')
    if rarity is None:
        rarity = row.get('base_rarity')
    return (kind if kind in ('rune', 'gem') else 'other', name,
            rarity, row.get('rune_level', row.get('level')))


def _location_key(row):
    index = row.get('slot_index')
    if index is None: index = row.get('observation_id', row.get('selection_id'))
    return row.get('container'), str(index), _metric_key(row)


def observation_items(observation):
    if 'items' in observation:
        result = [deepcopy(r) for r in observation['items']]
    else:
        result = []
        for container, data in observation.get('containers', {}).items():
            result.extend(dict(deepcopy(r), container=container) for r in data.get('slots', []))
    known = {(r.get('container'), r.get('slot_index'), identity(r)) for r in result}
    for container in ('carriage', 'ground'):
        for row in observation.get(container, {}).get('items', []):
            row = dict(deepcopy(row), container=container)
            token = (container, row.get('slot_index'), identity(row))
            if token not in known:
                result.append(row); known.add(token)
    for row in observation.get('character', {}).get('equipment', []):
        row = dict(deepcopy(row), container='equipped')
        token = ('equipped', row.get('equipped_slot'), identity(row))
        if token not in known:
            result.append(row); known.add(token)
    return result


class ManagedMetrics:
    """Session-only loot counts: initial holdings and transfers are not drops.

    Generated identities are remembered for the whole session. Fungible stacks
    require a complete all-source observation and use conserved total quantities.
    A confirmed disposal adjusts the previous balance before the next snapshot.
    Stable explicit drop events can be supplied instead of inferred changes.
    """
    def __init__(self):
        self.baselined = False
        self.seen_uids = set()
        self.seen_events = set()
        self.balances = Counter()
        self.initial = Counter()
        self.locations = Counter()
        self.initial_locations = Counter()
        self.rows = {}
        self.totals = {}
        self.disposal_ids = set()
        self.synthesis_ids = set()
        self.crafted_uids = set()
        self.uid_acquired = set()
        self.uid_outcomes = set()
        self.inference_mode = None
        self.native_ledgers = {}
        self.native_bag_uids = set()
        self.native_policy = {}
        self.native_counts = Counter()
        self.native_estimates = Counter()
        self.native_pending = Counter()
        self.native_estimate_queue = {}
        self.native_pickup_credits = Counter()
        self.native_initialised = False
        self.native_initial_pending = Counter()
        self.native_initial_receipts = Counter()
        self.native_generations = Counter()
        self.panel_cursor = DisposalLogCursor()
        self.panel_enabled = False
        self.panel_pending = []
        self.panel_initial = {}
        self.panel_initial_gone = {}
        self.panel_tick = 0
        self.panel_events = 0
        self.panel_unparsed = 0
        self.panel_gold = 0
        self.gold = 0
        self.gold_source = None
        self.gold_previous = None
        self.gold_available = False
        self.gold_reason = '等待读取游戏金币掉落统计。'

    def _gold_observe(self, observation):
        ledger = observation.get('gold_ledger', {})
        self.gold_available = False
        if not isinstance(ledger, dict) or ledger.get('available') is not True:
            self.gold_reason = (ledger.get('reason') if isinstance(ledger, dict) else None) or '等待读取游戏金币掉落统计。'
            return
        source, total = ledger.get('id'), ledger.get('total')
        if (not isinstance(source, str) or not source or type(total) is not int
                or not 0 <= total <= 2**63-1):
            self.gold_reason = '游戏金币掉落统计格式未确认。'
            return
        # A cumulative pickup counter survives spending, selling and map repeats.
        # First reads, character changes and save rollbacks establish a baseline.
        if self.gold_source == source and self.gold_previous is not None:
            self.gold += max(0, total - self.gold_previous)
        self.gold_source, self.gold_previous = source, total
        self.gold_available, self.gold_reason = True, ''

    def _panel_observe(self, observation, rows, *, baseline=False):
        if 'system_log' not in observation: return
        self.panel_enabled = True
        new = self.panel_cursor.observe(observation['system_log'], baseline=baseline)
        if baseline:
            self.panel_initial = {r['item_uid']:deepcopy(r) for r in rows
                if item_kind(r) == 'equipment' and r.get('rarity') in (0,1,2) and r.get('item_uid')
                and r.get('container') in ('ground','carriage') and not r.get('issues')}
        if self._complete(observation):
            self.panel_tick += 1
            current = {r['item_uid']:r for r in rows if r.get('item_uid')}
            for uid, row in list(self.panel_initial.items()):
                now = current.get(uid)
                if now and now.get('container') in ('ground','carriage'): continue
                del self.panel_initial[uid]
                if now is None:
                    key = (row.get('name_key') or row.get('internal_name'), row.get('rarity'))
                    # Allow for the inventory/log snapshots straddling disposal.
                    self.panel_initial_gone.setdefault(key, []).append(self.panel_tick + 2)
            if not self.panel_initial:
                self.panel_pending.extend(new)
                new = []
            for event in self.panel_pending:
                receipt = event.get('receipt')
                if not isinstance(receipt, dict):
                    self.panel_unparsed += 1; continue
                row = receipt.get('item', {})
                field = {'sell':'sold','salvage':'salvaged'}.get(receipt.get('action'))
                rarity = row.get('rarity')
                if rarity in (3,4): continue  # WaveManager carries ancient/mist flags.
                if not field or type(rarity) is not int or rarity not in (0,1,2) or receipt.get('quantity') != 1:
                    self.panel_unparsed += 1; continue
                key = (row.get('name_key') or row.get('internal_name'), rarity)
                initial = self.panel_initial_gone.get(key, [])
                if initial:
                    initial.pop(0); continue
                self._record(row, field, 1)
                self.panel_events += 1
                if field == 'sold': self.panel_gold += receipt.get('gold') or 0
            self.panel_pending = []
            self.panel_initial_gone = {key:[expiry for expiry in values if expiry > self.panel_tick]
                for key,values in self.panel_initial_gone.items() if any(expiry > self.panel_tick for expiry in values)}
        # Only defer if source identities are incomplete or initial ground
        # items still need distinguishing. Ordinary new log records book now.
        self.panel_pending.extend(deepcopy(new))

    def _record(self, row, field, quantity):
        key = _metric_key(row)
        if key is None or quantity <= 0: return
        self.rows.setdefault(key, deepcopy(row))
        totals = self.totals.setdefault(key, {'count': 0, 'sold': 0, 'salvaged': 0, 'stored': 0})
        totals[field] += quantity

    @staticmethod
    def _complete(observation):
        if observation.get('complete') is not True: return False
        return all(observation.get(k, {}).get('complete', observation.get(k, {}).get('available')) is True
                   for k in ('carriage', 'ground'))

    def _native_observe(self, observation, rows, *, baseline=False):
        ledger = observation.get('loot_ledger', {})
        if not isinstance(ledger, dict) or ledger.get('complete') is not True or ledger.get('available') is not True: return
        baseline = baseline or not self.native_initialised
        self.native_initialised = True
        bag = {r['item_uid']:r for r in rows if item_kind(r) == 'equipment'
               and r.get('container') in ('inventory', 'storage', 'equipped') and r.get('item_uid') and not r.get('issues')}
        newly_bagged = Counter(r.get('rarity') for uid,r in bag.items() if uid not in self.native_bag_uids)
        self.native_bag_uids.update(bag)
        if not baseline: self.native_pickup_credits.update(newly_bagged)
        self.native_pending = Counter(equipment_bucket(r) for r in rows if item_kind(r) == 'equipment'
            and r.get('container') in ('ground', 'carriage')
            and r.get('item_uid') in self.uid_acquired and not r.get('issues'))
        if baseline:
            initial = [r for r in rows if item_kind(r) == 'equipment' and r.get('container') in ('ground', 'carriage')]
            self.native_initial_pending = Counter(equipment_bucket(r) for r in initial)
            self.native_initial_receipts = Counter((r.get('name_key') or r.get('internal_name'), equipment_bucket(r)) for r in initial)
        run_id = ledger.get('id')
        if not run_id:
            self.native_policy = dict(ledger.get('policy', self.native_policy)); return
        counts, receipts = ledger.get('counts', {}), ledger.get('receipts', [])
        if not isinstance(counts, dict) or not isinstance(receipts, list): return
        if any(type(n) is not int or n < 0 for n in counts.values()): return
        previous = self.native_ledgers.get(run_id)
        if baseline:
            self.native_ledgers[run_id] = deepcopy(ledger)
            self.native_policy = dict(ledger.get('policy', {})); return
        previous_counts = previous.get('counts', {}) if previous else {}
        old_indices = {r['index'] for r in (previous or {}).get('receipts', [])}
        if (any(n < previous_counts.get(k, 0) for k,n in counts.items())
                or previous and len(receipts) < len(previous.get('receipts', []))):
            self.native_estimate_queue.clear(); self.native_pickup_credits.clear()
            self.native_generations[run_id] += 1
            if (previous and ledger.get('run_id') and previous.get('run_id')
                    and ledger['run_id'] != previous['run_id']):
                previous_counts, old_indices = {}, set()
                self.native_pickup_credits.update(newly_bagged)
            else:
                self.native_ledgers[run_id] = deepcopy(ledger); self.native_policy = dict(ledger.get('policy', {})); return
        delta = {int(k):n - previous_counts.get(k, 0) for k,n in counts.items()}
        special = Counter()
        for receipt in receipts:
            if receipt['index'] in old_indices: continue
            row = receipt['item']; bucket = equipment_bucket(row)
            if row.get('rarity') == 3 and bucket in ('ancient', 'black_mist'): special[bucket] += 1
            initial_key = (row.get('name_key') or row.get('internal_name'), bucket)
            if self.native_initial_receipts[initial_key] > 0:
                self.native_initial_receipts[initial_key] -= 1
                continue
            field = {'sell':'sold', 'salvage':'salvaged'}.get(receipt.get('action'))
            if field:
                event_id = ('native_carriage', run_id, self.native_generations[run_id], receipt['index'])
                if event_id not in self.disposal_ids:
                    self.disposal_ids.add(event_id); self._record(row, field, 1)
        for rarity,bucket in ((0,'normal'), (1,'uncommon'), (2,'rare'), (3,'legendary'), (4,'divine')):
            amount = delta.get(rarity, 0)
            if rarity == 3: amount = max(0, amount - sum(special.values()))
            old = min(amount, self.native_initial_pending[bucket])
            self.native_initial_pending[bucket] -= old; amount -= old
            self.native_counts[bucket] += amount
            if rarity < 3:
                if self.panel_enabled:
                    # Never combine log receipts with policy-based estimates:
                    # a failed read can recover those same events next time.
                    self.native_estimate_queue.pop(rarity, None)
                    self.native_pickup_credits.pop(rarity, None)
                    continue
                policy = ledger.get('policy', {}).get(str(rarity))
                # Lower grades have only aggregate pickups. A stable policy
                # provides an estimate, never a verified disposal receipt.
                queued_policy, queued = self.native_estimate_queue.pop(rarity, (None, 0))
                excluded = min(queued, self.native_pickup_credits[rarity])
                self.native_pickup_credits[rarity] -= excluded
                if policy in (1, 2) and policy == queued_policy and policy == self.native_policy.get(str(rarity)):
                    estimate = max(0, queued - excluded)
                    field = 'sold' if policy == 1 else 'salvaged'
                    row = {'kind':'equipment', 'rarity':rarity, 'is_ancient':False, 'is_black_mist':False}
                    self._record(row, field, estimate); self.native_estimates[(bucket, field)] += estimate
                excluded = min(amount, self.native_pickup_credits[rarity])
                self.native_pickup_credits[rarity] -= excluded
                if policy == self.native_policy.get(str(rarity)):
                    self.native_estimate_queue[rarity] = (policy, amount - excluded)
        for bucket, amount in special.items():
            old = min(amount, self.native_initial_pending[bucket])
            self.native_initial_pending[bucket] -= old
            self.native_counts[bucket] += amount-old
        self.native_ledgers[run_id] = deepcopy(ledger)
        self.native_policy = dict(ledger.get('policy', {}))

    def observe(self, observation):
        if observation.get('complete') is not True: return
        rows = observation_items(observation)
        totals = Counter()
        examples = {}
        unique = {}
        locations = Counter()
        for row in rows:
            key = _metric_key(row)
            if key is None or row.get('issues'): continue
            uid = row.get('item_uid')
            if isinstance(uid, str) and uid:
                if uid not in unique: unique[uid] = row
            else:
                totals[key] += _quantity(row); examples[key] = row
                locations[_location_key(row)] += _quantity(row)
        if not self.baselined:
            if 'drop_events' not in observation and not self._complete(observation): return
            self.seen_uids.update(unique)
            self.balances = totals
            self.initial = totals.copy()
            self.locations = locations
            self.initial_locations = locations.copy()
            self.baselined = True
            self.inference_mode = 'events' if 'drop_events' in observation else 'snapshots'
            for event in observation.get('drop_events', []):
                self.seen_events.add(str(event.get('event_id')))
            self._panel_observe(observation, rows, baseline=True)
            self._native_observe(observation, rows, baseline=True)
            self._gold_observe(observation)
            return
        self._gold_observe(observation)
        if self._complete(observation):
            # Transport known initial units along source decreases/target gains.
            # Newly dropped stacks stay distinguishable from initial warehouse
            # stacks, even when both have the same ItemData/grade/level.
            initial_pool = Counter()
            new_pool = Counter()
            next_initial = Counter()
            for location, before in self.locations.items():
                lost = max(0, before - locations[location])
                old_lost = min(lost, self.initial_locations[location])
                initial_pool[location[2]] += old_lost
                new_pool[location[2]] += lost - old_lost
                next_initial[location] = max(0, self.initial_locations[location] - lost)
            for location, current in locations.items():
                gained = max(0, current - self.locations[location])
                from_initial = min(gained, initial_pool[location[2]])
                next_initial[location] += from_initial
                initial_pool[location[2]] -= from_initial
                gained -= from_initial
                from_new = min(gained, new_pool[location[2]])
                new_pool[location[2]] -= from_new
            self.initial_locations = +next_initial
            self.locations = locations
            self.initial = Counter()
            for location, quantity in self.initial_locations.items(): self.initial[location[2]] += quantity
        if self.inference_mode == 'events':
            for event in observation.get('drop_events', []):
                event_id = event.get('event_id')
                if event_id is None or str(event_id) in self.seen_events: continue
                self.seen_events.add(str(event_id))
                row = event.get('item', event)
                if row.get('item_uid') in self.crafted_uids:
                    continue
                self._record(row, 'count', event.get('quantity', _quantity(row)))
                if row.get('item_uid'): self.uid_acquired.add(row['item_uid'])
            self.seen_uids.update(unique)
            self._panel_observe(observation, rows)
            self._native_observe(observation, rows)
            return
        for uid, row in unique.items():
            if uid not in self.seen_uids:
                self._record(row, 'count', _quantity(row)); self.uid_acquired.add(uid)
        self.seen_uids.update(unique)
        self._panel_observe(observation, rows)
        self._native_observe(observation, rows)
        if not self._complete(observation): return
        for key, n in totals.items():
            increase = max(0, n - self.balances[key])
            self._record(examples[key], 'count', increase)
        self.balances = totals

    def synthesized(self, receipt_id, inputs, outputs):
        """Account only for a confirmed transformation, never rebaseline live loot."""
        if receipt_id in self.synthesis_ids:
            return
        self.synthesis_ids.add(receipt_id)
        for row in inputs:
            if row.get('item_uid'):
                self.seen_uids.add(row['item_uid'])
                continue
            key, location, quantity = _metric_key(row), _location_key(row), row['quantity']
            self.balances[key] = max(0, self.balances[key] - quantity)
            self.locations[location] = max(0, self.locations[location] - quantity)
            old = min(quantity, self.initial_locations[location])
            self.initial_locations[location] -= old
            self.initial[key] = max(0, self.initial[key] - old)
        for row in outputs:
            uid = row.get('item_uid')
            if uid:
                self.seen_uids.add(uid)
                self.crafted_uids.add(uid)
                continue
            key, location, quantity = _metric_key(row), _location_key(row), row['quantity']
            self.balances[key] += quantity
            self.locations[location] += quantity
            # Crafted units are known non-loot, even when they merge into an
            # existing stack containing both initial and freshly looted units.
            self.initial_locations[location] += quantity
            self.initial[key] += quantity

    def committed(self, operation_id, row, kind, quantity=None):
        if operation_id in self.disposal_ids: return
        self.disposal_ids.add(operation_id)
        field = {'sell': 'sold', 'salvage': 'salvaged', 'transfer': 'stored'}.get(kind)
        if field is None: return
        n = _quantity(row) if quantity is None else quantity
        key = _metric_key(row)
        uid = row.get('item_uid')
        # Outcomes in the cumulative-drop table apply only to drops this session.
        if uid:
            receipt_key = (field, uid)
            tracked = n if uid in self.uid_acquired and receipt_key not in self.uid_outcomes else 0
            self.uid_outcomes.add(receipt_key)
        else:
            location = _location_key(row)
            initial = self.initial_locations[location]
            excluded = min(n, initial)
            tracked = n - excluded
            if field != 'stored':
                self.initial[key] = max(0, self.initial[key] - excluded)
                self.initial_locations[location] = max(0, initial - excluded)
                self.locations[location] = max(0, self.locations[location] - n)
            totals = self.totals.get(key, {})
            prior = totals.get('stored', 0) if field == 'stored' else totals.get('sold', 0) + totals.get('salvaged', 0)
            tracked = min(tracked, max(0, totals.get('count', 0) - prior))
        if field != 'stored' and not uid and self.inference_mode == 'snapshots':
            self.balances[key] = max(0, self.balances[key] - n)
        self._record(row, field, tracked)

    def state(self):
        equipment = {bucket: {'name': BUCKET_LABELS[bucket], 'count': 0, 'sold': 0, 'salvaged': 0, 'stored': 0}
                     for bucket in EQUIPMENT_BUCKETS}
        runes, gems, others = [], [], []
        groups = {'rune': runes, 'gem': gems, 'other': others}
        for key, totals in sorted(self.totals.items(), key=lambda p: str(p[0])):
            row = self.rows[key]
            if key[0] == 'equipment':
                if key[1] in equipment: equipment[key[1]].update(totals)
            else:
                value = {'name_key': key[1], 'name': row.get('name', key[1]), 'rarity': key[2], 'level': key[3], **totals}
                groups[key[0]].append(value)
        for (bucket, field), quantity in self.native_estimates.items():
            equipment[bucket]['estimated_' + field] = quantity
        for bucket, totals in equipment.items():
            totals['count'] = max(totals['count'], self.native_counts[bucket] + self.native_pending[bucket],
                                  totals['sold'] + totals['salvaged'])
        note = '已出售／分解包含助手处置与游戏马车自动处理。'
        if self.panel_enabled:
            note += '普通、优秀、稀有的马车自动处置按游戏系统日志逐件计数。'
            if self.panel_pending: note += '部分新日志等待完整扫描核对初始物品来源。'
            if not self.panel_cursor.available: note += '游戏日志暂不可读，自动处置统计等待恢复；未用估算补填。'
            if self.panel_cursor.incomplete: note += '本次日志读取起点缺失或历史已被覆盖，统计可能不完整。'
            if self.panel_unparsed: note += f'另有 {self.panel_unparsed} 条处置日志格式未确认，未计入。'
        if self.native_estimates or not self.panel_enabled:
            note += '带 ≈ 的低品质数量按稳定马车策略估算，策略变化区间不估算。'
        return {'equipment': equipment, 'runes': runes, 'gems': gems, 'others': others, 'note':note,
                'gold': {'count': self.gold, 'available': self.gold_available, 'reason': self.gold_reason},
                'game_log':{'enabled':self.panel_enabled, 'available':self.panel_cursor.available,
                    'incomplete':self.panel_cursor.incomplete or bool(self.panel_unparsed),
                    'confirmed_disposals':self.panel_events,'sale_gold':self.panel_gold,
                    'unparsed':self.panel_unparsed,'reason':self.panel_cursor.reason}}


class ManagedMode:
    def __init__(self, settings=None):
        self.settings = validate_settings(settings or deepcopy(DEFAULT_SETTINGS))
        self.running = False
        self.phase = 'idle'
        self.status = '托管未开始。'
        self.pending = None
        self.metrics = ManagedMetrics()
        self.session = None
        self.character_id = None
        self.last_success = None
        self.last_success_difficulty = None
        self.target_dungeon = self.settings['farming']['dungeon_id']
        self.target_difficulty = None if self.settings['farming']['difficulty']=='current' else self.settings['farming']['difficulty']
        self.push_failed = False
        self.repeat_index = 0
        self.repeat_completed = 0
        self.repeat_generation = 0
        self.repeat_maps = []
        self.handled_runs = set()
        self.run_counters = {}
        self.run_counter_sources = {}
        self.run_counter_fallbacks = {}
        self.run_history_sequence = None
        self.run_entry = None
        self.completion_rewards = CompletionRewards()
        self.stargazing_seen = set()
        self.stargazing_skipped = set()
        self.stargazing_state = {'verdict': 'idle', 'reason': '', 'hits': [], 'skipped': 0}
        self.stargazing_return = None
        self.stargazing_entry_rejections = {}
        self.repeat_counts = {}
        self.force_dispose = set()
        self.equip_reserved = None
        self.sequence = 0
        self.await_effect = None
        self.await_count = 0
        self.reward_barrier = False
        self.barrier_reads = 0
        self.cleanup_processing = False
        self.finishing = False
        self.pressure_active = False
        self.finish_empty_reads = 0
        self.merchant_pending = False
        self.merchant_opened = False
        self.merchant_cleanup_pending = False
        self.merchant_received_uids = set()
        self.merchant_counts = {}
        self.merchant_last_balance = None
        self.merchant_last_capacity = None
        self.growth_wait = ''

    def start(self, observation):
        if self.running or self.pending: raise ValidationError('托管已在运行。')
        if observation.get('complete') is not True: raise ValidationError('请先完整读取背包与仓库。')
        if (not self.settings['auto_equip']['enabled'] and not self.settings['farming']['enabled']
                and not growth_enabled(self.settings) and not synthesis_enabled(self.settings)):
            raise ValidationError('请至少开启一项托管功能。')
        self.__init__(self.settings)
        self.merchant_received_uids.update(observation.get('merchant_uids',[]))
        self.running = True
        self.session = observation.get('session', observation.get('bridge_session'))
        self.character_id = self._control_character_id(observation)
        if self.target_difficulty is None:
            self.target_difficulty = observation.get('scene',{}).get('world_difficulty') or observation.get('scene',{}).get('difficulty')
        if self.settings['farming']['enabled']:
            if self.settings['farming']['mode']=='push':
                target = observation.get('farming_start')
                if target:
                    self.target_dungeon,self.target_difficulty = target['dungeon_id'],target['difficulty']
            else:
                initial_difficulty = observation.get('scene',{}).get('world_difficulty') or observation.get('scene',{}).get('difficulty')
                self.repeat_maps = [dict(row,difficulty=initial_difficulty if row['difficulty']=='current' else row['difficulty'])
                                    for row in self.settings['farming']['repeat_maps']]
                first = self.repeat_maps[0]
                self.target_dungeon,self.target_difficulty = first['dungeon_id'],first['difficulty']
        if not self.settings['farming']['enabled']:
            self.target_dungeon = observation.get('scene', {}).get('dungeon_id')
        self._baseline_runs(observation)
        self.metrics.observe(observation)
        self.phase = 'cleanup' if self.settings['farming']['enabled'] and self._town(observation) else 'farming'
        self.reward_barrier = self.phase == 'cleanup'
        self.status = '托管已开始。'
        return self.state()

    def stop(self, reason='托管已停止。'):
        self.running = False; self.phase = 'stopped'; self.status = reason
        self.pending = None; self.equip_reserved = None
        self.await_effect = None
        self.finishing = False
        return self.state()

    def request_finish(self):
        """Keep submitted receipts/fences; finish town cleanup without re-entry."""
        if not self.running and self.phase != 'paused': return self.state()
        self.finishing = True
        self.finish_empty_reads = 0
        self.running = True
        self.reward_barrier = True; self.barrier_reads = 0
        self.status = '正在结束托管：回城整理奖励后停止。'
        return self.state()

    def pause(self, reason='托管已暂停，可继续本次统计。'):
        """The executor must cancel/wait for in-flight work before calling this."""
        self.running = False; self.phase = 'paused'; self.status = reason
        self.pending = None; self.await_effect = None
        return self.state()

    def resume(self, observation):
        if self.running or self.pending is not None: raise ValidationError('托管正在运行。')
        if self.phase != 'paused': raise ValidationError('本次托管已停止，请重新开始。')
        if observation.get('complete') is not True: raise ValidationError('请先完整读取背包与仓库。')
        session = observation.get('session', observation.get('bridge_session'))
        if self.session is not None and session != self.session:
            raise ValidationError('角色或游戏连接已改变，不能继续上次托管。')
        character_id = self._control_character_id(observation)
        if self.character_id is not None and character_id is not None and character_id != self.character_id:
            raise ValidationError('角色已改变，不能继续上次托管；请重新开始。')
        if self.character_id is None: self.character_id = character_id
        self._baseline_runs(observation)
        self.running = True
        self.pending = None
        # A confirmed synthesis remains fenced across an identity-related pause.
        # Re-reading the original character must settle it before another craft.
        if not self.await_effect or self.await_effect.get('kind') not in SYNTHESIS_ACTIONS:
            self.await_effect = None; self.await_count = 0
        self.phase = 'cleanup' if (self.settings['farming']['enabled'] or self.finishing) and self._town(observation) else 'farming'
        self.reward_barrier = self.phase == 'cleanup'
        self.barrier_reads = 0
        self.metrics.observe(observation)
        self.cleanup_processing = False
        self.status = '继续本次托管，累计掉落统计已保留。'
        return self.state()

    def _pause(self, reason):
        self.running = False; self.phase = 'paused'; self.status = reason
        self.pending = None
        return None

    @staticmethod
    def _control_character_id(observation):
        # Only ManagedService's live control identity is authoritative. The
        # presentation `character` may still describe the previous login.
        value = observation.get('character_id')
        return value if isinstance(value, str) and value.strip() else None

    def check_identity(self, observation, *, require_character=False):
        session = observation.get('session', observation.get('bridge_session'))
        if self.session is not None and session != self.session:
            self._pause('游戏连接已改变，托管已暂停；请重新开始。')
            return False
        character_id = self._control_character_id(observation)
        if self.character_id is not None and character_id is not None and character_id != self.character_id:
            self._pause('角色已改变，托管已暂停；请重新开始。')
            return False
        if character_id is None and require_character:
            self.status = '等待当前角色身份重新确认，暂不取料或合成。'
            return False
        if self.character_id is None: self.character_id = character_id
        return True

    def state(self):
        return {'settings': deepcopy(self.settings), 'running': self.running, 'finishing': self.finishing, 'phase': self.phase,
                'status': self.status, 'pending': deepcopy(self.pending), 'last_success': self.last_success,
                'target_dungeon': self.target_dungeon, 'target_difficulty':self.target_difficulty,
                'last_success_difficulty':self.last_success_difficulty,
                'merchant_counts': deepcopy(self.merchant_counts), 'push_failed': self.push_failed,
                'repeat_index':self.repeat_index,'repeat_completed':self.repeat_completed,
                'repeat_maps':deepcopy(self.repeat_maps),'statistics': self.metrics.state(),
                'stargazing': deepcopy(self.stargazing_state)}

    @staticmethod
    def _town(observation):
        return observation.get('scene', {}).get('in_town') is True

    @staticmethod
    def _bag_free(observation):
        bag = observation.get('counts', {}).get('inventory')
        if bag is not None:
            capacity, occupied = bag.get('capacity'), bag.get('occupied')
        else:
            bag = observation.get('containers', {}).get('inventory', {})
            capacity, occupied = bag.get('slot_count'), bag.get('occupied_count')
        if type(capacity) is not int or type(occupied) is not int or not 0 <= occupied <= capacity:
            raise ValidationError('背包空格数量尚未完整读取。')
        return capacity - occupied

    @staticmethod
    def _safe(row):
        return bool(identity(row) and not row.get('issues') and row.get('movable', True) is not False
                    and type(row.get('locked')) is bool and type(row.get('count', 1)) is int and row.get('count', 1) > 0)

    @staticmethod
    def _review(row):
        if row.get('issues') or row.get('rule_status') in ('review', 'unknown'): return True
        if row.get('rule_status') == 'no_rule' and row.get('rules_known') is True: return False
        return row.get('review') is not False

    @staticmethod
    def _quality_known(row):
        return (type(row.get('rarity')) is int and row['rarity'] in range(5)
                and type(row.get('is_black_mist')) is bool and type(row.get('is_ancient')) is bool)

    @classmethod
    def _reward_pending(cls, row):
        if item_kind(row) != 'equipment': return False
        if not cls._safe(row): return True
        # Hidden affixes are expected on black-mist gear. Its verified identity
        # and positive retention evidence suffice to lock/store it; never turn
        # a missing affix read into a disposal decision or a batch-wide wait.
        if mandatory_storage(row) or row.get('locked') is True: return False
        return (cls._review(row) or not cls._quality_known(row)
                or type(row.get('matches')) is not bool)

    @classmethod
    def _protected(cls, row):
        return (bool(mandatory_storage(row)) or row.get('matches') is True
                or row.get('locked') is True or cls._review(row) or not cls._quality_known(row))

    @classmethod
    def _disposable(cls, row):
        return item_kind(row) == 'equipment' and cls._safe(row) and not cls._protected(row) and row.get('matches') is False

    def _action(self, kind, row=None, **data):
        self.finish_empty_reads = 0
        self.sequence += 1
        action = {'action_id': f'{id(self):x}-{self.sequence}', 'kind': kind, **data}
        if action.get('reason_code') == 'stargazing_skip':
            action['repeat_generation'] = self.repeat_generation
        if row is not None:
            action['item'] = deepcopy(row)
        self.pending = action
        self.await_effect = None
        self.status = action.get('reason', kind)
        return deepcopy(action)

    def complete(self, action_id, success=True, *, error=None, result=None):
        """A success receipt must say verified=True; no optimistic game success."""
        if self.pending is None or self.pending['action_id'] != action_id:
            raise ValidationError('托管动作编号不一致。')
        action = self.pending
        self.pending = None
        if not success or not isinstance(result, dict) or result.get('verified') is not True:
            return self.stop('操作未确认，托管已停止：' + (error or (result or {}).get('reason', '游戏执行结果未核验。')))
        quantity = result.get('quantity')
        if action.get('item') is not None and quantity is not None and (
                type(quantity) is not int or not 1 <= quantity <= _quantity(action['item'])):
            return self.stop('操作数量未确认，托管已停止。')
        self.await_effect = action
        self.await_count = 0
        row = action.get('item')
        if action['kind'] in ('sell', 'salvage'):
            self.metrics.committed(action_id, row, action['kind'], result.get('quantity'))
            self.force_dispose.discard(identity(row))
        elif action['kind'] == 'transfer':
            if action.get('reason_code') == 'warehouse_eviction': self.force_dispose.add(identity(row))
            if action.get('reason_code') == 'equip_prepare': self.equip_reserved = identity(row)
            if action.get('target') == 'storage': self.metrics.committed(action_id, row, 'transfer', result.get('quantity'))
        elif action['kind'] == 'equip':
            self.equip_reserved = None
        elif action['kind'] == 'merchant_open':
            self.merchant_opened = True
        elif action['kind'] == 'merchant_close':
            self.merchant_opened = False
            self.merchant_cleanup_pending = False
        elif action['kind'] == 'merchant_buy':
            self.merchant_opened = result.get('merchant_open') is True
            self.merchant_cleanup_pending = True
            key = str(action['shop_slot'])
            self.merchant_counts[key] = self.merchant_counts.get(key, 0) + action.get('quantity',1)
            self.merchant_received_uids.update(result.get('received_uids',[]))
            self.cleanup_processing = False
        elif action['kind'] in SYNTHESIS_ACTIONS:
            inputs, outputs = result.get('inputs'), result.get('outputs')
            if (result.get('effect_confirmed') is not True or not isinstance(inputs, list) or not inputs
                    or not isinstance(outputs, list) or not outputs
                    or any(not isinstance(item, dict) or type(item.get('quantity')) is not int
                           or item['quantity'] < 1 or _metric_key(item) is None
                           for item in inputs + outputs)):
                return self.stop('合成材料或产物回执未完整确认，已停止重复操作。')
            self.metrics.synthesized(result.get('receipt_id') or action_id, inputs, outputs)
            self.await_effect = dict(action, synthesis_receipt=deepcopy(result))
        elif action['kind'] == 'return_town':
            self.phase = 'cleanup'
            self.cleanup_processing = False
        elif action['kind'] == 'enter_dungeon':
            self.phase = 'farming'
            self.expect_run_entry(action)
        if action.get('reason_code') == 'stargazing_skip':
            source = action['source_run_id']
            # A submitted portal may arrive after this run has already won.
            # Preserve its completed quota while following the accepted travel.
            if str(source) not in self.handled_runs:
                self.stargazing_skipped.add(source)
            self.stargazing_seen.add(source)
            self.stargazing_state['skipped'] = len(self.stargazing_skipped)
            self.stargazing_return = None
            self.stargazing_entry_rejections.pop(source, None)
            index = action['next_repeat_index']
            if (index != self.repeat_index
                    and action.get('repeat_generation', self.repeat_generation) == self.repeat_generation):
                # Wins can advance both source and destination quotas while
                # combat travel is pending. A late portal receipt must not
                # restore the target chosen before those wins were counted.
                self.repeat_counts[self.repeat_index] = self.repeat_completed
                self.repeat_index = index
                self.repeat_completed = self.repeat_counts.get(index, 0)
                target = self.repeat_maps[index]
                self.target_dungeon, self.target_difficulty = target['dungeon_id'], target['difficulty']
        elif action['kind'] == 'switch_difficulty':
            self.phase = 'cleanup'
        if action['kind'] in ('return_town', 'collect_ground', 'collect_carriage', 'open_chest', 'merchant_buy'):
            self.reward_barrier = True; self.barrier_reads = 0
        if action['kind'] == 'open_chest':
            self.cleanup_processing = False
        return self.state()

    def _effect_visible(self, observation, all_items):
        """Do not queue the same action while a supposedly fresh read is stale."""
        action = self.await_effect
        if not action: return True
        kind, old = action['kind'], action.get('item')
        if kind in SYNTHESIS_ACTIONS:
            receipt = action['synthesis_receipt']
            def current(item):
                if item.get('item_uid'):
                    return [r for r in all_items if r.get('item_uid') == item['item_uid']]
                return [r for r in all_items if r.get('container') == item.get('container')
                        and r.get('slot_index') == item.get('slot_index') and _metric_key(r) == _metric_key(item)]
            visible = (all(sum(_quantity(r) for r in current(item))
                           <= _quantity(item) - item['quantity'] for item in receipt['inputs'])
                       and all(sum(_quantity(r) for r in current(item)) >= item['quantity']
                               for item in receipt['outputs']))
        elif old:
            matches = [r for r in all_items if r.get('container') == old.get('container')
                       and identity(r) == identity(old)]
            if kind == 'lock':
                visible = bool(matches and matches[0].get('locked') is True)
            elif kind == 'equip':
                visible = any(identity(r) == identity(old) and r.get('equipped_slot') == action['target_slot']
                              for r in observation.get('character', {}).get('equipment', []))
            else:
                visible = not matches or sum(_quantity(r) for r in matches) < _quantity(old)
        elif kind in ('allocate_talent', 'learn_life_skill'):
            group = 'talents' if kind == 'allocate_talent' else 'life_skills'
            state = observation.get('progression', {})
            visible = (state.get('available') is True and state.get('class_mask') == action['class_mask']
                       and state[group]['ranks'].get(action['node_id'], 0) == action['expected_rank'] + 1)
        elif kind == 'set_skill':
            state = observation.get('progression', {})
            visible = (state.get('available') is True and state['skills'].get('readable') is not False
                       and state['skills']['equipped'] == action['skill_slots'])
        elif kind == 'merchant_open':
            visible = observation.get('progression', {}).get('merchant', {}).get('open') is True
        elif kind == 'merchant_close':
            visible = observation.get('progression', {}).get('merchant', {}).get('open') is False
        elif kind == 'merchant_refresh':
            state=observation.get('progression',{})
            visible=state.get('available') is True and set(action['shop_slots']) <= {r['slot'] for r in state.get('merchant',{}).get('stock',[])}
        elif kind == 'merchant_buy':
            state = observation.get('progression', {})
            visible = (state.get('available') is True
                       and state['soul_shards']['current'] == action['before_shards'] - action['expected_price']*action.get('quantity',1))
        elif kind == 'return_town':
            visible = self._town(observation)
        elif kind == 'enter_dungeon':
            scene = observation.get('scene', {})
            visible = (not self._town(observation) and str(scene.get('dungeon_id')) == str(action['dungeon_id'])
                       and (not action.get('difficulty') or scene.get('difficulty')==action['difficulty'])
                       and run_record(scene.get('run')).get('status') in ('running', 'settling', 'success'))
            if action.get('reason_code') == 'stargazing_skip':
                new_id = run_record(scene.get('run')).get('id')
                visible = visible and bool(new_id and new_id != action.get('source_run_id'))
        elif kind == 'switch_difficulty':
            visible = self._town(observation) and observation.get('scene',{}).get('difficulty')==action['difficulty']
        else: visible = False
        if visible:
            self.await_effect = None; self.await_count = 0
            return True
        self.await_count += 1
        self.status = '等待游戏状态确认，暂不重复执行动作。'
        if self.await_count >= 5:
            self._pause('已验证动作没有反映在连续读取中，托管已暂停，请检查游戏状态。')
        return False

    def _targets(self, observation, pages):
        storage = observation.get('storage', {})
        page_size = storage.get('page_size', observation.get('transfer', {}).get('page_size'))
        allowed = storage.get('allowed_indices', observation.get('transfer', {}).get('storage_indices'))
        if (type(page_size) is not int or page_size < 1 or not isinstance(allowed, list)
                or any(type(n) is not int or n < 0 for n in allowed) or len(set(allowed)) != len(allowed)):
            raise ValidationError('仓库页大小或开放格位未确认。')
        return sorted(n for n in allowed if n // page_size + 1 in pages)

    @staticmethod
    def _room(row, targets, all_items):
        storage = {r['slot_index']: r for r in all_items if r.get('container') == 'storage' and type(r.get('slot_index')) is int}
        if any(n not in storage for n in targets): return True
        if item_kind(row) != 'gem': return False
        maximum = row.get('max_stack')
        if type(maximum) is not int or maximum < 2: return False
        key = _metric_key(row)
        space = sum(max(0, maximum - _quantity(storage[n])) for n in targets
                    if _metric_key(storage[n]) == key and not storage[n].get('issues') and not storage[n].get('item_uid'))
        return space >= _quantity(row)

    def _equip(self, observation, all_items):
        if not self.settings['auto_equip']['enabled']: return None
        e = self.settings['auto_equip']; candidates = []
        for row in all_items:
            if row.get('container') not in ('inventory', 'storage') or item_kind(row) != 'equipment' or not self._safe(row): continue
            power = row.get('power')
            if not isinstance(power, dict) or power.get('available') is not True: continue
            for score in power.get('comparisons', [power]):
                if not equip_candidate_allowed(row, score, e, observation.get('character')): continue
                target, delta = score['target'], score['delta']
                if row.get('container') == 'storage' and self._bag_free(observation) == 0: continue
                candidates.append((delta, -target, row, score))
        if not candidates: return None
        _, _, row, score = max(candidates, key=lambda c: (c[0], c[1], str(identity(c[2]))))
        wanted = e['slot_locks'].get(str(score['target']))
        transitional = bool(wanted and row.get('name_key') != wanted)
        if row['container'] == 'storage':
            return self._action('transfer', row, target='inventory', reason_code='equip_prepare',
                                reason='将战力更高的过渡装备取到背包。' if transitional else '将可提升固定面板战力的装备取到背包。')
        return self._action('equip', row, target_slot=score['target'], expected_delta=score['delta'],
                            reason='尚未穿戴指定暗金，换上战力更高的过渡装备。' if transitional else '换上固定面板战力更高的装备。')

    def _storage_transfer(self, observation, row, pages, all_items, mandatory=False):
        targets = self._targets(observation, pages)
        material = storage_material_kind(row)
        label = STORAGE_MATERIALS.get(material) or {'equipment':'装备','rune':'符文','gem':'宝石'}.get(item_kind(row),'物品')
        if not targets:
            return self._pause('所选'+label+'仓库页尚未解锁或没有开放格位，物品保留；请解锁仓库页或修改存放分区。')
        if self._room(row, targets, all_items):
            return self._action('transfer', row, target='storage', target_indices=targets,
                                **({'storage_group': material} if material else {}),
                                reason='将奖励存入指定仓库页。')
        if item_kind(row) != 'equipment':
            return self._pause('容量不足：所选'+label+'仓库页没有可用空格或堆叠容量，物品留在背包；请腾出空格或修改存放分区后继续。')
        cleanup = self.settings['cleanup']
        if not mandatory:
            return self._action(equipment_policy(cleanup,row,fallback=True), row,
                reason_code='equipment_fallback', reason='装备专用仓库页已满，按品质配置处置未命中装备。')
        if self._bag_free(observation) == 0:
            disposable = next((r for r in all_items if r.get('container') == 'inventory' and self._disposable(r)), None)
            if disposable:
                return self._action(equipment_policy(cleanup,disposable,fallback=True), disposable,
                    reason_code='equipment_fallback', reason='按品质处理背包垃圾，为保留装备腾出搬运空格。')
            return self._pause('容量不足：背包没有空格，无法取出仓库垃圾；黑雾、神圣和保留装备留在背包。')
        junk = next((r for r in all_items if r.get('container') == 'storage' and r.get('slot_index') in targets and self._disposable(r)), None)
        if junk:
            return self._action('transfer', junk, target='inventory', reason_code='warehouse_eviction',
                                reason='取出所选装备页中的未锁垃圾，随后按配置出售或分解。')
        return self._pause('容量不足：所选装备仓库页已满，且没有可安全清理的未命中装备；保留装备留在背包，请腾出空格后继续。')

    def _background_lock(self, all_items):
        if not self.settings['background']['rule_monitoring']: return None
        for row in all_items:
            if (row.get('container') in ('inventory', 'storage') and item_kind(row) == 'equipment'
                    and self._safe(row) and row.get('matches') is True and row.get('locked') is False):
                return self._action('lock', row, reason='托管监控：锁定背包或仓库中命中规则的装备。')
        return None

    def _background_organize(self, observation, all_items):
        if not self.settings['background']['inventory_automation']: return None
        config = observation.get('automation_settings')
        if not isinstance(config, dict): return None
        bag = [r for r in all_items if r.get('container') == 'inventory' and self._safe(r)]
        def store(row, group):
            targets = self._targets(observation, config[group]['pages'])
            if targets and self._room(row, targets, all_items):
                return self._action('transfer', row, target='storage', target_indices=targets,
                    reason_code='background_organize', automation_group=group, automation_settings=deepcopy(config),
                    reason='托管自动整理：将物品存入自动整理设置中的仓库页。')
        for group, kind in (('gems', 'gem'), ('runes', 'rune')):
            if config[group]['enabled']:
                for row in bag:
                    if item_kind(row) == kind:
                        action = store(row, group)
                        if action: return action
        pressure = config['pressure']; free = self._bag_free(observation)
        capacity = observation['counts']['inventory']['capacity']
        self.pressure_active = (pressure['enabled'] and (self.pressure_active or free <= pressure['threshold'])
                                and free < min(pressure['target_free'], capacity))
        if self.pressure_active:
            for row in bag:
                if (item_kind(row) == 'equipment' and (pressure['equipment_filter'] == 'all'
                        or row['locked'] is (pressure['equipment_filter'] == 'locked'))):
                    action = store(row, 'pressure')
                    if action: return action
        carriage = config['carriage']
        if carriage['enabled'] and free > 0 and observation.get('carriage', {}).get('complete') is True:
            for row in all_items:
                if (row.get('container') == 'carriage' and item_kind(row) in carriage['types']
                        and row.get('collectable') is True and identity(row) and not row.get('issues')):
                    return self._action('collect_carriage', row, reason_code='background_organize',
                        automation_settings=deepcopy(config), reason='托管自动整理：收取所选类型的马车物品。')
        return None

    def _collect_rewards(self, observation, all_items):
        """Fill the bag before processing; only complete reads can prove empty."""
        complete = True
        drops = []
        for source in ('carriage', 'ground'):
            data = observation.get(source, {})
            if data.get('complete') is not True:
                complete = False
                continue
            if data.get('items'):
                source_rows = [r for r in all_items if r.get('container') == source]
                if not source_rows:
                    return self._pause('马车或地面物品身份未确认，已暂停托管。'), False
                drops.extend(source_rows)
        if drops:
            if self._bag_free(observation) == 0:
                return self._pause('背包没有空格，无法继续收取马车或地面物品。'), False
            valid = [r for r in drops if not r.get('issues') and identity(r)]
            if not valid:
                return self._pause('马车或地面物品身份未确认，已暂停托管。'), False
            # Do not fill remaining space with chests ahead of other rewards.
            row = min(valid, key=lambda r: item_kind(r) == 'chest')
            source = row['container']
            return self._action('collect_' + source, row,
                reason='批量收取' + ('马车' if source == 'carriage' else '地面') + '物品，收完或背包满后统一整理。'), False
        if not complete:
            self.finish_empty_reads = 0
            self.status = '等待完整读取马车与地面掉落。'
        return None, complete

    def _return_town(self, observation, all_items=None, **data):
        """Count wins immediately, but collect their settled drops before travel."""
        if observation.get('scene', {}).get('dead') is not True:
            if not self.completion_rewards.ready(observation):
                self.status = self.completion_rewards.reason
                ground = observation.get('ground') or {}
                if ground.get('complete') is not True:
                    return None
                rows = [r for r in (all_items if all_items is not None else observation_items(observation))
                        if r.get('container') == 'ground' and identity(r) and not r.get('issues')]
                # Currency/potions do not consume a bag slot. Chests are picked
                # last and remain unopened until the normal town cleanup.
                free = self._bag_free(observation)
                pickable = [r for r in rows if free > 0 or r.get('item_type') == 1]
                if pickable:
                    row = min(pickable, key=lambda r: (r.get('item_type') != 1, item_kind(r) == 'chest'))
                    return self._action('collect_ground', row, reason_code='completion_rewards',
                        reason='切图前拾取本局地面奖励，确认收到后再继续。')
                if rows and free == 0:
                    action = self._background_organize(observation, all_items or observation_items(observation))
                    if action:
                        return action
                    return self._pause('背包已满，本局地面奖励尚未收完；已暂停且未切图，请腾出空间后继续。')
                if ground.get('items'):
                    self.status = '地面奖励身份待核验，等待重新读取，暂不切图。'
                return None
        if self.completion_rewards.run_id is not None:
            data['protect_completion_rewards'] = True
        return self._action('return_town', **data)

    def merchant_batch_ready(self, observation):
        # Keep player consumables in place. Capacity still comes from ALL
        # occupied slots, not just the equipment eligible for batch screening.
        bag=[r for r in observation['items'] if r.get('container')=='inventory']
        return (self._bag_free(observation)>0 and all(retained_merchant_item(r) or (
                r.get('item_uid') in self.merchant_received_uids and item_kind(r)=='equipment'
                and self._safe(r) and not self._review(r) and self._quality_known(r)) for r in bag)
                and managed_town_rewards_clear(observation))

    def _merchant_purchase(self, observation):
        state=observation['progression'];shop=state['merchant']
        selected={int(k) for k,v in self.settings['merchant']['weights'].items() if v>0}
        missing=sorted(selected-{r['slot'] for r in shop['stock']})
        if missing:
            return self._action('merchant_refresh',class_mask=state['class_mask'],shop_slots=missing,
                reason='商人预缓存库存用完，等待游戏同步补充库存后继续购买。')
        purchase=next_purchase(self.settings['merchant'],state,self.merchant_counts,
                               max_quantity=min(PURCHASE_BATCH_LIMIT,self._bag_free(observation)))
        if not purchase:return None
        if not shop.get('open'):
            return self._action('merchant_open',class_mask=state['class_mask'],reason='奖励整理完成，打开神秘商人。')
        self.phase='shopping';return self._action(**purchase)

    def _cleanup(self, observation, all_items):
        if not self._town(observation):
            return self._return_town(observation, all_items, reason='返回主城后整理奖励。')
        state = observation.get('progression', {})
        if self.merchant_cleanup_pending:
            if state.get('available') is not True or state.get('busy') is True:
                self.status = '等待游戏完成商人同步，购买已确认，不会重复扣款。'; return None
            if not self.finishing and self.merchant_pending and self.merchant_batch_ready(observation):
                action=self._merchant_purchase(observation)
                if action:return action
            if state.get('merchant', {}).get('open'):
                return self._action('merchant_close', class_mask=state['class_mask'], reason='关闭神秘商人后筛选和整理购买的装备。')
            self.merchant_opened = False
            self.merchant_cleanup_pending = False
        settings = self.settings['cleanup']
        bag = [r for r in all_items if r.get('container') == 'inventory']
        if not self.cleanup_processing and self._bag_free(observation) > 0:
            action, sources_empty = self._collect_rewards(observation, all_items)
            if action or not sources_empty:
                return action
        # Finish a full batch before refilling. One sale freeing one slot must
        # not immediately lead to another pickup/sale cycle.
        self.cleanup_processing = True
        if any(managed_required_storage(r) and not self._safe(r) for r in bag):
            return self._pause('必须收纳的物品读取或搬运状态未确认，保留物品并暂停托管。')
        if self.reward_barrier:
            uncertain = [r for r in bag if self._reward_pending(r)]
            if uncertain:
                self.barrier_reads += 1
                self.status = '等待新奖励身份、词条和筛选结论完整读取，暂不出售或分解。'
                if self.barrier_reads >= 5:
                    return self._pause('新奖励仍待核验，托管已暂停；未出售或分解本轮装备。')
                return None
        # Mandatory protection cannot be disabled in settings.
        protected = [r for r in bag if item_kind(r) == 'equipment' and self._safe(r)
                     and (mandatory_storage(r) or r.get('matches') is True
                          or r.get('locked') is True)]
        # Phase 1: finish and observe EVERY required lock before any disposal,
        # including warehouse-capacity fallback disposal. Each verified lock is
        # followed by the state fence before this loop can move to the next item.
        for row in protected:
            if row.get('locked') is False:
                return self._action('lock', row, reason='锁定黑雾、神圣或命中筛选规则的装备。')
        action = self._background_lock(all_items)
        if action: return action
        self.reward_barrier = False; self.barrier_reads = 0
        # Phase 2: only fully screened/locked rewards may now be rearranged.
        for row in bag:
            if identity(row) in self.force_dispose:
                if not self._disposable(row): return self._pause('待清理仓库装备状态发生变化，已停止以保留装备。')
                return self._action(equipment_policy(settings,row,fallback=True), row,
                    reason_code='equipment_fallback', reason='按品质处理从装备专用页取出的未命中装备。')
        action = self._equip(observation, all_items)
        if action: return action
        for row in protected:
            return self._storage_transfer(observation, row, settings['equipment_pages'], all_items, mandatory=True)
        # Free the crafting workspace only after the complete protection and
        # auto-equip stages. Storage materials remain available to the planner
        # until it has exhausted eligible recipes, even if their home is full.
        if synthesis_enabled(self.settings):
            for row in bag:
                if (item_kind(row) == 'equipment' and self._safe(row) and not self._protected(row)
                        and row.get('matches') is False):
                    handling = equipment_policy(settings, row)
                    if row.get('item_uid') in self.merchant_received_uids and handling == 'store':
                        handling = equipment_policy(settings, row, fallback=True)
                    if handling == 'store':
                        return self._storage_transfer(observation, row, settings['equipment_pages'], all_items)
                    return self._action(handling, row, reason='合成前按托管配置整理已筛选的装备。')
            if not self.finishing:
                action = synthesis_action(self.settings, observation)
                if action:
                    if not self.check_identity(observation, require_character=True): return None
                    if state.get('merchant', {}).get('open'):
                        return self._action('merchant_close', class_mask=state['class_mask'],
                            reason='关闭商人后再整理和合成材料。')
                    self.phase = 'synthesizing'
                    return self._action(action['kind'], action.get('item'),
                        **{k: v for k, v in action.items() if k not in ('kind', 'item')})
        for row in bag:
            if item_kind(row) in ('rune', 'gem'):
                return self._storage_transfer(observation, row, settings[item_kind(row)+'_pages'], all_items, mandatory=True)
        for row in bag:
            material = storage_material_kind(row)
            if material:
                if not self._safe(row):
                    return self._pause(STORAGE_MATERIALS[material]+'身份或搬运状态未确认，物品保留，等待重新读取。')
                return self._storage_transfer(observation, row, settings[material+'_pages'], all_items, mandatory=True)
        for row in bag:
            kind = item_kind(row)
            if not self._safe(row): continue
            if kind == 'equipment':
                if self._protected(row): continue
                if row.get('matches') is not False: continue
                handling, pages = equipment_policy(settings,row), settings['equipment_pages']
                if row.get('item_uid') in self.merchant_received_uids and handling=='store':
                    handling=equipment_policy(settings,row,fallback=True)
            else: continue
            if handling == 'store':
                return self._storage_transfer(observation, row, pages, all_items)
            return self._action(handling, row, reason='按托管配置处理奖励。')
        # Finish the current equipment/rune/gem batch before refilling. All
        # outside rewards must be collected before opening bag chests.
        action, sources_empty = self._collect_rewards(observation, all_items)
        if action or not sources_empty:
            self.cleanup_processing = False
            return action
        # Each chest opening starts another collection batch and lock barrier.
        for row in bag:
            if item_kind(row) == 'chest' and self._safe(row) and row.get('locked') is False:
                if self._bag_free(observation) == 0: return self._pause('背包没有空格，暂不能安全打开宝箱。')
                return self._action('open_chest', row, reason='奖励处理完毕，打开宝箱后重新扫描。')
        if self.finishing:
            if self.merchant_opened and observation.get('progression', {}).get('merchant', {}).get('open'):
                return self._action('merchant_close', class_mask=observation['progression']['class_mask'], reason='结束消费，关闭神秘商人。')
            self.finish_empty_reads += 1
            if self.finish_empty_reads < 2:
                self.status = '奖励已整理，等待再次确认马车卸货与地面掉落已清空。'; return None
            self.stop('托管已停止：已回城并完成奖励整理。')
            return None
        state = observation.get('progression', {})
        if growth_enabled(self.settings):
            action, self.growth_wait = growth_action(self.settings, state)
            if action: self.phase = 'growing'; return self._action(**action)
        if self.settings['merchant']['enabled'] and self.merchant_pending:
            # The previous batch has completed all rule/lock/disposal stages.
            if state.get('available') is not True:
                return self._pause(state.get('reason') or '灵魂碎片与商人读取未确认，暂不消费。')
            if state.get('busy') is True:
                self.status = '等待游戏完成在线商人同步，暂不消费。'; return None
            if any(item_kind(r) == 'equipment' and (not self._safe(r) or self._review(r)) for r in bag):
                return self._pause('背包仍有待核验装备，保留当前奖励，暂不继续购买。')
            shop = state['merchant']
            if shop.get('unlock_known') is not True or shop.get('unlocked') is not True:
                return self._pause('神秘商人尚未解锁或解锁状态未确认，暂不购买。')
            if shop.get('available') is not True: return self._pause(shop.get('reason') or '主城神秘商人尚未确认。')
            if not shop.get('stock') and not shop.get('open'):
                return self._action('merchant_open', class_mask=state['class_mask'], reason='奖励整理完成，打开神秘商人读取报价。')
            if self._bag_free(observation)==0:return self._pause('背包没有空格，暂不购买。')
            purchase=self._merchant_purchase(observation)
            if purchase:return purchase
            if self.merchant_opened and shop.get('open'):
                return self._action('merchant_close', class_mask=state['class_mask'], reason='剩余预算不足购买下一件计划商品，关闭神秘商人。')
            self.merchant_pending = False
            self.merchant_last_balance = state['soul_shards']['current']
            self.merchant_last_capacity = state['soul_shards']['maximum']
        if not self.settings['farming']['enabled']:
            self.phase = 'waiting'; self.status = self.growth_wait or '整理和消费完成，继续监控成长。'
            return None
        if self._bag_free(observation) < self.settings['farming']['free_threshold']:
            return self._pause('整理后背包空格仍少于回城门槛，保留装备或材料占用较多，请先处理容量。')
        self.phase = 'travel'
        scene = observation.get('scene',{})
        entry_difficulty = scene.get('mythic_entry_difficulty') if self.target_difficulty==MYTHIC else self.target_difficulty
        if self.target_difficulty==MYTHIC and entry_difficulty not in DIFFICULTIES:
            self.status = '等待读取神话裂隙的解锁难度。'; return None
        if entry_difficulty and (scene.get('world_difficulty') or scene.get('difficulty'))!=entry_difficulty:
            return self._action('switch_difficulty',difficulty=entry_difficulty,
                reason='主城整理完成，切换到'+DIFFICULTY_LABELS[entry_difficulty]+'难度。')
        return self._action('enter_dungeon', dungeon_id=self.target_dungeon,difficulty=self.target_difficulty,
                            reason='奖励整理完成，返回目标关卡。')

    @staticmethod
    def _run_counter(run):
        value = run.get('completion_counter')
        if (isinstance(value, dict) and isinstance(value.get('id'), str) and value['id']
                and type(value.get('count')) is int and 0 <= value['count'] <= 10000000):
            return value
        return None

    def _run_history(self, observation):
        value = observation.get('run_history')
        if (isinstance(value, dict) and self.session is not None and self.character_id is not None
                and value.get('session') == self.session and value.get('character_id') == self.character_id
                and type(value.get('sequence')) is int and value['sequence'] >= 0
                and isinstance(value.get('receipts'), list)):
            return value
        return None

    def _baseline_runs(self, observation):
        """Opening/resuming managed mode must not import earlier game rounds."""
        history = self._run_history(observation)
        self.run_history_sequence = history['sequence'] if history else None
        self.run_counters = {}
        self.run_counter_sources = {}
        self.run_counter_fallbacks = {}
        self.run_entry = None
        scene = observation.get('scene', {})
        run = scene.get('run') if isinstance(scene.get('run'), dict) else {}
        counter = self._run_counter(run)
        if scene.get('available') is not False:
            if ((run.get('retained_terminal') or counter) and run.get('status') != 'failed'
                    and (run.get('completion_confirmed') is True or run.get('status') == 'success')):
                self.handled_runs.add(str(run.get('id')))
            if counter and not run.get('retained_terminal'):
                self.run_counters[counter['id']] = counter['count']
                source = (str(run.get('dungeon_id', run.get('map_id', scene.get('dungeon_id')))),
                          run.get('difficulty') or scene.get('difficulty'))
                self.run_counter_sources[source] = counter['id']

    def expect_run_entry(self, action, observation=None):
        """An accepted map entry resets native totals; native repeat does not."""
        if self.run_entry and self.run_entry['action_id'] == action['action_id']: return
        source = run_record((observation or {}).get('scene', {}).get('run')).get('id')
        self.run_entry = {'action_id': action['action_id'], 'dungeon_id': action['dungeon_id'],
                          'difficulty': action.get('difficulty'),
                          'source_run_id': action.get('source_run_id', source), 'consumed': False,
                          'fallback_wins': set()}

    def _repeat_wins(self, dungeon, difficulty, count):
        self.last_success, self.last_success_difficulty = dungeon, difficulty
        total = self.repeat_completed + count
        quota = self.repeat_maps[self.repeat_index]['runs']
        if total < quota:
            self.repeat_completed = total
            return
        # All receipts here belong to this map. Excess native repeats must not
        # be credited to the other configured map when the target changes.
        self.repeat_generation += 1
        self.repeat_counts[self.repeat_index] = total % quota if len(self.repeat_maps) == 1 else 0
        self.repeat_index = (self.repeat_index+1) % len(self.repeat_maps)
        self.repeat_completed = self.repeat_counts.get(self.repeat_index, 0)
        target = self.repeat_maps[self.repeat_index]
        self.target_dungeon, self.target_difficulty = target['dungeon_id'], target['difficulty']

    def _runs(self, observation):
        scene = observation.get('scene', {})
        run = run_record(scene.get('run'))
        status = run.get('status')
        if status != 'failed' and run.get('completion_confirmed') is True:
            status = 'success'
        run_id = run.get('id')
        dungeon = run.get('dungeon_id', run.get('map_id', scene.get('dungeon_id')))
        difficulty = run.get('difficulty') or scene.get('difficulty') or self.target_difficulty
        # A run active at startup can belong to another map. Do not count it.
        if str(dungeon)!=str(self.target_dungeon) or difficulty!=self.target_difficulty: return
        counter = self._run_counter(run)
        if (self.settings['farming']['mode']=='repeat' and counter and run_id is not None
                and not run.get('retained_terminal')):
            previous = self.run_counters.get(counter['id'])
            entry = self.run_entry
            new_entry = False
            if (entry and not entry['consumed'] and str(entry['dungeon_id']) == str(dungeon)
                    and entry['difficulty'] == difficulty and run_id != entry['source_run_id']):
                # MapData/WaveManager objects can be reused across a town trip.
                # The first new-map sample may already include several wins.
                previous = 0
                entry['consumed'] = True
                new_entry = True
            self.run_counters[counter['id']] = counter['count']
            self.run_counter_sources[(str(dungeon), difficulty)] = counter['id']
            # A new/reset counter has no evidence for older unseen rounds.
            # A confirmed current win is still independently countable.
            count = (counter['count'] - previous if previous is not None and counter['count'] >= previous
                     else int(status == 'success' and str(run_id) not in self.handled_runs
                              and run_id not in self.stargazing_skipped))
            if new_entry:
                # A destination win may have been confirmed before its optional
                # native counter could be read. Keep entry-local fallback wins
                # separate from totals belonging to the previous map visit.
                count = max(0, count - len(entry['fallback_wins']))
                entry['fallback_wins'].clear()
            fallback = self.run_counter_fallbacks.get(counter['id'], set())
            if new_entry or previous is None or counter['count'] < previous:
                fallback.clear()
            else:
                # A successful read may temporarily omit optional counter data.
                # Its RunPlan fallback was already credited; a later native
                # delta includes that same victory and must only add the rest.
                for _ in range(min(count, len(fallback))):
                    fallback.pop()
                    count -= 1
            if count > 0: self._repeat_wins(dungeon, difficulty, count)
            if status == 'success': self.handled_runs.add(str(run_id))
            # Keep a bounded set of native counter identities, independent of
            # the game's short-lived RunPlan IDs and completion popups.
            if len(self.run_counters) > 256:
                expired = next(iter(self.run_counters))
                del self.run_counters[expired]
                self.run_counter_fallbacks.pop(expired, None)
                self.run_counter_sources = {key:value for key,value in self.run_counter_sources.items() if value != expired}
            return
        if run_id in self.stargazing_skipped: return
        if status not in ('success', 'failed') or run_id is None or str(run_id) in self.handled_runs: return
        # Native revival can resume this same run. Death alone must not move
        # the push target backwards or consume this run's terminal receipt.
        if status == 'failed' and scene.get('dead') is True: return
        # Death is reversible through native revival in the same RunPlan.
        # Only successful repeat runs consume their success deduplication key.
        if self.settings['farming']['mode']=='repeat' and status != 'success': return
        self.handled_runs.add(str(run_id))
        if status == 'success':
            self.last_success = dungeon
            self.last_success_difficulty = difficulty
        if self.settings['farming']['mode']=='repeat':
            entry = self.run_entry
            if (entry and not entry['consumed'] and str(entry['dungeon_id']) == str(dungeon)
                    and entry['difficulty'] == difficulty and run_id != entry['source_run_id']):
                entry['fallback_wins'].add(str(run_id))
            else:
                key = self.run_counter_sources.get((str(dungeon), difficulty))
                if key:
                    self.run_counter_fallbacks.setdefault(key, set()).add(str(run_id))
            self._repeat_wins(dungeon, difficulty, 1)
            return
        points = [row if isinstance(row,dict) else {'dungeon_id':row,'difficulty':difficulty}
                  for row in self.settings['farming']['progression']]
        pos = next((i for i,row in enumerate(points) if str(row['dungeon_id'])==str(dungeon) and row['difficulty']==difficulty),None)
        self.push_failed = status=='failed'
        if pos is None:
            self._pause('当前关卡未在已核验的推图路线中，等待重新读取关卡。')
            return
        index = min(pos+1,len(points)-1) if status=='success' else max(0,pos-1)
        self.target_dungeon,self.target_difficulty = points[index]['dungeon_id'],points[index]['difficulty']

    def observe_runs(self, observation):
        """Account verified run evidence independently of inventory/action waits."""
        if not self.running or not self.settings['farming']['enabled']: return
        history = self._run_history(observation)
        if history:
            cursor = self.run_history_sequence
            if cursor is not None and history['sequence'] >= cursor:
                for receipt in history['receipts']:
                    if (not isinstance(receipt, dict) or type(receipt.get('sequence')) is not int
                            or not cursor < receipt['sequence'] <= history['sequence']): continue
                    run = receipt.get('run')
                    if not isinstance(run, dict): continue
                    self._runs({'scene': {'run': run}})
                    if not self.running: break
            self.run_history_sequence = history['sequence']
        if self.running and observation.get('scene', {}).get('available') is not False:
            self._runs(observation)

    def _stargazing(self, observation):
        """Called only for a live target run, after return/cleanup safety gates."""
        if not stargazing_enabled(self.settings): return None
        scene = observation['scene']
        run = run_record(scene.get('run'))
        run_id = run.get('id')
        if not run_id or run_id in self.stargazing_seen: return None
        if run.get('status') not in ('running', 'settling') or run.get('completion_confirmed') is True: return None
        verdict = stargazing_decide(observation.get('loot_plan'), observation, self.settings['farming']['stargazing'])
        self.stargazing_state.update(verdict)
        if verdict['verdict'] == 'unknown': return None
        if verdict['verdict'] == 'keep':
            self.stargazing_seen.add(run_id)
            return None
        # Never abandon generated rewards. Known drops travel back to town and
        # are collected before re-entry; incomplete reads cannot authorize travel.
        if not combat_rewards_known(observation):
            self.stargazing_state.update(verdict='unknown', reason='地面／马车状态待核验，暂不重开。')
            return None
        if scene.get('travel_blocked') is True:
            self.stargazing_state['reason'] = scene.get('travel_wait_reason') or '等待角色恢复可切图状态。'
            return None
        self.phase = 'stargazing'
        alternate = (self.repeat_index + 1) % len(self.repeat_maps)
        target = self.repeat_maps[alternate]
        # The native entry rejects the active map. A different configured map
        # can be tried in combat; difficulty changes always go through town.
        different = str(target['dungeon_id']) != str(scene.get('dungeon_id'))
        if different and target['difficulty'] in DIFFICULTIES and target['difficulty'] == scene.get('difficulty') and combat_rewards_clear(observation):
            return self._action('enter_dungeon', dungeon_id=target['dungeon_id'], difficulty=target['difficulty'],
                source_run_id=run_id, next_repeat_index=alternate, reason_code='stargazing_skip',
                reason='观星未命中高价值奖励，切换到另一张配置关卡重新观星。')
        return self._return_town(observation, source_run_id=run_id, next_repeat_index=alternate, reason_code='stargazing_skip',
                            reason='观星未命中高价值奖励，回城整理后重新进入配置关卡。')

    def stargazing_rejected(self, action):
        """A portal refusal during screen fade/attacks is temporary, not failure."""
        if (not self.pending or self.pending.get('action_id') != action.get('action_id')
                or action.get('reason_code') != 'stargazing_skip' or action['kind'] != 'enter_dungeon'):
            raise ValidationError('观星切图请求状态已变化。')
        self.pending = None
        source = action['source_run_id']
        attempts = self.stargazing_entry_rejections.get(source, 0) + 1
        self.stargazing_entry_rejections[source] = attempts
        if attempts < 8:
            self.phase = 'stargazing'
            self.status = '游戏暂未接受局内切图，等待过场或攻击结束后重新核验。'
            return False
        self.phase = 'returning'
        # Mark only after the return receipt, not on native rejection.
        self.stargazing_return = deepcopy(action)
        self.status = '局内切图连续被拒绝，改为回城整理后重进。'
        return True

    def stargazing_travel_failed(self, action, scene):
        """Keep this run after confirmed cancelled portals, never an unknown write."""
        if action.get('reason_code') != 'stargazing_skip' or self.finishing:
            return False
        self.stargazing_return = None
        run = run_record(scene.get('run'))
        if (run.get('id') == action.get('source_run_id')
                and run.get('status') in ('running', 'settling')
                and run.get('completion_confirmed') is not True):
            self.stargazing_seen.add(run['id'])
            self.stargazing_state.update(verdict='keep', hits=[],
                reason='观星回城多次被打断，本局继续完成，下局再筛选。')
        self.phase = 'farming'
        self.status = '观星跳过未完成，重新读取当前关卡；不再反复回城。'
        return True

    def next_action(self, observation):
        if not self.running: return None
        if not self.check_identity(observation,
                require_character=bool(self.await_effect and self.await_effect.get('kind') in SYNTHESIS_ACTIONS)):
            return None
        try:
            self.observe_runs(observation)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            return self._pause(str(exc))
        if not self.running: return None
        if self.pending is not None: return None
        self.completion_rewards.observe(observation)
        if observation.get('complete') is not True:
            self.status = '等待完整背包与仓库读取。'; return None
        self.merchant_received_uids.update(observation.get('merchant_uids',[]))
        try:
            all_items = observation_items(observation)
            if not self._effect_visible(observation, all_items): return None
            self.metrics.observe(observation)
            scene = observation.get('scene', {})
            if scene.get('scene_changing') is True or scene.get('teleporting') is True:
                self.status = '等待游戏完成场景切换，不重复请求。'; return None
            if scene.get('online_saving') is True:
                self.status = '等待游戏完成在线同步。'; return None
            if (self.settings['farming']['enabled'] and scene.get('available') is True
                    and scene.get('dead') is True and not self._town(observation)):
                # Death recovery must not depend on a still-readable wave plan,
                # merchant data, or growth data while the character is dead.
                self.phase = 'farming'
                self.status = '角色已死亡，等待游戏原生复活后重新判断关卡。'
                return None
            if growth_enabled(self.settings) and observation.get('progression', {}).get('busy') is True:
                self.status = '等待游戏完成在线同步，已确认的动作不会重复执行。'; return None
            if self.finishing:
                scene = observation.get('scene', {})
                if scene.get('available') is not True or scene.get('scene_changing') is True:
                    self.status = '正在结束托管，等待场景稳定后回城整理。'; return None
                self.phase = 'cleanup' if self._town(observation) else 'returning'
                return self._cleanup(observation, all_items)
            if self.settings['merchant']['enabled']:
                state = observation.get('progression', {})
                if state.get('available') is not True:
                    self.status = state.get('reason') or '等待读取灵魂碎片容量。'; return None
                shards = state['soul_shards']
                trigger = shards['current'] * 100 >= shards['maximum'] * self.settings['merchant']['trigger_percent']
                changed = (self.merchant_last_balance is None or shards['current'] > self.merchant_last_balance
                           or shards['maximum'] != self.merchant_last_capacity)
                if trigger and changed: self.merchant_pending = True
                if self.merchant_pending:
                    self.phase = 'cleanup' if self._town(observation) else 'returning'
                    return self._cleanup(observation, all_items)
            if self.settings['farming']['enabled']:
                scene = observation.get('scene', {})
                if scene.get('available') is False:
                    self.status = '等待场景状态读取。'; return None
                if self._town(observation): self.phase = 'cleanup'
                if self.phase == 'cleanup': return self._cleanup(observation, all_items)
                if self.stargazing_return:
                    run = run_record(scene.get('run'))
                    if (self.stargazing_return['source_run_id'] != run.get('id')
                            or run.get('status') not in ('running', 'settling')
                            or run.get('completion_confirmed') is True):
                        # A win during the wait is already counted above. It
                        # must not be marked as a skipped run by a late return.
                        self.stargazing_return = None
                    else:
                        verdict = stargazing_decide(observation.get('loot_plan'), observation, self.settings['farming']['stargazing'])
                        self.stargazing_state.update(verdict)
                        if verdict['verdict'] == 'keep':
                            self.stargazing_return = None
                            self.stargazing_seen.add(run['id'])
                        elif verdict['verdict'] == 'unknown' or not combat_rewards_known(observation):
                            self.status = '重新核验本局计划与实际奖励，暂不重开。'
                            return None
                        elif scene.get('travel_blocked') is True:
                            self.status = scene.get('travel_wait_reason') or '等待角色恢复可切图状态。'
                            return None
                        else:
                            self.phase = 'returning'
                            return self._return_town(observation, all_items, source_run_id=self.stargazing_return['source_run_id'],
                                next_repeat_index=self.stargazing_return['next_repeat_index'],
                                reason_code='stargazing_skip', reason='局内切图连续被拒绝，回城收取并整理奖励后重新进入配置关卡。')
                if self.target_difficulty and scene.get('difficulty')!=self.target_difficulty:
                    self.phase = 'returning'
                    return self._return_town(observation, all_items, reason='切换目标难度前，先回到主城整理。')
                if self._bag_free(observation) < self.settings['farming']['free_threshold']:
                    self.phase = 'returning'
                    return self._return_town(observation, all_items, reason='背包剩余空格低于门槛，自动回城整理。')
                if run_record(scene.get('run')).get('status') in ('success', 'failed'):
                    # TownPortalSystem.fri rejects the already active combat map.
                    # Finish every run through the original return/cleanup/entry
                    # path, including repeats of the same map after a failure.
                    self.phase = 'returning'
                    return self._return_town(observation, all_items, reason='本轮结束，回城整理后复刷或推进关卡。')
                if scene.get('dungeon_id') is not None and str(scene['dungeon_id']) != str(self.target_dungeon):
                    self.phase = 'returning'
                    return self._return_town(observation, all_items, reason='当前关卡与托管目标不同，回城整理后切换关卡。')
                action = self._stargazing(observation)
                if action: return action
            if self._town(observation) and any(r.get('container')=='inventory'
                    and r.get('item_uid') in self.merchant_received_uids for r in all_items):
                self.phase='cleanup';return self._cleanup(observation,all_items)
            if self._town(observation) and synthesis_enabled(self.settings):
                self.phase = 'cleanup'
                return self._cleanup(observation, all_items)
            action, self.growth_wait = growth_action(self.settings, observation.get('progression', {}))
            if action: self.phase = 'growing'; return self._action(**action)
            action = self._background_lock(all_items)
            if action: return action
            action = self._equip(observation, all_items)
            if action: return action
            action = self._background_organize(observation, all_items)
            if action: return action
            self.status = self.growth_wait or (self.stargazing_state['reason'] if stargazing_enabled(self.settings)
                and self.stargazing_state['reason'] else '正在刷图，等待奖励或更好的装备。' if self.settings['farming']['enabled'] else '正在监控装备与成长目标。')
            return None
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            return self._pause('托管读取未确认，已暂停：' + str(exc))
