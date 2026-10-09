"""Pure, current-character map recommendations from verified shipped data.

Rewards follow the native fallback path. Timings are estimates, initialized
from current combat throughput and wave waits, then passively calibrated.
No native method invocation, process access, game input, or persistence here.
"""
from __future__ import annotations
from functools import lru_cache
import json
import math
import re
from pathlib import Path
from statistics import median
import struct
from .paths import RESOURCE_ROOT


def _number(value, default=None):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def _f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _positive(value, default=None):
    value = _number(value)
    return value if value is not None and value > 0 else default


@lru_cache(maxsize=2)
def load_recommendation_catalog(path=None):
    catalog = json.loads(Path(path or RESOURCE_ROOT/'data/recommendation-catalog.json').read_text(encoding='utf-8-sig'))
    if catalog.get('schema_version') != 1 or not isinstance(catalog.get('maps'), list) or not isinstance(catalog.get('enemies'), dict):
        raise ValueError('推荐资源目录格式不一致。')
    return catalog


def difficulty_id(value):
    return {0: 'Normal', 1: 'Nightmare', 2: 'Inferno', 'Inferno1': 'Inferno', '普通': 'Normal', '噩梦': 'Nightmare', '炼狱': 'Inferno'}.get(value, value)


def baseline_enemy_xp(base_health, enemy_level, config):
    """Enemy.cmg: double HP*XpPerBaseHealth; float32 level scale; round-even."""
    if not _positive(base_health) or not 1 <= enemy_level <= 70:
        raise ValueError('敌人基础血量或等级无效。')
    scale = _f32(_f32(enemy_level-1) * _f32(config['XpPerLevelScale']))
    return max(1, round(base_health * _f32(config['XpPerBaseHealth']) * (1.0+scale)))


def enemy_xp(base_health, enemy_level, player_level, xp_gain_multiplier, difficulty_multiplier, config, *, planned_xp=None):
    """GameManager death 0x77DFC0. Positive planned XP bypasses all multipliers.

    Zero/negative plan entries fall back to live level/bonus calculation. The
    native fallback floors the final award to one even at zero level factor.
    """
    if isinstance(planned_xp, int) and not isinstance(planned_xp, bool) and planned_xp > 0:
        return planned_xp
    multiplier = _positive(xp_gain_multiplier)
    difficulty = _positive(difficulty_multiplier)
    if not multiplier or not difficulty or not 1 <= player_level <= 70:
        raise ValueError('角色经验倍率或等级无效。')
    baseline = baseline_enemy_xp(base_health, enemy_level, config)
    cutoff = config['XpZeroUnderLevel']
    if cutoff <= 0:
        raise ValueError('等级惩罚配置无效。')
    penalty = min(1.0, max(0.0, _f32(_f32(cutoff-(player_level-enemy_level)) / _f32(cutoff))))
    amount = _f32(_f32(baseline) * penalty)
    amount = _f32(amount * _f32(multiplier))
    amount = _f32(amount * _f32(difficulty))
    return max(1, round(amount))


def level_growth(level, rates):
    """GameConfig.ccv: level capped 1..70, exponents 29/20/20, double powers."""
    level = min(70, max(1, level))
    early = min(29, level-1)
    middle = min(20, max(0, level-30))
    late = min(20, max(0, level-50))
    a = math.pow(1.0+_f32(rates[0]), early)
    b = math.pow(1.0+_f32(rates[1]), middle)
    c = math.pow(1.0+_f32(rates[2]), late)
    return c*(b*a)


def enemy_health(base_health, level, difficulty, config, *, elite=False):
    """Enemy.bjv effective MaxHP; base_health includes any verified override.

    difficulty is a difficulty config dict. Result is a double workload; the
    game may round its displayed/integer max HP separately.
    """
    if not _positive(base_health) or not 1 <= level <= 70:
        raise ValueError('敌人血量或等级无效。')
    return base_health*level_growth(level, config['health_growth'])*difficulty['health_multiplier']*(config['EliteHealthMultiplier'] if elite else 1.0)


