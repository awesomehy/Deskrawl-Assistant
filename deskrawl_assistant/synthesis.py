"""Pure synthesis policy. Native recipes and fresh item identities are authoritative."""
from __future__ import annotations

from copy import deepcopy
from .models import ValidationError
from .item_protection import is_rune


DEFAULT_SYNTHESIS = {
    'gems': {'enabled': False, 'max_quality': 5},
    'runes': {'enabled': False, 'max_quality': 'rare', 'excluded_keys': []},
}
RUNE_QUALITIES = ('uncommon', 'rare', 'grand', 'set')
RUNE_RARITIES = {1: 'uncommon', 2: 'rare', 3: 'grand', 4: 'grand', 5: 'set'}
KINDS = ('synthesize_gems', 'synthesize_runes')


def validate_settings(value):
    if not isinstance(value, dict) or set(value) != {'gems', 'runes'}:
        raise ValidationError('自动合成配置格式无效。')
    result = deepcopy(value)
    for name, defaults in DEFAULT_SYNTHESIS.items():
        group = result[name]
        if not isinstance(group, dict) or set(group) != set(defaults) or type(group['enabled']) is not bool:
            raise ValidationError('自动合成配置字段无效：' + name)
    gems, runes = result['gems'], result['runes']
    if type(gems['max_quality']) is not int or gems['max_quality'] not in range(1, 7):
        raise ValidationError('宝石最高合成品质无效。')
    if runes['max_quality'] not in RUNE_QUALITIES:
        raise ValidationError('符文最高合成品质无效。')
    keys = runes['excluded_keys']
    if (not isinstance(keys, list) or len(keys) > 4096
            or any(not isinstance(key, str) or not key or len(key) > 256 for key in keys)
            or len(set(keys)) != len(keys)):
        raise ValidationError('跳过合成的符文列表无效。')
    runes['excluded_keys'] = sorted(keys)
    return result


def enabled(settings):
    config = settings.get('synthesis', DEFAULT_SYNTHESIS)
    return any(config.get(group, {}).get('enabled') is True for group in ('gems', 'runes'))


def item_key(row):
    return row.get('internal_name') or row.get('name_key')


def rune_quality(row):
    key = item_key(row) or ''
    if 'SetRune' in key:
        return 'set'
    if key.startswith('LegendaryRune_'):
        return 'grand'
    if key.startswith('RareRune_'):
        return 'rare'
    if key.startswith('UncommonRune_'):
        return 'uncommon'
    return RUNE_RARITIES.get(row.get('rarity', row.get('base_rarity')))


def _quantity(row):
    value = row.get('count', 1)
    return value if type(value) is int and value > 0 else 0


def _eligible(row, kind, allowed):
    if (row.get('container') not in ('inventory', 'storage') or row.get('issues')
            or row.get('movable') is False or row.get('locked') is not False
            or row.get('slot_locked') is not False
            or _quantity(row) < 1 or row.get('is_equipped') is True):
        return False
    if row['container'] == 'storage' and row.get('slot_index') not in allowed:
        return False
    if kind == 'rune':
        return bool(row.get('item_class') == 'RuneData' and is_rune(row)
                    and row.get('item_uid') and _quantity(row) == 1
                    and row.get('identity_verified') is not False and rune_quality(row))
    return row.get('item_class') == 'GemData'


def _input(row, quantity):
    return dict(deepcopy(row), quantity=quantity)


def _withdraw(row, group):
    return {'kind': 'transfer', 'item': deepcopy(row), 'target': 'inventory',
            'reason_code': 'synthesis_prepare', 'synthesis_group': group,
            'reason': '从已解锁仓库取出自动合成材料。'}


def _gem_action(config, state, rows, allowed, free):
    gold = state.get('gold')
    if type(gold) is not int or gold < 0:
        return None
    recipes = sorted(state.get('recipes', []), key=lambda r: (r.get('input_quality', 99), str(r.get('recipe_id'))))
    for recipe in recipes:
        source, target = recipe.get('input_quality'), recipe.get('output_quality')
        cost, maximum_stack = recipe.get('gold_cost'), recipe.get('output_max_stack')
        if (type(source) is not int or type(target) is not int or not 1 <= source < target <= config['max_quality']
                or target != source + 1 or recipe.get('input_count') != 10 or recipe.get('output_count') != 1
                or type(cost) is not int or cost < 0 or gold < cost or not recipe.get('recipe_id')
                or not recipe.get('input_key') or not recipe.get('output_key')
                or type(maximum_stack) is not int or not 1 <= maximum_stack <= 2**31-1):
            continue
        outputs = [row for row in rows if row.get('container') == 'inventory'
                   and item_key(row) == recipe['output_key']]
        if any(not 1 <= _quantity(row) <= maximum_stack for row in outputs):
            continue
        output_room = free * maximum_stack + sum(maximum_stack - _quantity(row) for row in outputs)
        key = recipe['input_key']
        # The game counts/removes ALL same-asset stacks, including locked ones.
        # A safe aggregate quantity alone cannot establish a safe native call.
        same_bag = [row for row in rows if row.get('container') == 'inventory' and item_key(row) == key]
        if any(not _eligible(row, 'gem', allowed) for row in same_bag):
            continue
        selected = [row for row in rows if item_key(row) == key and _eligible(row, 'gem', allowed)]
        bag = [row for row in selected if row['container'] == 'inventory']
        stored = sorted((row for row in selected if row['container'] == 'storage'), key=lambda row: row['slot_index'])
        quantity = sum(_quantity(row) for row in bag)
        if quantity >= 10 and free >= 1:
            count = min(quantity // 10, gold // cost if cost else quantity // 10,
                        output_room, 100000)
            if count < 1:
                continue
            remaining, inputs = count * 10, []
            # Match Inventory.fdu's reverse-slot traversal exactly.
            for row in sorted(bag, key=lambda r: r.get('slot_index', -1), reverse=True):
                take = min(remaining, _quantity(row))
                if take:
                    inputs.append(_input(row, take)); remaining -= take
            return {'kind': 'synthesize_gems', 'recipe_id': recipe['recipe_id'], 'recipe': deepcopy(recipe),
                    'input_keys': [key], 'input_uids': [], 'inputs': inputs, 'count': count,
                    'max_quality': config['max_quality'], 'excluded_keys': [],
                    'reason': '按最高产出品质批量合成宝石。'}
        if quantity >= 10 or sum(_quantity(row) for row in stored) + quantity < 10:
            continue
        # Reserve enough slots to finish staging plus an output slot. This
        # conservative bound also works when source stacks cannot merge.
        missing, staging = 10 - quantity, []
        for row in stored:
            staging.append(row); missing -= _quantity(row)
            if missing <= 0:
                break
        if missing <= 0 and free >= len(staging) + 1:
            return _withdraw(staging[0], 'gems')
    return None


