"""Build-gated synthesis through the game's original crafting and cube methods.

Snapshots are read-only. Mutations remain separately unverified capabilities.
The gem remover ignores locks and has no slot argument, so any locked/unknown
matching stack blocks the entire recipe. Rune input is always nine fresh UIDs.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import struct
import time

from .paths import RESOURCE_ROOT
from .native_memory import MemoryReadError, SnapshotChangedError, GameBusyError
from .native_dispatcher import Argument, Invocation, NativeActionError
from .progression_runtime import _GrowthCopy
from .recommendation_reader import decode_obscured_int
from .synthesis import RUNE_QUALITIES, item_key, rune_quality


@lru_cache(maxsize=1)
def gem_recipes():
    return json.loads((RESOURCE_ROOT / 'data/synthesis-catalog.json').read_text(encoding='utf-8'))['gems']


def _quality(value):
    key = str(value).replace('_', '').replace(' ', '').casefold()
    return {'1': 'uncommon', 'uncommon': 'uncommon', '2': 'rare', 'rare': 'rare',
            '3': 'grand', 'legendary': 'grand', 'grand': 'grand',
            '5': 'set', 'runeset': 'set', 'set': 'set'}.get(key)


def _total(rows, key):
    return sum(row['count'] for row in rows if item_key(row) == key)


def gem_inputs(rows, recipe, count):
    """Pure native-removal policy, including fdu's reverse-slot traversal."""
    if type(count) is not int or not 1 <= count <= 100000:
        raise NativeActionError('宝石合成次数无效。')
    matching = [row for row in rows if item_key(row) == recipe['input_key']]
    if any(row.get('item_class') != 'GemData' or row.get('locked') is not False
           or row.get('slot_locked') is not False or row.get('issues')
           or type(row.get('count')) is not int or row['count'] < 1 for row in matching):
        raise NativeActionError('同种宝石包含锁定或未确认的背包堆叠，原生扣料不能指定堆叠，已跳过。')
    remaining, selected = count * 10, []
    for row in sorted(matching, key=lambda r: r['slot_index'], reverse=True):
        take = min(remaining, row['count'])
        if take:
            selected.append(dict(deepcopy(row), quantity=take)); remaining -= take
    if remaining:
        raise NativeActionError('背包中的未锁定宝石数量不足，重新规划。')
    return selected


def rune_inputs(rows, uids, recipe, maximum, excluded, equipped=()):
    """Validate explicit identities; trained runes are permitted by policy."""
    if (not isinstance(uids, list) or len(uids) != 9 or len(set(uids)) != 9
            or any(not isinstance(uid, str) or not uid for uid in uids)):
        raise NativeActionError('符文合成必须指定九枚不同的物品 UID。')
    if (maximum not in RUNE_QUALITIES or recipe['input_quality'] not in RUNE_QUALITIES
            or recipe['output_quality'] not in RUNE_QUALITIES
            or not RUNE_QUALITIES.index(recipe['input_quality']) < RUNE_QUALITIES.index(recipe['output_quality'])
            <= RUNE_QUALITIES.index(maximum)):
        raise NativeActionError('符文配方超过最高合成品质，或属于不允许的同品质重铸。')
    found = {}
    for row in rows:
        uid = row.get('item_uid')
        if uid in uids:
            if uid in found:
                raise NativeActionError('背包符文 UID 重复，等待重新读取。')
            found[uid] = row
    if set(found) != set(uids):
        raise NativeActionError('合成符文已移动或变化，请重新规划。')
    result = []
    for uid in uids:
        row = found[uid]
        if (row.get('container') != 'inventory' or row.get('item_class') != 'RuneData'
                or row.get('count') != 1 or row.get('locked') is not False
                or row.get('slot_locked') is not False or row.get('issues')
                or row.get('identity_verified') is False or uid in equipped
                or item_key(row) in excluded or rune_quality(row) != recipe['input_quality']):
            raise NativeActionError('符文锁定、穿戴、跳过名单或品质已变化，已取消合成。')
        result.append(dict(deepcopy(row), quantity=1))
    return result


class SynthesisReceiptPending(NativeActionError):
    """The native batch finished, but its economic receipt is still pending."""


