"""Isolated WebView2 login with window-scoped Steam Community acceleration."""
from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .steam_network import LoginTunnel, login_host_allowed
from .steam_login_transport import CommunityLoginTransport, attach_community_transport, community_request
from .steam_session import ORIGIN, SteamSessionError


def accelerated_login_url(url):
    """Ask Steam to serve its own Akamai assets; keep the login origin intact."""
    if not community_request(url): return url
    parsed = urlsplit(url)
    if parsed.path.rstrip('/') not in ('/login', '/login/home'): return url
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key != '_cdn']
    query.append(('_cdn', 'akamai'))
    return urlunsplit(parsed._replace(query=urlencode(query)))


class _ClosedEvent:
    def __init__(self):
        self.handlers = []
        self.ended = False
        self.guard = threading.Lock()

    def __iadd__(self, callback):
        with self.guard:
            if not self.ended: self.handlers.append(callback)
            ended = self.ended
        if ended: callback()
        return self

    def emit(self):
        with self.guard:
            if self.ended: return
            self.ended = True
            callbacks, self.handlers = self.handlers, []
        for callback in callbacks: callback()


class SteamLoginWindow:
    def __init__(self, parent, title, url, *, profile_directory, width=1000, height=760,
                 min_size=(740, 580), tunnel=None, community_transport=None, visible=True):
        self.parent = parent
        self.events = SimpleNamespace(closed=_ClosedEvent())
        self.form = self.browser = None
        self.ready = False
        root = Path(profile_directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.profile = TemporaryDirectory(prefix='login-', dir=root, ignore_cleanup_errors=True)
        # Only this newly created temporary child may be removed on disposal.
        if not Path(self.profile.name).resolve().is_relative_to(root):
            raise SteamSessionError('Steam 登录缓存目录无法确认。')
        self.tunnel = tunnel or LoginTunnel()
        self.community_transport = community_transport or CommunityLoginTransport()
        from .steam_login_diagnostics import LoginDiagnostics
        self.diagnostics = LoginDiagnostics(root.parent / 'steam-login-diagnostic.json')
        try:
            self.tunnel.start()
            self._invoke(lambda: self._create(title, url, width, height, min_size, visible))
        except Exception:
            if self.form is not None:
                try: self._invoke(self.form.Dispose)
                except Exception: pass
            self.tunnel.close()
            self.community_transport.close()
            self.profile.cleanup()
            raise

    def _invoke(self, callback):
        from System import Action
        if self.parent is None or self.parent.IsDisposed:
            raise SteamSessionError('助手窗口尚未就绪。')
        if self.parent.InvokeRequired: self.parent.Invoke(Action(callback))
        else: callback()

    def _create(self, title, url, width, height, min_size, visible):
        # These assemblies are already loaded by the main pywebview window.
        from System.Drawing import Size
        from System.Windows.Forms import Form, DockStyle, FormStartPosition, Label
        from Microsoft.Web.WebView2.WinForms import WebView2, CoreWebView2CreationProperties
        form, browser = Form(), WebView2()
        self.form, self.browser = form, browser
        form.Text = title
        form.Size = Size(width, height)
        form.MinimumSize = Size(*min_size)
        form.StartPosition = FormStartPosition.CenterScreen
        form.Icon = self.parent.Icon
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = self.profile.name
        props.IsInPrivateModeEnabled = True
        props.AdditionalBrowserArguments = self.tunnel.browser_arguments
        browser.CreationProperties = props
        browser.Dock = DockStyle.Fill
        form.Controls.Add(browser)

        def navigate(_, event):
            try:
                target = urlsplit(event.Uri)
                allowed = (target.scheme == 'https' and login_host_allowed(target.hostname or '')
                    and target.port in (None, 443) and not target.username and not target.password)
            except ValueError: allowed = False
            if not allowed:
                event.Cancel = True

        def popup(_, event):
            event.Handled = True

        def initialized(_, event):
            if form.IsDisposed: return
            if not event.IsSuccess:
                browser.Visible = False
                label = Label()
                label.Text = 'Steam 登录窗口初始化失败，请关闭后重试。'
                label.Dock = DockStyle.Fill
                form.Controls.Add(label)
                return
            self.ready = True
            browser.CoreWebView2.Settings.AreDevToolsEnabled = False
            browser.CoreWebView2.Settings.IsPasswordAutosaveEnabled = False
            browser.CoreWebView2.Settings.IsGeneralAutofillEnabled = False
            browser.CoreWebView2.NewWindowRequested += popup
            browser.CoreWebView2.NavigationStarting += navigate
            attach_community_transport(self, self.community_transport)
            from .steam_login_diagnostics import attach_login_diagnostics
            try: attach_login_diagnostics(self, self.diagnostics)
            except Exception: pass  # Optional diagnostics must not block login.
            browser.CoreWebView2.Navigate(accelerated_login_url(url))

        def closed(*_):
            self.ready = False
            self.events.closed.emit()
            browser.Dispose()
            def release():
                self.community_transport.close()
                self.tunnel.close()
                self.profile.cleanup()
            threading.Thread(target=release, name='steam-login-dispose', daemon=True).start()

        browser.CoreWebView2InitializationCompleted += initialized
        form.FormClosed += closed
        if not visible:
            # A native handle is enough for the offline component smoke test;
            # it must not display a second window over the user's game.
            _ = form.Handle
            _ = browser.Handle
        browser.EnsureCoreWebView2Async(None)
        if visible: form.Show()

    def show(self):
        def show():
            if self.form is not None and not self.form.IsDisposed:
                from System.Windows.Forms import FormWindowState
                self.form.Show()
                self.form.WindowState = FormWindowState.Normal
                self.form.Activate()
        self._invoke(show)

    def destroy(self):
        def close():
            if self.form is not None and not self.form.IsDisposed: self.form.Close()
        self._invoke(close)

    def get_current_url(self):
        values = []
        self._invoke(lambda: values.append(str(self.browser.CoreWebView2.Source) if self.ready else ''))
        return values[0]

    def get_cookies(self):
        done, values, errors = threading.Event(), [], []
        def begin():
            if not self.ready: done.set();return
            from System import Action
            task = self.browser.CoreWebView2.CookieManager.GetCookiesAsync(ORIGIN)
            def finish():
                try:
                    for item in task.GetAwaiter().GetResult():
                        if item.Name not in ('steamLoginSecure', 'sessionid'): continue
                        cookie = SimpleCookie()
                        cookie[item.Name] = item.Value
                        cookie[item.Name]['domain'] = item.Domain
                        cookie[item.Name]['path'] = item.Path
                        cookie[item.Name]['secure'] = item.IsSecure
                        cookie[item.Name]['httponly'] = item.IsHttpOnly
                        values.append(cookie)
                except Exception: errors.append(True)
                finally: done.set()
            task.GetAwaiter().OnCompleted(Action(lambda: self._invoke(finish)))
        self._invoke(begin)
        if not done.wait(5) or errors: raise SteamSessionError('Steam 登录状态暂未读取完成。')
        return values


def create_login_window(title, url, *, enabled, profile_directory, **options):
    import webview
    if not enabled(): return webview.create_window(title, url, **options)
    if not webview.windows: raise SteamSessionError('助手窗口尚未就绪。')
    return SteamLoginWindow(webview.windows[0].native, title, url,
        profile_directory=profile_directory, width=options.get('width', 1000),
        height=options.get('height', 760), min_size=options.get('min_size', (740, 580)))
