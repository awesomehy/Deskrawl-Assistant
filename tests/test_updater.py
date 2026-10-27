"""Update failures must leave the current exe and user configuration intact."""
import copy
import errno
import hashlib
import io
import json
import socket
import ssl
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

from deskrawl_assistant.updater import (API_ROOT, CHECK_INTERVAL, CHECK_RETRY_DELAYS, REPOSITORY_URL, UpdateManager,
    download_release, github_ssl_context, independent_process_environment, launch_helper, open_github,
    parse_release, trusted_url, validate_executable, version_tuple)


BINARY=b'MZ'+b'test payload'*100


class RestartEnvironmentTests(unittest.TestCase):
    def test_restart_drops_old_extraction_context_without_mutating_parent(self):
        parent={'_PYI_APPLICATION_HOME_DIR':'deleted old temp','_PYI_ARCHIVE_FILE':'same.exe',
            '_PYI_PARENT_PROCESS_LEVEL':'1','_MEIPASS2':'legacy temp',
            'PYINSTALLER_RESET_ENVIRONMENT':'0','DESKRAWL_ASSISTANT_DATA_DIR':'personal settings',
            'PATH':'system path','PYINSTALLER_STRICT_UNPACK_MODE':'1'}
        before=parent.copy();clean=independent_process_environment(parent)
        self.assertEqual(parent,before)
        self.assertEqual(clean,{'DESKRAWL_ASSISTANT_DATA_DIR':'personal settings','PATH':'system path',
            'PYINSTALLER_STRICT_UNPACK_MODE':'1','PYINSTALLER_RESET_ENVIRONMENT':'1'})

    def test_windows_environment_case_and_future_private_fields_are_handled(self):
        clean=independent_process_environment({'_pyi_future_field':'old','_MeIpAsS2':'old',
            'pyinstaller_reset_environment':'0','Temp':'user temp'})
        self.assertEqual(clean,{'Temp':'user temp','PYINSTALLER_RESET_ENVIRONMENT':'1'})

    def test_clean_launch_still_preserves_user_directory_and_requests_new_unpack(self):
        self.assertEqual(independent_process_environment({'LOCALAPPDATA':'user directory'}),
            {'LOCALAPPDATA':'user directory','PYINSTALLER_RESET_ENVIRONMENT':'1'})

    def test_update_helper_is_spawned_without_the_frozen_parents_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);signal=folder/'ready.json';signal.write_text('{}')
            descriptor=folder/'plan.json'
            descriptor.write_text(json.dumps({'helper_ready':str(signal)}),encoding='utf-8')
            parent={'SystemRoot':r'C:\Windows','_PYI_APPLICATION_HOME_DIR':'old extraction',
                '_PYI_ARCHIVE_FILE':'same.exe','_PYI_PARENT_PROCESS_LEVEL':'1',
                'DESKRAWL_ASSISTANT_DATA_DIR':'retained settings'}
            with patch.dict('deskrawl_assistant.updater.os.environ',parent,clear=True), \
                    patch('deskrawl_assistant.updater.subprocess.STARTUPINFO',create=True), \
                    patch('deskrawl_assistant.updater.subprocess.STARTF_USESHOWWINDOW',1,create=True), \
                    patch('deskrawl_assistant.updater.subprocess.CREATE_NO_WINDOW',0,create=True), \
                    patch('deskrawl_assistant.updater.subprocess.Popen') as spawn:
                self.assertIs(launch_helper((folder/'replace.ps1',descriptor)),spawn.return_value)
            child=spawn.call_args.kwargs['env']
            self.assertFalse(any(key.startswith('_PYI_') for key in child))
            self.assertEqual(child['PYINSTALLER_RESET_ENVIRONMENT'],'1')
            self.assertEqual(child['DESKRAWL_ASSISTANT_DATA_DIR'],'retained settings')
            self.assertFalse(signal.exists())


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


