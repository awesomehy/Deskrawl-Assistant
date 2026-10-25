"""Pure opt-in auction policy. Native eligibility is rechecked at submission."""
from copy import deepcopy

from .models import ValidationError

CATEGORIES = {
    'divine_equipment': '神圣装备', 'black_mist_equipment': '黑雾装备',
    'set_rune': '符文套装', 'grand_rune': '宏伟符文',
    'legendary_rein': '传说缰绳', 'consecration_stone': '开光石', 'gold_bar': '金条',
}

DEFAULT_AUCTION = {
    'upload_enabled': False, 'sell_enabled': False, 'price_rule': 'lowest_sell',
    'categories': [], 'item_keys': [], 'excluded_keys': [],
}


def validate_settings(value, available_keys=None):
    if not isinstance(value, dict):
        raise ValidationError('拍卖行配置字段不完整。')
    value = deepcopy(value)
    # Retired development controls no longer gate managed uploads.
    value.pop('allow_locked', None)
    value.pop('retain_count', None)
    value.setdefault('excluded_keys', [])
    value.setdefault('categories', [])
    if set(value) != set(DEFAULT_AUCTION):
        raise ValidationError('拍卖行配置字段不完整。')
    if any(type(value[k]) is not bool for k in ('upload_enabled', 'sell_enabled')):
        raise ValidationError('拍卖行开关必须为布尔值。')
    if value['price_rule'] not in ('lowest_sell', 'highest_buy'):
        raise ValidationError('Steam 市场价格规则无效。')
    for field in ('categories', 'item_keys', 'excluded_keys'):
        keys = value[field]
        if (not isinstance(keys, list) or len(keys) > 4096 or len(set(k for k in keys if isinstance(k, str))) != len(keys)
                or any(not isinstance(k, str) or not k or len(k) > 256 for k in keys)):
            raise ValidationError('拍卖行物品名单无效。')
    if not set(value['categories']) <= set(CATEGORIES):
        raise ValidationError('拍卖行商品类别无效。')
    # Retired item-specific permissions must never remain active behind the
    # category-only UI. Do not broaden them into whole categories on migration.
    if value['item_keys'] and not value['categories']:
        value['upload_enabled'] = False
        value['sell_enabled'] = False
    value['item_keys'] = []
    # Sale is the second step of managed upload. Do not implicitly authorize
    # removing items from the game when migrating an old sale-only setting.
    if not value['upload_enabled']:
        value['sell_enabled'] = False
    if enabled(value) and not value['categories']:
        raise ValidationError('请先选择允许上传或出售的商品类别。')
    return value


def enabled(settings):
    value = settings.get('auction', settings)
    return value.get('upload_enabled') is True


def key(row):
    return row.get('internal_name') or row.get('name_key')


def category(row):
    """Classify only known tradable families; native eligibility is separate."""
    name = key(row) or row.get('key') or ''
    if row.get('is_black_mist') is True or row.get('variant') == 'black_mist' or row.get('quality') == 'black_mist':
        return 'black_mist_equipment'
    if name.startswith('Divine'):
        return 'divine_equipment'
    if 'SetRune' in name:
        return 'set_rune'
    if name.startswith('LegendaryRune_'):
        return 'grand_rune'
    if name.startswith('Rein_') and row.get('rarity', row.get('base_rarity')) == 3:
        return 'legendary_rein'
    return {'GoldBar': 'gold_bar', 'UpgradeCommodity1': 'consecration_stone'}.get(name)


def allows(config, row):
    name = key(row) or row.get('key')
    return (bool(name) and name not in config.get('excluded_keys', [])
            and category(row) in config.get('categories', []))


def quantity(row):
    count = row.get('count', 1)
    return count if type(count) is int and count > 0 else 0


def eligible(row, settings):
    config = settings.get('auction', settings)
    return (config.get('upload_enabled') is True and allows(config, row)
            and row.get('auction_candidate') is True
            and row.get('auction_identity_verified', row.get('identity_verified')) is True
            and isinstance(row.get('item_uid'), str) and bool(row['item_uid'])
            and row.get('container') in ('inventory', 'storage')
            and row.get('is_equipped') is not True and row.get('bound') is not True
            and not row.get('issues') and quantity(row) == 1)


def next_action(settings, observation):
    """Collect rewards/protect equipment before calling; never writes itself."""
    config = settings.get('auction', settings)
    state = observation.get('auction', {})
    if (config.get('upload_enabled') is not True or observation.get('complete') is not True
            or observation.get('scene', {}).get('in_town') is not True
            or state.get('available') is not True or state.get('busy') is True
            or state.get('steamid_verified') is not True):
        return None
    if state.get('pending') is True:
        if state.get('needs_refresh') is True:
            return {'kind': 'auction_open', 'reason_code': 'auction_reconcile',
                    'reason': '刷新拍卖行列表，核验上次在途上传；不重复上传物品。'}
        return None
    items = observation.get('items', [])
    blocked = set(state.get('blocked_uids', []))
    blocked_keys = set(state.get('blocked_keys', []))
    candidates = [r for r in items if eligible(r, config) and r['item_uid'] not in blocked and key(r) not in blocked_keys]
    if state.get('needs_refresh') is True:
        if candidates:
            return {'kind': 'auction_open', 'reason_code': 'auction_refresh',
                    'reason': '打开游戏拍卖行，读取服务器许可和剩余名额。'}
        return None
    if state.get('enabled') is not True:
        return None
    limit = state.get('listing_limit', state.get('capacity', 8))
    count = state.get('listing_count')
    if count is None and isinstance(state.get('listings'), list):
        count = sum(row.get('received') is not True for row in state['listings'])
    if type(limit) is not int or not 1 <= limit <= 8 or type(count) is not int or not 0 <= count < limit:
        return None
    candidates.sort(key=lambda r: (r.get('container') != 'inventory', str(key(r)), r.get('slot_index', 0)))
    for row in candidates:
        # Withdraw first, then re-read before uploading this exact item.
        if row['container'] == 'storage':
            counts = observation.get('counts', {}).get('inventory', {})
            capacity = counts.get('capacity', counts.get('slot_count'))
            occupied = counts.get('occupied', counts.get('occupied_count'))
            if type(capacity) is not int or type(occupied) is not int:
                continue
            free = capacity - occupied
            if type(free) is not int or free <= 0:
                continue
            if row.get('slot_index') not in observation.get('storage', {}).get('allowed_indices', []):
                continue
            return {'kind': 'transfer', 'item': deepcopy(row), 'target': 'inventory',
                    'reason_code': 'auction_prepare', 'reason': '将白名单物品取至背包，核验后上传拍卖行。'}
        return {'kind': 'auction_upload', 'item': deepcopy(row), 'quantity': 1,
                'reason_code': 'auction_upload',
                'reason': '按白名单上传至游戏拍卖行／Steam 库存。'}
    return None
