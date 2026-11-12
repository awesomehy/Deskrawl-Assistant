"""UID-less commodity uploads use native stack consumption, never forged UIDs.

The operation ID below is a local transaction fence, not a game item identity.
Only the two mapped commodities use this path; other missing identities still
fail closed. The normal generated-item upload path remains separate.
"""
from copy import deepcopy
import re
import struct
import time

from .native_dispatcher import Argument, Invocation, NativeActionError, MAX_GUARDS
from .native_memory import MemoryReadError, SnapshotChangedError


COMMODITIES = frozenset(('GoldBar', 'UpgradeCommodity1'))


def is_commodity(row):
    return (row.get('internal_name', row.get('name_key')) in COMMODITIES
            and row.get('item_type') == 7 and row.get('base_rarity') == 3
            and not row.get('item_uid') and not row.get('instance_id'))


def commodity_candidate(row):
    evidence = row.get('read_evidence')
    return (is_commodity(row) and row.get('container') in ('inventory', 'storage')
            and type(row.get('count')) is int and row['count'] > 0
            and bool(row.get('transfer_key')) and not row.get('issues')
            and isinstance(evidence, dict) and evidence.get('snapshot_consistent') is True
            and row.get('steam_retired') is not True and row.get('is_equipped') is not True)


def operation_id(value):
    return isinstance(value, str) and re.fullmatch(r'commodity:[a-f0-9]{32}', value) is not None


def upload_reference(row):
    return row.get('item_uid') or row.get('uid') or (
        row.get('auction_operation_id') if operation_id(row.get('auction_operation_id')) else None)


def upload_receipt_matches(row, receipt):
    if row.get('auction_operation_id'):
        identity = (operation_id(row['auction_operation_id'])
                    and receipt.get('uploaded_operation_id') == row['auction_operation_id']
                    and not receipt.get('uploaded_uid')
                    and isinstance(receipt.get('uploaded_instance_id'), str)
                    and 0 < len(receipt['uploaded_instance_id']) <= 128
                    and (not row.get('instance_id') or row['instance_id'] == receipt['uploaded_instance_id']))
    else:
        identity = (bool(row.get('instance_id'))
                    and receipt.get('uploaded_instance_id') == row['instance_id']
                    and receipt.get('uploaded_uid') == (row.get('item_uid') or row.get('uid')))
    return bool(identity and receipt.get('uploaded_internal_name') == (
        row.get('internal_name') or row.get('item_key')))


def commodity_total(snapshot, name):
    if snapshot.get('complete') is not True:
        return None
    total = 0
    for kind in ('inventory', 'storage'):
        container = snapshot.get('containers', {}).get(kind)
        if not isinstance(container, dict) or container.get('complete') is False:
            return None
        for row in container.get('slots', []):
            if row.get('internal_name') != name:
                continue
            if (not is_commodity(row) or row.get('issues')
                    or type(row.get('count')) is not int or row['count'] <= 0):
                return None
            total += row['count']
    return total


def commodity_receipt(selected, state, snapshot):
    """Both a fresh server addition and exactly one native removal are needed."""
    checked = state.get('server_checked_at')
    submitted = selected.get('submitted_at')
    if (not operation_id(selected.get('auction_operation_id')) or state.get('busy')
            or state.get('needs_refresh') or state.get('available') is not True
            or state.get('steamid_verified') is not True or state.get('steamid') != selected.get('steamid')
            or type(checked) not in (int, float)
            or type(submitted) not in (int, float)
            or not 0 < submitted <= checked <= time.time() < checked + 30
            or not isinstance(state.get('listings'), list)):
        return None
    before_ids = selected.get('before_listing_ids')
    before_count = selected.get('before_count')
    if (not isinstance(before_ids, list) or type(before_count) is not int or before_count < 1
            or commodity_total(snapshot, selected['internal_name']) != before_count - 1):
        return None
    added = [row for row in state['listings'] if row.get('instance_id') not in before_ids]
    if (len(added) != 1 or not isinstance(added[0].get('instance_id'), str)
            or not 0 < len(added[0]['instance_id']) <= 128
            or added[0].get('internal_name') != selected['internal_name']
            or added[0].get('received') is not False or type(added[0].get('pending')) is not bool):
        return None
    listing = added[0]
    return {'uploaded_uid': None, 'uploaded_operation_id': selected['auction_operation_id'],
            'uploaded_instance_id': listing['instance_id'],
            'uploaded_internal_name': selected['internal_name'], 'quantity': 1,
            'listing': deepcopy(listing), 'steamid': selected['steamid'],
            'steam_pending': listing['pending'], 'listing_count': state.get('listing_count'),
            'receipt_id': 'auction:' + listing['instance_id'] + ':' + str(listing.get('listed_at'))}


