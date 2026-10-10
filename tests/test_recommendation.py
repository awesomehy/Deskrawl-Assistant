"""Native reward goldens, shipped resources and current-profile ranking."""
import copy
import math
import unittest

from deskrawl_assistant.recommendation import (
    baseline_enemy_xp, enemy_xp, enemy_health, estimate_run, level_growth,
    load_recommendation_catalog, map_access, profile_effective_combat,
    recommend_maps,
)


CATALOG = load_recommendation_catalog()


def profile(**overrides):
    result = dict(level=47, xp_gain_multiplier=2.322, fingerprint='same-build',
                  difficulty='Normal', combat={'normal_dps': 1000, 'boss_dps': 500},
                  unlocked_maps={'Normal': [m['map_id'] for m in CATALOG['maps']]},
                  entry_items={r['item_id']: 10000 for m in CATALOG['maps'] for r in m['requirements']},
                  is_demo=False)
    result.update(overrides)
    return result


def tiny_catalog(maps=None):
    catalog = copy.deepcopy(CATALOG)
    catalog['config']['health_growth'] = [0, 0, 0]
    catalog['config']['damage_growth'] = [0, 0, 0]
    catalog['enemies'] = {'test': {'base_health': 100, 'base_damage': 10}}
    encounter = {'weight': 1, 'enemies': [{'enemy_id': 'test', 'count': 1, 'row': 0}]}
    stage = dict(map_id='SnowHill1', stage='test', family='SnowHill', min_level=1, max_level=1,
                 waves=1, boss=False, elite_chance=0, normal_pool=[encounter],
                 boss_encounter={'weight': 0, 'enemies': []}, requirements=[],
                 prerequisite=None, supported=True, unavailable=False, locked_in_demo=False)
    catalog['maps'] = [dict(stage, **changes) for changes in (maps or [{}])]
    return catalog


def tiny_profile(**overrides):
    values = dict(level=1, xp_gain_multiplier=1, combat={'normal_dps': 10, 'boss_dps': 10})
    values.update(overrides)
    return profile(**values)


class RewardFormulaTests(unittest.TestCase):
    def test_verified_current_plan_enemy_goldens(self):
        config = CATALOG['config']
        self.assertEqual(baseline_enemy_xp(300, 57, config), 1980)
        self.assertEqual(baseline_enemy_xp(350, 57, config), 2310)
        self.assertEqual(enemy_xp(300, 57, 47, 2.322, 1, config), 4598)
        self.assertEqual(enemy_xp(350, 57, 47, 2.322, 1, config), 5364)

    def test_positive_plan_reward_is_not_multiplied_again(self):
        self.assertEqual(enemy_xp(300, 57, 70, 100, 2, CATALOG['config'], planned_xp=4598), 4598)
        for planned in (0, -1, None, True):
            self.assertEqual(enemy_xp(300, 57, 47, 2.322, 1, CATALOG['config'], planned_xp=planned), 4598)

    def test_level_penalty_zero_still_awards_one_and_rounds_even(self):
        config = CATALOG['config']
        self.assertEqual(enemy_xp(100, 1, 7, 1, 1, config), 1)
        self.assertEqual(enemy_xp(100, 1, 70, 50, 2, config), 1)
        self.assertEqual(enemy_xp(1, 1, 1, 2.5, 1, config), 2)
        self.assertEqual(enemy_xp(1, 1, 1, 3.5, 1, config), 4)

    def test_growth_boundaries_and_elite_health(self):
        rates = [.12, .08, .06]
        # The input rates are narrowed to float32 before adding one in double.
        import struct
        g = [1+struct.unpack('<f', struct.pack('<f', x))[0] for x in rates]
        self.assertEqual(level_growth(1, rates), 1)
        self.assertAlmostEqual(level_growth(30, rates), g[0]**29)
        self.assertAlmostEqual(level_growth(31, rates), g[0]**29*g[1])
        self.assertAlmostEqual(level_growth(50, rates), g[0]**29*g[1]**20)
        self.assertAlmostEqual(level_growth(51, rates), g[0]**29*g[1]**20*g[2])
        self.assertEqual(level_growth(90, rates), level_growth(70, rates))
        hp = enemy_health(100, 50, CATALOG['difficulties']['Nightmare'], CATALOG['config'])
        self.assertAlmostEqual(enemy_health(100, 50, CATALOG['difficulties']['Nightmare'], CATALOG['config'], elite=True), hp*3)


