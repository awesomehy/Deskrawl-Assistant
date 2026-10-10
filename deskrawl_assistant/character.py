"""Pure character attributes and counterfactual panel attack calculation.

EquipmentUI.gdw/gdx, Player.bjz and gj.dqn are verified against the shipped
compatibility baseline. A preview uses a fixed single-boss comparison model
and replaces only upgraded affixes and socketed gems. It never equips
anything and does not predict new legendary effects, skill levels or combat DPS.
"""
from copy import deepcopy
import math
from .native_memory import MemoryReadError
from .stat_display import _single, is_percent_stat, upgraded_value

EQUIPPED_SLOTS = ('Weapon', 'Helm', 'Chest', 'Pants', 'Boots', 'Belt',
                  'Ring1', 'Ring2', 'Necklace', 'Shoulder', 'Gloves', 'Back')
SLOT_LABELS = ('武器', '头盔', '胸甲', '裤子', '靴子', '腰带',
               '戒指 1', '戒指 2', '项链', '护肩', '手套', '披风')
PANEL_GROUPS = (
    ((65, 1), *((n, .4) for n in range(23, 29)), (62, .25)),
    ((29, .35), (30, .2), (32, .25), (58, .5), (31, .25),
     (43, .15), (45, .25), (46, .25), (63, .25), (40, .25)),
    ((33, .5), (34, .25), (61, .25)),
)
PREVIEW_NOTE = '固定单体 Boss 场景：角色满血、一个 Boss、无临时增益；近战 2 米、远程 7 米。沿用攻击力面板公式估算，仅替换词条和已有宝石；传奇特效、技能触发变化不预测，不自动转移宝石。'


def boss_distance(class_mask):
    return 7 if class_mask in (2, 4) else 2


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e18:
        raise ValueError('装备或角色数值尚未完整读取。')
    return value


def attribute_text(stat, value):
    value = number(value)
    percent = is_percent_stat(stat)
    digits = 1 if percent else 2 if stat in (10, 21) else 0
    text = f'{value * 100 if percent else value:.{digits}f}'
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text + ('%' if percent else '')


def final_stats(model):
    """Compose layers including sampled, active conditional bonuses.

Conditional layers are already filtered by the reader. Recursive conversions
use capped source values; unexpected cycles are refused, never turned to zero.
"""
    base, caches = model['base'], model['caches']
    flat, percent, product = model['status']
    conditions = model.get('conditional', ({}, {}))
    result, visiting = {}, set()

    def stat(key):
        if key in result:
            return result[key]
        if key in visiting:
            raise ValueError('属性转换存在循环，暂不能计算换装。')
        visiting.add(key)
        value = base.get(key, 0) + sum(caches.get(off, {}).get(key, 0) for off in (536, 568, 632, 648))
        if key == 8:
            value += stat(0)
        elif key == 9:
            value += stat(2)
        elif key == 60:
            dex = stat(1)
            if dex + 1500 == 0:
                raise ValueError('敏捷数值无效。')
            value += dex / (dex + 1500)
        value += sum(stat(src) * coef for src, target, coef in model.get('conversions', ()) if target == key)
        value += conditions[0].get(key, 0)
        bonus = sum(caches.get(off, {}).get(key, 0) for off in (544, 576, 640)) + conditions[1].get(key, 0)
        value *= 1 + bonus
        if key in (7, 8, 9, 10):
            value *= 1 + stat({7: 42, 8: 44, 9: 59, 10: 57}[key])
        value = (value + flat.get(key, 0)) * (1 + percent.get(key, 0)) * product.get(key, 1)
        number(value)
        if key in (4, 13, 22, 37):
            value = min(value, .85)
        elif key == 60:
            value = min(value, 1)
        result[key] = value
        visiting.remove(key)
        return value

    for key in range(66):
        stat(key)
    return result


def attack_speed(stats):
    return _single(_single(stats[21] if stats[21] > 0 else 1) * _single(1 + _single(stats[6])))


def panel_attack(stats, primary):
    if primary not in (0, 1, 2):
        raise ValueError('职业主属性尚未确认。')
    score = (int(stats[20]) + stats[3]) * (1 + stats[primary] / 100)
    score *= attack_speed(stats)
    score *= 1 + _single(_single(stats[4]) * _single(stats[5]))
    for group in PANEL_GROUPS:
        score *= 1 + sum(stats[key] * weight for key, weight in group)
    return int(number(score))


