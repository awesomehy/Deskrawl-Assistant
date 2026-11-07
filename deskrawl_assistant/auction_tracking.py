"""Unified current-player pending trades and explicit repricing UI."""
from copy import deepcopy
import re
import threading
from uuid import uuid4

from .models import ValidationError
from .steam_session import SteamSessionError
from .sale_tracking import RepriceDeferred
from .auction import eligible, allows, key
from .auction_runtime import auction_candidate_fields
from .item_protection import is_minion_equipment


def observe_candidates(service, items, complete, *, session=None, character_id=None):
    """Read worker cache, independent of managed run and automatic sale policy."""
    with service.guard:
        context = getattr(service, 'pending_trade_context', {})
        coordinator = getattr(service, 'auction_coordinator', None)
        observe = getattr(getattr(coordinator, 'trading', None), 'observe_game_trade_context', None)
        if observe:
            observe(context, items, complete and session == context.get('session')
                    and character_id == context.get('character_id'))
        if (not complete or not context.get('steamid') or session != context.get('session')
                or character_id != context.get('character_id')):
            return
        candidates = {}
        catalog = service._market_catalog()
        for row in items:
            if row.get('container') not in ('inventory', 'storage'):
                continue
            item, _ = catalog.resolve(row)
            if not item:
                continue
            identity = row.get('item_uid') or row.get('instance_id') or (
                str(row.get('container')) + ':' + str(row.get('slot_index')))
            candidates[identity] = {'id': 'local-' + identity, 'item_uid': row.get('item_uid'),
                'instance_id': row.get('instance_id'), 'name': service._describe_item(item.key)[0],
                'item_key': item.key, 'variant': item.variant, 'container': row['container'],
                'count': row.get('count', 1), 'status': 'waiting_town',
                '_policy_item': {**deepcopy(row), **auction_candidate_fields(row, catalog)},
                'message': '背包 / 仓库中，待上架；自动处理仍遵循允许类别和强制保留设置。'}
        service.auction_tracking_candidates = candidates
        service.auction_tracking_scope = (session, character_id)


