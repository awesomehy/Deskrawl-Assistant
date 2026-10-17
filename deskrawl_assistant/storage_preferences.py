"""Shared partition choices; game permissions decide which slots are usable."""
from copy import deepcopy
import json

from .models import ValidationError

DEFAULT_STORAGE_PAGES = {'equipment': (1, 2, 3), 'rune': (4,), 'gem': (5,)}

SHARED_STORAGE_GROUPS = (('rune', 'runes'), ('gem', 'gems'))
MANDATORY_STORAGE_GROUPS = (('equipment', '装备'), ('rune', '符文'), ('gem', '宝石'),
                            ('gold_bar', '金条'), ('legendary_rein', '传说缰绳'))


def available_storage_pages(snapshot, connected):
    """None means unread; an empty set means no unlocked slots.

    Occupancy does not change unlock permissions: a full page remains selectable.
    """
    if not connected or not isinstance(snapshot, dict) or snapshot.get('complete') is not True:
        return None
    transfer = snapshot.get('transfer', {})
    if not isinstance(transfer, dict): return None
    size, indices = transfer.get('page_size'), transfer.get('storage_indices')
    if (transfer.get('available') is not True or type(size) is not int or size < 1
            or not isinstance(indices, list) or any(type(n) is not int or n < 0 for n in indices)
            or len(set(indices)) != len(indices)):
        return None
    return {n // size + 1 for n in indices}


def validate_available_storage_pages(cleanup, available_pages, groups=MANDATORY_STORAGE_GROUPS):
    """Allow extra reserved pages, but require an unlocked destination per group."""
    if available_pages is None: return
    missing = [label for kind, label in groups
               if not any(page in available_pages for page in cleanup[kind + '_pages'])]
    if missing:
        raise ValidationError('回城整理：' + '、'.join(missing)
            + '必须至少配置 1 个已解锁且有开放格位的仓库页；未解锁页只能作为额外预留。')


def organizer_with_shared_pages(automation, cleanup):
    """Managed cleanup is the persisted authority for these two destinations."""
    result = deepcopy(automation)
    for kind, group in SHARED_STORAGE_GROUPS:
        result[group]['pages'] = list(cleanup[kind + '_pages'])
    return result


def validate_shared_pages(automation, cleanup):
    # Both views share mandatory rune/gem destinations, even with organizers off.
    if not cleanup['rune_pages']:
        raise ValidationError('请选择符文共用存放页，用于收纳套装／宏伟符文。')
    if not cleanup['gem_pages']:
        raise ValidationError('请选择宝石共用存放页，所有宝石必须收纳。')


def write_settings_files(values):
    """Stage both views before replacing either, restoring files on failure."""
    originals = {path: path.read_bytes() if path.exists() else None for path in values}
    replaced = []
    try:
        for path, value in values.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.with_suffix('.json.tmp').write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        for path in values:
            path.with_suffix('.json.tmp').replace(path)
            replaced.append(path)
    except Exception:
        for path in reversed(replaced):
            original = originals[path]
            if original is None:
                path.unlink()
            else:
                temp = path.with_suffix('.json.tmp')
                temp.write_bytes(original)
                temp.replace(path)
        raise
    finally:
        for path in values:
            path.with_suffix('.json.tmp').unlink(missing_ok=True)
