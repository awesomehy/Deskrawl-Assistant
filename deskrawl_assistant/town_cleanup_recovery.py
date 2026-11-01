"""A narrow cleanup allowance, never a replacement for native scene state."""
from __future__ import annotations


CLEANUP_KINDS = frozenset(('lock', 'transfer', 'sell', 'salvage',
                           'open_chest', 'collect_ground', 'collect_carriage'))


def cleanup_action_allowed(action):
    return (action.get('kind') in CLEANUP_KINDS
            and (action['kind'] != 'transfer' or action.get('target') == 'storage')
            and action.get('reason_code') not in ('equip_prepare', 'synthesis_prepare', 'auction_prepare'))


def cleanup_ready(observation):
    scene = observation.get('scene', {})
    proof = scene.get('town_cleanup_recovery') or {}
    if not isinstance(proof, dict): return False
    return (observation.get('complete') is True and scene.get('available') is True
            and scene.get('in_town') is True and scene.get('scene_changing') is True
            and all(scene.get(k) is False for k in ('teleporting', 'online_saving', 'dead', 'in_battle'))
            and type(scene.get('online_pending_requests')) is int and scene['online_pending_requests'] == 0
            and proof.get('eligible') is True and bool(proof.get('character_id'))
            and proof.get('character_id') == observation.get('character_id')
            and bool(proof.get('session')) and proof.get('session') == observation.get('session'))


class StableTown:
    """Only successful guarded native samples advance the stability window.

    A long read gap, changed roots/role or any failed sample revokes the grant.
    Action-time reads can check a grant, but cannot create one themselves.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.signature = self.character_id = None
        self.started = self.last = None
        self.samples = 0

    def observe(self, signature, character_id, now):
        if not signature or not character_id:
            self.reset()
            return False
        if (signature != self.signature or character_id != self.character_id
                or self.last is None or not 0 <= now - self.last <= 30):
            self.reset()
            self.signature, self.character_id, self.started = signature, character_id, now
        self.samples += 1
        self.last = now
        return self.check(signature, now)

    def check(self, signature, now):
        return (bool(signature) and signature == self.signature and self.samples >= 3
                and self.started is not None and now - self.started >= 10
                and self.last is not None and 0 <= now - self.last <= 30)
