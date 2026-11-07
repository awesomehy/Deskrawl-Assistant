"""Exact Steam listing tracking and explicitly confirmed cancel/relist operations.

All methods that perform I/O run under SteamTradingService._operation. Public
snapshots are local projections only. Absence from active listings is never a
sale receipt. A repricing write is fenced durably before either POST.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import secrets
import time
from urllib.parse import quote, urlencode

from .steam_market import APP_ID, CURRENCIES, parse_listing
from .steam_session import SteamSessionError

LISTING_INTERVAL = 5
LISTING_TTL = 12
HISTORY_INTERVAL = 15
HISTORY_PAGES_PER_PASS = 5
HISTORY_SCAN_TTL = 600
QUOTE_REFRESH_INTERVAL = 60
QUOTE_DISPLAY_TTL = 600
REPRICE_RECOVERY_TTL = 600


class RepriceDeferred(SteamSessionError):
    """Cancellation is fenced; only return verification may be retried."""
    def __init__(self, record_id, message):
        super().__init__(message)
        self.record_id = record_id


class _RepriceUnsafe(SteamSessionError):
    """The original authorization or physical return identity is invalid."""


def removal_acknowledged(raw):
    """Cancel has a scalar JSON receipt, unlike the sell-item endpoint.

    Steam's own remove-listing UI accepts a truthy JSON response. Accept its
    explicit success forms here without weakening the object-only sell parser.
    This receipt alone never authorizes a new sale: the caller also verifies
    disappearance of the old listing and the exact returned inventory delta.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, UnicodeError):
            return False
    if raw is True or type(raw) is int and raw == 1:
        return True
    if isinstance(raw, dict) and raw.get('success') in (True, 1):
        return True
    if (raw is False or type(raw) is int and raw == 0
            or isinstance(raw, dict) and 'success' in raw):
        raise SteamSessionError('Steam 拒绝下架，未重新出售。')
    # An unrecognised receipt is not success or permission to replay the POST.
    # The caller may only continue with read-only outcome verification.
    return False


def return_signature(asset):
    """Exact Steam description-class signature, not a physical identity alone.

    Cancellation may mint a new asset id and omit the game's GUID. Identical
    class metadata alone cannot authorize re-selling that asset. A changed id
    additionally requires the bounded before/after cancellation history proof.
    """
    import re
    description = asset.get('description', {})
    classid = str(asset.get('classid', description.get('classid', '')))
    instanceid = str(asset.get('instanceid', description.get('instanceid', '0')))
    if (str(description.get('appid')) != str(APP_ID)
            or not re.fullmatch(r'[1-9]\d{0,24}', classid)
            or not re.fullmatch(r'\d{1,25}', instanceid)
            or not description.get('market_hash_name')):
        return None
    identity = [str(APP_ID), '2', classid, instanceid,
        {k: description.get(k) for k in ('market_hash_name', 'type', 'descriptions',
                                         'owner_descriptions', 'properties')}]
    return sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def amount_text(value, currency_id):
    if type(value) is not int or value < 0 or currency_id not in CURRENCIES:
        return None
    return f'{CURRENCIES[currency_id]} {Decimal(value) / 100:.2f}'


def project_receipt(receipt):
    """Shared projection used by both managed and manual item lists."""
    status = receipt.get('status')
    value = {k: receipt[k] for k in ('tracking_id', 'my_price', 'lowest_price', 'lowest_price_cached',
        'lowest_price_checked_at', 'lowest_price_expires_at', 'proceeds',
        'can_reprice', 'reprice_available', 'currency', 'checked_at') if k in receipt}
    if status == 'sold':
        value.update(status='sold', message='当前背包、仓库和交易列表均无此物品，按已出售处理；到账金额未核实。'
                     if receipt.get('assumed_sold') else '已在 Steam 成交记录中核实售出。' if receipt.get('proceeds')
                     else '已核实售出；实际到账金额尚未核实。')
    elif status == 'listed' and receipt.get('active') is not False:
        value.update(status='listed', message='已上架 Steam 市场，暂未售出。')
    elif receipt.get('confirmation_pending') is True:
        value.update(status='pending_confirmation', message='手机令牌暂未确认；确认后自动核验挂牌状态。')
    elif status == 'captured':
        value.update(status='uploaded', message='已上架游戏交易所，暂未上架 Steam 市场。')
    else:
        value.update(status='uncertain', message=receipt.get('tracking_message') or
                     '卖单状态待核验；查询失败或卖单消失均不会被视为售出，也不会重复提交。')
        previous = receipt.get('previous_status')
        if previous in ('uploaded', 'pending_confirmation', 'listed'):
            value['previous_status'] = previous
        value['needs_verification'] = True
        value['can_reprice'] = False
    return value


