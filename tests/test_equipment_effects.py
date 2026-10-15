"""Fixed-scene equipment effect boundaries, without native calls or UnityPy."""
from copy import deepcopy
import unittest

from deskrawl_assistant.equipment_effects import apply_equipment_effect_changes, effect_catalog, effects_supported


def gear(name): return {'name_key':name}


def model():
    return {'caches':{536:{},544:{}},'conditional':({},{}),'conversions':[],
            'equipment_effects_verified':True}


class EquipmentEffectBoundaryTests(unittest.TestCase):
    def test_fixed_ring_bonus_is_not_multiplied_by_upgrade_level(self):
        value=model();row={**gear('LegendaryRing7'),'upgrade_level':100}
        apply_equipment_effect_changes(value,[],[row],2)
        self.assertAlmostEqual(.35,value['conditional'][0][6],places=6)

    def test_other_slot_keeps_shared_effect_and_never_adds_it_twice(self):
        value=model();value['conditional'][0][6]=.35
        original=deepcopy(value)
        apply_equipment_effect_changes(value,[gear('LegendaryRing7')],
            [gear('LegendaryRing7'),gear('LegendaryRing7')],2)
        self.assertEqual(original,value)
        apply_equipment_effect_changes(value,[gear('LegendaryRing7'),gear('LegendaryRing7')],
            [gear('LegendaryRing7')],2)
        self.assertEqual(original,value)

    def test_full_health_neck_effect_is_removed_from_only_its_own_condition_layer(self):
        value=model();value['conditional'][0][65]=1.25
        apply_equipment_effect_changes(value,[gear('LegendaryNeck5')],[],2)
        self.assertAlmostEqual(.5,value['conditional'][0][65],places=6)

    def test_permanent_penalty_and_bonus_are_removed_together(self):
        value=model();value['caches'][536]={48:5,42:-.15}
        apply_equipment_effect_changes(value,[gear('LegendaryChestArmor2')],[],2)
        self.assertAlmostEqual(0,value['caches'][536][48])
        self.assertAlmostEqual(0,value['caches'][536][42],places=6)

    def test_temporary_status_requirement_is_excluded_from_fixed_scene(self):
        value=model();original=deepcopy(value)
        apply_equipment_effect_changes(value,[],[gear('LegendaryBoots2')],2)
        self.assertEqual(original,value)

    def test_conversion_effects_are_added_and_removed_without_dropping_other_conversions(self):
        catalog=effect_catalog()
        key=next(k for k,rows in catalog['items'].items()
                 if any(e['class']=='EquipmentEffect_StatConversion' for e in rows))
        value=model();value['conversions']=[(1,3,.125)]
        apply_equipment_effect_changes(value,[],[gear(key)],2)
        self.assertGreater(len(value['conversions']),1)
        apply_equipment_effect_changes(value,[gear(key)],[],2)
        self.assertEqual([(1,3,.125)],value['conversions'])

    def test_missing_equipped_conversion_fails_closed(self):
        key=next(k for k,rows in effect_catalog()['items'].items()
                 if any(e['class']=='EquipmentEffect_StatConversion' for e in rows))
        with self.assertRaisesRegex(ValueError,'属性转换'):
            apply_equipment_effect_changes(model(),[gear(key)],[],2)

    def test_unknown_build_never_reuses_exact_build_equipment_effects(self):
        value=model();value['equipment_effects_verified']=False
        with self.assertRaisesRegex(ValueError,'构建'):
            apply_equipment_effect_changes(value,[],[gear('LegendaryRing7')],2)
        catalog=effect_catalog()
        self.assertTrue(effects_supported({'metadataSha256':catalog['metadata'],
                                          'gameAssemblySha256':catalog['assembly']}))
        self.assertFalse(effects_supported({'metadataSha256':catalog['metadata'],
                                           'gameAssemblySha256':'another-build'}))
