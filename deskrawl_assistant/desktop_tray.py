"""WinForms tray and WebView2 cache operations on their owning UI thread."""
from __future__ import annotations

import threading
import time
import re


class TraySnapshotCache:
    """UI reads are constant-time; background projections never touch WinForms."""
    def __init__(self, provider=None, *, clock=time.monotonic):
        self.provider, self.clock = provider, clock
        self.value = None
        self.stopped = threading.Event()
        self.thread = None

    def refresh(self):
        try:
            value = self.provider() if self.provider else None
        except Exception:
            value = None
        if isinstance(value, dict) and not self.stopped.is_set():
            # Publish one immutable-by-convention tuple so the UI cannot mix
            # a new timestamp with an older projection.
            self.value = (self.clock(), value)

    def start(self):
        if not self.provider or self.thread is not None: return
        def work():
            while not self.stopped.is_set():
                self.refresh()
                self.stopped.wait(2)
        self.thread = threading.Thread(target=work, name='tray-status', daemon=True)
        self.thread.start()

    def read(self):
        cached = self.value
        if cached and 0 <= self.clock() - cached[0] <= 10:
            return cached[1]
        return {'dungeon': '当前副本：等待读取', 'loot': [], 'unavailable': True}

    def close(self):
        self.stopped.set()


def _menu_text(value, limit=90):
    text = ' '.join(str(value).split())
    text = text if len(text) <= limit else text[:limit - 1] + '…'
    return text.replace('&', '&&')  # WinForms treats a lone & as a mnemonic.


class DesktopTray:
    def __init__(self, window, icon_path, show, exit, *, snapshot=None, reprice=None, sale=None):
        self.window, self.icon_path = window, str(icon_path)
        self.show_callback, self.exit_callback = show, exit
        self.reprice_callback = reprice
        self.sale_callback = sale
        self.icon = self.menu = self.image = None
        self.status_cache = TraySnapshotCache(snapshot)
        self.dungeon_item = self.loot_item = None

    @staticmethod
    def _item(text, *, enabled=True):
        from System.Windows.Forms import ToolStripMenuItem
        item = ToolStripMenuItem(_menu_text(text))
        item.ToolTipText = str(text)
        item.Enabled = enabled
        return item

    def _refresh_menu(self, *_):
        value = self.status_cache.read()
        self.dungeon_item.Text = _menu_text(value['dungeon'])
        self.dungeon_item.ToolTipText = value['dungeon']
        # Remove and dispose old children (including nested pages) on the
        # owning UI thread; repeated opens must not leak native menu handles.
        children = self.loot_item.DropDownItems
        while children.Count:
            old = children[0]
            children.RemoveAt(0)
            old.Dispose()
        rows = value.get('loot', [])
        other = value.get('other_trades', [])
        total = len(rows) + len(other)
        self.loot_item.Text = '待交易物品' + (f'（{total} 条）' if total else '')
        if not rows and not other:
            label = ('信息正在更新，请稍后重试' if value.get('unavailable') else
                     '当前暂无待交易物品')
            children.Add(self._item(label, enabled=False))
        else:
            self._add_rows(children, rows, value.get('loot_actions', []))
        if other:
            folded = self._item(f'未满足自动上架条件（{len(other)} 条）')
            self._add_rows(folded.DropDownItems, other, value.get('other_trade_actions', []))
            children.Add(folded)

    def _trade_item(self, text, record_id=None):
        callback, identity, suffix = None, None, ''
        if (self.reprice_callback and isinstance(record_id, str)
                and re.fullmatch(r'[a-f0-9]{32}', record_id)):
            callback, identity, suffix = self.reprice_callback, record_id, ' · 点击改价'
        elif (self.sale_callback and isinstance(record_id, dict) and record_id.get('kind') == 'sale'
                and isinstance(record_id.get('id'), str)
                and re.fullmatch(r'(?:[a-f0-9]{32}|inventory-[1-9]\d{0,24})', record_id['id'])):
            callback, identity, suffix = self.sale_callback, record_id['id'], ' · 点击定价上架'
        item = self._item(text + suffix, enabled=bool(callback))
        if callback:
            # Only open a read-only shared preview. Menu clicks never POST a
            # cancellation/sale and never perform network I/O on the UI thread.
            item.Click += lambda *_: callback(identity)
        return item

    def _add_rows(self, children, rows, actions=()):
        def entry(index):
            return self._trade_item(rows[index], actions[index] if index < len(actions) else None)
        if len(rows) <= 25:
            for index in range(len(rows)): children.Add(entry(index))
        else:
            for start in range(0, len(rows), 25):
                page = self._item(f'第 {start + 1}–{min(start + 25, len(rows))} 条')
                for index in range(start, min(start + 25, len(rows))):
                    page.DropDownItems.Add(entry(index))
                children.Add(page)

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
            from System.Windows.Forms import NotifyIcon, ContextMenuStrip, ToolStripSeparator
            self.image = Icon(self.icon_path)
            self.menu = ContextMenuStrip()
            self.menu.ShowItemToolTips = True
            self.dungeon_item = self._item('当前副本：等待读取', enabled=False)
            self.loot_item = self._item('待交易物品')
            self.menu.Items.Add(self.dungeon_item)
            self.menu.Items.Add(self.loot_item)
            self.menu.Items.Add(ToolStripSeparator())
            show = self._item('显示助手')
            quit = self._item('退出助手')
            show.Click += lambda *_: self.show_callback()
            quit.Click += lambda *_: self.exit_callback()
            self.menu.Items.Add(show)
            self.menu.Items.Add(quit)
            self.menu.Opening += self._refresh_menu
            self._refresh_menu()
            self.icon = NotifyIcon()
            self.icon.Icon = self.image
            self.icon.Text = 'Deskrawl 装备助手'
            self.icon.ContextMenuStrip = self.menu
            self.icon.DoubleClick += lambda *_: self.show_callback()
            self.icon.Visible = True
        self._invoke(create)
        self.status_cache.start()

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
        self.status_cache.close()
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
