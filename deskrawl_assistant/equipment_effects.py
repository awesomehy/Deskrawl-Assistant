"""Counterfactual changes for exact-build serialized equipment stat effects.

Only numeric stat modifiers and conversions are supported. Triggered skills,
status stacks and combat procs are deliberately outside the fixed-boss model.
"""
from functools import lru_cache
import json
import math

from .paths import RESOURCE_ROOT


@lru_cache(maxsize=1)
def effect_catalog():
    data = json.loads((RESOURCE_ROOT/'data/loot-item-ui.json').read_text(encoding='utf-8'))
    if data.get('comparison_effects_schema') != 1:
        raise ValueError('装备属性特效目录尚未适配。')
    return {'metadata':data['metadata_sha256'], 'assembly':data['comparison_game_assembly_sha256'],
            'items':{name:row['comparison_effects'] for name,row in data['items'].items()
                     if row.get('comparison_effects')}}


def effects_supported(profile):
    catalog = effect_catalog()
    return (isinstance(profile, dict) and profile.get('metadataSha256') == catalog['metadata']
            and profile.get('gameAssemblySha256') == catalog['assembly'])


def _active(condition, distance):
    # Same fixed scene as character_reader._conditional: full health, one boss,
    # in combat, and class-specific distance. No status stacks or temporary buffs.
    if condition in (-1, 1, 3, 4): return True
    if condition in (0, 5): return False
    if condition == 2: return distance > 6
    raise ValueError('装备属性特效条件尚未适配。')


def _effects(rows, catalog, distance):
    result = {}
    for row in rows:
        key = row.get('name_key') or row.get('internal_name')
        for effect in catalog['items'].get(key, ()):
            kind = effect['class']
            if kind == 'EquipmentEffect_StatModifier':
                if effect['required_status'] or not _active(effect['condition'], distance): continue
            elif kind != 'EquipmentEffect_StatConversion':
                raise ValueError('装备属性特效类型尚未适配。')
            # ItemData references shared effect assets. The same active effect
            # must stay present if another equipped slot still supplies it.
            result[(kind, effect['id'])] = effect
    return result


def apply_equipment_effect_changes(model, before, after, distance):
    """Adjust a copied model by changed effects across the entire loadout."""
    catalog = effect_catalog()
    old, new = _effects(before, catalog, distance), _effects(after, catalog, distance)
    if old.keys() == new.keys(): return
    if model.get('equipment_effects_verified') is False:
        raise ValueError('当前游戏构建的装备属性特效尚未适配，暂不自动比较。')
    for sign, changes in ((-1, old.keys()-new.keys()), (1, new.keys()-old.keys())):
        for identity in sorted(changes):
            effect = (old if sign < 0 else new)[identity]
            if effect['class'] == 'EquipmentEffect_StatConversion':
                source, target, fraction = effect['source'], effect['target'], effect['fraction']
                conversions = list(model.get('conversions', ()))
                if sign > 0:
                    conversions.append((source, target, fraction))
                else:
                    index = next((n for n,(s,t,v) in enumerate(conversions)
                                  if s == source and t == target and math.isclose(v, fraction, rel_tol=1e-7, abs_tol=1e-9)), None)
                    if index is None: raise ValueError('穿戴装备的属性转换未完整读取，等待重新比较。')
                    conversions.pop(index)
                model['conversions'] = conversions
                continue
            for modifier in effect['modifiers']:
                stat, kind, value = modifier['stat'], modifier['type'], modifier['value']
                if effect['condition'] == -1:
                    layer = model['caches'].setdefault(536 if kind == 0 else 544, {})
                else:
                    layer = model.setdefault('conditional', ({}, {}))[kind]
                layer[stat] = layer.get(stat, 0) + sign * value
