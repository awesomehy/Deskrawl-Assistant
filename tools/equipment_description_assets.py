"""Read the linked status/ability tokens used by exact-build equipment prose.

No native methods are called. Exporters verify the game hashes before using
these reviewed serialized layouts; ambiguous managed records abort the export.
"""
import math
import re
import struct

from deskrawl_assistant.native_reader import decode_obscured_float


class Reader:
    def __init__(self, data, offset=0):
        self.data, self.pos = data, offset

    def take(self, fmt):
        value = struct.unpack_from('<' + fmt, self.data, self.pos)
        self.pos += struct.calcsize('<' + fmt)
        return value[0] if len(value) == 1 else value

    def string(self):
        length = self.take('i')
        end = self.pos + length
        if not 0 <= length <= 256 or end > len(self.data):
            raise ValueError('Invalid equipment token string')
        value = self.data[self.pos:end].decode('utf-8')
        self.pos = (end + 3) & ~3
        if self.pos > len(self.data) or any(self.data[end:self.pos]):
            raise ValueError('Invalid equipment token string padding')
        return value

    def leveled(self):
        values = []
        for _ in range(2):
            raw = self.data[self.pos:self.pos + 28]
            if len(raw) != 28 or any(raw[20:]):
                raise ValueError('Unverified equipment leveled-float layout')
            value = decode_obscured_float(raw[:20])
            if not math.isfinite(value):
                raise ValueError('Nonfinite equipment token value')
            values.append(value)
            self.pos += 28
        return values


def token(reader, value):
    """Apply the serialized TokenSpec format and suffix at the base level."""
    name, fmt, suffix = (reader.string() for _ in range(3))
    highlight, floor = reader.take('ii')
    if highlight not in (0, 1) or floor not in (0, 1) or not math.isfinite(value):
        raise ValueError('Invalid equipment TokenSpec')
    if not re.fullmatch(r'0(?:\.#{1,2})?%?', fmt):
        raise ValueError('Unverified equipment token format: ' + fmt)
    value *= 100 if fmt.endswith('%') else 1
    if floor:
        value = math.floor(value)
    places = fmt.count('#')
    rendered = f'{value:.{places}f}'
    if places:
        rendered = rendered.rstrip('0').rstrip('.')
    return name, rendered + ('%' if fmt.endswith('%') else '') + suffix


def managed_body(data, kind):
    """Find one exact Unity managed-reference type header, never a loose name."""
    def serialized(value):
        raw = struct.pack('<i', len(value)) + value.encode()
        return raw + bytes(-len(raw) % 4)
    header = serialized(kind) + serialized('') + serialized('Assembly-CSharp')
    at = data.find(header)
    if at < 0 or at % 4 or data.find(header, at + 1) >= 0:
        raise ValueError('Missing or ambiguous equipment managed record: ' + kind)
    return Reader(data, at + len(header))


def damage_tokens(data):
    reader = managed_body(data, 'DamageEffect')
    # AbilityEffect: two PPtrs, volume and pitch vectors; then DamageEffect.
    reader.pos += 40
    base_damage = reader.take('q')
    scaling, growth = reader.leveled()
    can_crit, direct, suppress, damage_type, stutter = reader.take('iiiif')
    if (base_damage != 0 or growth != 0 or scaling < 0 or not math.isfinite(stutter)
            or any(v not in (0, 1) for v in (can_crit, direct, suppress)) or damage_type not in range(6)):
        raise ValueError('Unverified equipment damage description')
    name, value = token(reader, scaling)
    if not name:
        raise ValueError('Missing equipment damage token')
    return {name: value}


def status_tokens(data, *, shield=False):
    reader = Reader(data, 12)
    show, stackable, stacks = reader.take('iii')
    duration, _ = reader.leveled()
    if show not in (0, 1) or stackable not in (0, 1) or stacks < 1 or duration < 0:
        raise ValueError('Invalid equipment status parameters')
    name, value = token(reader, duration)
    if name != 'duration':
        raise ValueError('Unverified equipment duration token')
    result = {'duration': value, 'stacks': str(stacks)}
    if shield:
        reader = managed_body(data, 'ShieldBehavior')
        health, growth = reader.leveled()
        mana, mana_growth = reader.leveled()
        reapply = reader.take('i')
        if growth or mana or mana_growth or reapply not in (0, 1) or not 0 <= health <= 1:
            raise ValueError('Unverified equipment shield parameters')
        name, value = token(reader, health)
        if name != 'shield':
            raise ValueError('Unverified equipment shield token')
        result[name] = value
    return result


def linked_asset(data, offset, resolve, expected):
    if resolve is None:
        raise ValueError('Equipment description needs its linked asset')
    file_id, path_id = struct.unpack_from('<iq', data, offset)
    if not path_id:
        raise ValueError('Missing linked equipment description asset')
    kind, name, raw = resolve(file_id, path_id)
    if kind != expected or not name:
        raise ValueError('Unexpected linked equipment description class')
    return name, raw
