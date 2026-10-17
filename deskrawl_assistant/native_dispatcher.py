"""Build-gated Unity main-thread mailbox, with no remote worker-thread calls.

Installing this component is an explicit mutation of the game process. Merely
constructing it, verifying a profile, and compiling a mailbox are read-only.
The original PeekMessageW import is chained, and the command is executed only
on the window-owning thread which already has an IL2CPP thread context. A
command uses runtime_invoke so managed exceptions remain in the game runtime.

After restoring the import we deliberately retain the small remote allocation:
another thread may still return through the trampoline. Windows reclaims it on
game exit; freeing it on a timer would create a use-after-free race.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import struct
import threading
import time

from .native_memory import MemoryReadError
from .paths import RESOURCE_ROOT
from .action_log import record

IDLE, QUEUED, RUNNING, DONE, CANCELLED, GUARD_FAILED, EXCEPTION, REJECTED, INVALID = range(9)
# A full 40-slot bag plus scene, reward and difficulty checks can exceed 256
# eight-byte guard records. Reserve 384 records in the existing 16 KiB mailbox;
# keep every predicate, the call/argument limits and the final allocation bound.
HEADER_SIZE, CALL_SIZE, MAX_CALLS, MAX_GUARDS, MAX_ARGS = 192, 64, 16, 384, 8
# Unity 6 GC handles are pointer-sized, not the uint32 used by older IL2CPP.
# The call table's final QWORD is the GC handle; +40 remains reserved and +44
# holds flags. Keeping a handle at +40 would overlap those flags on x64.
CALL_GCHANDLE_OFFSET = 56
REQUIRE_TRUE, SKIP_NULL_FIRST, REQUIRE_FALSE = 1, 2, 4


class NativeActionError(MemoryReadError):
    def __init__(self, message, *, uncertain=False, state=None, last_call=None, failed_method=None, mutation_started=None):
        super().__init__(message)
        self.uncertain, self.state = uncertain, state
        self.last_call, self.failed_method = last_call, failed_method
        self.mutation_started = mutation_started


@dataclass(frozen=True)
class Argument:
    """Reference arguments are object pointers, value arguments point at bytes."""
    value: int = 0
    raw: bytes | None = None
    result_index: int | None = None

    @classmethod
    def reference(cls, pointer):
        return cls(value=int(pointer))

    @classmethod
    def integer(cls, value):
        return cls(raw=struct.pack('<i', value))

    @classmethod
    def boolean(cls, value):
        if type(value) is not bool:
            raise ValueError('Boolean argument must be a boolean')
        return cls(raw=bytes([value]))

    @classmethod
    def previous(cls, index):
        return cls(result_index=index)


@dataclass(frozen=True)
class Invocation:
    method: str
    instance: int = 0
    arguments: tuple[Argument, ...] = ()
    require_true: bool = False
    skip_null_first: bool = False
    require_false: bool = False
    # Host-side safety metadata only; None means the side effects are unknown.
    mutates: bool | None = None


def failure_details(calls, state, last_call):
    """Describe the executed prefix without changing legacy uncertainty policy."""
    if state in (CANCELLED, GUARD_FAILED):
        return {'last_call': None, 'failed_method': None, 'mutation_started': False}
    if type(last_call) is not int or not 0 <= last_call < len(calls):
        return {'last_call': last_call, 'failed_method': None, 'mutation_started': None}
    prefix = calls[:last_call + 1]
    mutated = True if any(call.mutates is True for call in prefix) else (
        False if all(call.mutates is False for call in prefix) else None)
    return {'last_call': last_call, 'failed_method': calls[last_call].method, 'mutation_started': mutated}


def load_profile():
    return json.loads((RESOURCE_ROOT / 'data/game-actions-profile.json').read_text(encoding='utf-8'))


def verify_profile(reader, profile):
    """No permissive relocation: new native actions need an approved profile."""
    reader.check_game_files()
    root = reader.memory.path.parent
    for filename, expected in profile['sha256'].items():
        path = root / filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise NativeActionError('当前游戏构建尚未核验后台指令，托管动作已停止；普通装备读取仍可使用')
    for name in ('gameassembly.dll', 'unityplayer.dll'):
        module = reader.memory.modules.get(name)
        if not module or module['path'].casefold() != str(root / ('GameAssembly.dll' if name == 'gameassembly.dll' else 'UnityPlayer.dll')).casefold():
            raise NativeActionError('游戏模块路径不一致，未安装后台指令')
    for collection in ('exports', 'methods'):
        for item in profile[collection].values():
            expected = bytes.fromhex(item['prefix'])
            if reader.memory.read(reader.module['base'] + item['rva'], len(expected)) != expected:
                raise NativeActionError('游戏指令代码已改变，未安装后台指令')


def compile_mailbox(base, assembly, profile, image, original_peek, thread_id, calls, guards):
    """Pure encoding; references to past results are resolved on the main thread."""
    if not 1 <= len(calls) <= MAX_CALLS or len(guards) > MAX_GUARDS:
        raise ValueError('后台指令批次数量超出限制')
    out = bytearray(HEADER_SIZE + MAX_CALLS * CALL_SIZE + MAX_GUARDS * 24)
    call_start = HEADER_SIZE
    guard_start = call_start + MAX_CALLS * CALL_SIZE

    def append(raw, alignment=8):
        out.extend(b'\0' * (-len(out) % alignment))
        address = base + len(out)
        out.extend(raw)
        return address

    blank = append(b'\0')
    addresses = {}
    for index, call in enumerate(calls):
        spec = profile['methods'].get(call.method)
        if not spec or len(call.arguments) != len(spec['parameters']) or len(call.arguments) > MAX_ARGS:
            raise ValueError('后台指令签名不一致')
        if spec['isStatic'] != (call.instance == 0):
            raise ValueError('后台指令实例与静态签名不一致')
        class_name = spec.get('declaring_type', call.method.rsplit('.', 1)[0])
        name = addresses.setdefault(class_name, append(class_name.encode('ascii') + b'\0'))
        values = []
        for arg in call.arguments:
            if arg.result_index is not None:
                if type(arg.result_index) is not int or not 0 <= arg.result_index < index:
                    raise ValueError('只能引用本批次已经执行的结果')
                values.append(struct.pack('<IIQ', 1, arg.result_index, 0))
            else:
                value = append(arg.raw) if arg.raw is not None else arg.value
                if type(value) is not int or not 0 <= value < 2**64:
                    raise ValueError('后台指令参数无效')
                values.append(struct.pack('<IIQ', 0, 0, value))
        arguments = append(b''.join(values)) if values else 0
        if call.require_true and call.require_false:
            raise ValueError('布尔返回条件互相冲突')
        if (call.require_true or call.require_false) and spec['returnType'] != 'System.Boolean':
            raise ValueError('布尔返回条件只能用于布尔方法')
        flags = (REQUIRE_TRUE if call.require_true else 0) | (SKIP_NULL_FIRST if call.skip_null_first else 0) | (REQUIRE_FALSE if call.require_false else 0)
        struct.pack_into('<QIIQQQIIQ', out, call_start + index * CALL_SIZE,
            name, int(spec['token'], 16), len(values), assembly + spec['rva'], arguments, 0, 0, flags, call.instance)
    rows = []
    for address, raw in guards:
        if not isinstance(raw, bytes) or not raw:
            raise ValueError('前置校验数据无效')
        for offset in range(0, len(raw), 8):
            piece = raw[offset:offset+8]
            rows.append((address+offset, int.from_bytes(piece, 'little'), (1 << (len(piece)*8))-1))
    if len(rows) > MAX_GUARDS:
        raise ValueError('前置校验数据超出限制')
    for index, row in enumerate(rows):
        struct.pack_into('<QQQ', out, guard_start + index*24, *row)
    exports = profile['exports']
    struct.pack_into('<IIII', out, 48, thread_id, len(calls), len(rows), 0)
    for offset, value in ((80, original_peek), (88, assembly+exports['il2cpp_runtime_invoke']['rva']),
            (96, assembly+exports['il2cpp_class_get_methods']['rva']),
            (104, assembly+exports['il2cpp_class_from_name']['rva']),
            (112, assembly+exports['il2cpp_thread_current']['rva']),
            (120, assembly+exports['il2cpp_gchandle_new']['rva']),
            (128, assembly+exports['il2cpp_gchandle_free']['rva']),
            (136, image), (144, blank), (152, base+guard_start), (160, base+call_start)):
        struct.pack_into('<Q', out, offset, value)
    struct.pack_into('<I', out, 168, 1)
    if len(out) > 16384:
        raise ValueError('后台指令数据超出限制')
    return bytes(out)


class _Assembler:
    def __init__(self):
        self.code, self.labels, self.fixups = bytearray(), {}, []

    def emit(self, data):
        self.code.extend(bytes.fromhex(data) if isinstance(data, str) else data)

    def label(self, label):
        self.labels[label] = len(self.code)

    def jump(self, opcode, label):
        self.emit(opcode)
        self.fixups.append((len(self.code), label))
        self.emit(b'\0'*4)

    def finish(self):
        for pos, label in self.fixups:
            struct.pack_into('<i', self.code, pos, self.labels[label]-(pos+4))
        return bytes(self.code)


def build_trampoline(mailbox):
    """Windows x64 ABI: shadow space, 16-byte calls, seven nonvolatile saves.

    This is an import replacement, never an inline patch. No game function is
    entered from another thread, and nested message pumps cannot reenter a job.
    """
    a = _Assembler()
    a.emit('53 56 57 41 54 41 55 41 56 41 57 48 81 ec b0 00 00 00')
    a.emit('49 bc'); a.emit(struct.pack('<Q', mailbox))  # r12 = mailbox
    a.emit('48 8b 84 24 10 01 00 00 48 89 44 24 20')  # fifth PeekMessage argument
    a.emit('41 ff 54 24 50 89 44 24 30')  # chain original, save original BOOL
    a.emit('4c 89 e3 65 48 8b 04 25 48 00 00 00 3b 43 30')  # TEB thread ID
    a.jump('0f 85', 'return')
    a.emit('83 bb a8 00 00 00 00'); a.jump('0f 84', 'return')
    a.emit('f0 ff 43 40 48 ff 43 48')  # inflight and main-thread heartbeat
    a.emit('b8 01 00 00 00 ba 02 00 00 00 f0 0f b1 13')
    a.jump('0f 85', 'leave')
    a.emit('83 7b 04 00'); a.jump('0f 85', 'cancelled')
    a.emit('ff 53 70 48 85 c0'); a.jump('0f 84', 'invalid')
    a.emit('48 8b b3 98 00 00 00 8b 7b 38 85 ff'); a.jump('0f 84', 'guards_done')
    a.label('guards')
    a.emit('48 8b 06 48 8b 00 48 23 46 10 48 3b 46 08')
    a.jump('0f 85', 'guard_failed')
    a.emit('48 83 c6 18 ff cf'); a.jump('0f 85', 'guards')
    a.label('guards_done')
    a.emit('45 31 ed 4c 8b b3 a0 00 00 00')  # r13 call index, r14 call table
    a.label('next_call')
    a.emit('4c 89 ee 48 c1 e6 06 4c 01 f6 44 89 6b 3c')
    a.emit('48 8b 8b 88 00 00 00 48 8b 93 90 00 00 00 4c 8b 06 ff 53 68')
    a.emit('48 85 c0'); a.jump('0f 84', 'invalid')
    a.emit('49 89 c7 48 c7 44 24 38 00 00 00 00')
    a.label('find_method')
    a.emit('4c 89 f9 48 8d 54 24 38 ff 53 60 48 85 c0')
    a.jump('0f 84', 'invalid')
    a.emit('8b 50 48 3b 56 08'); a.jump('0f 85', 'find_method')
    a.emit('48 8b 10 48 3b 56 10'); a.jump('0f 85', 'invalid')
    a.emit('0f b6 50 52 3b 56 0c'); a.jump('0f 85', 'invalid')
    a.emit('48 89 84 24 80 00 00 00 45 31 d2 4c 8b 5e 18')
    a.label('arguments')
    a.emit('44 3b 56 0c'); a.jump('0f 83', 'args_done')
    a.emit('4c 89 d0 48 c1 e0 04 4c 01 d8 83 38 00')
    a.jump('0f 84', 'immediate_arg')
    a.emit('8b 40 04 48 c1 e0 06 4c 01 f0 48 8b 40 20')
    a.jump('e9', 'store_arg')
    a.label('immediate_arg'); a.emit('48 8b 40 08')
    a.label('store_arg'); a.emit('4a 89 44 d4 40 41 ff c2'); a.jump('e9', 'arguments')
    a.label('args_done')
    a.emit('f6 46 2c 02'); a.jump('0f 84', 'invoke')
    a.emit('48 83 7c 24 40 00'); a.jump('0f 84', 'advance')
    a.label('invoke')
    a.emit('48 c7 43 28 00 00 00 00 48 8b 8c 24 80 00 00 00 48 8b 56 30')
    a.emit('4c 8d 44 24 40 4c 8d 4b 28 ff 53 58 48 89 46 20 48 89 43 20')
    a.emit('48 83 7b 28 00'); a.jump('0f 85', 'exception')
    a.emit('48 85 c0'); a.jump('0f 84', 'check_bool')
    a.emit('48 89 c1 31 d2 ff 53 78 48 89 46 38')  # full-width GC handle at +56
    a.label('check_bool')
    a.emit('f6 46 2c 05'); a.jump('0f 84', 'advance')
    a.emit('48 8b 46 20 48 85 c0'); a.jump('0f 84', 'rejected')
    a.emit('f6 46 2c 04'); a.jump('0f 85', 'check_false')
    a.emit('80 78 10 01'); a.jump('0f 85', 'rejected')
    a.jump('e9', 'advance')
    a.label('check_false'); a.emit('80 78 10 00'); a.jump('0f 85', 'rejected')
    a.label('advance')
    a.emit('41 ff c5 44 3b 6b 34'); a.jump('0f 82', 'next_call')
    a.emit('bf 03 00 00 00'); a.jump('e9', 'cleanup')
    for label, state in (('cancelled', CANCELLED), ('guard_failed', GUARD_FAILED),
            ('exception', EXCEPTION), ('rejected', REJECTED), ('invalid', INVALID)):
        a.label(label); a.emit('bf'); a.emit(struct.pack('<I', state)); a.jump('e9', 'cleanup')
    a.label('cleanup')
    a.emit('45 31 ed 4c 8b b3 a0 00 00 00')
    a.label('free_handles')
    a.emit('44 3b 6b 34'); a.jump('0f 83', 'finished')
    a.emit('4c 89 ee 48 c1 e6 06 4c 01 f6 48 8b 4e 38 48 85 c9')
    a.jump('0f 84', 'handle_done')
    a.emit('ff 93 80 00 00 00 48 c7 46 38 00 00 00 00')
    a.label('handle_done'); a.emit('41 ff c5'); a.jump('e9', 'free_handles')
    a.label('finished')
    a.emit('48 8b 43 10 48 89 43 08 89 3b')  # publish terminal state last
    a.label('leave'); a.emit('f0 ff 4b 40')
    a.label('return')
    a.emit('8b 44 24 30 48 81 c4 b0 00 00 00 41 5f 41 5e 41 5d 41 5c 5f 5e 5b c3')
    return a.finish()


class MainThreadDispatcher:
    """One bounded job at a time; cancellation never interrupts an equip batch."""
    def __init__(self, reader, *, profile=None):
        self.reader = reader
        self.profile = profile or load_profile()
        self.installed = False
        self.lock = threading.RLock()
        self.sequence = 0
        self.handle = None
        self.mailbox = self.code = 0
        self.original_peek = self.thread_id = self.iat = 0
        self.verified = False
        self.tainted = False

    def verify(self):
        verify_profile(self.reader, self.profile)
        self.verified = True
        return {'compatible': True, 'native_verified': False, 'build': self.profile['build']}

    def _api(self):
        import ctypes as C
        from ctypes import wintypes as W
        from .native_memory import K
        self.C, self.K = C, K
        K.VirtualAllocEx.argtypes = (W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, W.DWORD)
        K.VirtualAllocEx.restype = C.c_void_p
        K.VirtualProtectEx.argtypes = (W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, C.POINTER(W.DWORD))
        K.VirtualProtectEx.restype = W.BOOL
        K.FlushInstructionCache.argtypes = (W.HANDLE, C.c_void_p, C.c_size_t)
        K.FlushInstructionCache.restype = W.BOOL
        K.WriteProcessMemory.argtypes = (W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t))
        K.WriteProcessMemory.restype = W.BOOL
        user = C.WinDLL('user32', use_last_error=True)
        user.GetWindowThreadProcessId.argtypes = (W.HWND, C.POINTER(W.DWORD))
        user.GetWindowThreadProcessId.restype = W.DWORD
        user.IsWindowVisible.argtypes = (W.HWND,)
        user.IsWindowVisible.restype = W.BOOL
        user.GetWindow.argtypes = (W.HWND, W.UINT)
        user.GetWindow.restype = W.HWND
        user.GetClassNameW.argtypes = (W.HWND, W.LPWSTR, C.c_int)
        user.GetClassNameW.restype = C.c_int
        callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        user.EnumWindows.argtypes = (callback, W.LPARAM)
        windows = []
        def found(hwnd, _):
            pid = W.DWORD()
            tid = user.GetWindowThreadProcessId(hwnd, C.byref(pid))
            if pid.value == self.reader.pid and user.IsWindowVisible(hwnd) and not user.GetWindow(hwnd, 4):
                name = C.create_unicode_buffer(256)
                if tid and user.GetClassNameW(hwnd, name, len(name)) and name.value == 'UnityWndClass':
                    windows.append((int(hwnd), tid))
            return True
        user.EnumWindows(callback(found), 0)
        tids = {tid for _, tid in windows}
        if len(tids) != 1:
            raise NativeActionError('未唯一确认游戏窗口主线程，未安装后台指令')
        self.thread_id = tids.pop()

    def _write(self, address, raw):
        C, K = self.C, self.K
        value, written = C.create_string_buffer(raw), C.c_size_t()
        if not K.WriteProcessMemory(self.handle, address, value, len(raw), C.byref(written)) or written.value != len(raw):
            raise NativeActionError('后台指令数据写入不完整', uncertain=self.installed)

    def _protect(self, address, size, protection):
        from ctypes import wintypes as W
        old = W.DWORD()
        if not self.K.VirtualProtectEx(self.handle, address, size, protection, self.C.byref(old)):
            raise NativeActionError('后台指令内存权限设置失败')
        return old.value

    def _iat_write(self, value):
        # A pointer-sized write is brief, but suspending prevents a concurrent
        # indirect load from observing a partial WriteProcessMemory operation.
        from .container_transfer import NT
        if NT.NtSuspendProcess(self.handle) < 0:
            raise NativeActionError('无法短暂暂停消息入口更新，未修改接口')
        try:
            current = self.reader.memory.u64(self.iat)
            expected = self.original_peek if value == self.code else self.code
            if current != expected:
                raise NativeActionError('消息接口在更新前改变，未覆盖其他组件')
            old = self._protect(self.iat, 8, 4)
            try:
                self._write(self.iat, struct.pack('<Q', value))
            finally:
                self._protect(self.iat, 8, old)
        finally:
            if NT.NtResumeProcess(self.handle) < 0 and NT.NtResumeProcess(self.handle) < 0:
                raise NativeActionError('恢复游戏消息线程失败，请停止托管并检查游戏', uncertain=True)

    def install(self):
        with self.lock:
            if self.installed:
                return
            self.verify()
            self._api()
            self.handle = self.K.OpenProcess(0x800 | 0x400 | 0x10 | 0x20 | 0x8, False, self.reader.pid)
            if not self.handle:
                raise NativeActionError('无法取得后台指令所需进程权限')
            try:
                unity = self.reader.memory.modules['unityplayer.dll']
                self.iat = unity['base'] + self.profile['iat']['peek_message_w_rva']
                self.original_peek = self.reader.memory.u64(self.iat)
                allowed = ('user32.dll', 'win32u.dll')
                if not any(m['base'] <= self.original_peek < m['base']+m['size']
                        for n, m in self.reader.memory.modules.items() if n in allowed):
                    raise NativeActionError('游戏消息接口已被其他组件修改，未安装后台指令')
                self.mailbox = self.K.VirtualAllocEx(self.handle, None, 16384, 0x3000, 4) or 0
                self.code = self.K.VirtualAllocEx(self.handle, None, 4096, 0x3000, 4) or 0
                if not self.mailbox or not self.code:
                    raise NativeActionError('无法分配后台指令缓冲区')
                image = self.reader.memory.u64(self.reader.classes['Inventory'])
                dummy = Invocation('TownPortalSystem.frj', 1)
                empty = bytearray(compile_mailbox(self.mailbox, self.reader.module['base'], self.profile,
                    image, self.original_peek, self.thread_id, [dummy], []))
                struct.pack_into('<I', empty, 52, 0)  # idle probe has no invocations
                self._write(self.mailbox, bytes(empty))
                code = build_trampoline(self.mailbox)
                self._write(self.code, code)
                self._protect(self.code, len(code), 0x20)
                if not self.K.FlushInstructionCache(self.handle, self.code, len(code)):
                    raise NativeActionError('无法确认后台指令缓存')
                if self.reader.memory.u64(self.iat) != self.original_peek:
                    raise NativeActionError('安装期间游戏消息接口改变，未安装后台指令')
                self.installed = True
                self._iat_write(self.code)
                started = time.monotonic()
                while time.monotonic()-started < 2:
                    if self.reader.memory.u64(self.mailbox+72):
                        return
                    time.sleep(.02)
                raise NativeActionError('游戏主线程未响应后台调度器，已撤销消息接口')
            except Exception:
                self.close()
                raise

    def invoke(self, calls, guards=(), *, cancel_event=None, timeout=5, validate=lambda:True):
        with self.lock:
            if self.tainted:
                raise NativeActionError('上一条后台指令完成状态不明确，已停止继续执行', uncertain=True)
            if (cancel_event is not None and cancel_event.is_set()) or not validate():
                raise NativeActionError('托管操作已停止', state=CANCELLED)
            self.install()
            self.reader.check_game_files()
            if self.reader.memory.u64(self.iat) != self.code:
                raise NativeActionError('后台调度入口被其他组件改变，操作已停止')
            header = self.reader.memory.read(self.mailbox, HEADER_SIZE)
            if struct.unpack_from('<I', header)[0] in (QUEUED, RUNNING):
                raise NativeActionError('上一条后台指令仍在执行', uncertain=True)
            image = self.reader.memory.u64(self.reader.classes['Inventory'])
            raw = bytearray(compile_mailbox(self.mailbox, self.reader.module['base'], self.profile,
                image, self.original_peek, self.thread_id, calls, list(guards)))
            self.sequence += 1
            struct.pack_into('<Q', raw, 16, self.sequence)
            # The hook may inspect IDLE concurrently. Keep its inflight counter,
            # heartbeat and enabled bit out of the bulk copy.
            self._write(self.mailbox, bytes(raw[:64]))
            self._write(self.mailbox+80, bytes(raw[80:168]))
            self._write(self.mailbox+HEADER_SIZE, bytes(raw[HEADER_SIZE:]))
            if (cancel_event is not None and cancel_event.is_set()) or not validate():
                raise NativeActionError('托管操作已停止或配置改变，指令未提交', state=CANCELLED)
            self._write(self.mailbox, struct.pack('<I', QUEUED))
            deadline = time.monotonic() + max(.1, min(float(timeout), 30))
            state = QUEUED
            while True:
                try:
                    current = self.reader.memory.read(self.mailbox, HEADER_SIZE)
                except MemoryReadError as exc:
                    # A submitted command may have executed before the process
                    # exited. Never report this as safely cancelled or retry it.
                    self.tainted = True
                    raise NativeActionError('游戏已退出或后台指令状态无法读取，完成状态不明确；已停止继续执行',
                                            uncertain=True,state=state) from exc
                state = struct.unpack_from('<I', current)[0]
                sequence = struct.unpack_from('<Q', current, 8)[0]
                if state >= DONE and sequence == self.sequence:
                    if state == DONE:
                        return {'invoked': True, 'completed': True, 'state': state,
                            'sequence': self.sequence, 'native_verified': False,
                            'last_call': struct.unpack_from('<I', current, 60)[0]}
                    messages = {CANCELLED:'托管操作已停止，指令未执行', GUARD_FAILED:'物品或角色状态改变，指令未执行',
                        EXCEPTION:'游戏原生操作返回异常，已停止后续动作', REJECTED:'游戏原生操作拒绝本次请求',
                        INVALID:'游戏方法签名或主线程环境校验失败，已停止动作'}
                    last_call = struct.unpack_from('<I', current, 60)[0]
                    raise NativeActionError(messages.get(state, '后台指令状态异常'), state=state,
                        uncertain=state == EXCEPTION or (state in (INVALID, REJECTED) and last_call > 0),
                        **failure_details(calls, state, last_call))
                if cancel_event is not None and cancel_event.is_set():
                    self._write(self.mailbox+4, struct.pack('<I', 1))
                if not self.reader.memory.is_running():
                    self.tainted = True
                    raise NativeActionError('游戏已退出，托管操作已停止', uncertain=True,state=state)
                if time.monotonic() >= deadline:
                    self._write(self.mailbox+4, struct.pack('<I', 1))
                    self.tainted = True
                    record('game_action_timeout',sequence=self.sequence,state=state,
                           last_call=struct.unpack_from('<I',current,60)[0])
                    raise NativeActionError('后台指令响应超时，完成状态不明确；已停止继续执行', uncertain=True, state=state)
                time.sleep(.02)

    def close(self):
        with self.lock:
            if not self.handle:
                return
            try:
                if self.installed and self.reader.memory.is_running():
                    self._write(self.mailbox+168, b'\0'*4)
                    self._write(self.mailbox+4, struct.pack('<I', 1))
                    if self.reader.memory.u64(self.iat) == self.code:
                        self._iat_write(self.original_peek)
                    # Allocations remain mapped for safe returns through a hook
                    # that was already entered. They contain no persistent file.
            finally:
                self.K.CloseHandle(self.handle)
                self.handle = None
                self.installed = False
