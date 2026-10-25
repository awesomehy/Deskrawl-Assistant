"""Explicit selected-item auction batches, isolated from managed permissions."""
from copy import deepcopy
import secrets
import re
import threading
import time

from .auction import CATEGORIES, key
from .auction_runtime import auction_eligibility
from .models import ValidationError


def _rows(snapshot):
    return [row for name in ('inventory', 'storage')
            for row in snapshot.get('containers', {}).get(name, {}).get('slots', [])]


def _identity(row):
    return (row.get('item_uid'), row.get('instance_id'), key(row))


def _position(row):
    return (*_identity(row), row.get('container'), row.get('slot_index'), row.get('count'))


def _verified(row):
    row = deepcopy(row)
    evidence = row.get('read_evidence', {})
    row['auction_identity_verified'] = (row.get('identity_verified') is True
        or row.get('identity_verified') is not False and evidence.get('snapshot_consistent') is True
        and bool(row.get('item_uid')) and bool(row.get('instance_id')))
    return row


class ManualAuctionServiceMixin:
    def manual_auction_availability(self):
        """UI gate from the read worker's cache; never attach/read on HTTP GET."""
        def blocked(reason):
            return {'available': False, 'reason': reason}
        try:
            self._manual_auction_gate()
        except ValidationError as exc:
            return blocked(str(exc))
        if (self.snapshot or {}).get('complete') is not True:
            return blocked('物品尚未读取完整，请先刷新物品。')
        read_at = getattr(self, 'managed_control_read_at', None)
        if read_at is None or not 0 <= time.monotonic() - read_at <= 10:
            return blocked('当前游戏状态尚未确认，请等待重新读取。')
        control = self.managed_control
        if control.get('available') is not True:
            return blocked('当前游戏状态尚未确认，请等待重新读取。')
        scene = control.get('scene')
        context = {**control, **scene} if isinstance(scene, dict) else control
        if context.get('available') is not True:
            return blocked('当前游戏状态尚未确认，请等待重新读取。')
        in_town = scene.get('in_town') if isinstance(scene, dict) else scene == 'town'
        if in_town is not True:
            return blocked('手动上架仅支持在主城操作，请先回城。')
        if any(context.get(key) is True for key in ('scene_changing', 'teleporting')):
            return blocked('场景切换或传送中，请等待到达主城后再上架。')
        pending = context.get('online_pending_requests')
        if context.get('online_saving') is True or type(pending) is int and pending > 0:
            return blocked('游戏正在处理在线请求，请稍后再上架。')
        if (any(context.get(key) is not False for key in ('scene_changing', 'teleporting', 'online_saving'))
                or type(pending) is not int or pending != 0):
            return blocked('当前游戏状态尚未确认，请等待重新读取。')
        if control.get('dead') is True or control.get('in_battle') is True:
            return blocked('角色当前状态不允许手动上架，请等待恢复。')
        if not str(control.get('character_id') or '').startswith('1:'):
            return blocked('手动上架仅支持已进入的在线角色。')
        capability = self.managed_capabilities.get('auction_upload', {})
        if not isinstance(capability, dict) or capability.get('available') is not True:
            return blocked('游戏拍卖行操作尚未核验，请等待重新读取。')
        return {'available': True, 'reason': ''}

    def _manual_auction_gate(self):
        # Caller holds only service.guard. No game/network calls here.
        if self.closed.is_set() or self.updating or self.game_actions_blocked():
            raise ValidationError('助手正在退出、更新或切换游戏，请稍后再试。')
        if not self.client.connected:
            raise ValidationError('请先连接游戏。')
        if self.managed.running or self.managed.finishing:
            raise ValidationError('请先暂停托管，再批量上架所选物品。')
        if self.busy:
            raise ValidationError('当前操作尚未完成，请稍后再试。')

    def _manual_auction_identity(self):
        identity = self.client.steam_identity_snapshot()
        if (identity.get('available') is not True or identity.get('steamid_verified') is not True
                or not identity.get('steamid') or not identity.get('session') or not identity.get('character_id')):
            raise ValidationError('当前在线角色和 Steam 账号尚未核验，请重新读取游戏。')
        if identity.get('scene') != 'town':
            raise ValidationError('批量上架仅支持在主城操作，请先回城。')
        return {field: identity[field] for field in ('steamid', 'session', 'character_id')}

    def _manual_auction_capability(self):
        capability = self.client.managed_capabilities().get('auction_upload', {})
        if capability.get('available') is not True:
            raise ValidationError(capability.get('reason') or '当前版本的原生拍卖上传尚未开放。')

    def _manual_auction_reason(self, row, snapshot):
        policy = auction_eligibility(_verified(row), preliminary=True)
        if not policy['eligible']:
            return policy['reason']
        catalog = self._market_catalog()
        if not catalog or not catalog.resolve(row)[0]:
            return '该物品的 Steam 商品映射尚未核验，暂时跳过。'
        if row.get('container') == 'storage':
            transfer = snapshot.get('transfer', {})
            if (transfer.get('available') is not True or not row.get('transfer_key')
                    or row.get('slot_index') not in transfer.get('storage_indices', [])):
                return '所在仓库页未解锁或搬运条件尚未核验。'
        coordinator = getattr(self, 'auction_coordinator', None)
        if coordinator:
            with coordinator.guard:
                if coordinator.journal_error:
                    return '上传记录无法读取，暂不重复提交。'
                if any(intent.get('uid') == row.get('item_uid')
                       for intent in coordinator.intents.values()):
                    return '该物品已有上传记录，请先核验游戏拍卖行与 Steam 库存。'
        return ''

    def manual_auction_preview(self, payload):
        # Accept the retired boolean from older pages, without restoring a lock gate.
        if (not isinstance(payload, dict) or set(payload) - {'items', 'mode', 'price_rule', 'allow_locked'}
                or payload.get('mode') not in ('upload', 'sell')
                or payload.get('price_rule', 'lowest_sell') not in ('lowest_sell', 'highest_buy')
                or type(payload.get('allow_locked', False)) is not bool):
            raise ValidationError('批量上架参数无效。')
        ids = payload.get('items')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 250
                or any(not isinstance(value, str) or not 0 < len(value) <= 500 for value in ids)
                or len(set(ids)) != len(ids)):
            raise ValidationError('请勾选要上架的物品；每次最多 250 件，不可重复选择。')
        with self.guard:
            self._manual_auction_gate()
            snapshot = deepcopy(self.snapshot)
        if not snapshot or snapshot.get('complete') is not True:
            raise ValidationError('物品清单尚未完整读取，请刷新后重新选择。')
        identity = self._manual_auction_identity()
        self._manual_auction_capability()
        if snapshot.get('bridge_session') != identity['session']:
            raise ValidationError('角色连接已变化，请刷新后重新选择。')
        indexed = {}
        for row in _rows(snapshot):
            token = row.get('transfer_key') or row.get('instance_id')
            if token:
                if token in indexed:
                    raise ValidationError('物品选择编号重复，请刷新后重新选择。')
                indexed[token] = row
        if any(value not in indexed for value in ids):
            raise ValidationError('所选物品已变化或移动，请刷新后重新选择。')
        prepared, public = [], []
        for token in ids:
            row = indexed[token]
            reason = self._manual_auction_reason(row, snapshot)
            label = self._describe_item(key(row))[0]
            public.append({'id': token, 'name': label, 'item_key': key(row), 'count': row.get('count', 1),
                'container': row.get('container'), 'status': 'skipped' if reason else 'ready', 'reason': reason})
            if not reason:
                prepared.append({'row': deepcopy(row), 'public_index': len(public) - 1})
        # A selected bag item can free space before a selected storage item.
        prepared.sort(key=lambda entry: entry['row'].get('container') != 'inventory')
        token = secrets.token_urlsafe(32)
        with self.guard:
            self._manual_auction_gate()
            if self.snapshot is None or self.snapshot.get('bridge_session') != identity['session']:
                raise ValidationError('角色连接已变化，请刷新后重新选择。')
            self.manual_auction_preview_value = {'token': token, 'expires': time.monotonic() + 120,
                'identity': identity, 'client': self.client, 'rows': prepared, 'items': public,
                'mode': payload['mode'], 'price_rule': payload.get('price_rule', 'lowest_sell')}
        return {'token': token, 'mode': payload['mode'], 'price_rule': payload.get('price_rule', 'lowest_sell'),
            'items': public, 'ready_count': len(prepared), 'skipped_count': len(public) - len(prepared),
            'can_start': bool(prepared), 'message': '仅处理本次勾选的具体物品，不修改托管的类别、保留名单和设置。'}

    def manual_auction_state(self):
        with self.guard:
            return deepcopy(getattr(self, 'manual_auction_value', {
                'running': False, 'phase': 'idle', 'message': '', 'completed': 0,
                'total': 0, 'items': [], 'can_cancel': False}))

    def manual_auction_start(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {'token'} or not isinstance(payload['token'], str)
                or re.fullmatch(r'[A-Za-z0-9_-]{43}', payload['token']) is None):
            raise ValidationError('请先核验所选商品并确认本次批量上架。')
        with self.guard:
            self._manual_auction_gate()
            plan = getattr(self, 'manual_auction_preview_value', None)
            if (not plan or not secrets.compare_digest(plan['token'], payload['token'])
                    or time.monotonic() >= plan['expires']):
                raise ValidationError('确认清单已失效，请重新核验所选物品。')
            self.manual_auction_preview_value = None  # one confirmation, one run
            if not plan['rows'] or self.client is not plan['client']:
                raise ValidationError('没有可上架物品，或游戏连接已变化，请重新核验。')
            self.manual_auction_value = {'running': True, 'phase': 'preparing',
                'batch_id': secrets.token_hex(16),
                'message': '正在复核所选物品和当前角色…', 'completed': 0, 'total': len(plan['rows']),
                'can_cancel': True, 'mode': plan['mode'],
                'items': [{**row, 'status': 'queued' if row['status'] == 'ready' else 'skipped',
                           'message': row['reason']} for row in plan['items']]}
            self.busy = True
            self.operation_kind = 'manual_auction'
            self.cancelled.clear()
            self.error = ''
            self.message = '正在批量上架所选物品…'
            self.worker = threading.Thread(target=self._manual_auction_work, args=(plan,), daemon=True)
            self.worker.start()
        return self.manual_auction_state()

    def manual_auction_cancel(self):
        with self.guard:
            if getattr(self, 'manual_auction_value', {}).get('running'):
                self.cancelled.set()
                self.manual_auction_value.update(can_cancel=False, phase='cancelling',
                    message='已停止后续提交，正在核验在途操作；已取出的物品保留在背包。')
        return self.manual_auction_state()

    def _manual_auction_active(self, plan, *, identity=False):
        with self.guard:
            valid = (not self.cancelled.is_set() and not self.closed.is_set() and not self.updating
                and self.client is plan['client'] and self.client.connected
                and not self.managed.running and not self.game_actions_blocked())
        if valid and identity:
            try:
                valid = self._manual_auction_identity() == plan['identity']
            except Exception:
                valid = False
        return valid

    def _manual_auction_update(self, index, status, message):
        with self.guard:
            self.manual_auction_value['items'][index].update(status=status, message=message)
            self.manual_auction_value['message'] = message

    def _manual_auction_fresh(self, plan):
        if not self._manual_auction_active(plan, identity=True):
            raise ValidationError('操作已取消，或角色、账号、连接状态已经改变，未提交后续物品。')
        snapshot = self.client.snapshot()
        if snapshot.get('complete') is not True or snapshot.get('bridge_session') != plan['identity']['session']:
            raise ValidationError('物品或角色正在变化，请刷新后重新选择。')
        return snapshot

    @staticmethod
    def _manual_auction_find(snapshot, expected, *, position=True):
        found = [row for row in _rows(snapshot) if _identity(row) == _identity(expected)]
        if len(found) != 1 or position and _position(found[0]) != _position(expected):
            raise ValidationError('所选物品已变化或移动，未替换为其他同名物品；请刷新后重新选择。')
        return found[0]

    def _manual_auction_native(self, plan, coordinator):
        native = coordinator.observe(self.client.auction_state())
        with coordinator.guard:
            if any(row.get('steamid') == plan['identity']['steamid']
                   and row.get('phase') in ('submitted', 'uncertain', 'confirmed')
                   for row in coordinator.intents.values()):
                raise ValidationError('已有上传尚待完整核验，暂不上传本批次物品。')
        if native.get('available') is not True or native.get('steamid_verified') is not True:
            raise ValidationError('游戏拍卖行或当前账号尚未读取，请稍后重新核验。')
        if native.get('steamid') != plan['identity']['steamid']:
            raise ValidationError('游戏账号发生变化，未上传后续物品。')
        if native.get('pending') or native.get('selection_occupied') or native.get('busy'):
            raise ValidationError('游戏拍卖行存在在途上传、待处理选择或同步请求，请核验后再试。')
        if native.get('needs_refresh') or type(native.get('listing_count')) is not int or native['listing_count'] >= 8:
            self.client.execute_game_action('auction_open', {}, self.cancelled,
                validate=lambda: self._manual_auction_active(plan))
            native = coordinator.observe(self.client.auction_state())
        if (native.get('available') is not True or native.get('steamid_verified') is not True
                or native.get('steamid') != plan['identity']['steamid'] or native.get('pending')
                or native.get('selection_occupied')):
            raise ValidationError('刷新后拍卖行账号或在途状态发生变化，未上传物品。')
        if native.get('enabled') is not True or native.get('needs_refresh') or native.get('busy'):
            raise ValidationError('游戏拍卖行尚未确认可用，未上传物品。')
        count = native.get('listing_count')
        limit = native.get('listing_limit', 8)
        if type(count) is not int or type(limit) is not int or not 0 <= count < limit <= 8:
            raise ValidationError('游戏拍卖行名额已满或尚未确认，剩余物品未上传。')
        return native

    def _manual_auction_one(self, plan, entry, coordinator):
        index, expected = entry['public_index'], entry['row']
        self._manual_auction_update(index, 'running', '正在核验商品并上传…')
        snapshot = self._manual_auction_fresh(plan)
        row = self._manual_auction_find(snapshot, expected)
        reason = self._manual_auction_reason(row, snapshot)
        if reason:
            self._manual_auction_update(index, 'skipped', reason)
            return
        self._manual_auction_native(plan, coordinator)
        if row['container'] == 'storage':
            bag = snapshot['containers'].get('inventory', {})
            capacity, occupied = bag.get('slot_count'), bag.get('occupied_count')
            if type(capacity) is not int or type(occupied) is not int or capacity <= occupied:
                self._manual_auction_update(index, 'skipped', '背包没有可用空格，仓库物品保留原处。')
                return
        steamid = plan['identity']['steamid']
        ticket = None
        if plan['mode'] == 'sell':
            try:
                ticket = coordinator.trading.prepare_upload(steamid, key(row),
                    'black_mist' if row.get('is_black_mist') is True else 'normal', manual=True)
            except Exception:
                raise ValidationError('Steam 登录、账号或库存基线未确认，尚未上传该物品。') from None
        if not self._manual_auction_active(plan, identity=True):
            raise ValidationError('操作已取消或账号已改变，未上传该物品。')
        def upload_authorized():
            return self._manual_auction_active(plan) and (ticket is None
                or coordinator.trading.upload_binding_current(ticket, steamid))
        if not upload_authorized():
            raise ValidationError('Steam 绑定已经变化，未上传该物品；请重新登录并核验。')
        if row['container'] == 'storage':
            self.client.move_items([row], 'inventory', lambda: self._manual_auction_active(plan))
            snapshot = self._manual_auction_fresh(plan)
            row = self._manual_auction_find(snapshot, expected, position=False)
            if row.get('container') != 'inventory' or row.get('count') != expected.get('count'):
                raise ValidationError('所选物品搬运结果未确认，未上传；请核对背包。')
        else:
            row = self._manual_auction_find(self._manual_auction_fresh(plan), expected)
        # Save intent before the native submit. An unknown receipt blocks all
        # retries; cancellation after submission never erases that intent.
        intent = coordinator.submitted(row, steamid, ticket)
        try:
            result = self.client.execute_game_action('auction_upload', {
                'item': row, 'expected_steamid': steamid, 'quantity': 1},
                self.cancelled, validate=upload_authorized)
        except Exception as exc:
            if getattr(exc, 'mutation_started', None) is False and not getattr(exc, 'uncertain', False):
                coordinator.rejected(intent)
                self._manual_auction_update(index, 'failed', '游戏在提交前拒绝了该物品，未重复上传。')
                return
            self._manual_auction_update(index, 'uncertain', '游戏上传结果尚未确认；已保留记录，停止本批次，禁止重复提交。')
            raise ValidationError('上传结果待核验，剩余物品未提交。') from None
        if result.get('effect_confirmed') is not True:
            if result.get('not_submitted') is True:
                coordinator.rejected(intent)
                self._manual_auction_update(index, 'failed', '游戏确认未提交上传，请刷新后检查。')
                return
            self._manual_auction_update(index, 'uncertain', '游戏上传回执尚未确认，不会重复提交。')
            raise ValidationError('上传结果待核验，剩余物品未提交。')
        try:
            coordinator.confirmed(intent, result, enqueue=False)
        except Exception:
            self._manual_auction_update(index, 'uncertain', '上传回执与所选物品身份不一致，保留核验记录，禁止重复提交。')
            raise ValidationError('上传回执身份待核验，已停止后续操作。') from None
        self._manual_auction_update(index, 'uploaded', '已确认上传游戏交易所 / Steam 库存。')
        if not ticket:
            return
        # Read-only reconciliation continues even after cancellation. It does
        # not authorize another sale or a new upload.
        try:
            coordinator.trading.complete_upload(ticket, count=1, expected_instance_id=row['instance_id'])
            with coordinator.guard:
                coordinator.intents[intent]['phase'] = 'resolved'
                coordinator._save()
        except Exception:
            with coordinator.guard:
                coordinator.jobs[intent] = {**coordinator.intents[intent], 'phase': 'complete', 'attempts': 0}
                coordinator.wake.set()
            self._manual_auction_update(index, 'uploaded', '已上传；Steam 新资产仍待精确核验，未提交卖单，请稍后核对。')
            raise ValidationError('Steam 资产映射尚未确认，已停止后续上传。') from None
        if not self._manual_auction_active(plan, identity=True):
            self._manual_auction_update(index, 'uploaded', '已上传；操作已取消或角色状态变化，未提交 Steam 卖单。')
            return
        settings = {'sell_enabled': True, 'categories': list(CATEGORIES), 'excluded_keys': [],
                    'price_rule': plan['price_rule']}
        value = coordinator.trading.sale_once(settings, steamid,
            validate=lambda: self._manual_auction_active(plan, identity=True), proof_scope=frozenset({ticket}))
        receipt = coordinator.trading.proof_state(ticket, steamid)
        status = receipt.get('status')
        if status == 'pending':
            self._manual_auction_update(index, 'pending_confirmation', '卖单已提交，等待 Steam 手机或邮件确认；尚未确认挂牌或成交。')
        elif status == 'listed':
            self._manual_auction_update(index, 'listed', 'Steam 已接受挂牌；是否成交请以市场记录为准。')
        elif status in ('intent', 'uncertain'):
            self._manual_auction_update(index, 'uncertain', 'Steam 卖单结果待核验，禁止重复提交该资产。')
            raise ValidationError('Steam 卖单结果尚未确认，已停止后续上传。')
        else:
            self._manual_auction_update(index, 'uploaded', value.get('sale_message') or '已上传，Steam 未接受新卖单，请在库存或市场核验。')
            raise ValidationError('Steam 未确认新卖单，已停止后续上传。')

    def _manual_auction_work(self, plan):
        message, failed = '', False
        try:
            coordinator = self._auction_service()
            # Never hold service.guard while waiting for the background worker.
            while not coordinator.operation.acquire(timeout=.2):
                if not self._manual_auction_active(plan):
                    raise ValidationError('操作已取消，未开始本批次。')
            try:
                snapshot = self._manual_auction_fresh(plan)
                self._manual_auction_capability()
                # Reject the entire stale confirmation before the first write.
                for entry in plan['rows']:
                    self._manual_auction_find(snapshot, entry['row'])
                for entry in plan['rows']:
                    if not self._manual_auction_active(plan):
                        break
                    self._manual_auction_one(plan, entry, coordinator)
                    with self.guard:
                        self.manual_auction_value['completed'] += 1
                        self.manual_auction_value['phase'] = 'running'
                message = '本次所选物品已处理完成；等待确认的卖单请在 Steam 中核验。'
            finally:
                coordinator.operation.release()
        except ValidationError as exc:
            message, failed = str(exc), True
        except Exception:
            message, failed = '批量上架暂不可用，已停止后续操作；请核验物品与上传记录。', True
        finally:
            try:
                if self.client.connected:
                    self._publish(self.client.snapshot())
            except Exception:
                pass
            with self.guard:
                cancelled = self.cancelled.is_set()
                if cancelled:
                    message = '批量上架已停止，已提交的操作已保留核验记录；已取出的物品保留在背包。'
                for row in self.manual_auction_value['items']:
                    if row['status'] in ('queued', 'running'):
                        row.update(status='cancelled' if cancelled else 'failed',
                                   message='未提交后续操作。' if cancelled else message)
                self.manual_auction_value.update(running=False, can_cancel=False,
                    phase='cancelled' if cancelled else 'failed' if failed else 'completed', message=message)
                self.message = message
                self.busy = False
                self.operation_kind = None
            self.journal.append('system', 'manual_auction_finished', message)