def _basic_skill_damage(effects, crit_factor, targets, warnings):
    """Known per-cast damage only; no mana/cooldown rotation is invented."""
    single = normal = 0.0
    periodic = []
    for effect in effects:
        kind = effect.get('type')
        if kind == 'DamageEffect' and _positive(effect.get('damage')):
            damage = effect['damage']*(crit_factor if effect.get('can_crit') else 1.0)
            single += damage
            normal += damage
        elif kind == 'EveryNthCastEffect':
            periodic.append(effect)
        elif kind in ('SpawnParticleAoEEffect', 'SpawnProjectileEffect'):
            nested = effect.get('on_hit', effect.get('effects', effect.get('on_tick', [])))
            hit, area = _basic_skill_damage(nested, crit_factor, targets, warnings)
            single += hit
            normal += area*(targets if kind == 'SpawnParticleAoEEffect' else 1.0)
            if kind == 'SpawnProjectileEffect' and effect.get('projectiles', 1) > 1:
                warnings.append('多发弹道按一次命中估计，额外命中与穿透由刷图校准。')
        else:
            warnings.append('普通攻击有尚未建模的触发效果，首次时间为估计。')
    for effect in periodic:
        interval = _positive(effect.get('interval'))
        triggered = effect.get('triggered', [])
        hit, area = _basic_skill_damage(triggered, crit_factor, targets, warnings)
        if interval:
            if effect.get('replaces'):
                if hit or area:
                    single = single*(1-1/interval)+hit/interval
                    normal = normal*(1-1/interval)+area/interval
                else:
                    warnings.append('替换攻击效果尚未读取，暂以普通击伤害初始化并自动校准。')
            else:
                single += hit/interval
                normal += area/interval
    return single, normal


def profile_effective_combat(profile, catalog=None):
    """Return a numeric combat dict for theory and run-start normalization.

    Reader-computed skill throughput takes precedence. Otherwise use final
    damage/attack speed/critical values, never unmodified BaseDamage fields.
    """
    combat = dict(profile.get('combat') or {})
    damage = _positive(combat.get('damage'))
    speed = _positive(combat.get('attack_speed'))
    crit = min(1.0, max(0.0, _number(combat.get('crit_chance'), 0.0)))
    crit_multiplier = max(1.0, _number(combat.get('crit_multiplier'), 1.0))
    single = damage*speed*(1.0+crit*(crit_multiplier-1.0)) if damage and speed else None
    targets = max(1.0, _number(combat.get('effective_targets'), 1.0))
    normal = single*targets if single else None
    source = 'final_stats_theory'
    warnings = []
    for skill in profile.get('skills', []):
        if 0 not in skill.get('tags', []):
            continue
        hit, area = _basic_skill_damage(skill.get('effects', []), 1.0+crit*(crit_multiplier-1.0), targets, warnings)
        period = max(_number(skill.get(field), 0.0) for field in ('cooldown', 'global_cooldown', 'standing_time', 'channel_duration'))
        if hit > 0 and period > 0:
            single, normal = hit/period, area/period
            source = 'basic_skill_theory'
            warnings.append('首次时间以当前普通攻击技能估计；额外技能、耗蓝循环及控制效果由刷图校准。')
            break
    combat['effective_targets'] = targets
    combat['boss_dps'] = _positive(combat.get('boss_dps'), _positive(combat.get('dps'), single))
    combat['normal_dps'] = _positive(combat.get('normal_dps'), _positive(combat.get('dps'), normal))
    combat.setdefault('dps_source', source)
    combat['model_warnings'] = list(dict.fromkeys(warnings))
    return combat


def _difficulty(profile, options, catalog):
    wanted = options.get('difficulty', 'current')
    key = difficulty_id(profile.get('difficulty', 'Normal') if wanted == 'current' else wanted)
    if key not in catalog['difficulties']:
        raise ValueError('当前难度不在已验证范围。')
    return key, catalog['difficulties'][key]


