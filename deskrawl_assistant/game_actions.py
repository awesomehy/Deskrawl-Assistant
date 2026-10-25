"""Guarded original-game actions for managed mode.

Construction and capability/scene inspection never install a dispatcher.
Only actions with recorded in-game validation are enabled by the build profile;
other paths require an explicit supervised validation run.
An invocation acknowledgement is not claimed to be an online-save receipt.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import struct
import time

from .native_memory import MemoryReadError, SnapshotChangedError, GameBusyError, DeathRecoveryBusyError, OnlineSyncBusyError
from .native_dispatcher import Argument, Invocation, MainThreadDispatcher, NativeActionError, load_profile
from .action_log import record
from .item_protection import divine_equipment, disposal_protection, mandatory_storage
from .equipment_requirements import equip_eligibility
from .mythic import MYTHIC, MAP_ID as MYTHIC_MAP, floor_id, floor_number, WORLD_DIFFICULTIES


from .progression import NEW_ACTIONS
from .synthesis import KINDS as SYNTHESIS_ACTIONS
from .auction_runtime import KINDS as AUCTION_ACTIONS
from .run_receipts import RunReceipts

KINDS = ('return_town', 'enter_map', 'switch_difficulty', 'equip', 'sell', 'salvage', 'open_chest', 'collect_ground') + NEW_ACTIONS + SYNTHESIS_ACTIONS + AUCTION_ACTIONS
CAPABILITIES = {'travel': ('return_town', 'enter_map'), 'equip': ('equip',), 'sell': ('sell',),
    'difficulty': ('switch_difficulty',), 'salvage': ('salvage',), 'chests': ('open_chest',), 'ground_pickup': ('collect_ground',), 'talents': ('allocate_talent',),
    'life_skills': ('learn_life_skill',), 'skills': ('set_skill',), 'merchant': ('merchant_open', 'merchant_buy', 'merchant_close', 'merchant_refresh'),
    'gem_synthesis': ('synthesize_gems',), 'rune_synthesis': ('synthesize_runes',), 'auction_upload': AUCTION_ACTIONS}


def normalise_run(run, completed, dead=False):
    """Separate a confirmed win from the later reward/return completion flag.

    WaveManager.esx sets 249 after the last wave and persists the map-complete
    key. With built-in repeat, the plan can restart without ever setting 248.
    A confirmed final kill may count the run while rewards are still settling;
    it must not force a town return before the game's completion flow finishes.
    """
    if not run:
        return None
    result = {**run, 'dungeon_id':run.get('map_id'), 'status':'running'}
    if dead or run.get('dead') or run.get('phase') == 'dead':
        result.update(status='failed', completion_evidence='verified_Player_dead_flag')
    elif run.get('finished') or run.get('final_kill_settled') is True:
        valid = (run.get('map_id') in completed and type(run.get('wave_count')) is int
            and run['wave_count'] > 0 and run.get('wave_index', -1) >= run['wave_count']
            and run.get('wave_active') is False and run.get('enemy_count') == 0)
        result.update(status='success' if valid and run.get('finished') else 'settling',
            completion_confirmed=valid,
            completion_evidence='WaveManager_completed_last_wave_and_SaveSystem_completed_map' if valid else 'incomplete_completion_evidence')
    return result


class _Read:
    """Read objects against the stricter action profile and retain root guards."""
    def __init__(self, reader, profile):
        self.reader, self.p, self.profile = reader, reader.memory, profile
        self.guards = []

    def read(self, address, size):
        raw = self.p.read(address, size)
        self.guards.append((address, raw))
        return raw

    def ptr(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def integer(self, address):
        return struct.unpack('<i', self.read(address, 4))[0]

    def boolean(self, address):
        value = self.read(address, 1)[0]
        if value not in (0, 1):
            raise MemoryReadError('托管状态布尔值不一致')
        return bool(value)

    def verify(self, obj, name):
        if not obj:
            raise MemoryReadError(f'{name} 尚未载入')
        klass = self.ptr(obj)
        self.klass(klass, name)
        return obj

    def klass(self, klass, name):
        expected = [{'name': f['name'], 'offset': f['rawRegistrationOffset'], 'token': int(f['token'], 16)}
            for f in self.profile['types'][name]['fields']]
        actual_name = self.profile['types'][name].get('name', name)
        if not klass or self.reader.class_name(klass) != actual_name or self.reader.fields(klass) != expected:
            raise MemoryReadError(f'{name} 字段布局与后台指令版本不一致')
        return klass

    def singleton(self, name):
        klass = self.klass(self.ptr(self.reader.module['base'] + self.profile['singletons'][name]), name)
        static = self.ptr(klass+184)
        return self.verify(self.ptr(static), name)

    def pointers(self, obj, element, limit=4096):
        rows, block = self.reader.array(obj, element, stride=8, limit=limit)
        self.guards.append((obj+24, struct.pack('<Q', len(rows))))
        if block: self.guards.append((obj+32, block))
        return [struct.unpack('<Q', raw)[0] for raw in rows]

    def list(self, obj, element, limit=4096, selected_only=False):
        if not obj or self.reader.class_name(self.p.u64(obj)) != 'List`1':
            raise MemoryReadError('托管掉落列表尚未确认')
        fields = {f['name']: f['offset'] for f in self.reader.fields(self.p.u64(obj))}
        if any(fields.get(k) != v for k, v in {'_items':16, '_size':24, '_version':28}.items()):
            raise MemoryReadError('托管掉落列表布局不一致')
        array, count, _ = struct.unpack('<Qii', self.read(obj+16, 16))
        records, block = self.reader.array(array, element, stride=8, limit=limit)
        values = [struct.unpack('<Q', raw)[0] for raw in records]
        self.guards.append((array+24, struct.pack('<Q', len(values))))
        if not 0 <= count <= len(values):
            raise MemoryReadError('托管掉落列表数量异常')
        # The list header guards count/version and the array pointer. Unused
        # backing-array capacity cannot contain a live list entry.
        if not selected_only and count: self.guards.append((array+32, block[:count*8]))
        return values[:count]

    def finish(self):
        if any(self.p.read(a, len(raw)) != raw for a, raw in self.guards):
            raise SnapshotChangedError('角色、场景或掉落正在变化，等待下一轮')


class GameActions:
    def __init__(self, reader, *, dispatcher=None, allow_unverified=False):
        self.reader = reader
        self.profile = load_profile()
        self.dispatcher = dispatcher or MainThreadDispatcher(reader, profile=self.profile)
        self.allow_unverified = allow_unverified
        self.verified_kinds = set(self.profile.get('verified_kinds',[])) & set(KINDS)
        self.enabled_kinds = self.verified_kinds | (set(self.profile.get('enabled_kinds', [])) & set(KINDS))
        self._compatible = None
        self._last_terminal_run = None
        self._last_character = None
        self._run_receipts = RunReceipts()
        self._panel_log_cache = {}
        self._progression = None
        self._synthesis = None
        self._auction = None
        self._reconnect = None

    def reconnect_runtime(self):
        if self._reconnect is None:
            from .reconnect_runtime import ReconnectRuntime
            self._reconnect = ReconnectRuntime(self)
        return self._reconnect

    def capabilities(self):
        """Read-only check; local test opt-in never claims completed validation."""
        reason = '后台动作尚未实机验收，当前不会自动调用游戏方法'
        if self.allow_unverified is True:
            reason = '本地托管测试模式，尚未完成实机验收'
        try:
            if self._compatible is None:
                self.dispatcher.verify()
                self._compatible = True
            self.reader.check_game_files()
            compatible = True
        except (MemoryReadError, OSError, ValueError, KeyError) as exc:
            compatible, reason = False, str(exc)
        fully_verified = len(self.verified_kinds) == len(KINDS)
        if compatible and fully_verified:
            reason = ''
        elif compatible and len(self.enabled_kinds) == len(KINDS):
            reason = '全部动作已开放，部分新增动作尚未实机验收。'
        elif compatible and self.enabled_kinds:
            reason = '已开放已发布动作；新增拍卖行动作尚未实机验收，仅供开发测试。'
        result = {'compatible': compatible, 'native_verified': fully_verified,
            'build': self.profile['build'], 'reason': reason, 'installed': self.dispatcher.installed,
            'test_mode': self.allow_unverified is True}
        for key, kinds in CAPABILITIES.items():
            verified = all(kind in self.verified_kinds for kind in kinds)
            released = all(kind in self.enabled_kinds for kind in kinds)
            test_enabled = compatible and not released and self.allow_unverified is True
            result[key] = {'available': compatible and (released or test_enabled), 'implemented': True,
                'release_enabled': compatible and released,
                'native_verified': verified, 'test_enabled': test_enabled,
                'reason': ('本地托管测试模式，尚未完成实机验收' if test_enabled
                           else '' if compatible and verified else '已开放，尚未实机验收' if compatible and released else reason)}
        return result

    def progression_runtime(self):
        if self._progression is None:
            from .progression_runtime import ProgressionRuntime
            self._progression = ProgressionRuntime(self)
        return self._progression

    def progression_snapshot(self, *, merchant=False):
        return self.progression_runtime().snapshot(merchant=merchant)

    def synthesis_runtime(self):
        if self._synthesis is None:
            from .synthesis_runtime import SynthesisRuntime
            self._synthesis = SynthesisRuntime(self)
        return self._synthesis

    def synthesis_snapshot(self, *, gems=True, runes=True):
        return self.synthesis_runtime().snapshot(gems=gems, runes=runes)

    def auction_runtime(self):
        if self._auction is None:
            from .auction_runtime import AuctionRuntime
            self._auction = AuctionRuntime(self)
        return self._auction

    def auction_state(self):
        return self.auction_runtime().snapshot()

    def steam_identity_snapshot(self):
        """Read current authorized account without opening the auction UI."""
        return self.auction_runtime().identity_snapshot()

    def auction_upload(self, row, *, expected_steamid=None,
                       cancel_event=None, validate=lambda: True):
        return self.execute('auction_upload', {'item': row,
                            'expected_steamid': expected_steamid},
                            cancel_event=cancel_event, validate=validate)

    def mark_verified(self, kinds):
        """Called only by a supervised in-game validation, not a web endpoint."""
        if any(kind not in KINDS for kind in kinds):
            raise ValueError('未知的游戏动作')
        self.verified_kinds.update(kinds)

    def _context(self, *, allow_busy=False):
        self.reader.check_game_files()
        read = _Read(self.reader, self.profile)
        manager = read.singleton('GameManager')
        if read.integer(manager+168) != 1:
            raise NativeActionError('请先进入角色，当前不能执行托管动作')
        area = read.singleton('GameAreaManager')
        scene = read.integer(area+120)
        if scene not in (0, 1, 2):
            raise NativeActionError('当前场景类型未核验')
        save = read.singleton('SaveSystem')
        read.read(save+32, 8)  # mode and character slot cannot change
        if read.boolean(save+193) and not allow_busy:
            raise GameBusyError('游戏暂时禁止物品操作，等待在线同步完成。')
        return read, manager, area, scene

    def _online_state(self, read):
        save = read.singleton('SaveSystem')
        saving = read.boolean(save+193)
        pending = 0
        if read.integer(save+32) == 1:
            cloud = read.singleton('CloudClient')
            pending = read.integer(cloud+276)
            if pending < 0: raise NativeActionError('在线请求队列状态异常，已停止操作。')
        return {'online_saving':saving, 'online_pending_requests':pending}

    def _require_travel_ready(self, read):
        state = self._online_state(read)
        if state['online_saving'] or state['online_pending_requests']:
            raise OnlineSyncBusyError(f"等待在线同步完成后切换场景（待处理请求 {state['online_pending_requests']}）。")
        return state

    def _equip_level(self, read, manager, selected, item, target_slot=None):
        """Re-read the same Player/config fields as ddt before any game call."""
        from .recommendation_reader import _Copy, decode_obscured_int
        copy = _Copy(self.reader)
        combat = copy.verify(copy.ptr(manager + 64), 'PlayerCombatController')
        player = copy.verify(copy.ptr(combat + 32), 'Player')
        copy.klass(copy.ptr(copy.ptr(player) + 88), 'Character')
        config = copy.verify(copy.ptr(manager + 32), 'GameConfig')
        character = {'available': True,
                     'level': decode_obscured_int(copy.read(player + 32, 16)),
                     'max_level': copy.integer(config + 264)}
        if target_slot in (6, 7):
            equipment = read.singleton('EquipmentManager')
            equipped = copy.dictionary(copy.ptr(equipment + 40), 'pointer')
            character['equipment'] = [
                {'equipped_slot': slot, 'internal_name': self.reader.asset_name(equipped[slot])}
                for slot in (6, 7) if equipped.get(slot)]
        current = {**selected, 'base_rarity': copy.integer(item + 36),
                   'equip_slot': copy.integer(item + 144)}
        eligibility = equip_eligibility(current, character, target_slot=target_slot)
        copy.finish()
        read.guards.extend(copy.guards)
        if not eligibility['can_equip']:
            raise NativeActionError(eligibility['reason'])
        return eligibility

    def _enrich_drops(self, read, rows):
        registry, stamp = self.reader.registry()
        read.guards.append((stamp['object']+24, stamp['header']))
        old_stamps = self.reader.snapshot_stamps
        try:
            self.reader.snapshot_stamps = []
            for row in rows:
                uid = row.get('item_uid')
                obj = registry.get(uid) if uid else None
                if obj:
                    row.update(self.reader.generated(obj))
                    row.update(actual_quality_unknown=False, quality_verified=True)
                elif row.get('is_equipment'):
                    row.update(actual_quality_unknown=True, quality_verified=False)
                    row.pop('rarity', None)
                    row['issues'] = ['掉落实例品质尚未在注册表核验']
                else:
                    row.update(actual_quality_unknown=False, quality_verified=True)
            read.guards.extend(self.reader.snapshot_stamps)
        finally:
            self.reader.snapshot_stamps = old_stamps
        return rows

    def _carriage(self, read, *, in_town=False):
        from .carriage_transfer import read_carriage, CarriageUnavailable
        try:
            value = read_carriage(self.reader)
            rows = [dict(row, container='carriage') for row in value['items']]
            self._enrich_drops(read, rows)
            read.guards.extend(value['guards'])
            # Town offloading first removes held drops from the carriage list
            # and then animates them onto the ground. Those gaps are not empty
            # reward sources and must not complete a graceful stop.
            # jm.MoveNext clears the offload Coroutine at +176 on completion;
            # +168 is a reused scratch list and may remain populated in combat.
            settling = in_town and bool(read.ptr(value['carriage'] + 176))
            for offset in ((160,) if in_town else ()):
                pending = read.ptr(value['carriage'] + offset)
                if not pending: continue
                if self.reader.class_name(read.p.u64(pending)) != 'List`1':
                    raise MemoryReadError('马车卸货列表尚未确认')
                fields = {f['name']:f['offset'] for f in self.reader.fields(read.p.u64(pending))}
                if any(fields.get(k) != v for k,v in {'_items':16,'_size':24,'_version':28}.items()):
                    raise MemoryReadError('马车卸货列表布局不一致')
                # Clear() increments _version even for an empty animation list
                # on each game frame; only its size is relevant to settling.
                count = read.integer(pending + 24)
                if not 0 <= count <= 8192: raise MemoryReadError('马车卸货数量未确认')
                settling = settling or count > 0
            return {'available':True, 'complete':not settling, 'settling':settling, 'items':rows}
        except CarriageUnavailable as exc:
            # No carriage in town is a complete empty source; a changing list
            # is not an empty source and cannot permit the cleanup cycle to end.
            return {'available':False, 'complete':'正在' not in str(exc), 'items':[], 'reason':str(exc)}

    def _ground(self, read, manager, selection_id=None):
        rows = []
        list_obj = read.ptr(manager+216)
        world = read.list(list_obj, 'LootDrop', 8192, selected_only=selection_id is not None)
        carriage = read.ptr(manager+88)
        carriage_items = set()
        if carriage:
            # Exact HorseCarriage layout is verified by the existing reader.
            from .carriage_transfer import read_carriage
            value = read_carriage(self.reader)
            carriage_items = {row['_address'] for row in value['items']}
        for position, address in enumerate(world):
            if not address or address in carriage_items:
                continue
            if self.reader.class_name(self.reader.memory.u64(address)) != 'LootDrop':
                raise MemoryReadError('地面掉落对象类型改变')
            header = self.reader.memory.read(address, 216)
            if not struct.unpack_from('<Q', header, 16)[0] or header[185]:
                continue
            item = struct.unpack_from('<Q', header, 120)[0]
            if not item:
                if not (header[132] or header[133]):
                    continue
                row = {'internal_name':'GoldDrop' if header[132] else 'HealthPotionDrop',
                    'is_equipment':False, 'item_type':1, 'item_class':'LootDrop'}
            else:
                row = self.reader.base_item(item)
            uid = self.reader.string(struct.unpack_from('<Q', header, 136)[0]) or ''
            if row['internal_name'] == 'GoldDrop':
                # LootDrop.cwb passes this value directly to PlayerData's gold
                # receiver. +164 is the material count, not a coin's value.
                count = struct.unpack_from('<i', header, 128)[0]
            elif row['internal_name'] == 'HealthPotionDrop':
                count = 1  # Native pickup refills exactly one flask charge.
            else:
                count = max(1, struct.unpack_from('<i', header, 164 if row['item_type'] == 1 else 168)[0])
            if not 1 <= count <= 2147483647:
                raise MemoryReadError('地面物品数量异常')
            identity = f'{self.reader.session_id}:{address}:{item}:{uid}'
            token = hashlib.sha256(f'{identity}:{count}'.encode()).hexdigest()
            if selection_id is not None and token != selection_id:
                continue
            read.verify(address, 'LootDrop')
            row = {**row, 'item_uid': uid, 'count': count, 'container':'ground',
                'observation_id':hashlib.sha256(identity.encode()).hexdigest(),
                'selection_id':token,
                '_address':address, '_header':header}
            # Ignore animation/flight fields; identity and consumed flag matter.
            read.guards.extend(((address, header[:24]), (address+120, header[120:153]),
                (address+164, header[164:172]), (address+185, header[185:186])))
            if selection_id is not None:
                array = self.reader.memory.u64(list_obj+16)
                read.guards.append((array+32+position*8, struct.pack('<Q', address)))
            rows.append(row)
        return self._enrich_drops(read, rows)

    def _map_objects(self, read, area, selected_id=None):
        array = read.ptr(area+184)
        if not array:
            raise NativeActionError('关卡资源列表尚未载入')
        result = {}
        records, _ = self.reader.array(array, 'MapData', stride=8, limit=4096)
        read.guards.append((array+24, struct.pack('<Q', len(records))))
        for index, raw in enumerate(records):
            obj = struct.unpack('<Q', raw)[0]
            if obj:
                key = self.reader.asset_name(obj)
                if selected_id is not None and key != selected_id:
                    continue
                read.verify(obj, 'MapData')
                read.guards.append((array+32+index*8, raw))
                if key in result:
                    raise NativeActionError('关卡资源名称重复，已停止自动入图')
                result[key] = obj
        return result

    def _difficulty_state(self, read, manager):
        klass = read.klass(read.ptr(self.reader.module['base']+self.profile['statics']['ey']),'ey')
        static = read.ptr(klass+184)
        current,maximum = read.integer(static),read.integer(static+4)
        if current not in (0,1,2) or maximum not in (0,1,2) or current>maximum:
            raise NativeActionError('当前难度或难度解锁状态未确认')
        config = read.verify(read.ptr(manager+32),'GameConfig')
        tiers = read.pointers(read.ptr(config+200),'DifficultyTierConfig',16)
        values = []
        for obj in tiers:
            if obj: values.append(read.integer(read.verify(obj,'DifficultyTierConfig')+16))
        if len(values)!=len(set(values)) or any(value not in (0,1,2) for value in values):
            raise NativeActionError('游戏难度配置未确认')
        names,labels = ('Normal','Nightmare','Inferno'),('普通','噩梦','炼狱')
        return names[current],[{'id':key,'label':labels[n],'value':n,'unlocked':n<=maximum and n in values}
                               for n,key in enumerate(names)]

    def _completion_state(self, read):
        """These native visibility flags guard the game's shortcut return key."""
        result = {}
        for name, offset in (('CombatMapCompletionUI', 0), ('MythicCompletionUI', 8)):
            value = read.ptr(self.reader.module['base']+self.profile['statics'][name])
            # IL2CPP resolves this TypeInfo usage only when the game first uses
            # the completion UI. These exact tokens are verified against this
            # build's DLL and metadata; other values still need strict checks.
            if value == self.profile['uninitialized_type_tokens'][name]:
                result[name] = False
                continue
            klass = read.klass(value, name)
            static = read.ptr(klass+184)
            result[name] = read.boolean(static+offset) if static else False
        return result

    def _return_town_call(self, read, manager, portal):
        self._require_travel_ready(read)
        completion = self._completion_state(read)
        combat = read.verify(read.ptr(manager+64), 'PlayerCombatController')
        player = read.ptr(combat+32)
        if not player or self.reader.class_name(self.reader.memory.u64(player)) != 'Player':
            raise NativeActionError('当前角色实体未确认')
        dead = read.boolean(player+296)
        if dead:
            # The portal shortcut does not cancel DeathUI's auto-revive timer
            # or run the death settlement coroutine. Use BackToTownButton's
            # original handler, which stops the timer and marks its request.
            type_info = read.ptr(self.reader.module['base'] + self.profile['singletons']['DeathUI'])
            if not type_info or type_info == self.profile['uninitialized_type_tokens']['DeathUI']:
                raise DeathRecoveryBusyError('等待死亡界面载入后返回主城。')
            klass = read.klass(type_info, 'DeathUI')
            static = read.ptr(klass + 184)
            death_ui = read.ptr(static) if static else 0
            if not death_ui:
                raise DeathRecoveryBusyError('等待死亡界面实例载入后返回主城。')
            read.verify(death_ui, 'DeathUI')
            if read.boolean(death_ui + 73):
                raise DeathRecoveryBusyError('死亡后的复活或回城正在处理，等待游戏完成。')
            return Invocation('DeathUI.return_to_town', death_ui)
        # frj is a shortcut toggle and may cancel an active channel. Keep the
        # accepted Boolean paths for living characters and completion returns.
        method = 'TownPortalSystem.frl' if any(completion.values()) else 'TownPortalSystem.frk'
        return Invocation(method, portal, require_true=True)

    def action_context_snapshot(self):
        """Fresh scene/sync gates for inventory actions, without campaign data."""
        read,_,area,scene = self._context(allow_busy=True)
        online = self._online_state(read)
        portal = read.singleton('TownPortalSystem')
        value = {'available':True,'scene':{'available':True,'in_town':scene==1,'in_dungeon':scene==2,
                 'teleporting':read.boolean(portal+76),'scene_changing':read.boolean(area+176),**online}}
        read.finish()
        return value

    def _capture_run_receipts(self, recommendation):
        """Preserve a guarded win before unrelated loot/UI reads can fail.

        The campaign reader verifies its own plan, completion and character
        guards. This smaller guard window also brackets the campaign read with
        the current role slot and live death flag; changing either invalidates
        the receipt before it enters the buffer.
        """
        from .recommendation_reader import read_campaign
        read = _Read(self.reader, self.profile)
        manager = read.singleton('GameManager')
        if read.integer(manager + 168) != 1:
            raise NativeActionError('请先进入角色，再读取关卡。')
        save = read.singleton('SaveSystem')
        read.read(save + 32, 8)
        combat = read.verify(read.ptr(manager + 64), 'PlayerCombatController')
        player = read.verify(read.ptr(combat + 32), 'Player')
        dead = read.boolean(player + 296)
        campaign = recommendation if recommendation is not None else read_campaign(self.reader)
        if not campaign.get('available') or not isinstance(campaign.get('profile'), dict):
            if campaign.get('retryable') is True:
                raise SnapshotChangedError(campaign.get('reason') or '关卡进度正在变化，等待下一轮')
            raise NativeActionError(campaign.get('reason') or '关卡进度暂不可读')
        progress = campaign['profile']
        completed = set(progress['completed_maps'].get(progress['difficulty'], []))
        completed.update(progress['mythic_completed'])
        run = normalise_run(campaign.get('run'), completed, dead)
        read.finish()
        # An explicitly supplied snapshot has no guard provenance here. Keep
        # this legacy input usable by read-only callers, without publishing it
        # as newly observed native evidence (or exposing an old identity).
        history = (self._run_receipts.observe(getattr(self.reader, 'session_id', None),
                                              progress['character_id'], run)
                   if recommendation is None else None)
        if progress['character_id'] != self._last_character:
            self._last_terminal_run = None
            self._last_character = progress['character_id']
        return campaign, history

    def control_snapshot(self, recommendation=None):
        """Read-only: actual progression plus loaded map objects and ground loot."""
        run_history = None
        try:
            if not self.capabilities()['compatible']:
                raise NativeActionError(self.capabilities()['reason'])
            recommendation, run_history = self._capture_run_receipts(recommendation)
            read, manager, area, scene = self._context(allow_busy=True)
            online = self._online_state(read)
            portal = read.singleton('TownPortalSystem')
            teleporting, scene_changing = read.boolean(portal+76), read.boolean(area+176)
            combat = read.verify(read.ptr(manager+64), 'PlayerCombatController')
            player = read.ptr(combat+32)
            if not player or self.reader.class_name(self.reader.memory.u64(player)) != 'Player':
                raise NativeActionError('当前角色实体未确认')
            dead = read.boolean(player+296)
            in_battle = read.boolean(combat+80)
            travel_state = self._combat_travel_state(read, manager) if scene == 0 else {
                'player_state': None, 'gathering': False, 'travel_blocked': False, 'travel_wait_reason': ''}
            completion = self._completion_state(read)
            from .recommendation_reader import _catalog, _Copy
            progress = recommendation['profile']
            difficulty = progress['difficulty']
            current_difficulty,difficulties = self._difficulty_state(read,manager)
            if difficulty!=current_difficulty: raise SnapshotChangedError('游戏难度正在变化，等待重新读取')
            unlocked = set(progress['unlocked_maps'].get(difficulty, []))
            completed = set(progress['completed_maps'].get(difficulty, []))
            objects = self._map_objects(read, area)
            catalog = _catalog()
            maps_by_difficulty = {}
            for tier in difficulties:
                key,diff_value = tier['id'],tier['value']
                tier_unlocked = set(progress['unlocked_maps'].get(key,[]))
                tier_completed = set(progress['completed_maps'].get(key,[]))
                maps = []
                for row in catalog['maps']:
                    if row.get('map_type') == 1: continue
                    requirements = [entry for entry in row.get('requirements', []) if entry['difficulty_mask'] & (1 << diff_value)]
                    enough = all(progress['entry_items'].get(entry['item_id'], 0) >= entry['count'] for entry in requirements)
                    maps.append({'map_id':row['map_id'], 'name':row['stage'], 'stage':row['stage'],'difficulty':key,
                    'family':row.get('family'), 'map_type':row.get('map_type'),
                    'prerequisite':row.get('prerequisite'), 'unlocked':tier['unlocked'] and row['map_id'] in tier_unlocked,
                    'completed':row['map_id'] in tier_completed, 'loaded':row['map_id'] in objects,
                    'requirements':requirements, 'can_enter':tier['unlocked'] and row['map_id'] in tier_unlocked and row['map_id'] in objects and enough,
                    'min_level':row.get('min_level'), 'max_level':row.get('max_level')})
                maps_by_difficulty[key] = maps
            mythic = next((row for row in catalog['maps'] if row['map_id']==MYTHIC_MAP and row.get('map_type')==1),None)
            maps_by_difficulty[MYTHIC] = []
            minimum = progress['mythic_min_difficulty']
            entry_difficulty = max((difficulty,minimum),key=WORLD_DIFFICULTIES.index)
            if mythic and not mythic.get('unavailable') and not (progress['is_demo'] and mythic.get('locked_in_demo')):
                requirements = [entry for entry in mythic.get('requirements',[]) if entry['difficulty_mask'] & (1<<WORLD_DIFFICULTIES.index(entry_difficulty))]
                enough = all(progress['entry_items'].get(entry['item_id'],0)>=entry['count'] for entry in requirements)
                for floor in progress['mythic_unlocked']:
                    maps_by_difficulty[MYTHIC].append({'map_id':floor_id(floor),'name':f'层级 {floor}','stage':f'层级 {floor}',
                        'difficulty':MYTHIC,'map_type':1,'mythic_floor':floor,'unlocked':True,'loaded':MYTHIC_MAP in objects,
                        'completed':floor_id(floor) in progress['mythic_completed'], 'requirements':requirements,
                        'can_enter':MYTHIC_MAP in objects and enough})
            difficulties.append({'id':MYTHIC,'label':'神话裂隙','unlocked':bool(maps_by_difficulty[MYTHIC])})
            ground = self._ground(read, manager)
            carriage = self._carriage(read, in_town=scene == 1)
            try:
                from .game_loot_ledger import read_loot_ledger
                loot_ledger = read_loot_ledger(self.reader, read, manager)
            except (MemoryReadError, ValueError, KeyError, struct.error) as exc:
                loot_ledger = {'available':False, 'complete':False, 'reason':str(exc)}
            try:
                from .game_panel_log import read_panel_log
                system_log = read_panel_log(self.reader, self.profile, manager, cache=self._panel_log_cache)
            except (MemoryReadError, OSError, ValueError, KeyError, UnicodeError, struct.error) as exc:
                system_log = {'available':False, 'complete':False, 'reason':str(exc)}
            from .statistics_reader import read_gold_ledger
            gold_ledger = read_gold_ledger(self.reader, progress['character_id'])
            active = read.ptr(manager+112)
            map_id = self.reader.asset_name(active) if active else None
            scene_difficulty = difficulty
            if map_id == MYTHIC_MAP and scene == 0:
                # WaveManager belongs to the connection-verified read profile,
                # not the narrower native-action profile. Keep its guards until
                # the whole control snapshot is checked, including floor changes.
                copy = _Copy(self.reader)
                wave = copy.verify(copy.ptr(manager+104),'WaveManager')
                map_id,scene_difficulty = floor_id(copy.integer(wave+304)),MYTHIC
                read.guards.extend(copy.guards)
            read.finish()
            run = normalise_run(recommendation.get('run'), completed | set(progress['mythic_completed']), dead)
            if progress['character_id'] != self._last_character:
                self._last_terminal_run = None
                self._last_character = progress['character_id']
            if run and (run['status'] in ('success', 'failed') or run.get('completion_confirmed') is True):
                self._last_terminal_run = dict(run)
            elif not run and self._last_terminal_run:
                run = dict(self._last_terminal_run, retained_terminal=True)
            return {'available':True, 'compatible':True, 'scene':{0:'combat',1:'town',2:'fishing'}[scene],
                'scene_changing':scene_changing, 'teleporting':teleporting, 'dead':dead, 'in_battle':in_battle, **travel_state, **online,
                'completion_visible':completion,
                'run_history':run_history,
                'loot_ledger':loot_ledger,
                'system_log':system_log,
                'gold_ledger':gold_ledger,
                'map_id':map_id, 'difficulty':scene_difficulty, 'world_difficulty':difficulty,'mythic_entry_difficulty':entry_difficulty, 'run':run,
                'maps':maps_by_difficulty[scene_difficulty], 'maps_by_difficulty':maps_by_difficulty,'difficulties':difficulties,
                'ground':[{k:v for k,v in row.items() if not k.startswith('_')} for row in ground],
                'ground_complete':True, 'character_id':progress['character_id'], 'native_verified':False,
                'carriage':{**carriage, 'items':[{k:v for k,v in row.items() if not k.startswith('_')} for row in carriage['items']]},
                'captured_at':datetime.now(timezone.utc).isoformat(), 'reason':''}
        except (MemoryReadError, OSError, ValueError, KeyError, UnicodeError, struct.error) as exc:
            return {'available':False, 'compatible':self._compatible is True, 'maps':[], 'ground':[],
                'ground_complete':False, 'native_verified':False, 'reason':str(exc),
                'retryable':isinstance(exc, SnapshotChangedError),
                **({'run_history':run_history} if run_history is not None else {})}

    def _selected_inventory(self, payload, read):
        requested = payload.get('item', payload.get('row', payload))
        index = requested.get('slot_index')
        if type(index) is not int or not 0 <= index < 2048 or requested.get('container', 'inventory') != 'inventory':
            raise NativeActionError('原生装备操作只接受当前背包格子')
        snapshot = self.reader.snapshot()
        if not snapshot.get('complete'):
            raise SnapshotChangedError('物品快照不完整，未执行装备操作')
        inventory = snapshot['containers']['inventory']
        row = next((row for row in inventory['slots'] if row['slot_index'] == index), None)
        if not row or row.get('issues'):
            raise NativeActionError('这件背包物品已变化，未执行操作')
        row = dict(row)
        row['_before_total_count'] = sum(candidate.get('count', 0)
            for container in snapshot['containers'].values() for candidate in container['slots']
            if candidate.get('internal_name') == row.get('internal_name'))
        identity = any(requested.get(key) for key in ('item_uid', 'instance_id', 'transfer_key'))
        if not identity:
            raise NativeActionError('缺少物品身份校验，不接受仅凭格号操作')
        for key in ('item_uid', 'instance_id', 'transfer_key', 'internal_name', 'count'):
            if key in requested and requested[key] != row.get(key):
                raise NativeActionError('物品身份或数量改变，未执行操作')
        root = read.singleton('Inventory')
        array = read.ptr(root+56)
        records, block = self.reader.array(array, 'InventorySlot', limit=2048, stride=8)
        if index >= len(records):
            raise NativeActionError('背包格号超出当前容量')
        address = struct.unpack('<Q', records[index])[0]
        if not address or self.reader.memory.u64(address) != self.reader.classes['InventorySlot']:
            raise NativeActionError('物品槽对象类型未核验')
        read.guards.append((array+32+index*8, block[index*8:index*8+8]))
        header = read.read(address, 48)
        if self.reader.transfer_key('inventory', index, address, header, row) != row['transfer_key']:
            raise NativeActionError('物品内容或锁定状态在准备时改变，未执行操作')
        item, uid = struct.unpack_from('<Q', header, 16)[0], struct.unpack_from('<Q', header, 32)[0]
        if (self.reader.string(uid) or '') != (row.get('item_uid') or ''):
            raise NativeActionError('物品编号在准备时改变，未执行操作')
        if row.get('item_uid'):
            registry, stamp = self.reader.registry()
            generated = registry.get(row['item_uid'])
            if generated:
                saved_stamps = self.reader.snapshot_stamps
                try:
                    self.reader.snapshot_stamps = []
                    fresh = self.reader.generated(generated)
                    if any(row.get(key) != value for key, value in fresh.items()):
                        raise NativeActionError('装备实例、词条或锁定状态改变，未执行操作')
                    read.guards.extend(self.reader.snapshot_stamps)
                finally:
                    self.reader.snapshot_stamps = saved_stamps
                read.guards.append((stamp['object']+24, stamp['header']))
        return row, address, header, item, uid

    @staticmethod
    def _disposition(row, payload):
        if payload.get('disposition_authorized') is not True:
            raise NativeActionError('处置装备必须先通过托管规则重新核验')
        if reason := disposal_protection(row): raise NativeActionError(reason)
        if row.get('slot_locked') or row.get('locked') or row.get('is_black_mist') or row.get('rarity', row.get('base_rarity')) == 4:
            raise NativeActionError('已锁定、黑雾或神圣物品必须保留，未执行处置')
        if row.get('issues') or (row.get('is_equipment') and row.get('modifiers_complete') is not True):
            raise NativeActionError('词条读取不完整，未执行处置')
        if payload.get('rule_matched') is True or payload.get('rule_pending') is True:
            raise NativeActionError('符合保留规则或待核验的物品不能处置')

    def _require_empty_town(self, read, manager, scene):
        if scene != 1: raise NativeActionError('进入地图或切换难度前必须先回主城整理。')
        if self._ground(read,manager): raise NativeActionError('主城仍有地面物品，禁止进入地图或切换难度。')
        carriage = self._carriage(read,in_town=True)
        if carriage.get('complete') is not True or carriage.get('items') != []:
            raise NativeActionError('马车仍有物品或正在卸货，禁止进入地图或切换难度。')
        self._require_stored_inventory(read)

    def _combat_travel_state(self, read, manager):
        """Mirror the original portal gates; keep their reads in native guards."""
        combat = read.verify(read.ptr(manager + 64), 'PlayerCombatController')
        state = read.integer(combat + 48)
        gathering = read.boolean(combat + 52)
        active = read.ptr(manager + 112)
        if active: read.verify(active, 'MapData')
        map_type = read.integer(active + 32) if active else None
        # TownPortalSystem.frm permits attacks only in CombatMapType 2.
        blocked = not active or gathering or (state == 1 and map_type != 2)
        reason = ('等待当前关卡载入后再切图。' if not active else
                  '角色正在采集，等待结束后再切图。' if gathering else
                  '角色正在攻击，等待可切图状态后再尝试。' if blocked else '')
        return {'player_state': state, 'gathering': gathering,
                'travel_blocked': blocked, 'travel_wait_reason': reason}

    def _require_stargazing_run(self, read, manager, source_run_id):
        # Keep the active map/plan pointers in the mailbox guards. A native
        # repeat occurring after the Python preflight must cancel the action.
        from .recommendation_reader import _Copy
        copy = _Copy(self.reader)
        wave = copy.verify(copy.ptr(manager + 104), 'WaveManager')
        plan = copy.verify(copy.ptr(manager + 120), 'RunPlan')
        active = copy.ptr(manager + 112)
        if copy.ptr(wave + 88) != plan or copy.ptr(wave + 40) != active or not active:
            raise SnapshotChangedError('观星关卡计划正在切换。')
        if not copy.boolean(wave + 96) or copy.boolean(wave + 248) or copy.boolean(wave + 249):
            raise SnapshotChangedError('关卡已结束或正在切换，重新核验通关计数后再操作。')
        raw_id = self.reader.string(copy.ptr(plan + 16)) or ''
        if not raw_id or hashlib.sha256(raw_id.encode()).hexdigest() != source_run_id:
            raise SnapshotChangedError('观星源关卡已变化，禁止局内切图。')
        copy.finish()
        read.guards.extend(copy.guards)

    def _require_stored_inventory(self, read):
        # Guard empty slots too: an offload can fill an existing slot after the
        # planning snapshot without changing the inventory array itself.
        root = read.singleton('Inventory')
        array = read.ptr(root+56)
        if not array: raise NativeActionError('背包尚未读取，禁止进入地图或切换难度。')
        stamps = self.reader.snapshot_stamps
        self.reader.snapshot_stamps = []
        try:
            registry,_ = self.reader.registry()
            # Keep registry identity/count/version, not every stored item's
            # modifiers. The native mailbox has a bounded guard capacity.
            read.guards.extend(self.reader.snapshot_stamps[:3])
            for slot in read.pointers(array,'InventorySlot',limit=2048):
                if not slot: continue
                header = self.reader.memory.read(slot,48)
                if struct.unpack_from('<Q',header)[0] != self.reader.classes['InventorySlot']:
                    raise MemoryReadError('背包槽位类型不一致，禁止进入地图或切换难度。')
                read.guards.append((slot+16,header[16:24]))
                item = struct.unpack_from('<Q',header,16)[0]
                if not item: continue
                read.guards.append((slot+32,header[32:40]))
                row = dict(self.reader.base_item(item))
                uid = self.reader.string(struct.unpack_from('<Q',header,32)[0])
                generated = registry.get(uid)
                if generated:
                    fresh = self.reader.memory.read(generated,128)
                    if struct.unpack_from('<Q',fresh)[0] != self.reader.classes['GeneratedItemData']:
                        raise MemoryReadError('物品实例类型不一致，禁止进入地图或切换难度。')
                    row.update(rarity=struct.unpack_from('<i',fresh,20)[0],is_black_mist=bool(fresh[105]))
                    read.guards.extend(((generated+20,fresh[20:24]),(generated+105,fresh[105:106])))
                elif row.get('is_equipment'):
                    raise NativeActionError('背包装备实例未确认，禁止进入地图或切换难度。')
                if mandatory_storage(row):
                    raise NativeActionError('背包仍有必须收纳的物品，禁止进入地图或切换难度。')
        finally:
            self.reader.snapshot_stamps = stamps

    def execute(self, kind, payload=None, *, cancel_event=None, validate=lambda:True):
        """Execute only a newly checked inventory identity or selected world drop.

        New native paths remain disabled unless supervised validation is opted
        into. Callers own fresh rule/settings checks and pass a validate callback
        which is checked again immediately before publishing the native job.
        """
        kind = {'chest':'open_chest'}.get(kind, kind)
        if kind not in KINDS:
            raise NativeActionError('未知的原生托管动作')
        if not self.allow_unverified and kind not in self.enabled_kinds:
            raise NativeActionError('后台动作尚未实机验收，当前不会自动调用游戏方法')
        if cancel_event is not None and cancel_event.is_set() or not validate():
            raise NativeActionError('托管操作已停止或配置已经改变')
        if not self.capabilities()['compatible']:
            raise NativeActionError(self.capabilities()['reason'])
        payload = payload or {}
        if kind in AUCTION_ACTIONS:
            return self.auction_runtime().execute(kind, payload, cancel_event=cancel_event, validate=validate)
        if kind in SYNTHESIS_ACTIONS:
            result = self.synthesis_runtime().execute(kind, payload, cancel_event=cancel_event, validate=validate)
            result['native_verified'] = kind in self.verified_kinds
            return result
        if kind in NEW_ACTIONS:
            result = self.progression_runtime().execute(kind, payload, cancel_event=cancel_event, validate=validate)
            result['native_verified'] = kind in self.verified_kinds
            return result
        read, manager, area, scene = self._context()
        portal = read.singleton('TownPortalSystem')
        if read.boolean(portal+76) or read.boolean(area+176):
            raise GameBusyError('游戏正在切换场景，等待完成。')
        calls, selected = [], None
        if kind == 'return_town':
            if scene == 1:
                return {'completed':True, 'already_in_town':True, 'native_verified':kind in self.verified_kinds}
            if payload.get('reason_code') == 'stargazing_skip':
                self._require_stargazing_run(read, manager, payload.get('source_run_id'))
                travel_state = self._combat_travel_state(read, manager)
                if travel_state['travel_blocked']: raise GameBusyError(travel_state['travel_wait_reason'])
            if payload.get('protect_completion_rewards') is True:
                # Retain list/header guards through native submission as well:
                # late spawns after service preflight must cancel departure.
                if self._ground(read, manager):
                    raise GameBusyError('本局仍有地面奖励，先拾取后再回城。')
            calls = [self._return_town_call(read, manager, portal)]
        elif kind == 'switch_difficulty':
            self._require_travel_ready(read)
            self._require_empty_town(read,manager,scene)
            current,options = self._difficulty_state(read,manager)
            target = next((row for row in options if row['id']==payload.get('difficulty')),None)
            if not target or not target['unlocked']: raise NativeActionError('目标难度尚未解锁，不能自动切换')
            if current==target['id']: return {'effect_confirmed':True,'already_selected':True,'difficulty':current}
            calls = [Invocation('ey.dcq',arguments=(Argument.integer(target['value']),))]
        elif kind == 'enter_map':
            self._require_travel_ready(read)
            stargazing_entry = payload.get('reason_code') == 'stargazing_skip'
            if not stargazing_entry:
                self._require_empty_town(read,manager,scene)
            state = self.control_snapshot()
            if not state['available']:
                # This read precedes dispatch: a snapshot race has no action to
                # confirm and must be replanned, not treated as a native failure.
                if state.get('retryable') is True:
                    raise SnapshotChangedError(state['reason'])
                raise NativeActionError(state['reason'])
            if stargazing_entry:
                if (scene != 0 or (state.get('run') or {}).get('id') != payload.get('source_run_id')
                        or not payload.get('source_run_id') or state.get('dead') is True
                        or (state.get('run') or {}).get('status') not in ('running', 'settling')):
                    raise SnapshotChangedError('观星源关卡已变化，禁止局内切图。')
                self._require_stargazing_run(read,manager,payload['source_run_id'])
                travel_state = self._combat_travel_state(read, manager)
                if travel_state['travel_blocked']: raise GameBusyError(travel_state['travel_wait_reason'])
                if self._ground(read,manager):
                    raise SnapshotChangedError('地面已有实际奖励，禁止观星重开。')
                carriage = self._carriage(read,in_town=False)
                if carriage.get('complete') is not True or carriage.get('items') != []:
                    raise SnapshotChangedError('马车已有奖励或状态待核验，禁止观星重开。')
                if payload.get('map_id') == state.get('map_id'):
                    raise NativeActionError('游戏不允许局内直接重进同一张图。')
            key = payload.get('map_id')
            floor = floor_number(key)
            target_difficulty = payload.get('difficulty', state['difficulty'])
            if (target_difficulty==MYTHIC) != (floor is not None):
                raise NativeActionError('神话裂隙层数与目标类型不一致。')
            row = next((row for row in state.get('maps_by_difficulty',{}).get(target_difficulty,state['maps']) if row['map_id'] == key), None)
            if not row or not row['can_enter']:
                raise NativeActionError('目标关卡尚未解锁、缺少材料或资源未载入')
            if not floor and target_difficulty != state['difficulty']:
                raise NativeActionError('请先在游戏里切换难度；托管不会绕过难度解锁')
            if floor and (scene!=1 or stargazing_entry):
                raise NativeActionError('神话裂隙必须回城整理后进入。')
            native_key = MYTHIC_MAP if floor else key
            obj = self._map_objects(read, area, native_key)[native_key]
            read.read(obj+24, 24)  # prerequisite and availability flags
            if floor:
                if read.integer(obj+32)!=1: raise NativeActionError('神话裂隙地图类型未确认。')
                if read.boolean(manager+260): raise GameBusyError('游戏正在提交关卡请求，等待完成。')
                # Match MythicPortalInteractable: native unlock + material gates,
                # then the floor-aware GameManager entry. WaveManager consumes
                # entry materials when accepting the run, never deduct twice here.
                calls = [Invocation('GameManager.travel_busy',require_false=True),
                    Invocation('SaveSystem.mythic_unlocked',arguments=(Argument.reference(obj),Argument.integer(floor)),require_true=True),
                    Invocation('TownPortalSystem.entry_materials',arguments=(Argument.reference(obj),),require_true=True),
                    Invocation('GameManager.enter_mythic',manager,(Argument.reference(obj),Argument.integer(floor)))]
            else:
                calls = [Invocation('TownPortalSystem.fri', portal, (Argument.reference(obj),), require_true=True)]
        elif kind == 'collect_ground':
            rows = self._ground(read, manager, payload.get('selection_id'))
            selected = next((row for row in rows if row['selection_id'] == payload.get('selection_id')), None)
            if not selected:
                raise SnapshotChangedError('这件地面掉落已变化，等待下一轮读取')
            if selected.get('issues'):
                raise SnapshotChangedError('地面物品实际身份或品质未核验，等待下一轮读取')
            self._before_pickup(selected, read)
            calls = [Invocation('LootDrop.cwc', selected['_address'])]
        else:
            selected, slot, header, item, uid = self._selected_inventory(payload, read)
            index = selected['slot_index']
            if kind in ('sell', 'salvage'):
                self._disposition(selected, payload)
                if scene != 1:
                    raise NativeActionError('托管出售和分解只在回到主城后执行')
                if kind == 'salvage':
                    if not selected.get('is_equipment'):
                        raise NativeActionError('分解入口只接受装备')
                    calls = [Invocation('hm.ecc', arguments=(Argument.integer(index),))]
                else:
                    merchant = read.singleton('MerchantUI')
                    calls = [Invocation('MerchantUI.gzy', merchant, (Argument.integer(index), Argument(raw=b'\0'*8)))]
            elif kind == 'open_chest':
                if selected.get('item_type') != 2 or selected.get('internal_name') not in ('TreasureChest1', 'TreasureChest2', 'TreasureChest3'):
                    # These are verified consumable assets, not a ChestData class.
                    raise NativeActionError('原生开箱入口只接受已确认的宝箱')
                consumable = read.singleton('ConsumableItemSystem')
                calls = [Invocation('ConsumableItemSystem.eyu', consumable,
                    (Argument.integer(index), Argument.boolean(False)), require_true=True)]
            elif kind == 'equip':
                if divine_equipment(selected):
                    raise NativeActionError('神圣装备禁止自动穿戴，保留交易资格。')
                if not selected.get('is_equipment'):
                    raise NativeActionError('换装入口只接受装备')
                target = payload.get('equip_slot', payload.get('target_slot'))
                if type(target) is not int or not 0 <= target < 12:
                    raise NativeActionError('换装目标槽位无效')
                self._equip_level(read, manager, selected, item, target_slot=target)
                equipment = read.singleton('EquipmentManager')
                root = read.singleton('Inventory')
                from .recommendation_reader import _Copy
                copy = _Copy(self.reader)
                equipped = copy.dictionary(copy.ptr(equipment+40), 'pointer')
                uids = copy.dictionary(copy.ptr(equipment+48), 'pointer')
                replaced = self.reader.string(uids.get(target, 0)) or ''
                if 'expected_replaced_uid' not in payload or (payload['expected_replaced_uid'] or '') != replaced:
                    raise NativeActionError('当前穿戴装备已改变，需重新计算战力后再换装')
                selected['_replaced_uid'] = replaced
                read.guards.extend(copy.guards)
                copy.finish()
                calls = [
                    Invocation('EquipmentManager.ddq', arguments=(Argument.reference(item), Argument.integer(target)), require_true=True, mutates=False),
                    Invocation('EquipmentManager.ddt', arguments=(Argument.reference(item), Argument.reference(uid)), require_true=True, mutates=False),
                    Invocation('EquipmentManager.ddu', arguments=(Argument.reference(item),), require_true=True, mutates=False),
                    Invocation('EquipmentManager.ddv', arguments=(Argument.reference(uid),), require_false=True, mutates=False),
                    Invocation('EquipmentManager.ddw', equipment, (Argument.reference(item), Argument.reference(uid), Argument.integer(target)), require_false=True, mutates=False),
                    Invocation('EquipmentManager.deh', equipment, (Argument.integer(target),), mutates=False),
                    Invocation('EquipmentManager.ded', equipment, (Argument.reference(item), Argument.integer(target), Argument.reference(uid)), mutates=True),
                    Invocation('Inventory.fcj', root, (Argument.integer(index),), mutates=True),
                    Invocation('Inventory.fcc', root, (Argument.previous(6), Argument.integer(1), Argument.previous(5)), require_true=True, skip_null_first=True, mutates=True),
                ]
        read.finish()
        methods = [call.method for call in calls]
        item_log = ({k:selected.get(k) for k in ('internal_name', 'item_uid', 'count', 'selection_id')}
            if selected is not None else None)
        record('game_action_requested',kind=kind,methods=methods,
               map_id=payload.get('map_id'),difficulty=payload.get('difficulty'),item=item_log)
        try:
            result = self.dispatcher.invoke(calls, read.guards, cancel_event=cancel_event, validate=validate)
        except NativeActionError as exc:
            record('game_action_failed',kind=kind,methods=methods,error=str(exc),
                   uncertain=exc.uncertain,state=exc.state,item=item_log)
            raise
        # The native call can return before asynchronous travel/save concludes.
        result.update({'kind':kind, 'native_verified':kind in self.verified_kinds,
            'online_save_confirmed':False, 'effect_confirmed':False})
        if kind in ('return_town', 'enter_map'):
            result['transition_requested'] = True
            result['completed'] = False
        elif kind=='switch_difficulty':
            result['effect_confirmed'] = self._confirm_difficulty(payload['difficulty'])
            if not result['effect_confirmed']:
                raise NativeActionError('切换难度已请求，但实际难度未确认，已停止重复操作',uncertain=True)
        elif selected is not None:
            result['item'] = {k:v for k,v in selected.items() if not k.startswith('_')}
            result['effect_confirmed'] = self._confirm_effect(kind, selected, payload)
            result['quantity'] = selected.get('_confirmed_count', selected.get('count', 1))
            if not result['effect_confirmed']:
                record('game_action_unconfirmed',kind=kind,item=item_log,
                    receipt=selected.get('_pickup_receipt'))
                raise NativeActionError('游戏方法已执行，但物品结果尚未确认；已停止重复操作', uncertain=True)
            if '_confirmed_snapshot' in selected:
                result['_receipt_snapshot'] = selected['_confirmed_snapshot']
        return result

    def _confirm_difficulty(self, target):
        # Post-call confirmation may run during an online save. Read the actual
        # scene and difficulty, without treating that save as an action failure.
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            self.reader.check_game_files()
            read = _Read(self.reader,self.profile)
            manager,area = read.singleton('GameManager'),read.singleton('GameAreaManager')
            current,_ = self._difficulty_state(read,manager)
            town = read.integer(area+120)==1
            try: read.finish()
            except SnapshotChangedError:
                time.sleep(.05)
                continue
            if town and current==target: return True
            time.sleep(.05)
        return False

    def _virtual_balance(self, selected, read):
        """Copied receive-side balances for materials, gold and potion charges."""
        name = selected['internal_name']
        if name == 'HealthPotionDrop':
            system = read.singleton('HealthPotionSystem')
            charges = read.integer(system+72)
            if charges < 0: raise MemoryReadError('药水使用次数未确认')
            return {'charges':charges}
        if name == 'GoldDrop':
            data = read.singleton('PlayerData')
            from .experience_reader import decode_obscured_int
            return decode_obscured_int(read.read(data+48,16))
        from .recommendation_reader import _Copy
        copy = _Copy(self.reader)
        inventory = read.singleton('Inventory')
        amounts = copy.dictionary(copy.ptr(inventory+64),'int','object')
        count = sum(amount for item,amount in amounts.items() if self.reader.asset_name(item)==name)
        copy.finish(); read.guards.extend(copy.guards)
        return count

    def _before_pickup(self, selected, read):
        if selected.get('item_type')==1:
            selected['_before_virtual'] = self._virtual_balance(selected,read)
            return
        snapshot = self.reader.snapshot()
        if not snapshot.get('complete'): raise SnapshotChangedError('拾取前背包读取不完整。')
        rows = [r for c in snapshot['containers'].values() for r in c['slots']]
        selected['_before_total_count'] = sum(r.get('count',0) for r in rows
            if r.get('internal_name')==selected['internal_name'])
        if selected.get('item_uid') and any(r.get('item_uid')==selected['item_uid'] for r in rows):
            raise SnapshotChangedError('掉落唯一编号已在容器中，未重复拾取。')
        # Bind the receive-side container root, without guarding every unrelated
        # stored item. The selected drop's identity/count/consumed flag are
        # checked on the main thread, followed by conserved receive-side totals.
        inventory = read.singleton('Inventory')
        array = read.ptr(inventory+56)
        read.read(array+24,8)

    def _virtual_drop_consumed(self, selected, read):
        """Bind the native consumed flag to this exact gold or potion drop.

        Native pickup marks these drops consumed even at full potion charges.
        Auto-use or other coin pickups may change balances before our read.
        Disappearance alone is not a receipt; a recycled object cannot qualify.
        """
        before = selected.get('_header')
        flags = {'GoldDrop':(1, 0), 'HealthPotionDrop':(0, 1)}.get(selected.get('internal_name'))
        if flags is None or not isinstance(before, bytes) or len(before) < 186 or before[185] != 0:
            return False
        address = selected['_address']
        klass = read.ptr(address)
        read.klass(klass, 'LootDrop')
        return (struct.pack('<Q', klass) == before[:8]
            and read.ptr(address+16) in (0, struct.unpack_from('<Q', before, 16)[0])
            and tuple(before[132:134]) == flags
            and read.read(address+120, 33) == before[120:153]
            and read.read(address+164, 8) == before[164:172]
            and read.boolean(address+185))

    def _confirm_pickup(self, selected):
        # Receipt reads may overlap the save triggered by the accepted pickup.
        # They never submit another native call, including on a changing read.
        read, manager, _, _ = self._context(allow_busy=True)
        rows = self._ground(read, manager)
        remaining = next((r for r in rows if r.get('observation_id')==selected['observation_id']), None)
        removed = selected['count']-(remaining.get('count', 0) if remaining else 0)
        received = False
        evidence = {'remaining_count':remaining.get('count', 0) if remaining else 0}
        if 0 < removed <= selected['count']:
            if selected.get('item_type') == 1:
                balance = self._virtual_balance(selected, read)
                before = selected['_before_virtual']
                evidence.update(before=before, after=balance)
                if selected['internal_name'] == 'HealthPotionDrop':
                    refilled = balance['charges'] > before['charges']
                    consumed = self._virtual_drop_consumed(selected, read) if not refilled else False
                    evidence['consumed'] = consumed
                    received = remaining is None and (refilled or consumed)
                elif selected['internal_name'] == 'GoldDrop':
                    consumed = self._virtual_drop_consumed(selected, read)
                    evidence['consumed'] = consumed
                    # Other auto-picked coins can arrive in the same frame.
                    # Require this drop's consumed flag and at least its value,
                    # rather than an exact total-balance delta of one coin pile.
                    received = remaining is None and consumed and balance-before >= removed
                else:
                    received = balance-before == removed
            else:
                snapshot = self.reader.snapshot()
                evidence['inventory_complete'] = snapshot.get('complete') is True
                if snapshot.get('complete'):
                    owned = [r for c in snapshot['containers'].values() for r in c['slots']]
                    if selected.get('item_uid'):
                        received = any(r.get('item_uid')==selected['item_uid'] and r.get('internal_name')==selected['internal_name'] for r in owned)
                    else:
                        amount = sum(r.get('count', 0) for r in owned if r.get('internal_name')==selected['internal_name'])
                        received = amount-selected['_before_total_count'] == removed
        read.finish()
        selected['_pickup_receipt'] = evidence
        if received:
            selected['_confirmed_count'] = removed
        return received

    def _confirm_effect(self, kind, selected, payload):
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if kind == 'collect_ground':
                try:
                    if self._confirm_pickup(selected): return True
                except MemoryReadError as exc:
                    selected['_pickup_receipt'] = {'read_error':str(exc)}
            else:
                snapshot = self.reader.snapshot()
                if snapshot.get('complete'):
                    current = next((row for row in snapshot['containers']['inventory']['slots']
                        if row['slot_index'] == selected['slot_index']), None)
                    if kind == 'equip':
                        from .character_reader import read_character
                        state = read_character(self.reader)
                        rows = [row for container in snapshot['containers'].values() for row in container['slots']]
                        replaced = selected.get('_replaced_uid')
                        conserved = not replaced or any(row.get('item_uid')==replaced for row in rows)
                        if conserved and state['available'] and any(row.get('item_uid') == selected.get('item_uid')
                                and row.get('equipped_slot') == payload.get('equip_slot', payload.get('target_slot')) for row in state['equipment']):
                            return True
                    elif kind == 'open_chest':
                        if not current or current.get('internal_name') != selected['internal_name'] or current.get('count', 0) < selected['count']:
                            return True
                    else:
                        rows = [row for container in snapshot['containers'].values() for row in container['slots']]
                        if selected.get('item_uid'):
                            if not any(row.get('item_uid') == selected['item_uid'] for row in rows):
                                selected['_confirmed_snapshot'] = snapshot
                                return True
                        elif sum(row.get('count', 0) for row in rows if row.get('internal_name') == selected['internal_name']) < selected['_before_total_count']:
                            selected['_confirmed_snapshot'] = snapshot
                            return True
            time.sleep(.05)
        return False

    def close(self):
        self.dispatcher.close()
