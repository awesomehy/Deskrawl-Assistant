"""Build-gated reads and original callbacks for the online disconnect menu."""
from .game_actions import _Read
from .native_memory import MemoryReadError
from .native_dispatcher import Argument, Invocation, NativeActionError


def _notice_kind(text, has_actions):
    if has_actions or not text:
        return None
    return {'连接已断开。': 'disconnect', '无法连接服务器。': 'server_unavailable'}.get(
        text.splitlines()[0].strip())


class ReconnectRuntime:
    def __init__(self, actions):
        self.actions = actions
        self.reader, self.profile = actions.reader, actions.profile

    def _active(self, read, obj):
        if not obj:
            return False
        read.verify(obj, 'GameObject')
        native = read.ptr(obj + 16)
        # UnityPlayer's installed-build get_activeSelf implementation reads
        # this byte. The dispatcher profile gates the complete engine hash.
        return bool(native and read.boolean(native + 0x46))

    def _interactable(self, read, obj):
        if not obj:
            return False
        klass = read.ptr(obj)
        read.klass(klass, 'GameButton')
        klass = read.ptr(klass + 88)
        read.klass(klass, 'Button')
        read.klass(read.ptr(klass + 88), 'Selectable')
        return read.boolean(obj + 32) and read.boolean(obj + 216) and read.boolean(obj + 232)

    def _popup_text(self, read, obj):
        if not obj:
            return ''
        klass = read.ptr(obj)
        for _ in range(5):
            if self.reader.class_name(klass) == 'TMP_Text':
                read.klass(klass, 'TMP_Text')
                pointer = read.ptr(obj + 224)
                value = self.reader.string(pointer)
                # Retain the content as a preflight guard, without logging it.
                if pointer and value:
                    read.read(pointer + 16, 4 + len(value.encode('utf-16-le')))
                return value or ''
            klass = read.ptr(klass + 88)
            if not klass:
                break
        raise MemoryReadError('重连提示文本类型未确认。')

    def _read(self):
        self.reader.check_game_files()
        if not self.actions.dispatcher.verified:
            self.actions.dispatcher.verify()
        read = _Read(self.reader, self.profile)
        manager = read.singleton('GameManager')
        game_state = read.integer(manager + 168)
        popup = read.singleton('PopupPanel')
        popup_visible = self._active(read, read.ptr(popup + 32))
        text = self._popup_text(read, read.ptr(popup + 40)) if popup_visible else ''
        # Only exact observed network notices with no delegate can be dismissed.
        # The retry notice cannot arm login; the owning flow must have observed
        # an actual disconnect first. Generic and destructive prompts stay manual.
        notice = _notice_kind(text, bool(read.ptr(popup + 64) or read.ptr(popup + 72))) if popup_visible else None
        disconnected = notice == 'disconnect'
        state = {'in_game': game_state == 1, 'popup_visible': popup_visible,
                 'disconnected': disconnected, 'retryable_network_error': notice == 'server_unavailable',
                 'menu_ready': False}
        context = {'popup': popup}
        if notice:
            state['confirm_ready'] = self._interactable(read, read.ptr(popup + 48))
        if game_state == 0:
            menu = read.singleton('MainMenuUI')
            context['menu'] = menu
            state['menu_ready'] = self._active(read, read.ptr(menu + 32))
            state.update(mode=read.integer(menu + 188), selected_slot=read.integer(menu + 192),
                         profiles_ready=read.boolean(menu + 200))
            if state['menu_ready'] and state['mode'] == 1 and state['profiles_ready']:
                cloud = read.singleton('CloudClient')
                state['busy'] = any([read.boolean(cloud + offset) for offset in (232, 248, 249)])
                pointers = read.pointers(read.ptr(menu + 112), 'SaveSlotEntryUI', limit=100)
                context['entries'] = pointers
                state['entries'] = []
                for obj in pointers:
                    read.verify(obj, 'SaveSlotEntryUI')
                    state['entries'].append({'mode': read.integer(obj + 32), 'slot': read.integer(obj + 36),
                                             'filled': read.boolean(obj + 40)})
                state['play_ready'] = self._interactable(read, read.ptr(menu + 120))
        read.finish()
        return state, context, read

    def snapshot(self):
        return self._read()[0]

    def execute(self, kind, character_index, expected_character, *, validate, cancel_event=None):
        from .reconnect import ReconnectFlow, validate_settings
        settings = validate_settings({'enabled': True, 'character_index': character_index})
        state, context, read = self._read()
        flow = ReconnectFlow()
        # The owning managed flow already observed the disconnect, then each
        # callback is independently re-planned against a fresh native snapshot.
        flow.active, flow.started = True, 0
        # The owning flow remembers whether it has selected this attempt. Here
        # recheck the requested callback against native slot and role guards.
        flow.selection_confirmed = kind != 'select_character'
        planned = flow.decide(state, settings, expected_character, 0)['kind']
        if kind != planned:
            raise NativeActionError('重连界面已变化，等待重新读取。')
        if kind in {'confirm_disconnect', 'confirm_network_error'}:
            if state.get('confirm_ready') is not True:
                raise NativeActionError('掉线确认按钮尚不可用。')
            call = Invocation('PopupPanel.confirm_disconnect', context['popup'], mutates=True)
        elif kind == 'online_tab':
            call = Invocation('MainMenuUI.online_tab', context['menu'], mutates=True)
        elif kind == 'select_character':
            call = Invocation('MainMenuUI.select_character', context['menu'],
                              (Argument.reference(context['entries'][character_index - 1]),), mutates=True)
        elif kind == 'play':
            call = Invocation('MainMenuUI.play', context['menu'], mutates=True)
        else:
            raise NativeActionError('当前不允许提交重连操作。')
        if not validate():
            raise NativeActionError('托管已取消，不再执行重连。')
        self.actions.dispatcher.invoke([call], read.guards, cancel_event=cancel_event, validate=validate)
        return {'submitted': True}
