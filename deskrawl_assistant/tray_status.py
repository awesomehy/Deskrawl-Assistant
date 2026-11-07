"""Read-only tray projection from existing worker snapshots and exact receipts."""
from collections import Counter
from copy import deepcopy
import time
import re

from .mythic import MYTHIC, floor_number
from .world_boss import PUMPKIN, DAILY_LIMIT

DIFFICULTIES = {'Normal': '恶魔', 'Nightmare': '噩梦', 'Inferno': '炼狱'}
SKULLS = {'Normal': 'Skull', 'Nightmare': 'SkullNightmare', 'Inferno': 'SkullInferno'}
LOCATIONS = {'inventory': '背包', 'storage': '仓库', 'ground': '地面',
             'carriage': '马车', 'equipped': '已装备'}
SALE_STATES = {'uploaded': '拍卖行已上架', 'pending_confirmation': 'Steam 市场待确认',
               'listed': 'Steam 市场已上架', 'sold': 'Steam 市场已出售', 'uncertain': '上架状态待核验'}


def dungeon_text(control, *, fresh=True):
    if not fresh or control.get('available') is not True:
        return '当前副本：等待读取'
    scene = control.get('scene')
    if isinstance(scene, dict):
        town, combat = scene.get('in_town') is True, scene.get('in_dungeon') is True
    else:
        town, combat = scene == 'town', scene == 'combat'
    if town: return '当前副本：主城'
    if not combat: return '当前副本：未在副本中'
    if control.get('teleporting') is True or control.get('scene_changing') is True:
        return '当前副本：场景切换中'
    map_id, difficulty = control.get('map_id'), control.get('difficulty')
    rows = (control.get('maps_by_difficulty') or {}).get(difficulty, control.get('maps', []))
    option = next((r for r in rows if r.get('map_id', r.get('id')) == map_id), {})
    if map_id == PUMPKIN:
        state = option.get('world_boss') or {}
        remaining = state.get('remaining')
        number = str(remaining) if (state.get('available') is True and type(remaining) is int
                                    and 0 <= remaining <= DAILY_LIMIT) else '未知'
        return f'当前副本：南瓜王（今日剩余 {number}/{DAILY_LIMIT}）'
    floor = floor_number(map_id)
    name = f'神话裂隙 · 第 {floor} 层' if floor else option.get('name') or option.get('stage') or map_id or '未知副本'
    skull_difficulty = control.get('mythic_entry_difficulty') if difficulty == MYTHIC else difficulty
    skull_id = SKULLS.get(skull_difficulty)
    if not skull_id: return f'当前副本：{name}（头骨余额未知）'
    balances = control.get('entry_items')
    amount = balances.get(skull_id, 0) if isinstance(balances, dict) else None
    number = str(amount) if type(amount) is int and amount >= 0 else '未知'
    return f'当前副本：{name}（{DIFFICULTIES[skull_difficulty]}头骨：{number}）'


def loot_lines(drops, sales, describe, *, locations_fresh=True):
    """Join exact instance identities, never a same-name Steam item."""
    by_uid = {row['item_uid']: row for row in sales if row.get('item_uid')}
    grouped = Counter()
    for row in drops:
        category = row['category']
        name = row.get('name') or (describe(row['item_key'])[0] if row.get('item_key') else None) or category
        if row.get('is_minion_equipment') and '仆从装备' not in name: name += ' · 仆从装备'
        if category == '黑雾装备' and '黑雾' not in name: name += ' · 黑雾'
        if category == '神圣装备' and '神圣' not in name: name += ' · 神圣'
        if category == '套装符文' and not any(s in name for s in ('符文套装', '套装符文')): name += ' · 符文套装'
        if category == '宏伟符文' and '宏伟' not in name: name += ' · 宏伟'
        sale = by_uid.get(row.get('item_uid'))
        status = SALE_STATES.get(sale.get('status')) if sale else None
        remaining = row['count']
        if status:
            amount = sale.get('count', 1)
            amount = min(remaining, amount) if type(amount) is int and amount > 0 else 0
            grouped[(name, status)] += amount
            remaining -= amount
        if locations_fresh:
            for container, amount in row['locations'].items():
                take = min(amount, remaining)
                if take:
                    grouped[(name, LOCATIONS.get(container, '位置待核验'))] += take
                    remaining -= take
        if remaining:
            grouped[(name, '位置待核验')] += remaining
    return [f'{name}' + (f' ×{count}' if count > 1 else '') + f'（{status}）'
            for (name, status), count in grouped.items() if count]


class TrayStatusMixin:
    def tray_snapshot(self):
        """Background-only; no game reads, Steam requests or economic actions.

        Do not compete with a running managed action for the service lock. The
        tray keeps a bounded-age cached view if an action is currently busy.
        """
        if not self.guard.acquire(blocking=False): return None
        try:
            connected = self.client.connected
            control = deepcopy(self.managed_control)
            read_at = self.managed_control_read_at
        finally:
            self.guard.release()
        # The projection may briefly wait for its own local locks. Do not
        # retain the game/service lock while building a Steam/trade view.
        sales = self.managed_auction_tracking(include_identity=True)
        fresh = connected and read_at is not None and 0 <= time.monotonic() - read_at <= 20
        lines, other, actions, other_actions = [], [], [], []
        for row in sales.get('items', []):
            if row.get('status') == 'sold':
                continue
            status = row.get('status')
            previous = row.get('previous_status') if status == 'uncertain' else None
            label = SALE_STATES.get(previous or status, '待核验')
            if (previous or status) == 'waiting_town':
                label = LOCATIONS.get(row.get('container'), '背包 / 仓库') + '中，待上架'
            if (previous or status) == 'listed':
                label = (row.get('my_price') or '价格待核验') + '，底价 ' + (row.get('lowest_price') or '暂不可读')
                if row.get('lowest_price') and row.get('lowest_price_cached'):
                    label += '（缓存）'
                label += ' · Steam 市场已上架'
            if (status == 'uncertain' and previous) or row.get('needs_verification'):
                label += ' · 待核验'
            count = row.get('count', 1)
            target = other if row.get('auto_eligible') is False else lines
            target.append(row.get('name', '未识别物品') + (f' ×{count}' if count > 1 else '') + f'（{label}）')
            record_id = row.get('tracking_id')
            can_open = ((previous or status) == 'listed' and (row.get('reprice_available') or row.get('can_reprice'))
                        and isinstance(record_id, str) and re.fullmatch(r'[a-f0-9]{32}', record_id))
            action = record_id if can_open else None
            sale_id = row.get('id')
            if ((previous or status) == 'uploaded' and (row.get('sale_preview_available') or row.get('sale_available'))
                    and isinstance(sale_id, str)
                    and re.fullmatch(r'(?:[a-f0-9]{32}|inventory-[1-9]\d{0,24})', sale_id)):
                action = {'kind': 'sale', 'id': sale_id}
            (other_actions if target is other else actions).append(action)
        return {'dungeon': dungeon_text(control, fresh=fresh), 'loot': lines, 'other_trades': other,
                'loot_actions': actions, 'other_trade_actions': other_actions}
