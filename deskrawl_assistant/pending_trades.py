"""Read-only account-wide inventory discovery; never grants sale permission."""
from copy import deepcopy
import time
from uuid import uuid4

from .sale_tracking import project_receipt, return_signature
from .steam_market import APP_ID, CURRENCIES
from .steam_session import SteamSessionError


class PendingTradesMixin:
    def restore_upload_identities(self, intents):
        """Migrate old receipts by exact upload proof, never by item name.

        Older journals predate game_instance_id. A confirmed native intent and
        its unique Steam upload proof already identify the same physical item.
        Restoring that link changes no transaction status or sale permission.
        Called on the coordinator worker, before its network reads.
        """
        links = {}
        for intent in intents:
            proof, instance = intent.get('ticket'), intent.get('instance_id')
            if (intent.get('phase') not in ('confirmed', 'resolved') or not proof
                    or not isinstance(instance, str) or not 0 < len(instance) <= 128):
                continue
            identity = (intent.get('steamid'), proof, intent.get('item_key'), intent.get('variant'))
            uid = intent.get('uid')
            uid = uid if isinstance(uid, str) and 0 < len(uid) <= 256 else None
            links.setdefault(identity, set()).add((instance, uid))
        with self._operation:
            self._healthy_journal()
            changed = False
            for row in self._journal:
                identity = (row['steamid'], row.get('proof'), row['item_key'], row['variant'])
                identities = links.get(identity, ())
                if len(identities) == 1:
                    instance, uid = next(iter(identities))
                    if row.get('game_instance_id') not in (None, '', instance):
                        continue
                    if not row.get('game_instance_id'):
                        row['game_instance_id'] = instance
                        changed = True
                    if uid and not row.get('game_item_uid'):
                        row['game_item_uid'] = uid
                        changed = True
            if changed:
                self._save()

    def observe_game_trade_context(self, context, items, complete):
        """Publish game-side evidence; no writes or Steam requests on the reader."""
        native = context.get('native') or {}
        now = time.monotonic()
        checked = native.get('server_checked_at', 0)
        valid = (complete is True and context.get('identity_fresh', True)
                 and context.get('steamid_verified') is True and context.get('available') is True
                 and 0 <= now - context.get('read_at', 0) <= 20
                 and 0 <= now - context.get('native_read_at', 0) <= 20
                 and native.get('available') is True and not native.get('needs_refresh') and not native.get('busy')
                 and native.get('steamid_verified') is True and native.get('steamid') == context.get('steamid')
                 # Display retirement needs a completed server observation,
                 # not the short freshness lease used to authorize a write.
                 # Keep room for the independent Steam reads to complete.
                 and type(checked) in (int, float) and 0 < checked <= self.clock() < checked + 300
                 and isinstance(native.get('listings'), list))
        observed = []
        for item in items:
            if item.get('container') not in ('inventory', 'storage'):
                continue
            name = item.get('name_key') or item.get('internal_name')
            valid = valid and bool(name)
            observed.append({'item_key': name, 'instance_id': item.get('instance_id'), 'item_uid': item.get('item_uid'),
                             'variant': ('black_mist' if item['is_black_mist'] else 'normal')
                                 if type(item.get('is_black_mist')) is bool else None})
        for item in native.get('listings') or []:
            if item.get('received') is True:
                continue
            valid = valid and item.get('received') is False and bool(item.get('internal_name'))
            observed.append({'item_key': item.get('internal_name'), 'instance_id': item.get('instance_id'),
                             'variant': ('black_mist' if item['is_black_mist'] else 'normal')
                                 if type(item.get('is_black_mist')) is bool else None})
        with self.guard:
            self._game_trade_view = {'complete': bool(valid), 'steamid': context.get('steamid'),
                'checked_at': min(self.clock(), checked) if valid else 0, 'expires': self.clock() + 20,
                'items': observed}

    @staticmethod
    def _trade_present(row, items, *, conservative=False):
        instance = row.get('game_instance_id')
        for item in items:
            # A different current Steam asset belongs to its own transaction.
            # Game instance IDs can also survive copies/downloads. When both
            # local UIDs are known, they distinguish physical inventory rows.
            if item.get('assetid'):
                if item['assetid'] == row['assetid']:
                    return True
                continue
            if row.get('game_item_uid') and item.get('item_uid'):
                if item['item_uid'] == row['game_item_uid']:
                    return True
                continue
            if instance and item.get('instance_id') == instance:
                return True
            if (conservative and not instance and item.get('item_key') == row['item_key']
                    and item.get('variant') in (None, row['variant'])):
                return True
        return False

    def _retire_absent_trades(self):
        """User policy: complete absence means sold, without invented proceeds.

        Runs only on the Steam worker under _operation. Keep the journal fence
        and distinguish this conclusion from a positive Steam sale receipt.
        """
        with self.guard:
            game = deepcopy(getattr(self, '_game_trade_view', {}))
        steamid = game.get('steamid')
        evidence = self.current_trade_evidence(steamid, for_absence=True)
        if (not steamid or not game.get('complete') or self.clock() >= game.get('expires', 0)
                or not evidence.get('complete')):
            return
        items = game['items'] + evidence['items']
        changed = False
        for row in self._journal:
            if row['steamid'] != steamid:
                continue
            if row.get('assumed_sold') is True and self._trade_present(row, items):
                # A delayed exact asset can reappear; never auto-resubmit it.
                row.update(status='uncertain', tracking_message='物品重新出现，正在核验当前状态。')
                row.pop('assumed_sold', None)
                row.pop('assumed_sold_at', None)
                changed = True
                continue
            submitted = max(row.get('submitted_at', 0), row.get('created_at', 0))
            if (row['status'] not in ('captured', 'pending', 'listed', 'uncertain') or row.get('reprice')
                    or min(game['checked_at'], evidence['checked_at']) < submitted + 120
                    or self._trade_present(row, items, conservative=True)):
                continue
            row.update(status='sold', assumed_sold=True, assumed_sold_at=self.clock(), tracking_message='')
            # Absence establishes no sale time, buyer price or wallet credit.
            for field in ('sold_at', 'proceeds', 'proceeds_currency_id'):
                row.pop(field, None)
            changed = True
        if changed:
            self._save()

    def _retire_cancelled_reprice_displays(self, steamid):
        """Persist only the retirement of an absent old cancelled order."""
        evidence = self.current_trade_evidence(steamid, for_absence=True)
        if not evidence.get('complete'):
            return
        changed = False
        for row in self._journal:
            reprice = row.get('reprice') or {}
            if (row['steamid'] != steamid or reprice.get('phase') != 'cancel_submitted'
                    or evidence.get('checked_at', 0) < reprice.get('started_at', float('inf'))):
                continue
            present = self._trade_present(row, evidence['items'])
            if not present and not reprice.get('display_retired_at'):
                reprice['display_retired_at'] = self.clock()
                changed = True
            elif present and reprice.pop('display_retired_at', None) is not None:
                changed = True
        if changed:
            self._save()

    def _catalog_asset(self, asset):
        if not self.catalog:
            return None
        description = asset.get('description', {})
        name = description.get('market_hash_name')
        for key, entry in self.catalog.items.items():
            for variant in ('normal', 'black_mist'):
                if entry.get(variant, {}).get('market_hash_name') == name:
                    item = self._item(key, variant)
                    if self._matches(asset, item):
                        return item
        return None

    def _observe_market_listings(self, response, listings, steamid):
        """Adopt only exact active listing actions joined to Steam asset metadata.

        These records are permanently excluded from automatic sale selection.
        Inventory discovery remains separate from the durable transaction log.
        """
        assets = response.get('assets')
        assets = assets.get(str(APP_ID), {}) if isinstance(assets, dict) else {}
        assets = assets.get('2', {}) if isinstance(assets, dict) else {}
        if not isinstance(assets, dict):
            return
        from .steam_listing_receipts import listing_prices
        prices = listing_prices(response.get('results_html', ''), self.session.state().get('wallet_currency'))
        changed = False
        for listing in listings.values():
            if any(r['steamid'] == steamid and (r.get('listingid') == listing['listingid'] or
                    r['assetid'] == listing['assetid']) for r in self._journal):
                continue
            description = assets.get(listing['assetid'])
            if not isinstance(description, dict) or str(description.get('appid')) != str(APP_ID):
                continue
            asset = {'assetid': listing['assetid'], 'description': description}
            item = self._catalog_asset(asset)
            amount = str(description.get('amount', '1'))
            if not item or not amount.isdigit() or not 0 < int(amount) <= 10**9:
                continue
            # Existing journals remain bounded. Never evict an unresolved sale.
            from .steam_trading import JOURNAL_LIMIT
            if len(self._journal) >= JOURNAL_LIMIT:
                break
            row = {'id': uuid4().hex, 'steamid': steamid, 'assetid': listing['assetid'],
                   'listingid': listing['listingid'], 'item_key': item.key, 'variant': item.variant,
                   'amount': int(amount), 'sale_amount': int(amount), 'created_at': self.clock(),
                   'submitted_at': 0, 'source': 'observed_market', 'manual_only': True,
                   'status': 'pending' if listing['pending'] else 'listed'}
            instance = self._native_instance(asset)
            if instance:
                row['game_instance_id'] = instance
            currency = next((k for k, v in CURRENCIES.items()
                             if v == self.session.state().get('wallet_currency')), None)
            if currency:
                row['currency_id'] = currency
                if listing['listingid'] in prices:
                    row['buyer_total'] = prices[listing['listingid']]
            # Some Steam responses supply structured, currency-qualified prices.
            info = response.get('listinginfo')
            info = info.get(listing['listingid'], {}) if isinstance(info, dict) else {}
            if (currency and isinstance(info, dict) and info.get('converted_currencyid') in (currency, 2000 + currency)
                    and type(info.get('converted_price')) is int and info['converted_price'] > 0
                    and type(info.get('converted_fee')) is int and info['converted_fee'] >= 0):
                row['buyer_total'] = info['converted_price'] + info['converted_fee']
                row['seller_receive'] = info['converted_price']
            self._journal.append(row)
            changed = True
        if changed:
            self._save()

    def _observe_steam_inventory(self, steamid):
        if self.clock() < getattr(self, '_inventory_observe_after', 0):
            return
        self._inventory_observe_after = self.clock() + 30
        # An incomplete/failed read cannot be treated as an empty inventory.
        previous = getattr(self, '_observed_inventory', {})
        self._observed_inventory = {**previous, 'fresh': False}
        before = self.session.state()
        inventory = self._inventory(steamid)
        auth = self.session.state()
        if (auth.get('verified') is not True or auth.get('expired')
                or auth.get('steamid') != steamid
                or auth.get('binding_generation') != before.get('binding_generation')):
            raise SteamSessionError('Steam 登录已变化，未采用旧账号的库存响应。')
        rows, sale_assets = [], {}
        known = True
        for asset in inventory.values():
            item = self._catalog_asset(asset)
            if not item:
                known = False
                continue
            rows.append({'item_key': item.key, 'variant': item.variant, 'assetid': asset['assetid'],
                         'instance_id': self._native_instance(asset), 'count': asset['amount']})
            signature = return_signature(asset)
            if signature and asset['amount'] == 1:
                sale_assets[asset['assetid']] = {'item_key': item.key, 'variant': item.variant,
                    'signature': signature, 'game_instance_id': self._native_instance(asset),
                    'publisher_fee': asset['description'].get('market_fee')}
        self._observed_inventory = {'steamid': steamid, 'generation': auth.get('binding_generation'),
                                    'items': rows, 'sale_assets': sale_assets,
                                    'fresh': True, 'scope_complete': known, 'checked_at': self.clock(),
                                    'expires': self.clock() + 45, 'absence_expires': self.clock() + 120}

    def pending_trade_receipts(self, steamid):
        """Immutable worker snapshots only. No I/O, even while a request stalls."""
        rows = self._receipt_rows
        result = []
        seen = set()
        observed = getattr(self, '_observed_inventory', {})
        auth = self.auth_state() or {}
        same_scope = (observed.get('steamid') == steamid and auth.get('steamid') == steamid
                      and auth.get('verified') is True and not auth.get('expired')
                      and observed.get('generation') == auth.get('binding_generation'))
        evidence = self.current_trade_evidence(steamid, for_absence=True)
        for row in rows:
            if row['steamid'] != steamid:
                continue
            seen.add(row['assetid'])
            if row['status'] == 'sold':
                continue
            receipt = self._receipt_state(row, steamid)
            reprice = row.get('reprice') or {}
            # A disappeared cancelled order is historical, not a second
            # physical item alongside the freshly returned Steam inventory.
            # This only retires its display. Keep both durable write fences;
            # absence does NOT grant permission to match/re-submit an asset.
            retired_cancel = (reprice.get('phase') == 'cancel_submitted' and (
                bool(reprice.get('display_retired_at')) or evidence.get('complete')
                and evidence.get('checked_at', 0) >= reprice.get('started_at', float('inf'))
                and not self._trade_present(row, evidence['items'])))
            result.append({'id': row['id'], 'item_key': row['item_key'], 'variant': row['variant'],
                           'count': row.get('sale_amount', row['amount']), 'instance_id': row.get('game_instance_id'),
                           'proof': row.get('proof'), **project_receipt(receipt),
                           'display_retired': bool(retired_cancel),
                           'sale_preview_available': bool(same_scope and row['status'] == 'captured'
                               and not reprice and row['assetid'] in observed.get('sale_assets', {})
                               and not self._reprice_reserved(steamid, row['item_key'], row['variant'])),
                           'sale_available': row['status'] == 'captured' and not reprice
                           and receipt.get('status') == 'captured' and row['amount'] == 1})
        if observed.get('steamid') == steamid:
            fresh = (observed.get('fresh') is True and self.clock() < observed.get('expires', 0)
                     and auth.get('steamid') == steamid and auth.get('verified') is True and not auth.get('expired')
                     and observed.get('generation') == auth.get('binding_generation'))
            for item in observed.get('items', []):
                if item['assetid'] in seen:
                    continue
                reserved = self._reprice_reserved(steamid, item['item_key'], item['variant'])
                result.append({'id': 'inventory-' + item['assetid'], **deepcopy(item),
                    'status': 'uploaded' if fresh else 'uncertain', 'previous_status': 'uploaded',
                    'needs_verification': not fresh, 'can_reprice': False,
                    # Stale observations may open a read-only editor, never
                    # authorise a sale. Submission still re-reads exact assets.
                    'sale_preview_available': bool(same_scope and not reserved
                        and item['assetid'] in observed.get('sale_assets', {})),
                    'sale_available': fresh and item['count'] == 1 and not reserved,
                    'message': '原改价正在核验退回，确认后按原设定价格重新上架。' if reserved else
                               '已在 Steam 库存，暂未上架 Steam 市场。'})
        return result

    def sold_trade_instances(self, steamid):
        """Exact retired identities prevent stale native lists resurrecting sales."""
        return frozenset(row['game_instance_id'] for row in self._receipt_rows
                         if row['steamid'] == steamid and row['status'] == 'sold'
                         and row.get('game_instance_id'))

    def current_trade_evidence(self, steamid, *, for_absence=False):
        """Complete current Steam views, independent of old upload intents.

        Absence is only a display-scope fact, never a sale receipt or permission
        to repeat a transaction. Failed/partial/account-switched reads are unknown.
        """
        auth = self.auth_state() or {}
        inventory = getattr(self, '_observed_inventory', {})
        listings = getattr(self, '_observed_listings', {})
        fresh = (auth.get('steamid') == steamid and auth.get('verified') is True and not auth.get('expired')
                 and all(view.get('steamid') == steamid and view.get('fresh') is True
                         and view.get('scope_complete', True) is True
                         and view.get('generation') == auth.get('binding_generation')
                         and self.clock() < view.get('absence_expires' if for_absence else 'expires',
                                                     view.get('expires', 0)) for view in (inventory, listings)))
        return {'complete': bool(fresh), 'checked_at': min(inventory.get('checked_at', 0), listings.get('checked_at', 0)),
                'items': deepcopy(inventory.get('items', []) + listings.get('items', []))
                if fresh else []}
