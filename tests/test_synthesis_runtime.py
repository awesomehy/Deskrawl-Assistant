"""Synthesis safety/receipt checks with copied fakes; no game process attached."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.native_dispatcher import NativeActionError, load_profile
from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.synthesis_runtime import SynthesisRuntime, gem_inputs, gem_recipes, rune_inputs


def gem(slot, count=10, **extra):
    return {'container': 'inventory', 'slot_index': slot, 'count': count,
            'internal_name': 'GemRuby1', 'item_class': 'GemData',
            'locked': False, 'slot_locked': False, **extra}


def rune(index, **extra):
    return {'container': 'inventory', 'slot_index': index, 'count': 1,
            'item_uid': f'rune-{index}', 'internal_name': f'UncommonRune_{index}',
            'item_class': 'RuneData', 'rarity': 1, 'locked': False,
            'slot_locked': False, 'rune_level': 15, 'rune_xp': 600, **extra}


class InputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.gem_recipe = next(r for r in gem_recipes() if r['recipe_id'] == 'GemUpgradeRuby2')
        self.rune_recipe = {'input_quality': 'uncommon', 'output_quality': 'rare'}

    def test_catalog_has_every_color_quality_with_known_costs(self):
        recipes = gem_recipes()
        self.assertEqual(len(recipes), 30)
        self.assertEqual(len({r['recipe_id'] for r in recipes}), 30)
        self.assertEqual({r['input_count'] for r in recipes}, {10})
        self.assertEqual({r['gold_cost'] for r in recipes}, {300, 1300, 5500, 23000, 100000})

    def test_gem_selection_matches_native_reverse_removal(self):
        rows = [gem(1, 9), gem(3, 15), gem(5, 6)]
        chosen = gem_inputs(rows, self.gem_recipe, 2)
        self.assertEqual([(r['slot_index'], r['quantity']) for r in chosen], [(5, 6), (3, 14)])
        self.assertNotIn('quantity', rows[-1])

    def test_any_matching_locked_stack_blocks_even_with_enough_unlocked(self):
        for extra in ({'locked': True}, {'locked': None}, {'slot_locked': True}, {'slot_locked': None}, {'issues': ['unreadable']}):
            with self.subTest(extra=extra):
                with self.assertRaises(NativeActionError):
                    gem_inputs([gem(1, 100), gem(2, 1, **extra)], self.gem_recipe, 1)

    def test_other_color_locked_gem_does_not_block(self):
        selected = gem_inputs([gem(0), gem(1, locked=True, internal_name='GemTopaz1')], self.gem_recipe, 1)
        self.assertEqual(selected[0]['slot_index'], 0)

    def test_insufficient_material_or_bad_count_is_rejected(self):
        for count in (0, -1, True, 1.5):
            with self.assertRaises(NativeActionError):
                gem_inputs([gem(0)], self.gem_recipe, count)
        with self.assertRaises(NativeActionError):
            gem_inputs([gem(0, 9)], self.gem_recipe, 1)

    def test_nine_different_names_and_trained_runes_are_allowed(self):
        rows = [rune(i) for i in range(9)]
        selected = rune_inputs(rows, [r['item_uid'] for r in rows], self.rune_recipe, 'rare', [])
        self.assertEqual(len(selected), 9)
        self.assertEqual(selected[0]['rune_level'], 15)
        self.assertEqual(selected[0]['rune_xp'], 600)

    def test_rune_lock_equipped_skip_and_identity_are_all_rechecked(self):
        for extra in ({'locked': True}, {'slot_locked': True}, {'count': 2}, {'identity_verified': False},
                      {'rarity': 2, 'internal_name': 'RareRune_other'}, {'container': 'storage'}):
            with self.subTest(extra=extra):
                rows = [rune(i, **(extra if i == 0 else {})) for i in range(9)]
                with self.assertRaises(NativeActionError):
                    rune_inputs(rows, [r['item_uid'] for r in rows], self.rune_recipe, 'rare', [])
        rows = [rune(i) for i in range(9)]; uids = [r['item_uid'] for r in rows]
        with self.assertRaises(NativeActionError):
            rune_inputs(rows, uids, self.rune_recipe, 'rare', [rows[0]['internal_name']])
        with self.assertRaises(NativeActionError):
            rune_inputs(rows, uids, self.rune_recipe, 'rare', [], [uids[0]])
        with self.assertRaises(NativeActionError):
            rune_inputs(rows, uids[:-1] + [uids[0]], self.rune_recipe, 'rare', [])

    def test_above_maximum_and_set_reroll_never_reach_native(self):
        rows = [rune(i) for i in range(9)]; uids = [r['item_uid'] for r in rows]
        for recipe, maximum in ((self.rune_recipe, 'uncommon'),
                                 ({'input_quality': 'set', 'output_quality': 'set'}, 'set')):
            with self.assertRaises(NativeActionError):
                rune_inputs(rows, uids, recipe, maximum, [])


class SnapshotAndReceiptTests(unittest.TestCase):
    def runtime(self):
        reader = Mock(); reader.session_id = 'fake-session'
        actions = SimpleNamespace(reader=reader, profile=load_profile(), verified_kinds=set())
        runtime = SynthesisRuntime(actions)
        return runtime, actions

    def test_synthesis_is_released_after_native_receipt_validation(self):
        profile = load_profile()
        for kind in ('synthesize_gems', 'synthesize_runes'):
            self.assertIn(kind, profile['verified_kinds'])
            self.assertNotIn(kind, profile['enabled_kinds'])
        from deskrawl_assistant.game_actions import CAPABILITIES
        self.assertEqual(CAPABILITIES['gem_synthesis'], ('synthesize_gems',))
        self.assertEqual(CAPABILITIES['rune_synthesis'], ('synthesize_runes',))

    def test_missing_cube_state_cannot_hide_gem_snapshot(self):
        runtime, actions = self.runtime()
        actions._context = Mock(return_value=(Mock(), 1, 2, 1))
        runtime._copy = Mock(return_value=Mock())
        runtime._gems = Mock(return_value={'available': True, 'recipes': [], 'gold': 12345})
        runtime._runes = Mock(side_effect=MemoryReadError('cube missing'))
        state = runtime.snapshot()
        self.assertTrue(state['gems']['available']); self.assertEqual(state['gems']['gold'], 12345)
        self.assertFalse(state['runes']['available'])
        actions.reader.memory.write.assert_not_called()
        state = runtime.snapshot(gems=True, runes=False)
        self.assertNotIn('runes', state)

    def test_gem_snapshot_exposes_real_output_stack_capacity_without_writes(self):
        runtime, actions = self.runtime()
        recipe = next(r for r in gem_recipes() if r['recipe_id'] == 'GemUpgradeRuby2')
        copy = Mock()
        copy.dictionary.return_value = {100: 200}
        copy.verify.return_value = 200
        copy.integer.return_value = 99
        actions.reader.string.return_value = 'GemRuby2'
        actions.reader.asset_name.return_value = 'GemRuby2'
        runtime._gold = Mock(return_value=1000)
        with patch('deskrawl_assistant.synthesis_runtime.gem_recipes', return_value=[recipe]):
            state = runtime._gems(copy, 300)
        self.assertEqual(state['recipes'][0]['output_max_stack'], 99)
        copy.integer.assert_called_with(336)
        actions.reader.memory.write.assert_not_called()
        self.assertNotIn('output_max_stack', recipe)

    def test_gem_receipt_requires_gold_and_exact_input_output_deltas(self):
        runtime, _ = self.runtime()
        recipe = next(r for r in gem_recipes() if r['recipe_id'] == 'GemUpgradeRuby2')
        before = [gem(0, 20)]
        selected = gem_inputs(before, recipe, 1)
        after = [gem(0, 10), gem(1, 1, internal_name='GemRuby2')]
        runtime._context = Mock(return_value=(Mock(), 1, 2)); runtime._copy = Mock(return_value=Mock())
        runtime._bag = Mock(return_value=(after, {}, 100, 40)); runtime._gold = Mock(return_value=700)
        receipt = runtime._confirm('synthesize_gems', before, selected, recipe, 1, {'gold': 1000}, {'sequence': 7})
        self.assertEqual(receipt['inputs'][0]['quantity'], 10)
        self.assertEqual(receipt['outputs'][0]['internal_name'], 'GemRuby2')
        self.assertEqual(receipt['outputs'][0]['quantity'], 1)
        self.assertIn('fake-session', receipt['receipt_id'])

    def test_online_rune_failure_is_confirmed_only_with_sequence_and_new_uid(self):
        runtime, _ = self.runtime()
        before = [rune(i) for i in range(9)]
        recipe = {'recipe_id': 'cube:uncommon:rare', 'input_quality': 'uncommon', 'output_quality': 'rare'}
        output = rune(12, rune_level=1, rune_xp=0)
        runtime._context = Mock(return_value=(Mock(), 1, 2)); runtime._copy = Mock(return_value=Mock())
        runtime._bag = Mock(return_value=([output], {}, 100, 40))
        runtime._runes = Mock(return_value={'sequence': 11})
        receipt = runtime._confirm('synthesize_runes', before, [dict(r, quantity=1) for r in before], recipe, 1,
                                   {'online': True, 'sequence': 10}, {'sequence': 1})
        self.assertEqual(receipt['sequence'], 11)
        self.assertEqual(receipt['outputs'][0]['item_uid'], 'rune-12')

    def test_unconfirmed_prior_call_cannot_be_automatically_retried(self):
        runtime, _ = self.runtime(); runtime._uncertain = True
        with self.assertRaises(NativeActionError) as caught:
            runtime.execute('synthesize_runes', {})
        self.assertTrue(caught.exception.uncertain)

    def test_confirmation_timeout_latches_before_any_later_submission(self):
        runtime, _ = self.runtime()
        recipe = next(r for r in gem_recipes() if r['recipe_id'] == 'GemUpgradeRuby2')
        rows = [gem(0, 20)]
        read = Mock(); read.ptr.return_value = 300; read.integer.return_value = 99
        runtime._context = Mock(return_value=(read, 100, 200))
        runtime._copy = Mock(return_value=Mock())
        runtime._bag = Mock(return_value=(rows, {}, 1000, 40))
        runtime._guard_bag_identities = Mock(); runtime._guard_inputs = Mock()
        runtime._gold = Mock(return_value=10000)
        runtime._find_recipe = Mock(return_value=500)
        runtime._invoke = Mock(return_value={'sequence': 1})
        runtime._confirm = Mock(side_effect=NativeActionError('unconfirmed', uncertain=True))
        payload = {'recipe_id': recipe['recipe_id'], 'count': 1, 'max_quality': 5,
                   'inputs': gem_inputs(rows, recipe, 1)}
        for _ in range(2):
            with self.assertRaises(NativeActionError) as caught:
                runtime.execute('synthesize_gems', payload)
            self.assertTrue(caught.exception.uncertain)
        self.assertEqual(runtime._invoke.call_count, 1)
        self.assertTrue(runtime._uncertain)

    def test_stale_plan_cannot_silently_choose_another_material_stack(self):
        runtime, _ = self.runtime()
        recipe = next(r for r in gem_recipes() if r['recipe_id'] == 'GemUpgradeRuby2')
        rows = [gem(1, 20)]
        read = Mock(); read.ptr.return_value = 300; read.integer.return_value = 99
        runtime._context = Mock(return_value=(read, 100, 200))
        runtime._copy = Mock(return_value=Mock())
        runtime._bag = Mock(return_value=(rows, {}, 1000, 40))
        runtime._guard_bag_identities = Mock(); runtime._guard_inputs = Mock()
        runtime._gold = Mock(return_value=10000)
        runtime._find_recipe = Mock(return_value=500)
        runtime._invoke = Mock()
        payload = {'recipe_id': recipe['recipe_id'], 'count': 1, 'max_quality': 5,
                   'inputs': gem_inputs([gem(0, 20)], recipe, 1)}
        from deskrawl_assistant.native_memory import SnapshotChangedError
        with self.assertRaises(SnapshotChangedError):
            runtime.execute('synthesize_gems', payload)
        runtime._invoke.assert_not_called()

    def test_guarded_bag_includes_empty_slots_so_locked_insertions_invalidate(self):
        runtime, _ = self.runtime(); read = Mock()
        read.ptr.side_effect = [100, 200]
        runtime._guard_bag_identities(read, 1000, 2)
        read.read.assert_any_call(116, 32)
        read.read.assert_any_call(216, 32)


if __name__ == '__main__':
    unittest.main()
