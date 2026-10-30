"""Dynamic equipment descriptions use serialized values and shipped prose."""
import json
from pathlib import Path
import struct
import unittest

from tools.equipment_effect_text import render_equipment_effect

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


if __name__ == '__main__':
    unittest.main()