class SynthesisRuntime:
    def __init__(self, actions):
        self.actions, self.reader = actions, actions.reader
        self._recipes = {}
        self._scan_key = None
        self._scanned = set()
        self._scan_positions = {}
        self._uncertain = False
        self._pending = None
        self._cleanup_pending = None

    def _copy(self):
        result = _GrowthCopy(self.reader)
        result.types.update({name: dict(row, name=row.get('name', name))
                             for name, row in self.actions.profile['types'].items()})
        return result

    def _singleton(self, copy, name):
        return copy.singleton(self.actions.profile['singletons'][name], name)

    def _context(self, *, after=False):
        read, manager, area, scene = self.actions._context(allow_busy=after)
        if scene != 1:
            raise NativeActionError('自动合成仅在主城整理时执行。')
        portal = read.singleton('TownPortalSystem')
        if read.boolean(portal + 76) or read.boolean(area + 176):
            raise GameBusyError('等待场景切换结束后合成。')
        save = read.singleton('SaveSystem')
        if not after:
            if read.boolean(save + 192):
                raise GameBusyError('等待游戏物品操作结束后合成。')
            self.actions._require_travel_ready(read)
        return read, manager, save

    def _gold(self, copy):
        data = self._singleton(copy, 'PlayerData')
        gold = decode_obscured_int(copy.read(data + 48, 16))
        if gold < 0:
            raise MemoryReadError('合成金币读取无效。')
        return gold

    def _gems(self, copy, save):
        # Native add uses the output ItemData's MaxStack. A free slot alone is
        # not sufficient for an arbitrarily large multi-stack crafting batch.
        # Read the same immutable assets already indexed by the save system;
        # this must never open the crafting UI or locate recipes by mutation.
        items = copy.dictionary(copy.ptr(save + 112), 'pointer', 'object')
        item_map = {self.reader.string(key): value for key, value in items.items()}
        recipes = deepcopy(gem_recipes())
        for recipe in recipes:
            asset = copy.verify(item_map.get(recipe['output_key']), 'GemData')
            if self.reader.asset_name(asset) != recipe['output_key']:
                raise MemoryReadError('宝石产物资源身份未确认。')
            maximum = copy.integer(asset + 152)
            if type(maximum) is not int or not 1 <= maximum <= 2**31-1:
                raise MemoryReadError('宝石产物堆叠上限读取无效。')
            recipe['output_max_stack'] = maximum
        return {'available': True, 'recipes': recipes, 'gold': self._gold(copy)}

    def _runes(self, copy, manager, save, *, include_objects=False):
        config = copy.verify(copy.ptr(manager + 32), 'GameConfig')
        mode = copy.integer(save + 32)
        entries, sequence, version = [], 0, 'local'
        if mode == 1:
            cloud = self._singleton(copy, 'CloudClient')
            state = copy.verify(copy.ptr(cloud + 448), 'CubeState')
            version = self.reader.string(copy.ptr(state + 16))
            sequence = copy.integer(state + 24)
            if not version or sequence < 0:
                raise MemoryReadError('在线魔盒规则尚未同步完整，暂不合成。')
            for raw in copy.array(copy.ptr(state + 32), 8, 'CubeRecipe', limit=32):
                obj = copy.verify(struct.unpack('<Q', raw)[0], 'CubeRecipe')
                source_name = self.reader.string(copy.ptr(obj + 16))
                target_name = self.reader.string(copy.ptr(obj + 24))
                # 1.1.0 adds equipment recipes to this same array. They are
                # deliberately outside automatic rune synthesis.
                if source_name in ('BlackMist', 'Divine') and target_name in ('BlackMist', 'Divine'):
                    continue
                source, target = _quality(source_name), _quality(target_name)
                chance = copy.number(obj + 32)
                entries.append((source, target, chance, copy.integer(obj + 36), copy.integer(obj + 40), obj))
        elif mode == 0:
            for raw in copy.array(copy.ptr(config + 40), 8, 'CubeRecipeConfig', limit=32):
                obj = copy.verify(struct.unpack('<Q', raw)[0], 'CubeRecipeConfig')
                source_kind, target_kind = copy.integer(obj + 16), copy.integer(obj + 24)
                if source_kind in (4, 5) and target_kind in (4, 5):
                    continue
                qualities = {0: 'uncommon', 1: 'rare', 2: 'grand', 3: 'set'}
                entries.append((qualities.get(source_kind), qualities.get(target_kind),
                                copy.number(obj + 28), copy.integer(obj + 20), copy.integer(obj + 32), None))
        else:
            raise MemoryReadError('当前角色模式不支持合成。')
        if not entries:
            raise MemoryReadError('符文魔盒配方尚未加载。')
        difficulty_type = copy.klass(copy.ptr(self.reader.module['base'] + self.actions.profile['statics']['ey']), 'ey')
        highest = copy.integer(copy.ptr(difficulty_type + 184) + 4)
        loot = copy.verify(copy.ptr(config + 80), 'LootConfig')
        threshold = copy.integer(loot + 160)
        if highest not in (0, 1, 2) or threshold not in (0, 1, 2):
            raise MemoryReadError('符文合成解锁难度读取无效。')
        recipes, objects = [], {}
        for source, target, chance, amount, cost, obj in entries:
            if (source not in RUNE_QUALITIES or target not in RUNE_QUALITIES
                    or not 0 <= chance <= 1 or amount != 9 or not 0 <= cost <= 2**31-1):
                raise MemoryReadError('符文在线配方存在未适配的品质或概率。')
            if RUNE_QUALITIES.index(target) <= RUNE_QUALITIES.index(source):
                continue  # Never expose the set-to-set reroll to auto synthesis.
            if RUNE_QUALITIES.index(target) != RUNE_QUALITIES.index(source) + 1 or not chance:
                raise MemoryReadError('符文升级配方与已核验的品质阶梯不一致。')
            recipes.append({'recipe_id': f'cube:{source}:{target}', 'input_quality': source,
                            'output_quality': target, 'input_count': 9, 'output_count': 1,
                            'gold_cost': cost, 'chance': chance,
                            # 1.1.0 checks the required difficulty of both the
                            # input and output kind. Among rune qualities only
                            # RuneSet is gated (LootConfig.MinSetDifficulty).
                            'available': 'set' not in (source, target) or highest >= threshold})
            objects[recipes[-1]['recipe_id']] = obj
        if len({r['recipe_id'] for r in recipes}) != len(recipes):
            raise MemoryReadError('符文在线配方重复，暂不合成。')
        rune_manager = self._singleton(copy, 'RuneManager')
        equipped = [self.reader.string(struct.unpack('<Q', raw)[0])
                    for raw in copy.array(copy.ptr(rune_manager + 56), 8, 'String', limit=16)]
        if any(not isinstance(uid, str) for uid in equipped):
            raise MemoryReadError('已穿戴符文 UID 尚未完整读取。')
        result = {'available': True, 'recipes': recipes, 'sequence': sequence, 'unlocked': True,
                'rules_version': version, 'online': mode == 1,
                'gold': self._gold(copy),
                'equipped_uids': [uid for uid in equipped if uid]}
        if include_objects:
            result['_recipe_objects'] = objects
        return result

    def snapshot(self, *, gems=True, runes=True):
        result = {}
        # Each group is isolated: an unavailable CubeState cannot hide gems.
        for group, requested in (('gems', gems), ('runes', runes)):
            if not requested:
                continue
            if self._uncertain or self._pending or self._cleanup_pending:
                result[group] = {'available': False, 'recipes': [],
                                 'reason': '上一轮合成或界面清理仍待核验，禁止重复消耗材料。'}
                continue
            try:
                read, manager, _, _ = self.actions._context(allow_busy=True)
                copy = self._copy()
                save = self._singleton(copy, 'SaveSystem')
                state = (self._gems(copy, save)
                         if group == 'gems' else self._runes(copy, manager, save))
                read.finish(); copy.finish()
                state['native_verified'] = ('synthesize_' + group) in self.actions.verified_kinds
                result[group] = state
            except (MemoryReadError, ValueError, KeyError, struct.error, UnicodeError) as exc:
                result[group] = {'available': False, 'recipes': [], 'reason': str(exc)}
        return result

    def _bag(self, read):
        reader = self.reader
        previous, reader.snapshot_stamps = reader.snapshot_stamps, []
        try:
            reader.item_cache = {}
            registry, stamp = reader.registry()
            bag = reader.container('Inventory', registry, 2048)
            if bag.get('complete') is not True:
                raise MemoryReadError('背包材料尚未完整读取，暂不合成。')
            for row in bag['slots']:
                if row.get('item_class') in ('GemData', 'RuneData'):
                    row['kind'] = 'gem' if row['item_class'] == 'GemData' else 'rune'
                if row.get('item_uid'):
                    row['identity_verified'] = row['item_uid'] in registry
            stamps = list(reader.snapshot_stamps)
            if any(reader.memory.read(a, len(raw)) != raw for a, raw in stamps):
                raise SnapshotChangedError('背包材料读取期间发生变化。')
            # Registry header/version guards protect generated UID identity;
            # its potentially huge entry block is only copied/checked above.
            read.guards.append((stamp['object'] + 24, stamp['header']))
            inventory = read.singleton('Inventory')
            array = read.ptr(inventory + 56)
            read.read(array + 24, 8)
            return bag['slots'], registry, array, bag['slot_count']
        finally:
            reader.snapshot_stamps = previous

    def _guard_inputs(self, read, inputs, registry, array):
        for row in inputs:
            slot = read.ptr(array + 32 + row['slot_index'] * 8)
            read.verify(slot, 'InventorySlot')
            raw = read.read(slot + 16, 32)
            obj, amount = struct.unpack_from('<Qi', raw)
            uid = self.reader.string(struct.unpack_from('<Q', raw, 16)[0])
            if (item_key(row) != self.reader.asset_name(obj) or amount != row['count']
                    or uid != row.get('item_uid') or bool(raw[24]) is not False):
                raise SnapshotChangedError('合成材料槽位已变化。')
            if uid:
                generated = read.verify(registry.get(uid), 'GeneratedItemData')
                if read.integer(generated + 20) != row.get('rarity') or read.boolean(generated + 73):
                    raise SnapshotChangedError('合成材料品质或锁定已变化。')

    def _guard_bag_identities(self, read, array, capacity):
        # An initially empty slot could acquire a locked matching gem between
        # planning and invocation. Guard every slot, not only selected stacks.
        for index in range(capacity):
            slot = read.ptr(array + 32 + index * 8)
            if slot:
                read.read(slot + 16, 32)

    def _recipe(self, read, pointer, expected):
        read.verify(pointer, 'RecipeData')
        if self.reader.asset_name(pointer) != expected['recipe_id']:
            raise MemoryReadError('宝石配方名称不一致。')
        ingredients = read.pointers(read.ptr(pointer + 24), 'CraftingIngredient', limit=8)
        if len(ingredients) != 1:
            raise MemoryReadError('宝石配方材料数量不一致。')
        ingredient = read.verify(ingredients[0], 'CraftingIngredient')
        source = read.verify(read.ptr(ingredient + 16), 'GemData')
        target = read.verify(read.ptr(pointer + 32), 'GemData')
        if (self.reader.asset_name(source) != expected['input_key']
                or self.reader.asset_name(target) != expected['output_key']
                or read.integer(ingredient + 24) != 10 or read.integer(pointer + 40) != 1
                or read.integer(pointer + 44) != expected['gold_cost'] or read.boolean(pointer + 48)):
            raise MemoryReadError('实时宝石合成配方与已核验资源不一致。')
        return pointer

    def _find_recipe(self, read, manager, expected, cancel_event, validate):
        key = expected['recipe_id']
        if key in self._recipes:
            mark = len(read.guards)
            try:
                return self._recipe(read, self._recipes[key], expected)
            except MemoryReadError:
                del read.guards[mark:]; del self._recipes[key]
        # Prefer the live crafting book, if the player has already opened it.
        mark = len(read.guards)
        try:
            ui = read.singleton('CraftingPanelUI')
            book = read.verify(read.ptr(ui + 192), 'RecipeBook')
            for pointer in read.pointers(read.ptr(book + 24), 'RecipeData', limit=512):
                if self.reader.asset_name(pointer) == key:
                    self._recipes[key] = self._recipe(read, pointer, expected)
                    return pointer
        except MemoryReadError:
            pass
        del read.guards[mark:]
        # RecipeData is not in SaveSystem's ItemData dictionary. Locate the
        # original loaded Unity asset by its verified class, never fabricate it.
        config = read.verify(read.ptr(manager + 32), 'GameConfig')
        anchor = read.verify(read.ptr(config + 112), 'RecipeData')
        klass = read.ptr(anchor)
        signature = (klass, anchor)
        if signature != self._scan_key:
            self._scan_key = signature; self._scanned.clear(); self._scan_positions.clear()
        deadline, budget = time.monotonic() + 2, 128 * 1024 * 1024
        needle, chunk = struct.pack('<Q', klass), 1024 * 1024
        known = {row['recipe_id']: row for row in gem_recipes()}
        regions = sorted((r for r in self.reader.memory.regions() if r.kind == 0x20000),
                         key=lambda r: (not r.base <= anchor < r.base + r.size, r.size > 16 * chunk,
                                        abs(r.base - anchor)))
        for region in regions:
            for offset in range(0, region.size, chunk):
                if (cancel_event is not None and cancel_event.is_set()) or not validate():
                    raise NativeActionError('宝石配方定位已取消。')
                if time.monotonic() >= deadline or budget <= 0:
                    raise GameBusyError('正在只读定位原生宝石配方，稍后自动继续；尚未扣料。')
                address, size = region.base + offset, min(chunk, region.size - offset)
                if (address, size) in self._scanned:
                    continue
                budget -= size
                try:
                    block = self.reader.memory.read(address, size)
                except MemoryReadError:
                    self._scanned.add((address, size)); continue
                pos = self._scan_positions.get((address, size), -1)
                while True:
                    if time.monotonic() >= deadline:
                        raise GameBusyError('正在只读定位原生宝石配方，稍后自动继续；尚未扣料。')
                    pos = block.find(needle, pos + 1)
                    if pos < 0:
                        break
                    self._scan_positions[address, size] = pos
                    pointer = address + pos
                    if pointer % 8:
                        continue
                    mark = len(read.guards)
                    try:
                        candidate = known.get(self.reader.asset_name(pointer))
                        if candidate is not None:
                            self._recipe(read, pointer, candidate)
                            self._recipes[candidate['recipe_id']] = pointer
                            if candidate['recipe_id'] == key:
                                return pointer
                        del read.guards[mark:]
                    except (MemoryReadError, UnicodeError, struct.error):
                        del read.guards[mark:]
                self._scanned.add((address, size)); self._scan_positions.pop((address, size), None)
        self._scanned.clear(); self._scan_positions.clear()
        raise GameBusyError('原生宝石配方资源暂未加载，稍后重试；尚未扣料。')

    def _invoke(self, calls, read, copy, *, cancel_event=None, validate=lambda: True):
        read.finish(); copy.finish()
        guards = list(dict.fromkeys(read.guards + copy.action_guards))
        return self.actions.dispatcher.invoke(calls, guards, cancel_event=cancel_event,
                                               timeout=5, validate=validate)

    def _cube_selections(self, read, ui):
        slots = read.pointers(read.ptr(ui + 64), 'CubeSlotUI', limit=16)
        if len(slots) != 9:
            raise MemoryReadError('魔盒材料槽位数量不一致。')
        selections = []
        for slot in slots:
            read.verify(slot, 'CubeSlotUI')
            obj = read.ptr(slot + 64)
            uid = self.reader.string(read.ptr(slot + 72))
            if obj or uid:
                selections.append((obj, uid))
        return slots, selections

    def execute(self, kind, payload, *, cancel_event=None, validate=lambda: True):
        if self._uncertain:
            raise NativeActionError('上一轮合成结果仍未确认，禁止重复消耗材料；请先人工核验。', uncertain=True)
        if self._pending or self._cleanup_pending:
            raise GameBusyError('上一轮合成仍在核验或清理，仅允许继续核验原操作。')
        if kind not in ('synthesize_gems', 'synthesize_runes'):
            raise NativeActionError('未知合成动作。')
        read, manager, save = self._context()
        copy = self._copy()
        rows, registry, array, capacity = self._bag(read)
        self._guard_bag_identities(read, array, capacity)
        group = 'gems' if kind == 'synthesize_gems' else 'runes'
        ui, opened = None, False
        if group == 'gems':
            recipe = next((r for r in gem_recipes() if r['recipe_id'] == payload.get('recipe_id')), None)
            maximum, count = payload.get('max_quality'), payload.get('count')
            if recipe is None or type(maximum) is not int or not 1 <= recipe['output_quality'] <= maximum <= 6:
                raise NativeActionError('宝石配方或最高合成品质已经变化。')
            selected = gem_inputs(rows, recipe, count)
            gold = self._gold(copy)
            if gold < recipe['gold_cost'] * count or len(rows) >= capacity:
                raise NativeActionError('宝石合成金币或产物空格不足，重新规划。')
            pointer = self._find_recipe(read, manager, recipe, cancel_event, validate)
            output_asset = read.ptr(pointer + 32)
            maximum_stack = read.integer(output_asset + 152)
            room = ((capacity - len(rows)) * maximum_stack
                    + sum(maximum_stack - r['count'] for r in rows if item_key(r) == recipe['output_key']))
            if maximum_stack < 1 or count > room:
                raise NativeActionError('宝石产物堆叠空间不足，重新规划较小批次。')
            # Guard every matching stack: native aggregate removal ignores locks.
            self._guard_inputs(read, [r for r in rows if item_key(r) == recipe['input_key']], registry, array)
            calls = [Invocation('et.dbi', arguments=(Argument.reference(pointer),), require_true=True),
                     Invocation('et.dbn', arguments=(Argument.reference(pointer), Argument.integer(count)), require_true=True)]
            before_state = {'gold': gold}
        else:
            state = dict(self._runes(copy, manager, save, include_objects=True))
            recipe_objects = state.pop('_recipe_objects')
            recipe = next((r for r in state['recipes'] if r['recipe_id'] == payload.get('recipe_id')), None)
            if recipe is None or recipe.get('available') is not True or payload.get('count') != 1:
                raise NativeActionError('符文配方尚未解锁或已变化。')
            if state['gold'] < recipe['gold_cost']:
                raise NativeActionError('符文合成金币不足，结束本轮符文合成。')
            selected = rune_inputs(rows, payload.get('input_uids'), recipe, payload.get('max_quality'),
                                   set(payload.get('excluded_keys', [])), state['equipped_uids'])
            self._guard_inputs(read, selected, registry, array)
            if read.ptr(manager + 120) or read.boolean(manager + 276):
                raise GameBusyError('等待上一局奖励同步完成后合成符文。')
            if state['online']:
                cloud = self._singleton(copy, 'CloudClient')
                if copy.boolean(cloud + 289) or copy.boolean(cloud + 290):
                    raise GameBusyError('等待在线角色请求结束后合成符文。')
            ui = read.singleton('CubePanelUI')
            opened = not read.boolean(ui + 40)
            if read.boolean(ui + 252):
                raise GameBusyError('符文魔盒正在处理上一轮操作。')
            slots, selections = self._cube_selections(read, ui)
            if selections:
                live_uids = {r.get('item_uid') for r in rows if r.get('item_uid')}
                # Older versions ended the animation without clearing consumed
                # slot references. A closed, idle cube can safely be reopened
                # only when ALL references identify runes absent from both the
                # complete bag and generated-item registry. Open's native reset
                # clears those references before our new selections are placed.
                if not opened or any(not obj or not uid or uid in registry or uid in live_uids
                                     for obj, uid in selections):
                    raise GameBusyError('魔盒中已有玩家选择的材料，请先完成或清空该操作。')
                for obj, _ in selections:
                    read.verify(obj, 'RuneData')
            calls = [Invocation('SaveSystem.ekf', instance=save, require_true=True)]
            if state['online']:
                # The new preflight is static and accepts the live CubeRecipe,
                # not a SaveSystem instance and ItemRarity enum.
                calls.append(Invocation('SaveSystem.eke',
                    arguments=(Argument.reference(recipe_objects[recipe['recipe_id']]),), require_true=True))
            if opened:
                calls.append(Invocation('CubePanelUI.Open', instance=ui))
            calls.append(Invocation('CubePanelUI.gba', instance=ui, require_true=True))
            calls.extend(Invocation('CubePanelUI.gbh', instance=ui, arguments=(Argument.reference(slot),
                         Argument.integer(row['slot_index']), Argument.boolean(False)))
                         for slot, row in zip(slots, selected))
            calls.append(Invocation('CubePanelUI.gbp', instance=ui))
            before_state = state
        # The caller's original plan cannot silently select a different set.
        identities = lambda entries: [(r.get('slot_index'), r.get('item_uid'), item_key(r), r.get('quantity')) for r in entries]
        if identities(selected) != identities(payload.get('inputs', [])):
            raise SnapshotChangedError('合成材料与规划不一致，重新读取后再执行。')
        try:
            result = self._invoke(calls, read, copy, cancel_event=cancel_event, validate=validate)
        except NativeActionError as exc:
            if exc.uncertain:
                self._uncertain = True
            raise
        # Only a completed mailbox batch can enter receipt recovery. A native
        # timeout/partial batch remains fenced above and is never resubmitted.
        self._pending = {'kind': kind, 'before': deepcopy(rows), 'selected': selected,
            'recipe': recipe, 'count': payload['count'], 'state': before_state,
            'invocation': result, 'started': time.monotonic(),
            'character_id': payload.get('character_id'), 'ui': ui, 'opened': opened}
        try:
            receipt = self._confirm(kind, rows, selected, recipe, payload['count'], before_state, result)
        except SynthesisReceiptPending:
            return self._pending_result()
        except NativeActionError as exc:
            if exc.uncertain:
                self._uncertain = True
            raise
        return self._confirmed(receipt, cancel_event=cancel_event, validate=validate)

    def _pending_result(self):
        elapsed = time.monotonic() - self._pending['started']
        return {'kind': self._pending['kind'], 'effect_confirmed': False,
                'synthesis_pending': True, 'timed_out': elapsed >= 120,
                'reason': '合成已提交，等待材料与产物回执；未重复消耗材料。'}

    def _confirmed(self, receipt, *, cancel_event=None, validate=lambda: True):
        pending = self._pending
        result = dict(pending['invocation'], **receipt, kind=pending['kind'],
                      effect_confirmed=True, online_save_confirmed=False, native_verified=False)
        self._pending = None
        self._uncertain = False
        if pending['ui'] is not None:
            self._cleanup_pending = {'ui': pending['ui'], 'opened': pending['opened'],
                'input_uids': {r['item_uid'] for r in pending['selected']},
                'character_id': pending['character_id']}
            self._recover_cleanup(cancel_event=cancel_event, validate=validate)
        result['ui_cleanup_pending'] = self._cleanup_pending is not None
        return result

    def reconcile(self, character_id, *, cancel_event=None, validate=lambda: True):
        """Poll the original receipt; never select materials or submit a craft."""
        pending = self._pending or self._cleanup_pending
        if pending is None:
            return None
        if pending.get('character_id') is not None and character_id != pending['character_id']:
            raise NativeActionError('合成核验期间角色已变化，未继续清理或消耗材料。', uncertain=True)
        if self._uncertain:
            raise NativeActionError('原生合成执行状态不明确，不能自动恢复。', uncertain=True)
        if self._pending:
            try:
                receipt = self._receipt(pending['kind'], pending['before'], pending['selected'],
                    pending['recipe'], pending['count'], pending['state'], pending['invocation'])
            except (MemoryReadError, ValueError, KeyError, struct.error, UnicodeError):
                receipt = None
            if receipt is not None:
                return self._confirmed(receipt, cancel_event=cancel_event, validate=validate)
            return self._pending_result()
        self._recover_cleanup(cancel_event=cancel_event, validate=validate)
        return {'ui_cleanup_pending': self._cleanup_pending is not None}

    def _recover_cleanup(self, *, cancel_event=None, validate=lambda: True):
        pending = self._cleanup_pending
        if pending is None or not validate() or cancel_event is not None and cancel_event.is_set():
            return
        if self._cleanup_cube(pending['ui'], pending['opened'], input_uids=pending['input_uids'],
                              cancel_event=cancel_event, validate=validate):
            self._cleanup_pending = None

    def _cleanup_cube(self, ui, opened, *, input_uids, cancel_event=None, validate=lambda: True):
        """Clear only assistant selections; close only assistant-opened panel.

        gbu ends the result animation; gbf resets the slot references through
        CubeSlotUI.gcl. Neither operation crafts or changes the material count.
        Confirm the resulting UI state independently of the economic receipt.
        """
        try:
            # Inspect again after invocation: a completed mailbox is not proof
            # that the animation, selection slots and panel are actually clean.
            for attempt in range(2):
                read, _, _ = self._context(after=True)
                if read.singleton('CubePanelUI') != ui:
                    read.finish()
                    return True
                animating = read.boolean(ui + 252)
                _, selections = self._cube_selections(read, ui)
                if any(uid and uid not in input_uids for _, uid in selections):
                    read.finish()
                    return True  # Player has replaced our selections: leave them.
                if any(obj and not uid for obj, uid in selections):
                    return False
                visible = read.boolean(ui + 40)
                if not animating and not selections and (not opened or not visible):
                    read.finish()
                    return True
                if attempt:
                    read.finish()
                    return False
                calls = [Invocation('CubePanelUI.gbu', instance=ui),
                         Invocation('CubePanelUI.gbf', instance=ui)]
                if opened and visible:
                    calls.append(Invocation('PanelBase.Close', instance=ui))
                self._invoke(calls, read, self._copy(), cancel_event=cancel_event, validate=validate)
        except (MemoryReadError, NativeActionError, ValueError, KeyError, struct.error, UnicodeError):
            # Economic confirmation is independent of UI cleanup. Retry only
            # guarded cleanup; never invalidate a receipt or resend a craft.
            return False

    def _confirm(self, kind, before, selected, recipe, count, state, invocation):
        deadline = time.monotonic() + (20 if kind == 'synthesize_runes' else 8)
        last_error = ''
        while time.monotonic() < deadline:
            try:
                receipt = self._receipt(kind, before, selected, recipe, count, state, invocation)
                if receipt is not None:
                    return receipt
            except (MemoryReadError, ValueError, KeyError, struct.error, UnicodeError) as exc:
                last_error = str(exc)
            time.sleep(.1)
        suffix = f'（{last_error}）' if last_error else ''
        raise SynthesisReceiptPending('合成请求已提交，但材料与产物结果尚未核验' + suffix + '；未重复消耗。', uncertain=True)

    def _receipt(self, kind, before, selected, recipe, count, state, invocation):
        read, manager, save = self._context(after=True)
        copy = self._copy()
        rows, _, _, _ = self._bag(read)
        if kind == 'synthesize_gems':
            gold = self._gold(copy)
            confirmed = (_total(before, recipe['input_key']) - _total(rows, recipe['input_key']) == count * 10
                         and _total(rows, recipe['output_key']) - _total(before, recipe['output_key']) == count
                         and state['gold'] - gold == count * recipe['gold_cost'])
            outputs, remaining = [], count
            old = {r['slot_index']: r for r in before}
            for row in rows:
                previous = old.get(row['slot_index'], {})
                delta = row['count'] - (previous.get('count', 0) if item_key(previous) == item_key(row) else 0)
                if item_key(row) == recipe['output_key'] and delta > 0:
                    take = min(delta, remaining)
                    outputs.append(dict(deepcopy(row), quantity=take)); remaining -= take
            confirmed = confirmed and remaining == 0
            sequence = invocation.get('sequence')
        else:
            after = self._runes(copy, manager, save)
            old_uids = {r.get('item_uid') for r in before if r.get('item_uid')}
            new_rows = [r for r in rows if r.get('item_uid') and r['item_uid'] not in old_uids]
            consumed = {r['item_uid'] for r in selected}
            confirmed = (not any(r.get('item_uid') in consumed for r in rows)
                         and len(new_rows) == 1 and new_rows[0].get('item_class') == 'RuneData'
                         and rune_quality(new_rows[0]) in (recipe['input_quality'], recipe['output_quality'])
                         and state['gold'] - after['gold'] == recipe['gold_cost']
                         and (not state['online'] or after['sequence'] == state['sequence'] + 1))
            outputs = [dict(deepcopy(r), quantity=1) for r in new_rows]
            sequence = after['sequence'] if state['online'] else invocation.get('sequence')
        read.finish(); copy.finish()
        if confirmed:
            return {'inputs': selected, 'outputs': outputs, 'recipe_id': recipe['recipe_id'],
                    'receipt_id': f"synthesis:{self.reader.session_id}:{kind}:{sequence}",
                    'sequence': sequence, 'quantity': count}
        return None