class CommodityAuctionMixin:
    def _commodity_selected(self, payload, read):
        row, slot, header, item, uid = self.actions._selected_inventory(payload, read)
        if (not is_commodity(row) or not row.get('transfer_key') or row.get('issues')
                or type(row.get('count')) is not int or row['count'] < 1):
            raise NativeActionError('商品种类、数量或槽位身份已变化，未上传。')
        read.verify(item, 'ItemData')
        # Immutable resource gates match AuctionUploadSlotUI.ghx's UID-less
        # branch. Bound/upgrade/socket fields do not exist on these stacks.
        if read.integer(item + 32) != 7 or read.integer(item + 36) != 3 or read.boolean(item + 236):
            raise NativeActionError('该商品不符合游戏原生上传规则，未上传。')
        # Native Inventory.fnz consumes the first matching stack, not necessarily
        # the selected slot. Guard EVERY bag slot's item/count/UID, including
        # empty slots, against sorting, combining, or player selection changes.
        root = read.singleton('Inventory')
        array = read.ptr(root + 56)
        records, block = self.reader.array(array, 'InventorySlot', limit=2048, stride=8)
        read.read(array + 24, 8)
        read.guards.append((array + 32, block))
        for record in records:
            address = struct.unpack('<Q', record)[0]
            read.verify(address, 'InventorySlot')
            read.read(address + 16, 24)
        snapshot = self.reader.snapshot()
        before = commodity_total(snapshot, row['internal_name'])
        if before is None or before < row['count'] or before != row.get('_before_total_count'):
            raise SnapshotChangedError('商品总量在准备上传时变化，未提交。')
        read.finish()
        if sum((len(raw) + 7) // 8 for _, raw in set(read.guards)) > MAX_GUARDS:
            raise NativeActionError('商品背包复核范围超出安全上限，未提交上传。')
        return row, item, before

    def _execute_commodity(self, payload, *, cancel_event, validate):
        from .auction_runtime import AuctionUploadPending
        requested = payload['item']
        op = requested.get('auction_operation_id')
        if not operation_id(op) or payload.get('quantity', 1) != 1:
            raise NativeActionError('商品上传需要独立的本地操作记录且每次仅上传一个，未提交。')
        expected = payload.get('expected_steamid')
        if (expected is not None and (not isinstance(expected, str)
                or re.fullmatch(r'\d{17}', expected, flags=re.ASCII) is None)):
            raise NativeActionError('上传预期 Steam 账号格式无效；尚未提交。')
        self._open(cancel_event=cancel_event, validate=validate)
        read = self._context(); state = self._state(read)
        if (state.get('steamid_verified') is not True or not state.get('steamid')
                or expected is not None and state['steamid'] != expected):
            raise NativeActionError('游戏授权账号尚未确认或发生变化，未提交商品。')
        if (state.get('busy') or state.get('selection_occupied') or state.get('enabled') is not True
                or state.get('needs_refresh') or not isinstance(state.get('listings'), list)
                or type(state.get('listing_count')) is not int or not 0 <= state['listing_count'] < 8):
            raise NativeActionError('拍卖行正在同步、名额已满或已有玩家选择，未提交商品。')
        row, item, before = self._commodity_selected(payload, read)
        selected = {'auction_operation_id': op, 'item_uid': None, 'internal_name': row['internal_name'],
                    'steamid': state['steamid'], 'submitted_at': time.time(), 'before_count': before,
                    'before_listing_ids': [r['instance_id'] for r in state['listings']]}
        # Reuse the original SELECT + SUBMIT: its empty-UID branch validates
        # ItemData, removes ONE commodity, queues auctionList and saves natively.
        # No generated-item qualification call and no invented instance ID.
        calls = [Invocation('PanelBase.auction_is_open', instance=state['_ui'], require_true=True, mutates=False),
                 Invocation('AuctionUI.upload_select', instance=state['_ui'], require_true=True, mutates=False,
                            arguments=(Argument.reference(item), Argument.reference(0))),
                 Invocation('AuctionUI.upload_submit', instance=state['_ui'], mutates=True)]
        self._pending[op] = selected
        try:
            self._persist()
        except OSError as exc:
            self._pending.pop(op, None)
            raise NativeActionError('无法保存商品上传记录，未提交。') from exc
        try:
            self._active_submit = True
            invocation = self._invoke(calls, read, cancel_event=cancel_event, validate=validate)
        except (NativeActionError, SnapshotChangedError) as exc:
            if (isinstance(exc, SnapshotChangedError) or getattr(exc, 'mutation_started', None) is False
                    or isinstance(exc, NativeActionError) and not exc.uncertain and exc.mutation_started is not True):
                self._pending.pop(op, None); self._persist()
                raise NativeActionError(str(exc), mutation_started=False) from exc
            raise AuctionUploadPending() from exc
        self._refreshed = self._refresh_wait = None
        self._refreshed_at = 0
        try:
            receipt = self._confirm_commodity(selected, cancel_event=cancel_event, validate=validate)
        except NativeActionError as exc:
            status = exc.native_status if isinstance(exc, AuctionUploadPending) else ''
            self._pending[op]['last_status'] = status
            self._persist()
            raise AuctionUploadPending(status) from exc
        self._confirmed[op] = deepcopy(receipt)
        self._confirmed = dict(list(self._confirmed.items())[-256:])
        self._pending.pop(op, None)
        try:
            self._persist()
        except OSError:
            self._pending[op] = selected
            raise
        return dict(invocation, **receipt, kind='auction_upload', native_verified=True,
                    effect_confirmed=True, online_save_confirmed=False)

    def _confirm_commodity(self, selected, *, cancel_event, validate):
        from .auction_runtime import AuctionUploadPending
        deadline, refreshed, status = time.monotonic() + 20, False, ''
        while time.monotonic() < deadline:
            try:
                state = self.snapshot()
                status = state.get('native_status') or status
                receipt = self._confirmed.get(selected['auction_operation_id'])
                if receipt:
                    return deepcopy(receipt)
                # Wait for the original native save/auctionList to leave busy.
                # Then explicitly obtain a server callback newer than submit.
                if (not refreshed and state.get('available') is True and not state.get('busy')
                        and state.get('selection_occupied') is False):
                    self._open(cancel_event=cancel_event, validate=validate, refresh_only=True)
                    refreshed = True
                    state = self.snapshot()
                receipt = self._confirmed.get(selected['auction_operation_id'])
                if receipt:
                    return deepcopy(receipt)
                snapshot = self.reader.snapshot()
                receipt = commodity_receipt(selected, state, snapshot)
                if receipt:
                    return receipt
            except (MemoryReadError, ValueError, KeyError, struct.error):
                pass
            time.sleep(.2)
        raise AuctionUploadPending(status)
