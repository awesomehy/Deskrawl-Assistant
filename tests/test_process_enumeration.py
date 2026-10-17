"""Toolhelp enumeration failures must not look like an empty game list."""
import ctypes as C
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant import native_memory


class ProcessEnumerationTests(unittest.TestCase):
    def enumerate(self, rows, error=18):
        kernel = Mock()
        kernel.CreateToolhelp32Snapshot.return_value = 123
        remaining = iter(rows)

        def read_row(_snapshot, pointer):
            row = next(remaining, None)
            if row is None:
                return False
            target = pointer._obj
            target.th32ProcessID, target.szExeFile = row
            return True

        kernel.Process32FirstW.side_effect = read_row
        kernel.Process32NextW.side_effect = read_row
        with patch.object(native_memory, 'K', kernel), \
                patch.object(native_memory.C, 'get_last_error', return_value=error):
            try:
                return native_memory.game_pids()
            finally:
                kernel.CloseHandle.assert_called_once_with(123)

    def test_normal_empty_snapshot_is_not_an_error(self):
        self.assertEqual(self.enumerate([]), [])

    def test_normal_completion_filters_case_insensitively(self):
        self.assertEqual(self.enumerate([(7, 'other.exe'), (42, 'Deskrawl.EXE'),
                                         (43, 'deskrawl.exe')]), [42, 43])

    def test_first_failure_does_not_authorize_an_empty_list(self):
        for error in (0, 5, 24):
            with self.subTest(error=error), self.assertRaises(OSError) as raised:
                self.enumerate([], error)
            self.assertEqual(raised.exception.winerror, error)

    def test_next_failure_does_not_return_a_partial_process_list(self):
        for rows in ([(42, 'deskrawl.exe')], [(7, 'other.exe')]):
            with self.subTest(rows=rows), self.assertRaises(OSError) as raised:
                self.enumerate(rows, 5)
            self.assertEqual(raised.exception.winerror, 5)

    def test_snapshot_creation_failure_preserves_error_without_closing_invalid_handle(self):
        kernel = Mock()
        kernel.CreateToolhelp32Snapshot.return_value = C.c_void_p(-1).value
        with patch.object(native_memory, 'K', kernel), \
                patch.object(native_memory.C, 'get_last_error', return_value=5), \
                self.assertRaises(OSError) as raised:
            native_memory.game_pids()
        self.assertEqual(raised.exception.winerror, 5)
        kernel.CloseHandle.assert_not_called()
        kernel.Process32FirstW.assert_not_called()


if __name__ == '__main__':
    unittest.main()
