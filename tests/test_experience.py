"""Measured rates, persistence, validation and real HTTP integration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.experience import ExperienceBook
from deskrawl_assistant.web_service import AssistantService
from test_web_server import HttpTests


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tick = 100.0
        self.book = ExperienceBook(Path(self.tmp.name)/'xp.sqlite3', clock=lambda:self.tick)

    def add(self, stage, xp, seconds, profile='角色A / 普通'):
        return self.book.add(dict(stage=stage, xp=xp, seconds=seconds, profile=profile))

    def test_weighted_total_rate_ranks_slow_high_reward_below_fast_stage(self):
        self.add('快关', 200, 20)
        self.add('慢关', 1000, 200)
        page=self.book.page()
        self.assertEqual([r['stage'] for r in page['rows']], ['快关','慢关'])
        self.assertEqual(page['rows'][0]['projected_xp'], 36000)
        self.assertEqual(page['rows'][1]['projected_xp'], 18000)
        self.add('快关', 100, 80)
        # Use 300/100, not the mean of per-run rates (10 + 1.25)/2.
        row=next(r for r in self.book.page()['rows'] if r['stage']=='快关')
        self.assertEqual(row['xp_per_hour'],10800)
        self.assertEqual(row['average_seconds'],50)

    def test_custom_horizon_and_complete_run_estimate_distinguish_partial_run(self):
        self.add('关卡', 1000, 2400)
        row=self.book.page(minutes=60)['rows'][0]
        self.assertEqual(row['projected_xp'],1500)
        self.assertEqual(row['completed_runs'],1)
        self.assertEqual(row['completed_runs_xp'],1000)
        row=self.book.page(minutes=10)['rows'][0]
        self.assertEqual(row['projected_xp'],250)
        self.assertEqual(row['completed_runs_xp'],0)

    def test_complete_run_mode_can_change_the_best_one_hour_stage(self):
        self.add('超时大关',2000,3700)
        self.add('可完成关卡',1000,1900)
        self.assertEqual(self.book.page()['rows'][0]['stage'],'超时大关')
        self.assertEqual(self.book.page(sort='completed')['rows'][0]['stage'],'可完成关卡')
        with self.assertRaises(ValueError): self.book.page(sort='invalid')

    def test_profiles_and_stages_never_merge(self):
        self.add('第一关',100,10,'甲')
        self.add('第一关',200,10,'乙')
        self.add('第二关',100,20,'甲')
        self.assertEqual(len(self.book.page()['rows']),3)
        self.assertEqual(len(self.book.page(profile='甲')['rows']),2)
        self.assertEqual(self.book.page()['profiles'],['乙','甲'])

    def test_empty_page_and_zero_xp_failure_are_valid(self):
        self.assertEqual(self.book.page()['rows'],[])
        self.add('失败循环',0,60)
        self.assertEqual(self.book.page()['rows'][0]['xp_per_hour'],0)

    def test_history_survives_restart_and_delete_removes_aggregate(self):
        sample=self.add('第一关',123,15)
        reopened=ExperienceBook(self.book.path)
        self.assertEqual(reopened.page()['rows'][0]['total_xp'],123)
        reopened.delete(sample['id'])
        self.assertEqual(self.book.page()['rows'],[])
        with self.assertRaises(ValueError): reopened.delete(sample['id'])

    def test_invalid_samples_do_not_write(self):
        base=dict(stage='关卡',xp=100,seconds=30,profile='甲')
        for field, value in [('xp',True),('xp',-1),('xp',1.2),('xp',float('nan')),('xp',10**400),
                             ('seconds',0),('seconds',float('inf')),('seconds',False),
                             ('stage','  '),('stage','a'*121),('profile','a'*121)]:
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                self.book.add(base|{field:value})
        self.assertEqual(self.book.page()['rows'],[])

    def test_invalid_horizons_and_ids_are_rejected(self):
        for value in (True,0,-1,float('nan'),float('inf'),50000):
            with self.subTest(value=value),self.assertRaises(ValueError): self.book.page(minutes=value)
        for value in (False,0,1.1,-1):
            with self.subTest(value=value),self.assertRaises(ValueError): self.book.delete(value)

    def test_timer_uses_monotonic_seconds_and_live_cumulative_xp(self):
        start=dict(available=True,total_xp=9999,character='A',session='1')
        active=self.book.start(dict(stage='第一关',profile='甲'),start)
        self.assertTrue(active['auto_xp'])
        self.tick+=60
        result=self.book.finish({}, start|{'total_xp':10299})
        self.assertEqual((result['xp'],result['seconds'],result['source']),(300,60,'live_timer'))
        self.assertIsNone(self.book.active_payload())

    def test_disconnect_character_or_session_change_preserves_timer(self):
        start=dict(available=True,total_xp=10,character='A',session='1')
        for end in ({'available':False},start|{'character':'B'},start|{'session':'2'},start|{'total_xp':1}):
            self.book.start(dict(stage='关卡'),start)
            self.tick+=10
            with self.assertRaises(ValueError):self.book.finish({},end)
            self.assertIsNotNone(self.book.active_payload())
            self.book.cancel()
        self.assertEqual(self.book.page()['rows'],[])

    def test_same_display_name_in_different_save_modes_is_not_same_character(self):
        live=dict(available=True,total_xp=100,character='同名角色',character_id='0:0:同名',session='same')
        self.book.start(dict(stage='关卡'),live)
        self.tick+=10
        with self.assertRaises(ValueError):self.book.finish({},live|{'character_id':'1:0:同名','total_xp':200})
        self.assertEqual(self.book.page()['rows'],[])

    def test_manual_timer_finish_and_duplicate_start(self):
        self.book.start(dict(stage='关卡'))
        with self.assertRaises(ValueError): self.book.start(dict(stage='其他关卡'))
        self.tick+=30
        with self.assertRaises(ValueError):self.book.finish({})
        self.assertIsNotNone(self.book.active_payload())
        result=self.book.finish({'xp':900})
        self.assertEqual(result['source'],'manual_timer')
        with self.assertRaises(ValueError):self.book.finish({'xp':900})

    def test_manual_correction_after_finish_error_preserves_original_end_time(self):
        self.book.start(dict(stage='关卡'))
        self.tick+=60
        with self.assertRaises(ValueError):self.book.finish({})
        self.tick+=30
        self.assertEqual(self.book.active_payload()['elapsed_seconds'],60)
        self.assertTrue(self.book.active_payload()['stopped'])
        result=self.book.finish({'xp':300})
        self.assertEqual(result['seconds'],60)

    def test_active_timer_is_not_resumed_after_app_restart(self):
        self.book.start(dict(stage='关卡'))
        self.assertIsNone(ExperienceBook(self.book.path).active_payload())


class ExperienceHttpTests(HttpTests):
    def setUp(self):
        super().setUp()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        class Client:
            connected=False
            def close(self):pass
        self.real=AssistantService(Client(),Path(self.tmp.name)/'rules.json')
        self.addCleanup(self.real.close)
        self.service.experience_page=self.real.experience_page
        self.service.experience_action=self.real.experience_action
        self.headers={'X-Assistant-Token':'test-session-token','Origin':self.url}

    def test_experience_crud_and_rate_through_http(self):
        code,_,body=self.request('/api/experience/sample',dict(stage='测试关卡',profile='甲',xp=100,seconds=20),self.headers)
        self.assertEqual(code,200)
        sample=json.loads(body)
        code,_,body=self.request('/api/experience?minutes=60')
        self.assertEqual(code,200)
        page=json.loads(body)
        self.assertEqual(page['rows'][0]['projected_xp'],18000)
        self.assertFalse(page['live']['available'])
        self.assertEqual(self.request('/api/experience/delete',{'id':sample['id']},self.headers)[0],200)
        self.assertEqual(json.loads(self.request('/api/experience')[2])['rows'],[])

    def test_default_auto_timer_profile_separates_live_characters(self):
        live={'available':True,'total_xp':100,'character':'角色甲','session':'test'}
        with patch.object(self.real,'experience_live',return_value=live):
            active=self.real.experience_action('start',{'stage':'关卡'})
        self.assertEqual(active['profile'],'角色甲')
        self.real.experience_action('cancel',{})

    def test_experience_mutation_uses_existing_origin_and_token_guards(self):
        sample=dict(stage='测试关卡',xp=100,seconds=20)
        self.assertEqual(self.request('/api/experience/sample',sample,{'Origin':self.url})[0],403)
        self.assertEqual(self.request('/api/experience/sample',sample,self.headers|{'Origin':'https://other.example'})[0],403)
        self.assertEqual(self.real.experience.page()['rows'],[])

    def test_invalid_experience_query_and_payload_return_400(self):
        for query in ('minutes=nan','minutes=0','minutes=inf','minutes=bad','sort=invalid','unknown=1','minutes=2&minutes=3'):
            with self.subTest(query=query): self.assertEqual(self.request('/api/experience?'+query)[0],400)
        self.assertEqual(self.request('/api/experience/sample',{'stage':'测试','xp':-1,'seconds':30},self.headers)[0],400)


if __name__=='__main__':unittest.main()
