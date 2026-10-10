"""Read public releases; install only a verified Windows asset on user request."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .paths import RESOURCE_ROOT, runtime_dir, data_root
from .version import VERSION, GITHUB_REPOSITORY, REPOSITORY_URL

API_ROOT = 'https://api.github.com/repos/' + GITHUB_REPOSITORY
MAX_EXE_BYTES = 256 * 1024 * 1024
CHECK_INTERVAL = 3600
ALLOWED_HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                 'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('发行版本号无效，只接受正式版本。')
    return tuple(map(int, value.lstrip('v').split('.')))


def trusted_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or
            parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('更新下载地址不属于 GitHub。')
    return url


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def open_github(url, accept):
    trusted_url(url)
    request = urllib.request.Request(url, headers={'Accept':accept,
        'User-Agent':'Deskrawl-Assistant/' + VERSION, 'X-GitHub-Api-Version':'2022-11-28'})
    # Respect the user's network proxy. No GitHub credentials are needed or read.
    return urllib.request.build_opener(GitHubRedirect()).open(request, timeout=25)


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    asset_id: int
    size: int
    sha256: str
    release_url: str
    asset_url: str


def parse_release(payload):
    if not isinstance(payload, dict) or payload.get('draft') is not False or payload.get('prerelease') is not False:
        raise ValueError('只更新已公开发布的正式版本。')
    tag = payload.get('tag_name')
    version_tuple(tag)
    if not tag.startswith('v'):
        raise ValueError('发行标签无效。')
    version = tag[1:]
    release_url = REPOSITORY_URL + '/releases/tag/' + tag
    if payload.get('html_url') != release_url:
        raise ValueError('发行版来源不属于本项目。')
    asset_name = 'Deskrawl-Assistant-' + tag + '.exe'
    assets = [a for a in payload.get('assets', []) if isinstance(a, dict) and a.get('name') == asset_name]
    if len(assets) != 1:
        raise ValueError('该发行版没有唯一的 Windows exe，请在 Release 页面查看。')
    asset = assets[0]
    asset_id, size, digest = asset.get('id'), asset.get('size'), asset.get('digest')
    if (type(asset_id) is not int or asset_id <= 0 or type(size) is not int or
            not 64 <= size <= MAX_EXE_BYTES or asset.get('state') != 'uploaded'):
        raise ValueError('发行程序的大小或上传状态无效。')
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('发行程序没有 SHA256 校验值，无法自动替换。')
    if asset.get('browser_download_url') != REPOSITORY_URL + '/releases/download/' + tag + '/' + asset_name:
        raise ValueError('发行程序下载来源不属于本项目。')
    return Release(version, tag, asset_id, size, digest[7:], release_url,
                   API_ROOT + '/releases/assets/' + str(asset_id))


def fetch_latest():
    with open_github(API_ROOT + '/releases/latest', 'application/vnd.github+json') as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('更新信息过大。')
    return parse_release(json.loads(raw))


def executable_version(path):
    dll = ctypes.WinDLL('version', use_last_error=True)
    dll.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    dll.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
    dll.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    dll.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
    dummy = ctypes.c_uint32()
    size = dll.GetFileVersionInfoSizeW(str(path), ctypes.byref(dummy))
    if not size:
        raise ValueError('下载程序没有可核验的版本信息。')
    buffer = ctypes.create_string_buffer(size)
    if not dll.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ValueError('无法读取下载程序的版本信息。')
    pointer, length = ctypes.c_void_p(), ctypes.c_uint32()
    if not dll.VerQueryValueW(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        raise ValueError('下载程序的版本信息不完整。')
    values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
    if values[0] != 0xFEEF04BD:
        raise ValueError('下载程序的版本签名无效。')
    # ProductVersion in VS_FIXEDFILEINFO.
    return (values[4] >> 16, values[4] & 65535, values[5] >> 16, values[5] & 65535)


def validate_executable(path, expected_version):
    with Path(path).open('rb') as source:
        header = source.read(64)
        if len(header) < 64 or header[:2] != b'MZ':
            raise ValueError('下载结果不是 Windows 程序。')
        offset = struct.unpack_from('<I', header, 60)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise ValueError('下载程序的头部无效。')
        source.seek(offset)
        pe = source.read(6)
        if pe != b'PE\0\0\x64\x86':
            raise ValueError('下载程序不是 Windows x64 程序。')
    if executable_version(path) != (*version_tuple(expected_version), 0):
        raise ValueError('下载程序的版本与发行标签不一致。')


def download_release(release, stage, cancelled, progress, *, opener=open_github, validate=validate_executable):
    stage = Path(stage)
    digest, count = hashlib.sha256(), 0
    created = False
    try:
        with opener(release.asset_url, 'application/octet-stream') as response, stage.open('xb') as target:
            created = True
            trusted_url(response.geturl())
            while True:
                if cancelled.is_set():
                    raise ValueError('已取消更新下载，旧程序保留。')
                block = response.read(256 * 1024)
                if not block:
                    break
                count += len(block)
                if count > release.size or count > MAX_EXE_BYTES:
                    raise ValueError('下载大小超出发行程序大小。')
                target.write(block)
                digest.update(block)
                progress(count, release.size)
            target.flush()
            os.fsync(target.fileno())
        if count != release.size or digest.hexdigest() != release.sha256:
            raise ValueError('下载文件校验失败，旧程序保留，请重试。')
        validate(stage, release.version)
        return stage
    except Exception:
        # This function only removes the uniquely named stage that it created.
        if created and stage.exists():
            stage.unlink()
        raise


class UpdateManager:
    def __init__(self, *, executable=None, current_version=VERSION, directory=None,
                 fetcher=fetch_latest, downloader=download_release, prepare=None, launch=None, abort=None, port=18741):
        self.current_version = current_version
        self.executable = (Path(executable).resolve() if executable is not None else
            Path(sys.executable).resolve() if getattr(sys, 'frozen', False) else None)
        self.directory = Path(directory) if directory is not None else runtime_dir() / 'updates'
        self.fetcher, self.downloader = fetcher, downloader
        self.prepare, self.launch, self.port = prepare, launch, port
        self.abort = abort
        self.guard = threading.RLock()
        self.closed = threading.Event()
        self.worker = None
        self.poll_thread = None
        self.release = None
        self.last_check = None
        self.phase = 'idle'
        self.error = ''
        self.checked_at = None
        self.progress = None
        self.message = '启动后自动检查更新，每小时检查一次。'
        self.notice = None
        try:
            previous = json.loads((self.directory / 'last-result.json').read_text(encoding='utf-8-sig'))
            if self.executable and Path(previous['target']).resolve() == self.executable:
                self.notice = {k:previous.get(k) for k in ('success','version','message','time')}
        except (OSError,ValueError,KeyError,TypeError):
            pass

    def snapshot(self):
        with self.guard:
            release = self.release
            available = bool(release and version_tuple(release.version) > version_tuple(self.current_version))
            return dict(phase=self.phase,current_version=self.current_version,
                latest_version=release.version if release else None, has_update=available,
                can_install=bool(self.executable and self.prepare and self.launch and available),
                release_url=release.release_url if release else REPOSITORY_URL + '/releases',
                message=self.message,error=self.error,checked_at=self.checked_at,progress=self.progress,
                install_directory=str(self.executable.parent) if self.executable else None,notice=self.notice)

    def start(self):
        def poll():
            if self.closed.wait(3):
                return
            while not self.closed.is_set():
                self.check()
                if self.closed.wait(CHECK_INTERVAL):
                    return
        self.poll_thread = threading.Thread(target=poll, daemon=True)
        self.poll_thread.start()

    def check(self):
        with self.guard:
            if self.closed.is_set() or self.phase in ('checking','downloading','installing'):
                return
            # Manual clicks share the one-hour automatic poll and a 60s cooldown.
            if self.last_check is not None and time.monotonic() - self.last_check < 60:
                return
            self.last_check = time.monotonic()
            self.phase, self.error = 'checking', ''
            self.message = '正在检查 GitHub 更新…'
            self.worker = threading.Thread(target=self._check, daemon=True)
            self.worker.start()

    def _check(self):
        try:
            release = self.fetcher()
            with self.guard:
                if self.closed.is_set():
                    return
                self.release = release
                newer = version_tuple(release.version) > version_tuple(self.current_version)
                self.phase = 'available' if newer else 'up_to_date'
                self.checked_at = datetime.now(timezone.utc).isoformat()
                self.message = '发现新版本 v' + release.version if newer else '当前 v' + self.current_version + '，暂无新版本。'
        except Exception as exc:
            with self.guard:
                self.phase, self.error = 'error', self._message(exc)
                self.message = '检查更新失败，可稍后重试；装备功能仍可使用。'

    @staticmethod
    def _message(exc):
        if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429):
            return 'GitHub 检查次数暂时受限，请稍后重试。'
        if isinstance(exc, (OSError, urllib.error.URLError)):
            return '无法连接 GitHub 或写入程序目录，请检查网络和目录权限。'
        return str(exc)[:300]

    def install(self):
        with self.guard:
            if self.phase in ('checking','downloading','installing'):
                raise ValueError('更新操作正在进行，请稍候。')
            if not self.snapshot()['can_install']:
                raise ValueError('当前没有可自动安装的新版本；源码模式请从 Release 下载 exe。')
            if self.closed.is_set():
                raise ValueError('助手已退出。')
            self.phase, self.error, self.progress = 'downloading', '', {'received':0,'total':self.release.size,'percent':0}
            self.message = '正在下载 v' + self.release.version + '，完成后将替换当前程序并重启。'
            self.worker = threading.Thread(target=self._install, args=(self.release,), daemon=True)
            self.worker.start()

    def _progress(self, count, total):
        with self.guard:
            self.progress = {'received':count,'total':total,'percent':int(count * 100 / total)}

    def _install(self, release):
        stage = None
        owned = False
        try:
            target = self.executable
            if target.suffix.lower() != '.exe' or not target.is_file():
                raise ValueError('无法确认当前程序位置，旧程序保留。')
            nonce = uuid.uuid4().hex
            stage = target.parent / ('.deskrawl-update-' + nonce + '.exe')
            self.downloader(release, stage, self.closed, self._progress)
            owned = True
            if self.closed.is_set():
                raise ValueError('助手已退出，取消替换。')
            # Re-check unsaved drafts, then stop and wait for the game's current write.
            self.prepare()
            if self.closed.is_set():
                raise ValueError('助手已退出，取消替换。')
            plan = self._plan(release, stage, nonce)
            with self.guard:
                self.phase, self.message = 'installing', '下载已校验，正在退出并替换程序…'
            self.launch(plan)
            stage = None  # The launched helper now owns this verified file.
        except Exception as exc:
            if self.abort:
                self.abort()
            with self.guard:
                self.phase, self.error, self.progress = 'error', self._message(exc), None
                self.message = '更新未完成，旧程序和个人配置保留。'
        finally:
            if owned and stage is not None and stage.exists():
                stage.unlink()

    def _plan(self, release, stage, nonce):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.executable.resolve()
        stage = stage.resolve()
        if stage.parent != target.parent:
            raise ValueError('更新文件必须位于当前程序目录。')
        plan = {'id':nonce,'target':str(target),'stage':str(stage),
            'data_root':str(data_root().resolve()),
            'backup':str(target.parent / ('.deskrawl-backup-' + nonce + '.exe')),
            'sha256':release.sha256,'size':release.size,'version':release.version,
            'parent_pid':os.getpid(),'port':self.port,
            'receipt':str((self.directory / ('ready-' + nonce + '.json')).resolve()),
            'helper_ready':str((self.directory / ('helper-' + nonce + '.json')).resolve()),
            'result':str((self.directory / 'last-result.json').resolve())}
        script = self.directory / ('replace-' + nonce + '.ps1')
        script.write_text((RESOURCE_ROOT / 'deskrawl_assistant/update_replace.ps1').read_text(encoding='utf-8-sig'), encoding='utf-8-sig')
        descriptor = self.directory / ('plan-' + nonce + '.json')
        descriptor.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8-sig')
        return script.resolve(), descriptor.resolve()

    def close(self):
        self.closed.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(1)


def independent_process_environment(environment=None):
    """Restart independently of an exiting one-file application's extraction."""
    source = os.environ if environment is None else environment
    clean = {key:value for key,value in source.items()
        if not key.upper().startswith('_PYI_') and key.upper() not in
        {'_MEIPASS2','PYINSTALLER_RESET_ENVIRONMENT'}}
    clean['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return clean


def launch_helper(plan):
    script, descriptor = plan
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen([str(shell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-WindowStyle', 'Hidden', '-File', str(script), '-Plan', str(descriptor)],
        startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
        env=independent_process_environment())
    payload = json.loads(descriptor.read_text(encoding='utf-8-sig'))
    signal = Path(payload['helper_ready'])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if signal.is_file():
            signal.unlink()
            return process
        if process.poll() is not None:
            break
        time.sleep(.1)
    raise ValueError('替换助手未能启动，旧程序保持运行。')


def application_ready():
    """The replacement helper waits until the new native window is loaded."""
    nonce = os.environ.get('DESKRAWL_ASSISTANT_UPDATE_ID', '')
    if not re.fullmatch(r'[0-9a-f]{32}', nonce):
        return
    target = runtime_dir() / 'updates' / ('ready-' + nonce + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps({'id':nonce,'version':VERSION,'ready':True}), encoding='utf-8')
    temp.replace(target)
