"""Native cumulative victories and scoped receipts, without game writes."""
from copy import deepcopy
import unittest

from deskrawl_assistant.managed_mode import ManagedMode
from tests.test_managed_mode import equipment, observation, settings


class RepeatAccountingTests(unittest.TestCase):
    def setup_repeat(self, *, counter=0, quota=300):
        config = settings()
        config['farming']['repeat_maps'] = [
            {'dungeon_id': 'a', 'difficulty': 'Normal', 'runs': quota},
            {'dungeon_id': 'b', 'difficulty': 'Normal', 'runs': quota},
        ]
        world = observation()
        world.update(character_id='character-a')
        world['scene'].update(difficulty='Normal')
        world['scene']['run'].update(dungeon_id='a', difficulty='Normal',
                                    completion_counter={'id': 'counter-a', 'count': counter})
        world['run_history'] = {'session': world['session'], 'character_id': 'character-a',
                                'sequence': 0, 'receipts': []}
        engine = ManagedMode(config)
        engine.start(world)
        return engine, world

    def update_run(self, world, name, count, *, win=False):
        world['scene']['run'].update(id=name, status='settling' if win else 'running',
                                     completion_confirmed=win)
        world['scene']['run']['completion_counter']['count'] = count

    def append_receipt(self, world):
        history = world['run_history']
        history['sequence'] += 1
        history['receipts'].append({'sequence': history['sequence'], 'run': deepcopy(world['scene']['run'])})

    def test_native_counter_recovers_several_entirely_missed_completion_windows(self):
        engine, world = self.setup_repeat(counter=37)
        self.update_run(world, 'new-run', 41)
        for _ in range(3): engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 4)
        self.update_run(world, 'new-run', 42, win=True)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 5)

    def test_unchanged_or_empty_carriage_does_not_gate_completed_rounds(self):
        for has_existing_item in (False, True):
            with self.subTest(has_existing_item=has_existing_item):
                engine, world = self.setup_repeat()
                if has_existing_item:
                    world['carriage']['items'] = [equipment('existing', container='carriage')]
                original = deepcopy(world['carriage'])
                for number in (1, 2, 3):
                    self.update_run(world, f'win-{number}', number, win=True)
                    engine.next_action(world)
                    self.assertEqual(engine.repeat_completed, number)
                    self.assertEqual(world['carriage'], original)
                self.update_run(world, 'native-next', 3)
                for _ in range(3): engine.next_action(world)
                self.assertEqual(engine.repeat_completed, 3)

    def test_history_and_counter_and_popup_do_not_duplicate_the_same_victories(self):
        engine, world = self.setup_repeat()
        for number in (1, 2):
            self.update_run(world, f'win-{number}', number, win=True)
            self.append_receipt(world)
        self.update_run(world, 'native-next', 2)
        for _ in range(4): engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 2)
        self.update_run(world, 'native-next', 3, win=True)
        self.append_receipt(world)
        engine.observe_runs(world)
        world['scene']['run']['status'] = 'success'
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 3)

    def test_verified_receipt_survives_failed_full_control_and_inventory_reads(self):
        engine, world = self.setup_repeat()
        self.update_run(world, 'short-win', 1, win=True)
        self.append_receipt(world)
        world.update(complete=False, character_id=None)
        world['scene'] = {'available': False, 'run': None}
        for _ in range(3): engine.next_action(world)
        self.assertEqual(engine.repeat_completed, 1)
        self.assertTrue(engine.running)

    def test_history_does_not_import_wins_before_start(self):
        engine, world = self.setup_repeat(counter=10)
        self.update_run(world, 'before-start', 10, win=True)
        self.append_receipt(world)
        engine.stop()
        engine.start(world)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 0)
        self.update_run(world, 'after-start', 11, win=True)
        self.append_receipt(world)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)

    def test_resume_baselines_wins_during_pause_and_preserves_prior_count(self):
        engine, world = self.setup_repeat()
        self.update_run(world, 'first', 1, win=True)
        self.append_receipt(world)
        engine.observe_runs(world)
        engine.pause()
        self.update_run(world, 'while-paused', 4, win=True)
        self.append_receipt(world)
        engine.resume(world)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)
        self.update_run(world, 'after-resume', 5, win=True)
        self.append_receipt(world)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 2)

    def test_other_character_or_bridge_history_is_not_counted(self):
        for key in ('session', 'character_id'):
            engine, world = self.setup_repeat()
            self.update_run(world, 'wrong-scope', 1, win=True)
            self.append_receipt(world)
            world['run_history'][key] = 'another'
            world['scene'] = {'available': False, 'run': None}
            engine.observe_runs(world)
            self.assertEqual(engine.repeat_completed, 0)

    def test_reset_counter_and_retained_terminal_never_reimport_old_wins(self):
        engine, world = self.setup_repeat(counter=20)
        self.update_run(world, 'win', 21, win=True)
        self.append_receipt(world)
        engine.observe_runs(world)
        self.update_run(world, 'next', 23)
        engine.observe_runs(world)
        retained = deepcopy(world['run_history']['receipts'][0]['run'])
        retained['retained_terminal'] = True
        world['scene']['run'] = retained
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 3)
        self.update_run(world, 're-entered', 0)
        world['scene']['run'].pop('retained_terminal')
        engine.observe_runs(world)
        self.update_run(world, 're-entered', 1, win=True)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 4)

    def test_failed_and_stargazing_exits_without_counter_increment_stay_zero(self):
        engine, world = self.setup_repeat()
        world['scene']['run']['status'] = 'failed'
        engine.observe_runs(world)
        engine.stargazing_skipped.add(world['scene']['run']['id'])
        self.update_run(world, 'skipped', 0)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 0)

    def test_map_counter_identity_changes_do_not_import_prior_rounds(self):
        engine, world = self.setup_repeat()
        world['scene']['run']['completion_counter'] = {'id': 'new-wave', 'count': 50}
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 0)
        self.update_run(world, 'first-seen-win', 51, win=True)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)

    def test_delayed_batch_never_credits_other_map_with_source_map_victories(self):
        engine, world = self.setup_repeat(quota=2)
        self.update_run(world, 'too-fast', 3)
        engine.observe_runs(world)
        self.assertEqual((engine.target_dungeon, engine.repeat_completed), ('b', 0))

    def test_confirmed_map_reentry_resets_reused_counter_before_first_sample(self):
        engine, world = self.setup_repeat(quota=2)
        self.update_run(world, 'a-finished', 2, win=True)
        engine.observe_runs(world)
        world['scene']['run'].update(dungeon_id='b', difficulty='Normal',
            completion_counter={'id': 'counter-b', 'count': 0}, status='running', completion_confirmed=False)
        engine.observe_runs(world)
        self.update_run(world, 'b-finished', 2, win=True)
        engine.observe_runs(world)
        self.assertEqual(engine.target_dungeon, 'a')
        action = {'action_id': 'accepted-reentry', 'kind': 'enter_dungeon',
                  'dungeon_id': 'a', 'difficulty': 'Normal'}
        engine.expect_run_entry(action, world)
        # The zero sample was missed; this reused native identity now holds
        # victories from this entry, not the preceding visit to map A.
        world['scene']['run'].update(dungeon_id='a', difficulty='Normal',
            completion_counter={'id': 'counter-a', 'count': 1})
        self.update_run(world, 'a-already-repeating', 1)
        for _ in range(3): engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)
        engine.pending = action
        engine.complete(action['action_id'], result={'verified': True})
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)

    def test_pending_entry_does_not_reset_the_still_active_source_run(self):
        engine, world = self.setup_repeat(counter=10)
        action = {'action_id': 'entry', 'kind': 'enter_dungeon', 'dungeon_id': 'a', 'difficulty': 'Normal'}
        engine.expect_run_entry(action, world)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 0)
        self.assertFalse(engine.run_entry['consumed'])

    def test_entry_fallback_win_is_not_credited_again_when_native_counter_returns(self):
        engine, world = self.setup_repeat(counter=10)
        action = {'action_id': 'new-entry', 'kind': 'enter_dungeon',
                  'dungeon_id': 'a', 'difficulty': 'Normal'}
        engine.expect_run_entry(action, world)
        self.update_run(world, 'new-entry-win', 1, win=True)
        # A valid success can be observed before its optional native counter.
        world['scene']['run'].pop('completion_counter')
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)
        # Explicit map entry resets the cumulative counter but reuses its id.
        # Its first sample includes the already-credited fallback victory.
        world['scene']['run']['completion_counter'] = {'id': 'counter-a', 'count': 1}
        for _ in range(3): engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)
        self.update_run(world, 'next-entry-win', 2, win=True)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 2)


if __name__ == '__main__':
    unittest.main()
