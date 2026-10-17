"""Anonymous, read-only Steam Community Market order-book quotes.

Only public ItemData names leave the machine. No login, Steam ID, asset ID,
item UID, account inventory, trade, or market-order operation is supported.
``for_item`` is nonblocking: a single bounded worker handles HTTP separately
from game reads and managed-mode actions. See docs/steam-market.md.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
import time
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

APP_ID = 4623570
STEAM_ORIGIN = 'https://steamcommunity.com'
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
# Steam market order amounts are represented in hundredths of a currency
# unit. Preserve the received currency ID; country/currency URL preferences
# are not guaranteed to be honored by Steam's new SSR market.
CURRENCIES = {1: 'USD', 2: 'GBP', 3: 'EUR', 4: 'CHF', 5: 'RUB', 6: 'PLN',
              7: 'BRL', 8: 'JPY', 9: 'NOK', 10: 'IDR', 11: 'MYR', 12: 'PHP',
              13: 'SGD', 14: 'THB', 15: 'VND', 16: 'KRW', 17: 'TRY', 18: 'UAH',
              19: 'MXN', 20: 'CAD', 21: 'AUD', 22: 'NZD', 23: 'CNY', 24: 'INR',
              25: 'CLP', 26: 'PEN', 27: 'COP', 28: 'ZAR', 29: 'HKD', 30: 'TWD',
              31: 'SAR', 32: 'AED', 33: 'SEK', 34: 'ARS', 35: 'ILS', 37: 'KZT',
              38: 'KWD', 39: 'QAR', 40: 'CRC', 41: 'UYU'}


class MarketDataError(ValueError):
    """A public response cannot safely be presented as this item's quote."""


class MarketUnavailable(OSError):
    def __init__(self, message='Steam 市场暂不可用', *, status=None, retry_after=0):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


@dataclass(frozen=True)
class MarketItem:
    key: str
    market_hash_name: str
    definition_id: int
    kind: str
    variant: str

    @property
    def market_url(self):
        return f'{STEAM_ORIGIN}/market/listings/{APP_ID}/{quote(self.market_hash_name, safe="")}'


class MarketCatalog:
    def __init__(self, data: Mapping):
        if (not isinstance(data, Mapping) or data.get('schema_version') != 1
                or data.get('appid') != APP_ID or not isinstance(data.get('items'), dict)):
            raise MarketDataError('市场物品词典版本不受支持')
        self.items = deepcopy(data['items'])
        for key, entry in self.items.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                raise MarketDataError('市场物品词典结构不受支持')
            for variant in ('normal', 'black_mist'):
                value = entry.get(variant)
                if value is None:
                    continue
                if (not isinstance(value, dict) or type(value.get('definition_id')) is not int
                        or value['definition_id'] <= 0 or not _valid_name(value.get('market_hash_name'))):
                    raise MarketDataError('市场物品定义无效')

    @classmethod
    def load(cls, path: str | Path):
        return cls(json.loads(Path(path).read_text(encoding='utf-8-sig')))

    def resolve(self, row: Mapping) -> tuple[MarketItem | None, str]:
        key = row.get('name_key') or row.get('internal_name')
        entry = self.items.get(key)
        if not entry:
            return None, 'unsupported'
        if row.get('issues'):
            return None, 'unverified'
        kind = entry.get('kind')
        rarity = row.get('rarity')
        if rarity is None:
            rarity = row.get('base_rarity')
        # Equipment and runes have instance binding flags. An absent flag is
        # unknown, never proof that the item is marketable.
        if kind in ('equipment', 'rune'):
            if row.get('bound') is True or row.get('is_equipped') is True or row.get('container') == 'equipped':
                return None, 'bound'
            if row.get('bound') is not False:
                return None, 'unverified'
        variant = 'black_mist' if row.get('is_black_mist') is True else 'normal'
        if kind == 'equipment':
            if variant == 'black_mist':
                if rarity != 3:
                    return None, 'unverified'
            elif rarity != entry.get('rarity') or rarity != 4 or row.get('is_black_mist') is not False:
                return None, 'unsupported'
        elif rarity != entry.get('rarity'):
            return None, 'unverified'
        value = entry.get(variant)
        if not value:
            return None, 'unsupported'
        return MarketItem(key, value['market_hash_name'], value['definition_id'], kind, variant), 'candidate'


