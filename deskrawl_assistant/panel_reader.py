"""Read the native panel visibility used by PanelBase.Close, without invoking it."""
from .native_memory import MemoryReadError


# Reviewed against the supported UnityPlayer build: Component.gameObject reads
# the native component's +32 pointer, and GameObject.activeSelf reads +70. Check
# both accessor bodies before following those offsets; unknown engines fail
# closed instead of falling back to PanelBase's unrelated managed flags.
_ACCESSORS = (
    (0xBA520, bytes.fromhex('40534883ec20488b5120488d4c2430')),
    (0xBCFB0, bytes.fromhex('0fb64146c3')),
)


def panel_active_self(copy, panel):
    unity = copy.reader.memory.modules.get('unityplayer.dll')
    if not unity:
        raise MemoryReadError('商人窗口显示状态尚未确认：UnityPlayer 未载入。')
    base, end = unity['base'], unity['base'] + unity['size']
    for rva, expected in _ACCESSORS:
        if copy.read(base + rva, len(expected)) != expected:
            raise MemoryReadError('商人窗口显示接口已变化，需要维护适配。')
    component = copy.ptr(panel + 16)
    if not component or not base <= copy.ptr(component) < end:
        raise MemoryReadError('商人窗口原生组件尚未确认。')
    game_object = copy.ptr(component + 32)
    if not game_object or not base <= copy.ptr(game_object) < end:
        raise MemoryReadError('商人窗口原生对象尚未确认。')
    return copy.boolean(game_object + 70)
