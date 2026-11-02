"""WinForms tray and WebView2 cache operations on their owning UI thread."""
from __future__ import annotations

import threading


class DesktopTray:
    def __init__(self, window, icon_path, show, exit):
        self.window, self.icon_path = window, str(icon_path)
        self.show_callback, self.exit_callback = show, exit
        self.icon = self.menu = self.image = None

    def _invoke(self, callback):
        from System import Action
        form = self.window.native
        if form is None or form.IsDisposed:
            raise ValueError('助手窗口尚未就绪。')
        if form.InvokeRequired:
            form.Invoke(Action(callback))
        else:
            callback()

    def start(self):
        def create():
            if self.icon is not None: return
            from System.Drawing import Icon
            from System.Windows.Forms import NotifyIcon, ContextMenuStrip, ToolStripMenuItem
            self.image = Icon(self.icon_path)
            self.menu = ContextMenuStrip()
            show = ToolStripMenuItem('显示助手')
            quit = ToolStripMenuItem('退出助手')
            show.Click += lambda *_: self.show_callback()
            quit.Click += lambda *_: self.exit_callback()
            self.menu.Items.Add(show)
            self.menu.Items.Add(quit)
            self.icon = NotifyIcon()
            self.icon.Icon = self.image
            self.icon.Text = 'Deskrawl 装备助手'
            self.icon.ContextMenuStrip = self.menu
            self.icon.DoubleClick += lambda *_: self.show_callback()
            self.icon.Visible = True
        self._invoke(create)

    def clear_browser_cache(self):
        done, errors = threading.Event(), []
        def begin():
            try:
                from System import Action
                from Microsoft.Web.WebView2.Core import CoreWebView2BrowsingDataKinds as Kinds
                core = self.window.native.browser.webview.CoreWebView2
                task = core.Profile.ClearBrowsingDataAsync(Kinds.DiskCache | Kinds.CacheStorage)
                def finish():
                    try: task.GetAwaiter().GetResult()
                    except Exception: errors.append('内置浏览器缓存未能清理，请稍后重试。')
                    finally: done.set()
                task.GetAwaiter().OnCompleted(Action(finish))
            except Exception:
                errors.append('当前内置浏览器不支持清理缓存，请更新 WebView2 后重试。')
                done.set()
        self._invoke(begin)
        if not done.wait(30): raise ValueError('内置浏览器仍在清理缓存，请稍后检查。')
        if errors: raise ValueError(errors[0])
        return {'cleared': True}

    def close(self):
        def dispose():
            if self.icon is not None:
                self.icon.Visible = False
                self.icon.Dispose()
                self.icon = None
            if self.menu is not None:
                self.menu.Dispose()
                self.menu = None
            if self.image is not None:
                self.image.Dispose()
                self.image = None
        # The native handle may already be disposed during final shutdown.
        if self.window.native is not None and not self.window.native.IsDisposed:
            self._invoke(dispose)
        else:
            dispose()
