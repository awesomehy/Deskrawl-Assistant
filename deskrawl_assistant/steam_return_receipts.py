"""Read Steam inventory-history return evidence without executing page scripts.

This page, unlike market/myhistory, includes cancelled listings and the NEW
inventory asset id. A before/after history boundary is mandatory: a matching
old cancellation or a same-class acquisition is not a return receipt.
"""
from hashlib import sha256
from html.parser import HTMLParser
import json
import re

from .steam_market import APP_ID
from .steam_session import SteamSessionError

CANCEL_TEXT = 'You canceled a listing on the Community Market. The item was returned to you.'


class _History(HTMLParser):
    def __init__(self, steamid):
        super().__init__(convert_charrefs=True)
        self.steamid, self.stack, self.rows = steamid, [], []
        self.row = None
        self.table = False
        self.table_depth = None
        self.table_closed = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if attrs.get('id') == 'inventory_history_table':
            if self.table:
                raise ValueError('duplicate history table')
            self.table = True
            self.table_depth = len(self.stack)
        if 'tradehistoryrow' in classes:
            if (not self.table or self.table_closed or self.table_depth is None
                    or len(self.stack) <= self.table_depth or self.row is not None or len(self.rows) >= 100):
                raise ValueError('history rows')
            self.row = {'depth': len(self.stack), 'description': '', 'items': [], 'ids': []}
        if self.row is not None and 'history_item' in classes:
            match = re.fullmatch(r'(history[a-f0-9]{40})_item\d+', attrs.get('id', ''))
            if not match:
                raise ValueError('history identity')
            self.row['ids'].append(match[1])
            item = {}
            for key in ('appid', 'classid', 'instanceid', 'contextid', 'amount'):
                value = attrs.get('data-' + key, '')
                if not re.fullmatch(r'\d{1,25}', value):
                    raise ValueError('history item')
                item[key] = value
            href = attrs.get('href')
            if href:
                target = re.fullmatch(r'https://steamcommunity\.com/profiles/' + re.escape(self.steamid)
                    + r'/inventory/#(\d+)_(\d+)_([1-9]\d{0,24})', href)
                if not target or target.groups()[:2] != (item['appid'], item['contextid']):
                    raise ValueError('history account/asset')
                item['assetid'] = target[3]
            self.row['items'].append(item)
        if tag not in ('img', 'br', 'hr', 'input', 'link', 'meta'):
            parent = self.stack[-1][1] if self.stack else False
            self.stack.append((tag, parent or 'tradehistory_event_description' in classes))

    def handle_endtag(self, tag):
        if tag in ('img', 'br', 'hr', 'input', 'link', 'meta'):
            return
        if not self.stack or self.stack[-1][0] != tag:
            # Only require well-formed structure inside the evidence rows.
            if self.row is not None:
                raise ValueError('history structure')
            return
        self.stack.pop()
        if self.table_depth is not None and len(self.stack) == self.table_depth:
            self.table_closed = True
        if self.row is not None and len(self.stack) == self.row['depth']:
            row, self.row = self.row, None
            if not row['items'] or len(set(row['ids'])) != 1:
                raise ValueError('history event')
            self.rows.append({'id': row['ids'][0], 'description': ' '.join(row['description'].split()),
                              'items': row['items']})

    def handle_data(self, data):
        if self.row is not None and self.stack and self.stack[-1][1]:
            self.row['description'] += data


def inventory_history(document, steamid):
    if isinstance(document, bytes):
        try:
            document = document.decode('utf-8', errors='strict')
        except UnicodeError:
            raise SteamSessionError('Steam 库存历史编码未核实，未猜测退回物品。') from None
    if not isinstance(document, str) or len(document) > 4 * 1024 * 1024:
        raise SteamSessionError('Steam 库存历史暂不可核验，未猜测退回物品。')
    parser = _History(steamid)
    try:
        parser.feed(document)
        if (not parser.table or not parser.table_closed or parser.row is not None or not parser.rows
                or len({r['id'] for r in parser.rows}) != len(parser.rows)):
            raise ValueError('incomplete history')
    except (ValueError, TypeError):
        raise SteamSessionError('Steam 库存历史格式未核实，未猜测退回物品。') from None
    return parser.rows


def history_anchor(row):
    return sha256(json.dumps(row, sort_keys=True, ensure_ascii=True).encode('ascii')).hexdigest()


def cancellation_return(rows, anchor, classid, instanceid, assetid, amount):
    """Exactly one new same-class event, explicitly a cancellation return.

    Caller must also prove only the target same-class listing disappeared,
    a unique exact inventory delta, unchanged full description and absence of
    the original listing. Unseen/truncated history is not negative evidence.
    """
    boundary = next((i for i, row in enumerate(rows) if history_anchor(row) == anchor), None)
    if boundary is None:
        return False
    relevant = [(row, item) for row in rows[:boundary] for item in row['items']
                if item['appid'] == str(APP_ID) and item['contextid'] == '2'
                and item['classid'] == str(classid) and item['instanceid'] == str(instanceid)]
    if len(relevant) != 1:
        return False
    row, item = relevant[0]
    return (row['description'] == CANCEL_TEXT and len(row['items']) == 1
            and item.get('assetid') == assetid and item['amount'] == str(amount))
