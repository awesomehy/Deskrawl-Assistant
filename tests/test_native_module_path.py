"""Module directories are compared by filesystem identity, not path spelling."""
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.native_reader import NativeReader


class ReachedBuildVerification(Exception):
    pass


class ModulePathTests(unittest.TestCase):
    def reader(self, process_directory, module_directory):
        reader = object.__new__(NativeReader)
        reader.memory = SimpleNamespace(path=process_directory/'Deskrawl.exe',
            modules={'gameassembly.dll':{'path':str(module_directory/'GameAssembly.dll')}})
        reader.on_compatibility = lambda state: None
        return reader

    def test_same_directory_reaches_normal_build_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spellings = [root, root/'..'/root.name]
            if os.name == 'nt': spellings.append(Path('\\\\?\\'+str(root)))
            for module in spellings:
                with self.subTest(module=module), patch('deskrawl_assistant.game_compatibility.resolve',
                        side_effect=ReachedBuildVerification):
                    with self.assertRaises(ReachedBuildVerification):
                        self.reader(root,module)._verify_build()

    def test_different_directory_is_still_rejected_before_build_verification(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with patch('deskrawl_assistant.game_compatibility.resolve') as resolve:
                with self.assertRaisesRegex(MemoryReadError,'路径与进程不一致'):
                    self.reader(Path(first),Path(second))._verify_build()
                resolve.assert_not_called()

    def test_unreadable_directory_does_not_bypass_verification(self):
        with patch.object(Path,'samefile',side_effect=PermissionError('access denied')):
            with self.assertRaisesRegex(MemoryReadError,'无法核验'):
                self.reader(Path('game'),Path('alias'))._verify_build()
