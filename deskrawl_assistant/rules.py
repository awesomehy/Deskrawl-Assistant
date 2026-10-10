"""Conservative matching and lock proposals; never access the game or send keys."""
from __future__ import annotations

import json
import operator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from .catalog import Catalog
from .models import (Condition, CountGroup, Evaluation, ItemObservation, LockDecision, Rule,
                     ValidationError, confidence_value)


_COMPARATORS = {">=": operator.ge, "<=": operator.le, ">": operator.gt,
                "<": operator.lt, "==": operator.eq}
DEFAULT_MIN_CONFIDENCE = Decimal("0.98")


def rules_from_dict(data: Any, catalog: Catalog) -> list[Rule]:
    version = None
    if isinstance(data, Mapping):
        version = data.get("schema_version", data.get("version"))
        if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
            raise ValidationError("规则文件版本必须是 1 或 2")
        if "schema_version" in data and "version" in data and data["schema_version"] != data["version"]:
            raise ValidationError("规则版本字段不一致")
        unknown = set(data) - {"schema_version", "version", "rules"}
        if unknown:
            raise ValidationError(f"规则文件包含未知字段：{', '.join(sorted(unknown))}")
        data = data.get("rules")
    if not isinstance(data, list):
        raise ValidationError("规则文件必须包含 rules 列表")
    rules = [Rule.from_dict(row, catalog) for row in data]
    if version is not None and any(rule.schema_version != version for rule in rules):
        raise ValidationError("规则内容与文件版本不一致：v1 使用 conditions，v2 使用 groups")
    ids = [rule.id for rule in rules]
    if len(ids) != len(set(ids)):
        raise ValidationError("规则 ID 不能重复")
    return rules


def load_rules(path: str | Path, catalog: Catalog) -> list[Rule]:
    with Path(path).open(encoding="utf-8-sig") as source:
        return rules_from_dict(json.load(source, parse_float=Decimal), catalog)


def observations_from_dict(data: Any) -> list[ItemObservation]:
    if isinstance(data, Mapping):
        version = data.get("schema_version", data.get("version"))
        if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
            raise ValidationError("观测文件版本必须是 1 或 2")
        if "schema_version" in data and "version" in data and data["schema_version"] != data["version"]:
            raise ValidationError("观测版本字段不一致")
        unknown = set(data) - {"schema_version", "version", "note", "items"}
        if unknown:
            raise ValidationError(f"观测文件包含未知字段：{', '.join(sorted(unknown))}")
        data = data.get("items")
    if not isinstance(data, list):
        raise ValidationError("观测文件必须包含 items 列表")
    return [ItemObservation.from_dict(row) for row in data]


def load_observations(path: str | Path) -> list[ItemObservation]:
    with Path(path).open(encoding="utf-8-sig") as source:
        return observations_from_dict(json.load(source, parse_float=Decimal))


def _equipment_key(item: ItemObservation, catalog: Catalog) -> tuple[str | None, str | None]:
    key = item.equipment_key
    if key is not None and key not in catalog.equipment:
        return None, "装备标识不在本地词典中，无法确认装备身份"
    name_key = catalog.resolve_equipment(item.name) if item.name else None
    if item.name and name_key is None:
        return None, "装备名称未精确识别或对应多个装备"
    if key is not None and name_key is not None and key != name_key:
        return None, "装备名称与装备标识冲突"
    if key is None:
        key = name_key
    if key is None:
        return None, "缺少可确认的装备名称或标识"
    return key, None


def _evaluate_condition(condition: Condition, item: ItemObservation, catalog: Catalog,
                        minimum: Decimal) -> tuple[str, str]:
    label = catalog.stats[condition.stat_key].label
    values = item.affixes.get(condition.stat_key, ())
    if not values:
        if item.tooltip_complete:
            return "miss", f"未出现词条：{label}"
        return "uncertain", f"提示信息不完整，无法确认是否包含{label}"
    if condition.operator == "exists":
        reliable = [v for v in values if v.confidence >= minimum]
        if not reliable:
            return "uncertain", f"{label}识别置信度不足"
        if condition.unit is not None and not any(v.unit == condition.unit for v in reliable):
            return "uncertain", f"{label}的单位与规则不一致或未识别"
        return "match", f"包含词条：{label}"
    # A numeric condition applies to one affix. Base stats, gems and duplicates
    # cannot be silently added together or reduced to whichever value passes.
    if len(values) != 1:
        return "uncertain", f"{label}有多条读数，无法确认来源；不会相加或择高"
    value = values[0]
    if value.confidence < minimum:
        return "uncertain", f"{label}识别置信度不足"
    if value.unit != condition.unit:
        return "uncertain", f"{label}的单位与规则不一致或未识别"
    if value.value is None:
        return "uncertain", f"{label}的数值未识别"
    suffix = "%" if condition.unit == "percent" else ""
    success = _COMPARATORS[condition.operator](value.value, condition.value)
    return ("match" if success else "miss",
            f"{label} {value.value}{suffix} {'满足' if success else '不满足'} {condition.operator} {condition.value}{suffix}")


