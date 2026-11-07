"""Shared, offline high-value classification and per-managed-run tray records.

This does not save capture files or enable the retired loot archive monitor.
"""
from collections import Counter
from copy import deepcopy
from functools import lru_cache

from .item_protection import mandatory_storage, is_minion_equipment
from .paths import RESOURCE_ROOT
from .steam_market import MarketCatalog


@lru_cache(maxsize=1)
def _market_catalog():
    return MarketCatalog.load(RESOURCE_ROOT / 'data/steam-market-catalog.json')


def high_value_reason(row):
    reason = mandatory_storage(row)
    if reason:
        return reason
    key = row.get('internal_name') or row.get('name_key')
    entry = _market_catalog().items.get(key)
    if not entry:
        return ''
    variant = 'black_mist' if row.get('is_black_mist') is True else 'normal'
    if entry.get('kind') == 'equipment':
        rarity = row.get('rarity', row.get('base_rarity'))
        expected = 3 if variant == 'black_mist' else entry.get('rarity')
        if type(rarity) is not int or rarity != expected:
            return ''
    if not entry.get(variant):
        return ''
    if key == 'GoldBar': return '金条'
    if key == 'UpgradeCommodity1': return '开光石'
    if key.startswith('Rein_'): return '传说缰绳'
    return '可交易物品'


class HighValueDrops:
    """Fed only by accepted ManagedMetrics pickups, never by inventory totals."""
    def __init__(self):
        self.rows = {}

    def record(self, row, metric_key, quantity):
        try:
            reason = high_value_reason(row)
        except (OSError, ValueError):
            # A broken optional display dictionary cannot stop managed actions.
            reason = mandatory_storage(row)
        if not reason: return
        uid = row.get('item_uid')
        # An unnamed/identity-less aggregate equipment receipt cannot identify
        # an individual item. Do not invent a specific drop from that counter.
        if not uid and metric_key[0] in ('equipment', 'minion_equipment'): return
        token = ('uid', uid) if uid else ('stack', metric_key)
        if token not in self.rows:
            self.rows[token] = {'item_uid': uid or '', 'item_key': row.get('internal_name') or row.get('name_key'),
                'name': row.get('name'), 'category': reason, 'is_minion_equipment': is_minion_equipment(row),
                'count': 0, 'locations': {}}
        value = self.rows[token]
        value['count'] += quantity

    def observe(self, unique, locations, initial_locations, *, complete):
        for (kind, identity), value in self.rows.items():
            if kind == 'uid':
                row = unique.get(identity)
                if row is not None:
                    value['locations'] = {row.get('container', 'unknown'): value['count']}
                elif complete:
                    value['locations'] = {}
            elif complete:
                current = Counter()
                for location, count in locations.items():
                    if location[2] == identity:
                        current[location[0]] += max(0, count - initial_locations[location])
                # Bound to accepted drops if an explicit event source and the
                # inventory snapshot briefly disagree on a stack quantity.
                remaining = value['count']
                value['locations'] = {}
                for container, count in current.items():
                    take = min(count, remaining)
                    if take: value['locations'][container] = take
                    remaining -= take

    def state(self):
        return deepcopy(list(self.rows.values()))
