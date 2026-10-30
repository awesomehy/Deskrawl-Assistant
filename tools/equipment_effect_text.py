"""Render verified serialized equipment effects with the game's own text.

The reviewed description methods use these localisation keys and the 0.# / 0.#%
formats. This is an export-time reader; it does not affect equipment scoring.
"""
import math
import re
import struct


def _number(value, percent=False):
    if not math.isfinite(value):
        raise ValueError('Nonfinite equipment effect parameter')
    value *= 100 if percent else 1
    return f'{value:.1f}'.rstrip('0').rstrip('.') + ('%' if percent else '')


def _string(data, offset):
    if len(data) < offset + 4:
        raise ValueError('Missing equipment effect description key')
    length = struct.unpack_from('<i', data, offset)[0]
    end = offset + 4 + length
    if not 1 <= length <= 256 or ((end + 3) & ~3) != len(data):
        raise ValueError('Invalid equipment effect description key length')
    if any(data[end:]):
        raise ValueError('Unexpected equipment effect string padding')
    return data[offset + 4:end].decode('utf-8')


def render_equipment_effect(kind, data, text):
    """Return resolved prose for supported effects, or '' for other classes.

    Layout mismatches and unresolved template tokens abort the export rather
    than publish an effect with guessed parameters.
    """
    if kind == 'EquipmentEffect_PotionDrops':
        chance, cooldown = struct.unpack('<ff', data)
        if not 0 <= chance <= 1 or cooldown < 0:
            raise ValueError('Invalid potion drop parameters')
        key = 'EquipmentEffect.PotionDrops'
        values = {'chance':_number(chance, True), 'cooldown':_number(cooldown)}
    elif kind == 'EquipmentEffect_AllStats':
        # Inherited StatModifier array, required status and condition precede
        # Bonus. This asset has no extra modifiers/status/condition.
        if len(data) != 24 or data[:20] != bytes(16) + b'\xff' * 4:
            raise ValueError('Unverified AllStats parent layout')
        bonus = struct.unpack_from('<f', data, 20)[0]
        key = 'EquipmentEffect.AllStats'
        values = {'0':_number(bonus * 100)}
    elif kind == 'EquipmentEffect_DivineHelmet3':
        normal, elite = struct.unpack('<ff', data)
        key = 'EquipmentEffect.DivineHelmet3'
        values = {'damage':_number(normal - 1, True), 'eliteDamage':_number(elite - 1, True)}
    elif kind == 'EquipmentEffect_AlwaysHealthConditionalDamage':
        if data:
            raise ValueError('Unexpected health-conditional effect parameters')
        key = 'EquipmentEffect.AlwaysHealthConditionalDamage'
        values = {}
    elif kind in ('EquipmentEffect_BeingHitTriggerAbility', 'EquipmentEffect_TriggerKillEffectsOnHit'):
        offset = 12 if kind == 'EquipmentEffect_BeingHitTriggerAbility' else 0
        if len(data) < offset + 4:
            raise ValueError('Missing equipment trigger chance')
        chance = struct.unpack_from('<f', data, offset)[0]
        if not 0 <= chance <= 1:
            raise ValueError('Invalid equipment trigger chance')
        if kind == 'EquipmentEffect_TriggerKillEffectsOnHit':
            cooldown = struct.unpack_from('<f', data, 4)[0]
            if not math.isfinite(cooldown) or cooldown < 0:
                raise ValueError('Invalid kill trigger cooldown')
        key = _string(data, offset + (8 if kind == 'EquipmentEffect_TriggerKillEffectsOnHit' else 4))
        values = {'chance':_number(chance, True)}
        if kind == 'EquipmentEffect_TriggerKillEffectsOnHit':
            if cooldown <= 0:raise ValueError('Invalid kill trigger rate')
            values['maxTriggers'] = str(round(1 / cooldown))
    else:
        return ''
    template = text(key)
    if not template:
        raise ValueError('Missing equipment effect localisation: ' + key)
    tokens = set(re.findall(r'\{([^{}]+)\}', template))
    if tokens != set(values):
        raise ValueError('Unverified equipment effect template: ' + key)
    return re.sub(r'\{([^{}]+)\}', lambda m:values[m[1]], template)
