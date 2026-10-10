"""Local-only HTTP shell; serves the assistant UI and narrow application API."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, unquote, parse_qs
import webbrowser
from .web_service import AssistantService
from .action_log import record
from .version import VERSION
from .updater import UpdateManager, launch_helper

WEB = Path(__file__).resolve().parent / 'web'
PORT = 18741


def check_experience_sampling(service):
    book = getattr(service, 'experience', None)
    if book is not None and book.active_payload() is not None:
        raise ValueError('请先结束并保存或取消经验采样，再进行更新。')


def make_handler(service, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, data, status=200, content_type='application/json; charset=utf-8'):
            if isinstance(data,(dict,list)): data = json.dumps(data,ensure_ascii=False).encode('utf-8')
            elif isinstance(data,str): data = data.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store' if not self.path.startswith('/icons/') else 'private, max-age=86400')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.valid_host(): return self.reply({'error':'仅限本机访问。'},403)
            path = unquote(urlsplit(self.path).path)
            try:
                if path=='/api/ping': return self.reply({'app':'deskrawl-local-assistant','version':3,
                    'app_version':VERSION,'pid':os.getpid(),'desktop':getattr(self.server,'desktop',None) is not None})
                if path=='/api/update':
                    updater = getattr(self.server,'updater',None)
                    value = updater.snapshot() if updater else {'phase':'unsupported','has_update':False,'can_install':False,
                        'message':'当前启动方式不支持自动更新。','current_version':VERSION}
                    if getattr(self.server,'desktop',None) is None: value['can_install'] = False
                    return self.reply(value)
                if path=='/api/session': return self.reply({'token':token})
                if path=='/api/catalog': return self.reply(service.catalog_payload())
                if path=='/api/state': return self.reply(service.state())
                if path=='/api/logs':
                    query = parse_qs(urlsplit(self.path).query)
                    allowed = {'category','query','before','limit'}
                    if set(query)-allowed or any(len(v)!=1 for v in query.values()):
                        raise ValueError('日志查询参数无效。')
                    params = {k:v[0] for k,v in query.items()}
                    for field in ('before','limit'):
                        if field in params: params[field] = int(params[field])
                    return self.reply(service.log_page(**params))
                if path=='/api/experience':
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if set(query)-{'minutes','profile','sort'} or any(len(v)!=1 for v in query.values()):
                        raise ValueError('经验查询参数无效。')
                    params = {k:v[0] for k,v in query.items()}
                    if 'minutes' in params: params['minutes'] = float(params['minutes'])
                    return self.reply(service.experience_page(**params))
                if path=='/api/recommendations':
                    if urlsplit(self.path).query:
                        raise ValueError('刷图推荐不接受额外查询参数。')
                    return self.reply(service.recommendation_page())
                if path=='/api/export/rules': return self.reply(service.export_rules())
                if path=='/api/export/items':
                    with service.guard: value = service.snapshot
                    service.journal.append('system','items_exported','导出当前背包与仓库清单。')
                    return self.reply(value or {'items':[]})
                if path=='/':
                    body = (WEB / 'index.html').read_text(encoding='utf-8').replace('__SESSION_TOKEN__',token).replace('__APP_VERSION__',VERSION)
                    return self.reply(body,content_type='text/html; charset=utf-8')
                file = (WEB / path.lstrip('/')).resolve()
                if WEB.resolve() not in file.parents or not file.is_file(): return self.reply({'error':'页面不存在。'},404)
                if file.suffix not in ('.css','.js','.png','.svg','.ico'): return self.reply({'error':'页面不存在。'},404)
                mime = {'.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8',
                        '.svg':'image/svg+xml','.png':'image/png','.ico':'image/x-icon'}.get(file.suffix)
                return self.reply(file.read_bytes(),content_type=mime)
            except (ValueError,TypeError) as exc: return self.reply({'error':str(exc)},400)
            except Exception as exc: return self.reply({'error':str(exc)},500)

        def do_POST(self):
            expected_origin = f'http://127.0.0.1:{self.server.server_port}'
            origin = self.headers.get('Origin',expected_origin)
            # The desktop embedded browser can rewrite its human-triggered
            # request Origin to the loopback host without the ephemeral port.
            # Require the exact bound Host and session header in every case.
            if (not self.valid_host() or origin not in (expected_origin, 'http://127.0.0.1', 'null')):
                record('web_source_rejected', origin=origin, path=urlsplit(self.path).path)
                return self.reply({'error':'操作来源无效，请重新打开助手页面。'},403)
            if self.headers.get('X-Assistant-Token')!=token:
                return self.reply({'error':'助手已更新，页面连接需要刷新。','session_expired':True},403)
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 0<=size<=1024*1024: raise ValueError('请求过大。')
                data = json.loads(self.rfile.read(size) or b'{}')
                if not isinstance(data,dict): raise ValueError('请求格式无效。')
                path = urlsplit(self.path).path
                if getattr(service,'updating',False) is True and path not in ('/api/stop','/api/shutdown'):
                    raise ValueError('正在替换程序，请稍候。')
                if path=='/api/action': service.start(data.get('kind'),data)
                elif path in ('/api/update/check','/api/update/install'):
                    updater = getattr(self.server,'updater',None)
                    if updater is None: raise ValueError('当前启动方式不支持更新。')
                    if path.endswith('/check'): updater.check()
                    else:
                        desktop = getattr(self.server,'desktop',None)
                        if desktop is None: raise ValueError('请在 exe 独立窗口中更新。')
                        if data.get('saved') is not True or desktop.dirty:
                            raise ValueError('请先保存规则和整理设置，再进行更新。')
                        check_experience_sampling(service)
                        updater.install()
                elif path=='/api/window/draft':
                    desktop = getattr(self.server,'desktop',None)
                    if desktop is None: raise ValueError('当前助手没有独立窗口。')
                    desktop.set_dirty(data.get('dirty'))
                elif path=='/api/window/activate':
                    desktop = getattr(self.server,'desktop',None)
                    if desktop is None: raise ValueError('当前助手没有独立窗口。')
                    desktop.activate()
                elif path in {'/api/experience/sample','/api/experience/delete','/api/experience/start','/api/experience/finish','/api/experience/cancel'}:
                    return self.reply(service.experience_action(path.rsplit('/',1)[-1], data))
                elif path=='/api/recommendations/settings':
                    return self.reply(service.recommendation_settings(data))
                elif path=='/api/recommendations/calibration/reset':
                    return self.reply(service.recommendation_reset())
                elif path=='/api/rule/save': return self.reply({'rule':service.save_rule(data)})
                elif path=='/api/rule/delete':
                    return self.reply({'deleted':service.delete_rules(data.get('ids',[data.get('id')]),data.get('equipment_key'))})
                elif path=='/api/rule/import':
                    return self.reply({'imported':service.import_rules(data.get('payload'),data.get('enabled',False))})
                elif path=='/api/rule/copy':
                    return self.reply(service.copy_rules(data.get('source_key'),data.get('target_key'),
                        data.get('rule_ids'),data.get('replace_existing',False),data.get('expected_target_ids')))
                elif path=='/api/monitor': service.set_monitoring(data.get('enabled'))
                elif path=='/api/automation/settings': return self.reply({'settings':service.save_automation(data.get('settings'))})
                elif path=='/api/automation/run': service.set_automation_running(data.get('enabled'))
                elif path=='/api/stop': service.stop()
                elif path=='/api/shutdown':
                    service.stop()
                    threading.Thread(target=self.server.shutdown,daemon=True).start()
                else: return self.reply({'error':'操作不存在。'},404)
                return self.reply({'ok':True})
            except (ValueError,TypeError,KeyError) as exc: return self.reply({'error':str(exc)},400)
            except Exception as exc: return self.reply({'error':str(exc)},500)
    return Handler


def existing_service(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/ping',timeout=1) as response:
            result = json.load(response)
            return result if result.get('app')=='deskrawl-local-assistant' else None
    except (OSError,ValueError):
        return None


class AssistantServer:
    """A stoppable server owned by the desktop window, without a browser launch."""
    def __init__(self, port=PORT, service=None, updater=None):
        self.service = service or AssistantService()
        self.finished = threading.Event()
        self.thread = None
        try:
            self.server = ThreadingHTTPServer(('127.0.0.1',port),make_handler(self.service,secrets.token_hex(24)))
            self.server.daemon_threads = True
            self.updater = updater or UpdateManager(prepare=self.prepare_update,launch=self.install_update,
                abort=self.abort_update,port=self.server.server_port)
            self.server.updater = self.updater
        except Exception:
            self.service.close()
            raise

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/'

    def start(self):
        if self.thread is not None: raise RuntimeError('服务已经启动。')
        def run():
            try: self.server.serve_forever(poll_interval=.1)
            finally:
                try: self.service.close()
                finally:
                    self.server.server_close()
                    self.finished.set()
        self.thread = threading.Thread(target=run,daemon=True)
        self.thread.start()
        self.updater.start()

    def prepare_update(self):
        desktop = getattr(self.server,'desktop',None)
        if desktop is None or desktop.dirty:
            raise ValueError('请保存未完成的配置后再更新。')
        with self.service.guard:
            check_experience_sampling(self.service)
            self.service.updating = True
        self.service.stop()
        worker = self.service.worker
        if worker and worker is not threading.current_thread(): worker.join(20)
        if worker and worker.is_alive():
            self.abort_update()
            raise ValueError('当前装备操作尚未结束，请稍后再试。')
        if desktop.dirty:
            self.abort_update()
            raise ValueError('下载期间有新的未保存改动，请先保存。')
        self.service.journal.append('system','update_installing','更新文件已校验，正在退出并替换程序。')

    def abort_update(self):
        with self.service.guard: self.service.updating = False

    def install_update(self, plan):
        launch_helper(plan)
        threading.Thread(target=self.server.shutdown,daemon=True).start()

    def close(self):
        self.updater.close()
        self.service.stop()
        if self.thread is not None:
            if not self.finished.is_set(): self.server.shutdown()
            self.thread.join()
        else:
            self.service.close()
            self.server.server_close()
            self.finished.set()


def main(*, port=PORT, open_browser=True):
    # Fixed loopback port allows a second launcher click to reuse the same
    # service instead of starting competing equipment writers.
    url = f'http://127.0.0.1:{port}/'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url+'api/ping',timeout=1) as response:
            if json.load(response).get('app')=='deskrawl-local-assistant':
                if open_browser: webbrowser.open(url)
                return
    except (OSError,ValueError): pass
    host = AssistantServer(port)
    try:
        host.start()
        if open_browser: webbrowser.open(url)
        host.finished.wait()
    finally:
        host.close()


if __name__=='__main__': main()
