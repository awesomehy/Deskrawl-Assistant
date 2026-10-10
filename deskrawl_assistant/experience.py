"""Persistent measured experience efficiency; no game writes or synthetic rewards."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import math
from pathlib import Path
import sqlite3
import threading
import time


def text_field(value, label, *, required=False):
    if not isinstance(value, str):
        raise ValueError(label + '必须为文字。')
    value = value.strip()
    if (required and not value) or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError(label + '须为 1 到 120 个字符。' if required else label + '最多 120 个字符。')
    return value


def number(value, label, maximum, *, positive=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label + '必须为有效数字。')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(label + '必须为有限数字。')
    if value < 0 or (positive and value <= 0) or value > maximum or (integer and int(value) != value):
        raise ValueError(label + '超出允许范围。')
    return int(value) if integer else float(value)


class ExperienceBook:
    def __init__(self, path, *, clock=time.monotonic, now=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())
        self.guard = threading.RLock()
        self.active = None
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS experience_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT NOT NULL, profile TEXT NOT NULL,
                xp INTEGER NOT NULL, seconds REAL NOT NULL,
                source TEXT NOT NULL, recorded_at TEXT NOT NULL)""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, data, *, source='manual'):
        stage = text_field(data.get('stage'), '关卡名称', required=True)
        profile = text_field(data.get('profile', ''), '角色 / 难度 / 配装')
        xp = number(data.get('xp'), '经验', 10**15, integer=True)
        seconds = number(data.get('seconds'), '完整循环耗时（秒）', 30*86400, positive=True)
        if seconds < 0.1:
            raise ValueError('耗时至少为 0.1 秒。')
        with self.guard, self._db() as db:
            cursor = db.execute('INSERT INTO experience_samples (stage,profile,xp,seconds,source,recorded_at) VALUES (?,?,?,?,?,?)',
                                (stage, profile, xp, seconds, source, self.now()))
            return {'id': cursor.lastrowid, 'stage': stage, 'profile': profile, 'xp': xp, 'seconds': seconds, 'source': source}

    def delete(self, sample_id):
        sample_id = number(sample_id, '记录编号', 2**63-1, positive=True, integer=True)
        with self.guard, self._db() as db:
            if db.execute('DELETE FROM experience_samples WHERE id=?', (sample_id,)).rowcount != 1:
                raise ValueError('采样记录不存在。')

    def start(self, data, live=None):
        stage = text_field(data.get('stage'), '关卡名称', required=True)
        profile = text_field(data.get('profile', ''), '角色 / 难度 / 配装')
        with self.guard:
            if self.active is not None:
                raise ValueError('已有计时采样，请先结束或取消。')
            live = live or {}
            auto = live.get('available') is True and isinstance(live.get('total_xp'), int)
            self.active = {'stage': stage, 'profile': profile, 'started_at': self.now(),
                           'start_xp': live.get('total_xp') if auto else None, 'auto_xp': auto,
                           'character': live.get('character'), 'session': live.get('session'),
                           '_character_id': live.get('character_id', live.get('character')), '_started': self.clock()}
            return self.active_payload()

    def active_payload(self):
        with self.guard:
            if self.active is None:
                return None
            return {k: v for k, v in self.active.items() if not k.startswith('_')} | {
                'elapsed_seconds': max(0, self.active.get('_ended', self.clock())-self.active['_started']),
                'stopped': '_ended' in self.active}

    def finish(self, data, live=None):
        with self.guard:
            if self.active is None:
                raise ValueError('尚未开始计时采样。')
            active = self.active
            if '_ended' not in active:
                if self.clock()-active['_started'] < 0.1:
                    raise ValueError('耗时至少为 0.1 秒。')
                active['_ended'] = self.clock()
                active['_finish_live'] = dict(live or {})
            seconds = active['_ended'] - active['_started']
            xp = data.get('xp')
            source = 'manual_timer'
            if xp is None:
                live = active['_finish_live']
                if not active['auto_xp'] or live.get('available') is not True or not isinstance(live.get('total_xp'), int):
                    raise ValueError('无法自动读取累计经验，请填入本次获得的经验后结束；计时已停止，等待补录。')
                if live.get('character_id', live.get('character')) != active['_character_id'] or live.get('session') != active['session']:
                    raise ValueError('角色或游戏连接已改变，请取消该采样或手动填写核实后的经验。')
                xp = live['total_xp'] - active['start_xp']
                if xp < 0:
                    raise ValueError('经验累计值下降，请取消采样或手动填写核实后的经验。')
                source = 'live_timer'
            result = self.add({'stage': active['stage'], 'profile': active['profile'], 'xp': xp, 'seconds': seconds}, source=source)
            self.active = None
            return result

    def cancel(self):
        with self.guard:
            self.active = None

    def page(self, *, minutes=60, profile='', sort='rate'):
        # A blank filter means all profiles; aggregation still keeps them separate.
        minutes = number(minutes, '预估时长（分钟）', 10080, positive=True)
        if sort not in ('rate', 'completed'):
            raise ValueError('经验比较口径无效。')
        profile = text_field(profile, '角色 / 难度 / 配装')
        budget = minutes * 60
        with self.guard, self._db() as db:
            db.row_factory = sqlite3.Row
            profiles = [r[0] for r in db.execute('SELECT DISTINCT profile FROM experience_samples ORDER BY profile')]
            condition, params = (' WHERE profile=?', (profile,)) if profile else ('', ())
            rows = []
            for row in db.execute('SELECT stage,profile,COUNT(*) samples,SUM(xp) total_xp,SUM(seconds) total_seconds FROM experience_samples'
                                  + condition + ' GROUP BY stage,profile', params):
                entry = dict(row)
                count, xp, seconds = entry['samples'], entry['total_xp'], entry['total_seconds']
                average_seconds = seconds / count
                runs = math.floor(budget / average_seconds)
                entry.update(average_xp=xp/count, average_seconds=average_seconds,
                             xp_per_hour=xp/seconds*3600, projected_xp=xp/seconds*budget,
                             completed_runs=runs, completed_runs_xp=runs*xp/count)
                rows.append(entry)
            sort_key = 'completed_runs_xp' if sort == 'completed' else 'projected_xp'
            rows.sort(key=lambda r: (-r[sort_key], -r['xp_per_hour'], -r['samples'], r['stage'], r['profile']))
            recent = [dict(r) for r in db.execute('SELECT * FROM experience_samples'+condition+' ORDER BY id DESC LIMIT 100', params)]
            return {'minutes': minutes, 'sort': sort, 'profiles': profiles, 'rows': rows, 'recent': recent,
                    'active': self.active_payload()}
