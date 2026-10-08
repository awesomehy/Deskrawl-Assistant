"""Validated data shared by the rule editor and the future game adapter.

Percent values are display values: 12.5 percent is stored as Decimal('12.5'),
never 0.125. No model guesses a unit, equipment enum or missing OCR confidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


class ValidationError(ValueError):
    """A configuration or observation cannot be used safely."""


def decimal_value(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{label}必须是有限数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError(f"{label}必须是有限数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{label}不能是 NaN 或无穷大")
    return result


def confidence_value(value: Any, label: str) -> Decimal:
    result = decimal_value(value, label)
    if not Decimal(0) <= result <= Decimal(1):
        raise ValidationError(f"{label}必须在 0 到 1 之间")
    return result


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValidationError(f"{label}必须是{'字符串' if empty else '非空字符串'}")
    return value.strip()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label}必须是对象")
    return value


def _known_fields(data: Mapping[str, Any], fields: set[str], label: str) -> None:
    unknown = set(data) - fields
    if unknown:
        raise ValidationError(f"{label}包含未知字段：{', '.join(sorted(unknown))}")


@dataclass(frozen=True)
class Condition:
    stat_key: str
    operator: str = "exists"
    value: Decimal | None = None
    unit: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Condition":
        data = _mapping(data, "词条条件")
        _known_fields(data, {"stat_key", "operator", "value", "unit"}, "词条条件")
        key = _text(data.get("stat_key"), "词条标识")
        operator = data.get("operator", "exists")
        if not isinstance(operator, str) or operator not in {"exists", ">=", "<=", ">", "<", "=="}:
            raise ValidationError(f"不支持的比较方式：{operator}")
        unit = data.get("unit")
        if unit is not None and (not isinstance(unit, str) or unit not in {"flat", "percent"}):
            raise ValidationError("单位只能是 flat（数值）或 percent（百分比）")
        if operator == "exists":
            if data.get("value") is not None:
                raise ValidationError("存在条件不能设置数值阈值")
            value = None
        else:
            if unit is None:
                raise ValidationError("数值比较必须明确单位，不能推断百分比")
            value = decimal_value(data.get("value"), "词条阈值")
        return cls(key, operator, value, unit)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"stat_key": self.stat_key, "operator": self.operator}
        if self.value is not None:
            result["value"] = str(self.value)
        if self.unit is not None:
            result["unit"] = self.unit
        return result


@dataclass(frozen=True)
class CountGroup:
    selected_stats: tuple[str, ...]
    operator: str = ">="
    count: int = 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CountGroup":
        data = _mapping(data, "词条组")
        _known_fields(data, {"selected_stats", "operator", "count"}, "词条组")
        stats = data.get("selected_stats")
        if not isinstance(stats, list) or not stats:
            raise ValidationError("每个启用的词条组必须勾选至少一种词条")
        stats = tuple(_text(key, "词条标识") for key in stats)
        if len(set(stats)) != len(stats):
            raise ValidationError("勾选的词条类型不能重复")
        operator = data.get("operator", ">=")
        if not isinstance(operator, str) or operator not in {">", ">="}:
            raise ValidationError("命中数量只能比较超过（>）或至少（>=）")
        count = data.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValidationError("命中数量门槛必须是非负整数")
        if operator == ">=" and count == 0:
            raise ValidationError("至少命中数量必须从 1 条开始，不能用 0 条绕过词条筛选")
        if count > len(stats) or (operator == ">" and count == len(stats)):
            raise ValidationError("数量门槛无法达到：命中数最多等于勾选的词条类型数")
        return cls(stats, operator, count)

    def to_dict(self) -> dict[str, Any]:
        return {"selected_stats": list(self.selected_stats), "operator": self.operator, "count": self.count}


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    equipment_keys: tuple[str, ...]
    conditions: tuple[Condition, ...] = ()
    mode: str = "all"
    enabled: bool = True
    groups: Mapping[str, CountGroup] = field(default_factory=dict)
    group_mode: str = "all"

    @property
    def is_count_rule(self) -> bool:
        return bool(self.groups)

    @property
    def schema_version(self) -> int:
        return 2 if self.is_count_rule else 1

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], catalog: Any = None) -> "Rule":
        data = _mapping(data, "规则")
        _known_fields(data, {"id", "name", "equipment_keys", "conditions", "mode", "enabled", "groups", "group_mode"}, "规则")
        is_count = "groups" in data
        if is_count and ("conditions" in data or "mode" in data):
            raise ValidationError("主副词条数量规则不能混用旧版逐条条件")
        if not is_count and "group_mode" in data:
            raise ValidationError("group_mode 只能用于主副词条数量规则")
        rule_id = _text(data.get("id"), "规则 ID")
        name = _text(data.get("name", rule_id), "规则名称")
        keys = data.get("equipment_keys")
        if not isinstance(keys, list) or not keys:
            raise ValidationError("规则必须指定至少一件特定装备")
        equipment_keys = tuple(_text(k, "装备标识") for k in keys)
        if len(set(equipment_keys)) != len(equipment_keys):
            raise ValidationError("规则含重复装备标识")
        mode = data.get("mode", "all")
        if not isinstance(mode, str) or mode not in {"all", "any"}:
            raise ValidationError("条件关系只能是 all（全部）或 any（任一）")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValidationError("enabled 必须是布尔值")
        conditions: tuple[Condition, ...] = ()
        groups: dict[str, CountGroup] = {}
        group_mode = data.get("group_mode", "all")
        if group_mode != "all":
            raise ValidationError("主词条优先，启用的主副词条组必须全部满足（group_mode=all）")
        if is_count:
            raw_groups = _mapping(data["groups"], "主副词条组")
            if not raw_groups:
                raise ValidationError("规则必须启用至少一个主词条或副词条组")
            _known_fields(raw_groups, {"primary", "secondary"}, "主副词条组")
            if "primary" not in raw_groups:
                raise ValidationError("主词条筛选必须启用；副词条筛选可以关闭")
            groups = {key: CountGroup.from_dict(value) for key, value in raw_groups.items()}
        else:
            raw_conditions = data.get("conditions")
            if not isinstance(raw_conditions, list) or not raw_conditions:
                raise ValidationError("规则必须指定至少一个词条条件")
            conditions = tuple(Condition.from_dict(c) for c in raw_conditions)
        if catalog is not None:
            unknown_equipment = [k for k in equipment_keys if k not in catalog.equipment]
            stat_keys = [c.stat_key for c in conditions] + [key for group in groups.values() for key in group.selected_stats]
            unknown_stats = [key for key in stat_keys if key not in catalog.stats]
            if unknown_equipment:
                raise ValidationError(f"未知装备标识：{', '.join(unknown_equipment)}")
            if unknown_stats:
                raise ValidationError(f"未知词条标识：{', '.join(unknown_stats)}")
        return cls(rule_id, name, equipment_keys, conditions, mode, enabled, groups, group_mode)

    def to_dict(self) -> dict[str, Any]:
        result = {"id": self.id, "name": self.name, "enabled": self.enabled,
                  "equipment_keys": list(self.equipment_keys)}
        if self.is_count_rule:
            result.update(groups={key: group.to_dict() for key, group in self.groups.items()}, group_mode=self.group_mode)
            if self.conditions:
                result["conditions"] = [condition.to_dict() for condition in self.conditions]
        else:
            result.update(mode=self.mode, conditions=[condition.to_dict() for condition in self.conditions])
        return result


@dataclass(frozen=True)
class AffixObservation:
    value: Decimal | None = None
    unit: str | None = None
    confidence: Decimal = Decimal(0)
    group: str = "unknown"
    group_confidence: Decimal = Decimal(0)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AffixObservation":
        data = _mapping(data, "词条观测")
        _known_fields(data, {"value", "unit", "confidence", "group", "group_confidence"}, "词条观测")
        unit = data.get("unit")
        if unit is not None and (not isinstance(unit, str) or unit not in {"flat", "percent"}):
            raise ValidationError("观测单位只能是 flat、percent 或 null")
        value = None if data.get("value") is None else decimal_value(data["value"], "词条读数")
        group = data.get("group", "unknown")
        if not isinstance(group, str) or group not in {"primary", "secondary", "unknown"}:
            raise ValidationError("词条分组只能是 primary、secondary 或 unknown")
        return cls(value, unit, confidence_value(data.get("confidence", 0), "词条置信度"),
                   group, confidence_value(data.get("group_confidence", 0), "分组置信度"))

    def to_dict(self) -> dict[str, Any]:
        return {"value": None if self.value is None else str(self.value),
                "unit": self.unit, "confidence": str(self.confidence), "group": self.group,
                "group_confidence": str(self.group_confidence)}


@dataclass(frozen=True)
class ItemObservation:
    equipment_key: str | None = None
    name: str = ""
    slot: str = ""
    item_id: str | None = None
    affixes: Mapping[str, tuple[AffixObservation, ...]] = field(default_factory=dict)
    lock_state: str = "unknown"
    name_confidence: Decimal = Decimal(0)
    lock_confidence: Decimal = Decimal(0)
    tooltip_complete: bool = False
    observed_token: str = ""
    groups_complete: Mapping[str, bool] = field(default_factory=lambda: {"primary": False, "secondary": False})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ItemObservation":
        data = _mapping(data, "装备观测")
        _known_fields(data, {"equipment_key", "name", "slot", "item_id", "itemid", "affixes",
                            "lock_state", "name_confidence", "nameconfidence", "lock_confidence",
                            "tooltip_complete", "observed_token", "groups_complete"}, "装备观测")
        equipment_key = data.get("equipment_key")
        if equipment_key is not None:
            equipment_key = _text(equipment_key, "装备标识")
        name = _text(data.get("name", ""), "装备名称", empty=True)
        slot = _text(data.get("slot", ""), "装备位置", empty=True)
        if "itemid" in data and "item_id" in data and data["itemid"] != data["item_id"]:
            raise ValidationError("item_id 和 itemid 不一致")
        item_id = data.get("item_id", data.get("itemid"))
        if item_id is not None:
            item_id = _text(item_id, "装备实例 ID")
        if "nameconfidence" in data and "name_confidence" in data and data["nameconfidence"] != data["name_confidence"]:
            raise ValidationError("名称置信度字段不一致")
        confidence = confidence_value(data.get("name_confidence", data.get("nameconfidence", 0)), "名称置信度")
        lock_confidence = confidence_value(data.get("lock_confidence", 0), "锁定标记置信度")
        lock_state = data.get("lock_state", "unknown")
        if not isinstance(lock_state, str) or lock_state not in {"locked", "unlocked", "unknown"}:
            raise ValidationError("锁定状态只能是 locked、unlocked 或 unknown")
        complete = data.get("tooltip_complete", False)
        if not isinstance(complete, bool):
            raise ValidationError("tooltip_complete 必须是布尔值")
        token = _text(data.get("observed_token", ""), "观测标识", empty=True)
        raw_groups_complete = _mapping(data.get("groups_complete", {}), "词条组完整性")
        _known_fields(raw_groups_complete, {"primary", "secondary"}, "词条组完整性")
        groups_complete = {"primary": False, "secondary": False}
        for key, flag in raw_groups_complete.items():
            if not isinstance(flag, bool):
                raise ValidationError("每个词条组的完整性必须是布尔值")
            groups_complete[key] = flag
        raw_affixes = _mapping(data.get("affixes", {}), "装备词条")
        affixes: dict[str, tuple[AffixObservation, ...]] = {}
        for key, values in raw_affixes.items():
            key = _text(key, "词条标识")
            if not isinstance(values, list) or not values:
                raise ValidationError("每个词条必须包含非空观测列表")
            affixes[key] = tuple(AffixObservation.from_dict(v) for v in values)
        return cls(equipment_key, name, slot, item_id, affixes, lock_state,
                   confidence, lock_confidence, complete, token, groups_complete)

    def to_dict(self) -> dict[str, Any]:
        return {"equipment_key": self.equipment_key, "name": self.name, "slot": self.slot,
                "item_id": self.item_id, "affixes": {k: [v.to_dict() for v in values] for k, values in self.affixes.items()},
                "lock_state": self.lock_state, "name_confidence": str(self.name_confidence),
                "lock_confidence": str(self.lock_confidence), "tooltip_complete": self.tooltip_complete,
                "observed_token": self.observed_token, "groups_complete": dict(self.groups_complete)}


@dataclass(frozen=True)
class Evaluation:
    status: str  # match / miss / uncertain
    rule_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LockDecision:
    action: str  # press_l / skip / review; this is a proposal, never an input event
    reasons: tuple[str, ...]
    matched_rule_ids: tuple[str, ...] = ()
    observed_token: str = ""
    item_id: str | None = None

    @property
    def should_lock(self) -> bool:
        return self.action == "press_l"
