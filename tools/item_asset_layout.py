"""Reviewed 1.1.0 serialized ItemData prefix (including variable chest loot).

Serialized bools and PPtrs do not have the runtime FieldInfo alignment. Never
derive these offsets by subtracting an object header from a runtime offset.
"""
import struct


def item_layout(data):
    if len(data) < 200:
        raise ValueError('Truncated 1.1.0 ItemData')
    count = struct.unpack_from('<i', data, 100)[0]
    # EnemySpecialDropEntry: Item PPtr, Chance, Difficulties, IsBlackMist.
    if not 0 <= count <= 128:
        raise ValueError('Invalid chest special-drop array')
    shift = count * 24
    result = {'effect': 36, 'slave': 40, 'max_stack': 132 + shift, 'max_carry': 136 + shift,
              'slot': 140 + shift, 'class_mask': 148 + shift,
              'effects': 176 + shift}
    if result['effects'] + 24 > len(data):
        raise ValueError('Truncated ItemData after chest special drops')
    return result
