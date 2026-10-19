"""Read-only reward decisions. Planned drops are never actual loot receipts."""
from copy import deepcopy
import hashlib
import json

from .loot_plan_reader import MAX_ITEMS, MAX_ITEMS_PER_WAVE, MAX_WAVES
from .mythic import MYTHIC, WORLD_DIFFICULTIES, floor_number

from .models import ValidationError


REWARDS = {
    'divine_equipment': '神圣装备',
    'black_mist_equipment': '黑雾传说装备',
    'primary_perfect': '传说装备 · 四词条全对',
    'rule_match': '传说装备 · 满足筛选规则',
    'set_rune': '符文套装',
    'legendary_rune': '宏伟符文',
    'rare_rune': '稀有符文',
    'uncommon_rune': '优秀符文',
    'gem': '任意宝石',
    'silver_chest': '银色宝箱',
    'golden_chest': '黄金宝箱',
    'mythic_chest': '神话宝箱',
    'legendary_rein': '传奇缰绳',
    'gold_bar': '金条',
}
FIXED_REWARDS = frozenset(('divine_equipment', 'black_mist_equipment', 'set_rune', 'legendary_rune'))
DEFAULT_STARGAZING = {'enabled': False, 'rewards': dict.fromkeys(REWARDS, True)}


def validate_stargazing(value):
    if not isinstance(value, dict) or set(value) - {'enabled', 'rewards'}:
        raise ValidationError('观星配置格式无效。')
    result = deepcopy(DEFAULT_STARGAZING)
    result.update(value)
    if type(result['enabled']) is not bool or not isinstance(result['rewards'], dict):
        raise ValidationError('观星开关必须为布尔值。')
    if set(result['rewards']) - set(REWARDS) or any(type(v) is not bool for v in result['rewards'].values()):
        raise ValidationError('观星奖励选项无效。')
    result['rewards'] = {**DEFAULT_STARGAZING['rewards'], **result['rewards']}
    for key in FIXED_REWARDS:
        result['rewards'][key] = True
    return result


def enabled(settings):
    farm = settings.get('farming', {})
    return farm.get('enabled') is True and farm.get('mode') == 'repeat' and farm.get('stargazing', {}).get('enabled') is True


def combat_rewards_clear(observation):
    """Never abandon actual ground/carriage drops, even when they are cheap."""
    scene = observation.get('scene') or {}
    return (scene.get('available') is True and scene.get('in_town') is False
            and scene.get('dead') is not True
            and all(isinstance(observation.get(key), dict)
                    and observation[key].get('complete') is True
                    and observation[key].get('items') == [] for key in ('ground', 'carriage')))


def combat_rewards_known(observation):
    """Returning to town preserves actual drops for the normal cleanup path."""
    scene = observation.get('scene') or {}
    return (scene.get('available') is True and scene.get('in_town') is False
            and scene.get('dead') is not True
            and all(isinstance(observation.get(key), dict)
                    and observation[key].get('complete') is True
                    and isinstance(observation[key].get('items'), list) for key in ('ground', 'carriage')))


def reward_categories(item):
    kind, rarity = item.get('kind'), item.get('rarity')
    key = item.get('item_key', '')
    if kind == 'equipment':
        if rarity == 4: return ['divine_equipment']
        if item.get('black_mist') is True: return ['black_mist_equipment']
        if rarity == 3:
            return [category for category, field in (('primary_perfect', 'primary_perfect'), ('rule_match', 'matches'))
                    if item.get(field) is True]
    if kind == 'rune':
        if rarity == 5 or key.startswith('SetRune'): return ['set_rune']
        if rarity in (3, 4) or key.startswith('LegendaryRune_'): return ['legendary_rune']
        return {2: ['rare_rune'], 1: ['uncommon_rune']}.get(rarity, [])
    if kind == 'gem': return ['gem']
    if kind == 'chest':
        return {'TreasureChest1': ['silver_chest'], 'TreasureChest2': ['golden_chest'],
                'TreasureChest3': ['mythic_chest']}.get(key, [])
    if kind == 'material':
        if key == 'GoldBar': return ['gold_bar']
        if isinstance(key, str) and key.startswith('Rein_') and type(rarity) is int and rarity == 3:
            return ['legendary_rein']
    return []


# Retained from the 1.2.8 offline prototype: full-plan validation and a
# content fingerprint independent of run IDs and display decorations.
def _complete_plan(preview, expected_map_id, previous_run_key):
    if not isinstance(preview, dict) or preview.get('available') is not True or preview.get('phase') != 'active':
        raise ValueError('本局完整掉落计划尚未可用，继续等待。')
    run, waves, totals = preview.get('run'), preview.get('waves'), preview.get('totals')
    if not isinstance(run, dict) or not isinstance(waves, list) or not isinstance(totals, dict):
        raise ValueError('掉落计划缺少局信息、完整波次或合计。')
    key = run.get('key')
    if not isinstance(key, str) or not key.strip():
        raise ValueError('掉落计划缺少可确认的本局标识。')
    if previous_run_key is not None and key == previous_run_key:
        raise ValueError('仍是上一局掉落计划，等待新局。')
    if not isinstance(run.get('map_id'), str) or not run['map_id']:
        raise ValueError('掉落计划关卡未确认。')
    if expected_map_id is not None and run['map_id'] != expected_map_id:
        raise ValueError('掉落计划关卡与目标关卡不一致。')
    count, current = run.get('total_waves'), run.get('current_wave')
    if type(count) is not int or not 1 <= count <= MAX_WAVES or len(waves) != count:
        raise ValueError('完整波次数不一致，不能判断为无目标掉落。')
    if type(current) is not int or not 0 <= current <= count:
        raise ValueError('当前波次未确认。')
    flattened = []
    for number, wave in enumerate(waves, 1):
        if not isinstance(wave, dict) or type(wave.get('number')) is not int or wave['number'] != number:
            raise ValueError('掉落计划波次不连续。')
        items = wave.get('items')
        if not isinstance(items, list) or len(items) > MAX_ITEMS_PER_WAVE:
            raise ValueError('掉落计划物品列表未完整读取。')
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('item_key'), str) or not item['item_key']:
                raise ValueError('掉落条目缺少物品标识。')
            flattened.append((number, item))
    if len(flattened) > MAX_ITEMS or type(totals.get('items')) is not int or totals['items'] != len(flattened):
        raise ValueError('掉落条目合计不一致，不能判断为无目标掉落。')
    return run, waves, flattened


