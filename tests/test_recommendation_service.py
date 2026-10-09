"""Service integration with the real map model and a read-only fake client."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from deskrawl_assistant.web_service import AssistantService
from deskrawl_assistant.recommendation_book import RecommendationBook


class Client:
    connected=False
    pid=999
    def __init__(self):
        self.profile={'character_id':'qa-role','character':'测试武僧','class':'Monk','fingerprint':'qa-gear',
            'level':47,'difficulty':'Normal','xp_gain_multiplier':2.322,
            'unlocked_maps':{'Normal':['Forest1','SnowHill5']},'entry_items':{},'is_demo':False,
            'combat':{'damage':400,'attack_speed':1.5,'crit_chance':0.2,'crit_multiplier':1.8,'move_speed':5}}
        self.run={'id':None,'map_id':None,'started':False,'at_start':False,'finished':False,'dead':False}
        self.xp=100
    def recommendation_snapshot(self):
        return {'available':True,'session':'qa-process','profile':deepcopy(self.profile),'run':deepcopy(self.run),'warnings':[]}
    def experience_snapshot(self):
        return {'available':True,'session':'qa-process','character_id':'qa-role','total_xp':self.xp,'run_id':self.run['id']}
    def snapshot(self):
        return {'complete':True,'containers':{}}
    def close(self):self.connected=False


class RecommendationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client=Client()
        self.service=AssistantService(self.client,Path(self.tmp.name)/'rules.json')
        self.addCleanup(self.service.close)
        self.at=0
        self.service.recommendations=RecommendationBook(Path(self.tmp.name)/'recommendations.sqlite3',clock=lambda:self.at)

    def refresh(self):
        self.client.connected=True
        self.service._observe_recommendation()
        return self.service.recommendation_page()

    def test_unconnected_page_contains_settings_and_clear_state(self):
        page=self.service.recommendation_page()
        self.assertFalse(page['available'])
        self.assertEqual(page['rows'],[])
        self.assertEqual(page['minutes'],60)
        self.assertEqual(page['difficulty_options'][0]['value'],'current')
        self.assertIn('连接',page['error'])

    def test_real_model_fills_current_role_rows_and_eligible_best(self):
        page=self.refresh()
        self.assertTrue(page['available'])
        self.assertTrue(page['best']['accessible'])
        self.assertEqual(page['character']['level'],47)
        self.assertGreater(len(page['rows']),60)
        self.assertIsNone(page['current'])
        json.dumps(page,allow_nan=False)

    def test_current_positive_plan_is_distinct_from_repeat_expectation(self):
        self.client.run.update(id='qa-run',map_id='SnowHill5',difficulty='Normal',phase='normal',
            planned_xp=149430,planned_normal_health=8000,planned_boss_health=0,started=True)
        page=self.refresh()
        self.assertEqual(page['current']['planned_xp'],149430)
        row=next(r for r in page['rows'] if r['map_id']=='SnowHill5')
        self.assertNotEqual(row['xp_per_run'],page['current']['planned_xp'])
        self.assertNotEqual(page['current']['stage'],'SnowHill5')
        self.assertEqual(page['current']['phase'],'普通波')

    def test_settings_change_budget_and_survive_new_book(self):
        settings={'minutes':30,'difficulty':'Normal','overhead_seconds':7,'include_locked':False,'sort':'completed'}
        self.service.recommendation_settings(settings)
        page=self.refresh()
        self.assertEqual(page['settings'],settings)
        self.assertEqual(page['minutes'],30)
        self.assertTrue(all(r['accessible'] is not False for r in page['rows']))
        other=RecommendationBook(self.service.recommendations.path)
        self.assertEqual(other.settings(['Normal','Nightmare','Inferno']),settings)

    def test_foreign_difficulty_never_uses_current_difficulty_unlock_list(self):
        self.service.recommendation_settings({'difficulty':'Nightmare'})
        page=self.refresh()
        self.assertTrue(page['available'])
        self.assertIsNone(page['best'])
        self.assertTrue(all(r['accessible'] is not True for r in page['rows']))

    def test_passive_run_uses_phase_waits_and_real_reward(self):
        self.refresh()
        self.at=1
        self.client.run.update(id='qa-run',map_id='SnowHill5',difficulty='Normal',phase='normal',started=True,at_start=True,
            planned_normal_health=8000,planned_boss_health=0,normal_waves=10,boss_waves=0,wave_count=10)
        self.refresh()
        self.at=4
        self.client.xp=200
        self.client.run.update(finished=True,at_start=False)
        page=self.refresh()
        self.assertEqual(page['calibration']['samples'],1)
        row=self.service.recommendations.calibration(self.client.profile)['runs'][0]
        self.assertEqual(row['xp'],100)
        self.assertEqual(row['family'],'SnowHill')
        self.assertGreater(row['normal_fixed_seconds'],0)
        self.assertEqual(row['boss_fixed_seconds'],0)
        self.assertEqual(row['combat']['xp_gain_multiplier'],2.322)

    def test_current_role_calibration_reset_is_recoverable(self):
        self.test_passive_run_uses_phase_waits_and_real_reward()
        self.service.recommendation_reset()
        self.assertEqual(self.service.recommendation_page()['calibration']['samples'],0)
        with self.service.recommendations._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM recommendation_runs').fetchone()[0],1)

    def test_bad_settings_are_not_persisted(self):
        with self.assertRaises(ValueError):self.service.recommendation_settings({'minutes':float('nan')})
        self.assertEqual(self.service.recommendation_page()['settings']['minutes'],60)


if __name__=='__main__':unittest.main()