class UpdateTLSTests(unittest.TestCase):
    def test_windows_uses_private_system_context_with_required_verification(self):
        with patch('deskrawl_assistant.updater.sys.platform','win32'), \
                patch('truststore.SSLContext') as factory:
            context=github_ssl_context()
        factory.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        self.assertIs(context,factory.return_value)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertIs(context.check_hostname,True)
        self.assertEqual(context.minimum_version,ssl.TLSVersion.TLSv1_2)
        context.set_alpn_protocols.assert_called_once_with(['http/1.1'])

    def test_each_request_gets_its_own_context_without_global_ssl_changes(self):
        original_class=ssl.SSLContext
        original_default=ssl._create_default_https_context
        first=github_ssl_context();second=github_ssl_context()
        self.assertIsNot(first,second)
        for context in (first,second):
            self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
        self.assertIs(ssl.SSLContext,original_class)
        self.assertIs(ssl._create_default_https_context,original_default)

    def test_missing_windows_dependency_fails_clearly_without_insecure_fallback(self):
        with patch('deskrawl_assistant.updater.sys.platform','win32'), \
                patch.dict(sys.modules,{'truststore':None}), \
                patch('deskrawl_assistant.updater.ssl.create_default_context') as default:
            with self.assertRaisesRegex(RuntimeError,'系统证书组件缺失'):
                github_ssl_context()
        default.assert_not_called()

    def test_non_windows_keeps_verified_standard_library_context(self):
        with patch('deskrawl_assistant.updater.sys.platform','linux'), \
                patch.dict(sys.modules,{'truststore':None}), \
                patch('deskrawl_assistant.updater.ssl.create_default_context') as default:
            context=github_ssl_context()
        self.assertIs(context,default.return_value)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_opener_uses_verified_context_for_metadata_and_asset_downloads(self):
        for url,accept in ((API_ROOT+'/releases/latest','application/vnd.github+json'),
                (API_ROOT+'/releases/assets/42','application/octet-stream')):
            with self.subTest(url=url), \
                    patch('deskrawl_assistant.updater.github_ssl_context') as context, \
                    patch('deskrawl_assistant.updater.urllib.request.HTTPSHandler') as https, \
                    patch('deskrawl_assistant.updater.urllib.request.build_opener') as builder:
                self.assertIs(open_github(url,accept),builder.return_value.open.return_value)
            https.assert_called_once_with(context=context.return_value)
            self.assertIs(builder.call_args.args[1],https.return_value)
            request=builder.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,url)
            self.assertEqual(request.get_header('Accept'),accept)
            self.assertEqual(builder.return_value.open.call_args.kwargs,{'timeout':25})

    def test_invalid_certificate_is_not_retried_with_verification_disabled(self):
        error=urllib.error.URLError(ssl.SSLCertVerificationError('untrusted'))
        with patch('deskrawl_assistant.updater.github_ssl_context'), \
                patch('deskrawl_assistant.updater.urllib.request.build_opener') as builder:
            builder.return_value.open.side_effect=error
            with self.assertRaises(urllib.error.URLError) as result:
                open_github(API_ROOT+'/releases/latest','application/json')
        self.assertIs(result.exception,error)
        builder.assert_called_once()
        builder.return_value.open.assert_called_once()

    def test_untrusted_destination_is_rejected_before_context_or_network(self):
        with patch('deskrawl_assistant.updater.github_ssl_context') as context, \
                patch('deskrawl_assistant.updater.urllib.request.build_opener') as builder:
            with self.assertRaises(ValueError):
                open_github('https://untrusted.invalid/update','application/json')
        context.assert_not_called();builder.assert_not_called()


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
        recorder = patch('deskrawl_assistant.updater.record')
        self.record = recorder.start()
        self.addCleanup(recorder.stop)
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

    def test_check_failure_does_not_blame_directory_or_write_to_it(self):
        self.manager.fetcher=Mock(side_effect=urllib.error.URLError(ConnectionResetError(10054,'reset')))
        with patch.object(Path,'mkdir') as mkdir:
            self.manager._check()
        state=self.manager.snapshot()
        self.assertIn('网络',state['error']);self.assertNotIn('目录',state['error'])
        mkdir.assert_not_called();self.prepared.assert_not_called();self.launched.assert_not_called()

    def test_failure_log_keeps_codes_without_proxy_credentials_urls_or_paths(self):
        reason=ConnectionRefusedError(10061,'http://name:private-password@proxy.invalid C:/private/file')
        self.manager.fetcher=Mock(side_effect=urllib.error.URLError(reason))
        self.manager._check()
        name=self.record.call_args.args[0];details=self.record.call_args.kwargs
        self.assertEqual(name,'update_check_failed')
        self.assertEqual(details['error_type'],'URLError')
        self.assertEqual(details['reason_type'],'ConnectionRefusedError')
        self.assertEqual(details['errno'],10061)
        encoded=json.dumps(details)
        for secret in ('private-password','proxy.invalid','C:/private/file'):
            self.assertNotIn(secret,encoded)
            self.assertNotIn(secret,self.manager.error)

    def test_failed_checks_retry_with_backoff_then_return_to_hourly_poll(self):
        self.manager.fetcher=Mock(side_effect=urllib.error.URLError(TimeoutError('timeout')))
        for delay in (*CHECK_RETRY_DELAYS,CHECK_INTERVAL,CHECK_INTERVAL):
            self.manager._check()
            self.assertEqual(self.manager.check_delay,delay)
        self.manager.fetcher=lambda:parse_release(payload())
        self.manager._check()
        self.assertEqual(self.manager.check_delay,CHECK_INTERVAL)
        self.assertEqual(self.manager.check_failures,0)
        self.assertEqual(self.manager.error,'')
        self.assertEqual(self.manager.phase,'available')

    def test_automatic_poll_uses_failed_result_and_recovers_without_installing(self):
        self.manager.closed=Mock()
        self.manager.closed.is_set.return_value=False
        self.manager.closed.wait.side_effect=[False,False,True]
        self.manager.fetcher=Mock(side_effect=[urllib.error.URLError(TimeoutError('timeout')),parse_release(payload())])
        with patch.object(self.manager,'check',side_effect=self.manager._check), \
                patch('deskrawl_assistant.updater.threading.Thread') as thread:
            self.manager.start()
            thread.call_args.kwargs['target']()
        self.assertEqual([call.args[0] for call in self.manager.closed.wait.call_args_list],
            [3,CHECK_RETRY_DELAYS[0],CHECK_INTERVAL])
        self.assertEqual(self.manager.fetcher.call_count,2)
        self.assertEqual(self.manager.phase,'available')
        self.launched.assert_not_called();self.prepared.assert_not_called()

    def test_poll_waits_for_check_result_before_choosing_retry_delay(self):
        self.manager.closed=Mock()
        self.manager.closed.is_set.return_value=False
        self.manager.closed.wait.side_effect=[False,True]
        self.manager.worker=Mock()
        self.manager.phase='checking'
        self.manager.worker.join.side_effect=lambda *_:setattr(self.manager,'check_delay',CHECK_RETRY_DELAYS[0])
        with patch.object(self.manager,'check'), patch('deskrawl_assistant.updater.threading.Thread') as thread:
            self.manager.start()
            thread.call_args.kwargs['target']()
        self.manager.worker.join.assert_called_once_with()
        self.assertEqual(self.manager.closed.wait.call_args.args,(CHECK_RETRY_DELAYS[0],))

    def test_close_during_failed_request_does_not_reschedule_or_change_status(self):
        def closed_fetch():
            self.manager.closed.set()
            raise urllib.error.URLError('offline')
        self.manager.fetcher=closed_fetch
        self.manager._check()
        self.assertEqual(self.manager.check_failures,0)
        self.record.assert_not_called()

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


