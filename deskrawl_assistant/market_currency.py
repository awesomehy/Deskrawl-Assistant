"""Display-only reference conversion of public Steam quotes.

Keep the native quote immutable. A separate lazy worker loads one dated ECB
batch via Frankfurter; it never reads player data or blocks inventory/game work.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .steam_market import CURRENCIES

RATE_URL = 'https://api.frankfurter.dev/v2/rates?base=EUR&providers=ECB'
MAX_RATE_BYTES = 128 * 1024
# Current ECB coverage intersected with Steam currencies; historical currencies
# are deliberately not offered merely because a provider's metadata lists them.
RATE_CURRENCIES = frozenset('AUD BRL CAD CHF CNY EUR GBP HKD IDR ILS INR JPY KRW MXN MYR NOK NZD PHP PLN SEK SGD THB TRY USD ZAR'.split())
CURRENCY_NAMES = {'CNY':'人民币', 'USD':'美元', 'HKD':'港币', 'EUR':'欧元',
                  'GBP':'英镑', 'JPY':'日元', 'KRW':'韩元', 'CAD':'加元',
                  'AUD':'澳元', 'CHF':'瑞士法郎', 'SGD':'新加坡元', 'NZD':'新西兰元',
                  'BRL':'巴西雷亚尔', 'IDR':'印尼盾', 'ILS':'以色列新谢克尔',
                  'INR':'印度卢比', 'MXN':'墨西哥比索', 'MYR':'马来西亚林吉特',
                  'NOK':'挪威克朗', 'PHP':'菲律宾比索', 'PLN':'波兰兹罗提',
                  'SEK':'瑞典克朗', 'THB':'泰铢', 'TRY':'土耳其里拉', 'ZAR':'南非兰特'}


def valid_currency_id(value):
    return type(value) is int and (value == 0 or CURRENCIES.get(value) in RATE_CURRENCIES)


def currency_options():
    priority = {'CNY':0, 'USD':1, 'HKD':2, 'EUR':3}
    return [{'id':key, 'code':code, 'name':CURRENCY_NAMES.get(code, code)}
            for key, code in sorted(CURRENCIES.items(), key=lambda pair: (priority.get(pair[1], 4), pair[1]))
            if code in RATE_CURRENCIES]


class _RateRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if (parsed.scheme != 'https' or parsed.hostname != 'api.frankfurter.dev'
                or parsed.username or parsed.password):
            raise ValueError('汇率响应地址无效')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_rates():
    request = Request(RATE_URL, headers={'Accept':'application/json',
        'User-Agent':'Deskrawl-Assistant reference currency/1.0'})
    with build_opener(_RateRedirect()).open(request, timeout=10) as response:
        parsed = urlparse(response.geturl())
        if parsed.scheme != 'https' or parsed.hostname != 'api.frankfurter.dev':
            raise ValueError('汇率响应地址无效')
        raw = response.read(MAX_RATE_BYTES + 1)
    if len(raw) > MAX_RATE_BYTES:
        raise ValueError('汇率响应过大')
    return json.loads(raw, parse_float=Decimal)


def _rates_date(value, now):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('汇率日期无效')
    actual = date.fromisoformat(value)
    if actual.isoformat() != value or actual > datetime.fromtimestamp(now, timezone.utc).date():
        raise ValueError('汇率日期无效')
    return actual


def _rate(value):
    if type(value) not in (int, float, str, Decimal):
        raise ValueError('汇率无效')
    number = Decimal(str(value))
    if not number.is_finite() or not Decimal('0.000000001') <= number <= Decimal('1000000000'):
        raise ValueError('汇率无效')
    return number


def parse_rates(value, now):
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise ValueError('汇率响应结构无效')
    dates, rates = set(), {}
    for row in value:
        if not isinstance(row, dict) or row.get('base') != 'EUR':
            raise ValueError('汇率基准无效')
        stamp = row.get('date')
        _rates_date(stamp, now)
        dates.add(stamp)
        code = row.get('quote')
        if (not isinstance(code, str) or len(code) != 3 or not code.isascii()
                or not code.isalpha() or not code.isupper() or code in rates):
            raise ValueError('汇率币种无效')
        rates[code] = format(_rate(row.get('rate')), 'f')
    if len(dates) != 1 or ('EUR' in rates and Decimal(rates['EUR']) != 1):
        raise ValueError('汇率批次不一致')
    rates['EUR'] = '1'
    return {'provider':'ECB', 'date':dates.pop(), 'rates':rates, 'fetched_at':now}


class MarketCurrency:
    def __init__(self, *, cache_path=None, transport=fetch_rates, clock=time.time,
                 ttl=3600, retry_delay=900, max_age_days=7, start_worker=True):
        self.cache_path = Path(cache_path) if cache_path else None
        self.transport, self.clock = transport, clock
        self.ttl, self.retry_delay, self.max_age_days = ttl, retry_delay, max_age_days
        self.guard = threading.RLock()
        self.closed = False
        self.worker = None
        self.refreshing = False
        self.retry_at = 0
        self.cache = None
        self.start_worker = start_worker
        self._load()

    def _load(self):
        if self.cache_path is None:
            return
        try:
            if self.cache_path.stat().st_size > MAX_RATE_BYTES:
                return
            value = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('provider') != 'ECB':
                return
            timestamp = value.get('fetched_at')
            if type(timestamp) not in (int, float) or not 0 <= timestamp <= self.clock():
                return
            rates = value.get('rates')
            if not isinstance(rates, dict):
                return
            self.cache = parse_rates([{'base':'EUR', 'quote':key, 'date':value.get('date'), 'rate':rate}
                                      for key, rate in rates.items()], timestamp)
        except (OSError, ValueError, TypeError, InvalidOperation):
            pass

    def _save(self, batch):
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_name(self.cache_path.name + '.tmp')
            temporary.write_text(json.dumps({'schema_version':1, **batch}), encoding='utf-8')
            temporary.replace(self.cache_path)
        except OSError:
            pass

    def _fetch(self):
        try:
            batch = parse_rates(self.transport(), self.clock())
        except Exception:
            with self.guard:
                self.retry_at = self.clock() + self.retry_delay
                self.refreshing = False
            return
        with self.guard:
            if self.closed:
                self.refreshing = False
                return
            self.cache = batch
            self.retry_at = 0
            self.refreshing = False
        self._save(batch)

    def for_quote(self, native, currency_id):
        if not valid_currency_id(currency_id):
            raise ValueError('不支持的市场显示币种。')
        result = deepcopy(native)
        if not currency_id or native.get('status') != 'ready' or native.get('eligible') is not True:
            return result
        target = CURRENCIES[currency_id]
        original = native.get('currency')
        if original == target:
            return result
        now = self.clock()
        with self.guard:
            expired = not self.cache or now - self.cache['fetched_at'] >= self.ttl
            if expired and not self.closed and not self.refreshing and now >= self.retry_at:
                self.refreshing = True
                if self.start_worker:
                    self.worker = threading.Thread(target=self._fetch, name='market-reference-rates', daemon=True)
                    self.worker.start()
            batch = deepcopy(self.cache)
            refreshing = self.refreshing
        result.update(currency_id=currency_id, currency=target,
            original_currency=original, original_currency_id=native.get('currency_id'),
            original_lowest_sell=native.get('lowest_sell'), original_highest_buy=native.get('highest_buy'),
            lowest_sell=None, highest_buy=None, converted=False,
            price_note='换算仅供参考；交易价格及钱包币种以 Steam 市场页面为准。')
        usable = batch and 0 <= (datetime.fromtimestamp(now, timezone.utc).date() - _rates_date(batch['date'], now)).days <= self.max_age_days
        if not usable or original not in batch['rates'] or target not in batch['rates']:
            result.update(status='queued' if refreshing else 'unavailable',
                message='等待读取参考汇率；详情可查看 Steam 原币种报价。' if refreshing
                        else '参考汇率暂不可用；详情可查看 Steam 原币种报价。')
            return result
        ratio = Decimal(batch['rates'][target]) / Decimal(batch['rates'][original])
        for key in ('lowest_sell', 'highest_buy'):
            amount = native.get(key)
            if amount is not None:
                result[key] = format((Decimal(amount) * ratio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '.2f')
        result.update(status='ready', converted=True, exchange_rate=format(ratio, '.8f'),
            exchange_rate_date=batch['date'], exchange_rate_stale=expired,
            exchange_rate_provider='ECB / Frankfurter')
        return result

    def close(self):
        with self.guard:
            self.closed = True
