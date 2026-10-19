"""Editor candidates from the shipped pools and verified native class filter.

HeroClass here is the class used to GENERATE an item, not its UsableClasses.
Keep the broad pools for reading and evaluating existing/traded items: filtering
by the current player must never erase a real affix or change a saved rule.
"""
from .runtime_adapter import headline_stats


def editor_pools(slot_pools, class_filters):
    """Return the broad view plus one primary-filtered view per HeroClass."""
    broad = {p['slot']: {group: [key for key in p[group]
                               if key not in headline_stats(p['slot'])]
                        for group in ('primary', 'secondary')}
             for p in slot_pools}
    exclusions = class_filters['primary_exclusion_masks']
    by_class = {
        str(value): {slot: {
            'primary': [key for key in pool['primary'] if not (value & exclusions.get(key, 0))],
            'secondary': list(pool['secondary']),
        } for slot, pool in broad.items()}
        for value in class_filters['classes'].values()
    }
    return broad, by_class