def map_access(profile, stage, difficulty, difficulty_config):
    reasons = []
    if stage.get('unavailable') or profile.get('is_demo') and stage.get('locked_in_demo'):
        return False, ['该关卡当前不可用。']
    available_difficulties = profile.get('unlocked_difficulties')
    if available_difficulties is not None and difficulty not in [difficulty_id(x) for x in available_difficulties]:
        return False, ['该难度尚未解锁。']
    unlocked = profile.get('unlocked_maps')
    per_difficulty = isinstance(unlocked, dict)
    if isinstance(unlocked, dict):
        unlocked = unlocked.get(difficulty)
    accessible = None
    confirmed_difficulty = difficulty == difficulty_id(profile.get('difficulty')) or available_difficulties is not None and difficulty in [difficulty_id(x) for x in available_difficulties]
    if not confirmed_difficulty and unlocked is None:
        reasons.append('该难度的解锁进度尚未读取。')
    if isinstance(unlocked, (list, tuple, set)) and (confirmed_difficulty or per_difficulty):
        accessible = stage['map_id'] in unlocked
        if not accessible:
            reasons.append('关卡尚未解锁。')
    elif not stage.get('prerequisite') and confirmed_difficulty:
        accessible = True
    else:
        reasons.append('关卡解锁状态尚未读取。')
    items = profile.get('entry_items')
    for requirement in stage.get('requirements', []):
        if not requirement['difficulty_mask'] & (1 << difficulty_config['value']):
            continue
        if items is None:
            if accessible is not False:
                accessible = None
            reasons.append('入场道具数量尚未读取。')
        elif _number(items.get(requirement['item_id']), 0) < requirement['count']:
            accessible = False
            reasons.append('入场道具不足。')
    return accessible, reasons


def _stats(enemy, level, difficulty, config):
    higher = difficulty['value'] > 0
    hp = enemy.get('high_difficulty_health', 0) if higher else 0
    damage = enemy.get('high_difficulty_damage', 0) if higher else 0
    hp = hp or enemy['base_health']
    damage = damage or enemy['base_damage']
    return hp, enemy_health(hp, level, difficulty, config), damage*level_growth(level, config['damage_growth'])*difficulty['damage_multiplier']


def _estimate_encounter(encounter, levels, profile, difficulty, catalog, elite_chance=0):
    result = {'xp': 0.0, 'health': 0.0, 'count': 0.0, 'max_damage': 0.0}
    config = catalog['config']
    for entry in encounter['enemies']:
        enemy = catalog['enemies'][entry['enemy_id']]
        for level in levels:
            base_hp, hp, damage = _stats(enemy, level, difficulty, config)
            reward = enemy_xp(base_hp, level, profile['level'], profile['xp_gain_multiplier'], difficulty['xp_multiplier'], config)
            weight = entry['count']/len(levels)
            result['xp'] += reward*weight
            result['health'] += hp*(1+elite_chance*(config['EliteHealthMultiplier']-1))*weight
            result['count'] += weight
            result['max_damage'] = max(result['max_damage'], damage*(config['EliteDamageMultiplier'] if elite_chance > 0 else 1))
    return result


def _run_features(profile, stage, difficulty, catalog):
    low, high = (stage['min_level'], stage['max_level']) if difficulty['value'] == 0 else (catalog['config']['MaxPlayerLevel'],)*2
    if not 1 <= low <= high <= 70:
        raise ValueError('地图敌人等级无效。')
    levels = range(low, high+1)
    boss = stage.get('boss', False)
    normal_waves = max(0, stage['waves']-int(boss))
    pool = stage['normal_pool']
    positive = [max(0, e['weight']) for e in pool]
    total = sum(positive)
    weights = [x/total if total else 1/len(pool) for x in positive]
    ordinary = [_estimate_encounter(e, levels, profile, difficulty, catalog, stage['elite_chance']) for e in pool]
    phase = {key: sum(w*e[key] for w, e in zip(weights, ordinary))*normal_waves for key in ['xp', 'health', 'count']}
    phase['max_damage'] = max(e['max_damage'] for e in ordinary)
    boss_phase = _estimate_encounter(stage['boss_encounter'], levels, profile, difficulty, catalog) if boss else {'xp': 0, 'health': 0, 'count': 0, 'max_damage': 0}
    return {'xp_per_run': phase['xp']+boss_phase['xp'], 'normal_health': phase['health'], 'boss_health': boss_phase['health'],
            'normal_waves': normal_waves, 'boss_waves': int(boss), 'waves': stage['waves'],
            'normal_count': phase['count'], 'boss_count': boss_phase['count'], 'enemy_level_min': low, 'enemy_level_max': high,
            'enemy_max_hit': max(phase['max_damage'], boss_phase['max_damage'])}


