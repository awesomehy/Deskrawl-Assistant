"""Dynamic equipment descriptions use serialized values and shipped prose."""
import json
from pathlib import Path
import struct
import unittest

from tools.equipment_effect_text import render_equipment_effect
from tools.equipment_description_assets import damage_tokens, status_tokens
from deskrawl_assistant.native_memory import MemoryReadError

BASE = Path(__file__).resolve().parents[1]


class EquipmentEffectTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads((BASE/'data/game-catalog.json').read_text(encoding='utf-8'))
        cls.labels = {k:v for t in data['tables'].values() for k,v in t['entries'].items()}

    def text(self, key):
        row = self.labels.get(key, {})
        return row.get('Chinese (Simplified)') or row.get('English') or ''

    def test_potion_drop_description_uses_asset_chance_and_cooldown(self):
        value = render_equipment_effect('EquipmentEffect_PotionDrops', struct.pack('<ff', .1, 1), self.text)
        self.assertEqual(value, '击杀敌人时有 10% 几率掉落生命药水。该效果每 1 秒最多掉落一瓶药水。掉落的生命药水将被立即拾取。药水次数已满时，立即饮用拾取的药水。')
        changed = render_equipment_effect('EquipmentEffect_PotionDrops', struct.pack('<ff', .125, 2.5), self.text)
        self.assertIn('12.5%', changed)
        self.assertIn('2.5 秒', changed)

    def test_multipliers_become_increases_rather_than_120_and_140_percent(self):
        value = render_equipment_effect('EquipmentEffect_DivineHelmet3', struct.pack('<ff', 1.2, 1.4), self.text)
        self.assertEqual(value, '造成的伤害额外提高 20%，对精英和首领提高至 40%。')

    def test_serialized_description_key_is_used_for_trigger_effect(self):
        for kind,key,chance,prefix in (
                ('EquipmentEffect_BeingHitTriggerAbility','LegendaryNeck4_Retaliation.Desc',.3,struct.pack('<iq',2,59941)),
                ('EquipmentEffect_TriggerKillEffectsOnHit','LegendaryShoulder3_KillEffects.Desc',.1,b'')):
            raw = prefix + struct.pack('<f',chance) + (struct.pack('<f',.2) if kind == 'EquipmentEffect_TriggerKillEffectsOnHit' else b'') + struct.pack('<i',len(key)) + key.encode()
            raw += bytes(-len(raw) % 4)
            value = render_equipment_effect(kind, raw, self.text)
            self.assertEqual(value, self.text(key).replace('{chance}', '30%' if chance == .3 else '10%').replace('{maxTriggers}', '5'))

    def test_unverified_values_layouts_and_templates_are_rejected(self):
        for raw in (struct.pack('<ff',-1,1),struct.pack('<ff',.1,float('nan'))):
            with self.assertRaises(ValueError):
                render_equipment_effect('EquipmentEffect_PotionDrops', raw, self.text)
        with self.assertRaises(ValueError):
            render_equipment_effect('EquipmentEffect_PotionDrops', struct.pack('<ff',.1,1), lambda key:'{changed}')
        with self.assertRaises(ValueError):
            render_equipment_effect('EquipmentEffect_AllStats', bytes(24), self.text)
        with self.assertRaises(ValueError):
            render_equipment_effect('EquipmentEffect_TriggerKillEffectsOnHit',struct.pack('<fi',.1,100),self.text)
        self.assertEqual(render_equipment_effect('UnverifiedEffect',b'',self.text),'')

    def test_new_legendary_parameters_and_fractional_cooldown_are_resolved(self):
        cases = (
            ('HighestElementalDamageBonus', 'LegendaryNeck8_Elemental', struct.pack('<f', .25),
             '你最高的元素伤害加成提高25%。'),
            ('DamagePerNearbyEnemy', 'LegendaryShoulder6_Damage', struct.pack('<ffi', 3, .03, 5),
             '每个近处敌人使你造成的伤害额外提高3%。首领按5个敌人计算。'),
            ('IronThornFrenzy', 'LegendaryBelt4_Frenzy', struct.pack('<iqif', 2, 62335, 2, .2),
             '铁棘生效期间，每承受2次敌人的直接命中，就触发一次额外的铁棘脉冲，每0.2秒最多触发一次。'),
        )
        for kind, key, raw, expected in cases:
            with self.subTest(kind=kind):
                self.assertEqual(render_equipment_effect('EquipmentEffect_' + kind, raw,
                                                        self.text, effect_key=key), expected)
        changed = render_equipment_effect('EquipmentEffect_HighestElementalDamageBonus',
            struct.pack('<f', .375), self.text, effect_key='LegendaryNeck8_Elemental')
        self.assertIn('37.5%', changed)

    def test_linked_status_uses_duration_stack_count_and_damage(self):
        raw = bytes(12) + struct.pack('<iii', 1, 1, 20) + leveled(5) + token_spec('duration', '0.#', 's')
        calls = []
        def resolve(file_id, path_id):
            calls.append((file_id, path_id))
            return 'StatusEffectData', 'LegendaryGloves8', raw
        value = render_equipment_effect('EquipmentEffect_DamagePerStatusStack',
            struct.pack('<iqf', 0, 5433, .08), self.text,
            effect_key='LegendaryGloves8_Damage', resolve=resolve)
        self.assertEqual(value, '施放冲锋时获得一层增益，持续5s。每层使造成的伤害额外提高8%，最多20层。')
        self.assertEqual(calls, [(0, 5433)])
        with self.assertRaisesRegex(ValueError, 'linked asset'):
            render_equipment_effect('EquipmentEffect_DamagePerStatusStack',
                struct.pack('<iqf', 0, 5433, .08), self.text, effect_key='LegendaryGloves8_Damage')

    def test_damage_managed_records_require_unique_header_and_verified_values(self):
        header = serialized('DamageEffect') + serialized('') + serialized('Assembly-CSharp')
        raw = header + bytes(40) + struct.pack('<q', 0) + leveled(2.75)
        raw += struct.pack('<iiiif', 1, 1, 0, 5, 0) + token_spec('damage', '0.#%')
        self.assertEqual(damage_tokens(raw), {'damage': '275%'})
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            damage_tokens(raw + header)
        with self.assertRaisesRegex(ValueError, 'damage description'):
            damage_tokens(raw.replace(leveled(2.75), leveled(2.75, .1)))
        broken = bytearray(bytes(12) + struct.pack('<iii', 1, 1, 20) + leveled(5) + token_spec('duration', '0.#'))
        broken[24] ^= 2
        with self.assertRaises(MemoryReadError):
            status_tokens(bytes(broken))

    def test_all_shipped_equipment_texts_are_resolved_and_new_items_are_complete(self):
        items = json.loads((BASE/'data/loot-item-ui.json').read_text(encoding='utf-8'))['items']
        for key, row in items.items():
            if row['kind'] != 'equipment':
                continue
            for text in [row['description']] + [t for g in row['effect_groups'] for t in g['texts']]:
                self.assertNotRegex(text, r'\{[^{}]+\}', key)
        self.assertEqual(items['LegendaryNeck8']['description'], '你最高的元素伤害加成提高25%。')
        self.assertIn('首领按5个敌人', items['LegendaryShoulder6']['description'])
        for key in ('LeaderboardReward1', 'LeaderboardReward2', 'LegendaryMinionGear1',
                    'RareMinionGear1', 'RareMinionGear2', 'RareMinionGear3'):
            self.assertFalse(items[key]['source_effect_ids'])
            self.assertIn('没有固定特殊效果', items[key]['effect_note'])


def serialized(value):
    raw = value.encode('utf-8')
    raw = struct.pack('<i', len(raw)) + raw
    return raw + bytes(-len(raw) % 4)


def token_spec(name, fmt, suffix=''):
    return serialized(name) + serialized(fmt) + serialized(suffix) + struct.pack('<ii', 1, 0)


def leveled(base, growth=0):
    def protected(value):
        raw = struct.pack('<f', value)
        checksum = 0x811C9DC6
        for byte in raw:
            checksum = ((checksum ^ byte) * 0x1000192) & 0xFFFFFFFF
        hidden = bytearray(raw)
        hidden[1], hidden[2] = hidden[2], hidden[1]
        return struct.pack('<IIIfI', checksum | 1, int.from_bytes(hidden, 'little'), 0, 0, 0) + bytes(8)
    return protected(base) + protected(growth)


if __name__ == '__main__':
    unittest.main()
