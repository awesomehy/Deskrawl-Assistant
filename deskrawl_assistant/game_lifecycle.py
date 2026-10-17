"""Steam launch and normal Windows close, independent of memory connection."""
from __future__ import annotations

import os
import threading
import time
from copy import deepcopy

from .action_log import record

STEAM_APP_ID = 4623570


class ProcessProbeError(RuntimeError):
    def __init__(self, stage, pid, cause):
        super().__init__(str(cause))
        self.stage, self.pid = stage, pid
        self.winerror = getattr(cause, 'winerror', None)


def probe_diagnostic(exc, *, stage='identity', pid=None):
    return {'stage': getattr(exc, 'stage', stage), 'pid': getattr(exc, 'pid', pid),
            'winerror': getattr(exc, 'winerror', None), 'reason': str(exc)}


def diagnostic_message(diagnostic):
    stages = {'enumerate_processes': '枚举进程', 'open_process': '打开进程',
              'image_name': '读取进程名称', 'creation_time': '读取进程启动时间',
              'exit_code': '检查进程存活状态',
              'identity': '核验进程身份', 'connected_process': '核验已连接进程'}
    stage = stages.get(diagnostic['stage'], diagnostic['stage'])
    pid = ' '+str(diagnostic['pid']) if diagnostic.get('pid') is not None else ''
    code = f"，Windows 错误 {diagnostic['winerror']}" if diagnostic.get('winerror') is not None else ''
    return f"{stage}{pid}未完成{code}：{diagnostic['reason']}"


class ProcessInstances(list):
    """Verified instances and independent failures from the same enumeration."""
    def __init__(self, instances, diagnostics):
        super().__init__(instances)
        self.diagnostics = diagnostics


class WindowsGameProcess:
    def instances(self):
        try:
            from .native_memory import game_pids
            pids = game_pids()
        except (OSError, ValueError, ImportError) as exc:
            raise ProcessProbeError('enumerate_processes', None, exc) from exc
        result, diagnostics = [], []
        for pid in pids:
            try:
                with self._identity(pid) as instance:
                    result.append(instance[1])
            except (ProcessProbeError, OSError, ValueError, ImportError) as exc:
                if getattr(exc,'winerror',None) in (6,87,1168):
                    continue  # Exited between enumeration and opening.
                # One inaccessible process must not hide another verified game.
                # Retain failures so none can authorize launch or normal close.
                diagnostics.append(probe_diagnostic(exc, pid=pid))
        return ProcessInstances(result, diagnostics)

    @staticmethod
    def _identity(pid):
        import ctypes as C
        from ctypes import wintypes as W
        from contextlib import contextmanager
        from pathlib import Path
        from .native_memory import K

        @contextmanager
        def opened():
            handle = K.OpenProcess(0x1000, False, pid)
            if not handle:
                raise ProcessProbeError('open_process', pid, C.WinError(C.get_last_error()))
            try:
                size = W.DWORD(32768)
                path = C.create_unicode_buffer(size.value)
                if not K.QueryFullProcessImageNameW(handle, 0, path, C.byref(size)):
                    raise ProcessProbeError('image_name', pid, C.WinError(C.get_last_error()))
                if Path(path.value).name.casefold() != 'deskrawl.exe':
                    raise ProcessProbeError('image_name', pid, ValueError('目标进程已变化，请刷新游戏状态。'))
                created, exited, kernel, user = (W.FILETIME() for _ in range(4))
                K.GetProcessTimes.argtypes = (W.HANDLE, *(C.POINTER(W.FILETIME),)*4)
                K.GetProcessTimes.restype = W.BOOL
                if not K.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
                    raise ProcessProbeError('creation_time', pid, C.WinError(C.get_last_error()))
                code = W.DWORD()
                if not K.GetExitCodeProcess(handle, C.byref(code)):
                    raise ProcessProbeError('exit_code', pid, C.WinError(C.get_last_error()))
                if code.value != 259:
                    # Toolhelp describes its earlier snapshot; an exited
                    # process can still have a readable image/creation time.
                    # Treat this as the same benign disappearance as OpenProcess
                    # ERROR_NOT_FOUND, rather than publishing a close target.
                    raise ProcessProbeError('exit_code', pid, C.WinError(1168))
                stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
                yield handle, {'pid':pid, 'instance':f'{pid}:{stamp}'}
            finally:
                K.CloseHandle(handle)
        return opened()

    def launch(self):
        os.startfile(f'steam://rungameid/{STEAM_APP_ID}')

    def close(self, expected):
        import ctypes as C
        from ctypes import wintypes as W
        from .native_memory import K
        pid = expected.get('pid')
        if type(pid) is not int or pid <= 0: raise ValueError('游戏进程未确认。')
        with self._identity(pid) as (handle, identity):
            if identity != expected: raise ValueError('游戏进程已变化，请重新确认关闭。')
            code = W.DWORD()
            if not K.GetExitCodeProcess(handle, C.byref(code)) or code.value != 259:
                return
            user = C.WinDLL('user32', use_last_error=True)
            callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
            user.EnumWindows.argtypes = (callback_type, W.LPARAM)
            user.GetWindowThreadProcessId.argtypes = (W.HWND, C.POINTER(W.DWORD))
            user.GetClassNameW.argtypes = (W.HWND, W.LPWSTR, C.c_int)
            user.IsWindowVisible.argtypes = (W.HWND,)
            user.PostMessageW.argtypes = (W.HWND, W.UINT, W.WPARAM, W.LPARAM)
            user.PostMessageW.restype = W.BOOL
            windows = []

            @callback_type
            def visit(hwnd, _):
                owner = W.DWORD()
                user.GetWindowThreadProcessId(hwnd, C.byref(owner))
                name = C.create_unicode_buffer(256)
                user.GetClassNameW(hwnd, name, len(name))
                if owner.value == pid and name.value == 'UnityWndClass' and user.IsWindowVisible(hwnd):
                    windows.append(hwnd)
                return True

            user.EnumWindows(visit, 0)
            if len(windows) != 1:
                raise ValueError('尚未找到唯一的游戏窗口，请等待游戏载入后重试。')
            # Hold the process handle through the message: Windows cannot reuse
            # this PID for a different process while we still own its handle.
            if not user.PostMessageW(windows[0], 0x10, 0, 0):  # WM_CLOSE, normal save/quit path
                raise C.WinError(C.get_last_error())


