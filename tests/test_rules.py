"""Safety-focused checks for item identity, units, uncertainty and toggle locking."""
from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from deskrawl_assistant.catalog import Catalog, CatalogEntry, load_catalog
from deskrawl_assistant.models import CountGroup, ItemObservation, Rule, ValidationError
from deskrawl_assistant.rules import (LockPlanner, evaluate, load_observations,
                                     load_rules, rules_from_dict)


ROOT = Path(__file__).resolve().parents[1]


class RuleSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog(ROOT / "data" / "game-catalog.json")

    def rule_data(self, *, operator=">=", value="10.5", unit="percent", mode="all"):
        condition = {"stat_key": "Stats.CritChance", "operator": operator}
        if operator != "exists":
            condition.update(value=value, unit=unit)
        return {"id": "crit-ring", "name": "暴击戒指", "enabled": True,
                "equipment_keys": ["LegendaryRing1"], "mode": mode, "conditions": [condition]}

    def item_data(self):
        return {"equipment_key": "LegendaryRing1", "name": "巨龙血牙印戒",
                "slot": "inventory:1", "item_id": "item-1", "observed_token": "capture-1",
                "tooltip_complete": True, "name_confidence": "0.999",
                "lock_state": "unlocked", "lock_confidence": "0.999",
                "affixes": {"Stats.CritChance": [{"value": "10.5", "unit": "percent", "confidence": "0.999"}]}}

    def evaluate_data(self, rule_data=None, item_data=None):
        return evaluate(Rule.from_dict(rule_data or self.rule_data(), self.catalog),
                        ItemObservation.from_dict(item_data or self.item_data()), self.catalog)

    def test_localization_keys_and_aliases_are_exact(self):
        self.assertEqual(209, len(self.catalog.equipment))
        self.assertEqual(68, len(self.catalog.stats))
        self.assertEqual("LegendaryRing1", self.catalog.resolve_equipment("巨龙血牙印戒"))
        self.assertEqual("Stats.CritChance", self.catalog.resolve_stat("Critical Hit Chance"))
        self.assertIsNone(self.catalog.resolve_equipment("巨龙血牙"))
        aliases = Catalog({"A": CatalogEntry("A", "Shared", "同名"), "B": CatalogEntry("B", "Shared", "同名")}, {})
        self.assertIsNone(aliases.resolve_equipment("同名"))

    def test_decimal_threshold_boundaries_without_float_rounding(self):
        rule = self.rule_data(value="0.10000000000000000001")
        item = self.item_data()
        item["affixes"]["Stats.CritChance"][0]["value"] = "0.10000000000000000000"
        self.assertEqual("miss", self.evaluate_data(rule, item).status)
        item["affixes"]["Stats.CritChance"][0]["value"] = "0.10000000000000000001"
        self.assertEqual("match", self.evaluate_data(rule, item).status)
        self.assertEqual("miss", self.evaluate_data(self.rule_data(operator=">", value="10.5")).status)
        self.assertEqual("match", self.evaluate_data(self.rule_data(operator="<=", value="10.5")).status)
        self.assertEqual("match", self.evaluate_data(self.rule_data(operator="<", value="10.6")).status)
        self.assertEqual("match", self.evaluate_data(self.rule_data(operator="==", value="10.5")).status)

    def test_unknown_equipment_never_becomes_a_match(self):
        item = self.item_data()
        item["equipment_key"] = "FutureRing"
        self.assertEqual("uncertain", self.evaluate_data(item_data=item).status)
        item = self.item_data()
        item["name"] = "苦痛锯齿巨剑"
        self.assertEqual("uncertain", self.evaluate_data(item_data=item).status)
        item = self.item_data()
        item.update(equipment_key="LegendarySword1", name="苦痛锯齿巨剑")
        self.assertEqual("miss", self.evaluate_data(item_data=item).status)

    def test_exact_name_can_resolve_without_a_runtime_enum(self):
        item = self.item_data()
        item["equipment_key"] = None
        self.assertEqual("match", self.evaluate_data(item_data=item).status)

    def test_flat_and_percent_are_never_interchanged(self):
        item = self.item_data()
        for unit in ("flat", None):
            item["affixes"]["Stats.CritChance"][0]["unit"] = unit
            self.assertEqual("uncertain", self.evaluate_data(item_data=item).status)
        with self.assertRaises(ValidationError):
            Rule.from_dict(self.rule_data(unit=None), self.catalog)

    def test_repeated_affixes_are_not_summed_or_selected(self):
        item = self.item_data()
        item["affixes"]["Stats.CritChance"] = [
            {"value": "4", "unit": "percent", "confidence": "1"},
            {"value": "12", "unit": "percent", "confidence": "1"}]
        self.assertEqual("uncertain", self.evaluate_data(item_data=item).status)
        self.assertEqual("match", self.evaluate_data(self.rule_data(operator="exists"), item).status)

    def test_partial_tooltip_can_never_generate_a_lock_action(self):
        item = self.item_data()
        item["tooltip_complete"] = False
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        observation = ItemObservation.from_dict(item)
        # A visible positive affix is meaningful evidence; the lock gate still
        # requires a complete tooltip independently of matching.
        self.assertEqual("match", evaluate(rule, observation, self.catalog).status)
        self.assertFalse(LockPlanner(self.catalog).plan(observation, [rule]).should_lock)
        item["affixes"] = {}
        self.assertEqual("uncertain", self.evaluate_data(item_data=item).status)
        item["tooltip_complete"] = True
        self.assertEqual("miss", self.evaluate_data(item_data=item).status)

    def test_all_any_keep_missing_and_uncertain_distinct(self):
        item = self.item_data()
        item["tooltip_complete"] = False
        rule = self.rule_data()
        rule["conditions"].append({"stat_key": "Stats.Strength", "operator": "exists"})
        self.assertEqual("uncertain", self.evaluate_data(rule, item).status)
        rule["mode"] = "any"
        self.assertEqual("match", self.evaluate_data(rule, item).status)
        item["affixes"]["Stats.CritChance"][0]["value"] = "2"
        self.assertEqual("uncertain", self.evaluate_data(rule, item).status)
        rule["mode"] = "all"
        self.assertEqual("miss", self.evaluate_data(rule, item).status)

    def test_low_confidence_cannot_trigger_lock(self):
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        for field in ("name_confidence", "lock_confidence"):
            item = self.item_data()
            item[field] = "0.97"
            self.assertFalse(LockPlanner(self.catalog).plan(ItemObservation.from_dict(item), [rule]).should_lock)
        item = self.item_data()
        item["affixes"]["Stats.CritChance"][0]["confidence"] = "0.97"
        self.assertEqual("uncertain", self.evaluate_data(item_data=item).status)
        item = self.item_data()
        item["affixes"]["Stats.Strength"] = [{"value": "1", "unit": "flat", "confidence": "0.1"}]
        self.assertFalse(LockPlanner(self.catalog).plan(ItemObservation.from_dict(item), [rule]).should_lock)

    def test_unknown_and_already_locked_are_never_toggled(self):
        planner = LockPlanner(self.catalog)
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        item = self.item_data()
        for state in ("unknown", "locked"):
            item["lock_state"] = state
            self.assertFalse(planner.plan(ItemObservation.from_dict(item), [rule]).should_lock)

    def test_missing_observation_token_prevents_action(self):
        item = self.item_data()
        item["observed_token"] = ""
        self.assertFalse(LockPlanner(self.catalog).plan(ItemObservation.from_dict(item), [Rule.from_dict(self.rule_data(), self.catalog)]).should_lock)

    def test_preview_does_not_reserve_or_deduplicate(self):
        planner = LockPlanner(self.catalog)
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        item = ItemObservation.from_dict(self.item_data())
        first, second = planner.plan(item, [rule]), planner.plan(item, [rule])
        self.assertTrue(first.should_lock)
        self.assertTrue(second.should_lock)
        planner.mark_attempt(first)
        self.assertFalse(planner.plan(item, [rule]).should_lock)
        planner.confirm_locked(first)
        newer = self.item_data()
        newer["observed_token"] = "capture-2"
        self.assertFalse(planner.plan(ItemObservation.from_dict(newer), [rule]).should_lock)

    def test_identical_equipment_instances_are_independent(self):
        planner = LockPlanner(self.catalog)
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        item = self.item_data()
        first = planner.plan(ItemObservation.from_dict(item), [rule])
        planner.mark_attempt(first)
        planner.confirm_locked(first)
        item.update(item_id="item-2", observed_token="capture-2")
        self.assertTrue(planner.plan(ItemObservation.from_dict(item), [rule]).should_lock)

    def test_failed_or_ambiguous_key_attempt_cannot_be_repeated(self):
        planner = LockPlanner(self.catalog)
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        item = ItemObservation.from_dict(self.item_data())
        action = planner.plan(item, [rule])
        planner.mark_attempt(action)
        planner.mark_failed(action)
        self.assertFalse(planner.plan(item, [rule]).should_lock)
        with self.assertRaises(ValidationError):
            planner.mark_attempt(action)

    def test_unknown_rule_fields_or_keys_fail_during_import(self):
        variants = []
        for key, value in (("equipment_keys", ["*"]), ("mode", "some"), ("enabled", "false")):
            row = self.rule_data()
            row[key] = value
            variants.append(row)
        row = self.rule_data()
        row["conditions"][0]["stat_key"] = "CritChance"
        variants.append(row)
        row = self.rule_data()
        row["conditons"] = row.pop("conditions")
        variants.append(row)
        row = self.rule_data()
        row["conditions"] = []
        variants.append(row)
        for row in variants:
            with self.subTest(row=row), self.assertRaises(ValidationError):
                rules_from_dict({"version": 1, "rules": [row]}, self.catalog)

    def test_invalid_numbers_and_confidence_are_rejected(self):
        for value in (True, None, "NaN", "Infinity", "10%", "1,000"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Rule.from_dict(self.rule_data(value=value), self.catalog)
        for value in ("-0.01", "1.01", True, "NaN"):
            row = self.item_data()
            row["name_confidence"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ItemObservation.from_dict(row)

    def test_duplicate_rule_ids_and_conflicting_versions_are_rejected(self):
        with self.assertRaises(ValidationError):
            rules_from_dict([self.rule_data(), self.rule_data()], self.catalog)
        with self.assertRaises(ValidationError):
            rules_from_dict({"schema_version": 1, "version": 2, "rules": []}, self.catalog)

    def test_examples_import_and_decimal_roundtrip(self):
        rules = load_rules(ROOT / "examples" / "lock-rules.json", self.catalog)
        observations = load_observations(ROOT / "examples" / "item-observations.json")
        self.assertTrue(rules)
        self.assertTrue(observations)
        self.assertTrue(all(not rule.enabled for rule in rules))
        item = ItemObservation.from_dict(json.loads(json.dumps(observations[0].to_dict())))
        self.assertEqual(observations[0], item)
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        self.assertEqual(Decimal("10.5"), Rule.from_dict(json.loads(json.dumps(rule.to_dict())), self.catalog).conditions[0].value)


class CountRuleSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog(ROOT / "data" / "game-catalog.json")

    def rule_data(self):
        return {"id": "group-ring", "name": "主副词条计数", "enabled": True,
                "equipment_keys": ["LegendaryRing1"], "group_mode": "all",
                "groups": {"primary": {"selected_stats": ["Stats.Strength", "Stats.Dexterity", "Stats.Intelligence"], "operator": ">", "count": 1},
                           "secondary": {"selected_stats": ["Stats.CritChance", "Stats.CritDamage"], "operator": ">", "count": 0}}}

    def affix(self, group, **changes):
        result = {"confidence": "1", "group": group, "group_confidence": "1"}
        result.update(changes)
        return result

    def item_data(self):
        return {"equipment_key": "LegendaryRing1", "name_confidence": "1", "item_id": "count-1", "observed_token": "count-frame-1",
                "lock_state": "unlocked", "lock_confidence": "1", "tooltip_complete": True,
                "groups_complete": {"primary": True, "secondary": True},
                "affixes": {"Stats.Strength": [self.affix("primary")], "Stats.Dexterity": [self.affix("primary")],
                            "Stats.CritChance": [self.affix("secondary")]}}

    def evaluate_data(self, rule=None, item=None):
        return evaluate(Rule.from_dict(rule or self.rule_data(), self.catalog),
                        ItemObservation.from_dict(item or self.item_data()), self.catalog)

    def test_counting_selected_types_ignores_their_numeric_values(self):
        item = self.item_data()
        item["affixes"]["Stats.Strength"][0].update(value="0.00001", unit="percent")
        item["affixes"]["Stats.Dexterity"][0].update(value="999999", unit="flat")
        result = self.evaluate_data(item=item)
        self.assertEqual("match", result.status)
        self.assertIn("2/3", result.reasons[0])
        self.assertIn("1/2", result.reasons[1])
        self.assertTrue(LockPlanner(self.catalog).plan(ItemObservation.from_dict(item), [Rule.from_dict(self.rule_data(), self.catalog)]).should_lock)

    def test_literal_more_than_and_at_least_have_different_boundary(self):
        item = self.item_data()
        del item["affixes"]["Stats.Dexterity"]
        rule = self.rule_data()
        self.assertEqual("miss", self.evaluate_data(rule, item).status)
        rule["groups"]["primary"]["operator"] = ">="
        self.assertEqual("match", self.evaluate_data(rule, item).status)

    def test_duplicate_lines_count_one_distinct_type(self):
        item = self.item_data()
        del item["affixes"]["Stats.Dexterity"]
        item["affixes"]["Stats.Strength"] = [self.affix("primary"), self.affix("primary")]
        result = self.evaluate_data(item=item)
        self.assertEqual("miss", result.status)
        self.assertIn("1/3", result.reasons[0])

    def test_same_type_can_count_once_in_each_explicit_group(self):
        rule = self.rule_data()
        for name in ("primary", "secondary"):
            rule["groups"][name] = {"selected_stats": ["Stats.Strength"], "operator": ">", "count": 0}
        item = self.item_data()
        item["affixes"]["Stats.Strength"] = [self.affix("primary"), self.affix("secondary")]
        self.assertEqual("match", self.evaluate_data(rule, item).status)

    def test_primary_and_secondary_are_not_interchangeable(self):
        item = self.item_data()
        item["affixes"]["Stats.CritChance"][0]["group"] = "primary"
        result = self.evaluate_data(item=item)
        self.assertEqual("miss", result.status)
        self.assertIn("0/2", result.reasons[1])

    def test_unit_flat_is_never_evidence_for_primary_group(self):
        item = self.item_data()
        item["affixes"]["Stats.Strength"][0].update(group="unknown", unit="flat", value="20")
        self.assertEqual("uncertain", self.evaluate_data(item=item).status)
        self.assertFalse(LockPlanner(self.catalog).plan(ItemObservation.from_dict(item), [Rule.from_dict(self.rule_data(), self.catalog)]).should_lock)

    def test_complete_tooltip_does_not_imply_complete_groups(self):
        item = self.item_data()
        item.pop("groups_complete")
        self.assertEqual("uncertain", self.evaluate_data(item=item).status)
        self.assertFalse(LockPlanner(self.catalog).plan(ItemObservation.from_dict(item), [Rule.from_dict(self.rule_data(), self.catalog)]).should_lock)

    def test_group_confidence_and_type_confidence_are_both_required(self):
        for field in ("confidence", "group_confidence"):
            item = self.item_data()
            item["affixes"]["Stats.Strength"][0][field] = "0.97"
            self.assertEqual("uncertain", self.evaluate_data(item=item).status)
        item = self.item_data()
        item["affixes"]["Stats.Strength"][0].pop("group_confidence")
        self.assertEqual("uncertain", self.evaluate_data(item=item).status)

    def test_groups_must_both_pass_and_any_is_rejected(self):
        item = self.item_data()
        del item["affixes"]["Stats.CritChance"]
        rule = self.rule_data()
        self.assertEqual("miss", self.evaluate_data(rule, item).status)
        rule["group_mode"] = "any"
        with self.assertRaises(ValidationError):
            Rule.from_dict(rule, self.catalog)

    def test_primary_miss_does_not_inspect_secondary(self):
        item = self.item_data()
        del item["affixes"]["Stats.Dexterity"]
        item["groups_complete"]["secondary"] = False
        result = self.evaluate_data(item=item)
        self.assertEqual("miss", result.status)
        self.assertEqual(1, len(result.reasons))
        self.assertIn("主词条", result.reasons[0])

    def test_primary_uncertain_precedes_secondary_miss(self):
        item = self.item_data()
        item["groups_complete"]["primary"] = False
        del item["affixes"]["Stats.CritChance"]
        result = self.evaluate_data(item=item)
        self.assertEqual("uncertain", result.status)
        self.assertEqual(1, len(result.reasons))

    def test_secondary_only_and_zero_at_least_are_rejected(self):
        rule = self.rule_data()
        del rule["groups"]["primary"]
        with self.assertRaises(ValidationError):
            Rule.from_dict(rule, self.catalog)
        with self.assertRaises(ValidationError):
            CountGroup.from_dict({"selected_stats": ["Stats.Strength"], "operator": ">=", "count": 0})
        default = CountGroup.from_dict({"selected_stats": ["Stats.Strength"]})
        self.assertEqual(">=", default.operator)
        self.assertEqual(1, default.count)

    def test_one_enabled_group_does_not_require_other_group_flag(self):
        rule = self.rule_data()
        del rule["groups"]["secondary"]
        item = self.item_data()
        item["groups_complete"] = {"primary": True}
        self.assertEqual("match", self.evaluate_data(rule, item).status)

    def test_unselected_type_does_not_count(self):
        item = self.item_data()
        del item["affixes"]["Stats.Dexterity"]
        item["affixes"]["Stats.Armor"] = [self.affix("primary")]
        self.assertEqual("miss", self.evaluate_data(item=item).status)

    def test_unknown_group_only_blocks_when_type_is_selected(self):
        item = self.item_data()
        item["affixes"]["Stats.Armor"] = [self.affix("unknown")]
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        observation = ItemObservation.from_dict(item)
        self.assertEqual("match", evaluate(rule, observation, self.catalog).status)
        self.assertTrue(LockPlanner(self.catalog).plan(observation, [rule]).should_lock)
        item["affixes"]["Stats.Intelligence"] = [self.affix("unknown")]
        observation = ItemObservation.from_dict(item)
        self.assertEqual("uncertain", evaluate(rule, observation, self.catalog).status)
        self.assertFalse(LockPlanner(self.catalog).plan(observation, [rule]).should_lock)

    def test_disabled_secondary_does_not_block_primary_only_lock(self):
        rule_data = self.rule_data()
        del rule_data["groups"]["secondary"]
        item = self.item_data()
        item["tooltip_complete"] = False
        item["groups_complete"]["secondary"] = False
        item["affixes"]["Stats.CritChance"] = [self.affix("unknown", confidence="0.1", group_confidence="0")]
        item["affixes"]["UnknownUnselectedStat"] = [self.affix("unknown", confidence="0")]
        rule = Rule.from_dict(rule_data, self.catalog)
        observation = ItemObservation.from_dict(item)
        self.assertTrue(LockPlanner(self.catalog).plan(observation, [rule]).should_lock)
        enabled_secondary = Rule.from_dict(self.rule_data(), self.catalog)
        self.assertEqual("review", LockPlanner(self.catalog).plan(observation, [enabled_secondary]).action)

    def test_unselected_low_confidence_and_missing_values_are_not_conditions(self):
        item = self.item_data()
        item["affixes"]["Stats.Armor"] = [self.affix("unknown", confidence="0.01", group_confidence="0")]
        for key in ("Stats.Strength", "Stats.Dexterity", "Stats.CritChance"):
            self.assertNotIn("value", item["affixes"][key][0])
        self.assertTrue(LockPlanner(self.catalog).plan(ItemObservation.from_dict(item), [Rule.from_dict(self.rule_data(), self.catalog)]).should_lock)

    def test_count_match_can_lock_even_when_matching_legacy_requires_more_data(self):
        item = self.item_data()
        item["tooltip_complete"] = False
        legacy_data = RuleSafetyTests().rule_data(operator="exists")
        legacy = Rule.from_dict(legacy_data, self.catalog)
        count = Rule.from_dict(self.rule_data(), self.catalog)
        decision = LockPlanner(self.catalog).plan(ItemObservation.from_dict(item), [legacy, count])
        self.assertTrue(decision.should_lock)
        self.assertEqual((count.id,), decision.matched_rule_ids)

    def test_impossible_negative_fractional_and_boolean_thresholds_rejected(self):
        for count, operator in ((3, ">"), (4, ">="), (-1, ">"), (1.5, ">"), (True, ">")):
            with self.subTest(count=count, operator=operator), self.assertRaises(ValidationError):
                CountGroup.from_dict({"selected_stats": ["Stats.Strength", "Stats.Dexterity", "Stats.Intelligence"], "operator": operator, "count": count})
        for stats in ([], ["Stats.Strength", "Stats.Strength"]):
            with self.assertRaises(ValidationError):
                CountGroup.from_dict({"selected_stats": stats, "count": 0})

    def test_rules_and_observations_roundtrip_with_explicit_group_evidence(self):
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        item = ItemObservation.from_dict(self.item_data())
        self.assertEqual(rule, Rule.from_dict(json.loads(json.dumps(rule.to_dict())), self.catalog))
        self.assertEqual(item, ItemObservation.from_dict(json.loads(json.dumps(item.to_dict()))))
        self.assertEqual(2, rule.schema_version)

    def test_version_contract_and_legacy_mixing_are_rejected(self):
        self.assertEqual(1, len(rules_from_dict({"version": 2, "rules": [self.rule_data()]}, self.catalog)))
        with self.assertRaises(ValidationError):
            rules_from_dict({"version": 1, "rules": [self.rule_data()]}, self.catalog)
        with self.assertRaises(ValidationError):
            rules_from_dict({"version": 2.0, "rules": [self.rule_data()]}, self.catalog)
        legacy = RuleSafetyTests().rule_data()
        with self.assertRaises(ValidationError):
            rules_from_dict({"version": 2, "rules": [legacy]}, self.catalog)
        mixed = self.rule_data()
        mixed["conditions"] = legacy["conditions"]
        with self.assertRaises(ValidationError):
            Rule.from_dict(mixed, self.catalog)

    def test_count_preview_and_distinct_instances_remain_independent(self):
        planner = LockPlanner(self.catalog)
        rule = Rule.from_dict(self.rule_data(), self.catalog)
        item = self.item_data()
        action = planner.plan(ItemObservation.from_dict(item), [rule])
        self.assertTrue(action.should_lock)
        self.assertTrue(planner.plan(ItemObservation.from_dict(item), [rule]).should_lock)
        planner.mark_attempt(action)
        self.assertFalse(planner.plan(ItemObservation.from_dict(item), [rule]).should_lock)
        item.update(item_id="count-2", observed_token="count-frame-2")
        self.assertTrue(planner.plan(ItemObservation.from_dict(item), [rule]).should_lock)


if __name__ == "__main__":
    unittest.main()
