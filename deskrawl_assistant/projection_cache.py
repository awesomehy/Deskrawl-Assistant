"""Bounded caches for pure UI projections, never for native write evidence."""
from copy import deepcopy
import threading


def without_sample_time(value):
    result = dict(value)
    # Keep missing/invalid timestamps distinguishable. Only a valid sample time
    # can change without changing the contents of a character/item projection.
    stamp = result.get('captured_at')
    if isinstance(stamp, str) and stamp.strip():
        result['captured_at'] = '<sample-time>'
    return result


def inventory_projection_key(snapshot, character, rules, catalog_identity):
    content = without_sample_time(snapshot)
    identifier = content.get('snapshot_id')
    # The adapter requires a valid observation token. Its exact value is not
    # exposed in these UI rows; pid, bridge_session, transfer keys, identity and
    # all completeness/quality evidence remain in the cache key unchanged.
    if ((isinstance(identifier, str) and identifier.strip())
            or (type(identifier) is int and identifier > 0)):
        content['snapshot_id'] = '<valid-snapshot>'
    return (content, without_sample_time(character),
            [rule.to_dict() for rule in rules], catalog_identity)


class ProjectionCache:
    """Keep one result; callers cannot mutate cached inputs or returned rows."""
    def __init__(self):
        self.guard = threading.Lock()
        self.key = None
        self.value = None

    def get(self, key, build):
        with self.guard:
            if self.value is None or self.key != key:
                value = build()
                self.key = deepcopy(key)
                self.value = value
            return deepcopy(self.value)

    def clear(self):
        with self.guard:
            self.key = None
            self.value = None
