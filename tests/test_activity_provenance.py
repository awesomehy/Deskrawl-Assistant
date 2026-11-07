"""Legacy provenance migrates without pruning receipts or weakening deduplication."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.activity_journal import ActivityJournal


def purchase(uid=None, **result):
    return {'action': {'kind': 'merchant_buy'},
            'result': {'verified': True, 'received_uid': uid, **result}}


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'activity.sqlite3'

    def legacy(self, entries):
        # This is the original schema, without any new projection or cursor.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                category TEXT NOT NULL, event TEXT NOT NULL, message TEXT NOT NULL,
                item_key TEXT NOT NULL, item_name TEXT NOT NULL, item_kind TEXT NOT NULL,
                quantity INTEGER, source TEXT, destination TEXT, context TEXT NOT NULL,
                event_key TEXT UNIQUE)''')
            db.executemany('''INSERT INTO events
                (time,category,event,message,item_key,item_name,item_kind,context,event_key)
                VALUES ('old','system',?,'old receipt','','','',?,?)''', entries)

    def projection(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = dict(db.execute('SELECT item_uid,event_id FROM merchant_item_provenance'))
            cursor = db.execute('SELECT last_event_id FROM journal_migrations').fetchone()[0]
            count = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        return rows, cursor, count

    def test_old_receipts_preserve_all_confirmed_uids_and_leave_history_untouched(self):
        contexts = [purchase('one'), purchase(received_uids=['two', 'three', 'two', '', 42]),
                    purchase('uncertain', verified=False), purchase('not-boolean', verified=1),
                    {'action': {'kind': 'transfer'}, 'result': {'verified': True, 'received_uid': 'other'}},
                    None, [], {'action': [], 'result': 'bad'}, purchase(received_uids='invalid-list')]
        entries = [('managed_action', json.dumps(value), f'old-{index}')
                   for index, value in enumerate(contexts)]
        entries += [('managed_action', '{broken json', 'broken'),
                    ('not_managed', json.dumps(purchase('wrong-event')), 'wrong-event')]
        self.legacy(entries)
        journal = ActivityJournal(self.path)
        with patch('deskrawl_assistant.activity_journal.PROVENANCE_BATCH_SIZE', 2):
            self.assertEqual(journal.merchant_item_uids(), {'one', 'two', 'three'})
        rows, cursor, count = self.projection()
        self.assertEqual((cursor, count), (len(entries), len(entries)))
        self.assertEqual(rows, {'one': 1, 'two': 2, 'three': 2})
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('SELECT event,context,event_key FROM events ORDER BY id').fetchall(), entries)

    def test_interrupted_batch_rolls_back_evidence_and_cursor_then_resumes(self):
        self.legacy([('managed_action', json.dumps(purchase(str(i))), str(i)) for i in range(1, 6)])
        journal = ActivityJournal(self.path)
        original = journal._index_merchant_receipt

        def fail_mid_batch(db, event_id, event, raw):
            original(db, event_id, event, raw)
            if event_id == 4:
                raise OSError('simulated interrupted migration')

        with patch('deskrawl_assistant.activity_journal.PROVENANCE_BATCH_SIZE', 2), \
             patch.object(journal, '_index_merchant_receipt', side_effect=fail_mid_batch):
            with self.assertRaises(OSError):
                journal.merchant_item_uids()
        self.assertEqual(self.projection(), ({'1': 1, '2': 2}, 2, 5))
        resumed = ActivityJournal(self.path)
        with patch.object(resumed, '_index_merchant_receipt', wraps=original) as indexed:
            self.assertEqual(resumed.merchant_item_uids(), {'1', '2', '3', '4', '5'})
        self.assertEqual([call.args[1] for call in indexed.call_args_list], [3, 4, 5])

    def test_new_append_during_legacy_backlog_cannot_skip_old_receipts(self):
        self.legacy([('managed_action', json.dumps(purchase('old')), 'old')])
        journal = ActivityJournal(self.path)
        journal.append('system', 'managed_action', 'new', context=purchase(received_uids=['old', 'new']))
        self.assertEqual(self.projection(), ({'old': 2, 'new': 2}, 0, 2))
        self.assertEqual(journal.merchant_item_uids(), {'old', 'new'})
        self.assertEqual(self.projection(), ({'old': 1, 'new': 2}, 2, 2))

    def test_new_writes_and_restarts_do_not_reparse_completed_history(self):
        journal = ActivityJournal(self.path)
        journal.append('system', 'managed_action', 'new', context=purchase('known'))
        journal.append('system', 'unrelated', 'large diagnostic', context={'body': 'x' * 65536})
        restarted = ActivityJournal(self.path)
        with patch('deskrawl_assistant.activity_journal.json.loads', side_effect=AssertionError('history reparsed')):
            self.assertEqual(restarted.merchant_item_uids(), {'known'})
        self.assertEqual(self.projection()[1:], (2, 2))

    def test_older_version_append_after_migration_is_caught_up_on_next_restart(self):
        journal = ActivityJournal(self.path)
        journal.append('system', 'managed_action', 'new', context=purchase('first'))
        journal.merchant_item_uids()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''INSERT INTO events
                (time,category,event,message,item_key,item_name,item_kind,context)
                VALUES ('old','system','managed_action','old writer','','','',?)''',
                (json.dumps(purchase('from-old-version')),))
        restarted = ActivityJournal(self.path)
        restarted.append('system', 'managed_action', 'latest', context=purchase('latest'))
        self.assertEqual(self.projection()[1], 1)
        self.assertEqual(restarted.merchant_item_uids(), {'first', 'from-old-version', 'latest'})
        self.assertEqual(self.projection()[1:], (3, 3))

    def test_event_key_conflict_cannot_inject_unwritten_purchase_provenance(self):
        journal = ActivityJournal(self.path)
        journal.append('system', 'unrelated', 'original', event_key='same-key')
        self.assertEqual(journal.append('system', 'managed_action', 'duplicate',
                                       context=purchase('forged'), event_key='same-key'), [])
        self.assertEqual(journal.merchant_item_uids(), set())
        self.assertEqual(self.projection(), ({}, 1, 1))

    def test_failed_append_transaction_retains_neither_receipt_nor_projection(self):
        journal = ActivityJournal(self.path)
        journal.append('system', 'managed_action', 'confirmed', context=purchase('before'))
        with self.assertRaises(ValueError):
            journal.append_many([
                {'category': 'system', 'event': 'managed_action', 'message': 'rolled back', 'context': purchase('never')},
                {'category': 'invalid', 'event': 'bad', 'message': 'invalid'}])
        self.assertEqual(self.projection(), ({'before': 1}, 1, 1))

    def test_large_unrelated_history_is_batched_and_its_context_is_not_decoded(self):
        self.legacy([('unrelated', 'not JSON: ' + 'x' * 2048, str(i)) for i in range(1030)] +
                    [('managed_action', json.dumps(purchase('last')), 'receipt')])
        journal = ActivityJournal(self.path)
        original_connect = journal._connect
        statements = []
        from contextlib import contextmanager

        @contextmanager
        def traced():
            with original_connect() as db:
                db.set_trace_callback(statements.append)
                yield db

        real_loads = json.loads
        with patch.object(journal, '_connect', traced), \
             patch('deskrawl_assistant.activity_journal.json.loads', wraps=real_loads) as decode:
            self.assertEqual(journal.merchant_item_uids(), {'last'})
        self.assertEqual(decode.call_count, 1)
        scans = [sql for sql in statements if 'FROM events WHERE id>' in sql]
        self.assertEqual(len(scans), 5)
        self.assertTrue(all('LIMIT 256' in sql and 'OFFSET' not in sql for sql in scans))
        self.assertEqual(self.projection()[1:], (1031, 1031))

    def test_two_journal_instances_keep_atomic_progress_during_migration_and_append(self):
        self.legacy([('managed_action', json.dumps(purchase(f'old-{i}')), str(i)) for i in range(20)])
        first, second = ActivityJournal(self.path), ActivityJournal(self.path)
        with patch('deskrawl_assistant.activity_journal.PROVENANCE_BATCH_SIZE', 2), ThreadPoolExecutor(2) as pool:
            migration = pool.submit(first.merchant_item_uids)
            writing = pool.submit(lambda: [second.append('system', 'managed_action', 'new',
                context=purchase(f'new-{i}')) for i in range(20)])
            migration.result(timeout=10)
            writing.result(timeout=10)
        self.assertEqual(ActivityJournal(self.path).merchant_item_uids(),
                         {f'{prefix}-{i}' for prefix in ('old', 'new') for i in range(20)})
        self.assertEqual(self.projection()[1:], (40, 40))


if __name__ == '__main__':
    unittest.main()
