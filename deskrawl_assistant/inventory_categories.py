"""Display/filter categories, independent of native action item kinds."""
import re

from .synthesis import rune_quality


RARITIES = {0: '普通', 1: '优秀', 2: '稀有', 3: '传说', 4: '神圣', 5: '符文套装'}
GEMS = ('粗糙', '碎裂', '粗砺', '抛光', '无瑕', '璀璨', '完美之星')


def inventory_category(row, kind, metadata):
    """Do not change `kind`: planners still use their established kinds."""
    key = row.get('internal_name') or row.get('name_key') or ''
    rarity = row.get('base_rarity', row.get('rarity', metadata.get('rarity')))
    if type(rarity) is not int or rarity not in RARITIES:
        rarity = None
    category = kind
    label = {'equipment': '角色装备', 'gem': '宝石', 'rune': '符文',
             'chest': '宝箱', 'material': '材料 / 其他'}.get(kind, '其他物品')
    quality = None
    rarity_label = RARITIES.get(rarity, '稀有度未确认')
    if kind == 'gem':
        match = re.fullmatch(r'Gem[A-Za-z]+([1-7])', key)
        if match:
            quality = 'gem-' + match[1]
            rarity_label = GEMS[int(match[1]) - 1]
    elif kind == 'rune':
        quality = rune_quality(dict(row, internal_name=key, rarity=rarity))
        if quality:
            rarity_label = {'uncommon': '优秀符文', 'rare': '稀有符文',
                            'grand': '宏伟符文', 'set': '符文套装'}[quality]
            quality = 'rune-' + quality
    elif kind == 'material':
        if key == 'GoldBar': category, label = 'gold_bar', '金条'
        elif key == 'UpgradeCommodity1': category, label = 'consecration_stone', '开光石'
        elif key.startswith('Rein_'): category, label = 'rein', '缰绳'
        elif re.match(r'(?:Common|Uncommon|UnCommon|Rare)Fish', key): category, label = 'fish', '鱼类'
        elif (row.get('item_type') or metadata.get('item_type')) == 1:
            category, label = 'craft_material', '合成 / 入场材料'
        elif (row.get('item_type') or metadata.get('item_type')) == 2:
            category, label = 'consumable', '药水 / 消耗品'
    return {'item_category': category, 'kind_label': label, 'rarity': rarity,
            'rarity_label': rarity_label, 'quality_key': quality}
