"""Explicitly authorised Deskrawl uploads and Steam sell orders.

No old inventory is automatically adopted. Asset identity must be a known
upload receipt or an exact before/after inventory delta. Durable intent is
written before a sell request: ambiguous results are never resubmitted.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import quote, urlencode
from uuid import uuid4

from .steam_market import APP_ID, MarketCatalog, MarketItem, parse_listing, MarketDataError
from .steam_session import SteamSession, SteamSessionError, _atomic_write
from .auction import CATEGORIES, allows as auction_allows

JOURNAL_LIMIT = 2000


def _int(value, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        raise SteamSessionError('Steam 金额或数量格式无效。')
    return value


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SteamSessionError('Steam 手续费格式无效。') from None
    if not result.is_finite() or not 0 <= result <= 1:
        raise SteamSessionError('Steam 手续费格式无效。')
    return result


def fee_parameters(wallet, publisher_fee):
    value = wallet['wallet']
    try:
        minimum = int(value['wallet_market_minimum'])
        increment = int(value['wallet_currency_increment'])
        maximum = int(value['wallet_trade_max_balance'])
    except (KeyError, TypeError, ValueError):
        raise SteamSessionError('Steam 钱包价格限制无效。') from None
    _int(minimum, 1)
    _int(increment, 1)
    _int(maximum, minimum)
    # Require an item/app fee or Steam's verified wallet default. Never use
    # another app's fee or a presumed fixed 15% combined fee.
    if publisher_fee is None:
        raise SteamSessionError('此物品的 Steam 发行商手续费尚未核实，暂不出售。')
    return {'minimum': minimum, 'increment': increment, 'maximum': maximum,
            'steam': _number(value.get('wallet_fee_percent')), 'publisher': _number(publisher_fee)}


def valid_market_amount(amount, parameters):
    minimum, increment = parameters['minimum'], parameters['increment']
    if amount <= minimum:
        return minimum
    if amount <= increment:
        return increment
    # The native JS uses positive half-up rounding to currency steps.
    return ((amount * 2 + increment) // (increment * 2)) * increment


def buyer_total(receive, parameters):
    receive = _int(receive, 1)
    fees = []
    for rate in (parameters['steam'], parameters['publisher']):
        if rate:
            floored = int((Decimal(receive) * rate).to_integral_value(rounding=ROUND_FLOOR))
            fees.append(valid_market_amount(floored, parameters))
        else:
            fees.append(0)
    return valid_market_amount(receive, parameters) + sum(fees)


def receive_for_total(total, parameters):
    """Only exact representable buyer totals; do not silently lower the price."""
    _int(total, 1, parameters['maximum'])
    step = parameters['increment']
    low, high = 0, total // step + 1
    while low < high:
        middle = (low + high) // 2
        amount = max(parameters['minimum'], middle * step)
        if buyer_total(amount, parameters) < total:
            low = middle + 1
        else:
            high = middle
    candidates = {parameters['minimum'], low * step, max(parameters['minimum'], (low - 1) * step)}
    for amount in sorted(candidates):
        if amount > 0 and valid_market_amount(amount, parameters) == amount and buyer_total(amount, parameters) == total:
            return amount
    raise SteamSessionError('目标买方总价不符合 Steam 手续费与币种步长，暂不出售。')


def _json(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise SteamSessionError('Steam 响应无法核验。') from None
    if not isinstance(value, dict):
        raise SteamSessionError('Steam 响应无法核验。')
    return value


def _assetid(value):
    value = str(value or '')
    if not re.fullmatch(r'[1-9]\d{0,24}', value):
        raise SteamSessionError('Steam 资产身份无效。')
    return value


class SteamTradingService:
    def __init__(self, path, catalog=None, *, session=None, transport=None, clock=time.time):
        self.path = Path(path)
        self.catalog = catalog
        self.clock = clock
        self.session = session or SteamSession(self.path / 'steam-session.dpapi', transport=transport, clock=clock)
        self.guard = threading.RLock()
        self._operation = threading.Lock()
        self._journal_path = self.path / 'steam-sale-journal.json'
        self._journal = []
        self._tickets = {}
        self._message = 'Steam 市场出售为开发验证功能，尚未进行实机出售验证。'
        self._status = 'idle'
        self._retry_after = 0
        self._auth_retry_after = 0
        self._reconcile_after = 0
        self._closed = False
        if self._journal_path.exists():
            try:
                raw = json.loads(self._journal_path.read_text(encoding='utf-8'))
                if raw.get('schema_version') != 1 or not isinstance(raw.get('records'), list):
                    raise ValueError()
                if len(raw['records']) > JOURNAL_LIMIT:
                    raise ValueError()
                self._journal = raw['records']
                self._tickets = raw.get('tickets', {})
                if not isinstance(self._tickets, dict) or len(self._tickets) > 50:
                    raise ValueError()
                for ticket, baseline in self._tickets.items():
                    if (not re.fullmatch(r'[a-f0-9]{32}', ticket) or not isinstance(baseline, dict)
                            or not re.fullmatch(r'7656\d{13}', str(baseline.get('steamid', '')))
                            or not isinstance(baseline.get('item_key'), str)
                            or baseline.get('variant') not in ('normal', 'black_mist')
                            or 'manual_only' in baseline and type(baseline['manual_only']) is not bool
                            or type(baseline.get('created_at')) not in (int, float)
                            or not isinstance(baseline.get('before'), dict)):
                        raise ValueError()
                    for assetid, amount in baseline['before'].items():
                        _assetid(assetid)
                        _int(amount, 1, 10**9)
                for row in self._journal:
                    if (not isinstance(row, dict) or not re.fullmatch(r'7656\d{13}', str(row.get('steamid', '')))
                            or row.get('status') not in ('captured', 'intent', 'uncertain', 'pending', 'listed', 'missing')
                            or not isinstance(row.get('item_key'), str)
                            or row.get('variant') not in ('normal', 'black_mist')):
                        raise ValueError()
                    if 'manual_only' in row and type(row['manual_only']) is not bool:
                        raise ValueError()
                    _assetid(row.get('assetid'))
                    _int(row.get('amount'), 1, 10**9)
                    if row.get('status') == 'intent':
                        row['status'] = 'uncertain'
                self._save()
            except Exception:
                self._journal = []
                self._tickets = {}
                self._status = 'journal_error'
                self._message = 'Steam 出售记录损坏；原文件保留，自动出售已暂停。'

    def _save(self):
        # Intent/ambiguous records may never be evicted and thereby retried.
        self._healthy_journal()
        if len(self._journal) > JOURNAL_LIMIT:
            raise SteamSessionError('Steam 出售记录达到上限，请先核验已有记录。')
        _atomic_write(self._journal_path, json.dumps({'schema_version': 1, 'records': self._journal,
                                                   'tickets': self._tickets},
                                                   ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def _healthy_journal(self):
        if self._status == 'journal_error':
            raise SteamSessionError('Steam 出售记录损坏；原文件保留，自动出售已暂停。')

    def state(self):
        with self.guard:
            state = self.session.state()
            counts = {key: sum(row.get('status') == key for row in self._journal)
                      for key in ('captured', 'intent', 'uncertain', 'pending', 'listed')}
            return {**state, 'status': self._status, 'sale_message': self._message,
                    'pending_count': counts['pending'], 'uncertain_count': counts['uncertain'],
                    'queued_count': counts['captured'], 'retry_after': self._retry_after,
                    'counts': counts, 'development': True}

    def login_window(self):
        return self.session.login_window()

    def refresh_auth(self):
        """Read-only verification on the Steam worker, also while managed mode is idle."""
        state = self.session.state()
        if (not state.get('bound') or state.get('verified') or state.get('login_open')
                or self._closed or self.clock() < max(self._auth_retry_after, self._retry_after)):
            return
        self._auth_retry_after = self.clock() + 30
        try:
            self.session.verify()
        except SteamSessionError as exc:
            self._limited(exc)

    def capture_session(self, cookies):
        return self.session.capture_session(cookies)

    def logout(self):
        # Invalidate credentials immediately, before waiting on a read-only
        # inventory/quote operation. A sale already in flight is journaled;
        # a sale still preparing must not start after logout was requested.
        result = self.session.logout()
        with self._operation:
            self._tickets.clear()
            if self._status != 'journal_error':
                self._save()
            if self._status != 'journal_error':
                self._status = 'idle'
            return result

    def close(self):
        self._closed = True
        self.session.close()

    def _account(self, expected):
        if self.clock() < self._retry_after:
            raise SteamSessionError('Steam 正在退避等待，暂不发送请求。')
        if not isinstance(expected, str) or not re.fullmatch(r'7656\d{13}', expected):
            raise SteamSessionError('游戏 Steam 身份尚未确认，暂不执行市场出售。')
        try:
            wallet = self.session.verify()
        except SteamSessionError as exc:
            self._limited(exc)
            raise
        if wallet['steamid'] != expected:
            raise SteamSessionError('绑定 Steam 与当前游戏账号不同，暂不执行市场出售。')
        return wallet

    def _limited(self, exc):
        if exc.status in (403, 429):
            self._retry_after = self.clock() + max(300, min(3600, exc.retry_after))

    def _item(self, key, variant='normal'):
        if self.catalog is None:
            raise SteamSessionError('Steam 可交易物品词典尚未加载。')
        entry = self.catalog.items.get(key)
        if not entry or variant not in ('normal', 'black_mist') or not entry.get(variant):
            raise SteamSessionError('该物品没有已验证的 Steam 市场定义。')
        value = entry[variant]
        return MarketItem(key, value['market_hash_name'], value['definition_id'], entry['kind'], variant)

    def _inventory(self, steamid):
        assets, descriptions, start = [], {}, None
        for _ in range(20):
            query = {'l': 'english', 'count': 2000}
            if start:
                query['start_assetid'] = start
            try:
                raw = _json(self.session.request('GET', f'/inventory/{steamid}/{APP_ID}/2?' + urlencode(query)))
            except SteamSessionError as exc:
                self._limited(exc)
                raise
            if raw.get('success') not in (1, True) or not isinstance(raw.get('assets', []), list):
                raise SteamSessionError('Steam 库存暂不可读。')
            for description in raw.get('descriptions', []):
                if not isinstance(description, dict) or int(description.get('appid', 0)) != APP_ID:
                    raise SteamSessionError('Steam 库存应用不匹配。')
                descriptions[(str(description.get('classid')), str(description.get('instanceid', '0')))] = description
            assets.extend(raw.get('assets', []))
            if not raw.get('more_items'):
                break
            new_start = raw.get('last_assetid')
            if not new_start or new_start == start:
                raise SteamSessionError('Steam 库存分页未确认。')
            start = new_start
        else:
            raise SteamSessionError('Steam 库存过大，暂不自动出售。')
        result = {}
        for asset in assets:
            if not isinstance(asset, dict) or int(asset.get('appid', 0)) != APP_ID or str(asset.get('contextid')) != '2':
                raise SteamSessionError('Steam 库存资产应用不匹配。')
            identity = _assetid(asset.get('assetid'))
            if identity in result:
                raise SteamSessionError('Steam 返回重复资产。')
            amount = asset.get('amount')
            if isinstance(amount, str) and amount.isdigit():
                amount = int(amount)
            _int(amount, 1, 10**9)
            description = descriptions.get((str(asset.get('classid')), str(asset.get('instanceid', '0'))))
            if description is None:
                raise SteamSessionError('Steam 库存资产描述缺失。')
            result[identity] = {**asset, 'assetid': identity, 'amount': amount, 'description': description}
        return result

    @staticmethod
    def _matches(asset, item):
        description = asset['description']
        if (description.get('marketable') not in (1, True)
                or description.get('market_hash_name') != item.market_hash_name):
            return False
        prefix = '[Black Mist]' if item.variant == 'black_mist' else '[Divine]' if item.kind == 'equipment' else None
        return not prefix or str(description.get('type', '')).startswith(prefix)

    def prepare_upload(self, expected_steamid, item_key, variant='normal', *, manual=False):
        with self._operation:
            self._healthy_journal()
            wallet = self._account(expected_steamid)
            item = self._item(item_key, variant)
            inventory = self._inventory(expected_steamid)
            ticket = uuid4().hex
            self._tickets[ticket] = {'steamid': expected_steamid, 'item_key': item_key, 'variant': variant,
                                     'binding_generation': wallet.get('binding_generation'),
                                     'manual_only': manual is True,
                                     'before': {key: value['amount'] for key, value in inventory.items()
                                                if self._matches(value, item)}, 'created_at': self.clock()}
            # Save the exact baseline before authorizing a game upload. Expiry
            # remains strict after restart; a long gap must never authorize a guess.
            self._tickets = {key: row for key, row in self._tickets.items()
                             if 0 <= self.clock() - row['created_at'] < 600}
            if len(self._tickets) > 50:
                self._tickets.pop(next(iter(self._tickets)))
            self._save()
            return ticket

    def upload_binding_current(self, ticket, expected_steamid):
        """No network I/O at a native upload boundary; logout invalidates it."""
        if not self._operation.acquire(blocking=False):
            return False
        try:
            row = self._tickets.get(ticket)
            state = self.session.state()
            return bool(row and not self._closed and row.get('steamid') == expected_steamid
                and 0 <= self.clock() - row['created_at'] < 600
                and state.get('bound') is True and state.get('verified') is True
                and state.get('steamid') == expected_steamid
                and state.get('binding_generation') == row.get('binding_generation'))
        finally:
            self._operation.release()

    def complete_upload(self, ticket, count=1, *, expected_instance_id=None):
        with self._operation:
            self._healthy_journal()
            _int(count, 1, 10**9)
            row = self._tickets.get(ticket)
            if not row or not 0 <= self.clock() - row['created_at'] < 600:
                raise SteamSessionError('上传库存基线已失效，暂不自动出售此物品。')
            if row.get('ambiguous'):
                raise SteamSessionError('此上传的 Steam 增量存在歧义，请手动核验；不会重新猜测资产。')
            if expected_instance_id is not None and (not isinstance(expected_instance_id, str)
                                                     or not 0 < len(expected_instance_id) <= 128):
                raise SteamSessionError('游戏上传实例身份无效。')
            self._account(row['steamid'])
            item = self._item(row['item_key'], row['variant'])
            inventory = self._inventory(row['steamid'])
            added = [(asset, asset['amount'] - row['before'].get(identity, 0))
                     for identity, asset in inventory.items() if self._matches(asset, item)
                     and asset['amount'] > row['before'].get(identity, 0)]
            total = sum(amount for _, amount in added)
            if len(added) > 1 or total > count:
                row['ambiguous'] = True
                self._save()
                self._status = 'awaiting_resolution'
                self._message = '同次上传出现多个或超额新增 Steam 资产，请手动核验；不会自动猜测。'
                raise SteamSessionError(self._message)
            if len(added) != 1 or total != count:
                raise SteamSessionError('上传后的 Steam 资产增量尚未精确匹配，暂不出售。')
            for asset, amount in added:
                try:
                    instance = self._native_instance(asset)
                except SteamSessionError:
                    row['ambiguous'] = True
                    self._save()
                    raise
                if expected_instance_id and instance and instance != expected_instance_id:
                    row['ambiguous'] = True
                    self._save()
                    raise SteamSessionError('Steam 新资产的游戏实例与上传回执不同，暂不出售。')
                self._register(row['steamid'], asset['assetid'], row['item_key'], row['variant'], amount,
                               'verified_upload_delta', proof=ticket, manual_only=row.get('manual_only') is True)
            self._tickets.pop(ticket, None)
            self._save()
            return self.state()

    def _register(self, steamid, assetid, item_key, variant, count, source, *, proof=None, manual_only=False):
        self._healthy_journal()
        identity = _assetid(assetid)
        _int(count, 1, 10**9)
        self._item(item_key, variant)
        existing = [row for row in self._journal if row['assetid'] == identity and row['steamid'] == steamid]
        if existing:
            if not proof or any(row.get('proof') == proof for row in existing):
                return
            if any(row['status'] in ('intent', 'uncertain', 'pending') for row in existing):
                raise SteamSessionError('此 Steam 资产还有待核验的卖单，新增上传暂不自动出售。')
        if len(self._journal) >= JOURNAL_LIMIT:
            raise SteamSessionError('Steam 出售记录达到上限，请先核验已有记录。')
        self._journal.append({'id': uuid4().hex, 'steamid': steamid, 'assetid': identity,
                              'item_key': item_key, 'variant': variant, 'amount': count,
                              'source': source, 'proof': proof, 'manual_only': manual_only is True,
                              'status': 'captured', 'created_at': self.clock()})
        self._save()

    def proof_state(self, proof, expected_steamid):
        """Narrow internal receipt view; never expose credentials or asset IDs."""
        with self._operation:
            matches = [row for row in self._journal
                       if row.get('proof') == proof and row.get('steamid') == expected_steamid]
            if len(matches) != 1:
                return {'status': 'unmatched'}
            row = matches[0]
            return {k: row[k] for k in ('status', 'buyer_total', 'seller_receive', 'currency_id') if k in row}

    def register_uploaded(self, assetid, item_key, variant='normal', count=1, *, expected_steamid):
        """Native receipt only; HTTP frontend must not expose this method."""
        with self._operation:
            self._healthy_journal()
            self._account(expected_steamid)
            inventory = self._inventory(expected_steamid)
            identity = _assetid(assetid)
            item = self._item(item_key, variant)
            asset = inventory.get(identity)
            if not asset or not self._matches(asset, item) or asset['amount'] < count:
                raise SteamSessionError('上传回执与 Steam 资产未匹配，暂不出售。')
            self._register(expected_steamid, identity, item_key, variant, count, 'native_upload_receipt')

    @staticmethod
    def _native_instance(asset):
        # Steam's numeric instanceid identifies a description variant, not the
        # game's GUID. Only explicitly named game dynamic properties qualify.
        values = []
        for source in (asset, asset.get('description', {})):
            for key in ('game_instance_id', 'game_instanceId'):
                if isinstance(source.get(key), str):
                    values.append(source[key])
            properties = source.get('properties')
            if isinstance(properties, dict):
                for key in ('game_instance_id', 'instanceId'):
                    if isinstance(properties.get(key), str):
                        values.append(properties[key])
        unique = set(values)
        if len(unique) > 1:
            raise SteamSessionError('Steam 资产的游戏实例属性存在冲突，暂不自动出售。')
        return next(iter(unique)) if len(unique) == 1 else None

    def _adopt_receipts(self, uploaded, steamid, inventory, whitelist):
        if not uploaded:
            return
        if isinstance(uploaded, dict):
            uploaded = [uploaded]
        if not isinstance(uploaded, list) or len(uploaded) > 100:
            raise SteamSessionError('上传回执格式无效。')
        for receipt in uploaded:
            if not isinstance(receipt, dict):
                continue
            key = receipt.get('name_key') or receipt.get('item_key')
            if key not in whitelist:
                continue
            variant = receipt.get('variant') or ('black_mist' if receipt.get('is_black_mist') else 'normal')
            item = self._item(key, variant)
            identity = receipt.get('assetid')
            if not identity and receipt.get('instance_id'):
                matches = [asset['assetid'] for asset in inventory.values()
                           if self._matches(asset, item) and self._native_instance(asset) == receipt['instance_id']]
                if len(matches) == 1:
                    identity = matches[0]
            if not identity:
                self._message = '游戏上传已完成，Steam 资产身份尚未匹配；不会按名称猜测出售。'
                continue
            identity = _assetid(identity)
            asset = inventory.get(identity)
            count = receipt.get('amount', receipt.get('count', 1))
            _int(count, 1, 10**9)
            if asset and self._matches(asset, item) and asset['amount'] >= count:
                self._register(steamid, identity, key, variant, count, 'native_upload_receipt')

    def _can_submit(self, validate):
        if self._closed:
            return False
        try:
            return validate is None or callable(validate) and validate() is True
        except Exception:
            return False

    def _reconcile_listings(self, steamid):
        """Positive evidence may confirm a sale; missing evidence never retries it."""
        rows = [r for r in self._journal if r['steamid'] == steamid
                and r['status'] in ('pending', 'uncertain')]
        if not rows or self.clock() < self._reconcile_after:
            return
        self._reconcile_after = self.clock() + 60
        from .steam_listing_receipts import listing_receipts
        changed = False
        for page in range(20):
            response = _json(self.session.request('GET', '/market/mylistings/?' +
                             urlencode({'start': page * 100, 'count': 100, 'l': 'english'})))
            if response.get('success') not in (True, 1):
                raise SteamSessionError('Steam 卖单列表暂不可读，保留已提交记录。')
            listings = listing_receipts(response.get('results_html'))
            for row in rows:
                matches = [listing for listing in listings.values()
                           if listing['assetid'] == row['assetid']
                           and (not row.get('listingid') or listing['listingid'] == row['listingid'])]
                if len(matches) == 1 and not matches[0]['pending']:
                    row.update(status='listed', listingid=matches[0]['listingid'], confirmed_at=self.clock())
                    changed = True
            if changed:
                self._save()
            total = response.get('total_count')
            if type(total) is not int or total < 0:
                raise SteamSessionError('Steam 卖单分页尚未确认，保留原记录。')
            if (page + 1) * 100 >= total:
                break
        if changed:
            self._message = '已在 Steam 当前卖单中确认上架；是否成交请以 Steam 市场记录为准。'

    def sale_once(self, settings, expected_steamid, uploaded=None, *, validate=None, proof_scope=None):
        """Submit at most one sale; caller schedules this outside managed reads."""
        if not self._operation.acquire(blocking=False):
            return self.state()
        try:
            if self._closed or not isinstance(settings, dict) or not settings.get('sell_enabled', settings.get('enabled', False)):
                return self.state()
            if self._status == 'journal_error' or self.clock() < self._retry_after:
                return self.state()
            if proof_scope is not None and (not isinstance(proof_scope, frozenset)
                    or not proof_scope or any(not isinstance(proof, str)
                    or re.fullmatch(r'[a-f0-9]{32}', proof) is None for proof in proof_scope)):
                raise SteamSessionError('手动出售的上传回执范围无效。')
            categories = settings.get('categories', [])
            if (not isinstance(categories, list) or any(not isinstance(value, str) or value not in CATEGORIES
                                                       for value in categories)):
                raise SteamSessionError('允许出售的商品类别无效。')
            excluded = settings.get('excluded_keys', [])
            if not isinstance(excluded, list) or any(not isinstance(key, str) for key in excluded):
                raise SteamSessionError('跳过出售物品配置无效。')
            excluded = set(excluded)
            # Expand against the shipped market catalog, never a remote name.
            # Keep the variant check below even if future resources share a key.
            # Legacy item_keys is accepted only for configuration migration;
            # it cannot authorize a sale after its UI has been removed.
            whitelist = set()
            for name, entry in (self.catalog.items.items() if self.catalog else []):
                if any(entry.get(variant) and auction_allows(settings,
                       {**entry, 'internal_name': name, 'variant': variant}) for variant in ('normal', 'black_mist')):
                    whitelist.add(name)
            rule = settings.get('price_rule', 'lowest_sell')
            if rule not in ('lowest_sell', 'highest_buy'):
                raise SteamSessionError('Steam 出售价格规则无效。')
            wallet = self._account(expected_steamid)
            try:
                self._reconcile_listings(expected_steamid)
            except SteamSessionError as exc:
                self._limited(exc)
                if self.clock() < self._retry_after:
                    raise
                # An unreadable market-history view cannot authorize a retry
                # of an old request; unrelated captured assets may still proceed.
                self._message = '部分已提交卖单仍待核验，不重复提交；其他资产继续按配置处理。'
            inventory = self._inventory(expected_steamid)
            self._adopt_receipts(uploaded, expected_steamid, inventory, whitelist)
            for row in self._journal:
                if (proof_scope is not None and row.get('proof') not in proof_scope
                        or proof_scope is None and row.get('manual_only') is True):
                    continue
                if (row['steamid'] != expected_steamid or row['item_key'] not in whitelist
                        or row['status'] != 'captured'):
                    continue
                entry = self.catalog.items.get(row['item_key'], {}) if self.catalog else {}
                if not auction_allows(settings, {**entry, 'internal_name': row['item_key'], 'variant': row['variant']}):
                    continue
                if any(other['steamid'] == expected_steamid and other['assetid'] == row['assetid']
                       and other['status'] in ('intent', 'uncertain', 'pending') for other in self._journal):
                    continue
                item = self._item(row['item_key'], row['variant'])
                asset = inventory.get(row['assetid'])
                if not asset or not self._matches(asset, item):
                    row['status'] = 'missing'
                    self._save()
                    continue
                amount = min(row['amount'], asset['amount'])
                if not amount:
                    continue
                document = self.session.request('GET', f'/market/listings/{APP_ID}/'
                                                + quote(item.market_hash_name, safe='') + '?l=english')
                quote_data = parse_listing(document, item)
                if quote_data['currency_id'] != wallet['currency_id']:
                    raise SteamSessionError('Steam 行情与钱包币种不一致，暂不出售；参考换算不能用于下单。')
                total = quote_data[rule]
                if total is None:
                    self._message = '当前价格规则没有可用订单，跳过此物品。'
                    continue
                value = Decimal(total) * 100
                if value != value.to_integral_value():
                    raise SteamSessionError('Steam 行情金额无效。')
                fee = asset['description'].get('market_fee')
                if fee is None:
                    fee = wallet.get('publisher_fee')
                parameters = fee_parameters(wallet, fee)
                receive = receive_for_total(int(value), parameters)
                if not self._can_submit(validate):
                    self._status = 'waiting'
                    self._message = '托管状态、账号或出售配置已改变，未提交新卖单。'
                    return self.state()
                # Persist intent before the request. From here any failure is
                # ambiguous, including a network timeout or shutdown.
                row.update(status='intent', buyer_total=int(value), seller_receive=receive,
                           sale_amount=amount, currency_id=wallet['currency_id'], submitted_at=self.clock())
                self._save()
                try:
                    if not self._can_submit(validate):
                        row['status'] = 'captured'
                        self._status = 'waiting'
                        self._message = '托管状态、账号或出售配置已改变，未提交新卖单。'
                        return self.state()
                    response = _json(self.session.post('/market/sellitem/',
                                                      {'appid': APP_ID, 'contextid': '2',
                                                       'assetid': asset['assetid'], 'amount': amount, 'price': receive},
                                                      expected_steamid=expected_steamid,
                                                      expected_generation=wallet.get('binding_generation')))
                    if response.get('success') not in (1, True):
                        row['status'] = 'uncertain'
                        self._message = 'Steam 未确认出售结果，请核对市场记录；不会重复提交此资产。'
                    elif response.get('requires_confirmation'):
                        row['status'] = 'pending'
                        self._message = '卖单已提交，请在 Steam 手机或邮件中确认；暂不重复出售此资产。'
                    else:
                        row['status'] = 'listed'
                        self._message = 'Steam 已接受挂牌；是否成交请以市场记录为准。'
                    if response.get('listingid'):
                        row['listingid'] = _assetid(response['listingid'])
                except Exception:
                    row['status'] = 'uncertain'
                    self._message = 'Steam 出售请求结果未确认，请核对市场记录；不会重复提交此资产。'
                    raise
                finally:
                    self._save()
                self._status = row['status']
                return self.state()
            self._status = 'idle'
            return self.state()
        except SteamSessionError as exc:
            self._message = str(exc)
            self._status = 'login_required' if exc.status == 401 else 'waiting'
            if exc.status in (403, 429):
                self._retry_after = self.clock() + max(300, min(3600, exc.retry_after))
            else:
                self._retry_after = self.clock() + 30
            return self.state()
        except MarketDataError:
            self._message = 'Steam 行情或物品身份未匹配，暂不出售。'
            self._status = 'waiting'
            self._retry_after = self.clock() + 60
            return self.state()
        except Exception:
            # Never propagate HTML, cookies, server bodies, or exception text.
            self._message = 'Steam 操作未确认，已暂缓出售；托管刷图可继续。'
            self._status = 'waiting'
            self._retry_after = self.clock() + 60
            return self.state()
        finally:
            self._operation.release()