def _evaluate_group(name: str, group: CountGroup, item: ItemObservation,
                    catalog: Catalog, minimum: Decimal) -> tuple[str, str]:
    label = "主词条" if name == "primary" else "副词条"
    hit_types: set[str] = set()
    uncertain_types: set[str] = set()
    for key in group.selected_stats:
        for value in item.affixes.get(key, ()):
            if value.confidence < minimum or value.group not in {"primary", "secondary"} or value.group_confidence < minimum:
                uncertain_types.add(key)
            elif value.group == name:
                hit_types.add(key)
    count = len(hit_types)
    threshold = f"{'超过' if group.operator == '>' else '至少'} {group.count} 条"
    detail = f"{label}：命中已勾选的 {count}/{len(group.selected_stats)} 种词条，要求{threshold}"
    hits = "、".join(catalog.stats[key].label for key in group.selected_stats if key in hit_types)
    detail += f"；命中词条：{hits or '无'}"
    if not item.groups_complete.get(name, False):
        return "uncertain", detail + "；该组分类或读取尚未完整，无法确认"
    if uncertain_types:
        unknown = "、".join(catalog.stats[key].label for key in group.selected_stats if key in uncertain_types)
        return "uncertain", detail + f"；{unknown}的类型或主副分组证据不足"
    success = count > group.count if group.operator == ">" else count >= group.count
    return "match" if success else "miss", detail + ("；满足数量条件" if success else "；不满足数量条件")


def evaluate(rule: Rule, item: ItemObservation, catalog: Catalog,
             min_confidence: Any = DEFAULT_MIN_CONFIDENCE) -> Evaluation:
    minimum = confidence_value(min_confidence, "最低置信度")
    if not rule.enabled:
        return Evaluation("miss", rule.id, ("规则未启用",))
    # Validate direct constructors too. A malformed Rule must not weaken matching.
    Rule.from_dict(rule.to_dict(), catalog)
    key, error = _equipment_key(item, catalog)
    if error:
        return Evaluation("uncertain", rule.id, (error,))
    if item.name_confidence < minimum:
        return Evaluation("uncertain", rule.id, ("装备名称识别置信度不足",))
    if key not in rule.equipment_keys:
        return Evaluation("miss", rule.id, ("装备名称不在此规则指定的装备中",))
    if rule.is_count_rule:
        primary = _evaluate_group("primary", rule.groups["primary"], item, catalog, minimum)
        if primary[0] != "match":
            return Evaluation(primary[0], rule.id, (primary[1],))
        if "secondary" not in rule.groups:
            return Evaluation("match", rule.id, (primary[1], "副词条筛选已关闭，仅检查主词条"))
        secondary = _evaluate_group("secondary", rule.groups["secondary"], item, catalog, minimum)
        return Evaluation(secondary[0], rule.id, (primary[1], secondary[1]))
    else:
        results = [_evaluate_condition(c, item, catalog, minimum) for c in rule.conditions]
        mode = rule.mode
    statuses = [status for status, _ in results]
    if mode == "all":
        status = "miss" if "miss" in statuses else "uncertain" if "uncertain" in statuses else "match"
    else:
        status = "match" if "match" in statuses else "uncertain" if "uncertain" in statuses else "miss"
    return Evaluation(status, rule.id, tuple(reason for _, reason in results))


def primary_perfect_rules(item: ItemObservation, rules: Iterable[Rule], catalog: Catalog) -> list[Rule]:
    """A display-only mark for four random primaries within one enabled pool."""
    if not item.groups_complete.get('primary') or item.name_confidence < DEFAULT_MIN_CONFIDENCE:
        return []
    primaries = [(key, value) for key, values in item.affixes.items() for value in values if value.group == 'primary']
    if len(primaries) != 4 or any(value.confidence < DEFAULT_MIN_CONFIDENCE or
            value.group_confidence < DEFAULT_MIN_CONFIDENCE for _, value in primaries):
        return []
    matches = []
    for rule in rules:
        if not rule.enabled or not rule.is_count_rule:
            continue
        if all(key in rule.groups['primary'].selected_stats for key, _ in primaries):
            primary_only = replace(rule, groups={'primary':rule.groups['primary']})
            if evaluate(primary_only, item, catalog).status == 'match':
                matches.append(rule)
    return matches


