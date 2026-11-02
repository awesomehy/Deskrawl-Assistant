"""Run explicitly on Windows: hidden native WebView2, fake cookies, zero Steam I/O."""
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
    from deskrawl_assistant.steam_login_window import SteamLoginWindow
    from deskrawl_assistant.steam_network import LoginTunnel
    from deskrawl_assistant.steam_session import normalise_cookies

    class OfflineConnector:
        calls = 0
        def connect(self, host):
            self.calls += 1
            raise AssertionError('No upstream connections allowed during native smoke test')

    results, errors = {}, []
    connector = OfflineConnector()
    with tempfile.TemporaryDirectory(prefix='deskrawl-login-smoke-') as directory:
        parent = webview.create_window('Offline login smoke', html='<html></html>', hidden=True)
        def verify():
            child = None
            try:
                for _ in range(100):
                    if parent.native is not None: break
                    time.sleep(.05)
                child = SteamLoginWindow(parent.native, 'Offline Steam login', 'about:blank',
                    profile_directory=Path(directory)/'login', tunnel=LoginTunnel(connector), visible=False)
                deadline = time.monotonic() + 20
                while not child.ready and time.monotonic() < deadline: time.sleep(.05)
                assert child.ready, 'WebView2 initialization did not complete'
                def add_cookies():
                    manager = child.browser.CoreWebView2.CookieManager
                    for name, value in [('steamLoginSecure', '76561198000000001%7C%7Coffline-fake-token'),
                                        ('sessionid', 'abcdef1234567890abcdef12')]:
                        cookie = manager.CreateCookie(name, value, '.steamcommunity.com', '/')
                        cookie.IsSecure = True
                        cookie.IsHttpOnly = True
                        manager.AddOrUpdateCookie(cookie)
                child._invoke(add_cookies)
                values = normalise_cookies(child.get_cookies())
                assert set(values) == {'steamLoginSecure', 'sessionid'}
                assert child.get_current_url() == 'about:blank'
                props = child.browser.CreationProperties
                assert props.IsInPrivateModeEnabled
                assert '--proxy-server=http://127.0.0.1:' in props.AdditionalBrowserArguments
                assert '--proxy-server' not in parent.native.browser.webview.CreationProperties.AdditionalBrowserArguments
                ended = threading.Event()
                child.events.closed += ended.set
                child.destroy()
                assert ended.wait(3), 'Closed event missing'
                deadline = time.monotonic() + 5
                while child.tunnel.thread.is_alive() and time.monotonic() < deadline: time.sleep(.05)
                assert not child.tunnel.thread.is_alive()
                assert connector.calls == 0
                results.update(native_webview2=True, cookie_capture=True, isolated_proxy=True,
                    private_profile=True, closed_event=True, tunnel_released=True, steam_requests=0)
            except Exception:
                import traceback
                errors.append(traceback.format_exc())
            finally:
                if child is not None:
                    try: child.destroy()
                    except Exception: pass
                parent.destroy()
        webview.start(verify, gui='edgechromium', debug=False, private_mode=True,
                      storage_path=str(Path(directory)/'parent'))
    print(json.dumps({'checks': results, 'errors': errors}, ensure_ascii=False), flush=True)
    return 1 if errors else 0


if __name__ == '__main__': raise SystemExit(main())
