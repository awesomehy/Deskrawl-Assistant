"""Synthesis safety/receipt checks with copied fakes; no game process attached."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.native_dispatcher import NativeActionError, load_profile
from deskrawl_assistant.native_memory import MemoryReadError, GameBusyError
from deskrawl_assistant.synthesis_runtime import (SynthesisRuntime, SynthesisReceiptPending,
                                                gem_inputs, gem_recipes, rune_inputs)


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

    def rune_execution(self):
        runtime, actions = self.runtime()
        before = [rune(i) for i in range(9)]
        recipe = {'recipe_id':'cube:uncommon:rare', 'input_quality':'uncommon',
                  'output_quality':'rare', 'available':True}
        read = Mock(); read.ptr.return_value = 0; read.boolean.return_value = False
        read.singleton.return_value = 700
        read.pointers.return_value = list(range(1000,1009))
        actions.reader.string.return_value = ''
        copy = Mock(); copy.boolean.return_value = False
        runtime._context = Mock(return_value=(read,100,200))
        runtime._copy = Mock(return_value=copy)
        runtime._singleton = Mock(return_value=500)
        runtime._bag = Mock(return_value=(before,{},300,40))
        runtime._guard_bag_identities = Mock(); runtime._guard_inputs = Mock()
        runtime._runes = Mock(return_value={'recipes':[recipe], 'online':True,
            'sequence':10, 'equipped_uids':[]})
        runtime._invoke = Mock(return_value={'sequence':1})
        selected = [dict(row,quantity=1) for row in before]
        payload = {'recipe_id':recipe['recipe_id'], 'input_uids':[r['item_uid'] for r in before],
            'inputs':selected, 'count':1, 'max_quality':'rare', 'character_id':'role-a'}
        receipt = {'inputs':selected, 'outputs':[dict(rune(12),quantity=1)],
            'recipe_id':recipe['recipe_id'], 'receipt_id':'test-receipt', 'quantity':1}
        return runtime, payload, receipt

    def cube_state(self, runtime, *, visible=False, animating=False, selections=()):
        """Mutable native UI fake: consumed materials may still occupy UI slots."""
        state = {'visible': visible, 'animating': animating,
                 'selections': list(selections) + [(0, '')] * (9 - len(selections))}
        read = runtime._context.return_value[0]
        slots = [1000 + index * 128 for index in range(9)]
        read.pointers.return_value = slots

        def pointer(address):
            if address == 700 + 64:
                return 900
            for index, slot in enumerate(slots):
                obj, uid = state['selections'][index]
                if address == slot + 64:
                    return obj
                if address == slot + 72:
                    return 30000 + index if uid else 0
            return 0

        read.ptr.side_effect = pointer
        read.boolean.side_effect = lambda address: {
            700 + 40: state['visible'], 700 + 252: state['animating']}.get(address, False)
        runtime.reader.string.side_effect = lambda address: (
            state['selections'][address - 30000][1] if 30000 <= address < 30009 else '')
        return state

    def test_delayed_rune_receipt_recovers_without_another_native_submission(self):
        runtime, payload, receipt = self.rune_execution()
        runtime._confirm = Mock(side_effect=SynthesisReceiptPending('delayed',uncertain=True))
        with patch('deskrawl_assistant.synthesis_runtime.time.monotonic',return_value=100):
            result = runtime.execute('synthesize_runes',payload)
        self.assertTrue(result['synthesis_pending'])
        self.assertFalse(runtime._uncertain)
        with self.assertRaises(GameBusyError): runtime.execute('synthesize_runes',payload)
        # Reconcile uses the real material/sequence validator, including a
        # same-quality output (an unsuccessful probabilistic upgrade).
        runtime._bag.return_value = ([rune(12)],{},300,40)
        runtime._runes.return_value = {'sequence':11}
        runtime._cleanup_cube = Mock(return_value=True)
        result = runtime.reconcile('role-a')
        self.assertTrue(result['effect_confirmed'])
        self.assertEqual(result['outputs'][0]['item_uid'],'rune-12')
        self.assertEqual(runtime._invoke.call_count,1)
        self.assertIsNone(runtime._pending)
        self.assertIsNone(runtime.reconcile('role-a'))

    def test_receipt_timeout_stays_read_only_and_does_not_accept_wrong_sequence(self):
        runtime, payload, _ = self.rune_execution()
        runtime._confirm = Mock(side_effect=SynthesisReceiptPending('delayed',uncertain=True))
        with patch('deskrawl_assistant.synthesis_runtime.time.monotonic',return_value=100):
            runtime.execute('synthesize_runes',payload)
        runtime._bag.return_value = ([rune(12)],{},300,40)
        runtime._runes.return_value = {'sequence':12}
        with patch('deskrawl_assistant.synthesis_runtime.time.monotonic',return_value=221):
            result = runtime.reconcile('role-a')
        self.assertTrue(result['synthesis_pending']); self.assertTrue(result['timed_out'])
        self.assertEqual(runtime._invoke.call_count,1)
        self.assertIsNotNone(runtime._pending)
        runtime._runes.return_value = {'sequence':11}
        runtime._cleanup_cube = Mock(return_value=True)
        self.assertTrue(runtime.reconcile('role-a')['effect_confirmed'])

    def test_pending_receipt_rejects_character_change_without_cleaning_ui(self):
        runtime, payload, _ = self.rune_execution()
        runtime._confirm = Mock(side_effect=SynthesisReceiptPending('delayed',uncertain=True))
        runtime.execute('synthesize_runes',payload)
        with self.assertRaisesRegex(NativeActionError,'角色已变化'):
            runtime.reconcile('role-b')
        self.assertEqual(runtime._invoke.call_count,1)

    def test_confirmed_craft_cleanup_can_recover_without_latching_economic_uncertainty(self):
        runtime, payload, receipt = self.rune_execution()
        runtime._confirm = Mock(return_value=receipt)
        runtime._cleanup_cube = Mock(side_effect=[False,True])
        result = runtime.execute('synthesize_runes',payload)
        self.assertTrue(result['effect_confirmed']); self.assertTrue(result['ui_cleanup_pending'])
        self.assertFalse(runtime._uncertain)
        self.assertIsNone(runtime._pending)
        with self.assertRaises(GameBusyError): runtime.execute('synthesize_runes',payload)
        self.assertFalse(runtime.reconcile('role-a')['ui_cleanup_pending'])
        self.assertEqual(runtime._invoke.call_count,1)
        self.assertIsNone(runtime._cleanup_pending)

    def test_cleanup_clears_consumed_references_then_confirms_slots_and_panel(self):
        runtime, _, _ = self.rune_execution()
        state = self.cube_state(runtime, visible=True, animating=True,
                                selections=[(20000, 'rune-0')])

        def native_reset(*args, **kwargs):
            state.update(visible=False, animating=False, selections=[(0, '')] * 9)
        runtime._invoke.side_effect = native_reset
        self.assertTrue(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        self.assertEqual([c.method for c in runtime._invoke.call_args.args[0]],
                         ['CubePanelUI.gbu','CubePanelUI.gbf','PanelBase.Close'])
        self.assertEqual(runtime._context.call_count, 2)

    def test_cleanup_preserves_player_materials_and_their_open_panel(self):
        runtime, _, _ = self.rune_execution()
        self.cube_state(runtime, visible=True, selections=[(20000, 'player-new-rune')])
        self.assertTrue(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        runtime._invoke.assert_not_called()

    def test_cleanup_of_hidden_consumed_slots_does_not_reopen_panel(self):
        runtime, _, _ = self.rune_execution()
        state = self.cube_state(runtime, selections=[(20000, 'rune-0')])
        runtime._invoke.side_effect = lambda *a, **k: state.update(selections=[(0, '')] * 9)
        self.assertTrue(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        self.assertEqual([c.method for c in runtime._invoke.call_args.args[0]],
                         ['CubePanelUI.gbu','CubePanelUI.gbf'])

    def test_cleanup_incomplete_native_reset_stays_pending_and_retries_only_reset(self):
        runtime, _, _ = self.rune_execution()
        state = self.cube_state(runtime, animating=True, selections=[(20000, 'rune-0')])
        # Ending the animation alone was the original bug: UI references remain.
        runtime._invoke.side_effect = lambda *a, **k: state.update(animating=False)
        self.assertFalse(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        runtime._invoke.side_effect = lambda *a, **k: state.update(selections=[(0, '')] * 9)
        self.assertTrue(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        for call in runtime._invoke.call_args_list:
            self.assertEqual([c.method for c in call.args[0]],
                             ['CubePanelUI.gbu','CubePanelUI.gbf'])

    def test_cleanup_does_not_clear_unknown_material_identity(self):
        runtime, _, _ = self.rune_execution()
        self.cube_state(runtime, selections=[(20000, '')])
        self.assertFalse(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        runtime._invoke.assert_not_called()

    def test_player_selection_after_cleanup_readback_is_preserved(self):
        runtime, _, _ = self.rune_execution()
        state = self.cube_state(runtime, selections=[(20000, 'rune-0')])
        runtime._invoke.side_effect = lambda *a, **k: state.update(
            selections=[(20001, 'player-new-rune')] + [(0, '')] * 8, visible=True)
        self.assertTrue(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        self.assertEqual(runtime._invoke.call_count, 1)
        self.assertEqual(state['selections'][0][1], 'player-new-rune')

    def test_old_hidden_consumed_refs_recover_before_selecting_next_batch(self):
        runtime, payload, _ = self.rune_execution()
        self.cube_state(runtime, selections=[(20000+i, f'consumed-{i}') for i in range(9)])
        runtime._confirm = Mock(side_effect=SynthesisReceiptPending('delayed', uncertain=True))
        self.assertTrue(runtime.execute('synthesize_runes', payload)['synthesis_pending'])
        calls = runtime._invoke.call_args.args[0]
        names = [c.method for c in calls]
        self.assertLess(names.index('CubePanelUI.Open'), names.index('CubePanelUI.gbh'))
        self.assertEqual(names.count('CubePanelUI.gbp'), 1)
        self.assertNotIn('CubePanelUI.gbf', names)  # Native Open resets the old references.

    def test_existing_or_unknown_cube_selections_block_a_fresh_craft(self):
        cases = [
            ('visible consumed refs', True, [(20000, 'consumed')], {}),
            ('live bag UID', False, [(20000, 'rune-0')], {}),
            ('live storage UID', False, [(20000, 'stored-rune')], {'stored-rune': 50000}),
            ('mixed stale and live', False, [(20000, 'consumed'), (20001, 'rune-0')], {}),
            ('unknown UID', False, [(20000, '')], {}),
            ('unknown material', False, [(0, 'consumed')], {}),
        ]
        for label, visible, selections, registry in cases:
            with self.subTest(label=label):
                runtime, payload, _ = self.rune_execution()
                self.cube_state(runtime, visible=visible, selections=selections)
                rows, _, array, capacity = runtime._bag.return_value
                runtime._bag.return_value = rows, registry, array, capacity
                with self.assertRaisesRegex(GameBusyError, '已有玩家选择'):
                    runtime.execute('synthesize_runes', payload)
                runtime._invoke.assert_not_called()

    def test_two_consecutive_batches_clear_their_own_consumed_references(self):
        runtime, payload, receipt = self.rune_execution()
        state = self.cube_state(runtime)
        runtime._confirm = Mock(return_value=receipt)

        def native_batch(calls, *args, **kwargs):
            names = [c.method for c in calls]
            if 'CubePanelUI.gbp' in names:
                state.update(visible=True, animating=True,
                    selections=[(20000+i, uid) for i, uid in enumerate(payload['input_uids'])])
            else:
                self.assertEqual(names, ['CubePanelUI.gbu','CubePanelUI.gbf','PanelBase.Close'])
                state.update(visible=False, animating=False, selections=[(0, '')] * 9)
            return {'sequence': 1}

        runtime._invoke.side_effect = native_batch
        for start in (0, 20):
            rows = [rune(i) for i in range(start, start+9)]
            runtime._bag.return_value = rows, {}, 300, 40
            payload.update(inputs=[dict(r, quantity=1) for r in rows],
                           input_uids=[r['item_uid'] for r in rows])
            result = runtime.execute('synthesize_runes', payload)
            self.assertTrue(result['effect_confirmed'])
            self.assertFalse(result['ui_cleanup_pending'])
            self.assertIsNone(runtime._cleanup_pending)
        self.assertEqual(runtime._invoke.call_count, 4)  # Two crafts, two verified cleanups.

    def test_cleanup_already_finished_does_not_require_another_native_call(self):
        runtime, _, _ = self.rune_execution()
        runtime._invoke.side_effect = AssertionError('already clean')
        self.assertTrue(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        runtime._invoke.assert_not_called()

    def test_pending_receipt_never_resets_the_unconfirmed_animation(self):
        runtime, payload, _ = self.rune_execution()
        runtime._confirm = Mock(side_effect=SynthesisReceiptPending('delayed',uncertain=True))
        runtime._cleanup_cube = Mock(side_effect=AssertionError('unconfirmed receipt'))
        runtime.execute('synthesize_runes',payload)
        runtime._receipt = Mock(return_value=None)
        self.assertTrue(runtime.reconcile('role-a')['synthesis_pending'])
        runtime._cleanup_cube.assert_not_called()

    def test_cleanup_busy_error_does_not_invalidate_confirmed_receipt(self):
        runtime, _, _ = self.rune_execution()
        runtime._context.side_effect = GameBusyError('busy cleanup')
        self.assertFalse(runtime._cleanup_cube(700,True,input_uids={'rune-0'}))
        self.assertFalse(runtime._uncertain)
        runtime._invoke.assert_not_called()

    def test_cleanup_native_error_preserves_receipt_and_only_retries_ui_cleanup(self):
        runtime, payload, receipt = self.rune_execution()
        runtime._confirm = Mock(return_value=receipt)
        state = self.cube_state(runtime)

        def native_batch(calls, *args, **kwargs):
            if any(c.method == 'CubePanelUI.gbp' for c in calls):
                state['animating'] = True
                return {'sequence': 1}
            raise NativeActionError('cleanup delayed', uncertain=True)
        runtime._invoke.side_effect = native_batch
        result = runtime.execute('synthesize_runes', payload)
        self.assertTrue(result['effect_confirmed'])
        self.assertTrue(result['ui_cleanup_pending'])
        self.assertFalse(runtime._uncertain)
        self.assertIsNone(runtime._pending)
        with self.assertRaises(GameBusyError):
            runtime.execute('synthesize_runes', payload)
        self.assertEqual(runtime._invoke.call_count, 2)

    def test_uncertain_native_batch_never_enters_receipt_recovery(self):
        runtime, payload, _ = self.rune_execution()
        runtime._invoke.side_effect = NativeActionError('native timeout',uncertain=True)
        with self.assertRaises(NativeActionError): runtime.execute('synthesize_runes',payload)
        self.assertTrue(runtime._uncertain); self.assertIsNone(runtime._pending)
        with self.assertRaises(NativeActionError): runtime.execute('synthesize_runes',payload)
        self.assertEqual(runtime._invoke.call_count,1)


if __name__ == '__main__':
    unittest.main()
