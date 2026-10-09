"""Windows WebView2 host for the existing local assistant interface."""
from __future__ import annotations

import json
import threading
import urllib.request

from .paths import data_root
from .action_log import record
from .web_server import AssistantServer, existing_service


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

    def set_dirty(self, value):
        if not isinstance(value,bool): raise ValueError('配置状态必须为布尔值。')
        self.dirty = value

    def activate(self):
        def bring_forward():
            if self.minimized: self.window.restore()
            self.window.show()
        threading.Thread(target=bring_forward,daemon=True).start()

    def closing(self):
        if self.dirty and not self.exiting:
            return bool(self.window.create_confirmation_dialog('退出装备助手？',
                '当前规则有未保存的改动。退出将放弃改动，并停止持续监控。'))
        return True

    def closed(self):
        self.exiting = True
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


def main(port=18741):
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
    host = AssistantServer(port)
    try:
        window = webview.create_window('Deskrawl 装备助手 v1.1.4',host.url+'?desktop=1',
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
        window.events.loaded += lambda: record('desktop_window_loaded')
        host.start()
        def close_after_shutdown():
            host.finished.wait()
            if not hooks.exiting:
                hooks.exiting = True
                window.destroy()
        threading.Thread(target=close_after_shutdown,daemon=True).start()
        cache = data_root() / 'webview-cache'
        cache.mkdir(parents=True,exist_ok=True)
        webview.start(gui='edgechromium',debug=False,private_mode=True,storage_path=str(cache))
    finally:
        host.close()
