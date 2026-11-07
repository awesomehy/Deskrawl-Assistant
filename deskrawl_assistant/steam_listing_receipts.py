"""Read Steam's own listing action attributes without executing returned JS."""
from html.parser import HTMLParser
from decimal import Decimal
import re

from .steam_market import APP_ID
from .steam_session import SteamSessionError


class _Listings(HTMLParser):
    # These are the exact active/confirmation actions used by market.js.
    action = re.compile(r"\b(RemoveMarketListing|CancelMarketListingConfirmation)\(\s*"
                        r"(['\"])(mylisting(?:_confirmation)?)\2\s*,\s*"
                        r"['\"]?([1-9]\d{0,24})['\"]?\s*,\s*"
                        r"['\"]?(\d+)['\"]?\s*,\s*['\"]?(\d+)['\"]?\s*,\s*"
                        r"['\"]?([1-9]\d{0,24})['\"]?\s*\)")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = {}

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name not in ('onclick', 'href') or not value:
                continue
            for match in self.action.finditer(value):
                function, _, prefix, listingid, appid, contextid, assetid = match.groups()
                pending = function == 'CancelMarketListingConfirmation'
                if int(appid) != APP_ID or contextid != '2':
                    continue
                if prefix != ('mylisting_confirmation' if pending else 'mylisting'):
                    continue
                row = {'listingid': listingid, 'assetid': assetid, 'pending': pending}
                if listingid in self.rows and self.rows[listingid] != row:
                    raise SteamSessionError('Steam 卖单回读存在冲突，保留原记录。')
                self.rows[listingid] = row


def listing_receipts(document):
    if not isinstance(document, str) or len(document) > 4 * 1024 * 1024:
        raise SteamSessionError('Steam 卖单页面格式无效。')
    parser = _Listings()
    parser.feed(document)
    return parser.rows


def listing_prices(document, currency):
    """Read the buyer's first price in a precisely identified own-listing row.

    Never interpret the parenthesized seller receipt as a buyer total. Unknown
    currency/layout is omitted rather than guessed from arbitrary page text.
    """
    class Prices(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.values = [], {}
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            lid = re.fullmatch(r'mylisting(?:_confirmation)?_([1-9]\d{0,24})', attrs.get('id', ''))
            parent = self.stack[-1] if self.stack else (None, False)
            value = (lid.group(1) if lid else parent[0],
                     'market_listing_price' in attrs.get('class', '').split())
            if tag not in ('br', 'img', 'input', 'hr', 'meta', 'link'):
                self.stack.append(value)
        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()
        def handle_data(self, text):
            if not self.stack or not self.stack[-1][1]:
                return
            lid, _ = self.stack[-1]
            if lid and lid not in self.values and text.strip():
                self.values[lid] = text.strip()
    parser = Prices()
    parser.feed(document)
    symbols = {'CNY': ('¥', '￥'), 'USD': ('$',), 'EUR': ('€',), 'GBP': ('£',), 'HKD': ('HK$',)}
    prefixes = (currency,) + symbols.get(currency, ()) if currency else ()
    result = {}
    for lid, value in parser.values.items():
        for prefix in prefixes:
            match = re.fullmatch(re.escape(prefix) + r'\s*((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})', value)
            if match:
                amount = int(Decimal(match[1].replace(',', '')) * 100)
                if 0 < amount <= 10**12:
                    result[lid] = amount
                break
    return result
