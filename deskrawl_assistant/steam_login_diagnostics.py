"""Bounded local login diagnostics; never retain URLs, credentials or response bodies."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from .steam_login_transport import LOGIN_AUTH_PATHS

_HOSTS = frozenset(('steamcommunity.com', 'login.steampowered.com', 'store.steampowered.com',
                    'api.steampowered.com'))
_PATHS = LOGIN_AUTH_PATHS | frozenset((
    '/jwt/finalizelogin', '/jwt/finalizelogin/', '/jwt/settoken', '/jwt/settoken/',
    '/jwt/refresh', '/jwt/refresh/', '/login/settoken', '/login/settoken/',
    '/login/transfer', '/login/transfer/', '/login/home/',
))
_AUTH_METHODS = frozenset(path.split('/')[-2] for path in LOGIN_AUTH_PATHS)


def diagnostic_target(url):
    try:
        target = urlsplit(url)
        if (target.scheme != 'https' or target.hostname not in _HOSTS
                or target.port not in (None, 443) or target.username or target.password):
            return None
        if target.path in _PATHS:
            return {'host': target.hostname, 'path': target.path}
        parts = target.path.strip('/').split('/')
        if (target.hostname == 'api.steampowered.com' and len(parts) == 3
                and parts[0] == 'IAuthenticationService' and parts[1] in _AUTH_METHODS
                and parts[2] == 'v1'):
            return {'host': target.hostname, 'path': target.path}
    except ValueError:
        pass
    return None


def response_summary(raw):
    """Only structural/numeric flags from auth JSON; discard tokens and identities."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        return {}
    if not isinstance(data, dict): return {}
    safe = {}
    for key in ('result', 'eresult', 'success'):
        value = data.get(key)
        if type(value) is int and 0 <= value <= 1000: safe[key] = value
    if 'transfer_info' in data:
        safe['transfer_list_present'] = isinstance(data['transfer_info'], list)
        if isinstance(data['transfer_info'], list):
            safe['transfer_count'] = min(100, len(data['transfer_info']))
            safe['transfer_hosts'] = sorted({target['host'] for row in data['transfer_info']
                if isinstance(row, dict) and (target := diagnostic_target(row.get('url', '')))})
    if 'steamID' in data: safe['identity_present'] = bool(data['steamID'])
    domain = data.get('primary_domain')
    if isinstance(domain, str) and domain.lstrip('.') in _HOSTS:
        safe['primary_domain'] = domain.lstrip('.')
    return safe


class LoginDiagnostics:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.events = []
        self.created = datetime.now(timezone.utc).isoformat()

    def record(self, event, target, **details):
        if not target: return
        # Callers provide only fields built below, never arbitrary exception text.
        with self.lock:
            row = {'event': event, **target, 'seconds': round(time.monotonic() - self.started, 2)}
            row.update(details)
            self.events.append(row)
            self.events = self.events[-80:]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'schema_version': 1, 'created_at': self.created,
                    'events': self.events}, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
                temporary.replace(self.path)
            except OSError:
                pass  # Diagnostics must never affect the authentication flow.


def attach_login_diagnostics(window, diagnostics):
    """Observe only endpoint metadata through native WebView2; no JS/cookie reads."""
    core = window.browser.CoreWebView2
    pending = {}

    def response(_, event):
        target = diagnostic_target(str(event.Request.Uri))
        if not target: return
        fields = {'status': int(event.Response.StatusCode)}
        for key in ('x-eresult', 'x-errorcode'):
            if event.Response.Headers.Contains(key):
                value = str(event.Response.Headers.GetHeader(key))
                if value.isascii() and value.isdigit() and len(value) <= 3:
                    fields[key] = int(value)
        diagnostics.record('response', target, **fields)
        # Native response bodies can contain refresh tokens. Read only this small
        # JSON result in memory, retain structural flags, and never persist raw.
        if target['path'].startswith(('/jwt/', '/login/settoken', '/login/transfer')):
            from System import Action, Array, Byte
            task = event.Response.GetContentAsync()
            def finish():
                try:
                    stream = task.GetAwaiter().GetResult()
                    if stream is None: return
                    try:
                        buffer = Array.CreateInstance(Byte, 65536)
                        length = stream.Read(buffer, 0, len(buffer))
                        raw = bytes(buffer)[:length]
                    finally:
                        stream.Dispose()
                    fields = response_summary(raw)
                    if fields: diagnostics.record('result', target, **fields)
                except Exception:
                    pass
            def completed():
                try: window._invoke(finish)
                except Exception: pass  # Window may close before the read completes.
            task.GetAwaiter().OnCompleted(Action(completed))

    def requested(_, event):
        target = diagnostic_target(str(event.Request.Uri))
        if target: diagnostics.record('request', target, method=str(event.Request.Method))

    for name in ('Network.requestWillBeSent', 'Network.loadingFailed', 'Network.loadingFinished'):
        receiver = core.GetDevToolsProtocolEventReceiver(name)
        def protocol(_, event, name=name):
            try:
                value = json.loads(str(event.ParameterObjectAsJson))
                request_id = value.get('requestId')
                if name == 'Network.requestWillBeSent':
                    target = diagnostic_target(value.get('request', {}).get('url', ''))
                    if target: pending[request_id] = target
                    if len(pending) > 100: pending.clear()
                else:
                    target = pending.pop(request_id, None)
                    if target and name == 'Network.loadingFailed':
                        error = value.get('errorText', '')
                        # Fixed browser error identifiers only; never arbitrary text.
                        import re
                        code = error if re.fullmatch(r'net::ERR_[A-Z_]{1,80}', error) else 'network_error'
                        diagnostics.record('network_error', target, code=code)
            except Exception:
                pass
        receiver.DevToolsProtocolEventReceived += protocol
    def safe(callback):
        def invoke(*args):
            try: callback(*args)
            except Exception: pass
        return invoke
    core.WebResourceRequested += safe(requested)
    core.WebResourceResponseReceived += safe(response)
    core.CallDevToolsProtocolMethodAsync('Network.enable', '{}')
