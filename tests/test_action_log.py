"""Only replaceable diagnostics rotate; I/O failure never blocks game operations."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant import action_log


class ActionLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'assistant-actions.jsonl'
        runtime = patch.object(action_log, 'runtime_dir', return_value=self.root)
        runtime.start()
        self.addCleanup(runtime.stop)

    def read(self, path=None):
        return [json.loads(line) for line in (path or self.path).read_text(encoding='utf-8').splitlines()]

    def test_utf8_bytes_rotate_to_four_files_and_keep_complete_recent_records(self):
        with patch.object(action_log, 'MAX_BYTES', 400):
            for i in range(8):
                self.assertTrue(action_log.record('操作', sequence=i, text='测' * 75))
        paths = sorted(self.root.iterdir())
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(path.stat().st_size <= 400 for path in paths))
        records = [row for path in paths for row in self.read(path)]
        self.assertEqual(sorted(row['sequence'] for row in records), [4, 5, 6, 7])
        self.assertTrue(all(row['event'] == '操作' and row['text'] == '测' * 75 for row in records))

    def test_preexisting_oversized_log_is_preserved_as_a_complete_backup(self):
        legacy = b'old diagnostic\n' * 1000
        self.path.write_bytes(legacy)
        with patch.object(action_log, 'MAX_BYTES', 256):
            self.assertTrue(action_log.record('first'))
        self.assertEqual(self.path.with_name(self.path.name + '.1').read_bytes(), legacy)
        self.assertEqual(self.read()[0]['event'], 'first')

    def test_oversized_new_diagnostic_is_observably_summarized_within_limit(self):
        with patch.object(action_log, 'MAX_BYTES', 512):
            self.assertTrue(action_log.record('large_snapshot', snapshot='大' * 1000))
        record = self.read()[0]
        self.assertEqual(record['event'], 'large_snapshot')
        self.assertTrue(record['details_omitted'])
        self.assertGreater(record['original_bytes'], 3000)
        self.assertLessEqual(self.path.stat().st_size, 512)

    def test_write_failure_is_false_and_does_not_raise(self):
        with patch.object(Path, 'open', side_effect=PermissionError('locked')):
            self.assertFalse(action_log.record('operation_complete'))

    def test_rotation_failure_leaves_current_log_intact_and_is_reported(self):
        legacy = b'keep this diagnostic\n' * 100
        self.path.write_bytes(legacy)
        with patch.object(action_log, 'MAX_BYTES', 256), \
             patch.object(Path, 'replace', side_effect=PermissionError('locked')):
            self.assertFalse(action_log.record('operation_complete'))
        self.assertEqual(self.path.read_bytes(), legacy)

    def test_serialization_programming_errors_are_not_swallowed(self):
        with self.assertRaises(TypeError):
            action_log.record('bad-caller', unsupported=object())
        self.assertFalse(self.path.exists())

    def test_concurrent_writers_preserve_whole_json_lines(self):
        with ThreadPoolExecutor(8) as pool:
            written = list(pool.map(lambda i: action_log.record('operation_complete', sequence=i), range(100)))
        self.assertTrue(all(written))
        self.assertEqual(sorted(row['sequence'] for row in self.read()), list(range(100)))

    def test_only_owned_diagnostic_names_rotate(self):
        sentinel = self.root / 'activity.sqlite3'
        sentinel.write_bytes(b'activity and receipt history')
        unrelated = self.root / 'assistant-actions.jsonl.keep'
        unrelated.write_bytes(b'user archive')
        with patch.object(action_log, 'MAX_BYTES', 256):
            for i in range(12):
                self.assertTrue(action_log.record('diagnostic', sequence=i, text='x' * 100))
        self.assertEqual(sentinel.read_bytes(), b'activity and receipt history')
        self.assertEqual(unrelated.read_bytes(), b'user archive')


if __name__ == '__main__':
    unittest.main()
