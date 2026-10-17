"""Automatic anonymous community metrics. Never sends inventory or performs game actions."""
from __future__ import annotations

from datetime import datetime
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

DEFAULT_URL = 'http://hhyan.me:46293'
LEGACY_DEFAULT_URL = 'https://hhyan.me:46293'
CLASS_NAMES = {1: 'warrior', 2: 'mage', 4: 'hunter', 8: 'monk'}


def validate_url(value):
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError('统计服务地址无效。')
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError:
        raise ValueError('统计服务端口无效。') from None
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ('', '/') or parts.scheme not in ('https', 'http')):
        raise ValueError('请填写服务根地址，例如 http://hhyan.me:46293。')
    if parts.scheme == 'http':
        try:
            ip = ipaddress.ip_address(parts.hostname)
            private = ip.is_loopback or any(ip in ipaddress.ip_network(net) for net in
                (('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16') if ip.version == 4 else ('fc00::/7',)))
        except ValueError:
            private = False
        if not private and (parts.hostname.lower(), parts.port) != ('hhyan.me', 46293):
            raise ValueError('其他公网统计地址需要 HTTPS；当前已配置的 hhyan.me:46293 可使用 HTTP。')
    return value.rstrip('/')


class NoRedirects:
    """Constructed lazily to avoid importing networking in game read paths."""
    @staticmethod
    def open(request):
        from urllib.request import HTTPRedirectHandler, build_opener
        class Reject(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        return build_opener(Reject()).open(request, timeout=6)


class CommunityStats:
    def __init__(self, path, version, provider=lambda: False, *, start=True, transport=None, statistics_provider=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock, self.stop_event, self.wake = threading.RLock(), threading.Event(), threading.Event()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=3)
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        defaults = {'enabled': True, 'url': DEFAULT_URL, 'client_id': str(uuid.uuid4()), 'token': '',
                    'sequence': 0, 'play_seconds': 0, 'divine_count': 0, 'rune_set_count': 0,
                    'acknowledged': 0, 'last_ack': None, 'pending': None, 'statistics_valid': False}
        with self.db:
            for key, value in defaults.items():
                self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, json.dumps(value)))
        self.values = {key: json.loads(value) for key, value in self.db.execute('SELECT key,value FROM settings')}
        # Community reporting is automatic; the former development opt-out flag
        # no longer controls the worker after upgrade.
        with self.db:
            self._set('enabled', True)
        # The earlier development default assumed TLS, but the deployed NAS
        # listener uses HTTP. Change only the never-acknowledged old default;
        # preserve custom servers and a working explicitly configured TLS URL.
        if self.values['url'] == LEGACY_DEFAULT_URL and not self.values['acknowledged']:
            with self.db:
                self._set('url', DEFAULT_URL)
                self._set('token', '')
                self._set('pending', None)
        # Replace the earlier inventory-based observation cache once. Old estimates
        # and pending reports must not be presented as native game totals.
        if self.values.get('statistics_source') != 'game_statistics':
            with self.db:
                for key in ('play_seconds', 'divine_count', 'rune_set_count'):
                    self._set(key, 0)
                self._set('statistics_valid', False)
                self._set('pending', None)
                self._set('statistics_source', 'game_statistics')
                self.db.execute('DROP TABLE IF EXISTS seen')
        self.version, self.provider, self.transport = version, provider, transport or self._request
        self.statistics_provider = statistics_provider
        self.character, self.last_observed = None, 0
        self.error, self.read_error = '', ''
        self.worker = None
        if start:
            self.worker = threading.Thread(target=self._loop, daemon=True, name='community-statistics')
            self.worker.start()

    def _set(self, key, value):
        self.values[key] = value
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    def configure(self, data):
        if not isinstance(data, dict) or set(data) != {'url'}:
            raise ValueError('社区统计设置无效。')
        url = validate_url(data['url'])
        with self.lock, self.db:
            if url != self.values['url']:
                self._set('token', '')
                self._set('pending', None)
                self._set('last_ack', None)
            self._set('url', url)
            self.error, self.read_error = '', ''
        self.wake.set()
        return self.status()

    def status(self):
        with self.lock:
            return {key: self.values[key] for key in ('enabled', 'url', 'play_seconds', 'divine_count', 'rune_set_count',
                                                    'acknowledged', 'last_ack', 'statistics_valid')} | {
                    'error': self.error or self.read_error, 'interval_seconds': 30, 'source': 'game_statistics'}

    def observe(self, statistics):
        """Copy game totals exactly, including historical data and character resets."""
        with self.lock:
            if not self.values['enabled']:
                return
            keys = ('play_seconds', 'divine_count', 'rune_set_count')
            if (not isinstance(statistics, dict) or statistics.get('available') is not True
                    or statistics.get('source') != 'verified_game_statistics'
                    or not statistics.get('session') or statistics.get('class_mask') not in CLASS_NAMES
                    or type(statistics.get('level')) is not int or not 1 <= statistics['level'] <= 200
                    or any(type(statistics.get(key)) is not int or not 0 <= statistics[key] <= 10**10 for key in keys)):
                self.last_observed = 0
                self.read_error = '游戏累计统计暂不可读，保留上次有效数据；进入角色后自动重试。'
                return
            self.last_observed = time.monotonic()
            self.character = {'player_class': CLASS_NAMES[statistics['class_mask']], 'level': statistics['level']}
            self.read_error = ''
            with self.db:
                for key in keys:
                    self._set(key, statistics[key])
                self._set('statistics_valid', True)

    def _request(self, url, data, token=''):
        headers = {'Content-Type': 'application/json', 'User-Agent': 'Deskrawl-Assistant-Community/1'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        with NoRedirects.open(Request(url, json.dumps(data).encode(), headers)) as response:
            if not 200 <= response.status < 300:
                raise ValueError('统计服务未确认上报。')
            return json.loads(response.read(8192))

    def report_once(self):
        # This dedicated worker reads every 30 seconds even with the UI closed or
        # inventory monitoring disabled. Failures cannot affect game automation.
        with self.lock:
            enabled = self.values['enabled'] and not self.stop_event.is_set()
        if not enabled:
            return False
        if self.statistics_provider is not None:
            try:
                statistics = self.statistics_provider() if self.provider() else None
            except Exception:
                statistics = None
            self.observe(statistics)
        with self.lock:
            if not self.values['enabled'] or self.stop_event.is_set():
                return False
            url, identity, token = self.values['url'], self.values['client_id'], self.values['token']
        if not token:
            registration = self.transport(url + '/v1/register', {'schema': 1, 'client_id': identity})
            if (registration.get('client_id') != identity or registration.get('schema') != 1 or
                    not re.fullmatch(r'[0-9a-f]{64}', registration.get('token', ''))):
                raise ValueError('统计服务注册回执无效。')
            with self.lock, self.db:
                if self.stop_event.is_set() or not self.values['enabled'] or self.values['url'] != url:
                    return False
                token = registration['token']; self._set('token', token)
        with self.lock, self.db:
            if self.stop_event.is_set() or not self.values['enabled'] or self.values['url'] != url:
                return False
            report = self.values['pending']
            if not report:
                connected = bool(self.provider() and self.character and time.monotonic() - self.last_observed < 30)
                report = {'schema': 1, 'client_id': identity, 'report_id': str(uuid.uuid4()),
                          'sequence': self.values['sequence'] + 1, 'version': self.version, 'connected': connected,
                          'player_class': self.character['player_class'] if connected else 'unknown',
                          'level': self.character['level'] if connected else None,
                          **{k: self.values[k] for k in ('play_seconds', 'divine_count', 'rune_set_count')}}
                self._set('sequence', report['sequence']); self._set('pending', report)
        try:
            receipt = self.transport(url + '/v1/report', report, token)
        except HTTPError as exc:
            if exc.code in (400, 401):
                with self.lock, self.db:
                    if self.values['url'] == url:
                        self._set('token', ''); self._set('pending', None)
            raise
        if (receipt.get('accepted') is not True or receipt.get('report_id') != report['report_id'] or
                receipt.get('sequence') != report['sequence'] or not isinstance(receipt.get('accepted_at'), str)):
            raise ValueError('统计服务回执不匹配，将重试同一份上报。')
        if datetime.fromisoformat(receipt['accepted_at']).tzinfo is None:
            raise ValueError('统计服务回执时间无效。')
        with self.lock, self.db:
            if not self.values['enabled'] or self.values['url'] != url or self.values['pending'] != report:
                return False
            self._set('pending', None); self._set('last_ack', receipt['accepted_at'])
            self._set('acknowledged', self.values['acknowledged'] + 1); self.error = ''
        return True

    def _loop(self):
        next_report = 0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= next_report:
                    started = time.monotonic()
                    try:
                        self.report_once()
                    except Exception:
                        with self.lock:
                            self.error = '统计服务暂不可用，稍后重试；游戏和托管继续运行。'
                    next_report = started + 30
            except Exception:
                self.error = '本地统计暂不可用；游戏和托管继续运行。'
            self.wake.wait(1)
            if self.wake.is_set():
                next_report = 0; self.wake.clear()

    def close(self):
        self.stop_event.set(); self.wake.set()
        if self.worker and self.worker is not threading.current_thread():
            # statistics_provider may still own a native client read. Keep the
            # database until that read and the
            # worker have finished; a timed join does not confirm completion.
            self.worker.join()
        with self.lock:
            self.db.close()
