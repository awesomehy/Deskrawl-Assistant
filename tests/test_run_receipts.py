"""Verified native wins survive unrelated control reads and native restarts.

All native reads use a byte-backed fixture; no process is opened and no game
operation is submitted by these tests.
"""
from contextlib import ExitStack
from copy import deepcopy
import struct
import unittest
from unittest.mock import patch

from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.run_receipts import RunReceipts
from tests.test_completion_type_initialization import CompletionFixture


def win(run_id='run-1', **changes):
    return {'id': run_id, 'status': 'settling', 'completion_confirmed': True,
            'dungeon_id': 'map-a', 'difficulty': 'Normal', **changes}


def campaign(run_id='run-1', *, confirmed=True, character_id='fixture-character'):
    return {'available': True, 'source': 'verified_game_memory', 'run': {
        'id': run_id, 'map_id': 'map-a', 'difficulty': 'Normal',
        'wave_count': 10, 'wave_index': 10 if confirmed else 0,
        'wave_active': not confirmed, 'enemy_count': 0 if confirmed else 5,
        'finished': False, 'final_kill_settled': confirmed,
        'completion_counter': {'id': 'fixture-counter', 'count': 17},
    }, 'profile': {
        'character_id': character_id, 'difficulty': 'Normal',
        'unlocked_maps': {'Normal': ['map-a']},
        'completed_maps': {'Normal': ['map-a']}, 'entry_items': {},
        'mythic_min_difficulty': 'Inferno', 'mythic_unlocked': [],
        'mythic_completed': [], 'is_demo': False,
    }}


class RunReceiptsTests(unittest.TestCase):
    def test_only_confirmed_wins_are_retained_and_run_ids_are_deduplicated(self):
        receipts = RunReceipts()
        for run in (None, {'id': 'running', 'status': 'running'},
                    {'id': 'dead', 'status': 'failed', 'completion_confirmed': True},
                    {'id': 'unconfirmed', 'completion_confirmed': 1},
                    {'status': 'success'}):
            self.assertEqual(receipts.observe('session-a', 'char-a', run)['sequence'], 0)
        first = receipts.observe('session-a', 'char-a', win())
        repeated = receipts.observe('session-a', 'char-a', win(status='success'))
        self.assertEqual(first, repeated)
        self.assertEqual(first['sequence'], 1)
        self.assertEqual(len(first['receipts']), 1)
        finished = receipts.observe('session-a', 'char-a', win('run-2', status='success',
                                                             completion_confirmed=False))
        self.assertEqual(finished['sequence'], 2)

    def test_new_running_plan_retains_the_previous_verified_win(self):
        receipts = RunReceipts()
        first = receipts.observe('session-a', 'char-a', win())
        self.assertEqual(receipts.observe('session-a', 'char-a',
                                         {'id': 'run-2', 'status': 'running'}), first)

    def test_scope_changes_clear_receipts_but_never_rewind_sequence(self):
        receipts = RunReceipts()
        receipts.observe('session-a', 'char-a', win())
        changed = receipts.observe('session-a', 'char-b', None)
        self.assertEqual(changed, {'session': 'session-a', 'character_id': 'char-b',
                                   'sequence': 1, 'receipts': []})
        receipts.observe('session-a', 'char-b', win())
        changed = receipts.observe('session-b', 'char-b', None)
        self.assertEqual(changed['sequence'], 2)
        self.assertEqual(changed['receipts'], [])
        self.assertEqual(receipts.observe('session-b', 'char-b', win())['sequence'], 3)

    def test_unknown_scope_does_not_expose_previous_character_receipts(self):
        receipts = RunReceipts()
        receipts.observe('session-a', 'char-a', win())
        for session, character in ((None, 'char-a'), ('session-a', None),
                                    ('', 'char-a'), ('session-a', ' ')):
            self.assertIsNone(receipts.observe(session, character, win('run-2')))

    def test_buffer_is_bounded_and_caller_mutation_cannot_change_evidence(self):
        receipts = RunReceipts()
        run = win(completion_counter={'id': 'counter-a', 'count': 1})
        first = receipts.observe('session-a', 'char-a', run)
        run['completion_counter']['count'] = 999
        first['receipts'][0]['run']['completion_counter']['count'] = 500
        self.assertEqual(receipts.snapshot()['receipts'][0]['run']['completion_counter']['count'], 1)
        for index in range(2, 301):
            receipts.observe('session-a', 'char-a', win(f'run-{index}'))
        result = receipts.snapshot()
        self.assertEqual(result['sequence'], 300)
        self.assertEqual(len(result['receipts']), 256)
        self.assertEqual(result['receipts'][0]['sequence'], 45)
        self.assertEqual(result['receipts'][-1]['sequence'], 300)


