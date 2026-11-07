"""Windows WebView2 host for the existing local assistant interface."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request

from .paths import data_root, cache_root, RESOURCE_ROOT
from .action_log import record
from .web_server import AssistantServer, existing_service
from .version import VERSION
from .updater import application_ready


def verify_runtime():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full') as key:
            release = winreg.QueryValueEx(key,'Release')[0]
        if release < 394802: raise OSError('Outdated .NET Framework')
    except OSError:
        raise RuntimeError('独立窗口需要 .NET Framework 4.6.2 或更新版本。请安装 Windows 更新后重试。') from None
    guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    candidates = ((winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}'),
                  (winreg.HKEY_CURRENT_USER, rf'Software\Microsoft\EdgeUpdate\Clients\{guid}'))
    for hive, path in candidates:
        try:
            with winreg.OpenKey(hive,path) as key:
                version = winreg.QueryValueEx(key,'pv')[0]
            if tuple(int(n) for n in version.split('.')) >= (86,0,622,0): return
        except (OSError,ValueError,AttributeError): pass
    raise RuntimeError('独立窗口需要 Microsoft Edge WebView2 运行环境。\n'
        '请运行发布压缩包中的 MicrosoftEdgeWebview2Setup.exe，安装完成后再打开助手。\n'
        '安装时需要联网；助手本身不会打开系统浏览器。')


class WindowHooks:
    def __init__(self, window, service):
        self.window = window
        self.service = service
        self.dirty = False
        self.exiting = False
        self.minimized = False
        self.hotkeys = None
        self.hotkey_error = ''
        self.hotkey_capture_until = 0
        self.hidden = False
        self.tray = None
        self.tray_error = ''
        self.visibility_guard = threading.RLock()
        self.hotkey_guard = threading.RLock()

    def set_dirty(self, value):
        if not isinstance(value,bool): raise ValueError('配置状态必须为布尔值。')
        self.dirty = value

    def activate(self):
        threading.Thread(target=self.show_window,daemon=True).start()

    def show_window(self):
        with self.visibility_guard:
            if self.exiting: return
            self.window.show()
            if self.minimized: self.window.restore()
            self.minimized = False
            self.hidden = False

    def start_tray(self):
        if self.tray is not None: return
        from .desktop_tray import DesktopTray
        tray = DesktopTray(self.window, RESOURCE_ROOT / 'deskrawl_assistant/web/assistant.ico',
                           self.activate, self.request_exit, snapshot=self.service.tray_snapshot,
                           reprice=self.open_trade_reprice, sale=self.open_trade_sale)
        try: tray.start()
        except Exception:
            try: tray.close()
            except Exception: pass
            self.tray_error = '系统托盘未能启动，关闭窗口将暂时最小化到任务栏。'
            return
        self.tray = tray
        self.tray_error = ''

    def open_trade_reprice(self, record_id):
        if not isinstance(record_id, str) or not re.fullmatch(r'[a-f0-9]{32}', record_id):
            return
        def open_preview():
            if self.exiting: return
            self.show_window()
            try:
                self.window.evaluate_js('window.AuctionTrackingUI?.openReprice(' + json.dumps(record_id) + ');')
            except Exception:
                record('tray_reprice_preview_unavailable')
        threading.Thread(target=open_preview, name='tray-reprice-preview', daemon=True).start()

    def open_trade_sale(self, record_id):
        if (not isinstance(record_id, str)
                or not re.fullmatch(r'(?:[a-f0-9]{32}|inventory-[1-9]\d{0,24})', record_id)):
            return
        def open_preview():
            if self.exiting: return
            self.show_window()
            try:
                self.window.evaluate_js('window.AuctionTrackingUI?.openSale(' + json.dumps(record_id) + ');')
            except Exception:
                record('tray_sale_preview_unavailable')
        threading.Thread(target=open_preview, name='tray-sale-preview', daemon=True).start()

    def hide_to_tray(self):
        with self.visibility_guard:
            if self.exiting: return
            self.hotkey_capture_until = 0
            if self.tray is None:
                self.minimize_window()  # Keep a recovery path if tray startup failed.
                return
            self.window.hide()
            self.hidden = True

    def request_exit(self):
        def exit_window():
            if self.exiting: return
            if self.dirty:
                self.show_window()
                if not self.window.create_confirmation_dialog('退出助手', '有未保存的配置，仍要退出助手吗？'):
                    return
            self.finish_exit()
        threading.Thread(target=exit_window,daemon=True).start()

    def finish_exit(self):
        # Both the tray and the accepted HTTP shutdown converge here. Claim
        # window destruction once; do not turn this close into hide-to-tray.
        with self.visibility_guard:
            if self.exiting: return
            self.exiting = True
        self.window.destroy()

    def close_after_shutdown(self, stopping):
        # Waiting for service.close() would leave a dead UI visible while a
        # Steam request or an in-flight game operation finishes. The host's
        # finally still joins that cleanup after the window loop returns.
        stopping.wait()
        self.finish_exit()

    def closing(self):
        if self.exiting: return True
        # Closing the chrome must never stop the service or discard drafts.
        threading.Thread(target=self.hide_to_tray, daemon=True).start()
        return False

    def minimize_window(self):
        if self.exiting: return
        self.minimized = True
        self.window.minimize()

    def toggle_window(self):
        if self.exiting or time.monotonic() < self.hotkey_capture_until: return
        def toggle():
            with self.visibility_guard:
                if self.exiting or time.monotonic() < self.hotkey_capture_until: return
                if self.hidden or (self.tray is None and self.minimized):
                    self.show_window()
                else:
                    self.hide_to_tray()
        threading.Thread(target=toggle, daemon=True).start()

    def start_hotkeys(self):
        if self.hotkeys is not None: return
        shortcut = self.service.assistant_preferences.snapshot()['boss_hotkey']
        with self.hotkey_guard:
            if self.hotkeys is not None: return
            from .window_hotkey import GlobalHotkey
            self.hotkeys = GlobalHotkey(self.toggle_window)
            if shortcut:
                try: self.hotkeys.change(shortcut)
                except (ValueError, OSError) as exc: self.hotkey_error = str(exc)

    def apply_hotkey(self, shortcut, persist):
        self.start_hotkeys()
        with self.hotkey_guard:
            self.hotkeys.change(shortcut, persist)
            self.hotkey_error = ''
            self.hotkey_capture_until = 0

    def hotkey_state(self):
        return {'active': bool(self.hotkeys and self.hotkeys.shortcut and not self.hotkeys.error),
                'error': self.hotkey_error or (self.hotkeys.error if self.hotkeys else '')}

    def release_hotkeys(self):
        if self.hotkeys: self.hotkeys.close()

    def release_desktop(self):
        self.release_hotkeys()
        if self.tray:
            self.tray.close()
            self.tray = None

    def clear_browser_cache(self):
        if self.tray is None: raise ValueError('请在助手独立窗口中清理浏览器缓存。')
        return self.tray.clear_browser_cache()

    def capture_hotkey(self, capturing):
        if type(capturing) is not bool: raise ValueError('快捷键录入状态无效。')
        self.hotkey_capture_until = time.monotonic() + 30 if capturing else 0
        return {'capturing': capturing}

    def closed(self):
        self.exiting = True
        self.release_desktop()
        self.service.stop()
        record('desktop_window_closed')

    def minimize(self): self.minimized = True
    def restore(self): self.minimized = False


def activate_existing(port):
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base+'/api/session',timeout=2) as response:
        token = json.load(response)['token']
    request = urllib.request.Request(base+'/api/window/activate',data=b'{}',
        headers={'Content-Type':'application/json','X-Assistant-Token':token,'Origin':base})
    with opener.open(request,timeout=3) as response:
        json.load(response)


def main(port=18741, *, test_managed=False):
    running = existing_service(port)
    if running:
        if running.get('desktop'):
            activate_existing(port)
            return
        raise RuntimeError('旧版网页助手仍在运行。请先点击旧版页面右上角的电源按钮退出，再打开独立窗口版。')
    verify_runtime()
    import webview
    webview.settings['ALLOW_DOWNLOADS'] = True
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
    service = None
    if test_managed:
        from .runtime_client import RuntimeClient
        from .web_service import AssistantService
        service = AssistantService(client=RuntimeClient(managed_test_mode=True))
    host = AssistantServer(port, service=service)
    try:
        title = 'Deskrawl 装备助手 v' + VERSION + (' · 托管测试版' if test_managed else '')
        window = webview.create_window(title,host.url+'?desktop=1',
            width=1280,height=820,min_size=(980,680),resizable=True,
            background_color='#11151c',text_select=True,zoomable=False)
        hooks = WindowHooks(window,host.service)
        host.server.desktop = hooks
        window.events.closing += hooks.closing
        window.events.closed += hooks.closed
        window.events.minimized += hooks.minimize
        window.events.restored += hooks.restore
        window.events.maximized += hooks.restore
        window.events.shown += lambda: record('desktop_window_shown')
        def loaded():
            record('desktop_window_loaded')
            hooks.start_tray()
            hooks.start_hotkeys()
            host.service.enable_native_steam_acceleration()
            application_ready()
        window.events.loaded += loaded
        host.start()
        threading.Thread(target=hooks.close_after_shutdown,args=(host.stopping,),daemon=True).start()
        cache = cache_root()
        cache.mkdir(parents=True,exist_ok=True)
        webview.start(gui='edgechromium',debug=False,private_mode=True,storage_path=str(cache),
            icon=str(RESOURCE_ROOT / 'deskrawl_assistant/web/assistant.ico'))
    finally:
        if 'hooks' in locals(): hooks.release_desktop()
        host.close()