class ShippedCatalogTests(unittest.TestCase):
    def test_catalog_integrity_and_actual_localization(self):
        self.assertEqual(len(CATALOG['maps']), 76)
        self.assertEqual(sum(m['supported'] for m in CATALOG['maps']), 71)
        self.assertEqual(len(CATALOG['enemies']), 65)
        self.assertEqual([d['label'] for d in CATALOG['difficulty_options']], ['普通', '噩梦', '炼狱'])
        for stage in CATALOG['maps']:
            if stage['supported']:
                for encounter in stage['normal_pool']+[stage['boss_encounter']]:
                    for entry in encounter['enemies']:
                        self.assertIn(entry['enemy_id'], CATALOG['enemies'])
        snow = next(m for m in CATALOG['maps'] if m['map_id'] == 'SnowHill5')
        self.assertEqual(snow['stage'], '维尔达克高地 5')

    def test_weighted_random_expectation_differs_from_current_plan(self):
        result = recommend_maps(profile(), CATALOG)
        row = next(r for r in result['rows'] if r['map_id'] == 'SnowHill5')
        # Pool weights are native float32 .8/.8/1; no plan reward is reused.
        w = next(m for m in CATALOG['maps'] if m['map_id'] == 'SnowHill5')['normal_pool'][0]['weight']
        expected = 10*(w*3*5364+w*3*4598+2*4598+5364)/(2*w+1)
        self.assertAlmostEqual(row['xp_per_run'], expected)
        self.assertNotEqual(row['xp_per_run'], 149430)

    def test_boss_replaces_last_wave_and_contains_adds(self):
        rows = {r['map_id']: r for r in recommend_maps(profile(), CATALOG)['rows']}
        self.assertEqual((rows['SnowHill4']['normal_waves'], rows['SnowHill4']['boss_waves']), (11, 1))
        self.assertEqual((rows['SnowHill7']['normal_waves'], rows['SnowHill7']['boss_waves']), (9, 1))
        self.assertEqual(rows['SnowHill4']['boss_count'], 11)
        self.assertEqual(rows['SnowHill7']['boss_count'], 7)

    def test_higher_difficulty_uses_level70_and_hp_override_for_xp(self):
        result = recommend_maps(profile(), CATALOG, options={'difficulty': 'Nightmare'})
        row = next(r for r in result['rows'] if r['map_id'] == 'SnowHill7')
        self.assertEqual((row['enemy_level_min'], row['enemy_level_max']), (70, 70))
        dragon = CATALOG['enemies']['SnowHillFrostDragon']
        self.assertEqual(dragon['high_difficulty_health'], 6000)
        boss = next(m for m in CATALOG['maps'] if m['map_id'] == 'SnowHill7')['boss_encounter']
        expected = sum(enemy_xp(CATALOG['enemies'][e['enemy_id']].get('high_difficulty_health') or CATALOG['enemies'][e['enemy_id']]['base_health'], 70, 47, 2.322, 1.5, CATALOG['config'])*e['count'] for e in boss['enemies'])
        self.assertGreaterEqual(row['xp_per_run'], expected)
        self.assertIsNone(row['accessible'])