def _matching_runs(profile, calibration, difficulty):
    fingerprint = profile.get('fingerprint')
    if not fingerprint:
        return []
    return [r for r in (calibration or {}).get('runs', []) if r.get('fingerprint') == fingerprint and difficulty_id(r.get('difficulty')) == difficulty
            and r.get('outcome') == 'success' and _positive(r.get('run_seconds'))]


def _timing(profile, features, combat, options):
    timing = profile.get('timing') or {}
    between = max(0.0, _number(timing.get('time_between_waves'), 2.5))
    initial = max(0.0, _number(timing.get('initial_spawn_delay'), 1.5))
    approach = max(0.0, _number(timing.get('approach_seconds_per_wave'), 0.0))
    if 'approach_seconds_per_wave' not in timing:
        spawn_distance = _positive(timing.get('spawn_distance'))
        move_speed = _positive(combat.get('move_speed'))
        if spawn_distance and move_speed:
            approach = spawn_distance/move_speed
    normal_fixed = initial+max(0, features['normal_waves']-1)*between+features['normal_waves']*approach if features['normal_waves'] else 0.0
    boss_fixed = features['boss_waves']*(between+approach) if features['normal_waves'] else features['boss_waves']*(initial+approach)
    fixed = normal_fixed+boss_fixed
    return {'normal_seconds': features['normal_health']/combat['normal_dps'], 'boss_seconds': features['boss_health']/combat['boss_dps'],
            'fixed_seconds': fixed, 'normal_fixed_seconds': normal_fixed, 'boss_fixed_seconds': boss_fixed,
            'overhead_seconds': max(0.0, _number(options.get('overhead_seconds'), 8.0))}


def _calibrated_timing(profile, stage, difficulty, features, timing, combat, calibration):
    runs = _matching_runs(profile, calibration, difficulty)
    normal_ratios, boss_ratios = [], []
    for run in runs:
        family = run.get('family') or re.sub(r'\d+$', '', run.get('map_id', ''))
        if family != stage['family']:
            continue
        saved = run.get('combat') or combat
        for phase, ratios in [('normal', normal_ratios), ('boss', boss_ratios)]:
            hp = _positive(run.get(phase+'_health'))
            seconds = _positive(run.get(phase+'_seconds'))
            dps = _positive(saved.get(phase+'_dps'))
            # Explicit phase waits are preferred. No deduction of an entire
            # run's waits from the Boss phase or inference from only total time.
            fixed = _number(run.get(phase+'_fixed_seconds'))
            if fixed is None and phase == 'normal':
                fixed = _number(run.get('fixed_seconds'), 0.0)
            fixed = fixed or 0.0
            if hp and seconds and dps and seconds > fixed:
                ratios.append((seconds-fixed)/(hp/dps))
    adjusted = dict(timing)
    if not (calibration or {}).get('overhead_user_set') and abs(timing['overhead_seconds']-8.0) < 1e-9:
        observed_overheads = [r['overhead_seconds'] for r in runs if (r.get('family') or re.sub(r'\d+$', '', r.get('map_id', ''))) == stage['family'] and _number(r.get('overhead_seconds')) is not None and 0 <= r['overhead_seconds'] <= 30]
        same_overheads = [r['overhead_seconds'] for r in runs if r.get('map_id') == stage['map_id'] and _number(r.get('overhead_seconds')) is not None and 0 <= r['overhead_seconds'] <= 30]
        if same_overheads or observed_overheads:
            adjusted['overhead_seconds'] = median(same_overheads or observed_overheads)
            adjusted['overhead_source'] = 'observed'
    samples = 0
    for phase, ratios in [('normal', normal_ratios), ('boss', boss_ratios)]:
        if ratios:
            factor = min(20.0, max(0.05, median(ratios)))
            adjusted[phase+'_seconds'] *= factor
            adjusted[phase+'_factor'] = factor
            samples = max(samples, len(ratios))
    same_map = [r for r in runs if r.get('map_id') == stage['map_id']]
    # Whole-run observations only calibrate this same map. They cannot infer
    # separate AOE/Boss throughput or transfer across unrelated mechanics.
    if same_map and not normal_ratios and not boss_ratios:
        theoretical = timing['normal_seconds']+timing['boss_seconds']
        ratios = []
        for run in same_map:
            fixed = _number(run.get('fixed_seconds'), timing['fixed_seconds'])
            elapsed = run['run_seconds']-fixed
            observed_theory = 0.0
            valid_saved = True
            for phase in ('normal', 'boss'):
                hp = _number(run.get(phase+'_health'))
                dps = _positive((run.get('combat') or {}).get(phase+'_dps'))
                if hp is None or hp < 0 or hp > 0 and dps is None:
                    valid_saved = False
                    break
                if hp:
                    observed_theory += hp/dps
            denominator = observed_theory if valid_saved and observed_theory > 0 else theoretical
            if elapsed > 0 and denominator > 0:
                ratios.append(elapsed/denominator)
        if ratios:
            factor = min(20.0, max(0.05, median(ratios)))
            adjusted['normal_seconds'] *= factor
            adjusted['boss_seconds'] *= factor
            adjusted['time_factor'] = factor
            samples = len(ratios)
    adjusted['calibrated'] = samples > 0
    adjusted['samples'] = samples
    return adjusted


