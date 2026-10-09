"""Update failures must leave the current exe and user configuration intact."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

from deskrawl_assistant.updater import (API_ROOT, REPOSITORY_URL, UpdateManager,
    download_release, parse_release, trusted_url, validate_executable, version_tuple)


BINARY=b'MZ'+b'test payload'*100
def payload(version='1.1.6',binary=BINARY):
    tag='v'+version;name='Deskrawl-Assistant-'+tag+'.exe'
    return {'tag_name':tag,'draft':False,'prerelease':False,
        'html_url':REPOSITORY_URL+'/releases/tag/'+tag,'assets':[{'name':name,'id':42,'size':len(binary),
            'digest':'sha256:'+hashlib.sha256(binary).hexdigest(),'state':'uploaded',
            'browser_download_url':REPOSITORY_URL+'/releases/download/'+tag+'/'+name}]}

class Response(io.BytesIO):
    def geturl(self):return 'https://release-assets.githubusercontent.com/test.exe'


class ReleaseTests(unittest.TestCase):
    def test_versions_use_numeric_order_and_reject_prereleases(self):
        self.assertGreater(version_tuple('v1.1.10'),version_tuple('1.1.9'))
        for value in ('1.1','1.1.6-beta','v01.1.6','../1.1.6',None):
            with self.assertRaises(ValueError):version_tuple(value)

    def test_public_asset_selects_exact_exe_and_hash(self):
        release=parse_release(payload())
        self.assertEqual(release.asset_url,API_ROOT+'/releases/assets/42')
        self.assertEqual(release.sha256,hashlib.sha256(BINARY).hexdigest())

    def test_draft_prerelease_missing_or_ambiguous_asset_is_rejected(self):
        cases=[]
        for field in ('draft','prerelease'):
            data=payload();data[field]=True;cases.append(data)
        data=payload();data['assets']=[];cases.append(data)
        data=payload();data['assets'].append(copy.deepcopy(data['assets'][0]));cases.append(data)
        for data in cases:
            with self.assertRaises(ValueError):parse_release(data)

    def test_wrong_source_digest_size_and_unfinished_upload_are_rejected(self):
        for field,value in (('digest',None),('size',True),('size',300*1024*1024),
                           ('state','new'),('browser_download_url','https://other.test/evil.exe')):
            data=payload();data['assets'][0][field]=value
            with self.assertRaises(ValueError):parse_release(data)
        data=payload();data['html_url']='https://github.com/other/repo/releases/tag/v1.1.6'
        with self.assertRaises(ValueError):parse_release(data)

    def test_redirect_hosts_require_https_without_credentials_or_custom_port(self):
        for url in ('http://github.com/a','https://github.com.evil.test/a','https://user@github.com/a',
                    'https://github.com:8080/a','file:///tmp/evil.exe'):
            with self.assertRaises(ValueError):trusted_url(url)
        self.assertEqual(trusted_url('https://release-assets.githubusercontent.com/a'),
                         'https://release-assets.githubusercontent.com/a')


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);self.stage=self.folder/'stage.exe'
        self.old=self.folder/'Deskrawl.exe';self.old.write_bytes(b'old executable')
        self.rule=self.folder/'rules.json';self.rule.write_bytes(b'personal rule')
        self.release=parse_release(payload());self.cancel=threading.Event()
        self.opener=lambda *_:Response(BINARY)

    def unchanged(self):
        self.assertEqual(self.old.read_bytes(),b'old executable')
        self.assertEqual(self.rule.read_bytes(),b'personal rule')

    def test_verified_download_never_overwrites_the_running_file(self):
        progress=Mock();validate=Mock()
        download_release(self.release,self.stage,self.cancel,progress,opener=self.opener,validate=validate)
        self.assertEqual(self.stage.read_bytes(),BINARY);self.unchanged()
        validate.assert_called_once_with(self.stage,'1.1.6')
        progress.assert_called_with(len(BINARY),len(BINARY))

    def test_interrupted_truncated_and_corrupt_downloads_remove_only_the_stage(self):
        for binary in (BINARY[:-1],b'Z'+BINARY[1:],BINARY+b'extra'):
            with self.assertRaises(ValueError):
                download_release(self.release,self.stage,self.cancel,Mock(),
                    opener=lambda *_:Response(binary),validate=Mock())
            self.assertFalse(self.stage.exists());self.unchanged()
        self.cancel.set()
        with self.assertRaisesRegex(ValueError,'取消'):
            download_release(self.release,self.stage,self.cancel,Mock(),opener=self.opener,validate=Mock())
        self.assertFalse(self.stage.exists());self.unchanged()

    def test_existing_stage_and_network_failure_do_not_remove_unowned_files(self):
        self.stage.write_bytes(b'preexisting file')
        with self.assertRaises(FileExistsError):
            download_release(self.release,self.stage,self.cancel,Mock(),opener=self.opener,validate=Mock())
        self.assertEqual(self.stage.read_bytes(),b'preexisting file');self.unchanged()
        with self.assertRaises(OSError):
            download_release(self.release,self.stage,self.cancel,Mock(),opener=Mock(side_effect=OSError('offline')))
        self.assertEqual(self.stage.read_bytes(),b'preexisting file');self.unchanged()

    def test_downloaded_binary_or_version_failure_keeps_old_exe(self):
        with self.assertRaisesRegex(ValueError,'wrong version'):
            download_release(self.release,self.stage,self.cancel,Mock(),opener=self.opener,
                validate=Mock(side_effect=ValueError('wrong version')))
        self.assertFalse(self.stage.exists());self.unchanged()

    def test_windows_program_header_and_version_are_both_verified(self):
        binary=bytearray(134);binary[:2]=b'MZ';binary[60:64]=(128).to_bytes(4,'little');binary[128:134]=b'PE\0\0\x64\x86'
        self.stage.write_bytes(binary)
        with patch('deskrawl_assistant.updater.executable_version',return_value=(1,1,6,0)):
            validate_executable(self.stage,'1.1.6')
        with patch('deskrawl_assistant.updater.executable_version',return_value=(1,1,5,0)):
            with self.assertRaisesRegex(ValueError,'版本'):validate_executable(self.stage,'1.1.6')
        binary[132:134]=b'\x4c\x01';self.stage.write_bytes(binary)
        with self.assertRaisesRegex(ValueError,'x64'):validate_executable(self.stage,'1.1.6')


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);self.exe=self.folder/'程序有 空格.exe';self.exe.write_bytes(b'old')
        self.prepared=Mock();self.launched=Mock();self.aborted=Mock()
        def download(release,stage,cancelled,progress):stage.write_bytes(BINARY);progress(len(BINARY),len(BINARY))
        self.manager=UpdateManager(executable=self.exe,current_version='1.1.5',directory=self.folder/'updates',
            fetcher=lambda:parse_release(payload()),downloader=download,
            prepare=self.prepared,launch=self.launched,abort=self.aborted,port=23456)
        self.addCleanup(self.manager.close)

    def test_check_never_installs_and_downgrade_is_not_available(self):
        self.manager._check();self.assertTrue(self.manager.snapshot()['has_update'])
        self.launched.assert_not_called();self.assertEqual(self.exe.read_bytes(),b'old')
        self.manager.fetcher=lambda:parse_release(payload('1.1.4'))
        self.manager._check();self.assertFalse(self.manager.snapshot()['has_update'])
        with self.assertRaises(ValueError):self.manager.install()

    def test_install_creates_a_guarded_plan_in_current_program_directory(self):
        self.manager._check();self.manager.install();self.manager.worker.join(3)
        self.assertEqual(self.manager.phase,'installing');self.assertEqual(self.exe.read_bytes(),b'old')
        self.prepared.assert_called_once()
        script,descriptor=self.launched.call_args.args[0]
        plan=json.loads(descriptor.read_text(encoding='utf-8-sig'))
        self.assertEqual(Path(plan['target']),self.exe.resolve())
        self.assertEqual(Path(plan['stage']).parent,self.exe.parent)
        self.assertEqual(Path(plan['backup']).parent,self.exe.parent)
        self.assertEqual(plan['port'],23456);self.assertTrue(script.is_file())

    def test_new_unsaved_draft_or_busy_write_aborts_before_helper_launch(self):
        self.manager._check();self.prepared.side_effect=ValueError('请先保存')
        self.manager.install();self.manager.worker.join(3)
        self.assertEqual(self.manager.phase,'error');self.launched.assert_not_called()
        self.aborted.assert_called_once();self.assertEqual(self.exe.read_bytes(),b'old')
        self.assertFalse(list(self.folder.glob('.deskrawl-update-*.exe')))

    def test_helper_failure_keeps_old_program_and_unblocks_service(self):
        self.manager._check();self.launched.side_effect=ValueError('helper failed')
        self.manager.install();self.manager.worker.join(3)
        self.assertEqual(self.manager.phase,'error');self.aborted.assert_called_once()
        self.assertEqual(self.exe.read_bytes(),b'old')

    def test_offline_check_preserves_known_update_without_blocking_equipment(self):
        self.manager._check();self.manager.fetcher=Mock(side_effect=OSError('offline'))
        self.manager._check();state=self.manager.snapshot()
        self.assertEqual(state['phase'],'error');self.assertTrue(state['has_update'])
        self.assertIn('仍可使用',state['message']);self.launched.assert_not_called()

    def test_repeat_check_and_install_share_single_worker(self):
        self.manager.check();self.manager.worker.join(3)
        worker=self.manager.worker;self.manager.check();self.assertIs(self.manager.worker,worker)
        self.manager.phase='downloading'
        with self.assertRaises(ValueError):self.manager.install()

    def test_first_check_is_not_skipped_just_after_windows_boot(self):
        with patch('deskrawl_assistant.updater.time.monotonic',return_value=3):
            self.manager.check()
            self.assertIsNotNone(self.manager.worker)
            self.manager.worker.join(3)
            self.assertTrue(self.manager.snapshot()['has_update'])
            worker=self.manager.worker
            self.manager.check()
            self.assertIs(self.manager.worker,worker)


if __name__ == '__main__': unittest.main()
