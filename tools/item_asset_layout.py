"""Reviewed 1.1.11 serialized ItemData prefix (including variable chest loot).

Serialized bools and PPtrs do not have the runtime FieldInfo alignment. Never
derive these offsets by subtracting an object header from a runtime offset.
"""
import struct


def item_layout(data):
    if len(data) < 240:
        raise ValueError('Truncated 1.1.11 ItemData')
    count = struct.unpack_from('<i', data, 128)[0]
    # EnemySpecialDropEntry: Item PPtr, Chance, Difficulties, IsBlackMist.
    if not 0 <= count <= 128:
        raise ValueError('Invalid chest special-drop array')
    shift = count * 24
    result = {'effect': 36, 'slave': 40, 'max_stack': 160 + shift, 'max_carry': 164 + shift,
              'slot': 168 + shift, 'class_mask': 176 + shift,
              'effects': 204 + shift}
    if result['effects'] + 24 > len(data):
        raise ValueError('Truncated ItemData after chest special drops')
    return result


def ancient_of_offset(data):
    """AncientOf follows both variable reference arrays, before FixedStats."""
    offset = item_layout(data)['effects']
    for stride in (12, 24):
        count = struct.unpack_from('<i', data, offset)[0]
        if not 0 <= count <= 256:
            raise ValueError('Invalid ItemData reference array')
        offset += 4 + count * stride
    if offset + 12 > len(data):
        raise ValueError('Missing AncientOf reference')
    return offset