class UpdateErrorTests(unittest.TestCase):
    def test_network_errors_are_identified_for_check_and_download(self):
        cases=[(socket.gaierror(-2,'DNS'),'DNS'),(TimeoutError('timed out'),'超时'),
            (ssl.SSLCertVerificationError('untrusted'),'证书'),(ssl.SSLError('handshake'),'HTTPS'),
            (ConnectionRefusedError(10061,'refused'),'网络')]
        for reason,expected in cases:
            for operation in ('check','install'):
                for error in (reason,urllib.error.URLError(reason)):
                    with self.subTest(reason=type(reason).__name__,operation=operation,wrapped=error is not reason):
                        message=UpdateManager._message(error,operation=operation)
                        self.assertIn(expected,message)
                        self.assertNotIn('目录',message)

    def test_filesystem_errors_only_claim_directory_permission_during_install(self):
        denied=PermissionError(errno.EACCES,'denied','private path')
        self.assertIn('网络请求被系统拒绝',UpdateManager._message(denied,operation='check'))
        self.assertIn('目录权限',UpdateManager._message(denied,operation='install'))
        missing=FileNotFoundError(errno.ENOENT,'missing','private path')
        self.assertIn('运行文件',UpdateManager._message(missing,operation='check'))
        self.assertIn('文件读写',UpdateManager._message(missing,operation='install'))
        disk=OSError(errno.ENOSPC,'full','private path')
        self.assertIn('可用空间',UpdateManager._message(disk,operation='install'))

    def test_http_permission_rejection_is_not_misreported_as_rate_limit(self):
        forbidden=urllib.error.HTTPError(API_ROOT,403,'forbidden',{},None)
        self.assertIn('HTTP 403',UpdateManager._message(forbidden))
        limited=urllib.error.HTTPError(API_ROOT,403,'limited',{'X-RateLimit-Remaining':'0'},None)
        self.assertIn('次数暂时受限',UpdateManager._message(limited))
        self.assertIn('次数暂时受限',UpdateManager._message(
            urllib.error.HTTPError(API_ROOT,429,'limited',{},None)))

    def test_ssl_failure_log_reports_verification_code_without_certificate_text(self):
        error=ssl.SSLCertVerificationError('private certificate detail')
        error.verify_code=20
        details=UpdateManager._failure_details(urllib.error.URLError(error))
        self.assertEqual(details['verify_code'],20)
        self.assertNotIn('private',json.dumps(details))


if __name__ == '__main__': unittest.main()
