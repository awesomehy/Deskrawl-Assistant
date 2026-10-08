"""Read-only runtime snapshots -> rule observations, with explicit evidence gates.

This module never opens a process or sends input. Pool membership is a candidate
classification, not proof that a modifier is a native random affix. Missing
provenance/complete-read evidence therefore remains unknown by default.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .catalog import Catalog
from .models import AffixObservation, ItemObservation, ValidationError, decimal_value


def headline_stats(slot_name: str) -> frozenset[str]:
    """Attributes shown in the game's headline rather than its affix lists.

    Verified for the supported build from EquipmentTooltipUI.hok (0x625fd0)
    and hob (0x623265, 0x6235dd, 0x62372f). WeaponDamage/WeaponSpeed are
    always skipped; Armor is skipped only on these six armor slots. An
    unexpected Armor record on any other slot must still be checked normally.
    """
    keys = {"Stats.WeaponDamage", "Stats.WeaponSpeed"}
    if slot_name in {"Helm", "Chest", "Pants", "Boots", "Gloves", "Shoulder"}:
        keys.add("Stats.Armor")
    return frozenset(keys)


@dataclass(frozen=True)
class RuntimeReadEvidence:
    identity_verified: bool = False
    slot_verified: bool = False
    lock_verified: bool = False
    enum_values_verified: bool = False
    modifiers_complete: bool = False
    native_affixes_verified: bool = False
    provenance_verified: bool = False
    snapshot_consistent: bool = False
    tooltip_complete_verified: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RuntimeReadEvidence":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ValidationError("运行读取证据必须是对象")
        names = {field.name for field in fields(cls)}
        # A bridge may expose additional diagnostic evidence. Only the named
        # boolean facts in this contract can strengthen an observation.
        flags = {}
        for name in names:
            flag = data.get(name, False)
            if not isinstance(flag, bool):
                raise ValidationError(f"读取证据 {name} 必须是布尔值")
            flags[name] = flag
        return cls(**flags)


@dataclass(frozen=True)
class AdaptedModifier:
    stat_key: str
    value: Decimal | None
    stat_value: int | None
    group: str
    confidence: Decimal
    group_confidence: Decimal
    record_index: int


@dataclass(frozen=True)
class AdaptedItem:
    observation: ItemObservation
    issues: tuple[str, ...]
    container: str = ""
    index: int | None = None
    item_uid: str | None = None
    instance_id: str | None = None
    base_stats: tuple[str, ...] = ()
    display_modifiers: tuple[AdaptedModifier, ...] = ()


@dataclass(frozen=True)
class AdaptedSnapshot:
    items: tuple[AdaptedItem, ...]
    issues: tuple[str, ...]
    snapshot_id: str = ""
    bridge_session: str = ""
    pid: int | None = None

    @property
    def observations(self) -> tuple[ItemObservation, ...]:
        return tuple(item.observation for item in self.items)


def _enum_int(value: Any) -> int | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _identifier(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    return None


class RuntimeAdapter:
    def __init__(self, catalog: Catalog, pool_path: str | Path):
        self.catalog = catalog
        with Path(pool_path).open(encoding="utf-8-sig") as source:
            data = json.load(source)
        try:
            if data["schema_version"] != 1:
                raise ValidationError("静态词条池版本不受支持")
            enums = data["enumConstants"]
            self.stat_names = self._enum_map(enums["StatType"])
            self.slot_names = self._enum_map(enums["EquipSlotType"])
            self.modifier_names = self._enum_map(enums["ModifierType"])
            self.metadata_sha256 = data.get("metadataSha256", "")
            self.pools: dict[int, dict[str, frozenset[str]]] = {}
            for pool in data["slotPools"]:
                value = _enum_int(pool.get("slotValue"))
                if value is None or self.slot_names.get(value) != pool.get("slot"):
                    raise ValidationError("静态词条池的部位与真实枚举不一致")
                groups = {group: frozenset(pool[group]) for group in ("primary", "secondary")}
                if any(key not in catalog.stats for keys in groups.values() for key in keys):
                    raise ValidationError("静态词条池包含本地词典未收录的属性")
                if value in self.pools:
                    raise ValidationError("静态词条池含重复部位")
                self.pools[value] = groups
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValidationError("静态词条池或真实枚举结构无效") from exc

    @staticmethod
    def _enum_map(rows) -> dict[int, str]:
        result = {}
        if not isinstance(rows, list):
            raise ValidationError("枚举常量必须是列表")
        for row in rows:
            value = _enum_int(row.get("value"))
            name = row.get("name")
            if value is None or not isinstance(name, str) or not name or value in result:
                raise ValidationError("枚举常量存在无效或重复记录")
            result[value] = name
        return result

    @staticmethod
    def _normalise(raw: Mapping[str, Any]) -> dict[str, Any]:
        """Support the bridge's nested slot row and explicit flattened records."""
        result = dict(raw)
        base = raw.get("item")
        generated = raw.get("generated")
        if isinstance(base, Mapping):
            for key in ("name_key", "internal_name", "equip_slot", "rarity"):
                if key not in result and key in base:
                    result[key] = base[key]
        if isinstance(generated, Mapping):
            for key in ("instance_id", "locked", "modifiers", "item_level", "modifiers_complete"):
                if key not in result and key in generated:
                    result[key] = generated[key]
        # The inventory slot index is a location, never an EquipSlotType.
        if "index" not in result:
            result["index"] = result.get("slot_index")
        return result

    def adapt_item(self, raw: Mapping[str, Any], *, snapshot_token: str = "",
                   session_id: str = "", evidence: RuntimeReadEvidence | None = None) -> AdaptedItem:
        if not isinstance(raw, Mapping):
            raise ValidationError("运行装备记录必须是对象")
        row = self._normalise(raw)
        issues: list[str] = []
        if evidence is None:
            try:
                evidence = RuntimeReadEvidence.from_dict(row.get("read_evidence"))
            except ValidationError as exc:
                issues.append(str(exc))
                evidence = RuntimeReadEvidence()
        container = row.get("container", "")
        container = container if isinstance(container, str) else ""
        index = _enum_int(row.get("index"))
        uid = _identifier(row.get("item_uid"))
        instance = _identifier(row.get("instance_id"))
        # UID and generated InstanceId are separate identifiers. Equality is
        # neither assumed nor required. Session scope prevents cross-run reuse.
        item_id = f"{session_id}:instance:{instance}" if session_id and instance and evidence.identity_verified else None
        if not item_id:
            issues.append("装备实例身份尚未核验，未建立可用于锁定的实例标识")
        keys = [value for value in (row.get("name_key"), row.get("internal_name"))
                if isinstance(value, str) and value]
        exact_keys = {key for key in keys if key in self.catalog.equipment}
        key = next(iter(exact_keys)) if len(exact_keys) == 1 else None
        if row.get("name_key") and row["name_key"] not in self.catalog.equipment:
            key = None
        if key is None:
            issues.append("尚未从已核验的字段读到精确装备名称键")
        identity_ready = evidence.identity_verified and evidence.snapshot_consistent and item_id is not None and key is not None
        name = self.catalog.equipment[key].label if key else (keys[0] if keys else "")
        slot_raw = row.get("equip_slot", row.get("equipment_slot"))
        slot_value = _enum_int(slot_raw)
        slot_name = self.slot_names.get(slot_value) if slot_value is not None else None
        slot_name_claim = slot_raw.get("name") if isinstance(slot_raw, Mapping) else None
        if slot_name_claim is not None and slot_name_claim != slot_name:
            slot_name = None
            issues.append("装备部位整数与名称不一致，未使用该部位分类")
        pool = self.pools.get(slot_value) if slot_name and evidence.slot_verified else None
        if pool is None:
            issues.append("装备真实部位尚未核验；背包位置不能作为词条池部位")
        locked = row.get("locked")
        lock_ready = isinstance(locked, bool) and evidence.lock_verified and evidence.snapshot_consistent
        lock_state = ("locked" if locked else "unlocked") if lock_ready else "unknown"
        if not lock_ready:
            issues.append("生成装备的锁定状态尚未核验；背包槽位锁不能替代装备锁")
        records = row.get("modifiers")
        records_are_list = isinstance(records, list)
        if not records_are_list:
            records = []
            issues.append("未读到完整原生装备词条列表")
        if not evidence.enum_values_verified:
            issues.append("运行版本的属性枚举尚未与本地静态枚举核验")
        complete = records_are_list and evidence.modifiers_complete and evidence.snapshot_consistent
        source_ready = evidence.native_affixes_verified or evidence.provenance_verified
        grouping_ready = bool(pool) and complete and source_ready and evidence.enum_values_verified
        if not source_ready:
            issues.append("Modifiers 的原生随机词条来源尚未核验；固定、宝石、强化和特殊来源不能计数")
        affixes: dict[str, list[AffixObservation]] = {}
        all_native_resolved = grouping_ready
        excluded = 0
        base_stats: list[str] = []
        display_modifiers: list[AdaptedModifier] = []
        candidate_types: set[str] = set()
        for record_index, record in enumerate(records):
            if not isinstance(record, Mapping):
                all_native_resolved = False
                issues.append("词条记录不完整，主副词条组不能声明完整")
                continue
            origin = record.get("origin", record.get("source"))
            non_native = {"fixed", "gem", "upgrade", "legendary", "black_mist", "inherent"}
            if evidence.provenance_verified and origin in non_native:
                excluded += 1
                continue
            native = (evidence.native_affixes_verified and origin in {None, "generated_modifiers"}) or (evidence.provenance_verified and origin in {"native", "native_random"})
            stat_raw = record.get("stat")
            stat = _enum_int(stat_raw)
            stat_name = self.stat_names.get(stat) if stat is not None else None
            claimed_name = stat_raw.get("name") if isinstance(stat_raw, Mapping) else None
            stat_valid = stat_name is not None and (claimed_name is None or claimed_name == stat_name)
            stat_key = f"Stats.{stat_name}" if stat_valid else f"Runtime.UnknownStat.{stat if stat is not None else 'unread'}"
            if stat_key not in self.catalog.stats:
                stat_valid = False
            raw_value = record.get("value")
            try:
                value = None if raw_value is None else decimal_value(raw_value, "运行词条数值")
            except ValidationError:
                value = None
                issues.append("存在无法读取的词条数值；类型计数不使用数值作为筛选条件")
            # GeneratedItemData.Modifiers also contains headline attributes.
            # They are neither random affixes nor evidence of an incomplete
            # affix list. Mirror the game's tooltip exclusion, using verified
            # slot, enum and native-source evidence rather than pool absence.
            if grouping_ready and native and stat_valid and stat_key in headline_stats(slot_name):
                if stat_key not in base_stats:
                    base_stats.append(stat_key)
                display_modifiers.append(AdaptedModifier(stat_key, value, stat, "base", Decimal(1), Decimal(1), record_index))
                continue
            # ModifierType is deliberately not converted into a primary /
            # secondary label or an assumed UI-percent scale.
            modifier_type = record.get("modifier_type", record.get("type"))
            modifier_value = _enum_int(modifier_type)
            if modifier_type is not None and modifier_value not in self.modifier_names:
                issues.append("存在未识别的数值叠加类型；未推断显示单位")
            group = "unknown"
            if pool and stat_valid:
                candidates = [name for name, keys_in_group in pool.items() if stat_key in keys_in_group]
                if len(candidates) == 1:
                    candidate_types.add(stat_key)
                    if grouping_ready and native:
                        group = candidates[0]
            if group == "unknown":
                all_native_resolved = False
            affixes.setdefault(stat_key, []).append(AffixObservation(
                value=value, unit=None,
                confidence=Decimal(1) if stat_valid and evidence.enum_values_verified else Decimal(0),
                group=group, group_confidence=Decimal(1) if group != "unknown" else Decimal(0)))
            display_modifiers.append(AdaptedModifier(stat_key, value, stat, group,
                Decimal(1) if stat_valid and evidence.enum_values_verified else Decimal(0),
                Decimal(1) if group != "unknown" else Decimal(0), record_index))
        if excluded:
            issues.append(f"已排除 {excluded} 条经来源核验的固定、宝石、强化或特殊词条")
        if base_stats:
            issues.append("基础属性不参与主副词条筛选：" + "、".join(self.catalog.stats[key].label for key in base_stats))
        if candidate_types and not all_native_resolved:
            issues.append("静态词条池仅提供候选归类，当前没有把候选当作已验证主副词条")
        if not complete:
            issues.append("词条列表读取完整性或同一快照的一致性尚未核验")
        location = f"{container}:{index}" if index is not None else container
        token = f"{snapshot_token}:{container}:{index if index is not None else ''}:{instance or uid or ''}" if snapshot_token else ""
        observation = ItemObservation(
            equipment_key=key, name=name, slot=location, item_id=item_id,
            affixes={key: tuple(values) for key, values in affixes.items()},
            lock_state=lock_state, name_confidence=Decimal(1) if identity_ready else Decimal(0),
            lock_confidence=Decimal(1) if lock_ready else Decimal(0),
            tooltip_complete=evidence.tooltip_complete_verified and evidence.snapshot_consistent,
            observed_token=token,
            groups_complete={"primary": bool(all_native_resolved), "secondary": bool(all_native_resolved)})
        return AdaptedItem(observation, tuple(dict.fromkeys(issues)), container, index, uid, instance,
                           tuple(base_stats), tuple(display_modifiers))

    def adapt_snapshot(self, payload: Mapping[str, Any], *, evidence: RuntimeReadEvidence | None = None) -> AdaptedSnapshot:
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            raise ValidationError("运行快照必须是 schema_version=1 的对象")
        snapshot_id = _identifier(payload.get("snapshot_id")) or ""
        session = _identifier(payload.get("bridge_session")) or ""
        pid = _enum_int(payload.get("pid"))
        issues = []
        if not snapshot_id or not session or pid is None or pid <= 0:
            issues.append("运行快照缺少进程、会话或快照标识，不能确认连续监控身份")
        errors = payload.get("errors", [])
        consistency = payload.get("complete") is True and isinstance(errors, list) and not errors
        consistency = consistency and bool(snapshot_id and session and pid is not None and pid > 0)
        if not consistency:
            issues.append("运行快照不完整或存在读取错误，本次保持待核验")
        enum_match = payload.get("metadata_sha256") == self.metadata_sha256 and bool(self.metadata_sha256)
        raw_rows: list[dict[str, Any]] = []
        containers = payload.get("containers")
        if isinstance(containers, Mapping):
            for name, container in containers.items():
                if not isinstance(container, Mapping) or not isinstance(container.get("slots"), list):
                    issues.append(f"容器 {name} 未提供可读取的槽位列表")
                    continue
                for row in container["slots"]:
                    if not isinstance(row, Mapping):
                        issues.append(f"容器 {name} 存在不完整槽位")
                        continue
                    if row.get("is_equipment") is False:
                        continue
                    # Empty slots are not item observations.
                    if row.get("item") is None and row.get("generated") is None and not row.get("item_uid"):
                        continue
                    raw = dict(row)
                    raw["container"] = str(name)
                    raw["_container_complete"] = container.get("slots_complete") is True
                    raw_rows.append(raw)
        elif isinstance(payload.get("items"), list):
            raw_rows = [dict(row) for row in payload["items"] if isinstance(row, Mapping)]
            if len(raw_rows) != len(payload["items"]):
                issues.append("运行快照包含无效装备行")
        else:
            raise ValidationError("运行快照必须包含 containers 或 items")
        items = []
        for row in raw_rows:
            try:
                read = evidence or RuntimeReadEvidence.from_dict(row.get("read_evidence"))
                read = replace(read,
                    snapshot_consistent=read.snapshot_consistent and consistency and row.get("_container_complete", True) is True,
                    enum_values_verified=read.enum_values_verified or enum_match)
                items.append(self.adapt_item(row, snapshot_token=snapshot_id, session_id=f"{pid}:{session}", evidence=read))
            except (ValidationError, TypeError) as exc:
                issues.append(f"有一件装备无法安全适配：{exc}")
        identities = [item.observation.item_id for item in items if item.observation.item_id]
        duplicate_ids = {value for value in identities if identities.count(value) > 1}
        if duplicate_ids:
            issues.append("同一快照存在重复生成装备实例标识，相关装备身份待核验")
            items = [replace(item, observation=replace(item.observation, name_confidence=Decimal(0), item_id=None),
                             issues=item.issues + ("生成装备实例标识重复",))
                     if item.observation.item_id in duplicate_ids else item for item in items]
        return AdaptedSnapshot(tuple(items), tuple(issues), snapshot_id, session, pid)