class NativeRunReceiptBufferTests(unittest.TestCase):
    def setUp(self):
        self.fixture = CompletionFixture()
        self.fixture.context()
        self.fixture.reader.session_id = 'fixture-session'
        self.game = self.fixture.game
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (
                ('capabilities', {'compatible': True}),
                ('_map_objects', {}),
                ('_carriage', {'available': True, 'complete': True, 'items': []})):
            self.stack.enter_context(patch.object(self.game, name, return_value=value))
        # Each control read appends Mythic to this list, so supply a fresh one.
        self.stack.enter_context(patch.object(self.game, '_difficulty_state',
            side_effect=lambda *_: ('Normal', [{'id': 'Normal', 'value': 0, 'unlocked': True}])))
        self.ground = self.stack.enter_context(patch.object(self.game, '_ground', return_value=[]))
        for name, value in (
                ('deskrawl_assistant.recommendation_reader._catalog', {'maps': []}),
                ('deskrawl_assistant.game_loot_ledger.read_loot_ledger', {'available': True}),
                ('deskrawl_assistant.game_panel_log.read_panel_log', {'available': True}),
                ('deskrawl_assistant.statistics_reader.read_gold_ledger', {'available': False})):
            self.stack.enter_context(patch(name, return_value=value))

    def read(self, value=None):
        with patch('deskrawl_assistant.recommendation_reader.read_campaign',
                   return_value=value if value is not None else campaign()) as read:
            result = self.game.control_snapshot()
        read.assert_called_once_with(self.fixture.reader)
        self.fixture.dispatcher.invoke.assert_not_called()
        return result

    def assert_one_win(self, result):
        history = result['run_history']
        self.assertEqual(history['session'], 'fixture-session')
        self.assertEqual(history['character_id'], 'fixture-character')
        self.assertEqual(history['sequence'], 1)
        self.assertEqual([row['run']['id'] for row in history['receipts']], ['run-1'])
        self.assertIs(history['receipts'][0]['run']['completion_confirmed'], True)
        self.assertEqual(history['receipts'][0]['run']['status'], 'settling')

    def test_confirmed_native_repeat_survives_next_running_plan(self):
        first = self.read()
        self.assertTrue(first['available'], first['reason'])
        self.assert_one_win(first)
        receipt = first['run_history']['receipts'][0]['run']
        self.assertEqual(receipt['dungeon_id'], 'map-a')
        self.assertEqual(receipt['completion_counter'], {'id': 'fixture-counter', 'count': 17})
        second = self.read(campaign('run-2', confirmed=False))
        self.assertTrue(second['available'], second['reason'])
        self.assertEqual(second['run']['id'], 'run-2')
        self.assertEqual(second['run_history'], first['run_history'])

    def test_later_ground_or_ui_failure_keeps_already_guarded_receipt(self):
        for method in ('_ground', '_completion_state', '_carriage'):
            with self.subTest(method=method):
                with patch.object(self.game, method, side_effect=MemoryReadError('transient native read')):
                    result = self.read()
                self.assertFalse(result['available'])
                self.assert_one_win(result)
        recovered = self.read(campaign('run-2', confirmed=False))
        self.assertTrue(recovered['available'], recovered['reason'])
        self.assert_one_win(recovered)

    def test_later_full_snapshot_guard_failure_keeps_independent_receipt(self):
        def change_area(*_):
            self.fixture.memory.put(self.fixture.objects['GameAreaManager'] + 176, b'\1')
            return []
        self.ground.side_effect = change_area
        result = self.read()
        self.assertFalse(result['available'])
        self.assertIn('正在变化', result['reason'])
        self.assert_one_win(result)

    def test_campaign_failure_does_not_reuse_a_previous_identity_scope(self):
        self.assert_one_win(self.read())
        result = self.read({'available': False, 'reason': 'campaign unavailable'})
        self.assertFalse(result['available'])
        self.assertNotIn('run_history', result)

    def test_dead_character_and_unconfirmed_plans_never_create_receipts(self):
        for confirmed, dead in ((False, False), (True, True)):
            with self.subTest(confirmed=confirmed, dead=dead):
                self.fixture.memory.put(self.fixture.objects['Player'] + 296, bytes((dead,)))
                result = self.read(campaign(confirmed=confirmed))
                self.assertTrue(result['available'], result['reason'])
                self.assertEqual(result['run_history']['receipts'], [])
                self.assertEqual(result['run_history']['sequence'], 0)

    def test_live_death_or_role_slot_race_invalidates_receipt(self):
        for obj, offset, raw in (('Player', 296, b'\1'),
                                 ('SaveSystem', 36, struct.pack('<i', 1))):
            with self.subTest(object=obj):
                address = self.fixture.objects[obj] + offset
                original = self.fixture.memory.read(address, len(raw))
                def race(_):
                    self.fixture.memory.put(address, raw)
                    return campaign()
                with patch('deskrawl_assistant.recommendation_reader.read_campaign', side_effect=race):
                    result = self.game.control_snapshot()
                self.assertFalse(result['available'])
                self.assertNotIn('run_history', result)
                self.assertEqual(self.game._run_receipts.snapshot()['receipts'], [])
                self.fixture.memory.put(address, original)

    def test_identity_change_clears_old_runs_without_reusing_sequence(self):
        self.assert_one_win(self.read())
        result = self.read(campaign('run-2', confirmed=False, character_id='other-character'))
        self.assertTrue(result['available'], result['reason'])
        self.assertEqual(result['run_history'], {'session': 'fixture-session',
                         'character_id': 'other-character', 'sequence': 1, 'receipts': []})
        result = self.read(campaign('run-3', character_id='other-character'))
        self.assertEqual(result['run_history']['sequence'], 2)
        self.assertEqual(result['run_history']['receipts'][0]['sequence'], 2)
        self.fixture.reader.session_id = 'other-session'
        result = self.read(campaign('run-4', confirmed=False, character_id='other-character'))
        self.assertEqual(result['run_history'], {'session': 'other-session',
                         'character_id': 'other-character', 'sequence': 2, 'receipts': []})

    def test_supplied_campaign_is_compatible_but_cannot_create_native_history(self):
        self.assert_one_win(self.read())
        with patch('deskrawl_assistant.recommendation_reader.read_campaign') as read:
            result = self.game.control_snapshot(deepcopy(campaign('run-2')))
        read.assert_not_called()
        self.assertTrue(result['available'], result['reason'])
        self.assertIsNone(result['run_history'])
        self.assertEqual(self.game._run_receipts.snapshot()['sequence'], 1)


if __name__ == '__main__':
    unittest.main()
