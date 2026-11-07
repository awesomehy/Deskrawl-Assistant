"""Desktop launcher; runtime errors remain visible without a console."""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import traceback

from .paths import runtime_dir


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] == ['--steam-community-hosts']:
        # The elevated process exposes only a fixed hosts lease, never the
        # desktop server, updater, arbitrary commands or user-selected paths.
        from .steam_native_hosts import hosts_worker
        try:
            if len(values) != 5: raise ValueError()
            result = hosts_worker(int(values[1]), int(values[2]), values[3], values[4])
        except Exception: result = 1
        raise SystemExit(result)
    if values[:1] == ['--steam-community-proxy']:
        # Same-user watchdog can only lease the fixed Community proxy bypass.
        from .steam_native_proxy import proxy_worker
        try:
            if len(values) != 4: raise ValueError()
            result = proxy_worker(int(values[1]), int(values[2]), values[3])
        except Exception: result = 1
        raise SystemExit(result)
    parser = argparse.ArgumentParser(description='Deskrawl 装备助手')
    parser.add_argument('--port', type=int, default=18741)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--test-managed', action='store_true',
        help='启用尚未实机验收的托管动作，供本地测试；不会自动启动托管')
    args = parser.parse_args(values)
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError('本机页面端口必须在 1024 到 65535 之间。')
        if args.no_browser or args.browser:
            from .web_server import main as serve
            serve(port=args.port, open_browser=args.browser, test_managed=args.test_managed)
        else:
            from .native_window import main as desktop
            desktop(args.port, test_managed=args.test_managed)
    except Exception as exc:
        detail = traceback.format_exc()
        target = runtime_dir() / 'app-error.log'
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(detail, encoding='utf-8')
            message = f'装备助手无法启动：\n{exc}\n\n详细错误已记录到：\n{target}'
        except OSError:
            message = '装备助手无法启动，错误信息：\n'+detail
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, message, '装备助手无法启动', 0x10)
        raise SystemExit(1)
