"""Read Steam's own listing action attributes without executing returned JS."""
from html.parser import HTMLParser
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