class SaleTrackingMixin:
    def _reprice_reserved(self, steamid, item_key, variant):
        # Published immutable set: UI previews must not wait on Steam I/O.
        return (steamid, item_key, variant) in getattr(self, '_reprice_reservations', ())

    def _publish_reprice_reservations(self):
        self._reprice_reservations = frozenset(
            (task['steamid'], task['item_key'], task['variant'])
            for task in self._reprice_recoveries.values())

    @staticmethod
    def _reprice_intent(row):
        pending = row.get('reprice') or {}
        return deepcopy(([row.get(k) for k in ('steamid', 'item_key', 'variant', 'assetid',
            'game_instance_id', 'amount', 'sale_amount', 'currency_id', 'publisher_fee')],
            {k: pending.get(k) for k in ('phase', 'listingid', 'buyer_total', 'seller_receive',
                                       'started_at', 'return_signature', 'return_history', 'before')}))

    def _forget_reprice_recovery(self, row):
        self._reprice_recoveries.pop(row['id'], None)
        self._publish_reprice_reservations()

    def _check_reprice_authorization(self, row, task):
        if (self.clock() >= task['expires'] or self._reprice_intent(row) != task['intent']
                or not self._can_submit(task['validate'])):
            raise _RepriceUnsafe('改价授权已结束或账号、角色、登录及原交易已变化；未继续重新上架。')
        auth = self.auth_state()
        # peek_state can be unavailable while another thread holds the session
        # lock. A failed wallet GET also clears the cached wallet/steamid while
        # retaining the SAME binding generation. Neither is explicit revocation.
        if auth is None:
            raise SteamSessionError('Steam 登录暂未核实，继续等待原改价核验。')
        if (auth.get('expired') or auth.get('bound') is False
                or auth.get('binding_generation') != task['generation']
                or auth.get('steamid') is not None and auth['steamid'] != task['steamid']):
            raise _RepriceUnsafe('改价授权已结束或账号、角色、登录及原交易已变化；未继续重新上架。')
        if auth.get('verified') is not True or auth.get('steamid') != task['steamid']:
            raise SteamSessionError('Steam 登录暂未核实，继续等待原改价核验。')

    def _defer_reprice(self, row):
        task = self._reprice_recoveries[row['id']]
        self._check_reprice_authorization(row, task)
        task['attempts'] += 1
        task['next_at'] = self.clock() + min(5 * 2 ** min(task['attempts'] - 1, 4), 60)
        row['reprice']['last_checked_at'] = self.clock()
        row['status'] = 'uncertain'
        row['tracking_message'] = ('下架已提交，退回物品仍待核验，未重新上架；后台继续核验，确认后按原设定价格 '
            + str(amount_text(row['reprice']['buyer_total'], row['currency_id']))
            + ' 重新上架，不重复下架。')
        self._save()
        return RepriceDeferred(row['id'], row['tracking_message'])

    def _failed_reprice(self, row, message):
        self._forget_reprice_recovery(row)
        row['status'] = 'uncertain'
        row['tracking_message'] = ('未重新上架；' if row.get('reprice', {}).get('phase') == 'cancel_submitted'
                                   else '') + message
        self._save()

    def reprice_recovery_state(self, record_id):
        row = next((r for r in self._receipt_rows if r['id'] == record_id), None)
        if not row:
            return {'running': False, 'error': True, 'message': '原改价记录待核验，不重复提交。'}
        if not row.get('reprice') and row['status'] in ('pending', 'listed', 'sold'):
            return {'running': False, 'message': '已按原设定价格提交重挂；可能需要 Steam 手机确认。'}
        waiting = record_id in self._reprice_recoveries
        return {'running': False, 'waiting': waiting, 'error': not waiting,
                'message': row.get('tracking_message') or '改价结果待核验，不重复提交。'}

    def resume_reprices(self):
        """One bounded pass on the existing worker; never replay a fenced POST.

        Recovery retains the original in-process game authorization callback.
        Restart/close/logout discards it; historical journals are never adopted
        as new sale permission. Both durable POST fences survive unchanged.
        """
        if not self._reprice_recoveries or not self._operation.acquire(blocking=False):
            return False
        try:
            self._healthy_journal()
            for record_id, task in list(self._reprice_recoveries.items()):
                row = next((r for r in self._journal if r['id'] == record_id), None)
                if not row or row.get('reprice', {}).get('phase') != 'cancel_submitted':
                    self._reprice_recoveries.pop(record_id, None)
                    self._publish_reprice_reservations()
                    continue
                try:
                    self._check_reprice_authorization(row, task)
                except _RepriceUnsafe as exc:
                    self._failed_reprice(row, str(exc))
                    continue
                except SteamSessionError:
                    continue  # Read-only auth refresh may still recover.
                if self.clock() < max(task['next_at'], self._retry_after):
                    continue
                # Schedule before I/O, including a failed read. No busy loop.
                task['next_at'] = self.clock() + 5
                try:
                    wallet = self._account(row['steamid'])
                    self._check_reprice_authorization(row, task)
                    self._finish_reprice(row, wallet, task['validate'], attempts=1)
                except _RepriceUnsafe as exc:
                    self._failed_reprice(row, str(exc))
                except SteamSessionError as exc:
                    self._limited(exc)
                    if exc.auth_expired or exc.status == 401:
                        self._failed_reprice(row, 'Steam 登录已失效，未继续重新上架。')
                    elif row.get('reprice', {}).get('phase') == 'cancel_submitted':
                        try:
                            self._defer_reprice(row)
                        except _RepriceUnsafe as invalid:
                            self._failed_reprice(row, str(invalid))
                        except SteamSessionError:
                            pass  # The next auth refresh must verify the same generation.
                    else:
                        self._failed_reprice(row, '重挂请求已记录，实际结果待核验；不会重复出售。')
                except Exception:
                    self._failed_reprice(row, '改价结果待核验；不会自动重复下架或出售，请检查 Steam 市场和库存。')
                return True
            return False
        finally:
            self._operation.release()

    def _return_history(self, steamid, generation):
        from .steam_return_receipts import inventory_history
        document = self.session.request('GET', f'/profiles/{steamid}/inventoryhistory/?'
            + urlencode({'app[]': APP_ID, 'l': 'english'}))
        auth = self.session.state()
        if (auth.get('verified') is not True or auth.get('expired')
                or auth.get('steamid') != steamid or auth.get('binding_generation') != generation):
            raise SteamSessionError('Steam 登录已变化，未采用旧账号的退回记录。')
        return inventory_history(document, steamid)

    def _return_baseline(self, asset, steamid, generation):
        """Keep the complete original same-class order set for return proof."""
        from .steam_return_receipts import history_anchor
        signature = return_signature(asset)
        if not signature:
            return {}
        assets = getattr(self, '_listing_assets', {})
        view = getattr(self, '_observed_listings', {})
        description = asset['description']
        classid, instanceid = str(description.get('classid')), str(description.get('instanceid', '0'))
        if (view.get('fresh') is not True or set(assets) != set(view.get('listing_ids', []))
                or any(return_signature(a) is None for a in assets.values())):
            return {}
        orders = {lid: a['assetid'] for lid, a in assets.items()
                  if str(a['description'].get('classid')) == classid
                  and str(a['description'].get('instanceid', '0')) == instanceid}
        rows = self._return_history(steamid, generation)
        return {'anchor': history_anchor(rows[0]), 'classid': classid, 'instanceid': instanceid, 'orders': orders}

    def _changed_return_proof(self, row, candidate, generation):
        from .steam_return_receipts import cancellation_return
        pending = row['reprice']
        baseline = pending.get('return_history', {})
        if (not baseline or not pending.get('return_signature')
                or return_signature(candidate) != pending['return_signature']):
            return False
        rows = self._return_history(row['steamid'], generation)
        return cancellation_return(rows, baseline['anchor'], baseline['classid'], baseline['instanceid'],
                                   candidate['assetid'], row.get('sale_amount', row['amount']))

    def _tracking_fields(self, row):
        auth = self.auth_state() or {}
        same_account = (auth.get('verified') is True and not auth.get('expired')
                        and auth.get('steamid') == row['steamid'])
        active = (same_account and self.clock() < getattr(self, '_active_until', 0)
                  and getattr(self, '_active_generation', None) == auth.get('binding_generation')
                  and row.get('active_checked_at') == getattr(self, '_active_checked_at', None)
                  and bool(row.get('listingid')) and row.get('status') == 'listed')
        currency = row.get('currency_id')
        quote_data = getattr(self, '_tracking_quotes', {}).get((row['item_key'], row['variant']), {})
        fresh_quote = (same_account and quote_data.get('steamid') == row['steamid']
                       and quote_data.get('generation') == auth.get('binding_generation')
                       and self.clock() < quote_data.get('expires_at', 0)
                       and quote_data.get('currency_id') == currency
                       and auth.get('wallet_currency') in (None, CURRENCIES.get(currency)))
        return {'tracking_id': row.get('id'), 'active': active,
                'previous_status': row.get('last_confirmed_status') or
                    {'captured': 'uploaded', 'pending': 'pending_confirmation', 'listed': 'listed'}.get(row['status']),
                'currency': CURRENCIES.get(currency),
                'my_price': amount_text(row.get('buyer_total'), currency),
                'lowest_price': amount_text(quote_data.get('lowest'), currency) if fresh_quote else None,
                'lowest_price_cached': bool(fresh_quote and self.clock() >= quote_data.get('checked_at', 0) + QUOTE_REFRESH_INTERVAL),
                'lowest_price_checked_at': quote_data.get('checked_at') if fresh_quote else None,
                'lowest_price_expires_at': quote_data.get('expires_at') if fresh_quote else None,
                'proceeds': amount_text(row.get('proceeds'), row.get('proceeds_currency_id')),
                'checked_at': row.get('sold_at', row.get('assumed_sold_at')) if row['status'] == 'sold' else row.get('active_checked_at'),
                'can_reprice': bool(active and not row.get('reprice')),
                # Opening a read-only preview is allowed during a stale listing
                # lease. Opening the editor grants no write permission; the
                # final write re-reads the exact order before cancelling it.
                'reprice_available': bool(same_account and row.get('listingid') and not row.get('reprice')
                    and (row['status'] == 'listed' or row.get('last_confirmed_status') == 'listed')
                    and row['status'] != 'sold'),
                'tracking_message': row.get('tracking_message', '')}

    def _tracking_history(self, steamid, rows):
        """Use Steam's structured history, matching listing AND exact asset.

        Event 3 is a completed purchase. Our known sell listing must be the
        corresponding seller-side listing, never a purchase by this account.
        Buyer payment is in the buyer's currency and is NOT wallet proceeds.
        """
        from .steam_trading import _json
        targets = list(rows)
        if not targets:
            return
        auth = self.session.state()
        scope = (steamid, auth.get('binding_generation'), tuple(sorted(
            (r['id'], r['assetid'], r.get('listingid'), r.get('seller_receive'),
             r.get('submitted_at'), r.get('sale_amount', r['amount']), r.get('currency_id'))
            for r in targets)))
        scan = getattr(self, '_history_scan', None)
        # Publish a continuation only after a successful pass. Failed reads
        # cannot leave partial evidence available for a later sale decision.
        self._history_scan = None

        def read_page(start):
            response = _json(self.session.request('GET', '/market/myhistory/?' +
                urlencode({'start': start, 'count': 100, 'l': 'english', 'norender': 1})))
            current = self.session.state()
            if (current.get('verified') is not True or current.get('expired')
                    or current.get('steamid') != steamid
                    or current.get('binding_generation') != auth.get('binding_generation')):
                raise SteamSessionError('Steam 登录已变化，未采用旧账号的成交响应。')
            if response.get('success') not in (True, 1):
                raise SteamSessionError('Steam 成交记录暂不可读。')
            events, listings, purchases = (response.get(k) for k in ('events', 'listings', 'purchases'))
            if not isinstance(events, list) or not isinstance(listings, dict) or not isinstance(purchases, dict):
                raise SteamSessionError('Steam 成交记录格式尚未核实。')
            total = response.get('total_count')
            if (type(total) is not int or total < 0
                    or len(events) != min(100, max(0, total - start))
                    or any(not isinstance(event, dict) for event in events)):
                raise SteamSessionError('Steam 成交记录分页尚未确认。')
            fingerprint = sha256(json.dumps(events, sort_keys=True).encode('utf-8')).digest()
            return events, listings, purchases, total, fingerprint

        head = read_page(0)
        if (not scan or scan['scope'] != scope or self.clock() >= scan['expires']
                or scan['total'] != head[3] or scan['head'] != head[4]):
            scan = {'scope': scope, 'expires': self.clock() + HISTORY_SCAN_TTL,
                    'total': head[3], 'head': head[4], 'next': 0, 'receipts': {}, 'pages': set()}
        receipts = scan['receipts']

        def consume_page(page, start):
            events, listings, purchases, total, fingerprint = page
            if total != scan['total'] or fingerprint in scan['pages']:
                raise SteamSessionError('Steam 成交记录正在变化，稍后重新核验。')
            scan['pages'].add(fingerprint)
            for event in events:
                if not isinstance(event, dict) or event.get('event_type') != 3:
                    continue
                lid = str(event.get('listingid', ''))
                listing = listings.get(lid, {})
                purchase = purchases.get(lid + '_' + str(event.get('purchaseid', '')), {})
                if not isinstance(listing, dict) or not isinstance(purchase, dict):
                    continue
                asset = listing.get('asset', {})
                purchased = purchase.get('asset', {})
                if not isinstance(asset, dict) or not isinstance(purchased, dict):
                    continue
                matching = [r for r in targets if r.get('listingid') == lid
                            and str(asset.get('id')) == r['assetid']]
                if not matching and not any(r.get('listingid') == lid and r['steamid'] == steamid for r in self._journal):
                    matching = [r for r in targets if not r.get('listingid') and str(asset.get('id')) == r['assetid']
                        and listing.get('original_price') == r.get('seller_receive')
                        and sum(other['steamid'] == steamid and other['assetid'] == r['assetid']
                                for other in self._journal) == 1]
                row = matching[0] if len(matching) == 1 else None
                if (not row or not isinstance(purchase, dict) or not isinstance(asset, dict)
                        or asset.get('appid') != APP_ID or str(asset.get('contextid')) != '2'
                        or str(asset.get('id')) != row['assetid']
                        # Listing amount is the REMAINDER (zero after sale).
                        # Purchase asset records the quantity transferred to
                        # the buyer; its new asset ID need not match ours.
                        or purchased.get('appid') != APP_ID or str(purchased.get('contextid')) != '2'
                        or str(purchased.get('amount')) != str(row.get('sale_amount', row['amount']))
                        or purchase.get('steamid_purchaser') in (None, '', steamid)
                        or purchase.get('failed') != 0 or purchase.get('needs_rollback') != 0
                        or purchase.get('funds_returned', 0) != 0
                        or str(purchase.get('listingid', lid)) != lid
                        or type(event.get('time_event')) is not int
                        or event['time_event'] < int(row.get('submitted_at', 10**20))):
                    continue
                receipt_key = (lid, str(event.get('purchaseid', '')))
                matches = receipts.setdefault(row['id'], {})
                # Two distinct receipts already prove ambiguity. Bound retained
                # evidence even for a long history or a repeatedly relisted asset.
                if receipt_key in matches or len(matches) < 2:
                    matches[receipt_key] = (event, purchase)
            scan['next'] = start + 100

        if scan['next'] == 0:
            consume_page(head, 0)
        # Recheck the first page on every pass before continuing old offsets.
        # New transactions, account changes or failures restart the scan. Each
        # worker pass remains bounded, but older sales no longer starve at 500.
        for _ in range(HISTORY_PAGES_PER_PASS - 1):
            if scan['next'] >= scan['total']:
                break
            if all(row.get('listingid') and len(receipts.get(row['id'], {})) == 1 for row in targets):
                break
            start = scan['next']
            consume_page(read_page(start), start)
        complete = scan['next'] >= scan['total']
        if not complete:
            self._history_scan = scan
        changed = False
        for row in targets:
            matches = receipts.get(row['id'], {})
            # Without a previously verified listing ID, require a complete,
            # bounded scan and exactly one matching purchase. Never pick the
            # first of several sales involving an asset that was relisted.
            if len(matches) != 1 or not row.get('listingid') and not complete:
                continue
            (lid, _), (event, purchase) = next(iter(matches.items()))
            row.update(status='sold', listingid=lid, sold_at=event['time_event'], tracking_message='')
            row.pop('reprice', None)
            received, denomination = purchase.get('received_amount'), purchase.get('received_currencyid')
            if (type(received) is int and received >= 0 and str(denomination).isdigit()
                    and int(denomination) in (row.get('currency_id'), 2000 + row.get('currency_id', 0))):
                row.update(proceeds=received, proceeds_currency_id=row['currency_id'])
            changed = True
        if changed:
            self._save()

    def _tracking_quote(self, steamid, row, wallet=None):
        wallet = wallet or self._account(steamid)
        item = self._item(row['item_key'], row['variant'])
        quote_data = parse_listing(self.session.request('GET', f'/market/listings/{APP_ID}/' +
                                  quote(item.market_hash_name, safe='') + '?l=english'), item)
        if quote_data['currency_id'] != wallet['currency_id']:
            raise SteamSessionError('Steam 行情与钱包币种不一致。')
        auth = self.auth_state() or {}
        if (auth.get('verified') is not True or auth.get('expired') or auth.get('steamid') != steamid
                or auth.get('binding_generation') != wallet.get('binding_generation')
                or auth.get('wallet_currency') not in (None, CURRENCIES.get(wallet['currency_id']))):
            raise SteamSessionError('Steam 登录或钱包币种已变化，未采用旧行情。')
        raw = quote_data.get('lowest_sell')
        lowest = Decimal(raw) * 100 if raw is not None else None
        bid = quote_data.get('highest_buy')
        highest = Decimal(bid) * 100 if bid is not None else None
        for value in (lowest, highest):
            if value is not None and (not value.is_finite() or value != value.to_integral_value() or value <= 0):
                raise SteamSessionError('Steam 行情金额无效。')
        if not hasattr(self, '_tracking_quotes'):
            self._tracking_quotes = {}
        result = {'lowest': int(lowest) if lowest is not None else None, 'currency_id': wallet['currency_id'],
                  'highest': int(highest) if highest is not None else None,
                  'steamid': steamid, 'generation': wallet.get('binding_generation'), 'checked_at': self.clock(),
                  'refresh_after': self.clock() + QUOTE_REFRESH_INTERVAL, 'expires_at': self.clock() + QUOTE_DISPLAY_TTL}
        self._tracking_quotes[(row['item_key'], row['variant'])] = result
        return result

    def _price_preview_quote(self, steamid, row, currency, auth):
        """Immediate local guidance; queue optional reads on the existing worker."""
        now = self.clock()
        key = (row['item_key'], row['variant'])
        cached = getattr(self, '_tracking_quotes', {}).get(key, {})
        scope = (steamid, auth.get('binding_generation'), currency)
        same_scope = tuple(cached.get(k) for k in ('steamid', 'generation', 'currency_id')) == scope
        usable = same_scope and now < cached.get('expires_at', 0)
        request_id = (*scope, *key)
        with self.guard:
            requests = getattr(self, '_price_preview_requests', {})
            requests = {k: v for k, v in requests.items() if now < v['expires']}
            if (not self._closed and now >= self._retry_after
                    and (not same_scope or now >= cached.get('refresh_after', 0))
                    and request_id not in requests and len(requests) < 16):
                requests[request_id] = {'steamid': steamid, 'generation': scope[1], 'currency_id': currency,
                    'item_key': key[0], 'variant': key[1], 'expires': now + 60}
            self._price_preview_requests = requests
            pending = request_id in requests and now >= self._retry_after
        lowest = amount_text(cached.get('lowest'), currency) if usable else None
        return {'lowest_price': lowest,
                'fee_parameters': self._preview_fee_parameters(steamid, currency, auth, row.get('publisher_fee')),
                'highest_price': amount_text(cached.get('highest'), currency) if usable else None,
                'default_price': lowest.split(' ', 1)[1] if lowest else '',
                'lowest_price_cached': bool(usable),
                'lowest_price_checked_at': cached.get('checked_at') if usable else None,
                'lowest_price_expires_at': cached.get('expires_at') if usable else None,
                'quote_pending': pending, 'quote_known': bool(usable),
                'quote_error': ('Steam 暂时限制请求，行情稍后重试；可先手填价格。' if now < self._retry_after
                    else cached.get('preview_error', '') if same_scope and not pending else '')}

    def refresh_price_previews(self):
        """One prioritized read, no new thread or economic operation."""
        if self._closed or not self._operation.acquire(blocking=False):
            return False
        request_id = None
        try:
            auth = self.auth_state() or {}
            with self.guard:
                requests = getattr(self, '_price_preview_requests', {})
                requests = {k: v for k, v in requests.items() if self.clock() < v['expires']
                    and auth.get('verified') is True and not auth.get('expired')
                    and v['steamid'] == auth.get('steamid')
                    and v['generation'] == auth.get('binding_generation')
                    and CURRENCIES.get(v['currency_id']) == auth.get('wallet_currency')}
                self._price_preview_requests = requests
                if not requests or self.clock() < self._retry_after:
                    return False
                request_id, request = next(iter(requests.items()))
            try:
                self._tracking_quote(request['steamid'], request)
            except (SteamSessionError, ValueError, KeyError) as exc:
                if isinstance(exc, SteamSessionError):
                    self._limited(exc)
                key = (request['item_key'], request['variant'])
                cached = getattr(self, '_tracking_quotes', {}).get(key, {})
                same_scope = all(cached.get(k) == request[k] for k in ('steamid', 'generation', 'currency_id'))
                if not hasattr(self, '_tracking_quotes'):
                    self._tracking_quotes = {}
                self._tracking_quotes[key] = {**(cached if same_scope else {
                    k: request[k] for k in ('steamid', 'generation', 'currency_id')}),
                    'refresh_after': self.clock() + 30,
                    'preview_error': '本次行情未读到，可先手填价格或稍后重新读取。'}
            return True
        finally:
            if request_id is not None:
                with self.guard:
                    # An expired request can be replaced while its slow read
                    # finishes. Do not discard the newer editor's request.
                    if self._price_preview_requests.get(request_id) is request:
                        self._price_preview_requests.pop(request_id, None)
            self._operation.release()

    def _refresh_tracking(self, steamid, rows):
        pending = (self._confirmation or {}).get('assets', ())
        absent = [r for r in rows if r.get('active_checked_at') != getattr(self, '_active_checked_at', None)
                  and r['assetid'] not in pending and r['status'] in ('listed', 'pending', 'uncertain')]
        if absent and self.clock() >= getattr(self, '_history_after', 0):
            self._history_after = self.clock() + HISTORY_INTERVAL
            try:
                self._tracking_history(steamid, absent)
            except SteamSessionError as exc:
                self._limited(exc)
            except (ValueError, KeyError):
                pass  # No positive sale evidence: leave the item unconfirmed.
        if self.clock() < self._retry_after:
            return
        # At most one new quote per worker pass; no N*network reads on UI GET.
        # Oldest/never-read quotes first so a long list cannot starve behind
        # its first few items as their short-lived quotes expire again.
        for row in sorted((r for r in rows if r['status'] == 'listed'), key=lambda r:
                getattr(self, '_tracking_quotes', {}).get((r['item_key'], r['variant']), {}).get('refresh_after', 0)):
            key = (row['item_key'], row['variant'])
            cached = getattr(self, '_tracking_quotes', {}).get(key, {})
            same_scope = (cached.get('steamid') == steamid
                          and cached.get('generation') == (self.auth_state() or {}).get('binding_generation'))
            if same_scope and self.clock() < cached.get('refresh_after', 0):
                continue
            try:
                self._tracking_quote(steamid, row)
            except (SteamSessionError, ValueError, KeyError) as exc:
                if isinstance(exc, SteamSessionError):
                    self._limited(exc)
                if not hasattr(self, '_tracking_quotes'):
                    self._tracking_quotes = {}
                # A failed refresh never overwrites the last successful quote.
                # Keep its original display deadline; backoff cannot renew it.
                self._tracking_quotes[key] = {**cached,
                    'refresh_after': self.clock() + 30} if same_scope else {
                        'steamid': steamid, 'generation': (self.auth_state() or {}).get('binding_generation'),
                        'refresh_after': self.clock() + 30}
            break

    def reprice_preview(self, record_id):
        # Price editing is a local projection, just like the tracking list.
        # Do not wait behind the worker's history, wallet or quote requests.
        # Use the published durable snapshot, never a partially modified row.
        self._healthy_journal()
        row = next((r for r in self._receipt_rows if r.get('id') == record_id), None)
        if not row:
            raise SteamSessionError('未找到此卖单的精确上传记录。')
        auth = self.auth_state() or {}
        fields = self._tracking_fields(row)
        if (self._closed or not fields['reprice_available'] or auth.get('verified') is not True
                or auth.get('expired') or auth.get('steamid') != row['steamid']):
            raise SteamSessionError('原卖单或 Steam 登录状态已变化，请刷新后重试。')
        currency = next((k for k, v in CURRENCIES.items() if v == auth.get('wallet_currency')), None)
        if currency is None or row.get('currency_id') not in (None, currency):
            raise SteamSessionError('Steam 钱包币种尚未确认，请在托管模式刷新 Steam 登录状态。')
        current = self.auth_state() or {}
        if any(current.get(k) != auth.get(k) for k in
               ('verified', 'expired', 'steamid', 'binding_generation', 'wallet_currency')):
            raise SteamSessionError('Steam 登录已变化，请重新打开改价窗口。')
        # _tracking_fields applies account, login-generation, currency and TTL
        # checks. A quote is optional guidance, never authority to cancel.
        price = self._price_preview_quote(row['steamid'], row, currency, auth)
        lowest = price['lowest_price']
        with self.guard:
            token = secrets.token_urlsafe(32)
            if not hasattr(self, '_reprice_previews'):
                self._reprice_previews = {}
            self._reprice_previews = {k: v for k, v in self._reprice_previews.items() if self.clock() < v['expires']}
            # Reopening the same editor replaces its old confirmation token.
            self._reprice_previews = {k: v for k, v in self._reprice_previews.items() if v['id'] != record_id}
            self._reprice_previews[token] = {'id': record_id, 'listingid': row['listingid'], 'assetid': row['assetid'],
                'steamid': row['steamid'], 'generation': auth.get('binding_generation'),
                'currency_id': currency, 'expires': self.clock() + 120}
            return {'token': token, 'tracking_id': record_id, 'currency': CURRENCIES[currency],
                    'my_price': amount_text(row.get('buyer_total'), row.get('currency_id')),
                    **price,
                    'message': ('已填入最近成功读取的底价，可修改；' if lowest else '暂无有效底价，可手填价格；')
                        + '确认后才实时核验原卖单、登录及钱包，核验通过才下架。'}

    def reprice(self, token, price, *, validate=None):
        from .steam_trading import _json, fee_parameters, receive_for_total
        with self._operation:
            with self.guard:
                preview = getattr(self, '_reprice_previews', {}).pop(token, None)
            if not preview or self.clock() >= preview['expires'] or not self._can_submit(validate):
                raise SteamSessionError('改价确认已失效，请重新核验。')
            row = next((r for r in self._journal if r.get('id') == preview['id']), None)
            if (not row or not (row['status'] == 'listed' or row['status'] == 'uncertain'
                    and row.get('last_confirmed_status') == 'listed') or row.get('reprice')
                    or row.get('listingid') != preview['listingid'] or row['assetid'] != preview['assetid']):
                raise SteamSessionError('原卖单已变化，未执行下架。')
            if self._reprice_reserved(row['steamid'], row['item_key'], row['variant']):
                raise SteamSessionError('同类物品的原改价尚未核验完成，未再次下架。')
            wallet = self._account(preview['steamid'])
            if wallet.get('binding_generation') != preview['generation']:
                raise SteamSessionError('Steam 登录已变化，请重新核验改价。')
            if wallet['currency_id'] != preview['currency_id']:
                raise SteamSessionError('Steam 钱包币种已变化，请重新核验价格。')
            try:
                total = Decimal(price) * 100
            except (InvalidOperation, TypeError, ValueError):
                raise SteamSessionError('请输入有效的买方支付总价。') from None
            if not total.is_finite() or total != total.to_integral_value() or total <= 0:
                raise SteamSessionError('价格必须为正数，最多两位小数。')
            parameters = fee_parameters(wallet, row.get('publisher_fee', wallet.get('publisher_fee')))
            receive = receive_for_total(int(total), parameters)  # Validate BEFORE cancelling.
            self._reconcile_after = 0
            self._reconcile_listings(row['steamid'])
            if not self._tracking_fields(row)['can_reprice']:
                raise SteamSessionError('原卖单已不在售，未执行下架。')
            before = self._inventory(row['steamid'])
            listing_asset = getattr(self, '_listing_assets', {}).get(row['listingid'], {})
            signature = return_signature(listing_asset) if listing_asset.get('assetid') == row['assetid'] else None
            history_baseline = (self._return_baseline(listing_asset, row['steamid'], wallet.get('binding_generation'))
                                if signature else {})
            if not self._can_submit(validate):
                raise SteamSessionError('当前玩家或操作状态已变化，未执行下架。')
            # Record both POST fences durably. A restart never replays either.
            row['reprice'] = {'phase': 'cancel_submitted', 'listingid': row['listingid'],
                              'buyer_total': int(total), 'seller_receive': receive, 'started_at': self.clock(),
                              'return_signature': signature,
                              'return_history': history_baseline,
                              'before': {identity: a['amount'] for identity, a in before.items()
                                         if self._matches(a, self._item(row['item_key'], row['variant']))}}
            row['tracking_message'] = '正在下架并核验退回物品，请勿重复操作。'
            self._save()
            try:
                if not self._can_submit(validate):
                    raise SteamSessionError('助手正在退出，未继续提交。')
                try:
                    receipt = self.session.post('/market/removelisting/' + row['listingid'], {},
                        expected_steamid=row['steamid'], expected_generation=wallet.get('binding_generation'))
                except SteamSessionError as exc:
                    if exc.status or exc.auth_expired:
                        raise
                    # Lost response: do not repeat cancellation. Exact current
                    # listing/inventory evidence can still confirm its outcome.
                    receipt = None
                row['reprice']['cancel_acknowledged'] = removal_acknowledged(receipt)
                self._active_until = 0
                self._confirmation = None
                self._reconcile_after = 0
                self._save()
                self._reprice_recoveries[row['id']] = {
                    'steamid': row['steamid'], 'item_key': row['item_key'], 'variant': row['variant'],
                    'generation': wallet.get('binding_generation'), 'validate': validate,
                    'intent': self._reprice_intent(row), 'attempts': 0, 'next_at': self.clock(),
                    'expires': row['reprice']['started_at'] + REPRICE_RECOVERY_TTL}
                self._publish_reprice_reservations()
                self._finish_reprice(row, wallet, validate)
            except SteamSessionError as exc:
                self._limited(exc)
                if (row['id'] in self._reprice_recoveries
                        and row.get('reprice', {}).get('phase') == 'cancel_submitted'
                        and not isinstance(exc, _RepriceUnsafe) and not exc.auth_expired and exc.status != 401):
                    try:
                        deferred = self._defer_reprice(row)
                    except _RepriceUnsafe as invalid:
                        self._failed_reprice(row, str(invalid))
                        raise
                    except SteamSessionError:
                        row['status'] = 'uncertain'
                        row['tracking_message'] = '下架已提交，等待同一 Steam 登录恢复核验；未重新上架。'
                        self._save()
                        raise RepriceDeferred(row['id'], row['tracking_message']) from None
                    raise deferred from None
                self._failed_reprice(row, str(exc) + ' 不会自动重复下架或出售。')
                raise
            except Exception:
                self._failed_reprice(row, '下架或重挂结果待核验；不会自动重复下架或出售，请核验 Steam 市场和库存。')
                raise

    def _finish_reprice(self, row, wallet, validate, *, attempts=8):
        """Shared immediate/background checks; at most one new sell POST."""
        from .steam_trading import _json, fee_parameters, receive_for_total
        task = self._reprice_recoveries[row['id']]
        self._check_reprice_authorization(row, task)
        if (wallet.get('binding_generation') != task['generation']
                or wallet['currency_id'] != row['currency_id']):
            raise _RepriceUnsafe('Steam 登录或钱包币种已变化，未重新上架。')
        before = {identity: {'amount': amount} for identity, amount in row['reprice']['before'].items()}
        total, receive = row['reprice']['buyer_total'], row['reprice']['seller_receive']
        item = self._item(row['item_key'], row['variant'])
        returned = None
        deadline = time.monotonic() + 20
        for attempt in range(attempts):
            row['reprice']['verification'] = 'inventory_return'
            inventory = self._inventory(row['steamid'])
            added = [a for identity, a in inventory.items() if self._matches(a, item)
                     and a['amount'] > before.get(identity, {}).get('amount', 0)]
            if (len(added) > 1 or sum(a['amount'] - before.get(a['assetid'], {}).get('amount', 0)
                                     for a in added) > row.get('sale_amount', row['amount'])):
                raise _RepriceUnsafe('下架后出现多个或超额新增资产，未猜测退回物品；未重新出售。')
            candidate = added[0] if len(added) == 1 else None
            if candidate and any(other['id'] != row['id'] and other['steamid'] == row['steamid']
                    and other['assetid'] == candidate['assetid'] and other['status'] != 'sold'
                    for other in self._journal):
                raise _RepriceUnsafe('退回候选资产已有其他交易记录，未重复出售。')
            instance = self._native_instance(candidate) if candidate else None
            if candidate and row.get('game_instance_id') and instance and instance != row['game_instance_id']:
                raise _RepriceUnsafe('退回物品的游戏实例与原卖单不同，未重新出售。')
            exact_identity = bool(candidate and (candidate['assetid'] == row['assetid']
                or instance and instance == row.get('game_instance_id')))
            if candidate:
                row['reprice']['verification'] = 'return_identity'
            # A matching description/delta alone is not identity.
            # Steam's inventory history must explicitly identify this
            # NEW asset as the unique cancellation return since the
            # pre-POST boundary. Concurrent same-class events block it.
            if (candidate and candidate['amount'] - before.get(candidate['assetid'], {}).get('amount', 0)
                    == row.get('sale_amount', row['amount'])
                    and (exact_identity
                         or self._changed_return_proof(row, candidate, wallet.get('binding_generation')))):
                # A return-looking inventory row cannot authorize a
                # second listing while the original order is still
                # active or the current listing view is incomplete.
                self._reconcile_after = 0
                row['reprice']['verification'] = 'listing_removal'
                self._reconcile_listings(row['steamid'])
                view = self._observed_listings
                history_orders_match = True
                if not exact_identity:
                    baseline = row['reprice']['return_history']
                    expected = {k: v for k, v in baseline['orders'].items()
                                if k != row['reprice']['listingid']}
                    assets = getattr(self, '_listing_assets', {})
                    actual = {lid: a['assetid'] for lid, a in assets.items()
                        if str(a['description'].get('classid')) == baseline['classid']
                        and str(a['description'].get('instanceid', '0')) == baseline['instanceid']}
                    history_orders_match = (set(assets) == set(view.get('listing_ids', []))
                        and all(return_signature(a) for a in assets.values()) and actual == expected)
                if (view.get('fresh') is True and view.get('steamid') == row['steamid']
                        and view.get('generation') == wallet.get('binding_generation')
                        and view.get('checked_at', 0) >= row['reprice']['started_at']
                        and isinstance(view.get('listing_ids'), list)
                        and row['reprice']['listingid'] not in view['listing_ids']
                        and history_orders_match):
                    row['reprice']['return_proof'] = 'exact_identity' if exact_identity else 'cancellation_history'
                    returned = candidate
                    break
            if time.monotonic() >= deadline or attempt + 1 >= attempts:
                break
            time.sleep(1)
        if not returned:
            raise SteamSessionError('退回物品身份或撤单退回记录尚未核实；未重新出售，请核验 Steam 库存。')
        # Account/fees and the exact returned asset can change during
        # read-only verification. Recheck immediately before persisting
        # the sell fence, retaining the USER'S price rather than a quote.
        current_wallet = self._account(row['steamid'])
        try:
            current_receive = receive_for_total(int(total), fee_parameters(current_wallet,
                returned['description'].get('market_fee', row.get('publisher_fee', current_wallet.get('publisher_fee')))))
        except SteamSessionError:
            raise _RepriceUnsafe('当前手续费已无法按原总价重新上架，未降低价格或继续出售。') from None
        if (current_wallet.get('binding_generation') != wallet.get('binding_generation')
                or current_wallet['currency_id'] != wallet['currency_id']
                or current_receive != receive):
            raise _RepriceUnsafe('钱包币种、手续费或登录已变化，未按旧授权重新出售。')
        current_asset = self._inventory(row['steamid']).get(returned['assetid'])
        if current_asset != returned:
            raise _RepriceUnsafe('退回物品再次发生变化，未重新出售。')
        self._check_reprice_authorization(row, self._reprice_recoveries[row['id']])
        row['reprice'].update(phase='sale_submitted', returned_assetid=returned['assetid'])
        # Discovery may still hold the cancellation's inventory read.
        # Publish the exact replacement identity before the sell POST.
        row.setdefault('previous_listings', []).append({'listingid': row['listingid'],
            'assetid': row['assetid'], 'buyer_total': row.get('buyer_total'), 'currency_id': row.get('currency_id')})
        row.update(assetid=returned['assetid'], buyer_total=int(total), seller_receive=receive,
            submitted_at=self.clock(), status='uncertain', currency_id=wallet['currency_id'])
        row.pop('listingid', None)
        row['tracking_message'] = '已核实退回，正在提交新价格。'
        self._save()
        self._forget_reprice_recovery(row)
        if not self._can_submit(validate):
            raise _RepriceUnsafe('助手正在退出，未继续提交新卖单。')
        response = _json(self.session.post('/market/sellitem/', {'appid': APP_ID, 'contextid': '2',
            'assetid': returned['assetid'], 'amount': row.get('sale_amount', row['amount']), 'price': receive},
            expected_steamid=row['steamid'], expected_generation=wallet.get('binding_generation')))
        if response.get('success') not in (True, 1):
            raise SteamSessionError('新卖单结果未确认，不会重复提交。')
        row.update(assetid=returned['assetid'], buyer_total=int(total), seller_receive=receive,
            submitted_at=self.clock(), status='pending' if response.get('requires_confirmation') else 'listed',
            tracking_message='')
        row.pop('listingid', None)
        if response.get('listingid'):
            from .steam_trading import _assetid
            row['listingid'] = _assetid(response['listingid'])
        row.pop('reprice', None)
        self._confirmation = None
        self._reconcile_after = 0
        self._active_until = 0
        self._save()
