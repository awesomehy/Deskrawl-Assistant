"""Diagnose access denial without changing process rights or masking OS errors."""
import ctypes as C
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant import native_memory as native


class ProcessAccessTests(unittest.TestCase):
    def test_denial_reports_confirmed_elevation_mismatch_before_reading(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = None
        with patch.object(native, 'K', kernel), \
                patch.object(native.C, 'get_last_error', return_value=5), \
                patch.object(native, 'process_access_diagnostics', return_value={
                    'assistant_elevated': False, 'game_elevated': True}), \
                self.assertRaises(native.ProcessAccessDeniedError) as error:
            native.ProcessMemory(42)
        self.assertEqual((error.exception.pid, error.exception.winerror), (42, 5))
        self.assertIn('检测到游戏以管理员权限运行', str(error.exception))
        kernel.OpenProcess.assert_called_once_with(0x1410, False, 42)
        kernel.ReadProcessMemory.assert_not_called()
        kernel.QueryFullProcessImageNameW.assert_not_called()

    def test_unknown_or_matching_privileges_do_not_claim_admin_is_the_cause(self):
        for assistant, game in ((None, None), (True, True), (False, False), (False, None)):
            message = str(native.ProcessAccessDeniedError(42, {
                'assistant_elevated': assistant, 'game_elevated': game}))
            self.assertNotIn('检测到游戏以管理员权限运行', message)
            self.assertIn('同一 Windows 账户', message)
            self.assertIn('安全软件的拦截记录', message)

    def test_other_windows_errors_are_preserved_without_access_diagnosis(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = None
        with patch.object(native, 'K', kernel), \
                patch.object(native.C, 'get_last_error', return_value=87), \
                patch.object(native, 'process_access_diagnostics') as diagnose, \
                self.assertRaisesRegex(native.MemoryReadError, r'WinError 87'):
            native.ProcessMemory(42)
        diagnose.assert_not_called()

    def test_read_only_diagnosis_closes_target_handle(self):
        kernel = Mock()
        kernel.GetCurrentProcess.return_value = 11
        kernel.OpenProcess.return_value = 22
        with patch.object(native, 'K', kernel), \
                patch.object(native, '_process_elevated', side_effect=[False, True]):
            self.assertEqual(native.process_access_diagnostics(42), {
                'assistant_elevated': False, 'game_elevated': True})
        kernel.OpenProcess.assert_called_once_with(0x1000, False, 42)
        kernel.CloseHandle.assert_called_once_with(22)

    def test_failed_diagnosis_preserves_partial_result_and_closes_handle(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 22
        with patch.object(native, 'K', kernel), \
                patch.object(native, '_process_elevated', side_effect=[False, OSError('denied')]):
            self.assertEqual(native.process_access_diagnostics(42), {
                'assistant_elevated': False, 'game_elevated': None})
        kernel.CloseHandle.assert_called_once_with(22)

    def test_token_query_closes_token_even_when_information_is_unavailable(self):
        for success in (True, False):
            kernel, security = Mock(), Mock()
            def open_token(handle, rights, token):
                self.assertEqual(rights, 0x8)
                token._obj.value = 33
                return True
            def info(token, kind, value, size, received):
                self.assertEqual(kind, 20)
                value._obj.value = 1
                received._obj.value = C.sizeof(native.W.DWORD)
                return success
            security.OpenProcessToken.side_effect = open_token
            security.GetTokenInformation.side_effect = info
            with patch.object(native, 'K', kernel), patch.object(native, 'A', security):
                self.assertIs(native._process_elevated(22), True if success else None)
            kernel.CloseHandle.assert_called_once()
            self.assertEqual(kernel.CloseHandle.call_args.args[0].value, 33)


if __name__ == '__main__': unittest.main()