class CombatAndAccessTests(unittest.TestCase):
    def test_final_stats_fallback_and_precomputed_dps(self):
        p = profile(combat={'damage': 100, 'attack_speed': 2, 'crit_chance': .25, 'crit_multiplier': 2, 'effective_targets': 2})
        combat = profile_effective_combat(p)
        self.assertEqual(combat['boss_dps'], 250)
        self.assertEqual(combat['normal_dps'], 500)
        p['combat']['normal_dps'] = 700
        self.assertEqual(profile_effective_combat(p)['normal_dps'], 700)
        self.assertFalse(recommend_maps(profile(combat={'damage': 0, 'attack_speed': 2}), CATALOG)['available'])

    def test_basic_skill_real_damage_and_global_cooldown_take_precedence(self):
        p = profile(combat={'damage': 460, 'attack_speed': 1.484, 'crit_chance': .05, 'crit_multiplier': 2.274}, skills=[dict(tags=[0], cooldown=.6737, global_cooldown=.75, standing_time=.5, effects=[dict(type='DamageEffect', damage=921, can_crit=True)])])
        combat = profile_effective_combat(p)
        self.assertAlmostEqual(combat['boss_dps'], 921*1.0637/.75)
        self.assertEqual(combat['dps_source'], 'basic_skill_theory')
        self.assertTrue(combat['model_warnings'])

    def test_every_third_known_replacement_is_averaged_once(self):
        p = profile(combat={}, skills=[dict(tags=[0], cooldown=1, effects=[dict(type='DamageEffect', damage=90), dict(type='EveryNthCastEffect', interval=3, replaces=True, triggered=[dict(type='DamageEffect', damage=180)])])])
        self.assertEqual(profile_effective_combat(p)['boss_dps'], 120)

    def test_actual_every_third_aoe_and_multi_projectile_do_not_duplicate_hits(self):
        p = profile(combat={'crit_chance': .05, 'crit_multiplier': 2.274}, skills=[dict(tags=[0], cooldown=.6737, global_cooldown=.75, effects=[dict(type='DamageEffect', damage=921, can_crit=True), dict(type='EveryNthCastEffect', interval=3, replaces=True, triggered=[dict(type='SpawnParticleAoEEffect', on_hit=[dict(type='DamageEffect', damage=1489, can_crit=True)])])])])
        self.assertAlmostEqual(profile_effective_combat(p)['boss_dps'], ((2*921+1489)/3)*1.0637/.75)
        p['skills'][0]['effects'] = [dict(type='SpawnProjectileEffect', projectiles=2, on_hit=[dict(type='DamageEffect', damage=100)])]
        combat = profile_effective_combat(p)
        self.assertAlmostEqual(combat['boss_dps'], 100/.75)
        self.assertTrue(any('多发弹道' in w for w in combat['model_warnings']))

    def test_current_difficulty_progress_never_unlocks_foreign_difficulty(self):
        stage = tiny_catalog()['maps'][0]
        p = tiny_profile()
        self.assertEqual(map_access(p, stage, 'Normal', CATALOG['difficulties']['Normal'])[0], True)
        self.assertIsNone(map_access(p, stage, 'Nightmare', CATALOG['difficulties']['Nightmare'])[0])
        p['unlocked_maps'] = ['SnowHill1']
        self.assertIsNone(map_access(p, stage, 'Nightmare', CATALOG['difficulties']['Nightmare'])[0])
        p['unlocked_maps'] = {'Nightmare': ['SnowHill1']}
        self.assertEqual(map_access(p, stage, 'Nightmare', CATALOG['difficulties']['Nightmare'])[0], True)

    def test_demo_and_difficulty_specific_entry_requirements(self):
        stage = tiny_catalog()['maps'][0]
        stage['requirements'] = [{'item_id': 'key', 'count': 2, 'difficulty_mask': 1}]
        self.assertFalse(map_access(tiny_profile(entry_items={'key': 1}), stage, 'Normal', CATALOG['difficulties']['Normal'])[0])
        self.assertIsNone(map_access(tiny_profile(entry_items=None), stage, 'Normal', CATALOG['difficulties']['Normal'])[0])
        stage['locked_in_demo'] = True
        self.assertFalse(map_access(tiny_profile(is_demo=True), stage, 'Normal', CATALOG['difficulties']['Normal'])[0])


