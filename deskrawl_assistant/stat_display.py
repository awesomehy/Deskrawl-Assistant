"""Presentation of verified copied values; never changes rule observations.

Supported-build native evidence:
EquipmentTooltipAttributeUI.hsd/hsb: percentage StatType bit mask, x100,
one decimal; flat affixes use zero decimals. gj.dqn: upgrade distribution
over the ORIGINAL modifier array, including headline and secondary records.
"""
from decimal import Decimal, ROUND_HALF_UP, localcontext, InvalidOperation
import math
import struct


PERCENT_STAT_MASK = 0x2FFFB7F27FFCBA07


def is_percent_stat(stat):
    return isinstance(stat, int) and not isinstance(stat, bool) and (
        stat == 64 or 4 <= stat <= 65 and bool((PERCENT_STAT_MASK >> (stat - 4)) & 1))


def _single(value):
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]


def upgraded_value(value, index, count, level, rarity):
    """Mirror 1.0.2a gj.dqn (RVA 0x7855a0) without game function invocation."""
    if value is None:
        return None
    value = _single(value)
    if level <= 0 or count == 0:
        return value
    early = min(level, 10)
    quotient, remainder = divmod(early * (2 if rarity >= 2 else 1), count)
    boosts = quotient + int(index < remainder) + level - early
    factor = _single(_single(boosts * _single(0.05)) + _single(1))
    return _single(value * factor)


def preview_value(modifier, row):
    raw = modifier.value
    if raw is None:
        return {'raw_value': None, 'value': None, 'display_value': '未读取', 'unit': None, 'value_note': ''}
    percent = is_percent_stat(modifier.stat_value) if modifier.confidence == 1 else False
    level, rarity = row.get('upgrade_level', 0), row.get('rarity', 0)
    verified_upgrade = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (level, rarity))
    modifiers = row.get('modifiers', [])
    note = ''
    try:
        value = upgraded_value(raw, modifier.record_index, len(modifiers), level, rarity) if verified_upgrade else _single(raw)
        if not math.isfinite(value):
            raise ValueError('Nonfinite display value')
        displayed = _single(value * 100) if percent else value
        if not math.isfinite(displayed):
            raise ValueError('Nonfinite percentage')
        # WeaponSpeed is a headline value, not a percentage affix.
        places = 1 if percent else 2 if modifier.stat_value == 21 else 0
        if modifier.confidence != 1:
            places = 3
            note = '原始读取值，单位未核验'
        elif not verified_upgrade:
            note = '原始读取值，强化等级未核验'
        with localcontext() as context:
            context.prec = 60
            text = format(Decimal(str(displayed)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), 'f')
        if '.' in text:
            text = text.rstrip('0').rstrip('.')
        if text == '-0':
            text = '0'
        return {'raw_value': str(raw), 'value': str(value), 'display_value': text + ('%' if percent else ''),
                'unit': 'percent' if percent else 'flat' if modifier.confidence == 1 else None, 'value_note': note}
    except (OverflowError, ValueError, TypeError, InvalidOperation):
        return {'raw_value': str(raw), 'value': None, 'display_value': '未读取', 'unit': None, 'value_note': '数值换算无法确认'}
