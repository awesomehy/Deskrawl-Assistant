"""Read static Deskrawl localization TextAssets; never write game assets or saves."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import io
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import UnityPy


EXPECTED_UNITYPY = "1.25.2"
TABLE_NAMES = ("Stats", "Equipments", "Items", "Abilities", "Talents", "Slaves", "Lifeskills")
REQUIRED_COLUMNS = ("Key", "English", "Chinese (Simplified)")
EQUIPMENT_NAME = re.compile(
    r"^(Common|Uncommon|UnCommon|Rare|Legendary|Divine)"
    r"(Belt|Boots|ChestArmor|Helm|Neck|Pant|Pants|Ring|Shoulder|Axe|Bow|Mace|Sword|Staff|Gloves|Back)"
    r"(\d+)$"
)
SET_ROOT = r"(?:Warrior|Monk|Sorcerer|Hunter)SetRune\d+"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_localization(text: str, name: str, path_id: int) -> dict:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline=""))
    if not reader.fieldnames or not set(REQUIRED_COLUMNS).issubset(reader.fieldnames):
        raise ValueError(f"{name}: required localization columns are missing")
    entries = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"{name}: malformed CSV row near line {reader.line_num}")
        key = row["Key"]
        if not key or key in entries:
            raise ValueError(f"{name}: empty or duplicate localization key {key!r}")
        # Preserve every original column and every value; placeholders remain unexpanded.
        entries[key] = row
    return {
        "source_path_id": path_id,
        "columns": reader.fieldnames,
        "row_count": len(entries),
        "entries": entries,
    }


def indexes_for(tables: dict) -> dict:
    stats = tables["Stats"]["entries"]
    stat_labels = [key for key in stats if not key.endswith(".Desc") and key != "Stats.AtCap"]
    stat_descriptions = [key for key in stats if key.endswith(".Desc")]
    equipments = tables["Equipments"]["entries"]
    root_groups = {
        "equipment_names": [],
        "gem_names": [],
        "generic_rune_names": [],
        "skill_rune_names": [],
        "set_rune_piece_names": [],
        "rune_set_names": [],
        "unclassified_root_keys": [],
    }
    for key in equipments:
        if "." in key:
            continue
        if EQUIPMENT_NAME.fullmatch(key):
            group = "equipment_names"
        elif re.fullmatch(r"Gem(?:Ruby|Amethyst|Diamond|Topaz|Emerald|Sapphire)\d+", key):
            group = "gem_names"
        elif re.fullmatch(r"UncommonRune(?:\d+|_.+)", key):
            group = "generic_rune_names"
        elif re.fullmatch(r"(?:Rare|Legendary)Rune_.+", key):
            group = "skill_rune_names"
        elif re.fullmatch(SET_ROOT + r"_\d+", key):
            group = "set_rune_piece_names"
        elif re.fullmatch(SET_ROOT, key):
            group = "rune_set_names"
        else:
            group = "unclassified_root_keys"
        root_groups[group].append(key)
    matches = [EQUIPMENT_NAME.fullmatch(key) for key in root_groups["equipment_names"]]
    return {
        "note": "Indexes classify literal localization keys only; these are not Unity enum mappings or proof of runtime availability.",
        "stat_label_keys": stat_labels,
        "stat_description_keys": stat_descriptions,
        "stat_template_keys": [key for key in stats if key == "Stats.AtCap"],
        "stat_labels_without_descriptions": [key for key in stat_labels if key + ".Desc" not in stats],
        "orphan_stat_descriptions": [key for key in stat_descriptions if key[:-5] not in stats],
        "equipment_root_key_groups": root_groups,
        "equipment_suffix_counts": dict(Counter(key.rsplit(".", 1)[-1] if "." in key else "root_key" for key in equipments)),
        "equipment_literal_rarity_counts": dict(Counter(match.group(1) for match in matches)),
        "equipment_literal_slot_counts": dict(Counter(match.group(2) for match in matches)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="resources.assets file to read")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "game-catalog.json")
    args = parser.parse_args()
    version = importlib.metadata.version("UnityPy")
    if version != EXPECTED_UNITYPY:
        raise RuntimeError(f"This extractor expects UnityPy=={EXPECTED_UNITYPY}, found {version}")
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    if source.parent == output.parent or source.parent in output.parents:
        raise ValueError("The output must be outside the game asset directory")
    environment = UnityPy.load(str(source))
    tables = {}
    unity_version = None
    for obj in environment.objects:
        if obj.type.name != "TextAsset":
            continue
        asset = obj.read()
        if asset.m_Name not in TABLE_NAMES:
            continue
        if asset.m_Name in tables:
            raise ValueError(f"Multiple TextAssets named {asset.m_Name}")
        if not isinstance(asset.m_Script, str):
            raise TypeError(f"{asset.m_Name}: unexpected TextAsset content type")
        tables[asset.m_Name] = parse_localization(asset.m_Script, asset.m_Name, obj.path_id)
        unity_version = getattr(obj.assets_file, "unity_version", unity_version)
    if set(tables) != set(TABLE_NAMES):
        raise ValueError(f"Missing required TextAssets: {set(TABLE_NAMES) - set(tables)}")
    tables = {name: tables[name] for name in TABLE_NAMES}
    indexes = indexes_for(tables)
    catalog = {
        "schema_version": 1,
        "scope": "Static shipped localization only. No character, save, account or UI TextAsset data.",
        "source": {
            "path": str(source),
            "size_bytes": source.stat().st_size,
            "sha256": sha256_file(source),
            "unity_version": unity_version,
            "game_version": None,
            "game_version_note": "Not inferred from localization or Unity version; verify separately in the running client.",
        },
        "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
        "extractor": {"name": "extract_catalog.py", "unitypy_version": version},
        "summary": {
            "table_row_counts": {name: table["row_count"] for name, table in tables.items()},
            "total_localization_rows": sum(table["row_count"] for table in tables.values()),
            "stat_label_count": len(indexes["stat_label_keys"]),
            "stat_description_count": len(indexes["stat_description_keys"]),
            "equipment_root_group_counts": {name: len(keys) for name, keys in indexes["equipment_root_key_groups"].items()},
        },
        "tables": tables,
        "indexes": indexes,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": catalog["summary"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