class RankingAndCalibrationTests(unittest.TestCase):
    def test_default_budget_and_rate_can_choose_different_maps(self):
        catalog = tiny_catalog([{}, {'map_id': 'SnowHill2'}])
        catalog['maps'][0]['normal_pool'][0]['enemies'][0]['count'] = 2
        catalog['maps'][1]['normal_pool'] = copy.deepcopy(catalog['maps'][0]['normal_pool'])
        catalog['maps'][1]['normal_pool'][0]['enemies'][0]['count'] = 3
        p = tiny_profile(unlocked_maps={'Normal': ['SnowHill1', 'SnowHill2']})
        self.assertEqual(recommend_maps(p, catalog, options={'minutes': 1})['best']['map_id'], 'SnowHill1')
        self.assertEqual(recommend_maps(p, catalog, options={'minutes': 1, 'sort': 'rate'})['best']['map_id'], 'SnowHill2')

    def test_short_budget_and_rate_have_different_eligibility(self):
        catalog = tiny_catalog()
        full = recommend_maps(tiny_profile(), catalog, options={'minutes': .01})
        rate = recommend_maps(tiny_profile(), catalog, options={'minutes': .01, 'sort': 'rate'})
        self.assertIsNone(full['best'])
        self.assertEqual(full['rows'][0]['completed_runs_xp'], 0)
        self.assertIsNotNone(rate['best'])
        self.assertGreater(rate['best']['projected_xp'], 0)
        self.assertEqual(recommend_maps(tiny_profile(), catalog, options={'minutes': 10080})['minutes'], 10080)
        for opts in ({'minutes': 10081}, {'minutes': 0}, {'sort': 'invalid'}):
            with self.assertRaises(ValueError): recommend_maps(tiny_profile(), catalog, options=opts)

    def test_phase_calibration_respects_family_fingerprint_and_fixed_wait(self):
        catalog = tiny_catalog([{}, {'map_id': 'Forest1', 'family': 'Forest'}])
        p = tiny_profile(unlocked_maps={'Normal': ['SnowHill1', 'Forest1']})
        run = dict(fingerprint=p['fingerprint'], map_id='SnowHill2', difficulty='Normal', outcome='success', run_seconds=21.5, normal_seconds=21.5, normal_health=100, normal_fixed_seconds=1.5, combat={'normal_dps': 10})
        rows = {r['map_id']: r for r in recommend_maps(p, catalog, {'runs': [run]})['rows']}
        self.assertEqual(rows['SnowHill1']['normal_seconds'], 20)
        self.assertTrue(rows['SnowHill1']['calibrated'])
        self.assertEqual(rows['Forest1']['normal_seconds'], 10)
        self.assertFalse(rows['Forest1']['calibrated'])
        run['fingerprint'] = 'other'
        self.assertFalse(recommend_maps(p, catalog, {'runs': [run]})['rows'][0]['calibrated'])

    def test_whole_run_calibration_does_not_transfer_to_other_maps(self):
        catalog = tiny_catalog([{}, {'map_id': 'SnowHill2'}])
        run = dict(fingerprint='same-build', map_id='SnowHill1', difficulty='Normal', outcome='success', run_seconds=21.5, fixed_seconds=1.5)
        rows = {r['map_id']: r for r in recommend_maps(tiny_profile(), catalog, {'runs': [run]})['rows']}
        self.assertEqual(rows['SnowHill1']['normal_seconds'], 20)
        self.assertEqual(rows['SnowHill2']['normal_seconds'], 10)

    def test_whole_run_calibration_normalizes_saved_damage_and_random_health(self):
        run = dict(fingerprint='same-build', map_id='SnowHill1', difficulty='Normal', outcome='success', run_seconds=21.5, fixed_seconds=1.5, normal_health=100, boss_health=0, combat={'normal_dps': 10, 'boss_dps': 10})
        p = tiny_profile(combat={'normal_dps': 20, 'boss_dps': 20})
        row = recommend_maps(p, tiny_catalog(), {'runs': [run]})['rows'][0]
        self.assertEqual(row['normal_seconds'], 10)

    def test_observed_restart_median_replaces_only_default_overhead(self):
        runs = [dict(fingerprint='same-build', map_id='SnowHill1', difficulty='Normal', outcome='success', run_seconds=11.5, overhead_seconds=value) for value in (3, 4, 5)]
        row = recommend_maps(tiny_profile(), tiny_catalog(), {'runs': runs})['rows'][0]
        self.assertEqual(row['overhead_seconds'], 4)
        self.assertEqual(row['overhead_source'], 'observed')
        custom = recommend_maps(tiny_profile(), tiny_catalog(), {'runs': runs}, {'overhead_seconds': 12})['rows'][0]
        self.assertEqual(custom['overhead_seconds'], 12)

    def test_manual_default_value_is_never_replaced_by_observations(self):
        run=dict(fingerprint='same-build',map_id='SnowHill1',difficulty='Normal',outcome='success',run_seconds=11.5,overhead_seconds=4)
        manual=recommend_maps(tiny_profile(),tiny_catalog(),{'runs':[run]},
                              {'overhead_mode':'manual','overhead_seconds':8})['rows'][0]
        auto=recommend_maps(tiny_profile(),tiny_catalog(),{'runs':[run]},
                            {'overhead_mode':'auto','overhead_seconds':8})['rows'][0]
        self.assertEqual(manual['overhead_seconds'],8)
        self.assertEqual(manual['overhead_source'],'manual')
        self.assertEqual(auto['overhead_seconds'],4)
        self.assertGreater(auto['completed_runs'],manual['completed_runs'])

    def test_auto_overhead_without_samples_uses_documented_default(self):
        row=recommend_maps(tiny_profile(),tiny_catalog(),options={'overhead_mode':'auto','overhead_seconds':20})['rows'][0]
        self.assertEqual(row['overhead_seconds'],8)
        self.assertEqual(row['overhead_source'],'default')

    def test_failure_cycle_uses_known_retained_xp_and_normalizes_bonus(self):
        p = tiny_profile()
        base = dict(fingerprint='same-build', map_id='SnowHill1', difficulty='Normal', level=1, combat={'xp_gain_multiplier': 2})
        runs = [dict(base, outcome='success', run_seconds=11.5), dict(base, outcome='success', run_seconds=11.5), dict(base, outcome='failure', run_seconds=5, xp=40)]
        row = recommend_maps(p, tiny_catalog(), {'runs': runs}, {'minutes': 1})['rows'][0]
        cycle = row['cycle_estimate']
        self.assertAlmostEqual(cycle['failure_xp'], 20)
        self.assertAlmostEqual(cycle['success_probability'], 2/3)
        self.assertAlmostEqual(row['xp_per_hour'], cycle['expected_xp_per_cycle']/cycle['seconds_per_cycle']*3600)
        self.assertGreater(row['effective_budget_xp'], row['completed_runs_xp'])
        self.assertFalse(any('收益按成功全清估算' in r for r in row['reasons']))
        runs[-1]['xp'] = None
        self.assertIsNone(recommend_maps(p, tiny_catalog(), {'runs': runs})['rows'][0]['cycle_estimate'])

    def test_three_known_failures_use_partial_rewards_without_inventing_success(self):
        runs = [dict(fingerprint='same-build', map_id='SnowHill1', difficulty='Normal', level=1,
                     outcome='failed', run_seconds=5, xp=10, combat={'xp_gain_multiplier': 1}) for _ in range(3)]
        result = recommend_maps(tiny_profile(), tiny_catalog(), {'runs': runs}, {'minutes': 1})
        row = result['rows'][0]
        self.assertEqual(row['cycle_estimate']['success_probability'], 0)
        self.assertEqual(row['cycle_estimate']['seconds_per_cycle'], 13)
        self.assertAlmostEqual(row['xp_per_hour'], 10/13*3600)
        self.assertEqual(row['effective_budget_xp'], 40)
        self.assertEqual(row['completed_runs'], 0)
        self.assertEqual(row['completed_runs_xp'], 0)
        self.assertIsNone(result['best'])
        self.assertIn('尚无成功通关观测', result['reason'])
        rate = recommend_maps(tiny_profile(), tiny_catalog(), {'runs': runs}, {'minutes': 1, 'sort': 'rate'})
        self.assertEqual(rate['best']['map_id'], 'SnowHill1')
        self.assertAlmostEqual(rate['best']['xp_per_hour'], 10/13*3600)
        self.assertTrue(any('含失败前保留经验' in text for text in row['reasons']))

    def test_insufficient_or_unknown_failure_rewards_keep_risk_without_probability(self):
        run = dict(fingerprint='same-build', map_id='SnowHill1', difficulty='Normal', level=1,
                   outcome='failed', run_seconds=5, xp=10, combat={'xp_gain_multiplier': 1})
        for runs in ([copy.deepcopy(run)], [copy.deepcopy(run)]*2,
                     [copy.deepcopy(run), copy.deepcopy(run), dict(run, xp=None)],
                     [copy.deepcopy(run), copy.deepcopy(run), dict(run, combat={})]):
            with self.subTest(runs=runs):
                row = recommend_maps(tiny_profile(), tiny_catalog(), {'runs': runs})['rows'][0]
                self.assertIsNone(row['cycle_estimate'])
                self.assertEqual(row['risk'], 'observed_failure')
                self.assertTrue(any('有失败记录' in text for text in row['reasons']))

    def test_known_zero_failure_reward_is_not_missing_reward(self):
        runs = [dict(fingerprint='same-build', map_id='SnowHill1', difficulty='Normal', level=1,
                     outcome='failed', run_seconds=5, xp=0, combat={'xp_gain_multiplier': 1}) for _ in range(3)]
        result = recommend_maps(tiny_profile(), tiny_catalog(), {'runs': runs}, {'minutes': 1})
        self.assertEqual(result['rows'][0]['cycle_estimate']['failure_xp'], 0)
        self.assertEqual(result['rows'][0]['effective_budget_xp'], 0)
        self.assertEqual(result['rows'][0]['xp_per_hour'], 0)
        self.assertIsNone(result['best'])

    def test_failed_samples_other_level_or_loadout_never_supply_probability(self):
        base = dict(fingerprint='same-build', map_id='SnowHill1', difficulty='Normal', level=2, combat={'xp_gain_multiplier': 1}, xp=0, run_seconds=5)
        runs = [dict(base, outcome='success'), dict(base, outcome='success'), dict(base, outcome='failure')]
        row = recommend_maps(tiny_profile(), tiny_catalog(), {'runs': runs})['rows'][0]
        self.assertIsNone(row['cycle_estimate'])
        self.assertEqual(row['risk'], 'unverified')
        for run in runs:
            run.update(level=1, fingerprint='other-loadout')
        row = recommend_maps(tiny_profile(), tiny_catalog(), {'runs': runs})['rows'][0]
        self.assertIsNone(row['cycle_estimate'])
        self.assertEqual(row['risk'], 'unverified')

    def test_consumed_keys_bound_full_attempts_without_mutating_inputs(self):
        catalog = tiny_catalog()
        catalog['maps'][0]['requirements'] = [{'item_id': 'key', 'count': 2, 'difficulty_mask': 1}]
        p = tiny_profile(entry_items={'key': 5}, entry_requirements_consumed=True)
        before = copy.deepcopy((p, catalog))
        row = recommend_maps(p, catalog)['rows'][0]
        self.assertEqual(row['completed_runs'], 2)
        self.assertEqual((p, catalog), before)


