"""Test loopback API behavior without interacting with the real game."""
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock
from deskrawl_assistant.web_server import make_handler


class FakeService:
    def __init__(self):self.actions=[]
    def start(self,kind,data):self.actions.append(kind)
    def catalog_payload(self):return {'equipment':[]}
    def state(self):return {'connected':False}
    def log_page(self,**filters):
        from deskrawl_assistant.activity_journal import CATEGORIES
        if filters.get('category') and filters['category'] not in CATEGORIES:raise ValueError('日志分类无效')
        if not 1<=filters.get('limit',50)<=200:raise ValueError('每页日志数量无效')
        return {'entries':[],'filters':filters}


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.log=patch('deskrawl_assistant.web_server.record')
        self.log.start();self.addCleanup(self.log.stop)
        self.service=FakeService()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.service,'test-session-token'))
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.addCleanup(self.finish)
    def finish(self):self.server.shutdown();self.server.server_close();self.thread.join(1)
    def request(self,path,data=None,headers=None):
        request=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers or {})
        try:r=urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:r=error
        with r:return r.status,r.headers,r.read()

    def test_javascript_mime_is_independent_of_windows_registry(self):
        code,headers,body=self.request('/app.js')
        self.assertEqual(code,200)
        self.assertEqual(headers['Content-Type'],'text/javascript; charset=utf-8')
        self.assertIn(b'async function api',body)

    def test_update_actions_require_session_and_never_accept_unsaved_drafts(self):
        updater=Mock();self.server.updater=updater
        self.server.desktop=SimpleNamespace(dirty=False)
        for headers in ({'Origin':self.url},{'Origin':'https://other.example','X-Assistant-Token':'test-session-token'}):
            code,_,_=self.request('/api/update/install',{'saved':True},headers)
            self.assertEqual(code,403)
        headers={'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        code,_,_=self.request('/api/update/install',{},headers);self.assertEqual(code,400)
        self.server.desktop.dirty=True
        code,_,_=self.request('/api/update/install',{'saved':True},headers);self.assertEqual(code,400)
        updater.install.assert_not_called()
        self.server.desktop.dirty=False
        code,_,_=self.request('/api/update/install',{'saved':True},headers);self.assertEqual(code,200)
        updater.install.assert_called_once_with()

    def test_browser_can_check_but_cannot_install_or_choose_arbitrary_executable(self):
        updater=Mock();self.server.updater=updater
        headers={'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        code,_,_=self.request('/api/update/check',{},headers);self.assertEqual(code,200)
        updater.check.assert_called_once_with()
        code,_,_=self.request('/api/update/install',{'saved':True,'target':'C:/other.exe'},headers)
        self.assertEqual(code,400);updater.install.assert_not_called()

    def test_stale_session_recovery_does_not_execute_rejected_action(self):
        code,_,body=self.request('/api/action',{'kind':'connect'},{'X-Assistant-Token':'old-token'})
        self.assertEqual(code,403)
        self.assertTrue(json.loads(body)['session_expired'])
        self.assertFalse(self.service.actions)
        _,_,body=self.request('/api/session')
        recovered=json.loads(body)['token']
        code,_,_=self.request('/api/action',{'kind':'connect'},{'X-Assistant-Token':recovered,'Origin':self.url})
        self.assertEqual(code,200)
        self.assertEqual(self.service.actions,['connect'])

    def test_cross_origin_cannot_mutate_even_with_valid_token(self):
        code,_,body=self.request('/api/action',{'kind':'unlock_all'},{'X-Assistant-Token':'test-session-token','Origin':'https://other.example'})
        self.assertEqual(code,403)
        self.assertFalse(self.service.actions)

    def test_sandboxed_browser_origin_can_send_authenticated_unlock(self):
        code,_,_=self.request('/api/action',{'kind':'unlock_all'},{'X-Assistant-Token':'test-session-token','Origin':'null'})
        self.assertEqual(code,200)
        self.assertEqual(self.service.actions,['unlock_all'])

    def test_desktop_browser_portless_loopback_origin_can_unlock(self):
        code,_,_=self.request('/api/action',{'kind':'unlock_all'},{'X-Assistant-Token':'test-session-token','Origin':'http://127.0.0.1'})
        self.assertEqual(code,200)
        self.assertEqual(self.service.actions,['unlock_all'])

    def test_sandboxed_origin_without_session_token_cannot_write(self):
        code,_,_=self.request('/api/action',{'kind':'unlock_all'},{'Origin':'null'})
        self.assertEqual(code,403)
        self.assertFalse(self.service.actions)

    def test_rule_import_forwards_enabled_choice_and_keeps_disabled_default(self):
        self.service.import_rules=Mock(return_value=2)
        headers={'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        payload={'version':2,'rules':[]}
        code,_,body=self.request('/api/rule/import',{'payload':payload,'enabled':True},headers)
        self.assertEqual(code,200);self.assertEqual(json.loads(body)['imported'],2)
        self.service.import_rules.assert_called_with(payload,True)
        self.request('/api/rule/import',{'payload':payload},headers)
        self.service.import_rules.assert_called_with(payload,False)

    def test_rule_copy_and_scoped_bulk_delete_require_session_and_forward_exact_selection(self):
        self.service.copy_rules=Mock(return_value={'copied':1})
        self.service.delete_rules=Mock(return_value=1)
        headers={'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        data={'source_key':'RingA','target_key':'RingB','rule_ids':['a'],'replace_existing':True,'expected_target_ids':['b']}
        self.assertEqual(self.request('/api/rule/copy',data)[0],403)
        self.service.copy_rules.assert_not_called()
        self.assertEqual(self.request('/api/rule/copy',data,headers)[0],200)
        self.service.copy_rules.assert_called_once_with('RingA','RingB',['a'],True,['b'])
        self.assertEqual(self.request('/api/rule/delete',{'ids':['a'],'equipment_key':'RingA'},headers)[0],200)
        self.service.delete_rules.assert_called_once_with(['a'],'RingA')

    def test_non_loopback_host_is_rejected(self):
        code,_,_=self.request('/api/state',headers={'Host':'other.example'})
        self.assertEqual(code,403)

    def test_path_traversal_cannot_serve_source_or_config(self):
        for path in ('/../web_service.py','/%2e%2e/web_service.py','/../../config/lock-rules.json'):
            code,_,_=self.request(path)
            self.assertEqual(code,404)

    def test_log_query_preserves_chinese_filter_cursor_and_limit(self):
        code,_,body=self.request('/api/logs?category=transfer&query=%E5%AE%9D%E7%9F%B3&before=123&limit=20')
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['filters'],dict(category='transfer',query='宝石',before=123,limit=20))

    def test_invalid_log_filters_are_client_errors(self):
        for query in ('limit=bad','limit=10000','category=invalid','unknown=1','limit=2&limit=3'):
            with self.subTest(query=query):self.assertEqual(self.request('/api/logs?'+query)[0],400)


if __name__=='__main__':unittest.main()