class LockPlanner:
    """Make repeatable proposals; track only real action attempts in this session.

    Call mark_attempt after starting a real input operation, then confirm_locked
    after observing the lock marker. An uncertain/failed input stays blocked:
    sending L again could reverse an action the game actually received.
    A preview calls plan only, which does not update any state.
    """

    def __init__(self, catalog: Catalog, min_confidence: Any = DEFAULT_MIN_CONFIDENCE):
        self.catalog = catalog
        self.min_confidence = confidence_value(min_confidence, "最低置信度")
        self._actions: dict[tuple[str, str], str] = {}

    @staticmethod
    def _identity(item_id: str | None, token: str) -> tuple[str, str]:
        return ("item", item_id) if item_id else ("token", token)

    def plan(self, item: ItemObservation, rules: Iterable[Rule]) -> LockDecision:
        def decision(action, reasons, matched=()):
            return LockDecision(action, tuple(reasons), tuple(matched), item.observed_token, item.item_id)

        if item.lock_state == "locked":
            return decision("skip", ["装备已锁定，无需按 L"])
        if item.lock_state != "unlocked":
            return decision("review", ["锁定状态未知；L 会切换状态，暂不操作"])
        if item.lock_confidence < self.min_confidence:
            return decision("review", ["未锁定标记识别置信度不足，暂不操作"])
        if not item.observed_token:
            return decision("review", ["缺少当前观测标识，无法绑定到本次装备提示"])
        state = self._actions.get(self._identity(item.item_id, item.observed_token))
        if state is not None:
            reason = "本会话已确认此装备锁定，不重复按 L" if state == "confirmed" else "此装备已有按键尝试，结果需核验；不会重复切换"
            return decision("skip", [reason])
        rules = list(rules)
        evaluations = [evaluate(rule, item, self.catalog, self.min_confidence) for rule in rules]
        matches = [entry for entry in evaluations if entry.status == "match"]
        if not matches:
            uncertain = [entry for entry in evaluations if entry.status == "uncertain"]
            reasons = [reason for entry in (uncertain or evaluations) for reason in entry.reasons]
            return decision("review" if uncertain else "skip", reasons or ["没有启用且匹配的规则"])
        matched_ids = [entry.rule_id for entry in matches]
        count_ids = {rule.id for rule in rules if rule.is_count_rule}
        # A v2 match has already verified each enabled group's complete flag,
        # selected types and classification evidence. Unselected types and an
        # unread/disabled secondary group are not additional conditions.
        eligible = [entry for entry in matches if entry.rule_id in count_ids]
        legacy = [entry for entry in matches if entry.rule_id not in count_ids]
        legacy_error = None
        if legacy:
            if not item.tooltip_complete:
                legacy_error = "装备提示信息不完整，旧版规则暂不自动锁定"
            else:
                for key, values in item.affixes.items():
                    if key not in self.catalog.stats:
                        legacy_error = f"包含未知词条 {key}，旧版规则需要核验完整提示"
                        break
                    if not values or any(value.confidence < self.min_confidence for value in values):
                        legacy_error = "装备提示中仍有低置信度词条，旧版规则暂不操作"
                        break
            if legacy_error is None:
                eligible.extend(legacy)
        if not eligible:
            return decision("review", [legacy_error or "规则所需的装备信息尚未可靠读取"], matched_ids)
        matched_ids = [entry.rule_id for entry in eligible]
        reasons = [f"匹配规则：{entry.rule_id}" for entry in eligible]
        reasons.extend(reason for entry in eligible for reason in entry.reasons)
        reasons.append("装备确认未锁定；可向当前目标发送一次 L")
        return decision("press_l", reasons, matched_ids)

    def mark_attempt(self, decision: LockDecision) -> None:
        if not decision.should_lock or not decision.observed_token:
            raise ValidationError("只能登记绑定观测标识的真实锁定动作")
        identity = self._identity(decision.item_id, decision.observed_token)
        if identity in self._actions:
            raise ValidationError("同一装备不能登记重复锁定动作")
        self._actions[identity] = "attempted"

    def confirm_locked(self, decision: LockDecision) -> None:
        identity = self._identity(decision.item_id, decision.observed_token)
        if identity not in self._actions:
            raise ValidationError("只能确认已经登记的锁定动作")
        self._actions[identity] = "confirmed"

    def mark_failed(self, decision: LockDecision) -> None:
        identity = self._identity(decision.item_id, decision.observed_token)
        if identity not in self._actions:
            raise ValidationError("只能标记已经登记的锁定动作")
        self._actions[identity] = "failed"
