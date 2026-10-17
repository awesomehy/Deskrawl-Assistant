"""Pure translations of the verified EquipmentManager.dds/ddt level checks."""


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(label + '尚未完整读取，暂不能自动穿戴。')
    return value


def required_level(row, max_level):
    """Mirror native priority: black mist, back slot, base rarity, item level.

    GameConfig.MaxPlayerLevel caps ItemLevel / 10. Generated rarity must not
    replace ItemData.Rarity when deciding the divine equipment requirement.
    """
    maximum = _integer(max_level, 1, 10000, '角色等级上限')
    mist = row.get('is_black_mist')
    if type(mist) is not bool:
        raise ValueError('装备黑雾状态尚未完整读取，暂不能自动穿戴。')
    if mist:
        return maximum
    slot = _integer(row.get('equip_slot'), 1, 11, '装备部位')
    if slot == 11:
        return 1
    rarity = _integer(row.get('base_rarity'), 0, 5, '装备基础品质')
    if rarity == 4:
        return maximum
    level = _integer(row.get('item_level', row.get('level')), -(2**31), 2**31 - 1, '装备等级')
    return min(maximum, max(1, level // 10)) if level > 0 else 1


def equip_eligibility(row, character, *, target_slot=None):
    """Unknown inputs never authorize a managed equip or preparation transfer."""
    result = {'available': False, 'can_equip': False, 'required_level': None,
              'character_level': None, 'reason': ''}
    try:
        if not isinstance(character, dict) or character.get('available') is not True:
            raise ValueError('角色等级尚未完整读取，暂不能自动穿戴。')
        level = _integer(character.get('level'), 1, 10000, '角色等级')
        needed = required_level(row, character.get('max_level'))
        result.update(available=True, can_equip=level >= needed,
                      required_level=needed, character_level=level)
        if level < needed:
            result['reason'] = f'角色 Lv.{level}，装备需要 Lv.{needed}，暂不能穿戴。'
            return result
        # Native EquipmentManager.ddv rejects black-mist instances until
        # BlackMistRevealState.Finalized. Readable affixes do not imply that
        # reveal has completed, and unknown state cannot authorize a swap.
        if row.get('is_black_mist') is True and row.get('black_mist_finalized') is not True:
            known = type(row.get('black_mist_finalized')) is bool
            result.update(available=known, can_equip=False, black_mist_blocked=True,
                reason='黑雾装备尚未完成揭示，保留收纳。' if known else '黑雾装备揭示状态尚未确认，保留收纳。')
            return result
        if target_slot is not None:
            _integer(target_slot, 0, 11, '目标装备槽位')
            if row.get('equip_slot') == 7 and target_slot in (6, 7):
                rarity = _integer(row.get('rarity'), 0, 4, '装备品质')
                if rarity >= 3:
                    key = row.get('name_key') or row.get('internal_name')
                    worn = character.get('equipment')
                    if not isinstance(key, str) or not key or not isinstance(worn, list):
                        raise ValueError('戒指种类或当前穿戴状态尚未完整读取。')
                    others = [item for item in worn if isinstance(item, dict)
                              and item.get('equipped_slot') == (7 if target_slot == 6 else 6)]
                    if len(others) > 1:
                        raise ValueError('当前戒指槽位读取不一致。')
                    if others:
                        other_key = others[0].get('name_key') or others[0].get('internal_name')
                        if not isinstance(other_key, str) or not other_key:
                            raise ValueError('另一枚戒指种类尚未完整读取。')
                        if other_key == key:
                            result.update(can_equip=False, duplicate_ring=True,
                                reason='同种传说戒指不能同时穿戴两枚。')
                            return result
    except (ValueError, AttributeError, TypeError) as exc:
        result.update(available=False, can_equip=False)
        result['reason'] = str(exc)
    return result
