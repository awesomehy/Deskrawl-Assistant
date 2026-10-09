"""Contiguous game-observation calibration, including real failure accounting."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from deskrawl_assistant.recommendation_book import RecommendationBook, validate_settings


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.at = 0
        self.session = "process-A"
        self.book = RecommendationBook(Path(self.tmp.name)/'recommendations.sqlite3', clock=lambda:self.at, now=lambda:'2026-10-09')
        self.profile = {'character_id':'test-char','fingerprint':'gear-A','level':47,
                        'difficulty':'Normal','combat':{'normal_dps':100,'boss_dps':100}}

    def snap(self, run_id=None, phase='normal', **flags):
        run = {'id':run_id,'map_id':'SnowHill5' if run_id else '', 'difficulty':'Normal',
               'started':bool(run_id),'at_start':True,'phase':phase,'planned_normal_health':1000,
               'planned_boss_health':2000, 'fixed_seconds':5, **flags}
        return {'available':True, 'session':self.session, 'profile':deepcopy(self.profile), 'run':run}

    def observe(self, seconds, run_id=None, xp=0, phase='normal', **flags):
        self.at = seconds
        self.book.observe(self.snap(run_id,phase,**flags), {'available':True,'character_id':'test-char', 'session':self.session,'run_id':run_id,'total_xp':xp})

    def runs(self):
        return self.book.calibration(self.profile)['runs']

    def complete(self, run_id='run-A', outcome='success'):
        self.observe(0)
        self.observe(1,run_id,100)
        self.observe(4,run_id,200)
        self.observe(7,run_id,300,phase='boss')
        self.observe(10,run_id,400,phase='boss',**({'finished':True} if outcome=='success' else {'dead':True}))

    def test_passive_success_has_split_phases_and_measured_xp(self):
        self.complete()
        row = self.runs()[0]
        self.assertEqual(row['xp'],300)
        self.assertEqual(row['run_seconds'],9)
        self.assertEqual(row['normal_seconds'],6)
        self.assertEqual(row['boss_seconds'],3)
        self.assertEqual(row['outcome'],'success')
        self.assertIsNone(self.book.active)

    def test_first_attach_mid_run_is_never_full_run(self):
        self.observe(0,'run-A',200)
        self.observe(3,'run-A',400,finished=True)
        self.assertEqual(self.runs(),[])
        self.observe(4,'run-B',400)
        self.observe(7,'run-B',450,finished=True)
        self.assertEqual(len(self.runs()),1)
        self.assertEqual(self.runs()[0]['xp'],50)

    def test_new_id_already_in_combat_cannot_start_full_sample(self):
        self.observe(0)
        self.observe(1,'run-A',200,at_start=False)
        self.observe(3,'run-A',400,finished=True,at_start=False)
        self.assertEqual(self.runs(),[])

    def test_preparing_plan_transitions_to_start_with_same_id(self):
        self.observe(0,'run-A',started=False)
        self.observe(1,'run-A')
        self.observe(3,'run-A',100,finished=True)
        self.assertEqual(len(self.runs()),1)

    def test_gap_discards_run_instead_of_counting_unobserved_time(self):
        self.observe(0)
        self.observe(1,'run-A')
        self.observe(9,'run-A',200,finished=True)
        self.assertEqual(self.runs(),[])

    def test_character_change_discards_active_observation(self):
        self.observe(0)
        self.observe(1,'run-A')
        self.profile['character_id']='other-char'
        self.observe(3,'run-A',200,finished=True)
        self.assertEqual(self.runs(),[])

    def test_loadout_change_discards_active_observation(self):
        self.observe(0)
        self.observe(1,'run-A')
        self.profile['fingerprint']='gear-B'
        self.observe(3,'run-A',200,finished=True)
        self.assertEqual(self.runs(),[])

    def test_levelup_stays_contiguous_and_retains_start_level(self):
        self.observe(0)
        self.observe(1,'run-A',100)
        self.profile['level']=48
        self.observe(3,'run-A',1000,finished=True)
        self.assertEqual(self.runs()[0]['xp'],900)
        self.assertEqual(self.runs()[0]['level'],47)

    def test_failure_keeps_partial_xp_and_failure_duration(self):
        self.complete(outcome='failed')
        self.assertEqual(self.runs()[0]['outcome'],'failed')
        self.assertEqual(self.runs()[0]['xp'],300)
        self.assertEqual(self.runs()[0]['run_seconds'],9)

    def test_reused_id_in_new_process_is_a_distinct_persistent_run(self):
        self.complete()
        self.session = 'process-B'
        self.observe(11)
        self.observe(12,'run-A',0)
        self.observe(15,'run-A',100,finished=True)
        self.assertEqual(len(self.runs()),2)

    def test_xp_from_new_run_cannot_be_assigned_to_previous_terminal(self):
        self.observe(0)
        self.observe(1,'run-A',100)
        self.at = 3
        self.book.observe(self.snap('run-A',finished=True), {'available':True,'character_id':'test-char',
            'session':self.session,'run_id':'run-B','total_xp':500})
        self.assertIsNone(self.runs()[0]['xp'])

    def test_duplicate_terminal_snapshot_does_not_duplicate_record(self):
        self.complete()
        self.observe(12,'run-A',400,finished=True)
        self.assertEqual(len(self.runs()),1)

    def test_back_to_back_restart_records_real_overhead(self):
        self.complete()
        self.observe(13,'run-B',400)
        self.assertEqual(self.runs()[0]['overhead_seconds'],3)

    def test_unobserved_idle_does_not_become_restart_overhead(self):
        self.complete()
        self.observe(50,'run-B',400)
        self.assertIsNone(self.runs()[0]['overhead_seconds'])

    def test_missing_or_changed_xp_keeps_timing_without_invented_reward(self):
        self.observe(0)
        self.at=1
        self.book.observe(self.snap('run-A'))
        self.observe(3,'run-A',200,finished=True)
        self.assertIsNone(self.runs()[0]['xp'])

    def test_empty_enemy_count_is_not_completion(self):
        self.observe(0)
        self.observe(1,'run-A')
        self.observe(3,'run-A',200,enemy_count=0)
        self.assertEqual(self.runs(),[])
        self.assertIsNotNone(self.book.active)

    def test_settings_persist_without_importing_rule_data(self):
        value = {'minutes':30,'difficulty':'Normal','sort':'rate','overhead_seconds':0,'include_locked':False}
        self.book.save_settings(value,['Normal'])
        other=RecommendationBook(self.book.path)
        self.assertEqual(other.settings(['Normal']),value)

    def test_reset_excludes_records_and_preserves_recoverable_evidence(self):
        self.complete()
        self.book.reset(self.profile)
        self.assertEqual(self.runs(),[])
        with self.book._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM recommendation_runs WHERE excluded=1').fetchone()[0],1)

    def test_invalid_settings_rejected(self):
        for value in ({'minutes':True},{'minutes':float('nan')},{'minutes':-1},{'overhead_seconds':float('inf')},
                      {'difficulty':'Unknown'},{'sort':'bad'},{'include_locked':1},{'untrusted':1}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):validate_settings(value,['Normal'])


if __name__=='__main__':unittest.main()
