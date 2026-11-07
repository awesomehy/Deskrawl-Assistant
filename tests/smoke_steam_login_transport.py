"""Windows native login interception: fake responses/cookies only, zero Steam I/O."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import webview
    from deskrawl_assistant.steam_acceleration import ProbeFailure
    from deskrawl_assistant.steam_login_window import SteamLoginWindow
    from deskrawl_assistant.steam_network import LoginTunnel
    from deskrawl_assistant.steam_session import normalise_cookies

    class OfflineConnector:
        def connect(self, host): raise ProbeFailure('offline_no_network')

    class OfflineTransport:
        closed = False
        def __init__(self): self.requests = [];self.prepared = False
        def prepare(self): self.prepared = True;return True
        def request(self, url, method, headers, body):
            assert self.prepared
            self.requests.append((url, method, {key.lower():value for key,value in headers}, body))
            if url.endswith('/start'):
                document = b'''<html><script>
                    fetch('/login/settoken', {method:'POST',credentials:'include',body:'offline-token'});
                    fetch('/login/omit', {credentials:'omit'});
                    fetch('/jwt/finalizelogin?nonce=offline-secret', {method:'POST',credentials:'include',body:'offline-secret'});
                    fetch('/login/transfer?nonce=offline-secret', {method:'POST',credentials:'include',body:'offline-secret'});
                    </script></html>'''
                return 200, [('Content-Type', 'text/html'),
                    ('Set-Cookie', 'steamLoginSecure=76561198000000001%7C%7Coffline-token; Path=/; Secure; HttpOnly; SameSite=None'),
                    ('Set-Cookie', 'sessionid=abcdef1234567890abcdef12; Path=/; Secure; HttpOnly; SameSite=None')], document
            if '/login/transfer' in url:
                raise ProbeFailure('offline-secret proxy-detail', kind='network')
            if '/jwt/finalizelogin' in url:
                body = json.dumps({'steamID':'76561198000000001', 'refresh_token':'offline-secret',
                    'primary_domain':'.steamcommunity.com', 'transfer_info':[
                        {'url':'https://steamcommunity.com/login/settoken', 'params':{'nonce':'offline-secret'}}]})
                return 200, [('Content-Type', 'application/json')], body.encode()
            return 200, [('Content-Type', 'text/plain')], b'ok'
        def close(self): self.closed = True

    results, errors = {}, []
    transport=OfflineTransport()
    with tempfile.TemporaryDirectory(prefix='deskrawl-login-transport-smoke-') as directory:
        parent=webview.create_window('Offline login transport',html='<html/>',hidden=True)
        def verify():
            child=None
            try:
                for _ in range(100):
                    if parent.native is not None: break
                    time.sleep(.05)
                child=SteamLoginWindow(parent.native, 'Offline',
                    'https://steamcommunity.com/login/start', profile_directory=Path(directory)/'login',
                    tunnel=LoginTunnel(OfflineConnector()), community_transport=transport, visible=False)
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    if (any(row[0].endswith('/omit') for row in transport.requests)
                            and any(row[0].endswith('/settoken') for row in transport.requests)
                            and any('/jwt/finalizelogin' in row[0] for row in transport.requests)): break
                    time.sleep(.05)
                assert child.ready
                posted=[r for r in transport.requests if r[0].endswith('/settoken')]
                omitted=[r for r in transport.requests if r[0].endswith('/omit')]
                assert len(posted)==1 and posted[0][1]=='POST' and posted[0][3]==b'offline-token'
                assert 'sessionid=abcdef1234567890abcdef12' in posted[0][2].get('cookie','')
                assert omitted and not omitted[0][2].get('cookie')
                assert set(normalise_cookies(child.get_cookies()))=={'steamLoginSecure','sessionid'}
                assert child.get_current_url()=='https://steamcommunity.com/login/start'
                deadline=time.monotonic()+5
                while not child.login_complete and time.monotonic()<deadline: time.sleep(.05)
                assert child.login_complete
                diagnostic_file=Path(directory)/'steam-login-diagnostic.json'
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    data=json.loads(diagnostic_file.read_text('utf-8')) if diagnostic_file.exists() else {}
                    summaries=[row for row in data.get('events',[]) if row['event']=='result']
                    failures=[row for row in data.get('events',[]) if row['event']=='transport_error']
                    if summaries and failures: break
                    time.sleep(.05)
                assert summaries and summaries[0]['identity_present']
                assert summaries[0]['transfer_count']==1
                assert summaries[0]['transfer_hosts']==['steamcommunity.com']
                assert failures and failures[0]['code']=='login_transport_connection_failed'
                raw=diagnostic_file.read_text('utf-8')
                assert all(secret not in raw for secret in ('offline-secret','offline-token','76561198000000001','nonce='))
                ended=threading.Event()
                child.events.closed += ended.set
                child.destroy()
                assert ended.wait(3)
                deadline=time.monotonic()+5
                while child.tunnel.thread.is_alive() and time.monotonic()<deadline: time.sleep(.05)
                assert transport.closed and not child.tunnel.thread.is_alive()
                results.update(native_interception=True, original_https_origin=True, cookie_roundtrip=True,
                    omit_credentials_respected=True, post_forwarded_once=True, post_body_unchanged=True,
                    transport_released=True, safe_finalize_diagnostics=True, safe_transport_failure_diagnostics=True,
                    community_completion_before_navigation=True, route_prepared_before_navigation=True,
                    steam_requests=0)
            except Exception:
                import traceback
                errors.append(traceback.format_exc())
            finally:
                if child is not None:
                    try: child.destroy()
                    except Exception: pass
                parent.destroy()
        webview.start(verify, gui='edgechromium', private_mode=True, storage_path=str(Path(directory)/'parent'))
    print(json.dumps({'checks':results, 'errors':errors},ensure_ascii=False),flush=True)
    return bool(errors)


if __name__=='__main__': raise SystemExit(main())
