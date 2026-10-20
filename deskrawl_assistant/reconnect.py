"""Serial reconnect decisions. Login callbacks never authorize inventory writes."""
from copy import deepcopy

from .models import ValidationError

DEFAULT_RECONNECT = {'enabled': False, 'character_index': 1}


def validate_settings(value):
    if (not isinstance(value, dict) or set(value) != set(DEFAULT_RECONNECT)
            or type(value['enabled']) is not bool
            or type(value['character_index']) is not int or not 1 <= value['character_index'] <= 100):
        raise ValidationError('断线重连需设置开关及 1 至 100 的角色列表序号。')
    return deepcopy(value)


class ReconnectFlow:
    """Only an observed disconnect arms login; ordinary menu use never does."""
    def __init__(self):
        self.active = False
        self.started = None
        self.next_at = 0
        self.attempts = {}

    def decide(self, state, settings, expected_character, now):
        if not settings['enabled']:
            return {'kind': 'idle'}
        if state.get('disconnected') is True:
            if not self.active:
                self.active, self.started = True, now
        if not self.active:
            return {'kind': 'idle'}
        if not expected_character or not expected_character.startswith('1:'):
            return {'kind': 'pause', 'reason': '重连前的在线角色身份未确认，请手动登录后重新启动托管。'}
        if now - self.started >= 600:
            return {'kind': 'pause', 'reason': '断线重连等待超过 10 分钟，请检查网络或游戏提示后继续托管。'}
        if state.get('in_game') is True and state.get('popup_visible') is not True:
            return {'kind': 'verify', 'reason': '已进入游戏，正在核对原角色和背包数据。'}
        if state.get('popup_visible') is True and state.get('disconnected') is not True:
            return {'kind': 'wait', 'reason': '重连遇到其他游戏提示，请手动处理；不会自动确认该弹窗。'}
        if now < self.next_at:
            return {'kind': 'wait', 'reason': '等待游戏确认重连操作，不重复点击。'}
        if state.get('disconnected') is True:
            if state.get('confirm_ready') is not True:
                return {'kind': 'wait', 'reason': '等待掉线确认按钮可用。'}
            return {'kind': 'confirm_disconnect', 'reason': '确认掉线提示，等待在线角色列表。'}
        if not state.get('menu_ready'):
            return {'kind': 'wait', 'reason': '等待游戏返回角色选择界面。'}
        if state.get('mode') != 1:
            return {'kind': 'online_tab', 'reason': '切换到在线角色列表。'}
        if not state.get('profiles_ready') or state.get('busy') is not False:
            return {'kind': 'wait', 'reason': '等待在线角色列表加载和服务器同步。'}
        index = settings['character_index']
        entries = state.get('entries', [])
        if index > len(entries):
            return {'kind': 'pause', 'reason': f'重连配置为第 {index} 个角色，实际列表仅有 {len(entries)} 项，请检查序号。'}
        entry = entries[index - 1]
        if entry.get('mode') != 1 or entry.get('filled') is not True:
            return {'kind': 'pause', 'reason': f'重连所选第 {index} 项不是已有在线角色，请检查序号。'}
        # The chosen list position may differ from the native save-slot number.
        if str(entry.get('slot')) != expected_character.split(':', 2)[1]:
            return {'kind': 'pause', 'reason': '重连所选序号与原托管角色不同，已暂停，避免对其他角色执行整理。'}
        if state.get('selected_slot') != entry['slot']:
            return {'kind': 'select_character', 'reason': f'选择列表第 {index} 个角色。'}
        if state.get('play_ready') is not True:
            return {'kind': 'wait', 'reason': '等待游戏允许开始在线游戏。'}
        return {'kind': 'play', 'reason': f'开始进入列表第 {index} 个角色，等待游戏加载。'}

    def submitted(self, kind, now):
        self.attempts[kind] = self.attempts.get(kind, 0) + 1
        # Start can be accepted without immediately leaving the menu. Give the
        # native cloud request time; retries still require the full fresh UI.
        self.next_at = now + (30 if kind == 'play' else 5)