def _failure_cycle(profile, stage, difficulty, calibration, xp, seconds, overhead):
    """At least three consistent outcomes can estimate an empirical cycle.

    Observed failure XP must be known (retained XP read by the observer). No
    probability is invented from level/HP. Bonus changes are normalized using
    saved run-start values, and different levels are never averaged together.
    """
    fp = profile.get('fingerprint')
    if not fp:
        return None
    runs = [r for r in (calibration or {}).get('runs', []) if r.get('fingerprint') == fp
            and r.get('map_id') == stage['map_id'] and difficulty_id(r.get('difficulty')) == difficulty
            and r.get('level', profile['level']) == profile['level']
            and r.get('outcome') in ('success', 'failure', 'failed') and _positive(r.get('run_seconds'))]
    successes = [r for r in runs if r['outcome'] == 'success']
    failures = [r for r in runs if r['outcome'] in ('failure', 'failed')]
    known = []
    for r in failures:
        reward = _number(r.get('xp'))
        old_bonus = _positive((r.get('combat') or {}).get('xp_gain_multiplier'), _positive(r.get('xp_gain_multiplier')))
        if reward is not None and reward >= 0 and old_bonus:
            known.append((reward*profile['xp_gain_multiplier']/old_bonus, r['run_seconds']+overhead))
    if len(runs) < 3 or not successes or not failures or len(known) != len(failures):
        return None
    probability = len(successes)/len(runs)
    fail_xp = sum(x[0] for x in known)/len(known)
    fail_seconds = sum(x[1] for x in known)/len(known)
    return {'success_probability': probability, 'failure_samples': len(failures), 'outcome_samples': len(runs),
            'failure_xp': fail_xp, 'expected_xp_per_cycle': probability*xp+(1-probability)*fail_xp,
            'seconds_per_cycle': probability*seconds+(1-probability)*fail_seconds}


