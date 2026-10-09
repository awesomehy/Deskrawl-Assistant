"""Persistent data must never be stored inside one-file extraction folders."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant import paths
from deskrawl_assistant.action_log import record
from deskrawl_assistant.runtime_client import write_runtime_json


class PackagedPathsTests(unittest.TestCase):
    def test_development_preserves_existing_configuration_location(self):
        with patch.object(sys, 'frozen', False, create=True), patch.dict(os.environ, {'DESKRAWL_ASSISTANT_DATA_DIR': ''}):
            self.assertEqual(paths.rules_file(), paths.RESOURCE_ROOT / 'config/lock-rules.json')

    def test_frozen_configuration_is_persistent_outside_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sys, 'frozen', True, create=True), patch.dict(os.environ, {'LOCALAPPDATA': folder, 'DESKRAWL_ASSISTANT_DATA_DIR': ''}):
                self.assertEqual(paths.data_root(), Path(folder) / 'Deskrawl装备助手')
                self.assertNotEqual(paths.data_root(), paths.RESOURCE_ROOT)

    def test_runtime_snapshots_and_logs_use_the_same_user_directory(self):
        with tempfile.TemporaryDirectory(prefix='中文目录 ') as folder:
            with patch.dict(os.environ, {'DESKRAWL_ASSISTANT_DATA_DIR': folder}):
                target = write_runtime_json('test.json', {'text': '装备数据'})
                record('packaging_path_test')
                self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'text': '装备数据'})
                log = paths.runtime_dir() / 'assistant-actions.jsonl'
                self.assertEqual(json.loads(log.read_text(encoding='utf-8'))['event'], 'packaging_path_test')
                with self.assertRaises(ValueError):
                    write_runtime_json('../outside.json', {})

    def test_installed_cache_can_be_redirected_without_global_environment_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            exe=Path(folder)/'assistant.exe'
            cache=Path(folder)/'cache'
            exe.with_name('runtime-paths.json').write_text(json.dumps({'cache_dir':str(cache)}),encoding='utf-8')
            with patch.object(sys,'frozen',True,create=True), patch.object(sys,'executable',str(exe)):
                with patch.dict(os.environ,{'DESKRAWL_ASSISTANT_CACHE_DIR':''}):
                    self.assertEqual(paths.cache_root(),cache/'webview')

    def test_installed_runtime_data_can_follow_configured_download_drive(self):
        with tempfile.TemporaryDirectory() as folder:
            exe=Path(folder)/'assistant.exe'
            root=Path(folder)/'persistent'
            exe.with_name('runtime-paths.json').write_text(json.dumps({'data_dir':str(root)}),encoding='utf-8')
            with patch.object(sys,'frozen',True,create=True), patch.object(sys,'executable',str(exe)):
                with patch.dict(os.environ,{'DESKRAWL_ASSISTANT_DATA_DIR':''}):
                    self.assertEqual(paths.data_root(),root)
                    self.assertEqual(paths.runtime_dir(),root/'data/runtime')

    def test_frozen_missing_localappdata_uses_windows_user_directory(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(Path, 'home', return_value=Path('C:/Users/Example')):
            with patch.dict(os.environ, {'LOCALAPPDATA': '', 'DESKRAWL_ASSISTANT_DATA_DIR': ''}):
                self.assertEqual(paths.data_root(), Path('C:/Users/Example/AppData/Local/Deskrawl装备助手'))


if __name__ == '__main__':
    unittest.main()
