"""Offline regression cases for the native same-legendary-ring gate.

The 1.0.3 EquipmentManager.ddw gate refuses a legendary-or-higher ring when
its ItemData is already equipped in the other ring slot. No game is connected.
"""
import unittest

from deskrawl_assistant.character import preview
from deskrawl_assistant.managed_mode import equip_candidate_allowed
from deskrawl_assistant.equipment_requirements import equip_eligibility


def ring(name, uid, dex, slot=None, rarity=3):
    row = {"is_equipment": True, "class_mask": 15, "equip_slot": 7,
           "modifiers_complete": True, "modifiers": [{"stat": 1, "type": 0, "value": dex}],
           "item_level": 1, "base_rarity": rarity, "rarity": rarity, "is_black_mist": False,
           "upgrade_level": 0, "socket_count": 0, "socketed_gems": [],
           "name_key": name, "internal_name": name, "item_uid": uid}
    if slot is not None:
        row["equipped_slot"] = slot
    return row


def player(equipment):
    model = {"base": {0: 10, 1: 100, 2: 20, 3: 10.5, 4: .2, 5: 1.5},
             "caches": {536: {20: 100, 21: 1, 1: 21}, 544: {}},
             "status": ({}, {}, {}), "conditional": ({}, {}), "conversions": []}
    return {"available": True, "level": 100, "max_level": 100, "class_mask": 4,
            "primary": 1, "equipment": equipment, "comparison_model": model, "model": model}


class AutoEquipNativeRingGateTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"minimum_delta": 1, "slot_locks": {}, "disabled_slots": []}
        self.current = player([ring("LegendaryRing1", "left", 20, 6),
                               ring("LegendaryRing2", "right", 1, 7)])
        self.candidate = ring("LegendaryRing1", "candidate", 30)

    def test_planner_rejects_duplicate_legendary_in_other_ring_slot(self):
        score = {"target": 7, "delta": 42, "replaced_uid": "right"}
        self.assertFalse(equip_candidate_allowed(self.candidate, score, self.settings, self.current))

    def test_preview_selects_replacing_same_ring_instead_of_impossible_larger_gain(self):
        result = preview(self.candidate, self.current, {"items": {}})
        self.assertTrue(result["available"])
        self.assertEqual(result["target"], 6)
        self.assertNotIn(7, [score["target"] for score in result["comparisons"]])

    def test_replacing_the_existing_legendary_is_still_allowed(self):
        self.assertTrue(equip_candidate_allowed(self.candidate,
            {"target": 6, "delta": 14, "replaced_uid": "left"}, self.settings, self.current))

    def test_rare_ring_is_not_subject_to_legendary_unique_ring_gate(self):
        candidate = ring("RareRing1", "candidate", 30, rarity=2)
        self.current["equipment"][0] = ring("RareRing1", "left", 20, 6, rarity=2)
        self.assertTrue(equip_candidate_allowed(candidate,
            {"target": 7, "delta": 42, "replaced_uid": "right"}, self.settings, self.current))

    def test_unknown_duplicate_ring_identity_does_not_authorize_target(self):
        candidate = dict(self.candidate)
        candidate.pop('name_key'); candidate.pop('internal_name')
        result = equip_eligibility(candidate, self.current, target_slot=7)
        self.assertFalse(result['can_equip']); self.assertFalse(result['available'])
        self.assertIn('戒指',result['reason'])

    def test_unrevealed_or_unknown_black_mist_is_never_scored_or_planned(self):
        for finalized in (False, None):
            with self.subTest(finalized=finalized):
                row = dict(self.candidate, is_black_mist=True, black_mist_finalized=finalized)
                score = {'target':6,'delta':14,'replaced_uid':'left'}
                self.assertFalse(equip_eligibility(row,self.current)['can_equip'])
                self.assertFalse(preview(row,self.current,{'items':{}})['available'])
                self.assertFalse(equip_candidate_allowed(row,score,self.settings,self.current))

    def test_finalized_black_mist_retains_normal_level_and_slot_eligibility(self):
        row = dict(self.candidate,is_black_mist=True,black_mist_finalized=True)
        self.assertTrue(equip_eligibility(row,self.current,target_slot=6)['can_equip'])
        self.assertFalse(equip_eligibility(row,self.current,target_slot=7)['can_equip'])
        self.assertFalse(equip_eligibility(row,dict(self.current,level=99),target_slot=6)['can_equip'])


if __name__ == "__main__":
    unittest.main()
