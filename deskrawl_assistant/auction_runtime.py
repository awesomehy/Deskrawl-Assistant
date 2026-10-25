"""Build-gated uploads through AuctionUI's complete original callback chain.

This transfers an item to Steam inventory; it does not place a market order.
No CloudClient request, forged instance ID or automatic unlock is performed.
Opening and uploading use the supervised validation recorded in the build profile.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import struct
import time

from .native_dispatcher import Argument, Invocation, NativeActionError, REJECTED
from .native_memory import MemoryReadError, SnapshotChangedError, GameBusyError
from .paths import runtime_dir
from .progression_runtime import _GrowthCopy


KINDS = ('auction_open', 'auction_upload')
CAPACITY = 8


def auction_eligibility(row, *, equipped_uids=(), preliminary=False):
    """Conservative copy of ItemData.cge; native cge is checked again in-job.

    Missing fields fail closed. Valid server identities are currently required
    even for commodities; the legacy UID-less Steam exchange path is excluded.
    """
    def rejected(code, reason):
        return {'eligible': False, 'reason_code': code, 'reason': reason}
    if row.get('container') not in ('inventory', 'storage') or type(row.get('count')) is not int or row['count'] != 1:
        return rejected('single_item_required', '当前仅支持有独立身份的单件物品，堆叠材料暂时跳过。')
    if (not isinstance(row.get('item_uid'), str) or not row['item_uid']
            or not isinstance(row.get('instance_id'), str) or not row['instance_id']):
        return rejected('identity_unavailable', '该物品缺少原生 UID 或服务器实例编号，暂不自动上传。')
    verified_identity = row.get('identity_verified') is True or row.get('auction_identity_verified') is True
    if row.get('issues') or row.get('identity_verified') is False or not verified_identity:
        return rejected('identity_unverified', '物品身份读取未完整核验。')
    if row.get('item_uid') in equipped_uids or row.get('is_equipped') is True:
        return rejected('equipped', '已穿戴的物品不能自动上传。')
    # Auction uploads deliberately ignore locks; never mutate them to upload.
    if row.get('bound') is not False:
        return rejected('bound', '物品已绑定或绑定状态未确认。')
    if (type(row.get('upgrade_level')) is not int or row['upgrade_level'] != 0
            or type(row.get('socket_count')) is not int or row['socket_count'] != 0):
        return rejected('modified', '强化或带孔物品不能上传，或相关状态尚未确认。')
    item_type, rarity = row.get('item_type'), row.get('base_rarity')
    effect_ok = row.get('effect') == 1 or preliminary is True and 'effect' not in row
    retired_ok = row.get('steam_retired') is False or preliminary is True and 'steam_retired' not in row
    normal = ((item_type == 3 and rarity == 4)
              or (item_type == 2 and effect_ok and type(rarity) is int and rarity >= 3)
              or (item_type == 5 and rarity in (3, 5))
              or (item_type == 7 and rarity == 3))
    hidden = (item_type == 3 and rarity == 3 and type(row.get('equip_slot')) is int and row['equip_slot'] != 11
              and row.get('is_black_mist') is True and row.get('black_mist_revealed') is False)
    if not hidden and (not normal or not retired_ok):
        return rejected('not_tradable', '物品不符合游戏原生拍卖行的可交易规则。')
    return {'eligible': True, 'reason_code': '', 'reason': ''}


class AuctionRuntime:
    def __init__(self, actions, *, journal_path=None):
        self.actions, self.reader = actions, actions.reader
        self.journal_path = Path(journal_path) if journal_path is not None else runtime_dir() / 'auction-pending.json'
        self._pending, self._journal_error = {}, ''
        self._confirmed = {}
        self._refreshed = None
        self._refresh_wait = None
        self._active_submit = False
        try:
            if self.journal_path.exists():
                data = json.loads(self.journal_path.read_text(encoding='utf-8'))
                if (not isinstance(data, dict) or data.get('schema') != 1
                        or not isinstance(data.get('pending'), dict)
                        or any(not isinstance(k, str) or not k or not isinstance(v, dict)
                               for k, v in data['pending'].items())):
                    raise ValueError('拍卖行在途记录格式无效。')
                self._pending = data['pending']
                self._confirmed = data.get('confirmed', {})
                if not isinstance(self._confirmed, dict) or any(not isinstance(v, dict) for v in self._confirmed.values()):
                    raise ValueError('拍卖行回执记录格式无效。')
        except (OSError, ValueError, TypeError) as exc:
            self._journal_error = str(exc)

    def _persist(self):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal_path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump({'schema': 1, 'pending': self._pending, 'confirmed': self._confirmed}, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.journal_path)

    def _copy(self):
        copy = _GrowthCopy(self.reader)
        copy.types.update({name: dict(row, name=row.get('name', name))
                           for name, row in self.actions.profile['types'].items()})
        return copy

    def _context(self, *, after=False):
        read, manager, area, scene = self.actions._context(allow_busy=after)
        if scene != 1:
            raise NativeActionError('拍卖行自动上传仅在主城整理时执行。')
        save = read.singleton('SaveSystem')
        if read.integer(save + 32) != 1:
            raise NativeActionError('游戏拍卖行仅支持在线角色。')
        if read.boolean(read.singleton('TownPortalSystem') + 76) or read.boolean(area + 176):
            raise GameBusyError('等待场景切换结束后处理拍卖行。')
        if not after:
            if read.boolean(save + 192):
                raise GameBusyError('等待物品操作完成后处理拍卖行。')
            self.actions._require_travel_ready(read)
        return read

    def _listings(self, read, ui):
        rows = []
        for offset in (168, 176):
            pointer = read.ptr(ui + offset)
            if not pointer:
                raise MemoryReadError('拍卖行服务器列表尚未载入。')
            for obj in read.list(pointer, 'AuctionListing', limit=256):
                read.verify(obj, 'AuctionListing')
                instance = self.reader.string(read.ptr(obj + 16))
                if not instance or any(r['instance_id'] == instance for r in rows):
                    raise MemoryReadError('拍卖行物品实例缺失或重复。')
                rows.append({'instance_id': instance,
                             'internal_name': self.reader.string(read.ptr(obj + 24)),
                             'rarity': self.reader.string(read.ptr(obj + 32)),
                             'listed_at': struct.unpack('<q', read.read(obj + 72, 8))[0],
                             'cancel_in': read.integer(obj + 80),
                             'pending': read.boolean(obj + 84),
                             'received': read.boolean(obj + 85)})
        return rows

    def _list_versions(self, read, ui):
        versions = []
        for offset in (168, 176):
            pointer = read.ptr(ui + offset)
            if not pointer:
                raise MemoryReadError('拍卖行服务器列表尚未载入。')
            # Validate the real List<T> layout and retain its complete header
            # guard. Constructor-created empty lists are not server receipts.
            read.list(pointer, 'AuctionListing', limit=256, selected_only=True)
            versions.append((pointer, read.integer(pointer + 28)))
        return tuple(versions)

    def _state(self, read):
        ui = read.singleton('AuctionUI')
        busy, loading = read.boolean(ui + 192), read.boolean(ui + 194)
        # Field 193 belongs to a second native action path; do not race it.
        busy = busy or read.boolean(ui + 193)
        slot = read.verify(read.ptr(ui + 104), 'AuctionUploadSlotUI')
        selected = self.reader.string(read.ptr(slot + 72)) or ''
        selected_item = read.ptr(slot + 64)
        cloud = read.singleton('CloudClient')
        # cf.boq parses SessionResponse and copies response.steamId (+24) to
        # CloudClient +128. +120 is the auth token and is deliberately unread.
        steamid = self.reader.string(read.ptr(cloud + 128))
        verified_account = isinstance(steamid, str) and len(steamid) == 17 and steamid.isascii() and steamid.isdecimal()
        session = getattr(self.reader, 'session_id', None)
        key = (session, ui, steamid) if verified_account and isinstance(session, str) and session else None
        versions = self._list_versions(read, ui)
        waiting = self._refresh_wait
        # fwo(success)->fwg clears both lists, incrementing both _version
        # values even when the server returned zero entries. Failed requests
        # clear loading but never touch these versions (enabled starts True).
        response_ready = bool(key and waiting and waiting['key'] == key and not loading and not busy
            and all(now[0] == before[0] and now[1] != before[1]
                    for now, before in zip(versions, waiting['versions'])))
        known = bool(key and not loading and (self._refreshed == key or response_ready))
        listings = self._listings(read, ui) if known else None
        enabled = read.boolean(ui + 195) if known else None
        status = self.reader.string(read.ptr(ui + 200)) or ''
        return {'available': True, 'enabled': enabled, 'listings': listings,
                'listing_count': sum(row['received'] is False for row in listings) if listings is not None else None,
                'received_count': sum(row['received'] is True for row in listings) if listings is not None else None,
                'capacity': CAPACITY, 'listing_limit': CAPACITY,
                # PanelBase+40 is a separate UI property, not visibility.
                # activeSelf can only be queried in an explicit native action.
                'busy': busy or loading, 'loading': loading, 'opened': None,
                'needs_refresh': loading or listings is None,
                'selected_uid': selected, 'selection_occupied': bool(selected_item or selected),
                'pending': bool(self._pending) or bool(self._journal_error),
                'blocked_uids': [p.get('item_uid') for p in self._pending.values() if p.get('item_uid')],
                'steamid': steamid if verified_account else None, 'steamid_verified': verified_account,
                'account_reason': '' if verified_account else '游戏当前授权 Steam 账号尚未同步，不能推测账号。',
                'reason': self._journal_error or ('服务器拍卖行暂不可用。' if enabled is False else status if not known else ''),
                'native_verified': all(kind in self.actions.profile.get('verified_kinds', []) for kind in KINDS),
                'identityless_materials_supported': False,
                '_ui': ui, '_slot': slot, '_refresh_key': key, '_list_versions': versions,
                '_response_ready': response_ready}

    def snapshot(self):
        try:
            # Inspection is read-only even when the feature is gated off.
            read = self._context(after=True)
            state = self._state(read); read.finish()
            if state.get('_response_ready'):
                self._refreshed = state['_refresh_key']
            elif state.get('loading'):
                self._refreshed = None
            if self._pending and state.get('steamid_verified') is True and not state.get('busy'):
                snapshot = self.reader.snapshot()
                self._reconcile(state, snapshot)
                state['pending'] = bool(self._pending) or bool(self._journal_error)
                state['blocked_uids'] = [p.get('item_uid') for p in self._pending.values() if p.get('item_uid')]
            state['confirmed_uploads'] = deepcopy(list(self._confirmed.values()))
            for key in ('_ui', '_slot', '_refresh_key', '_list_versions', '_response_ready'):
                state.pop(key, None)
            return state
        except (MemoryReadError, ValueError, KeyError, UnicodeError, struct.error) as exc:
            return {'available': False, 'enabled': None, 'listings': None,
                    'listing_count': None, 'capacity': CAPACITY, 'listing_limit': CAPACITY,
                    'busy': False, 'needs_refresh': True, 'reason': str(exc),
                    'pending': bool(self._pending) or bool(self._journal_error),
                    'blocked_uids': [p.get('item_uid') for p in self._pending.values() if p.get('item_uid')],
                    'steamid': None, 'steamid_verified': False, 'native_verified': False}

    def identity_snapshot(self):
        """Fresh guarded account/character identity, including outside town.

        This does not inspect, open or cache AuctionUI, and never reads the
        CloudClient token. Callers must explicitly opt into the market feature
        before requesting a snapshot; constructing GameActions performs none.
        """
        try:
            session = getattr(self.reader, 'session_id', None)
            if not isinstance(session, str) or not session:
                raise MemoryReadError('游戏读取会话尚未确认。')
            read, manager, area, scene = self.actions._context(allow_busy=True)
            save = read.singleton('SaveSystem')
            mode, slot = struct.unpack('<ii', read.read(save + 32, 8))
            if mode != 1 or not 0 <= slot < 100:
                raise MemoryReadError('当前不是已进入的在线角色，不能核验 Steam 账号。')
            if read.boolean(read.singleton('TownPortalSystem') + 76) or read.boolean(area + 176):
                raise GameBusyError('等待传送和场景切换结束后核验 Steam 账号。')
            data = read.singleton('PlayerData')
            label = self.reader.string(read.ptr(data + 40)) or ''
            if not isinstance(label, str):
                raise MemoryReadError('当前角色标识未完整读取。')
            cloud = read.singleton('CloudClient')
            steamid = self.reader.string(read.ptr(cloud + 128))
            if (not isinstance(steamid, str) or len(steamid) != 17
                    or not steamid.isascii() or not steamid.isdecimal()):
                raise MemoryReadError('游戏当前授权 Steam 账号尚未同步，不能推测账号。')
            read.finish()
            if getattr(self.reader, 'session_id', None) != session:
                raise SnapshotChangedError('游戏读取会话已变化，请重新核验 Steam 账号。')
            return {'available': True, 'steamid': steamid, 'steamid_verified': True,
                    'character_id': f'{mode}:{slot}:{label}', 'session': session,
                    'scene': {0: 'combat', 1: 'town', 2: 'fishing'}[scene],
                    'reason': '', 'source': 'verified_game_steam_identity'}
        except (MemoryReadError, OSError, ValueError, KeyError, UnicodeError, struct.error) as exc:
            # No cached identity is returned on transition, disconnection or
            # guard failure. A previous account must never authorize a sale.
            return {'available': False, 'steamid': None, 'steamid_verified': False,
                    'character_id': None, 'session': None, 'scene': None,
                    'reason': str(exc), 'source': 'verified_game_steam_identity'}

    def _invoke(self, calls, read, copy=None, *, cancel_event=None, validate=lambda: True):
        read.finish()
        if copy is not None:
            copy.finish()
        guards = list(dict.fromkeys(read.guards + (copy.action_guards if copy is not None else [])))
        return self.actions.dispatcher.invoke(calls, guards, cancel_event=cancel_event,
                                               timeout=5, validate=validate)

    def _open(self, *, cancel_event=None, validate=lambda: True):
        read = self._context()
        state = self._state(read)
        if state['busy']:
            raise GameBusyError('游戏拍卖行正在同步，请稍后重读。')
        if state['selection_occupied']:
            raise GameBusyError('拍卖行槽中已有玩家选择的物品，请先完成或清空。')
        self._refreshed = None
        self._refresh_wait = None
        try:
            # Only an explicit action installs the main-thread dispatcher.
            # The native Boolean query is hfz()->GameObject.activeSelf; +40
            # and +41 are not substitutes for this query.
            try:
                self._invoke([Invocation('PanelBase.auction_is_open', instance=state['_ui'],
                                         require_true=True, mutates=False)], read,
                             cancel_event=cancel_event, validate=validate)
                method = 'AuctionUI.refresh_list'
            except NativeActionError as exc:
                if (exc.state != REJECTED or exc.last_call != 0
                        or exc.mutation_started is not False or exc.uncertain):
                    raise
                method = 'AuctionUI.Show'
            read = self._context(); state = self._state(read)
            if state['busy'] or state['selection_occupied']:
                raise GameBusyError('游戏拍卖行状态已变化，未请求刷新。')
            self._refresh_wait = {'key': state['_refresh_key'], 'versions': state['_list_versions']}
            self._invoke([Invocation(method, instance=state['_ui'], mutates=False)], read,
                         cancel_event=cancel_event, validate=validate)
            return self._wait_open(cancel_event, validate)
        except Exception:
            self._refreshed = None
            raise
        finally:
            self._refresh_wait = None

    def _wait_open(self, cancel_event, validate):
        deadline = time.monotonic() + 8
        last = '等待服务器拍卖行列表。'
        while time.monotonic() < deadline:
            if (cancel_event is not None and cancel_event.is_set()) or not validate():
                raise NativeActionError('拍卖行同步已取消；尚未上传物品。')
            state = self.snapshot()
            if (state.get('available') is True and not state.get('busy') and not state.get('needs_refresh')
                    and type(state.get('enabled')) is bool and isinstance(state.get('listings'), list)):
                return dict(state, kind='auction_open', effect_confirmed=True)
            last = state.get('reason') or last
            time.sleep(.15)
        raise GameBusyError('拍卖行服务器列表同步超时；尚未上传物品。' + last)

    def _selected(self, payload, read):
        row, slot, header, item, uid = self.actions._selected_inventory(payload, read)
        registry, stamp = self.reader.registry()
        generated = read.verify(registry.get(row.get('item_uid')), 'GeneratedItemData')
        read.guards.append((stamp['object'] + 24, stamp['header']))
        # cge reads immutable ItemData and mutable GeneratedItemData fields.
        read.read(generated, 128)
        row['identity_verified'] = True  # fresh slot + registry + generated read above
        row.update(effect=read.integer(item + 52), steam_retired=read.boolean(item + 204))
        copy = self._copy()
        equipment = copy.singleton(self.actions.profile['singletons']['EquipmentManager'], 'EquipmentManager')
        rune = copy.singleton(self.actions.profile['singletons']['RuneManager'], 'RuneManager')
        equipped = {self.reader.string(p) for p in copy.dictionary(copy.ptr(equipment + 48), 'pointer').values()}
        equipped.update(self.reader.string(struct.unpack('<Q', raw)[0])
                        for raw in copy.array(copy.ptr(rune + 56), 8, 'String', limit=16))
        policy = auction_eligibility(row, equipped_uids=equipped)
        if not policy['eligible']:
            raise NativeActionError(policy['reason'])
        return row, item, uid, generated, copy

    def execute(self, kind, payload, *, cancel_event=None, validate=lambda: True):
        self._active_submit = False
        try:
            return self._execute(kind, payload, cancel_event=cancel_event, validate=validate)
        except NativeActionError as exc:
            if not self._active_submit and not exc.uncertain:
                exc.mutation_started = False
            raise
        except (MemoryReadError, OSError, ValueError, KeyError, UnicodeError, struct.error) as exc:
            raise NativeActionError(str(exc), uncertain=self._active_submit,
                                    mutation_started=True if self._active_submit else False) from exc

    def _execute(self, kind, payload, *, cancel_event=None, validate=lambda: True):
        if kind not in KINDS:
            raise NativeActionError('未知拍卖行动作。')
        if self._journal_error:
            raise NativeActionError('拍卖行在途记录无法读取，原文件保留；禁止提交操作。', uncertain=True)
        if kind == 'auction_open':
            return self._open(cancel_event=cancel_event, validate=validate)
        if self._pending:
            raise NativeActionError('上一次拍卖行上传仍未核验，禁止重复提交；请先检查游戏拍卖行与 Steam 库存。', uncertain=True)
        requested = payload.get('item', payload.get('row', payload))
        quantity = payload.get('quantity', 1)
        if type(quantity) is not int or quantity != 1:
            raise NativeActionError('原生上传当前仅支持明确的单件数量，堆叠数量语义尚未实机核验。')
        if not requested.get('item_uid') or not requested.get('instance_id'):
            raise NativeActionError('该物品缺少原生 UID 或服务器实例编号；不会为上传伪造身份。')
        expected_steamid = payload.get('expected_steamid')
        if expected_steamid is not None and (not isinstance(expected_steamid, str)
                or len(expected_steamid) != 17 or not expected_steamid.isascii()
                or not expected_steamid.isdecimal()):
            raise NativeActionError('上传预期 Steam 账号格式无效；尚未提交。')
        self._open(cancel_event=cancel_event, validate=validate)
        # Opening fetches asynchronously. Re-read everything after its callback.
        read = self._context(); state = self._state(read)
        if state.get('steamid_verified') is not True or not state.get('steamid'):
            raise NativeActionError('游戏当前授权 Steam 账号尚未核验；未提交上传，请等待账号同步。')
        if expected_steamid is not None and state.get('steamid') != expected_steamid:
            raise NativeActionError('游戏当前授权 Steam 账号已变化或尚未核验；未提交上传，请重新读取账号。')
        if state['busy'] or state['needs_refresh'] or state['enabled'] is not True:
            raise GameBusyError('等待游戏拍卖行启用并完成服务器同步；尚未上传。')
        if state['listing_count'] >= CAPACITY:
            raise NativeActionError('游戏拍卖行同时上传名额已满，本轮跳过。')
        if state['selection_occupied']:
            raise GameBusyError('拍卖行槽中已有其他物品，未改变玩家选择。')
        row, item, uid, generated, copy = self._selected(payload, read)
        instance = row['instance_id']
        if any(p['instance_id'] == instance for p in state['listings']):
            raise NativeActionError('该实例已存在于游戏拍卖行，禁止重复上传。', uncertain=True)
        calls = [Invocation('PanelBase.auction_is_open', instance=state['_ui'], require_true=True, mutates=False),
                 Invocation('ItemData.auction_tradable', instance=item,
                            arguments=(Argument.reference(generated),), require_true=True, mutates=False),
                 Invocation('AuctionUI.upload_select', instance=state['_ui'],
                            arguments=(Argument.reference(item), Argument.reference(uid)), require_true=True, mutates=False),
                 Invocation('AuctionUI.upload_submit', instance=state['_ui'], mutates=True)]
        read.finish(); copy.finish()
        # Durable intention precedes the native submit, including process exit.
        self._pending[instance] = {'item_uid': row['item_uid'], 'internal_name': row['internal_name'],
                                   'steamid': state['steamid']}
        try:
            self._persist()
        except OSError as exc:
            del self._pending[instance]
            raise NativeActionError('无法保存拍卖行在途记录，未提交上传。') from exc
        try:
            self._active_submit = True
            invocation = self._invoke(calls, read, copy, cancel_event=cancel_event, validate=validate)
        except NativeActionError as exc:
            if exc.mutation_started is False or (not exc.uncertain and exc.mutation_started is not True):
                del self._pending[instance]; self._persist()
                exc.mutation_started = False
            else:
                exc.uncertain = True
            raise
        except SnapshotChangedError as exc:
            # _invoke checks its copies before publishing any mailbox job.
            del self._pending[instance]; self._persist()
            raise NativeActionError(str(exc), mutation_started=False) from exc
        receipt = self._confirm(row)
        receipt['steamid'] = state['steamid']
        self._confirmed[instance] = deepcopy(receipt)
        self._confirmed = dict(list(self._confirmed.items())[-256:])
        pending = self._pending.pop(instance)
        try:
            self._persist()
        except OSError:
            self._pending[instance] = pending
            raise
        return dict(invocation, **receipt, kind=kind,
                    native_verified=kind in self.actions.profile.get('verified_kinds', []),
                    effect_confirmed=True, online_save_confirmed=False)

    @staticmethod
    def _receipt(selected, state, snapshot):
        if (snapshot.get('complete') is not True or state.get('busy')
                or not isinstance(state.get('listings'), list)):
            return None
        listing = next((p for p in state['listings'] if p['instance_id'] == selected['instance_id']), None)
        present = any(r.get('item_uid') == selected['item_uid'] or r.get('instance_id') == selected['instance_id']
                      for c in snapshot['containers'].values() for r in c['slots'])
        if (not listing or listing.get('internal_name') != selected['internal_name'] or present
                or listing.get('received') is not False):
            return None
        return {'uploaded_instance_id': selected['instance_id'], 'uploaded_uid': selected['item_uid'],
                'uploaded_internal_name': selected['internal_name'], 'listing': deepcopy(listing),
                'steamid': state.get('steamid'), 'steam_pending': listing['pending'],
                'listing_count': state['listing_count'],
                'receipt_id': f"auction:{selected['instance_id']}:{listing['listed_at']}"}

    def _reconcile(self, state, snapshot):
        """Recover only exact same-account receipts; absence alone proves nothing."""
        changed = False
        for instance, row in list(self._pending.items()):
            if not row.get('steamid') or row['steamid'] != state.get('steamid'):
                continue
            receipt = self._receipt({**row, 'instance_id': instance}, state, snapshot)
            if receipt:
                self._confirmed[instance] = receipt
                del self._pending[instance]
                changed = True
        if changed:
            # Keep recent receipts across the hand-off/crash boundary. They
            # authorize mapping only existing coordinator intents, never sales by name.
            self._confirmed = dict(list(self._confirmed.items())[-256:])
            try:
                self._persist()
            except OSError:
                self._journal_error = '拍卖行回执无法保存，请检查本地存储后继续核验。'
                raise

    def _confirm(self, selected):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                read = self._context(after=True); state = self._state(read)
                snapshot = self.reader.snapshot()
                read.finish()
                receipt = self._receipt(selected, state, snapshot)
                if receipt:
                    return dict(receipt, listings=deepcopy(state['listings']))
            except (MemoryReadError, ValueError, KeyError, struct.error):
                pass
            time.sleep(.2)
        raise NativeActionError('拍卖行上传已提交，但服务器列表与原生背包移除结果尚未同时确认；已禁止重复上传。', uncertain=True)
