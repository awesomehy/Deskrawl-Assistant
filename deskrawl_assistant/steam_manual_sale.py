"""Explicit single-asset inventory sale using the shared Steam journal/fences."""
from decimal import Decimal, InvalidOperation
import re
import secrets
import uuid

from .sale_tracking import return_signature
from .steam_market import APP_ID, CURRENCIES
from .steam_session import SteamSessionError


class ManualSteamSaleMixin:
    def _manual_sale_assetid(self, selection, steamid):
        # Selection refers only to an existing server-side projection. Never
        # accept a browser-supplied account, catalog key or arbitrary asset id.
        if re.fullmatch(r'inventory-[1-9]\d{0,24}', selection):
            return selection.split('-', 1)[1]
        row = next((r for r in self._receipt_rows if r['id'] == selection and r['steamid'] == steamid
                    and r['status'] == 'captured' and not r.get('reprice')), None)
        if row:
            return row['assetid']
        raise SteamSessionError('物品身份尚未核实，未准备出售。')

    def _manual_sale_unsubmitted(self, steamid, assetid):
        if any(r['steamid'] == steamid and r['assetid'] == assetid
               and (r['status'] != 'captured' or r.get('reprice')) for r in self._journal):
            raise SteamSessionError('该资产已有出售或改价记录，须先核验结果，不重复提交。')

    def manual_sale_preview(self, selection, steamid):
        # Opening an editor must never wait behind background history/quotes.
        # These immutable, account-scoped observations grant no sale authority:
        # manual_sale rechecks live orders, wallet and the full asset signature.
        self._healthy_journal()
        auth = self.auth_state() or {}
        currency = next((k for k, v in CURRENCIES.items() if v == auth.get('wallet_currency')), None)
        view = getattr(self, '_observed_inventory', {})
        if (self._closed or auth.get('verified') is not True or auth.get('expired')
                or auth.get('steamid') != steamid or currency is None
                or view.get('steamid') != steamid or view.get('generation') != auth.get('binding_generation')):
            raise SteamSessionError('Steam 登录、钱包或库存尚未确认，请刷新后重试。')
        assetid = self._manual_sale_assetid(selection, steamid)
        asset = view.get('sale_assets', {}).get(assetid)
        if asset and self._reprice_reserved(steamid, asset['item_key'], asset['variant']):
            raise SteamSessionError('同类物品的改价仍在核验退回，请等待原改价完成，不重复出售。')
        if not asset or any(r['steamid'] == steamid and r['assetid'] == assetid
                           and (r['status'] != 'captured' or r.get('reprice')) for r in self._receipt_rows):
            raise SteamSessionError('所选单件商品已变化或已有出售记录，请刷新待交易物品。')
        quote = self._price_preview_quote(steamid, asset, currency, auth)
        token = secrets.token_urlsafe(32)
        with self.guard:
            previews = getattr(self, '_manual_sale_previews', {})
            previews = {k: v for k, v in previews.items() if self.clock() < v['expires']
                        and (v['steamid'], v['assetid']) != (steamid, assetid)}
            if len(previews) >= 50:
                raise SteamSessionError('待确认操作过多，请关闭多余窗口后重试。')
            previews[token] = {'steamid': steamid, 'assetid': assetid, 'item_key': asset['item_key'],
                'variant': asset['variant'], 'generation': auth.get('binding_generation'),
                'currency_id': currency, 'signature': asset['signature'],
                'game_instance_id': asset['game_instance_id'], 'expires': self.clock() + 120}
            self._manual_sale_previews = previews
        return {'token': token, 'currency': CURRENCIES[currency], **quote,
            'message': '确认后实时核验所选单件商品、账号与钱包；可能需要 Steam 手机确认。'}

    def manual_sale(self, token, price, *, expected_steamid, validate=None):
        from .steam_trading import _json, _assetid, fee_parameters, receive_for_total, JOURNAL_LIMIT
        with self._operation:
            with self.guard:
                preview = getattr(self, '_manual_sale_previews', {}).pop(token, None)
            if (not preview or self.clock() >= preview['expires'] or not self._can_submit(validate)
                    or preview['steamid'] != expected_steamid):
                raise SteamSessionError('上架确认已失效或当前玩家已变化，请重新读取。')
            self._healthy_journal()
            if self.clock() < self._retry_after:
                raise SteamSessionError('Steam 暂时限制请求频率，尚未上架，请稍后重试。')
            wallet = self._account(expected_steamid)
            if (wallet.get('binding_generation') != preview['generation']
                    or wallet['currency_id'] != preview['currency_id']):
                raise SteamSessionError('Steam 登录或钱包币种已变化，未上架。')
            if self._reprice_reserved(expected_steamid, preview['item_key'], preview['variant']):
                raise SteamSessionError('同类物品的改价仍在核验退回，未提交其他出售。')
            try:
                total = Decimal(price) * 100
            except (InvalidOperation, ValueError, TypeError):
                raise SteamSessionError('请输入有效的买方支付总价。') from None
            if not total.is_finite() or total <= 0 or total != total.to_integral_value():
                raise SteamSessionError('价格必须为正数，最多两位小数。')
            self._manual_sale_unsubmitted(expected_steamid, preview['assetid'])
            # Full, fresh current orders are required; a stale or incomplete
            # page is not permission to create a second sale.
            self._reconcile_after = 0
            self._reconcile_listings(expected_steamid)
            view = self._observed_listings
            if (view.get('fresh') is not True or view.get('steamid') != expected_steamid
                    or view.get('generation') != preview['generation']
                    or any(r['assetid'] == preview['assetid'] for r in view.get('items', []))):
                raise SteamSessionError('Steam 当前卖单未完整核实或物品已有卖单，未重复上架。')
            asset = self._inventory(expected_steamid).get(preview['assetid'])
            if (not asset or asset['amount'] != 1 or return_signature(asset) != preview['signature']
                    or self._native_instance(asset) != preview['game_instance_id']
                    or not self._matches(asset, self._item(preview['item_key'], preview['variant']))):
                raise SteamSessionError('所选物品已变化，未按名称或同类物品替代出售。')
            fee = asset['description'].get('market_fee', wallet.get('publisher_fee'))
            receive = receive_for_total(int(total), fee_parameters(wallet, fee))
            if not self._can_submit(validate):
                raise SteamSessionError('当前玩家或操作状态已变化，未上架。')
            row = next((r for r in self._journal if r['steamid'] == expected_steamid
                        and r['assetid'] == preview['assetid']), None)
            if row is None:
                if len(self._journal) >= JOURNAL_LIMIT:
                    raise SteamSessionError('出售核验记录已满，未提交新卖单。')
                row = {'id': uuid.uuid4().hex, 'steamid': expected_steamid, 'assetid': preview['assetid'],
                    'item_key': preview['item_key'], 'variant': preview['variant'], 'amount': 1,
                    'game_instance_id': preview['game_instance_id'], 'source': 'explicit_inventory_sale',
                    'manual_only': True, 'created_at': self.clock()}
                self._journal.append(row)
            row.update(status='intent', buyer_total=int(total), seller_receive=receive, sale_amount=1,
                currency_id=wallet['currency_id'], publisher_fee=str(fee), submitted_at=self.clock())
            # Durable at-most-once fence BEFORE the only economic request.
            self._save()
            try:
                if not self._can_submit(validate):
                    raise SteamSessionError('助手正在退出或玩家已切换，未继续出售。')
                result = _json(self.session.post('/market/sellitem/', {'appid': APP_ID, 'contextid': '2',
                    'assetid': preview['assetid'], 'amount': 1, 'price': receive},
                    expected_steamid=expected_steamid, expected_generation=preview['generation']))
                if result.get('success') not in (True, 1):
                    raise SteamSessionError('卖单结果未确认，不会重复提交，请核验 Steam 库存和市场。')
                row.update(status='pending' if result.get('requires_confirmation') else 'listed', tracking_message='')
                if result.get('listingid'):
                    row['listingid'] = _assetid(result['listingid'])
            except Exception as exc:
                if isinstance(exc, SteamSessionError):
                    self._limited(exc)
                row.update(status='uncertain', tracking_message='手动上架结果待核验；不会重复提交，请检查 Steam 市场。')
                raise
            finally:
                self._reconcile_after = self._inventory_observe_after = self._active_until = 0
                self._confirmation = None
                self._save()
