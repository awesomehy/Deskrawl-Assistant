"""Track newly observed equipment across backpack and storage snapshots."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LootState:
    session: str
    seen: frozenset[str]
    pending: frozenset[str]

    @classmethod
    def observe(cls, snapshot, previous=None):
        if not snapshot.get('complete'):
            return previous
        session = snapshot.get('bridge_session')
        if not isinstance(session, str) or not session:
            return previous
        present = set()
        locked = set()
        for container in snapshot.get('containers', {}).values():
            for row in container.get('slots', []):
                identity = row.get('instance_id')
                if row.get('is_equipment') is True and isinstance(identity, str) and identity:
                    present.add(identity)
                    if row.get('locked') is True:
                        locked.add(identity)
        if previous is None or previous.session != session:
            return cls(session, frozenset(present), frozenset())
        pending = ((previous.pending | (present - previous.seen)) & present) - locked
        return cls(session, frozenset(present), frozenset(pending))
