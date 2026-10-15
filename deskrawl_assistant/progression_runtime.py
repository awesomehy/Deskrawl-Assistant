"""Read-only growth snapshots and guarded calls to original game entry points.

New actions are separately gated by GameActions' explicit local test mode.
No currency/rank/skill field is written by the assistant.
"""
import struct
import time
from contextlib import contextmanager, nullcontext

from .native_memory import MemoryReadError, SnapshotChangedError, GameBusyError
from .native_dispatcher import Argument, Invocation, NativeActionError
from .recommendation_reader import _Copy, decode_obscured_int
from .progression import catalog, merchant_prices
from .action_log import record


MERCHANT_SCAN_CHUNK = 1024 * 1024
MERCHANT_SCAN_BYTES = 256 * 1024 * 1024
MERCHANT_SCAN_SECONDS = 3


class _GrowthCopy(_Copy):
    """Keep complete read consistency checks and compact command predicates.

    Original Dictionary setters increment _version. Guard their identity and
    full header, while retaining entry blocks for finish() rather than placing
    all 85 life nodes and immutable asset tables in the native mailbox.
    """
    def __init__(self, reader):
        super().__init__(reader)
        self.action_guards = []
        self.guarding = True

    @contextmanager
    def copied_only(self):
        previous, self.guarding = self.guarding, False
        try: yield
        finally: self.guarding = previous

    def read(self, address, size, stable=True):
        raw = super().read(address, size, stable)
        if stable and self.guarding: self.action_guards.append((address, raw))
        return raw

    def dictionary(self, obj, *args, **kwargs):
        with self.copied_only():
            result = super().dictionary(obj, *args, **kwargs)
        if obj:
            self.ptr(obj)
            self.read(obj + 24, 24)  # array, count, free list/count and version
        return result

    def array(self, obj, stride, *args, **kwargs):
        with self.copied_only():
            result = super().array(obj, stride, *args, **kwargs)
        if obj:
            self.ptr(obj)
            self.read(obj + 24, 8)
        return result


