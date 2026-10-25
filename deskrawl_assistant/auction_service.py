"""Auction coordination: serialized game writes, separate bounded Steam I/O."""
from copy import deepcopy
from hashlib import sha256
import json
import os
import re
import threading
import time

from .auction import enabled, key
from .models import ValidationError


class AuctionCoordinator:
    def __init__(self, directory, trading, provider, *, start_worker=True):
        self.trading, self.provider = trading, provider
        self.path = directory / 'auction-upload-intents.json'
        self.guard = threading.RLock()
        # A manual batch owns this lock across baseline -> native upload ->
        # exact asset mapping, so the background worker cannot race that chain.
        self.operation = threading.RLock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.jobs = {}
        self.intents = {}
        self.native = {}
        self.cached = {}
        self.error = ''
        self.journal_error = False
        self.next_sale = 0
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding='utf-8'))
                if raw.get('schema_version') != 1 or not isinstance(raw.get('intents'), dict):
                    raise ValueError('invalid journal')
                self.intents = raw['intents']
                if any(not isinstance(v, dict) or v.get('phase') not in ('submitted', 'confirmed', 'resolved', 'uncertain')
                       or not isinstance(v.get('uid'), str) or not 0 < len(v['uid']) <= 256
                       or not isinstance(v.get('item_key'), str) or not 0 < len(v['item_key']) <= 256
                       or v.get('instance_id') is not None and (not isinstance(v['instance_id'], str)
                           or not 0 < len(v['instance_id']) <= 256)
                       or v.get('variant') not in ('normal', 'black_mist')
                       or v.get('steamid') is not None and (not isinstance(v['steamid'], str)
                           or re.fullmatch(r'7656\d{13}', v['steamid']) is None)
                       or v.get('ticket') is not None and (not isinstance(v['ticket'], str)
                           or re.fullmatch(r'[a-f0-9]{32}', v['ticket']) is None)
                       for v in self.intents.values()):
                    raise ValueError('invalid intent')
                # Only confirmed native receipts may resume Steam mapping.
                # A submitted/uncertain intent must first be reconciled by the game.
                for identity, row in self.intents.items():
                    if row['phase'] == 'confirmed' and row.get('ticket'):
                        self.jobs[identity] = {**deepcopy(row), 'phase': 'complete', 'attempts': 0}
        except Exception:
            self.journal_error = True
            self.error = '拍卖上传记录无法读取，原文件保留；暂不提交上传。'
        self.thread = threading.Thread(target=self._loop, name='steam-auction', daemon=True)
        if start_worker:
            self.thread.start()

    @staticmethod
    def identity(steamid, uid):
        return sha256((str(steamid) + '\0' + str(uid)).encode()).hexdigest()

    def _save(self):
        if self.journal_error:
            raise ValidationError(self.error)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump({'schema_version': 1, 'intents': self.intents}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def observe(self, native):
        with self.guard:
            self.native = deepcopy(native)
            result = deepcopy(native)
            steamid = native.get('steamid')
            if native.get('steamid_verified') is True and not self.journal_error:
                for receipt in native.get('confirmed_uploads', []):
                    if not isinstance(receipt, dict) or receipt.get('steamid') != steamid:
                        continue
                    identity = self.identity(steamid, receipt.get('uploaded_uid'))
                    intent = self.intents.get(identity)
                    if intent and intent['phase'] in ('submitted', 'uncertain'):
                        self.confirmed(identity, receipt)
            result['blocked_uids'] = list(native.get('blocked_uids', [])) + [row['uid'] for row in self.intents.values()
                if row.get('phase') != 'resolved']
            result['blocked_uids'] += [row['uid'] for row in self.jobs.values()
                if row.get('steamid') == steamid and row.get('phase') == 'failed' and row.get('retry_at', 0) > time.monotonic()]
            result['blocked_uids'] = sorted(set(result['blocked_uids']))
            result['blocked_keys'] = sorted(set(native.get('blocked_keys', [])) | {row['item_key'] for row in self.intents.values()
                if row.get('ticket') and row.get('phase') != 'resolved'})
            if self.journal_error:
                result.update(available=False, reason=self.error)
            return result

    def state(self):
        with self.guard:
            value = deepcopy(self.cached)
            native = self.native
            unresolved = sum(r.get('phase') in ('submitted', 'uncertain', 'confirmed') for r in self.intents.values())
            pending = any(r.get('phase') in ('prepare', 'preparing', 'complete', 'completing') for r in self.jobs.values())
            return {'steam': {'authenticated': value.get('verified') is True,
                    'bound': value.get('bound') is True,
                    'login_pending': value.get('login_open') is True,
                    'account_name': value.get('steamid') or '', 'wallet_currency': value.get('wallet_currency'),
                    'message': value.get('message') or ''},
                'upload': {'phase': 'pending' if pending else 'needs_review' if unresolved else 'idle',
                    'message': self.error or native.get('reason') or '仅上传白名单内已核验的单件物品。',
                    'listing_count': native.get('listing_count'), 'listing_limit': 8, 'pending': pending},
                'sell': {'phase': value.get('status', 'idle'), 'message': value.get('sale_message') or '',
                    'pending': value.get('queued_count', 0),
                    'pending_confirmation': value.get('pending_count', 0) > 0,
                    'confirmation_count': value.get('pending_count', 0),
                    'uncertain_count': value.get('uncertain_count', 0)}, 'error': self.error}

    def prepare(self, row, steamid, config):
        """Return ready ticket or enqueue I/O; called outside the game lock."""
        identity = self.identity(steamid, row['item_uid'])
        with self.guard:
            if self.journal_error or identity in self.intents:
                return None
            old = self.jobs.get(identity)
            signature = json.dumps(config, sort_keys=True)
            if (old and old.get('signature') == signature
                    and old.get('instance_id') == row.get('instance_id')):
                if old['phase'] == 'ready':
                    if time.monotonic() - old['created'] < 300:
                        return old['ticket']
                    old['phase'] = 'failed'
                if old['phase'] != 'failed' or old.get('retry_at', 0) > time.monotonic():
                    return None
            self.jobs[identity] = {'phase': 'prepare', 'uid': row['item_uid'], 'steamid': steamid,
                'instance_id': row.get('instance_id'),
                'item_key': key(row), 'variant': 'black_mist' if row.get('is_black_mist') is True else 'normal',
                'signature': signature, 'created': time.monotonic()}
            self.wake.set()
            return None

    def submitted(self, row, steamid, ticket=None):
        identity = self.identity(steamid, row['item_uid'])
        with self.guard:
            if identity in self.intents:
                raise ValidationError('该物品已有上传请求记录，禁止重复上传。')
            self.intents[identity] = {'uid': row['item_uid'], 'steamid': steamid, 'phase': 'submitted',
                'instance_id': row.get('instance_id'),
                'item_key': key(row), 'variant': 'black_mist' if row.get('is_black_mist') is True else 'normal',
                'created_at': time.time(), 'ticket': ticket}
            try:
                self._save()
            except Exception:
                self.intents.pop(identity, None)
                raise
            return identity

    def rejected(self, identity):
        """Only invoke on a proven pre-write failure, never a lost response."""
        with self.guard:
            intent = self.intents.pop(identity, None)
            job = self.jobs.get(identity)
            if job:
                job.update(phase='failed', retry_at=time.monotonic() + 30)
            elif intent:
                self.jobs[identity] = {**intent, 'phase': 'failed', 'retry_at': time.monotonic() + 30}
            self._save()

    def confirmed(self, identity, receipt, *, enqueue=True):
        with self.guard:
            row = self.intents[identity]
            # The local inventory UID and server GeneratedItemData instance ID
            # are different identifiers. Only the native exact receipt can
            # authorize mapping a newly uploaded Steam asset.
            if (not row.get('instance_id') or not isinstance(receipt, dict)
                    or receipt.get('uploaded_uid') != row['uid']
                    or receipt.get('uploaded_instance_id') != row['instance_id']
                    or receipt.get('uploaded_internal_name') != row['item_key']):
                row['phase'] = 'uncertain'
                self._save()
                raise ValidationError('拍卖上传回执与物品身份不一致，保留在途记录；请核验后继续。')
            row['phase'] = 'confirmed' if row.get('ticket') else 'resolved'
            self._save()
            job = self.jobs.get(identity)
            if row.get('ticket') and enqueue:
                self.jobs[identity] = {**row, 'phase': 'complete', 'attempts': 0}
                self.wake.set()

    def process_once(self):
        with self.operation:
            return self._process_once()

    def _process_once(self):
        refresh = getattr(self.trading, 'refresh_auth', None)
        if refresh:
            refresh()
        running, config, steamid = self.provider()
        sale_enabled = config.get('upload_enabled') is True and config.get('sell_enabled') is True
        with self.guard:
            candidate = next(((identity, deepcopy(row)) for identity, row in self.jobs.items()
                if row['phase'] == 'complete' and row.get('retry_at', 0) <= time.monotonic()
                or row['phase'] == 'prepare' and running and sale_enabled), None)
            if candidate:
                identity, job = candidate
                self.jobs[identity]['phase'] = 'completing' if job['phase'] == 'complete' else 'preparing'
        if candidate:
            identity, job = candidate
            try:
                if job['phase'] == 'prepare':
                    if steamid != job['steamid'] or json.dumps(config, sort_keys=True) != job['signature']:
                        raise ValidationError('拍卖配置或游戏账号已改变，重新读取后准备。')
                    ticket = self.trading.prepare_upload(job['steamid'], job['item_key'], job['variant'])
                    with self.guard:
                        self.jobs[identity].update(phase='ready', ticket=ticket)
                        self.error = ''
                else:
                    self.trading.complete_upload(job['ticket'], count=1, expected_instance_id=job['instance_id'])
                    with self.guard:
                        self.intents[identity]['phase'] = 'resolved'
                        self._save()
                        self.jobs.pop(identity, None)
                        self.error = ''
            except Exception:
                # Exceptions can contain HTTP payloads; keep cookies/session
                # and arbitrary upstream error text out of UI and logs.
                with self.guard:
                    old = self.jobs.get(identity)
                    if old:
                        attempts = old.get('attempts', 0) + 1
                        old.update(phase='complete' if job['phase'] == 'complete' and attempts < 5 else 'failed',
                                   attempts=attempts, retry_at=time.monotonic() + 30)
                    self.error = ('游戏已上传，但 Steam 资产尚未精确匹配；保留记录，暂不出售。'
                        if job['phase'] == 'complete' else 'Steam 登录、账号或库存基线暂未确认，跳过本次上传，其他托管继续。')
        elif running and sale_enabled and steamid and time.monotonic() >= self.next_sale:
            self.next_sale = time.monotonic() + 30
            signature = json.dumps(config, sort_keys=True)
            def still_authorized():
                current_running, current_config, current_steamid = self.provider()
                return (not self.closed.is_set() and current_running is True and current_steamid == steamid
                        and json.dumps(current_config, sort_keys=True) == signature)
            try:
                self.trading.sale_once(config, steamid, validate=still_authorized)
            except Exception:
                with self.guard:
                    self.error = 'Steam 市场暂不可用，稍后重试；已提交的订单不会重复发送。'
        # state() is only called by this network worker, never by a game tick.
        with self.guard:
            self.cached = deepcopy(self.trading.state())

    def _loop(self):
        while not self.closed.is_set():
            try:
                self.process_once()
            except Exception:
                with self.guard:
                    self.error = 'Steam 后台操作暂不可用，待重新核验；不影响刷图。'
            self.wake.wait(1)
            self.wake.clear()

    def close(self):
        self.closed.set()
        self.wake.set()
        self.trading.close()
        if self.thread.ident and self.thread is not threading.current_thread():
            self.thread.join(timeout=20)


class AuctionServiceMixin:
    def _auction_service(self):
        with self.guard:
            if getattr(self, 'auction_coordinator', None) is None:
                from .steam_trading import SteamTradingService
                trading = SteamTradingService(self.market_data_dir, catalog=self._market_catalog())
                self.auction_coordinator = AuctionCoordinator(self.market_data_dir, trading, self._auction_policy)
            return self.auction_coordinator

    def _auction_policy(self):
        with self.guard:
            running = (self.managed.running and not self.managed.finishing and self.client.connected
                       and not self.closed.is_set() and not self.cancelled.is_set())
            config = deepcopy(self.managed.settings['auction'])
            session, character_id = self.managed.session, self.managed.character_id
            client = self.client
        if not running or config.get('upload_enabled') is not True or config.get('sell_enabled') is not True:
            return running, config, None
        # This provider runs in the Steam worker and again at the sell boundary.
        # Never hold service.guard while waiting for the game's read lock;
        # never use an old AuctionUI account after a reconnect/role change.
        try:
            reader = getattr(client, 'steam_identity_snapshot', None)
            identity = reader() if reader else {}
        except Exception:
            identity = {}
        with self.guard:
            valid = (self.client is client and self.managed.running and not self.managed.finishing
                and not self.closed.is_set() and not self.cancelled.is_set() and client.connected
                and self.managed.settings['auction'] == config
                and self.managed.session == session and self.managed.character_id == character_id
                and bool(session) and bool(character_id)
                and identity.get('available') is True and identity.get('steamid_verified') is True
                and identity.get('session') == session and identity.get('character_id') == character_id)
            return bool(valid), config, identity.get('steamid') if valid else None

    def auction_state(self, *, desktop=False):
        coordinator = getattr(self, 'auction_coordinator', None)
        if coordinator is None and (self.market_data_dir / 'steam-session.dpapi').is_file():
            # Restore a saved login even when auction automation is disabled.
            # Reading status must not require opening a fresh login window.
            coordinator = self._auction_service()
        value = coordinator.state() if coordinator else {'steam': {}, 'upload': {}, 'sell': {}, 'error': ''}
        return {**value, 'login_available': bool(desktop),
                'login_reason': '' if desktop else '请在助手独立窗口中绑定 Steam。'}

    def auction_login(self):
        coordinator = self._auction_service()
        coordinator.trading.login_window()
        coordinator.wake.set()
        return self.auction_state(desktop=True)

    def auction_logout(self):
        coordinator = getattr(self, 'auction_coordinator', None)
        if coordinator:
            coordinator.trading.logout()
            with coordinator.guard:
                coordinator.cached = {}
                coordinator.jobs = {k: v for k, v in coordinator.jobs.items()
                    if v['phase'] in ('complete', 'completing')}
        return self.auction_state()

    def _managed_auction_state(self, scene):
        if not enabled(self.managed.settings) or scene.get('in_town') is not True:
            self.managed_auction = {}
            return {}
        if time.monotonic() < getattr(self, 'auction_refresh_after', 0):
            self.managed_auction = {}
            return {'available': False, 'reason': '游戏拍卖行暂不可用，稍后重新读取；其他托管继续。'}
        reader = getattr(self.client, 'auction_state', None)
        try:
            native = reader() if reader else {'available': False, 'reason': '本机原生拍卖接口尚不可用。'}
        except Exception:
            native = {'available': False, 'reason': '游戏拍卖状态暂不可读，跳过上传。'}
        self.managed_auction = deepcopy(native)
        return self._auction_service().observe(native)

    def _auction_catalog_payload(self):
        catalog = self._market_catalog()
        if not catalog:
            return []
        result = []
        for name, entry in catalog.items.items():
            meta = self.loot_item_ui['items'].get(name, {})
            material = name in ('GoldBar', 'UpgradeCommodity1')
            localized = self.item_names.get(name, {})
            equipment = self.catalog.equipment.get(name)
            result.append({'key': name, 'name': (equipment.label if equipment else None)
                or localized.get('Chinese (Simplified)') or meta.get('name') or name,
                'icon': meta.get('icon') or self.ui['equipment'].get(name, {}).get('icon'),
                'kind': entry.get('kind'), 'rarity': entry.get('rarity'),
                'quality': ('black_mist' if entry.get('black_mist') and not entry.get('normal')
                    else 'divine' if entry.get('kind') == 'equipment' else 'set' if entry.get('rarity') == 5
                    else 'grand' if entry.get('kind') == 'rune' else 'legendary'),
                'support_reason':
                    '无实例 ID 的金条／开光石暂需手动上传。' if material else ''})
        return result
