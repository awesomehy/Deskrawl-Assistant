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
    def game_action(self,kind,data):self.actions.append(('game',kind,data));return {'status':'requested'}
    def start(self,kind,data):self.actions.append(kind)
    def recommendation_page(self):
        return {'available':False,'rows':[],'error':'尚未连接角色'}
    def loot_preview_page(self):
        return {'available':False,'phase':'waiting','run':None,'waves':[]}
    def set_loot_capture(self, enabled):
        if type(enabled) is not bool: raise ValueError('开关必须为布尔值')
        self.actions.append('loot-capture-'+str(enabled))
        return {'enabled':enabled}
    def recommendation_settings(self,data):
        from deskrawl_assistant.recommendation_book import validate_settings
        return {'settings':validate_settings(data.get('settings',data),['Normal','Nightmare','Inferno'])}
    def recommendation_reset(self):
        self.actions.append('recommendation-reset')
        return {'ok':True}
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

    def test_local_steam_avatar_is_versioned_no_store_and_never_a_file_lookup(self):
        from deskrawl_assistant.steam_client_profile import ClientIdentity, SteamClientProfile
        from tests.test_steam_client_profile import png
        from pathlib import Path
        backend=Mock()
        backend.identity.return_value=ClientIdentity('online',42,123,7,Path('fake-steam'))
        backend.profile.return_value=('昵称',png())
        self.service.steam_client_profile=SteamClientProfile(backend)
        url=self.service.steam_client_profile.state()['avatar_url']
        code,headers,body=self.request(url)
        self.assertEqual((code,headers['Content-Type'],headers['Cache-Control'],body),
                         (200,'image/png','no-store',png()))
        for query in ('', '?v=../../config/loginusers.vdf', '?v='+'b'*64, '?v='+'a'*64+'&v='+'b'*64, '?path=secret'):
            self.assertEqual(self.request('/api/steam-client/avatar'+query)[0],404)
        backend.profile.assert_called_once()

    def test_game_process_actions_require_valid_origin_and_session(self):
        for endpoint,payload in (('start',{}),('close',{'confirmed':True,'target':{'pid':42,'instance':'42:1'}})):
            for headers in ({'Origin':self.url},{'Origin':'https://other.example','X-Assistant-Token':'test-session-token'}):
                self.assertEqual(self.request('/api/game/'+endpoint,payload,headers)[0],403)
            self.assertFalse(self.service.actions)
            headers={'Origin':self.url,'X-Assistant-Token':'test-session-token'}
            self.assertEqual(self.request('/api/game/'+endpoint,payload,headers)[0],200)
            self.assertEqual(self.service.actions.pop(),('game',endpoint,payload))

    def test_retired_loot_capture_endpoint_cannot_enable_local_archiving(self):
        for headers in ({'Origin':self.url},{'Origin':'https://other.example','X-Assistant-Token':'test-session-token'}):
            code,_,_=self.request('/api/loot-capture',{'enabled':True},headers)
            self.assertEqual(code,403)
        headers={'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        for enabled in (True, False, 'true'):
            code,_,_=self.request('/api/loot-capture',{'enabled':enabled},headers)
            self.assertEqual(code,404)
        self.assertFalse(self.service.actions)

    def test_main_page_renders_current_version_and_session(self):
        from deskrawl_assistant.version import VERSION
        code,_,body=self.request('/')
        self.assertEqual(code,200)
        self.assertNotIn(b'__APP_VERSION__',body)
        self.assertNotIn(b'__SESSION_TOKEN__',body)
        self.assertIn(('v'+VERSION).encode(),body)
        self.assertIn(b'test-session-token',body)
        self.assertNotIn(b'loot-capture',body)

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

    def test_disabled_experience_samples_do_not_block_update(self):
        updater=Mock();self.server.updater=updater
        self.server.desktop=SimpleNamespace(dirty=False)
        book=Mock();self.service.experience=book
        headers={'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        with patch('deskrawl_assistant.features.REPORTING_ENABLED',False):
            for active in ({'stage':'test','stopped':False},{'stage':'test','stopped':True}):
                book.active_payload.return_value=active
                self.assertEqual(self.request('/api/update/install',{'saved':True},headers)[0],200)
        book.active_payload.assert_not_called()
        self.assertEqual(updater.install.call_count,2)

    def test_update_preparation_rechecks_sample_started_during_download(self):
        from deskrawl_assistant.web_server import AssistantServer
        self.service.guard=threading.RLock()
        self.service.updating=False
        self.service.experience=Mock()
        self.service.experience.active_payload.return_value={'stage':'new sample'}
        owner=SimpleNamespace(server=SimpleNamespace(desktop=SimpleNamespace(dirty=False)),service=self.service)
        # Legacy behavior remains tested offline, but is absent in normal use.
        with patch('deskrawl_assistant.features.REPORTING_ENABLED',True):
            with self.assertRaisesRegex(ValueError,'经验采样'):
                AssistantServer.prepare_update(owner)
        self.assertFalse(self.service.updating)

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

    def test_managed_endpoints_require_session_and_never_forward_raw_native_actions(self):
        self.service.managed_page=Mock(return_value={'running':False,'capabilities':{}})
        self.service.save_managed=Mock()
        self.service.set_managed_running=Mock(return_value={'running':False})
        self.service.stop_managed=Mock(return_value={'running':False})
        self.service.finish_managed=Mock(return_value={'running':True,'finishing':True})
        self.service.reset_managed=Mock(return_value={'running':False})
        actions=[('/api/managed/settings',{'settings':{'offline':'settings'}},self.service.save_managed),
                 ('/api/managed/run',{'enabled':True},self.service.set_managed_running),
                 ('/api/managed/stop',{},self.service.finish_managed),
                 ('/api/managed/abort',{},self.service.stop_managed),
                 ('/api/managed/reset',{},self.service.reset_managed)]
        self.assertEqual(self.request('/api/managed')[0],200)
        for endpoint,payload,method in actions:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.request(endpoint,payload)[0],403)
                self.assertEqual(self.request(endpoint,payload,{'Origin':'https://other.example',
                    'X-Assistant-Token':'test-session-token'})[0],403)
                method.assert_not_called()
                self.assertEqual(self.request(endpoint,payload,{'Origin':self.url,
                    'X-Assistant-Token':'test-session-token'})[0],200)
                method.assert_called_once()
        self.service.save_managed.assert_called_once_with({'offline':'settings'})
        self.service.set_managed_running.assert_called_once_with(True)
        for endpoint in ('/api/managed/action','/api/managed/native','/api/managed/verify'):
            self.assertEqual(self.request(endpoint,{'address':123,'allow_unverified':True},
                {'Origin':self.url,'X-Assistant-Token':'test-session-token'})[0],404)
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

    def test_retired_report_routes_reject_reads_without_calling_the_service(self):
        self.service.recommendation_page=Mock(side_effect=AssertionError('report read'))
        self.service.experience_page=Mock(side_effect=AssertionError('experience read'))
        with patch('deskrawl_assistant.features.REPORTING_ENABLED',False):
            for path in ('/api/recommendations','/api/recommendations?minutes=bad',
                         '/api/experience','/api/experience?minutes=60'):
                code,_,body=self.request(path)
                self.assertEqual(code,410)
                self.assertTrue(json.loads(body)['disabled'])
        self.service.recommendation_page.assert_not_called()
        self.service.experience_page.assert_not_called()

    def test_loot_preview_is_uncached_read_only_and_rejects_parameters(self):
        code, headers, body = self.request('/api/loot-preview')
        self.assertEqual(code,200)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIsNone(json.loads(body)['run'])
        for query in ('run=other','pid=24572','wave=1'):
            self.assertEqual(self.request('/api/loot-preview?'+query)[0],400)
        headers = {'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        self.assertEqual(self.request('/api/loot-preview',{},headers)[0],404)
        self.assertFalse(self.service.actions)

    def test_recommendation_mutations_require_session_and_validate_values(self):
        enabled=patch('deskrawl_assistant.features.REPORTING_ENABLED',True)
        enabled.start();self.addCleanup(enabled.stop)
        endpoint='/api/recommendations/settings'
        self.assertEqual(self.request(endpoint,{'minutes':30})[0],403)
        headers={'X-Assistant-Token':'test-session-token','Origin':self.url}
        code,_,body=self.request(endpoint,{'minutes':30},headers)
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['settings']['minutes'],30)
        for value in ({'minutes':float('nan')},{'difficulty':'Other'},{'include_locked':1},{'sort':'bad'}):
            self.assertEqual(self.request(endpoint,value,headers)[0],400)
        self.assertEqual(self.request('/api/recommendations/calibration/reset',{})[0],403)
        self.assertFalse(self.service.actions)
        self.assertEqual(self.request('/api/recommendations/calibration/reset',{},headers)[0],200)
        self.assertEqual(self.service.actions,['recommendation-reset'])

    def test_retired_report_mutations_cannot_restart_sampling_or_change_saved_data(self):
        headers={'X-Assistant-Token':'test-session-token','Origin':self.url}
        self.service.experience_action=Mock(side_effect=AssertionError('experience action'))
        with patch('deskrawl_assistant.features.REPORTING_ENABLED',False):
            for path in ('/api/experience/start','/api/experience/finish','/api/experience/sample',
                         '/api/experience/delete','/api/experience/cancel',
                         '/api/recommendations/settings','/api/recommendations/calibration/reset'):
                self.assertEqual(self.request(path,{},headers)[0],410)
        self.service.experience_action.assert_not_called()
        self.assertEqual(self.service.actions,[])

    def test_invalid_log_filters_are_client_errors(self):
        for query in ('limit=bad','limit=10000','category=invalid','unknown=1','limit=2&limit=3'):
            with self.subTest(query=query):self.assertEqual(self.request('/api/logs?'+query)[0],400)


if __name__=='__main__':unittest.main()
