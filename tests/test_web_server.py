"""Test loopback API behavior without interacting with the real game."""
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from deskrawl_assistant.web_server import make_handler


class FakeService:
    def __init__(self):self.actions=[]
    def start(self,kind,data):self.actions.append(kind)
    def catalog_payload(self):return {'equipment':[]}
    def state(self):return {'connected':False}


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

    def test_non_loopback_host_is_rejected(self):
        code,_,_=self.request('/api/state',headers={'Host':'other.example'})
        self.assertEqual(code,403)

    def test_path_traversal_cannot_serve_source_or_config(self):
        for path in ('/../web_service.py','/%2e%2e/web_service.py','/../../config/lock-rules.json'):
            code,_,_=self.request(path)
            self.assertEqual(code,404)


if __name__=='__main__':unittest.main()
