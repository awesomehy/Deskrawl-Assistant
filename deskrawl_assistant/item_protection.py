"""Non-configurable retention policy shared by planning and native actions."""
import re


def _names(row):
    return tuple(v for key in ('internal_name','name_key') if isinstance(v := row.get(key),str))


def is_rune(row):
    return (row.get('kind') == 'rune' or row.get('item_class') == 'RuneData'
            or any(name.startswith('LegendaryRune_') or re.search(r'SetRune\d',name) for name in _names(row)))


def is_minion_equipment(row):
    """The native slot or shipped ItemData family identifies servant gear."""
    return (row.get('equip_slot') == 12 or row.get('slot') == 'Minion'
            or row.get('is_minion_equipment') is True
            or any(re.fullmatch(r'(?:Normal|Uncommon|Rare|Legendary|Divine)MinionGear\d+', name)
                   for key in ('internal_name', 'name_key', 'key')
                   if isinstance(name := row.get(key), str)))


def divine_equipment(row):
    return (row.get('is_divine') is True or
            (row.get('is_equipment') is True or row.get('kind') == 'equipment')
            and any(type(row.get(k)) is int and row[k] == 4 for k in ('rarity','base_rarity')))


def mandatory_storage(row):
    """Positive protection evidence always wins over a contradictory field."""
    if divine_equipment(row): return '神圣装备'
    if row.get('is_black_mist') is True: return '黑雾装备'
    names = _names(row)
    rarities = [row[k] for k in ('rarity','base_rarity') if type(row.get(k)) is int]
    if any(re.search(r'SetRune\d',name) for name in names) or is_rune(row) and 5 in rarities:
        return '套装符文'
    if any(name.startswith('LegendaryRune_') for name in names) or is_rune(row) and any(n in (3,4) for n in rarities):
        return '宏伟符文'
    return ''


def disposal_protection(row):
    reason = mandatory_storage(row)
    if reason: return reason+'必须收纳，禁止出售或分解。'
    if is_rune(row):
        rarity = row.get('rarity',row.get('base_rarity'))
        if type(rarity) is not int or rarity not in (0,1,2):
            return '符文品质未确认，禁止出售或分解。'
    return ''


def town_rewards_clear(observation):
    """A failed or incomplete read must never be interpreted as empty."""
    if observation.get('scene',{}).get('in_town') is not True: return False
    for key in ('ground','carriage'):
        data = observation.get(key)
        if not isinstance(data,dict) or data.get('complete') is not True or data.get('items') != []:
            return False
    return not any(mandatory_storage(row) for row in observation.get('items',[])
                   if row.get('container') == 'inventory')