def _valid_name(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


class _SSRParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.capture = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('id') == 'valve-ssr-data' and attrs.get('type') == 'application/json':
            if self.parts or self.capture:
                raise MarketDataError('Steam 返回重复页面数据')
            self.capture = True

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.capture = False


def _integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 10**14:
        raise MarketDataError('Steam 订单金额或数量格式已变化')
    return value


def _amount(value, count):
    count = _integer(count)
    amount = _integer(value if value is not None else 0)
    if not count:
        if amount:
            raise MarketDataError('Steam 订单金额与数量不一致')
        return None
    if not amount:
        raise MarketDataError('Steam 订单价格缺失')
    return format(Decimal(amount) / 100, '.2f')


def parse_listing(document: str, item: MarketItem) -> dict:
    """Parse only a verified exact item's live orderbook, never medians."""
    if not isinstance(document, str) or len(document.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise MarketDataError('Steam 市场页面过大')
    parser = _SSRParser()
    parser.feed(document)
    if not parser.parts:
        # Never infer prices from visible loose text or historical medians.
        raise MarketDataError('Steam 市场页面结构已变化')
    try:
        ssr = json.loads(''.join(parser.parts))
        context = ssr['renderContext']
        queries = json.loads(context['queryData'])['queries']
        if not isinstance(queries, list):
            raise TypeError('queries must be a list')
    except (KeyError, ValueError, TypeError) as exc:
        raise MarketDataError('Steam 市场页面数据无效') from exc
    descriptions, orderbooks = [], []
    for query in queries:
        if not isinstance(query, dict):
            continue
        key = query.get('queryKey')
        state = query.get('state', {})
        if not isinstance(state, dict) or state.get('status') != 'success':
            continue
        if key == ['market', 'description', APP_ID, item.market_hash_name]:
            descriptions.append(state.get('data'))
        if key == ['market', 'orderbook', APP_ID, item.market_hash_name]:
            orderbooks.append(state.get('data'))
    if len(descriptions) != 1 or len(orderbooks) != 1:
        raise MarketDataError('尚未匹配到此物品的 Steam 订单簿')
    description, book = descriptions[0], orderbooks[0]
    if (not isinstance(description, dict) or description.get('appid') != APP_ID
            or description.get('market_hash_name') != item.market_hash_name
            or description.get('marketable') not in (True, 1)):
        raise MarketDataError('Steam 物品名称或可售状态未匹配')
    # Match rarity/category too: identical English display names in different
    # categories must not silently share a quotation.
    expected = '[Black Mist]' if item.variant == 'black_mist' else (
        '[Divine]' if item.kind == 'equipment' else None)
    if expected and not str(description.get('type', '')).startswith(expected):
        raise MarketDataError('Steam 物品品质未匹配')
    if not isinstance(book, dict):
        raise MarketDataError('Steam 订单簿无效')
    currency = _integer(book.get('eCurrency'), minimum=1)
    if currency not in CURRENCIES:
        raise MarketDataError('Steam 返回未知币种，暂不换算价格')
    return {'status': 'ready', 'currency_id': currency, 'currency': CURRENCIES[currency],
            'lowest_sell': _amount(book.get('amtMinSellOrder'), book.get('cSellOrders')),
            'highest_buy': _amount(book.get('amtMaxBuyOrder'), book.get('cBuyOrders')),
            'sell_order_count': _integer(book.get('cSellOrders')),
            'buy_order_count': _integer(book.get('cBuyOrders')),
            'source': 'steam_orderbook',
            'price_note': 'Steam 市场订单总价（含市场手续费）；按物品种类报价，非单件属性估价。'}


class _SteamRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != 'https' or parsed.hostname != 'steamcommunity.com' or parsed.username or parsed.password:
            raise MarketUnavailable('Steam 返回异常跳转')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_listing(item: MarketItem, *, timeout=10.0) -> str:
    """Public anonymous GET. A private Steam session is never loaded."""
    url = item.market_url + '?' + urlencode({'l': 'english'})
    request = Request(url, headers={'User-Agent': 'Deskrawl-Assistant public market quotes/1.0',
                                    'Accept': 'text/html', 'Accept-Language': 'en-US,en;q=0.8'})
    try:
        with build_opener(_SteamRedirect()).open(request, timeout=timeout) as response:
            if urlparse(response.geturl()).hostname != 'steamcommunity.com':
                raise MarketUnavailable('Steam 返回异常地址')
            raw = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise MarketDataError('Steam 市场页面过大')
        return raw.decode('utf-8')
    except HTTPError as exc:
        retry = exc.headers.get('Retry-After', '')
        retry_after = min(3600, int(retry)) if retry.isdigit() else 0
        raise MarketUnavailable('Steam 市场限制请求，请稍后再试' if exc.code in (403, 429)
                                else 'Steam 市场暂不可用', status=exc.code, retry_after=retry_after) from exc
    except (URLError, TimeoutError, UnicodeError) as exc:
        raise MarketUnavailable('无法读取 Steam 市场，托管不受影响') from exc


class SteamMarketPrices:
    """A bounded single HTTP worker with positive/error/negative caching.

    Share one instance among account panels in a process. Call ``close`` on
    application shutdown. Failed requests and stale quotes never gate actions.
    """
    def __init__(self, catalog: MarketCatalog, *, cache_path: str | Path | None = None,
                 transport: Callable[[MarketItem], str] = fetch_listing,
                 clock: Callable[[], float] = time.time, request_gap=15.0,
                 cache_ttl=300.0, negative_ttl=1800.0, error_ttl=60.0,
                 max_cache=512, start_worker=True):
        self.catalog, self.transport, self.clock = catalog, transport, clock
        self.cache_path = Path(cache_path) if cache_path else None
        self.request_gap = max(0.0, float(request_gap))
        self.cache_ttl, self.negative_ttl, self.error_ttl = cache_ttl, negative_ttl, error_ttl
        self.max_cache = max(1, min(int(max_cache), 4096))
        self.cache = OrderedDict()
        self.pending = deque()
        self.pending_names = set()
        self.condition = threading.Condition()
        self.closed = False
        self.last_request = -float('inf')
        self.blocked_until = 0.0
        self.worker = None
        self.start_worker = start_worker
        self._load()

    def _load(self):
        if not self.cache_path:
            return
        try:
            if self.cache_path.stat().st_size > MAX_DOCUMENT_BYTES:
                return
            value = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('appid') != APP_ID:
                return
            if not isinstance(value.get('quotes'), dict):
                return
            allowed = {v['market_hash_name'] for entry in self.catalog.items.values()
                       for key in ('normal', 'black_mist') if (v := entry.get(key))}
            for key, entry in value.get('quotes', {}).items():
                if key not in allowed or not isinstance(entry, dict):
                    continue
                # Local disk entries are display-only. Revalidate their types
                # and exact currency before a previous quote is rendered.
                if entry.get('status') != 'ready' or entry.get('currency_id') not in CURRENCIES:
                    continue
                if entry.get('currency') != CURRENCIES[entry['currency_id']]:
                    continue
                if any(v is not None and (not isinstance(v, str) or not re.fullmatch(r'\d+\.\d{2}', v))
                       for v in (entry.get('lowest_sell'), entry.get('highest_buy'))):
                    continue
                if type(entry.get('fetched_at')) not in (float, int) or not 0 <= entry['fetched_at'] <= self.clock():
                    continue
                self.cache[key] = entry
                if len(self.cache) >= self.max_cache:
                    break
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        if not self.cache_path:
            return
        try:
            with self.condition:
                quotes = {k: deepcopy(v) for k, v in self.cache.items() if v.get('status') == 'ready'}
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_name(self.cache_path.name + '.tmp')
            temporary.write_text(json.dumps({'schema_version': 1, 'appid': APP_ID, 'quotes': quotes},
                                            ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.cache_path)
        except OSError:
            pass

    def for_item(self, row: Mapping) -> dict:
        item, status = self.catalog.resolve(row)
        if not item:
            return {'eligible': False, 'status': status}
        now = self.clock()
        name = item.market_hash_name
        with self.condition:
            cached = self.cache.get(name)
            ttl = self.cache_ttl if cached and cached['status'] == 'ready' else (
                self.negative_ttl if cached and cached['status'] == 'unmatched' else self.error_ttl)
            expired = not cached or now - cached['fetched_at'] >= ttl
            retry_after = cached.get('retry_after', 0) if cached else 0
            if type(retry_after) not in (int, float):
                retry_after = 0
            if (expired and now >= retry_after and not self.closed
                    and name not in self.pending_names and len(self.pending) < self.max_cache):
                self.pending.append(item)
                self.pending_names.add(name)
                if self.start_worker and self.worker is None:
                    self.worker = threading.Thread(target=self._run, name='steam-market-quotes', daemon=True)
                    self.worker.start()
                self.condition.notify_all()
            result = deepcopy(cached) if cached else {'status': 'queued', 'message': '等待读取 Steam 市场'}
            if cached:
                self.cache.move_to_end(name)
            if cached and expired and cached['status'] == 'ready':
                result['stale'] = True
            else:
                result['stale'] = False
            result.update(eligible=True, market_hash_name=name, market_url=item.market_url,
                          refreshing=name in self.pending_names)
            if now < self.blocked_until:
                result['retry_at'] = self.blocked_until
            return result

    def _run(self):
        while True:
            with self.condition:
                if self.closed:
                    return
                if not self.pending:
                    self.condition.wait()
                    continue
                delay = max(self.blocked_until, self.last_request + self.request_gap) - self.clock()
                if delay > 0:
                    self.condition.wait(min(delay, 60.0))
                    continue
                item = self.pending.popleft()
            self._fetch(item)

    def _fetch(self, item: MarketItem):
        """Separated from the worker loop for deterministic transport tests."""
        now = self.clock()
        with self.condition:
            self.last_request = now
        try:
            result = parse_listing(self.transport(item), item)
        except MarketDataError as exc:
            result = {'status': 'unmatched', 'message': str(exc)}
        except (OSError, ValueError) as exc:
            result = {'status': 'unavailable', 'message': str(exc) if isinstance(exc, MarketUnavailable)
                      else 'Steam 市场暂不可用，托管不受影响'}
            if isinstance(exc, MarketUnavailable) and exc.status in (403, 429):
                with self.condition:
                    self.blocked_until = max(self.blocked_until, now + max(300, exc.retry_after))
        except Exception:
            # Third-party/public format failures must not kill the daemon and
            # leave all subsequent item queries permanently stuck as queued.
            result = {'status': 'unavailable', 'message': 'Steam 市场读取失败，托管不受影响'}
        result['fetched_at'] = self.clock()
        result['fetched_at_utc'] = datetime.fromtimestamp(result['fetched_at'], timezone.utc).isoformat()
        with self.condition:
            previous = self.cache.get(item.market_hash_name)
            if result['status'] != 'ready' and previous and previous.get('status') == 'ready':
                # Keep a visibly stale last good quote through network outages;
                # retry age is separate from the original quote timestamp.
                old = deepcopy(previous)
                old['refresh_error'] = result['message']
                old['retry_after'] = result['fetched_at'] + self.error_ttl
                result = old
            self.cache[item.market_hash_name] = result
            self.cache.move_to_end(item.market_hash_name)
            while len(self.cache) > self.max_cache:
                self.cache.popitem(last=False)
            self.pending_names.discard(item.market_hash_name)
            self.condition.notify_all()
        self._save()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending.clear()
            self.pending_names.clear()
            self.condition.notify_all()