class GameLifecycle:
    def __init__(self, backend=None, clock=time.monotonic):
        self.backend = backend or WindowsGameProcess()
        self.clock = clock
        self.guard = threading.RLock()
        self.cached = None
        self.checked = -float('inf')
        self.checked_pid = None
        self.logged_state = None
        self.pending = None
        self.deadline = 0
        self.error = ''

    def state(self, *, fresh=False, known_running_pid=None):
        # Only the caller's live process handle may supply this evidence. It
        # confirms presence, never the fresh identity required by close().
        if type(known_running_pid) is not int or known_running_pid <= 0:
            known_running_pid = None
        with self.guard:
            now = self.clock()
            if (not fresh and self.cached is not None and now-self.checked < 1
                    and known_running_pid == self.checked_pid):
                return deepcopy(self.cached)
            try:
                probe = self.backend.instances()
                instances = list(probe)
                diagnostics = list(getattr(probe, 'diagnostics', []))
            except (ProcessProbeError, OSError, ValueError, ImportError) as exc:
                instances, diagnostics = [], [probe_diagnostic(exc)]
            if (known_running_pid is not None and not diagnostics
                    and not any(row['pid'] == known_running_pid for row in instances)):
                diagnostics.append({'stage': 'connected_process', 'pid': known_running_pid,
                    'winerror': None, 'reason': '现有连接确认游戏存活，进程身份暂不可重新核验。'})
            running = bool(instances) or known_running_pid is not None
            uncertain = bool(diagnostics)
            if self.pending == 'starting' and running or self.pending == 'closing' and not running and not uncertain:
                self.pending = None
                self.error = ''
            if self.pending and now >= self.deadline:
                self.error = 'Steam 启动尚未完成，请检查 Steam 后重试。' if self.pending=='starting' else '游戏尚未退出，请检查游戏窗口后重试。'
                self.pending = None
            close_verified = len(instances)==1 and not uncertain
            error = '；'.join(filter(None, [self.error, *(diagnostic_message(row) for row in diagnostics)]))
            self.cached = {'status':self.pending or ('running' if running else 'unknown' if uncertain else 'stopped'),
                'running':running, 'can_start':not running and not uncertain and not self.pending,
                'can_close':close_verified and not self.pending, 'target':instances[0] if close_verified else None,
                'count':len(instances), 'error':error, 'diagnostics':diagnostics,
                'unverified_count':len(diagnostics), 'control_unavailable':uncertain}
            self.checked, self.checked_pid = now, known_running_pid
            signature = (self.cached['status'], running, self.cached['can_close'],
                tuple((row['pid'], row.get('instance')) for row in instances),
                tuple((row['stage'], row['pid'], row['winerror'], row['reason']) for row in diagnostics), self.error)
            if signature != self.logged_state:
                self.logged_state = signature
                record('game_process_status_changed', status=self.cached['status'], running=running,
                       connected_pid=known_running_pid, verified_pids=[row['pid'] for row in instances],
                       can_close=self.cached['can_close'], diagnostics=diagnostics, error=self.error)
            return deepcopy(self.cached)

    def start(self):
        with self.guard:
            state = self.state(fresh=True)
            if not state['can_start']: raise ValueError('游戏已运行、正在启动或运行状态未确认。')
            self.backend.launch()
            self.pending, self.deadline, self.error = 'starting', self.clock()+60, ''
            return self.state(fresh=True)

    def close(self, target):
        with self.guard:
            state = self.state(fresh=True)
            if not state['can_close'] or target != state['target']:
                raise ValueError('游戏状态已变化，请刷新后重新确认关闭。')
            self.backend.close(target)
            self.pending, self.deadline, self.error = 'closing', self.clock()+20, ''
            return self.state(fresh=True)
