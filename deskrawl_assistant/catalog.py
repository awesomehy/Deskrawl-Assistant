"""Read literal shipped localization names, without inferring runtime enums."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .models import ValidationError


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    en: str
    zh: str

    @property
    def label(self) -> str:
        return self.zh or self.en or self.key


@dataclass(frozen=True)
class Catalog:
    equipment: Mapping[str, CatalogEntry]
    stats: Mapping[str, CatalogEntry]

    def resolve_equipment(self, name: str) -> str | None:
        """Only exact key/name matching; duplicate display names are ambiguous."""
        return self._resolve(name, self.equipment)

    def resolve_stat(self, name: str) -> str | None:
        return self._resolve(name, self.stats)

    @staticmethod
    def _resolve(name: str, entries: Mapping[str, CatalogEntry]) -> str | None:
        name = name.strip()
        if name in entries:
            return name
        matches = [key for key, entry in entries.items() if name and name in {entry.en, entry.zh}]
        return matches[0] if len(matches) == 1 else None


def load_catalog(path: str | Path) -> Catalog:
    with Path(path).open(encoding="utf-8-sig") as source:
        data = json.load(source)
    try:
        equipment_keys = data["indexes"]["equipment_root_key_groups"]["equipment_names"]
        stat_keys = data["indexes"]["stat_label_keys"]
        equipment_rows = data["tables"]["Equipments"]["entries"]
        stat_rows = data["tables"]["Stats"]["entries"]
    except (KeyError, TypeError) as exc:
        raise ValidationError("本地装备词典结构不受支持") from exc

    def entries(keys, rows):
        result = {}
        for key in keys:
            try:
                row = rows[key]
                if row["Key"] != key:
                    raise ValidationError(f"词典键与行标识不一致：{key}")
                result[key] = CatalogEntry(key, row.get("English", ""), row.get("Chinese (Simplified)", ""))
            except (KeyError, TypeError) as exc:
                raise ValidationError(f"词典缺少有效名称：{key}") from exc
        return result

    return Catalog(entries(equipment_keys, equipment_rows), entries(stat_keys, stat_rows))