def estimate_run(profile, run, catalog=None, options=None):
    """Estimate a current plan's duration and positive-XP reward separately.

    run may be a summary with normal_health/boss_health/waves or copied waves.
    This does not override map repeat expectations in recommend_maps.
    """
    catalog, options = catalog or load_recommendation_catalog(), options or {}
    combat = profile_effective_combat(profile, catalog)
    if not combat['normal_dps'] or not combat['boss_dps']:
        return {'available': False, 'reason': '最终战斗属性尚未读取。'}
    summary = dict(run)
    if isinstance(run.get('waves'), list):
        normal_hp = boss_hp = xp = 0.0
        normal_waves = boss_waves = 0
        exact = True
        key, diff = _difficulty(profile, options, catalog)
        for wave in run['waves']:
            wave_type = wave.get('type')
            boss = wave_type == 2 or str(wave_type).lower() == 'boss'
            elite = wave_type == 1 or str(wave_type).lower() == 'elite'
            normal_waves += int(not boss)
            boss_waves += int(boss)
            for entry in wave.get('enemies', []):
                enemy = catalog['enemies'].get(entry.get('id', entry.get('enemy_id')))
                effective_hp = _positive(entry.get('health'))
                hp = _positive(entry.get('baseHP', entry.get('baseHealth')))
                if enemy:
                    _, hp_model, _ = _stats(enemy, wave['level'], diff, catalog['config'])
                    hp = hp_model if not hp else hp*level_growth(wave['level'], catalog['config']['health_growth'])*diff['health_multiplier']
                elif hp:
                    hp = enemy_health(hp, wave['level'], diff, catalog['config'])
                if hp is not None and elite:
                    hp *= catalog['config']['EliteHealthMultiplier']
                if effective_hp is not None:
                    hp = effective_hp
                if hp is None:
                    return {'available': False, 'reason': '计划敌人血量缺少已验证来源。'}
                count = entry['count']
                if boss:
                    boss_hp += hp*count
                else:
                    normal_hp += hp*count
                planned = entry.get('xp')
                if isinstance(planned, int) and not isinstance(planned, bool) and planned > 0:
                    xp += planned*count
                else:
                    exact = False
                    if enemy:
                        base, _, _ = _stats(enemy, wave['level'], diff, catalog['config'])
                        xp += enemy_xp(base, wave['level'], profile['level'], profile['xp_gain_multiplier'], diff['xp_multiplier'], catalog['config'])*count
                    else:
                        return {'available': False, 'reason': '计划敌人经验缺少已验证来源。'}
        summary.update(normal_health=normal_hp, boss_health=boss_hp, normal_waves=normal_waves, boss_waves=boss_waves,
                       waves=normal_waves+boss_waves, xp_per_run=xp, xp_exact=exact)
    summary.setdefault('normal_waves', max(0, int(summary.get('waves', 1))-int(summary.get('boss_waves', bool(summary.get('boss_health'))))))
    summary.setdefault('boss_waves', int(bool(summary.get('boss_health'))))
    summary.setdefault('normal_health', 0.0)
    summary.setdefault('boss_health', 0.0)
    timing = _timing(profile, summary, combat, options)
    return {'available': True, **summary, **timing, 'run_seconds': timing['normal_seconds']+timing['boss_seconds']+timing['fixed_seconds'],
            'seconds_per_run': timing['normal_seconds']+timing['boss_seconds']+timing['fixed_seconds']+timing['overhead_seconds']}


