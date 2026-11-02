"""Small, per-profile preferences; data locations remain owned by paths.py."""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading


DEFAULTS = {'steam_acceleration_enabled': False, 'boss_hotkey': ''}


def normalize_hotkey(value):
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError('老板键格式无效。')
    if not value.strip():
        return ''
    parts = value.split('+')
    modifiers = parts[:-1]
    key = parts[-1].upper()
    if (not modifiers or len(set(modifiers)) != len(modifiers)
            or any(v not in ('Ctrl', 'Alt', 'Shift') for v in modifiers)
            or not {'Ctrl', 'Alt'}.intersection(modifiers)
            or not re.fullmatch(r'[A-Z0-9]|F(?:[1-9]|1[01])', key)):
        raise ValueError('请使用 Ctrl 或 Alt 搭配字母、数字或 F1–F11；不支持 Win 键。')
    normalized = '+'.join([m for m in ('Ctrl', 'Alt', 'Shift') if m in modifiers] + [key])
    if normalized in ('Alt+F4',):
        raise ValueError('此组合键由系统使用，请换一个组合。')
    return normalized


def validate_preferences(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('助手设置参数无效。')
    if type(value['steam_acceleration_enabled']) is not bool:
        raise ValueError('Steam 社区加速开关必须为布尔值。')
    return {'steam_acceleration_enabled': value['steam_acceleration_enabled'],
            'boss_hotkey': normalize_hotkey(value['boss_hotkey'])}


class AssistantSettings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.value = dict(DEFAULTS)
        self.load_error = ''
        try:
            if self.path.exists():
                value = json.loads(self.path.read_text(encoding='utf-8-sig'))
                if not isinstance(value, dict) or value.get('schema_version') != 1:
                    raise ValueError('设置版本无效')
                self.value = validate_preferences({k: value[k] for k in DEFAULTS})
        except (OSError, ValueError, KeyError, TypeError):
            self.load_error = '助手设置未能载入，原文件已保留；当前使用默认设置。'

    def snapshot(self):
        with self.lock:
            return dict(self.value)

    def acceleration_enabled(self):
        with self.lock:
            return self.value['steam_acceleration_enabled']

    def save(self, value, *, apply_hotkey=None):
        candidate = validate_preferences(value)
        with self.lock:
            changed = candidate['boss_hotkey'] != self.value['boss_hotkey']
            def persist():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(self.path.name + '.tmp')
                try:
                    temporary.write_text(json.dumps({'schema_version': 1, **candidate},
                                                     ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    temporary.replace(self.path)
                finally:
                    temporary.unlink(missing_ok=True)
            # Register the new shortcut before saving, and only release the
            # old shortcut after the atomic save. Conflicts leave both intact.
            if apply_hotkey:
                apply_hotkey(candidate['boss_hotkey'], persist)
            elif changed:
                raise ValueError('请在助手独立窗口中设置老板键。')
            else:
                persist()
            self.value = candidate
            self.load_error = ''
            return dict(candidate)
