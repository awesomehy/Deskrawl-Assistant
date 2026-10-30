"""Recognise resource-verified chest consumables across inventory and actions."""
from functools import lru_cache
import json

from .paths import RESOURCE_ROOT


@lru_cache(maxsize=1)
def chest_keys():
    items = json.loads((RESOURCE_ROOT / 'data/loot-item-ui.json').read_text(encoding='utf-8'))['items']
    return frozenset(key for key, item in items.items()
                     if item.get('kind') == 'chest' and item.get('item_type') == 2
                     and item.get('source_class') == 'ItemData')


def is_chest(row):
    return (row.get('is_equipment') is not True and row.get('item_type') in (None, 2)
            and (row.get('internal_name') or row.get('name_key')) in chest_keys())