def item_bonuses(row, item_ui):
    """Keep the original modifier index/count when applying upgrades."""
    mods = row.get('modifiers')
    if row.get('issues') or row.get('modifiers_complete') is not True or not isinstance(mods, list):
        raise ValueError('装备词条尚未完整读取。')
    upgrade, rarity, sockets = row.get('upgrade_level'), row.get('rarity'), row.get('socket_count')
    if any(type(v) is not int or v < 0 for v in (upgrade, rarity, sockets)) or upgrade > 1000 or sockets > 64:
        raise ValueError('强化、稀有度或孔位数尚未确认。')
    result = ({}, {})
    for index, mod in enumerate(mods):
        stat, kind = mod.get('stat'), mod.get('type')
        if type(stat) is not int or stat not in range(66) or type(kind) is not int or kind not in (0, 1):
            raise ValueError('该装备存在尚未支持的词条计算方式。')
        value = upgraded_value(number(mod.get('value')), index, len(mods), upgrade, rarity)
        result[kind][stat] = result[kind].get(stat, 0) + value
    gems = row.get('socketed_gems')
    if gems is None and sockets:
        raise ValueError('镶嵌宝石尚未读取，暂不能比较。')
    if not isinstance(gems or [], list) or len(gems or []) > sockets:
        raise ValueError('镶嵌宝石数量与孔位不一致。')
    slot = row.get('equip_slot')
    category = 2 if slot == 1 else 1 if slot in (7, 8) else 0
    for gem in gems or []:
        if not gem:
            continue
        groups = item_ui.get('items', {}).get(gem, {}).get('effect_groups', [])
        if len(groups) != 3 or groups[category].get('texts'):
            raise ValueError('镶嵌宝石效果尚未适配。')
        for mod in groups[category]['values']:
            stat, kind = mod.get('stat'), mod.get('modifier')
            if type(stat) is not int or stat not in range(66) or type(kind) is not int or kind not in (0, 1):
                raise ValueError('镶嵌宝石效果尚未适配。')
            result[kind][stat] = result[kind].get(stat, 0) + number(mod.get('value'))
    return result


def preview(row, character, item_ui):
    if not character.get('available'):
        return {'available': False, 'reason': character.get('reason') or '等待读取角色。'}
    if not row.get('is_equipment'):
        return {'available': False, 'reason': '该物品不参与换装比较。'}
    mask = row.get('class_mask')
    if type(mask) is not int or not mask & character['class_mask']:
        return {'available': False, 'incompatible': True, 'reason': '当前职业无法装备。'}
    targets = {1: (0,), 2: (1,), 3: (2,), 4: (3,), 5: (4,), 6: (5,),
               7: (6, 7), 8: (8,), 9: (10,), 10: (9,), 11: (11,)}.get(row.get('equip_slot'))
    if not targets:
        return {'available': False, 'reason': '装备部位尚未适配。'}
    try:
        added = item_bonuses(row, item_ui)
        current = final_stats(character['comparison_model'])
        before = panel_attack(current, character['primary'])
        equipped = {i['equipped_slot']: i for i in character['equipment']}
        comparisons = []
        for target in targets:
            old = equipped.get(target)
            removed = item_bonuses(old, item_ui) if old else ({}, {})
            model = deepcopy(character['comparison_model'])
            for kind, off in ((0, 536), (1, 544)):
                layer = model['caches'].setdefault(off, {})
                for stat in set(removed[kind]) | set(added[kind]):
                    layer[stat] = layer.get(stat, 0) - removed[kind].get(stat, 0) + added[kind].get(stat, 0)
            stats = final_stats(model)
            after = panel_attack(stats, character['primary'])
            changes = [{'stat': n, 'before': current[n], 'after': stats[n],
                        'before_text': attribute_text(n, current[n]), 'after_text': attribute_text(n, stats[n])}
                       for n in range(66) if abs(stats[n] - current[n]) > 1e-5]
            comparisons.append({'target': target, 'target_label': SLOT_LABELS[target],
                'replaced_uid': old.get('item_uid') if old else None, 'before': before, 'after': after,
                'delta': after - before, 'percent': (after - before) / before * 100 if before else None,
                'attributes': changes})
        best = max(comparisons, key=lambda c: c['delta'])
        return {'available': True, 'estimated': True, **best, 'comparisons': comparisons, 'note': PREVIEW_NOTE}
    except (ValueError, MemoryReadError, KeyError, TypeError, OverflowError) as exc:
        return {'available': False, 'reason': str(exc)}
