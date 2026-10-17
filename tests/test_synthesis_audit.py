"""Independent policy/native boundary audit; never attaches to a game."""
from copy import deepcopy
import unittest

from deskrawl_assistant.managed_mode import ManagedMetrics, ManagedMode
from deskrawl_assistant.synthesis import next_action
from deskrawl_assistant.synthesis_runtime import gem_inputs, rune_inputs
from tests.test_synthesis import config, drop_count, gem, rune, world


def fresh_gem(*args, **kwargs):
    row = gem(*args, **kwargs)
    row.setdefault('slot_locked', False)
    row.setdefault('max_stack', 100)
    return row


def fresh_rune(*args, **kwargs):
    row = rune(*args, **kwargs)
    row.setdefault('slot_locked', False)
    row.setdefault('identity_verified', True)
    return row


def fresh_world(rows=(), **kwargs):
    state = world(rows, **kwargs)
    for recipe in state['synthesis']['gems']['recipes']:
        recipe['output_max_stack'] = 100
    state['synthesis']['runes']['equipped_uids'] = []
    return state


class PlannerNativeBoundaryAudit(unittest.TestCase):
    def test_any_locked_or_unknown_matching_gem_slot_blocks_recipe(self):
        for value in (True, None):
            rows = [fresh_gem(100), fresh_gem(1, slot=1, slot_locked=value)]
            with self.subTest(value=value):
                self.assertIsNone(next_action(config(), fresh_world(rows)))

    def test_bad_rune_slot_or_identity_is_not_planned_for_consumption(self):
        for extra in ({'slot_locked': True}, {'slot_locked': None}, {'identity_verified': False}):
            rows = [fresh_rune(n, **(extra if n == 0 else {})) for n in range(9)]
            with self.subTest(extra=extra):
                self.assertIsNone(next_action(config(gems=False, runes=True), fresh_world(rows)))

    def test_batch_never_exceeds_actual_free_output_capacity(self):
        # Native's output capacity is one free slot, each holding ten outputs.
        rows = [fresh_gem(100, slot=n) for n in range(10)]
        state = fresh_world(rows, capacity=11)
        state['synthesis']['gems']['recipes'][0]['output_max_stack'] = 10
        action = next_action(config(), state)
        self.assertEqual(action['count'], 10)
        self.assertEqual(sum(r['quantity'] for r in action['inputs']), 100)
        self.assertEqual(action['inputs'], gem_inputs(rows, action['recipe'], action['count']))

    def test_existing_output_stack_room_is_used_but_never_overfilled(self):
        rows = [fresh_gem(100), fresh_gem(100, slot=1), fresh_gem(9, slot=2, grade=2, max_stack=10)]
        state = fresh_world(rows, capacity=4)
        state['synthesis']['gems']['recipes'][0]['output_max_stack'] = 10
        # 1 partially used stack + 1 empty slot can receive eleven outputs.
        action = next_action(config(), state)
        self.assertEqual(action['count'], 11)
        self.assertEqual(action['inputs'], gem_inputs(rows, action['recipe'], 11))

    def test_equipped_rune_uid_is_excluded_even_without_row_equipped_flag(self):
        rows = [fresh_rune(n) for n in range(9)]
        state = fresh_world(rows)
        state['synthesis']['runes']['equipped_uids'] = [rows[0]['item_uid']]
        self.assertIsNone(next_action(config(gems=False, runes=True), state))

    def test_full_bag_does_not_stage_a_storage_material_repeatedly(self):
        rows = [fresh_gem(9), fresh_gem(1, slot=50, container='storage')]
        state = fresh_world(rows, capacity=1)
        self.assertIsNone(next_action(config(), state))
        # The conservative full-bag policy stores or pauses; it never withdraws
        # a material to fill the same workspace with no possible output slot.
        engine = ManagedMode(config())
        engine.start(state)
        action = engine.next_action(state)
        self.assertTrue(action is None or action.get('target') == 'storage')
        self.assertFalse(action and action.get('reason_code') == 'synthesis_prepare')

    def test_rune_plan_is_accepted_by_the_native_uid_validator(self):
        rows = [fresh_rune(n, 'rare', rune_level=20, rune_xp=999) for n in range(9)]
        cfg = config(gems=False, runes=True)
        action = next_action(cfg, fresh_world(rows))
        self.assertEqual(action['inputs'], rune_inputs(rows, action['input_uids'], action['recipe'],
                                                       action['max_quality'], action['excluded_keys']))


class SynthesisAccountingAudit(unittest.TestCase):
    def test_two_tiers_and_merged_output_stacks_never_inflate_loot(self):
        metrics = ManagedMetrics()
        original = fresh_gem(100)
        old_output = fresh_gem(2, slot=1, grade=2)
        metrics.observe(fresh_world([original, old_output]))
        grade2 = fresh_gem(12, slot=1, grade=2)
        metrics.synthesized('first', [dict(original, quantity=100)], [dict(grade2, quantity=10)])
        metrics.observe(fresh_world([grade2]))
        grade3 = fresh_gem(1, slot=0, grade=3)
        metrics.synthesized('second', [dict(grade2, quantity=10)], [dict(grade3, quantity=1)])
        metrics.observe(fresh_world([fresh_gem(2, slot=1, grade=2), grade3]))
        self.assertEqual(drop_count(metrics), 0)
        metrics.observe(fresh_world([fresh_gem(3, slot=1, grade=2), grade3]))
        self.assertEqual(drop_count(metrics), 1)

    def test_probability_failure_return_and_subsequent_transfer_are_not_drops(self):
        metrics = ManagedMetrics()
        rows = [fresh_rune(n, 'rare') for n in range(9)]
        metrics.observe(fresh_world(rows))
        output = fresh_rune(20, 'rare', rune_level=1, rune_xp=0)
        metrics.synthesized('failed-roll', [dict(r, quantity=1) for r in rows], [dict(output, quantity=1)])
        metrics.observe(fresh_world([output]))
        stored = dict(deepcopy(output), container='storage', slot_index=51)
        metrics.committed('store-craft', output, 'transfer')
        metrics.observe(fresh_world([stored]))
        self.assertEqual(drop_count(metrics), 0)
        self.assertEqual(sum(r['stored'] for r in metrics.state()['runes']), 0)


if __name__ == '__main__':
    unittest.main()
