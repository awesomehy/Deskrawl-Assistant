"""Persistent, structured player activity; no game access or mutations."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid


CATEGORIES = {'loot', 'lock', 'transfer', 'settings', 'system', 'error'}


class ActivityJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                category TEXT NOT NULL, event TEXT NOT NULL, message TEXT NOT NULL,
                item_key TEXT NOT NULL, item_name TEXT NOT NULL, item_kind TEXT NOT NULL,
                quantity INTEGER, source TEXT, destination TEXT, context TEXT NOT NULL,
                event_key TEXT UNIQUE)''')
            db.execute('CREATE INDEX IF NOT EXISTS events_category_id ON events(category,id)')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def append(self, category, event, message, **details):
        return self.append_many([dict(category=category, event=event, message=message, **details)])

    def append_many(self, entries):
        if not entries:
            return []
        now = datetime.now(timezone.utc).isoformat()
        ids = []
        with self.guard, self._connect() as db:
            for row in entries:
                if row['category'] not in CATEGORIES:
                    raise ValueError('无效的日志分类')
                quantity = row.get('quantity')
                if quantity is not None and (type(quantity) is not int or quantity < 0):
                    raise ValueError('日志数量必须为非负整数')
                encode = lambda value: json.dumps(value, ensure_ascii=False) if value is not None else None
                cursor = db.execute('''INSERT OR IGNORE INTO events
                    (time,category,event,message,item_key,item_name,item_kind,quantity,
                    source,destination,context,event_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (row.get('time') or now, row['category'], row['event'], row['message'],
                     row.get('item_key', ''), row.get('item_name', ''), row.get('item_kind', ''),
                     quantity, encode(row.get('source')), encode(row.get('destination')),
                     encode(row.get('context', {})), row.get('event_key')))
                if cursor.rowcount:
                    ids.append(cursor.lastrowid)
        return ids

    def page(self, *, category='', query='', before=None, limit=50):
        if category and category not in CATEGORIES:
            raise ValueError('日志分类无效')
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError('日志搜索文字过长')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('每页日志数量须在 1–200 之间')
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError('日志分页位置无效')
        clauses, args = [], []
        if category:
            if category == 'loot':
                clauses.append("(category='loot' OR event='carriage_collected')")
            else:
                clauses.append('category=?'); args.append(category)
        if query.strip():
            text = query.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            clauses.append("(item_name LIKE ? ESCAPE '\\' OR message LIKE ? ESCAPE '\\')")
            args.extend(['%' + text + '%'] * 2)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.guard, self._connect() as db:
            db.row_factory = sqlite3.Row
            total = db.execute('SELECT COUNT(*) FROM events' + where, args).fetchone()[0]
            latest = db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
            if before is not None:
                where += (' AND ' if clauses else ' WHERE ') + 'id<?'
                args = [*args, before]
            records = db.execute('SELECT * FROM events' + where + ' ORDER BY id DESC LIMIT ?',
                                 [*args, limit + 1]).fetchall()
        more = len(records) > limit
        out = []
        for record in records[:limit]:
            row = dict(record)
            for field in ('source', 'destination', 'context'):
                row[field] = json.loads(row[field]) if row[field] is not None else None
            row.pop('event_key')
            out.append(row)
        return {'entries': out, 'total': total, 'latest_id': latest,
                'next_before': out[-1]['id'] if more and out else None}


def item_identity(row):
    return row.get('item_uid') or row.get('instance_id') or ''


def item_summary(row, container, index):
    count = row.get('count', 1)
    name = row.get('internal_name') or row.get('name_key')
    if not name or type(count) is not int or count < 1:
        return None
    return {'item_key': name, 'identity': item_identity(row), 'count': count,
            'container': container, 'slot_index': index,
            'observation_id': row.get('observation_id') or row.get('selection_id', '')}


class ItemActivityTracker:
    """Observe coherent snapshots; project confirmed tool moves before rereads.

    Pair negative and positive slot deltas before logging additions, so sorting,
    moving existing gear and merging existing gems are not reported as loot.
    """
    def __init__(self, journal, describe):
        self.journal = journal
        self.describe = describe
        self.session = None
        self.items = {}
        self.carriage = {}
        self.guard = threading.RLock()

    def reset(self):
        with self.guard:
            self.session = None
            self.items = {}; self.carriage = {}

    def _event(self, summary, event, quantity, source=None, destination=None, **context):
        name, kind = self.describe(summary['item_key'])
        message = {'item_acquired': '新增物品', 'carriage_acquired': '马车新增物品',
                   'game_item_moved': '游戏内物品位置变化'}[event]
        return dict(category='transfer' if event == 'game_item_moved' else 'loot',
                    event=event, message=message + '：' + name, item_key=summary['item_key'],
                    item_name=name, item_kind=kind, quantity=quantity,
                    source=source, destination=destination,
                    context={'session': self.session, 'identity': summary['identity'], **context})

    @staticmethod
    def _position(item, before, after):
        return {'container': item['container'], 'slot_index': item['slot_index'],
                'count_before': before, 'count_after': after}

    def observe(self, snapshot, carriage=None):
        if not snapshot or not snapshot.get('complete') or not snapshot.get('bridge_session'):
            return
        current = {}
        for container in ('inventory', 'storage'):
            for row in snapshot.get('containers', {}).get(container, {}).get('slots', []):
                summary = item_summary(row, container, row['slot_index'])
                if summary:
                    current[(container, row['slot_index'])] = summary
        available = carriage is not None and carriage.get('available') is True
        drops = {}
        if available:
            for index, row in enumerate(carriage.get('items', [])):
                summary = item_summary(row, 'carriage', row.get('slot_index', index))
                if summary and summary['observation_id']:
                    drops[summary['observation_id']] = summary
        with self.guard:
            if self.session != snapshot['bridge_session']:
                self.session = snapshot['bridge_session']
                self.items = current
                self.carriage = drops
                self.journal.append('system', 'observation_baseline',
                    '开始记录物品变化：现有背包、仓库和马车清单作为基线。',
                    context={'session': self.session, 'inventory_groups': sum(k[0] == 'inventory' for k in current),
                             'storage_groups': sum(k[0] == 'storage' for k in current), 'carriage_groups': len(drops)})
                return
            additions, removals = defaultdict(list), defaultdict(list)
            for location in sorted(self.items.keys() | current.keys()):
                old, new = self.items.get(location), current.get(location)
                old_key = (old['item_key'], old['identity']) if old else None
                new_key = (new['item_key'], new['identity']) if new else None
                if old_key == new_key:
                    difference = new['count'] - old['count']
                    if difference > 0:
                        additions[new_key].append([new, difference, old['count']])
                    elif difference < 0:
                        removals[old_key].append([old, -difference, old['count']])
                else:
                    if old: removals[old_key].append([old, old['count'], old['count']])
                    if new: additions[new_key].append([new, new['count'], 0])
            events = []
            for key, gains in additions.items():
                losses = removals[key]
                for target, quantity, target_before in gains:
                    for loss in losses:
                        if not quantity: break
                        source, remaining, source_before = loss
                        moved = min(quantity, remaining)
                        if not moved: continue
                        events.append(self._event(target, 'game_item_moved', moved,
                            self._position(source, source_before, source_before - moved),
                            self._position(target, target_before, target_before + moved)))
                        quantity -= moved; target_before += moved
                        loss[1] -= moved; loss[2] -= moved
                    if quantity:
                        events.append(self._event(target, 'item_acquired', quantity,
                            destination=self._position(target, target_before, target_before + quantity)))
            if available:
                for token, drop in drops.items():
                    old = self.carriage.get(token)
                    before = old['count'] if old else 0
                    if drop['count'] > before:
                        events.append(self._event(drop, 'carriage_acquired', drop['count'] - before,
                            destination=self._position(drop, before, drop['count'])))
            self.journal.append_many(events)
            self.items = current
            if available: self.carriage = drops
            elif carriage is not None and carriage.get('complete', True): self.carriage = {}

    def apply_movements(self, movements):
        """Update the observation baseline using only a confirmed transaction."""
        with self.guard:
            for movement in movements:
                summary = {'item_key': movement['name'], 'identity': movement.get('identity') or '',
                           'observation_id': movement.get('observation_id', '')}
                for position in (movement['source'], movement['destination']):
                    container, index = position['container'], position.get('slot_index')
                    after = position['count_after']
                    if container == 'carriage':
                        self.carriage.pop(summary['observation_id'], None)
                    elif container in ('inventory', 'storage') and index is not None:
                        if after:
                            self.items[(container, index)] = dict(summary, container=container, slot_index=index, count=after)
                        else:
                            self.items.pop((container, index), None)


def operation_id():
    return uuid.uuid4().hex
