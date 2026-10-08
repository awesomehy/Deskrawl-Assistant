import unittest
from deskrawl_assistant.loot_monitor import LootState


def snapshot(inventory=(), storage=(), session='game-a', complete=True):
    def rows(items):
        return [{'instance_id': identity, 'is_equipment': True, 'locked': locked} for identity, locked in items]
    return {'complete': complete, 'bridge_session': session, 'containers': {
        'inventory': {'slots': rows(inventory)}, 'storage': {'slots': rows(storage)}}}


class LootMonitorTests(unittest.TestCase):
    def test_first_scan_does_not_schedule_old_items(self):
        state = LootState.observe(snapshot([('old', False)]))
        self.assertEqual(state.pending, frozenset())

    def test_new_instance_is_pending(self):
        state = LootState.observe(snapshot([('old', False)]))
        state = LootState.observe(snapshot([('old', False), ('new', False)]), state)
        self.assertEqual(state.pending, frozenset({'new'}))

    def test_moving_existing_item_is_not_new_loot(self):
        state = LootState.observe(snapshot([('old', False)]))
        state = LootState.observe(snapshot(storage=[('old', False)]), state)
        self.assertFalse(state.pending)

    def test_pending_item_waits_until_it_can_be_locked(self):
        state = LootState.observe(snapshot())
        state = LootState.observe(snapshot([('new', False)]), state)
        state = LootState.observe(snapshot(storage=[('new', False)]), state)
        state = LootState.observe(snapshot([('new', False)]), state)
        self.assertEqual(state.pending, frozenset({'new'}))

    def test_confirmed_lock_removes_pending_action(self):
        state = LootState.observe(snapshot())
        state = LootState.observe(snapshot([('new', False)]), state)
        state = LootState.observe(snapshot([('new', True)]), state)
        self.assertFalse(state.pending)

    def test_disappeared_item_is_not_retained(self):
        state = LootState.observe(snapshot())
        state = LootState.observe(snapshot([('new', False)]), state)
        state = LootState.observe(snapshot(), state)
        self.assertFalse(state.pending)

    def test_reconnection_establishes_a_fresh_baseline(self):
        state = LootState.observe(snapshot())
        state = LootState.observe(snapshot([('new', False)]), state)
        state = LootState.observe(snapshot([('new', False)], session='game-b'), state)
        self.assertFalse(state.pending)

    def test_incomplete_snapshot_does_not_erase_pending_items(self):
        state = LootState.observe(snapshot())
        state = LootState.observe(snapshot([('new', False)]), state)
        self.assertIs(LootState.observe(snapshot(complete=False), state), state)