def _rune_action(config, state, rows, allowed, free):
    if state.get('unlocked') is not True:
        return None
    maximum = RUNE_QUALITIES.index(config['max_quality'])
    excluded = set(config['excluded_keys'])
    equipped = set(state.get('equipped_uids', []))
    recipes = sorted(state.get('recipes', []), key=lambda r: RUNE_QUALITIES.index(r['input_quality'])
                     if r.get('input_quality') in RUNE_QUALITIES else 99)
    for recipe in recipes:
        source, target = recipe.get('input_quality'), recipe.get('output_quality')
        if source not in RUNE_QUALITIES or target not in RUNE_QUALITIES:
            continue
        a, b = RUNE_QUALITIES.index(source), RUNE_QUALITIES.index(target)
        chance = recipe.get('chance')
        cost = recipe.get('gold_cost', 0)
        gold = state.get('gold')
        if (not a < b <= maximum or b != a + 1 or recipe.get('input_count') != 9
                or recipe.get('output_count') != 1 or not recipe.get('recipe_id')
                or type(chance) not in (int, float) or not 0 < chance <= 1
                or recipe.get('available') is False
                or type(cost) is not int or cost < 0
                or cost and (type(gold) is not int or gold < cost)):
            continue
        candidates = [row for row in rows if _eligible(row, 'rune', allowed)
                      and rune_quality(row) == source and item_key(row) not in excluded
                      and row['item_uid'] not in equipped]
        # Never submit duplicated generated identities from a stale snapshot.
        if len({row['item_uid'] for row in candidates}) != len(candidates):
            continue
        bag = sorted((row for row in candidates if row['container'] == 'inventory'),
                     key=lambda r: (r.get('rune_level') or 0, r.get('slot_index', -1), r['item_uid']))
        stored = sorted((row for row in candidates if row['container'] == 'storage'), key=lambda r: r['slot_index'])
        if len(bag) >= 9:
            inputs = [_input(row, 1) for row in bag[:9]]
            return {'kind': 'synthesize_runes', 'recipe_id': recipe['recipe_id'], 'recipe': deepcopy(recipe),
                    'input_keys': [], 'input_uids': [row['item_uid'] for row in bag[:9]],
                    'inputs': inputs, 'count': 1, 'max_quality': config['max_quality'],
                    'excluded_keys': sorted(excluded), 'reason': '按最高产出品质合成九枚同品质符文。'}
        missing = 9 - len(bag)
        if len(stored) >= missing and free >= missing:
            return _withdraw(stored[0], 'runes')
    return None


def next_action(settings, observation):
    """Plan one affordable recipe or one transfer; no memory or game calls."""
    if (not enabled(settings) or observation.get('complete') is not True
            or observation.get('scene', {}).get('in_town') is not True):
        return None
    scene = observation['scene']
    if any(scene.get(flag) is True for flag in ('online_saving', 'scene_changing', 'teleporting')):
        return None
    counts = observation.get('counts', {}).get('inventory', {})
    capacity, occupied = counts.get('capacity'), counts.get('occupied')
    if type(capacity) is not int or type(occupied) is not int or not 0 <= occupied <= capacity:
        return None
    allowed = observation.get('storage', {}).get('allowed_indices', [])
    if not isinstance(allowed, list) or any(type(index) is not int or index < 0 for index in allowed):
        return None
    config, states = settings.get('synthesis', DEFAULT_SYNTHESIS), observation.get('synthesis', {})
    rows = observation.get('items', [])
    for group, planner in (('gems', _gem_action), ('runes', _rune_action)):
        state = states.get(group, {})
        if config[group]['enabled'] and state.get('available') is True:
            action = planner(config[group], state, rows, set(allowed), capacity - occupied)
            if action:
                return action
    return None