def _fingerprint(run, waves):
    """Exclude run IDs, wave progress, translations and mutable UI decoration.

    A new run key proves only a new identity. Comparing this content hash is
    necessary when testing whether a re-entry actually changed planned loot.
    Even changed loot does not establish statistical independence/fairness.
    """
    fields = ('item_key', 'kind', 'slot', 'rarity', 'plan_rarity', 'level', 'quantity',
              'raw_qty', 'sockets', 'ancient', 'black_mist', 'chest_gold')
    content = {'map_id': run['map_id'], 'difficulty': run.get('difficulty_label'), 'waves': []}
    for wave in waves:
        rows = []
        for item in wave['items']:
            row = {field: item.get(field) for field in fields}
            modifiers = item.get('modifiers', [])
            if not isinstance(modifiers, list) or any(not isinstance(m, dict) for m in modifiers):
                raise ValueError('掉落计划词条列表不完整。')
            row['modifiers'] = [{k: m.get(k) for k in ('stat', 'type', 'value')}
                                for m in modifiers]
            rows.append(row)
        content['waves'].append({'number': wave['number'], 'gold': wave.get('gold'),
                                'potion_charges': wave.get('potion_charges'), 'items': rows})
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()



def decide(plan, observation, settings):
    """Only an atomic, complete plan for this exact run can yield a miss."""
    waiting = {'verdict': 'unknown', 'reason': '等待本局完整掉落计划，暂不重开。', 'hits': []}
    if not isinstance(plan, dict) or plan.get('available') is not True:
        return {**waiting, 'reason': (plan or {}).get('reason') or waiting['reason']} if isinstance(plan, dict) else waiting
    scene = observation.get('scene') or {}
    run = scene.get('run') or {}
    planned = plan.get('run') or {}
    if (plan.get('session') != observation.get('session') or not run.get('id')
            or planned.get('id') != run['id']
            or str(planned.get('map_id')) != str(scene.get('dungeon_id'))
            or planned.get('difficulty') != scene.get('difficulty')):
        return {**waiting, 'reason': '掉落计划与当前局尚未对应，等待重新核验。'}
    if scene.get('difficulty') == MYTHIC and (
            floor_number(scene.get('dungeon_id')) is None
            or planned.get('mythic_floor') != floor_number(scene['dungeon_id'])
            or planned.get('world_difficulty') not in WORLD_DIFFICULTIES
            or planned['world_difficulty'] != scene.get('world_difficulty')):
        return {**waiting, 'reason': '裂隙层数或世界难度尚未对应，等待重新核验。'}
    try:
        _, waves, flattened = _complete_plan(plan, str(scene.get('dungeon_id')), None)
        fingerprint = _fingerprint(planned, waves)
        waiting['content_fingerprint'] = fingerprint
    except (TypeError, ValueError):
        return waiting
    items = [item for _, item in flattened]
    choices = validate_stargazing(settings)['rewards']
    hits, uncertain = [], False
    for item in items:
        categories = [key for key in reward_categories(item) if choices[key]]
        if categories:
            hits.append({'name': item.get('name') or item.get('item_key'),
                         'categories': categories, 'labels': [REWARDS[key] for key in categories]})
            continue
        kind, rarity = item.get('kind'), item.get('rarity')
        if kind not in ('equipment', 'rune', 'gem', 'chest', 'material'):
            uncertain = True
        elif kind in ('equipment', 'rune') and rarity not in range(6):
            uncertain = True
        elif (kind == 'material' and isinstance(item.get('item_key'), str)
              and item['item_key'].startswith('Rein_') and choices['legendary_rein']
              and (type(rarity) is not int or rarity not in range(6))):
            uncertain = True
        elif kind == 'equipment' and rarity == 3 and (choices['primary_perfect'] or choices['rule_match']):
            if plan.get('rules_known') is not True or item.get('rule_status') == 'review' or 'matches' not in item:
                uncertain = True
    if hits:
        return {'verdict': 'keep', 'reason': '本局命中高价值奖励，正常完成副本。', 'hits': hits, 'content_fingerprint': fingerprint}
    if uncertain:
        return {**waiting, 'reason': '本局有物品或筛选结果待核验，暂不重开。'}
    return {'verdict': 'skip', 'reason': '本局未命中勾选的高价值奖励，准备重开。', 'hits': [], 'content_fingerprint': fingerprint}