class CurrentPlanTests(unittest.TestCase):
    def test_positive_plan_is_exact_separate_from_weighted_map_expectation(self):
        plan = {'waves': [dict(type='Normal', level=57, enemies=[dict(id='SnowHillZombieWarrior', count=3, xp=4598)] ) for _ in range(5)]+[dict(type='Normal', level=57, enemies=[dict(id='SnowHillWhelp', count=3, xp=5364)]) for _ in range(5)]}
        result = estimate_run(profile(), plan, CATALOG)
        self.assertTrue(result['available'])
        self.assertTrue(result['xp_exact'])
        self.assertEqual(result['xp_per_run'], 149430)
        self.assertGreater(result['normal_health'], 0)

    def test_effective_plan_hp_does_not_receive_growth_or_elite_twice(self):
        plan = {'waves': [dict(type='Elite', level=57, enemies=[dict(id='SnowHillZombieWarrior', count=2, xp=4598, health=100)])]}
        result = estimate_run(profile(), plan, CATALOG)
        self.assertEqual(result['normal_health'], 200)
        del plan['waves'][0]['enemies'][0]['health']
        elite = estimate_run(profile(), plan, CATALOG)['normal_health']
        plan['waves'][0]['type'] = 'Normal'
        normal = estimate_run(profile(), plan, CATALOG)['normal_health']
        self.assertAlmostEqual(elite, 3*normal)

    def test_fallback_requires_verified_enemy_source_and_boss_includes_adds(self):
        plan = {'waves': [dict(type=2, level=1, enemies=[dict(enemy_id='test', count=2, xp=0)])]}
        result = estimate_run(tiny_profile(), plan, tiny_catalog())
        self.assertEqual(result['boss_health'], 200)
        self.assertEqual(result['xp_per_run'], 200)
        self.assertFalse(result['xp_exact'])
        plan['waves'][0]['enemies'][0].update(enemy_id='unknown', health=100)
        self.assertFalse(estimate_run(tiny_profile(), plan, tiny_catalog())['available'])


if __name__ == '__main__':
    unittest.main()
