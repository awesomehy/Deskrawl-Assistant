"""Equipment cube policy. Every material is an explicitly selected generated UID."""
from copy import deepcopy
from functools import lru_cache
import json

from .paths import RESOURCE_ROOT


RECIPES = {
    'ancient_to_black_mist': ('AncientLegendary', 'BlackMist', 9),
    'black_mist_to_divine': ('BlackMist', 'Divine', 9),
    'divine_to_ancient': ('Divine', 'AncientDivine', 3),
}
DEFAULT_EQUIPMENT = {'enabled': False, 'include_matched': True,
                     **{key: False for key in RECIPES}}
PRIORITY = tuple(RECIPES)


@lru_cache(maxsize=1)
def ancient_aliases():
    return json.loads((RESOURCE_ROOT / 'data/game-catalog.json').read_text(encoding='utf-8')).get('equipment_asset_aliases', {})


def quality(row):
    key = row.get('internal_name') or row.get('name_key')
    if row.get('is_equipment') is not True:
        return None
    if row.get('is_black_mist') is True:
        return 'BlackMist'
    if key in ancient_aliases():
        return 'AncientDivine'
    if row.get('rarity') == 4:
        return 'Divine'
    if row.get('rarity') == 3 and row.get('is_ancient') is True:
        return 'AncientLegendary'
    return None


def eligible(row, config, settings, allowed, equipped=()):
    key = row.get('internal_name') or row.get('name_key')
    family = {key, ancient_aliases().get(key, key)}
    excluded = settings.get('auction', {}).get('excluded_keys', [])
    slot_locks = settings.get('auto_equip', {}).get('slot_locks', {}).values()
    # Auto-locking is not a material exclusion: the user expressly permits
    # unlocking. A slot lock, an unknown read, or a keep whitelist still is.
    return (row.get('container') in ('inventory', 'storage')
        and (row['container'] != 'storage' or row.get('slot_index') in allowed)
        and row.get('identity_verified') is True and isinstance(row.get('item_uid'), str) and bool(row['item_uid'])
        and bool(row.get('instance_id')) and row.get('count') == 1
        and row.get('is_equipment') is True and not row.get('issues')
        and row.get('movable') is not False and type(row.get('locked')) is bool
        and row.get('slot_locked') is False and row.get('is_equipped') is not True
        and row['item_uid'] not in equipped and not family.intersection(excluded) and not family.intersection(slot_locks)
        and row.get('rules_known') is True
        and type(row.get('upgrade_level')) is int and row['upgrade_level'] >= 0
        and (row.get('review') is False or quality(row) == 'BlackMist')
        and (config['include_matched'] or row.get('matches') is False and row.get('review') is False))


def plan(settings, observation):
    config = settings.get('synthesis', {}).get('equipment', DEFAULT_EQUIPMENT)
    state = observation.get('synthesis', {}).get('equipment', {})
    if (not config.get('enabled') or state.get('available') is not True
            or observation.get('complete') is not True
            or observation.get('scene', {}).get('in_town') is not True
            or any(observation['scene'].get(key) is True for key in ('online_saving', 'scene_changing', 'teleporting'))):
        return None
    counts = observation.get('counts', {}).get('inventory', {})
    capacity, occupied = counts.get('capacity'), counts.get('occupied')
    if type(capacity) is not int or type(occupied) is not int or not 0 <= occupied <= capacity:
        return None
    allowed = observation.get('storage', {}).get('allowed_indices', [])
    if not isinstance(allowed, list):
        return None
    equipped = set(state.get('equipped_uids', [])) | {r.get('item_uid') for r in observation.get('character', {}).get('equipment', [])}
    rows = [r for r in observation.get('items', [])
            if eligible(r, config, settings, allowed, equipped)]
    if len({r['item_uid'] for r in rows}) != len(rows):
        return None
    for recipe in sorted(state.get('recipes', []), key=lambda r: PRIORITY.index(r['option']) if r.get('option') in PRIORITY else len(PRIORITY)):
        name = recipe.get('option')
        if name not in RECIPES:
            continue
        source, target, amount = RECIPES[name]
        if (not config.get(name) or recipe.get('available') is not True or not recipe.get('recipe_id')
                or (recipe.get('input_quality'), recipe.get('output_quality'), recipe.get('input_count')) != (source, target, amount)
                or type(state.get('gold')) is not int or type(recipe.get('gold_cost')) is not int
                or state['gold'] < recipe['gold_cost'] or recipe['gold_cost'] < 0):
            continue
        candidates = [r for r in rows if quality(r) == source
            and type(r.get('item_level')) is int and r['item_level'] >= recipe.get('min_item_level', 0)]
        groups = {}
        for row in candidates:
            key = row.get('internal_name') or row.get('name_key')
            if source == 'Divine' and key not in ancient_aliases().values():
                continue
            groups.setdefault(key if source == 'Divine' else '*', []).append(row)
        for group in groups.values():
            group.sort(key=lambda r: (r['container'] != 'inventory', r.get('upgrade_level') or 0, r['slot_index'], r['item_uid']))
            chosen = group[:amount]
            if len(chosen) != amount:
                continue
            stored = [r for r in chosen if r['container'] == 'storage']
            if len(stored) > capacity - occupied:
                continue
            common = {'recipe_id': recipe['recipe_id'], 'recipe': deepcopy(recipe),
                'input_uids': [r['item_uid'] for r in chosen], 'input_keys': [],
                'inputs': [dict(deepcopy(r), quantity=1) for r in chosen],
                'include_matched': config['include_matched'], 'count': 1,
                'excluded_keys': sorted(settings.get('auction', {}).get('excluded_keys', [])),
                'synthesis_group': 'equipment'}
            if stored:
                return dict(common, kind='transfer', item=deepcopy(stored[0]), target='inventory',
                            reason_code='synthesis_prepare', reason='取出装备合成材料。')
            locked = next((r for r in chosen if r['locked']), None)
            if locked:
                return dict(common, kind='unlock', item=deepcopy(locked), reason_code='synthesis_prepare',
                            reason='按已启用配方解锁选定的装备材料，随后重新核验再合成。')
            return dict(common, kind='synthesize_equipment', reason='按实时装备配方合成选定材料。')
    return None