class ProgressionRuntime:
    def __init__(self, actions):
        self.actions, self.reader = actions, actions.reader
        self.npc = None
        self._merchant_search_key = None
        self._merchant_scanned = set()
        self._merchant_scan_positions = {}

    def _copy(self):
        value = _GrowthCopy(self.reader)
        value.types.update({name: dict(row, name=name) for name, row in self.actions.profile['types'].items()})
        return value

    def _singleton(self, copy, name):
        return copy.singleton(self.actions.profile['singletons'][name], name)

    def _merchant_quotes(self, copy, guard_slot=None):
        """Read the live per-slot queues used by MerchantUI.gzl/gzu.

        SaveSystem's serialized array may remain empty while these online
        offers are populated. Guard the queue cursor and the selected offer,
        including wrapped heads, rather than assuming the first array entry.
        """
        klass=copy.ptr(self.reader.module['base']+self.actions.profile['statics']['merchant_stock_type'])
        if not klass or klass==self.actions.profile.get('uninitialized_type_tokens',{}).get('merchant_stock_type'):return {}
        with copy.copied_only():copy.klass(klass,'fe')
        static=copy.ptr(klass+184)
        if not static:return {}
        stocks=copy.dictionary(copy.ptr(static),'pointer')
        names={r['slot']:name for r,name in zip(catalog()['shop'],
                    ('weapon','helm','chest','pants','boots','belt','ring','necklace','gloves','shoulder'))}
        quotes={}
        def quote(slot,obj):
            if slot not in names:raise MemoryReadError('商人库存部位无效。')
            copy.ptr(obj)
            with copy.copied_only():copy.verify(obj,'fd')
            queue=copy.ptr(obj+32);copy.ptr(queue)
            with copy.copied_only():copy.layout(queue,'Queue`1',{'_array':16,'_head':24,'_tail':28,'_size':32,'_version':36})
            array,head,tail,size,version=struct.unpack('<Qiiii',copy.read(queue+16,24))
            rows=copy.array(array,8,'GambleStockItem',limit=4096)
            if not (0<=size<=len(rows) and (not rows and head==tail==size==0 or rows and 0<=head<len(rows) and 0<=tail<len(rows))):
                raise MemoryReadError('商人库存队列数量或游标无效。')
            if not size:return None
            prices=[];ids=[]
            for offset in range(min(size,10)):
                raw=copy.read(array+32+((head+offset)%len(rows))*8,8)
                offer=struct.unpack('<Q',raw)[0];copy.ptr(offer)
                with copy.copied_only():copy.verify(offer,'GambleStockItem')
                prices.append(copy.integer(offer+68));ids.append(self.reader.string(copy.ptr(offer+16)))
            if any(not isinstance(uid,str) or not uid for uid in ids) or len(set(ids))!=len(ids):
                raise MemoryReadError('商人库存报价编号无效或重复。')
            return {'price':prices[0],'prices':prices,'level':copy.integer(obj+16),
                'difficulty':self.reader.string(copy.ptr(obj+24)),
                'instance_id':ids[0]}
        for slot,obj in stocks.items():
            # Verify every read at finish(), but unrelated shop slots cannot
            # affect this purchase and do not belong in its native predicates.
            with copy.copied_only() if guard_slot is not None and slot!=guard_slot else nullcontext():
                value=quote(slot,obj)
            if value:quotes[names[slot]]=value
        return quotes

    def _asset(self, copy, save, offset, type_name, target):
        rows = copy.dictionary(copy.ptr(save + offset), 'pointer', 'object')
        found = [obj for key, obj in rows.items() if self.reader.string(key) == target]
        if len(found) != 1: raise MemoryReadError('目标成长资源未唯一确认。')
        obj = copy.verify(found[0], type_name)
        if self.reader.asset_name(obj) != target: raise MemoryReadError('成长资源编号与对象不一致。')
        return obj

    def _ranks(self, copy, book, offset, expected, type_name):
        ranks = {}; pointers = {}
        for obj, rank in copy.dictionary(copy.ptr(book + offset), 'int', 'object').items():
            with copy.copied_only(): copy.verify(obj, type_name)
            key = self.reader.asset_name(obj)
            if key not in expected or key in ranks or not 0 <= rank <= expected[key]['max_points']:
                raise MemoryReadError('成长节点或已投入点数尚未确认。')
            ranks[key] = rank; pointers[key] = obj
        return ranks, pointers

    def _merchant_unlock(self, copy):
        requirements=catalog()['shop_unlock']
        try:
            klass=copy.klass(copy.ptr(self.reader.module['base']+self.actions.profile['statics']['merchant_unlocks_type']),'in')
            obj=copy.ptr(copy.ptr(klass+184))
            copy.layout(obj,'HashSet`1',{'_slots':24,'_count':32,'_lastIndex':36,'_version':56})
            array=copy.ptr(obj+24);count,last=struct.unpack('<ii',copy.read(obj+32,8));copy.read(obj+56,4)
            if not 0<=count<=last<=4096:raise MemoryReadError('主城解锁记录数量异常。')
            keys=[]
            for raw in copy.array(array,16,count=last):
                hash_code,_,pointer=struct.unpack('<iiQ',raw)
                if hash_code>=0 and pointer:keys.append(self.reader.string(pointer))
            if len(keys)!=count or any(not key for key in keys) or len(set(keys))!=len(keys):
                raise MemoryReadError('主城解锁记录完整性不一致。')
            return {'unlock_known':True,'unlocked':requirements['key'] in keys,'unlock_requirements':requirements}
        except MemoryReadError as exc:
            return {'unlock_known':False,'unlocked':False,'unlock_requirements':requirements,'unlock_reason':str(exc)}

    def _skills(self, copy, combat, definition, level):
        book = self._singleton(copy, 'PlayerAbilityBook')
        controller = copy.verify(copy.ptr(combat + 112), 'PlayerAbilityController')
        known = {s['id']: s for s in definition['abilities']}
        available = []
        for raw in copy.list(copy.ptr(book + 32), 8, 'AbilityData'):
            obj = copy.verify(struct.unpack('<Q', raw)[0], 'AbilityData')
            key = self.reader.asset_name(obj)
            if key not in known: raise MemoryReadError('当前职业可用技能未适配。')
            if level >= known[key]['level_requirement']: available.append(key)
        equipped = []
        for index in range(4):
            obj = copy.ptr(controller + 32 + index * 8)
            key = self.reader.asset_name(copy.verify(obj, 'AbilityData')) if obj else None
            if key and (key not in known or index not in known[key]['slots']):
                raise MemoryReadError('当前技能栏位未确认。')
            equipped.append(key)
        return {'readable': True, 'equipped': equipped, 'available': available}

    def snapshot(self, *, merchant=False, internal=False, after_action=False, quote_guard_slot=None):
        # Post-action rank/receipt reads are allowed while the original game
        # starts an online save. Mutation contexts still reject that busy flag.
        read, manager, _, scene = self.actions._context(allow_busy=after_action or not internal)
        copy = self._copy(); data = catalog()
        hero = copy.verify(copy.ptr(manager + 56), 'HeroData')
        mask = copy.integer(hero + 48); definition = data['heroes'].get(str(mask))
        if not definition or self.reader.asset_name(hero) != definition['id']:
            raise MemoryReadError('当前职业成长资源未确认。')
        combat = copy.verify(copy.ptr(manager + 64), 'PlayerCombatController')
        player = copy.verify(copy.ptr(combat + 32), 'Player')
        copy.klass(copy.ptr(copy.ptr(player) + 88), 'Character')
        level = decode_obscured_int(copy.read(player + 32, 16))
        # Merchant actions do not change talents, skills or gold. Retain full
        # snapshot consistency for those sections without duplicating unrelated
        # predicates in the finite native mailbox. Capacity/currency/stock and
        # every receiving slot remain guarded below and by GameActions.
        growth_context=copy.copied_only() if merchant and internal else nullcontext()
        with growth_context:
            saved = self._singleton(copy, 'PlayerData')
            gold = decode_obscured_int(copy.read(saved + 48, 16))
        if not 1 <= level <= 200 or gold < 0: raise MemoryReadError('角色等级或金币读取无效。')
        with copy.copied_only() if merchant and internal else nullcontext():
            talent_book = self._singleton(copy, 'PlayerTalentBook')
            talent_nodes = {n['id']: n for n in definition['talents']}
            talents, _ = self._ranks(copy, talent_book, 40, talent_nodes, 'TalentData')
            unspent = copy.integer(talent_book + 32)
            if not 0 <= unspent <= 1000: raise MemoryReadError('剩余天赋点无效。')
            life_book = self._singleton(copy, 'PlayerLifeSkillBook')
            life_nodes = {n['id']: n for n in data['life_skills']}
            life, _ = self._ranks(copy, life_book, 32, life_nodes, 'LifeSkillNodeData')
            try:
                skills = self._skills(copy, combat, definition, level)
            except SnapshotChangedError:
                raise
            except MemoryReadError as exc:
                # Town startup may not have a combat-only skill controller.
                # Its absence cannot invalidate independent currency/books.
                skills = {'readable': False, 'equipped': None, 'available': [], 'reason': str(exc)}
        inventory = self._singleton(copy, 'Inventory'); save = self._singleton(copy, 'SaveSystem')
        busy = copy.boolean(save + 193)
        items = copy.dictionary(copy.ptr(save + 112), 'pointer', 'object')
        item_map = {self.reader.string(key): value for key, value in items.items()}
        soul = copy.verify(item_map.get('SoulShard'), 'ItemData')
        if self.reader.asset_name(soul) != 'SoulShard': raise MemoryReadError('灵魂碎片资源身份未确认。')
        amounts = copy.dictionary(copy.ptr(inventory + 64), 'int', 'object')
        bonuses = copy.dictionary(copy.ptr(player + 664), 'int', 'object')
        capacity = copy.integer(soul + 140) + bonuses.get(soul, 0)
        expected_capacity = data['soul_maximum'] + sum(n['soul_capacity_per_rank'] * life.get(n['id'], 0) for n in data['life_skills'])
        balance = amounts.get(soul, 0)
        if capacity != expected_capacity or not data['soul_maximum'] <= capacity <= 3000 or not 0 <= balance <= capacity:
            raise MemoryReadError('灵魂碎片容量与生活技能加成不一致，暂不消费。')
        shop = {'available': False, 'open': False, 'stock': [], 'preview': [], 'reason': '回城后读取神秘商人。'}
        if merchant:
            shop.update(self._merchant_unlock(copy))
            if scene == 1:
                ui = self._singleton(copy, 'MerchantUI')
                currency = copy.ptr(ui + 192)
                shop['open'] = copy.boolean(ui + 40) and currency == soul
            # Saved serialized stocks are a fallback; live queues below are the
            # exact source used by the game's price and purchase methods.
            stocks = {}
            for raw in copy.array(copy.ptr(save + 80), 8, 'GambleSlotStock', limit=64):
                stock = copy.verify(struct.unpack('<Q', raw)[0], 'GambleSlotStock')
                slot_name = self.reader.string(copy.ptr(stock + 16))
                offer_array = copy.ptr(stock + 40)
                offers = copy.array(offer_array, 8, 'GambleStockItem', limit=4096)
                if offers: copy.read(offer_array + 32, 8)  # selected offer remains in a large quote array
                if offers:
                    offer = copy.verify(struct.unpack('<Q', offers[0])[0], 'GambleStockItem')
                    price = copy.integer(offer + 68)
                    stocks[slot_name.casefold()] = {'price': price, 'level': copy.integer(stock + 24),
                        'difficulty': self.reader.string(copy.ptr(stock + 32)),
                        'instance_id': self.reader.string(copy.ptr(offer + 16))}
            stocks.update(self._merchant_quotes(copy,guard_slot=quote_guard_slot))
            online = copy.integer(save + 32) == 1
            difficulty, _ = self.actions._difficulty_state(read, manager)
            with copy.copied_only():
                for entry in data['shop']:
                    obj = item_map.get(entry['id'])
                    copy.verify(obj, 'RandomEquipment')
                    if self.reader.asset_name(obj) != entry['id'] or copy.integer(obj + 216) != entry['slot']:
                        raise MemoryReadError('商人随机装备部位未确认。')
            rows,preview=merchant_prices(data['shop'],stocks,level,difficulty,online)
            shop.update(available=scene==1,stock=rows if scene==1 else [],preview=preview,
                        reason=('' if rows else '等待游戏刷新在线商人报价。') if scene==1 else '回城后购买；当前只预览报价。',online=online)
        copy.finish(); read.finish()
        result = {'available': True, 'busy': busy, 'class_mask': mask, 'level': level, 'gold': gold,
            'talents': {'ranks': talents, 'unspent': unspent}, 'life_skills': {'ranks': life},
            'skills': skills,
            'soul_shards': {'current': balance, 'maximum': capacity}, 'merchant': shop}
        if internal:
            result['_item_map'] = item_map
            result['_guards'] = read.guards + copy.action_guards
        return result

    def _merchant_npc(self, read, currency, *, cancel_event=None, validate=lambda: True):
        """Bounded read-only lookup of original stock; never fabricated shop assets.

        clw only uses its original GameObject name, stock and currency. Equivalent
        live/prefab instances may share these stock assets; no NPC movement occurs.
        Search references to the already verified currency instead of scanning
        once for metadata and again for objects. Graphics allocations can precede
        both; prefer live roots to avoid spending the scan budget on them.
        """
        deadline = time.monotonic() + MERCHANT_SCAN_SECONDS
        scanned = 0; chunks = []

        def check_budget(*, bytes_limit=False):
            if (cancel_event is not None and cancel_event.is_set()) or not validate():
                raise NativeActionError('神秘商人定位已取消。')
            if time.monotonic() >= deadline or bytes_limit and scanned >= MERCHANT_SCAN_BYTES:
                record('merchant_lookup_wait', reason='scan_budget', scanned_bytes=scanned,
                       completed_chunks=len(self._merchant_scanned), total_chunks=len(chunks))
                raise GameBusyError('神秘商人定位仍在进行，稍后自动继续；尚未提交购买。')

        check_budget()
        read.verify(currency, 'ItemData')
        if self.reader.asset_name(currency) != 'SoulShard':
            raise MemoryReadError('神秘商人货币资源未确认。')

        def valid(obj):
            read.verify(obj, 'DialogueInteractable')
            if not read.ptr(obj + 16): raise MemoryReadError('商人物体已失效。')
            if read.ptr(obj + 344) != currency: raise MemoryReadError('商人货币不一致。')
            stock = read.pointers(read.ptr(obj + 336), 'ItemData', limit=32)
            names = {self.reader.asset_name(read.verify(p, 'RandomEquipment')) for p in stock}
            if names != {r['id'] for r in catalog()['shop']} or len(stock) != len(names):
                raise MemoryReadError('神秘商人原始货架不完整。')
            return obj
        if self.npc:
            mark = len(read.guards)
            try: return valid(self.npc)
            except (MemoryReadError, UnicodeDecodeError, struct.error):
                self.npc = None
                del read.guards[mark:]
        p = self.reader.memory
        anchor = self.reader.singleton('Inventory')
        if type(anchor) is not int: anchor = 0
        key = (currency, anchor)
        if self._merchant_search_key != key:
            self._merchant_search_key = key
            self._merchant_scanned.clear()
            self._merchant_scan_positions.clear()
        anchors = [value for value in key if value]
        # Start around live roots even when they are inside a large allocation.
        # Small allocations follow, then the remaining large native buffers.
        for region in p.regions():
            check_budget()
            if region.kind != 0x20000: continue
            for offset in range(0, region.size, MERCHANT_SCAN_CHUNK):
                check_budget()
                address = region.base + offset
                size = min(MERCHANT_SCAN_CHUNK, region.size - offset)
                distances = {value:max(address-value, value-(address+size-1), 0) for value in anchors}
                nearby = [value for value in anchors if region.base <= value < region.base+region.size
                          and distances[value] <= 4*MERCHANT_SCAN_CHUNK]
                priority = 0 if anchor and anchor in nearby else 1 if nearby else 2 if region.size <= 16*MERCHANT_SCAN_CHUNK else 3
                distance = min(distances.values())
                chunks.append((priority, distance, address, size))
        chunks.sort()
        live_chunks = {(address, size) for _, _, address, size in chunks}
        self._merchant_scanned.intersection_update(live_chunks)
        self._merchant_scan_positions = {key:pos for key,pos in self._merchant_scan_positions.items()
                                         if key in live_chunks}
        needle = struct.pack('<Q', currency)
        for _, _, address, size in chunks:
            check_budget()
            if (address, size) in self._merchant_scanned: continue
            check_budget(bytes_limit=True)
            scanned += size
            try: block = p.read(address, size)
            except MemoryReadError:
                self._merchant_scanned.add((address, size))
                self._merchant_scan_positions.pop((address, size), None)
                continue
            # A dense block can take longer to validate than to read. Retain
            # only its cursor, re-read its bytes and verify each remaining
            # candidate afresh on retry; never carry mutation guards forward.
            pos = self._merchant_scan_positions.get((address, size), -1)
            while True:
                check_budget()
                pos = block.find(needle, pos + 1)
                if pos < 0: break
                self._merchant_scan_positions[address, size] = pos
                obj = address + pos - 344
                if obj < 0x10000 or obj % 8: continue
                mark = len(read.guards)
                try:
                    valid(obj)
                except (MemoryReadError, UnicodeDecodeError, struct.error):
                    del read.guards[mark:]
                    continue
                self.npc = obj
                self._merchant_scanned.clear()
                self._merchant_scan_positions.clear()
                record('merchant_lookup_found', scanned_bytes=scanned)
                return obj
            self._merchant_scanned.add((address, size))
            self._merchant_scan_positions.pop((address, size), None)
        # Scene loading can replace objects in an already visited chunk. Start
        # another fresh sweep on retry, retaining no stale object/read guards.
        self._merchant_scanned.clear()
        self._merchant_scan_positions.clear()
        record('merchant_lookup_wait', reason='not_loaded', scanned_bytes=scanned)
        raise GameBusyError('主城神秘商人对象暂未加载，稍后自动重试；尚未提交购买。')

    def execute(self, kind, payload, *, cancel_event=None, validate=lambda: True):
        before = self.snapshot(merchant=kind.startswith('merchant'), internal=True,
                               quote_guard_slot=payload.get('shop_slot') if kind=='merchant_buy' else 0)
        if kind == 'set_skill' and before.get('skills', {}).get('readable') is False:
            raise NativeActionError('技能读取未确认：' + before['skills'].get('reason', '等待技能控制器加载。'))
        if payload.get('class_mask') != before['class_mask']: raise NativeActionError('职业已经改变，重新规划。')
        read, manager, area, scene = self.actions._context(); read.guards.extend(before['_guards']); copy = self._copy()
        save = self._singleton(copy, 'SaveSystem'); calls = []
        portal = read.singleton('TownPortalSystem')
        if read.boolean(portal + 76) or read.boolean(area + 176):
            raise NativeActionError('游戏正在切换场景，等待成长操作。')
        hero = copy.verify(copy.ptr(manager + 56), 'HeroData')
        if copy.integer(hero + 48) != before['class_mask']: raise SnapshotChangedError('角色职业已改变。')
        if kind in ('allocate_talent', 'learn_life_skill'):
            life = kind == 'learn_life_skill'; definition = catalog()['life_skills'] if life else catalog()['heroes'][str(before['class_mask'])]['talents']
            nodes = {n['id']: n for n in definition}; key = payload.get('node_id')
            if key not in nodes: raise NativeActionError('加点目标不属于当前成长页。')
            if nodes[key].get('selectable') is False: raise NativeActionError('目标天赋未在游戏中开放。')
            node = self._asset(copy, save, 136 if life else 120, 'LifeSkillNodeData' if life else 'TalentData', key)
            if not node: raise NativeActionError('成长节点尚未载入。')
            book = self._singleton(copy, 'PlayerLifeSkillBook' if life else 'PlayerTalentBook')
            ranks, _ = self._ranks(copy, book, 32 if life else 40, nodes, 'LifeSkillNodeData' if life else 'TalentData')
            if ranks.get(key, 0) != payload.get('expected_rank'): raise SnapshotChangedError('加点状态已改变。')
            if life:
                costs = nodes[key]['gold_costs']; rank = ranks.get(key, 0)
                cost = max(0, costs[min(rank, len(costs)-1)]) if costs else 0
                saved = self._singleton(copy, 'PlayerData')
                if decode_obscured_int(copy.read(saved + 48, 16)) != before['gold']:
                    raise SnapshotChangedError('生活技能金币数量已改变。')
                if cost != payload.get('gold_cost') or before['gold'] - payload.get('gold_reserve', 0) < cost:
                    raise SnapshotChangedError('生活技能金币预算已改变。')
                calls = [Invocation('PlayerLifeSkillBook.fmu', book, (Argument.reference(node),), require_true=True),
                         Invocation('PlayerLifeSkillBook.fmv', book, (Argument.reference(node),), require_true=True)]
            else:
                calls = [Invocation('PlayerTalentBook.fod', book, (Argument.reference(node),), require_true=True),
                         Invocation('PlayerTalentBook.fom', book, (Argument.reference(node),))]
        elif kind == 'set_skill':
            hero = copy.verify(copy.ptr(manager + 56), 'HeroData')
            abilities = {self.reader.asset_name(copy.verify(struct.unpack('<Q', raw)[0], 'AbilityData')):struct.unpack('<Q', raw)[0]
                for raw in copy.list(copy.ptr(hero + 80), 8, 'AbilityData')}
            known = {s['id']: s for s in catalog()['heroes'][str(before['class_mask'])]['abilities']}
            combat = copy.verify(copy.ptr(manager + 64), 'PlayerCombatController')
            controller = copy.verify(copy.ptr(combat + 112), 'PlayerAbilityController'); book = self._singleton(copy, 'PlayerAbilityBook')
            target = payload.get('skill_slots')
            if not isinstance(target, list) or len(target) != 4 or len(set(k for k in target if k)) != len([k for k in target if k]):
                raise NativeActionError('目标技能组无效。')
            for index, key in enumerate(target):
                if key == before['skills']['equipped'][index]: continue
                if (key not in abilities or key not in before['skills']['available'] or index not in known[key]['slots']
                        or before['level'] < known[key]['level_requirement']): raise NativeActionError('目标技能未解锁或栏位不一致。')
                ability = abilities[key]
                calls.extend([Invocation('PlayerAbilityBook.bbv', book, (Argument.reference(ability),), require_true=True),
                    Invocation('PlayerAbilityController.bdi', 0, (Argument.reference(ability), Argument.integer(index)), require_true=True)])
            # All guards precede setters, including swaps of the two special slots.
            for index, key in enumerate(target):
                if key != before['skills']['equipped'][index]:
                    calls.append(Invocation('PlayerAbilityController.bdg', controller, (Argument.integer(index), Argument.reference(abilities[key]))))
        elif kind.startswith('merchant'):
            if scene != 1: raise NativeActionError('神秘商人只在主城消费。')
            if kind!='merchant_close' and (before['merchant'].get('unlock_known') is not True or before['merchant'].get('unlocked') is not True):
                raise NativeActionError('神秘商人尚未解锁或解锁状态未确认，不能购买。')
            if kind in ('merchant_buy', 'merchant_refresh'):
                # gzu also checks GameManager.dlu -> CloudClient's pending
                # requests, independently of SaveSystem's saving flag. Its
                # void early return is not a purchase receipt. Keep the read
                # guards in the mailbox so a newly queued request defers the
                # batch before any method is invoked.
                online = self.actions._online_state(read)
                if online['online_saving'] or online['online_pending_requests']:
                    raise GameBusyError('等待在线同步完成后执行神秘商人操作'
                                        f"（待处理请求 {online['online_pending_requests']}）。")
            if kind != 'merchant_close': self.actions._require_empty_town(read, manager, scene)
            ui = self._singleton(copy, 'MerchantUI')
            if kind == 'merchant_close':
                calls = [Invocation('PanelBase.Close', ui)]
            elif kind == 'merchant_open':
                currency = before['_item_map']['SoulShard']
                # An open soul-shard shop is already the requested result. A
                # stale planner or a manually opened dialog must not scan/reopen.
                if not (copy.boolean(ui + 40) and copy.ptr(ui + 192) == currency):
                    npc = self._merchant_npc(read, currency, cancel_event=cancel_event, validate=validate)
                    calls = [Invocation('DialogueInteractable.clw', npc)]
            elif kind == 'merchant_refresh':
                required=payload.get('shop_slots')
                if not isinstance(required,list) or not required or any(type(slot) is not int or not 1<=slot<=10 for slot in required) or len(set(required))!=len(required):
                    raise NativeActionError('待补充的商人库存部位无效。')
                # This is the same save/sync entry used by gzu's empty-stock
                # branch. It refills server stock without submitting a purchase.
                calls=[Invocation('SaveSystem.egx',save,(Argument.boolean(False),))]
            else:
                quote = next((r for r in before['merchant']['stock'] if r['id'] == payload.get('stock_id')), None)
                if not quote or quote['price'] != payload.get('expected_price') or quote['slot'] != payload.get('shop_slot'):
                    raise SnapshotChangedError('商人报价已变化。')
                if not before['merchant']['open'] or before['soul_shards']['current'] != payload.get('before_shards'):
                    raise SnapshotChangedError('商人货币或窗口状态已变化。')
                quantity=payload.get('quantity',1)
                if type(quantity) is not int or not 1<=quantity<=10 or quantity>quote.get('available_count',1) or quote['price']*quantity>before['soul_shards']['current']:
                    raise SnapshotChangedError('批量购买数量、库存或碎片已变化。')
                snapshot = self.reader.snapshot()
                if not snapshot.get('complete'): raise SnapshotChangedError('购买前背包读取不完整。')
                bag = snapshot['containers']['inventory']
                if bag['slot_count']-bag['occupied_count']<quantity: raise NativeActionError('背包空格不足，暂不购买。')
                before_uids = {r.get('item_uid') for r in bag['slots'] if r.get('item_uid')}
                item = before['_item_map'][quote['id']]
                copy.verify(item, 'RandomEquipment')
                if copy.integer(item+216)!=quote['slot']:
                    raise SnapshotChangedError('购买装备部位已变化。')
                # Random equipment ignores gzr's quantity argument. Invoke the
                # original one-item purchase repeatedly on the same main-thread
                # batch, with all stock/currency/capacity checks made up front.
                calls = [Invocation('MerchantUI.gzr', ui, (Argument.reference(item), Argument.integer(1))) for _ in range(quantity)]
        else: raise NativeActionError('未知成长动作。')
        if not calls:
            copy.finish(); read.finish()
            return {'effect_confirmed': True}
        copy.finish(); read.guards.extend(copy.action_guards); read.finish()
        guards = list(dict.fromkeys(read.guards))
        diagnostic = {'kind':kind, 'target':payload.get('node_id', payload.get('stock_id')),
                      'action_id':payload.get('action_id')}
        if kind == 'merchant_buy':
            diagnostic.update(quantity=quantity, shop_slot=payload['shop_slot'],
                              expected_price=payload['expected_price'],
                              before_shards=before['soul_shards']['current'])
        record('growth_action_requested', **diagnostic)
        result = self.actions.dispatcher.invoke(calls, guards, cancel_event=cancel_event, validate=validate)
        deadline = time.monotonic() + (12 if kind.startswith('merchant') else 3)
        confirmed = False; attempts = 0; last_read_error = None; observed = {}
        while time.monotonic() < deadline:
            attempts += 1
            try:
                after = self.snapshot(merchant=kind.startswith('merchant'), after_action=True)
                observed = {'class_mask':after['class_mask'], 'busy':after.get('busy'),
                            'after_shards':after['soul_shards']['current']}
                if after['class_mask'] != before['class_mask']: raise NativeActionError('动作执行期间角色已改变。', uncertain=True)
                if kind == 'allocate_talent':
                    # Leveling can award another point between the original
                    # setter and this read. The target rank is the receipt.
                    confirmed = after['talents']['ranks'].get(key, 0) == payload['expected_rank'] + 1
                    observed['rank'] = after['talents']['ranks'].get(key, 0)
                elif kind == 'learn_life_skill':
                    # fmv returned true after its original gold deduction.
                    # Concurrent gold drops do not invalidate that receipt.
                    confirmed = after['life_skills']['ranks'].get(key, 0) == payload['expected_rank'] + 1
                    observed['rank'] = after['life_skills']['ranks'].get(key, 0)
                elif kind == 'set_skill':
                    confirmed = (after['skills'].get('readable') is not False
                                 and after['skills']['equipped'] == payload['skill_slots'])
                    observed['skills'] = after['skills'].get('equipped')
                elif kind == 'merchant_open': confirmed = after['merchant']['open']
                elif kind == 'merchant_close': confirmed = not after['merchant']['open']
                elif kind == 'merchant_refresh':
                    confirmed = (set(required)<={r['slot'] for r in after['merchant']['stock']}
                                 and after['soul_shards']['current']==before['soul_shards']['current'])
                else:
                    snapshot = self.reader.snapshot()
                    received = [r for r in snapshot.get('containers', {}).get('inventory', {}).get('slots', [])
                        if r.get('item_uid') and r['item_uid'] not in before_uids and not r.get('issues')]
                    confirmed = (snapshot.get('complete') is True and len(received) == quantity
                        and len({row['item_uid'] for row in received}) == quantity
                        and all(row.get('equip_slot') == payload['shop_slot'] for row in received)
                        and after['soul_shards']['current'] == before['soul_shards']['current'] - payload['expected_price']*quantity)
                    observed.update(inventory_complete=snapshot.get('complete'),
                                    snapshot_issues=snapshot.get('issues', []),
                                    received_count=len(received),
                                    received_uids=[row['item_uid'] for row in received],
                                    received_slots=[row.get('equip_slot') for row in received],
                                    expected_shards=before['soul_shards']['current'] - payload['expected_price']*quantity)
                if confirmed: break
            except (MemoryReadError, SnapshotChangedError) as exc:
                last_read_error = f'{type(exc).__name__}: {exc}'
            time.sleep(.05)
        if not confirmed:
            record('growth_action_unconfirmed', **diagnostic, attempts=attempts,
                   last_read_error=last_read_error, observed=observed,
                   native_state={name:result.get(name) for name in ('state','sequence','last_call','completed')})
            label = {'allocate_talent':'战斗天赋加点', 'learn_life_skill':'生活技能学习',
                     'set_skill':'技能切换', 'merchant_open':'打开神秘商人',
                     'merchant_close':'关闭神秘商人', 'merchant_refresh':'神秘商人库存补充',
                     'merchant_buy':'神秘商人购买'}[kind]
            detail = ''
            if kind == 'merchant_buy' and observed.get('received_count') is not None:
                detail = (f"（预计 {quantity} 件，读到 {observed['received_count']} 件；"
                          f"碎片 {before['soul_shards']['current']} → {observed['after_shards']}）")
            raise NativeActionError(f'{label}动作已提交，但实际结果未确认{detail}；已停止重复操作。', uncertain=True)
        if kind == 'merchant_buy':
            result['received_uid'] = received[0]['item_uid']
            result['received_uids']=[row['item_uid'] for row in received]
            result['quantity']=quantity
            # Closing is its own planner action. A later online-save/UI failure
            # cannot erase a confirmed purchase or cause another debit retry.
            result['merchant_open'] = after['merchant']['open']
        result.update(effect_confirmed=True, online_save_confirmed=False, native_verified=False,
                      before_shards=before['soul_shards']['current'], kind=kind)
        if kind == 'learn_life_skill': result['gold_cost'] = payload['gold_cost']
        record('growth_action_confirmed', **diagnostic)
        return result