def recommend_maps(profile, catalog=None, calibration=None, options=None):
    catalog, options = catalog or load_recommendation_catalog(), options or {}
    level = profile.get('level')
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 70 or not _positive(profile.get('xp_gain_multiplier')):
        return {'available': False, 'reason': '当前角色等级或经验加成尚未读取。', 'rows': [], 'best': None, 'warnings': []}
    combat = profile_effective_combat(profile, catalog)
    if not combat['normal_dps'] or not combat['boss_dps']:
        return {'available': False, 'reason': '当前角色最终战斗属性尚未读取。', 'rows': [], 'best': None, 'warnings': []}
    key, diff = _difficulty(profile, options, catalog)
    minutes = _positive(options.get('minutes', 60.0))
    if minutes is None or minutes > 10080:
        raise ValueError('推荐时长最多为7天。')
    if options.get('sort', 'completed') not in ('completed', 'rate'):
        raise ValueError('推荐排序方式无效。')
    budget = minutes*60
    rows = []
    for stage in catalog['maps']:
        if not stage['supported']:
            continue
        accessible, reasons = map_access(profile, stage, key, diff)
        if accessible is False and options.get('include_locked', True) is False:
            continue
        features = _run_features(profile, stage, diff, catalog)
        timing = _timing(profile, features, combat, options)
        timing = _calibrated_timing(profile, stage, key, features, timing, combat, calibration)
        seconds = timing['normal_seconds']+timing['boss_seconds']+timing['fixed_seconds']+timing['overhead_seconds']
        count = math.floor(budget/seconds)
        items = profile.get('entry_items') or {}
        if profile.get('entry_requirements_consumed') is True:
            for requirement in stage['requirements']:
                if requirement['difficulty_mask'] & (1 << diff['value']):
                    count = min(count, max(0, math.floor(_number(items.get(requirement['item_id']), 0)/requirement['count'])))
        elif stage['requirements']:
            reasons.append('重复次数可能受入场道具数量限制。')
        rate = features['xp_per_run']/seconds*3600
        failures = [r for r in (calibration or {}).get('runs', []) if r.get('fingerprint') == profile.get('fingerprint') and r.get('map_id') == stage['map_id'] and difficulty_id(r.get('difficulty')) == key and r.get('outcome') in ('failure', 'failed')]
        risk = 'observed_failure' if failures else 'unverified'
        cycle = _failure_cycle(profile, stage, key, calibration, features['xp_per_run'], seconds, timing['overhead_seconds'])
        failure_budget_xp = None
        if cycle:
            cycle_count = math.floor(budget/cycle['seconds_per_cycle'])
            if profile.get('entry_requirements_consumed') is True:
                for requirement in stage['requirements']:
                    if requirement['difficulty_mask'] & (1 << diff['value']):
                        cycle_count = min(cycle_count, max(0, math.floor(_number(items.get(requirement['item_id']), 0)/requirement['count'])))
            rate = cycle['expected_xp_per_cycle']/cycle['seconds_per_cycle']*3600
            count = min(count, math.floor(cycle_count*cycle['success_probability']))
            failure_budget_xp = cycle_count*cycle['expected_xp_per_cycle']
            reasons.append('已按当前配装成功/失败观测估算周期收益，含失败前保留经验；少量样本仍有波动。')
        if failures and not cycle:
            reasons.append('当前配装有失败记录，收益按成功全清估算。')
        else:
            reasons.append('生存和技能机制尚未充分校准。')
        if timing.get('overhead_source') == 'observed':
            reasons.append('重开开销使用连续刷图观察的中位值。')
        reasons.append('普通随机波按资源池权重与等级区间求期望；BOSS替换末波。')
        reasons.append('已验证经验公式；耗时按平均伤害粗算并自动校准。' if timing['calibrated'] else '已验证经验公式；耗时按平均伤害粗算，随正常刷图自动校准。')
        if not (profile.get('timing') or {}).get('approach_seconds_per_wave'):
            reasons.append('移动、施法覆盖及重开开销会影响实际速度。')
        rows.append({'map_id': stage['map_id'], 'stage': stage['stage'], 'family': stage['family'], 'difficulty': key,
                     **features, **timing, 'seconds_per_run': seconds, 'xp_per_hour': rate,
                     'projected_xp': rate*minutes/60, 'completed_runs': count, 'completed_runs_xp': count*features['xp_per_run'],
                     'effective_budget_xp': failure_budget_xp if failure_budget_xp is not None else count*features['xp_per_run'],
                     'accessible': accessible, 'boss': stage['boss'], 'risk': risk,
                     'confidence': 'calibrated' if timing['calibrated'] and timing['samples'] >= 3 else 'estimated',
                     'reasons': reasons, 'combat': combat, 'model_valid': True, 'cycle_estimate': cycle})
    sort_key = 'xp_per_hour' if options.get('sort') == 'rate' else 'effective_budget_xp'
    rows.sort(key=lambda r: (r[sort_key], r['xp_per_hour'], r['map_id']), reverse=True)
    eligible = [r for r in rows if r['accessible'] is True and (options.get('sort') == 'rate' or r['completed_runs'] >= 1) and r['model_valid']]
    # An observed failure is evidence to prefer successful alternatives. No
    # invented probability or blanket level-gap safety threshold is applied.
    safe = [r for r in eligible if r['risk'] != 'observed_failure' or r['cycle_estimate'] is not None]
    best = (safe or eligible)[0] if eligible else None
    reason = '' if best else '当前可进入关卡在指定时长内不足一次完整通关。' if any(r['accessible'] is True for r in rows) else '可进入关卡尚未确认，请等待当前角色读取。'
    excluded = [{'map_id': m['map_id'], 'stage': m['stage'], 'reason': m.get('unsupported_reason', '生成参数尚未验证。')} for m in catalog['maps'] if not m['supported']]
    coverage = {'supported': sum(m['supported'] for m in catalog['maps']), 'total': len(catalog['maps']), 'excluded': excluded}
    return {'available': True, 'reason': reason, 'rows': rows, 'best': best, 'coverage': coverage,
            'warnings': [f"常规{coverage['supported']}图支持；特殊/神话图依赖动态参数，暂不参与排序。", '耗时按平均伤害粗算，并从正常刷图自动校准；排名接近时可参考前几名备选。', '预算完整通关收益不计未完成最后一轮，升级或换配装后会重新计算。']+combat['model_warnings'],
            'combat': combat, 'minutes': minutes, 'sort': options.get('sort', 'completed')}