class AuctionTrackingMixin:
    def managed_auction_tracking(self, *, include_identity=False, item_uids=()):
        # Kept as an API alias for older clients. Scope is the current player,
        # not a manual batch or a managed run. No network/game I/O on GET.
        import time
        with self.guard:
            context = deepcopy(getattr(self, 'pending_trade_context', {}))
            snapshot = getattr(self, 'snapshot', None) or {}
            character = getattr(self, 'character', {})
            valid = (getattr(getattr(self, 'client', None), 'connected', False) and context.get('steamid')
                     and context.get('session') == snapshot.get('bridge_session')
                     and character.get('available') is True
                     and character.get('character_id') == context.get('character_id'))
            job = deepcopy(getattr(self, 'auction_reprice_job', {}))
            if not valid:
                return {'items': [], 'total': 0, 'reprice': job,
                        'message': '连接游戏并确认当前玩家身份后读取待交易物品。'}
            fresh = (snapshot.get('complete') is True and
                     context.get('identity_fresh', True) and
                     0 <= time.monotonic() - context.get('read_at', 0) <= 20)
            scope = (context['session'], context['character_id'])
            candidates = deepcopy(getattr(self, 'auction_tracking_candidates', {})) if (
                getattr(self, 'auction_tracking_scope', None) == scope) else {}
            coordinator = getattr(self, 'auction_coordinator', None)
            config = deepcopy(getattr(getattr(self, 'managed', None), 'settings', {}).get('auction', {}))
        steamid = context['steamid']
        native = context.get('native', {})
        checked = native.get('server_checked_at', 0)
        native_fresh = (fresh and native.get('available') is True and not native.get('needs_refresh')
                        and not native.get('busy') and native.get('steamid') == steamid
                        and native.get('steamid_verified') is True
                        and type(checked) in (int, float) and 0 < checked <= time.time() < checked + 60
                        and isinstance(native.get('listings'), list)
                        and 0 <= time.monotonic() - context.get('native_read_at', 0) <= 20)
        native_instances = {r.get('instance_id') for r in native.get('listings') or []
                            if r.get('received') is False}
        rows = []
        for row in candidates.values():
            item = row.pop('_policy_item', {})
            row['auto_eligible'] = eligible(item, config)
            if not row['auto_eligible']:
                reason = ('未启用自动上架' if not config.get('upload_enabled') else
                          '已设为强制保留' if key(item) in config.get('excluded_keys', []) else
                          '未允许自动上架此类别' if not allows(config, item) else
                          '物品身份或当前状态尚未满足自动上架要求')
                row['message'] = reason + '；保留在原位置，可手动选择上架。'
            if not fresh:
                row.update(status='uncertain', previous_status='waiting_town', needs_verification=True)
            rows.append(row)
        by_uid = {r['item_uid']: r for r in rows if r.get('item_uid')}
        by_instance = {r['instance_id']: r for r in rows if r.get('instance_id')}
        def merge(row, *, uid=None, instance=None):
            existing = by_uid.get(uid) if uid else None
            existing = existing or (by_instance.get(instance) if instance else None)
            if existing is not None:
                # Current physical possession beats a historical upload intent.
                return existing
            rows.append(row)
            if uid: by_uid[uid] = row
            if instance: by_instance[instance] = row
            return row
        receipts = coordinator.trading.pending_trade_receipts(steamid) if coordinator else []
        sold_instances = set(coordinator.trading.sold_trade_instances(steamid)) if coordinator else set()
        evidence = coordinator.trading.current_trade_evidence(steamid) if coordinator else {}
        with self.guard:
            historical = getattr(self, '_historical_uploads', frozenset())
        retired_instances = {instance for account, instance in historical if account == steamid}
        if native_fresh:
            retired_instances.difference_update(native_instances)
        retired_instances.update(r['instance_id'] for r in receipts
                                 if r.get('display_retired') and r.get('instance_id'))
        proofs = {r['proof'] for r in receipts if r.get('proof')}
        with coordinator.guard if coordinator else threading.RLock():
            intents = deepcopy(coordinator.intents) if coordinator else {}
        for identity, intent in intents.items():
            if intent.get('steamid') != steamid:
                continue
            # Sold receipts are absent from the pending projection. Do not
            # recreate them as upload-only rows from the old native intent.
            if intent.get('ticket') and coordinator:
                receipt = coordinator.trading.proof_state(intent['ticket'], steamid, wait=False)
                if receipt.get('status') == 'sold':
                    if intent.get('instance_id'):
                        sold_instances.add(intent['instance_id'])
                    continue
                if intent['ticket'] in proofs:
                    continue
            instance = intent.get('instance_id')
            if intent['uid'] in by_uid or instance and instance in by_instance:
                continue
            if instance in sold_instances:
                continue
            phase = intent.get('phase')
            native_present = native_fresh and instance in native_instances
            # A resolved native upload is a historical success, not evidence
            # of current ownership. Steam views already project actual assets
            # and orders (including manually submitted sales). Do not add a
            # second ghost row from an old, unlinked upload receipt.
            if phase == 'resolved' and not native_present:
                created = intent.get('created_at')
                if intent.get('display_retired_at'):
                    if instance:
                        retired_instances.add(instance)
                    continue
                if (native_fresh and instance and type(created) in (int, float)
                        and checked >= created and instance not in native_instances):
                    # Exact absence in a newly completed native server list
                    # retires only the old upload display, never invents a sale.
                    retired_instances.add(instance)
                    continue
                if (evidence.get('complete') and type(created) in (int, float)
                        and evidence.get('checked_at', 0) >= created + 120):
                    if instance:
                        retired_instances.add(instance)
                    continue
                if instance in retired_instances:
                    continue
            row = {'id': identity, 'item_key': intent['item_key'], 'count': 1,
                   'variant': intent.get('variant'), 'item_uid': intent['uid'], 'instance_id': instance,
                   'status': 'uploaded' if phase in ('confirmed', 'resolved') else 'uncertain',
                   'message': '已上架拍卖行，未上架 Steam 市场。' if phase in ('confirmed', 'resolved')
                              else '上传请求已提交，结果待核验，不重复提交。'}
            if intent.get('ticket') and not intent.get('market_skipped'):
                row.update(status='uncertain', previous_status='uploaded', needs_verification=True)
            if phase == 'resolved' and not native_present:
                row.update(status='uncertain', previous_status='uploaded', needs_verification=True,
                           auto_eligible=False, message='历史上传已完成，当前库存与卖单尚待核验；不代表仍在架。')
            merge(row, uid=intent['uid'], instance=instance)
        for receipt in receipts:
            if receipt.get('display_retired'):
                continue
            row = deepcopy(receipt)
            instance = row.get('instance_id')
            # An exact Steam receipt supersedes an old upload-only record.
            existing = by_instance.get(instance) if instance else None
            if existing is not None and existing.get('container') not in ('inventory', 'storage'):
                existing.update(row)
            else:
                merge(row, instance=instance)
        if native.get('steamid') == steamid and native.get('steamid_verified') is True:
            for listing in native.get('listings') or []:
                instance = listing.get('instance_id')
                if (not instance or listing.get('received') is not False
                        or instance in by_instance or instance in sold_instances or instance in retired_instances):
                    continue
                merge({'id': 'auction-' + instance, 'item_key': listing.get('internal_name'),
                       'count': 1, 'status': 'uploaded' if native_fresh else 'uncertain',
                       'previous_status': 'uploaded', 'needs_verification': not native_fresh,
                       'message': '已上架拍卖行，未上架 Steam 市场。'}, instance=instance)
        with self.guard:
            self._historical_uploads = (frozenset(pair for pair in historical if pair[0] != steamid)
                | frozenset((steamid, instance) for instance in retired_instances))
        public = []
        for row in rows:
            row['name'] = row.get('name') or self._describe_item(row.get('item_key') or '未识别物品')[0]
            if row.get('variant') == 'black_mist':
                if is_minion_equipment({'internal_name': row.get('item_key')}):
                    if '黑雾传说仆从装备' not in row['name']:
                        row['name'] += ' · 黑雾传说仆从装备'
                elif '黑雾' not in row['name']:
                    row['name'] += ' · 黑雾'
            if not include_identity:
                row = {k: v for k, v in row.items() if k not in ('item_uid', 'instance_id', 'assetid', 'proof')}
            public.append(row)
        message = '当前玩家尚未完成出售的可交易物品，不限本次托管或手动上架；已核实售出的物品自动移出。'
        auth = coordinator.trading.auth_state() if coordinator else None
        if not auth or auth.get('verified') is not True or auth.get('expired') or auth.get('steamid') != steamid:
            message += ' Steam 登录未确认或账号不匹配，Steam 库存与卖单暂不能完整更新。'
        if not native_fresh:
            message += ' 游戏拍卖行列表尚未完整核验，保留已知记录；未据此判定为空。'
        if job.get('waiting') and coordinator:
            job = {**coordinator.trading.reprice_recovery_state(job.get('tracking_id')),
                   'job_id': job.get('job_id')}
        return {'items': public, 'total': len(public), 'reprice': job, 'message': message}

    def auction_tracking_refresh(self, data):
        if not isinstance(data, dict) or data:
            raise ValidationError('刷新拍卖行不接受物品、账号或请求地址。')
        self.start('auction_refresh')
        return {'message': '正在请求游戏刷新拍卖行当前列表，不上传或出售物品。'}

    def _refresh_trade_tracking_if_needed(self):
        """Serialized read-worker task; never a side effect of a UI GET."""
        import time
        with self.guard:
            context = getattr(self, 'pending_trade_context', {})
            snapshot = getattr(self, 'snapshot', None) or {}
            managed = self.managed
            if (not getattr(self, 'auction_coordinator', None) or context.get('scene') != 'town'
                    or not context.get('identity_fresh', True) or not context.get('steamid_verified')
                    or not context.get('steamid') or not snapshot.get('complete')
                    or not 0 <= time.monotonic() - context.get('read_at', 0) <= 20
                    or getattr(managed, 'pending', None) is not None or managed.finishing
                    or self.cancelled.is_set() or self.closed.is_set() or self.updating
                    or self.game_actions_blocked()):
                return
            scope = tuple(context.get(k) for k in ('steamid', 'session', 'character_id'))
            attempt = getattr(self, '_native_trade_refresh_attempt', None)
            if attempt and attempt[0] == scope and time.monotonic() - attempt[1] < 30:
                return
            native = context.get('native') or {}
            checked = native.get('server_checked_at', 0)
            if (native.get('available') is True and not native.get('needs_refresh') and not native.get('busy')
                    and native.get('steamid') == scope[0] and native.get('steamid_verified') is True
                    and type(checked) in (int, float) and 0 < checked <= time.time() < checked + 30):
                return
            self._native_trade_refresh_attempt = (scope, time.monotonic())
        try:
            self._refresh_native_trades(background=True)
        except Exception:
            # Optional read failure must not stop farming, monitoring or move
            # items. The invalidated view remains unknown until the next reply.
            return

    def _refresh_native_trades(self, *, background=False):
        """Explicit serialized read request through the original native UI."""
        import time
        with self.guard:
            client = self.client
        identity = self._manual_auction_identity()
        self._manual_auction_capability()
        def active():
            with self.guard:
                return (self.client is client and client.connected and not self.closed.is_set()
                    and not self.updating and not self.cancelled.is_set()
                    and (background or not self.managed.running) and not self.managed.finishing
                    and (not background or getattr(self.managed, 'pending', None) is None)
                    and not self.game_actions_blocked())
        if not active():
            raise ValidationError('当前正在操作游戏，稍后再刷新拍卖行。')
        with self.guard:
            context = getattr(self, 'pending_trade_context', {})
            self.pending_trade_context = {**context, 'native': {
                **(context.get('native') or {}), 'needs_refresh': True, 'server_checked_at': 0}}
            coordinator = getattr(self, 'auction_coordinator', None)
        observe = getattr(getattr(coordinator, 'trading', None), 'observe_game_trade_context', None)
        if observe:
            observe(self.pending_trade_context, [], False)
        started = time.time()
        result = client.execute_game_action('auction_open', {'refresh_only': True}, self.cancelled, validate=active)
        if not active() or self._manual_auction_identity() != identity:
            raise ValidationError('当前账号或角色已变化，未采用旧拍卖行响应。')
        native = client.auction_state()
        checked = native.get('server_checked_at', 0)
        if (result.get('effect_confirmed') is not True or native.get('available') is not True
                or native.get('steamid') != identity['steamid'] or native.get('steamid_verified') is not True
                or native.get('busy') or native.get('needs_refresh')
                or not isinstance(native.get('listings'), list)
                or type(checked) not in (int, float) or checked < started):
            raise ValidationError('拍卖行当前列表尚未收到完整刷新回执，保留原状态待核验。')
        snapshot = client.snapshot()
        self._publish(snapshot)
        with self.guard:
            context = getattr(self, 'pending_trade_context', {})
            if (not active() or any(context.get(k) != v for k, v in identity.items())):
                raise ValidationError('当前玩家已变化，未采用旧拍卖行列表。')
            self.pending_trade_context = {**context, 'native': deepcopy(native), 'native_read_at': time.monotonic()}
            coordinator = getattr(self, 'auction_coordinator', None)
        if coordinator:
            coordinator.observe(native)
            observe = getattr(getattr(coordinator, 'trading', None), 'observe_game_trade_context', None)
            if observe:
                items = [row for container in snapshot.get('containers', {}).values()
                         for row in container.get('slots', [])]
                observe(self.pending_trade_context, items, snapshot.get('complete') is True)
            coordinator.wake.set()
        return '已重新读取游戏拍卖行当前列表；Steam 库存和卖单继续独立核验。'

    def _reprice_account(self):
        import time
        context = getattr(self, 'pending_trade_context', {})
        if (not getattr(getattr(self, 'client', None), 'connected', False)
                or not context.get('steamid') or not context.get('identity_fresh', True)
                or not 0 <= time.monotonic() - context.get('read_at', 0) <= 20):
            raise ValidationError('当前游戏账号尚未确认，请刷新后再操作。')
        return context['steamid']

    def auction_sale_preview(self, data):
        if (not isinstance(data, dict) or set(data) != {'item_id'}
                or not isinstance(data['item_id'], str)
                or not re.fullmatch(r'(?:[a-f0-9]{32}|inventory-[1-9]\d{0,24})', data['item_id'])):
            raise ValidationError('请选择待交易物品中已上传、尚未出售的单件商品。')
        with self.guard:
            coordinator = getattr(self, 'auction_coordinator', None)
            if self.closed.is_set() or self.updating or not coordinator:
                raise ValidationError('Steam 服务尚未就绪。')
            if getattr(self, 'auction_reprice_job', {}).get('running'):
                raise ValidationError('上一笔市场操作尚未完成，请稍后重试。')
            account = self._reprice_account()
        try:
            result = coordinator.trading.manual_sale_preview(data['item_id'], account)
            if result.get('quote_pending'):
                coordinator.wake.set()
            return result
        except SteamSessionError as exc:
            raise ValidationError(str(exc)) from None

    def auction_sale_start(self, data):
        if (not isinstance(data, dict) or set(data) != {'token', 'price'}
                or not isinstance(data['token'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data['token'])
                or not isinstance(data['price'], str) or not re.fullmatch(r'\d{1,10}(?:\.\d{1,2})?', data['price'])):
            raise ValidationError('请重新读取物品并填写有效的买方总价。')
        with self.guard:
            coordinator = getattr(self, 'auction_coordinator', None)
            if self.closed.is_set() or self.updating or not coordinator:
                raise ValidationError('Steam 服务尚未就绪。')
            if getattr(self, 'auction_reprice_job', {}).get('running'):
                raise ValidationError('上一笔市场操作尚未完成，不重复提交。')
            account = self._reprice_account()
            # Bind the game character/session as well as the Steam account.
            scope = tuple(self.pending_trade_context.get(k) for k in ('session', 'character_id'))
            preview = getattr(coordinator.trading, '_manual_sale_previews', {}).get(data['token'], {})
            if preview.get('steamid') != account:
                raise ValidationError('账号或上架确认已变化，请重新读取。')
            def active():
                with self.guard:
                    return (not self.closed.is_set() and not self.updating
                            and self._reprice_account() == account
                            and tuple(self.pending_trade_context.get(k) for k in ('session', 'character_id')) == scope)
            job_id = uuid4().hex
            self.auction_reprice_job = {'running': True, 'job_id': job_id,
                                       'message': '正在核验所选物品并按填写价格上架 Steam 市场。'}
            def work():
                try:
                    with coordinator.operation:
                        coordinator.trading.manual_sale(data['token'], data['price'],
                                                       expected_steamid=account, validate=active)
                    result = {'running': False, 'message': '上架请求已提交；可能需要在 Steam 手机应用确认。'}
                except SteamSessionError as exc:
                    result = {'running': False, 'error': True, 'message': str(exc)}
                except Exception:
                    result = {'running': False, 'error': True,
                              'message': '上架未完成或结果待核验；不会自动重复出售，请检查 Steam 市场 / 库存。'}
                with self.guard:
                    self.auction_reprice_job = {**result, 'job_id': job_id}
                coordinator.wake.set()
            threading.Thread(target=work, name='steam-manual-sale', daemon=True).start()
            return deepcopy(self.auction_reprice_job)

    def auction_reprice_preview(self, data):
        if (not isinstance(data, dict) or set(data) != {'tracking_id'}
                or not isinstance(data['tracking_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', data['tracking_id'])):
            raise ValidationError('请选择本工具已确认的在售卖单。')
        with self.guard:
            coordinator = getattr(self, 'auction_coordinator', None)
            if self.closed.is_set() or self.updating or not coordinator:
                raise ValidationError('Steam 服务尚未就绪。')
            if getattr(self, 'auction_reprice_job', {}).get('running'):
                raise ValidationError('上一笔改价仍在核验，请稍后重试。')
            account = self._reprice_account()
        if not any(row.get('tracking_id') == data['tracking_id']
                   for row in coordinator.trading.pending_trade_receipts(account)):
            raise ValidationError('该卖单不属于当前玩家的待交易物品。')
        try:
            result = coordinator.trading.reprice_preview(data['tracking_id'])
            if result.get('quote_pending'):
                coordinator.wake.set()
            return result
        except SteamSessionError as exc:
            raise ValidationError(str(exc)) from None

    def auction_reprice_start(self, data):
        if (not isinstance(data, dict) or set(data) != {'token', 'price'}
                or not isinstance(data['token'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data['token'])
                or not isinstance(data['price'], str) or not re.fullmatch(r'\d{1,10}(?:\.\d{1,2})?', data['price'])):
            raise ValidationError('请重新核验卖单并填写有效的买方总价。')
        with self.guard:
            coordinator = getattr(self, 'auction_coordinator', None)
            if self.closed.is_set() or self.updating or not coordinator:
                raise ValidationError('Steam 服务尚未就绪。')
            if getattr(self, 'auction_reprice_job', {}).get('running'):
                raise ValidationError('上一笔改价仍在核验，不重复提交。')
            account = self._reprice_account()
            preview = getattr(coordinator.trading, '_reprice_previews', {}).get(data['token'], {})
            if preview.get('steamid') != account:
                raise ValidationError('游戏账号或改价确认已变化，请重新核验。')
            scope = tuple(self.pending_trade_context.get(k) for k in ('session', 'character_id'))
            def active():
                with self.guard:
                    return (not self.closed.is_set() and not self.updating
                            and self._reprice_account() == account
                            and tuple(self.pending_trade_context.get(k) for k in ('session', 'character_id')) == scope)
            job_id = uuid4().hex
            self.auction_reprice_job = {'running': True, 'job_id': job_id,
                                       'message': '正在下架、核验退回物品并按新价格重挂。'}
            def work():
                try:
                    with coordinator.operation:
                        with self.guard:
                            if self._reprice_account() != account:
                                raise ValidationError('当前玩家已变化，未执行改价。')
                        coordinator.trading.reprice(data['token'], data['price'], validate=active)
                    result = {'running': False, 'message': '重挂请求已提交；可能需要重新在 Steam 手机应用确认。'}
                except RepriceDeferred as exc:
                    result = {'running': False, 'waiting': True, 'tracking_id': exc.record_id, 'message': str(exc)}
                except SteamSessionError as exc:
                    result = {'running': False, 'error': True,
                              'message': str(exc) + ' 不会自动重复下架或出售，请核验物品状态。'}
                except Exception:
                    result = {'running': False, 'error': True,
                              'message': '改价未完成或结果待核验，请刷新物品状态并检查 Steam 市场 / 库存；不会自动重复提交。'}
                with self.guard:
                    self.auction_reprice_job = {**result, 'job_id': job_id}
                coordinator.wake.set()
            threading.Thread(target=work, name='steam-reprice', daemon=True).start()
            return deepcopy(self.auction_reprice_job)
