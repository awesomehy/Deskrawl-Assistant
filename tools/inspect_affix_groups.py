"""Extract verified static Deskrawl primary/secondary slot pools without game writes.

The serialized object has no full typetree. This parser is restricted to the
already examined metadata hash and explicitly checks the class, object head,
array bounds, decoded enum constants, and exact consumption of the object.
It does not classify live/player affixes or claim the config is unchanged at runtime.

Metadata constant format references:
https://github.com/SamboyCoding/Cpp2IL/blob/development/LibCpp2IL/Metadata/Il2CppFieldDefaultValue.cs
https://github.com/SamboyCoding/Cpp2IL/blob/development/LibCpp2IL/ClassReadingBinaryReader.cs
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import struct

import UnityPy

from extract_catalog import sha256_file


METADATA_HASH = "57627070d0fe8a00f82e373100768f529c4829f02d6aeb8ac3057aaab13cacfc"
CONFIG_PATH_ID = 59967
SCRIPT_PATH_ID = 374
CONFIG_BYTES = 1056
ENUM_DEFAULT_TYPE_INDEX = 24019


def compressed_integer(data: bytes, offset: int) -> tuple[int, bytes]:
    start = offset
    first = data[offset]
    offset += 1
    if first < 0x80:
        unsigned = first
    elif first == 0xF0:
        unsigned = struct.unpack_from("<I", data, offset)[0]
        offset += 4
    elif first == 0xFF:
        unsigned = 0xFFFFFFFF
    elif first == 0xFE:
        unsigned = 0xFFFFFFFE
    elif first & 0xC0 == 0xC0:
        unsigned = ((first & 0x3F) << 24) | int.from_bytes(data[offset:offset + 3], "big")
        offset += 3
    else:
        unsigned = ((first & 0x7F) << 8) | data[offset]
        offset += 1
    signed = -(1 << 31) if unsigned == 0xFFFFFFFF else -(unsigned // 2 + 1) if unsigned & 1 else unsigned // 2
    return signed, data[start:offset]


def enum_constants(path: Path) -> dict:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != METADATA_HASH:
        raise ValueError("Metadata hash differs from the examined Deskrawl build; revalidate the layout first.")

    def section(position: int, record_size: int | None = None) -> tuple[int, int, int]:
        offset, size, count = struct.unpack_from("<III", data, position)
        if offset < 380 or offset + size > len(data) or (record_size and size != record_size * count):
            raise ValueError("Metadata section layout changed.")
        return offset, size, count

    string_offset, string_size, _ = section(32)
    default_offset, _, default_count = section(92, 10)
    default_data_offset, default_data_size, _ = section(104)
    field_offset, _, _ = section(140, 10)
    type_offset, _, type_count = section(236, 76)

    def string(index: int) -> str:
        if not 0 <= index < string_size:
            raise ValueError("Invalid metadata string index.")
        start = string_offset + index
        end = data.find(b"\0", start, string_offset + string_size)
        if end < 0:
            raise ValueError("Unterminated metadata string.")
        return data[start:end].decode("utf-8")

    defaults = {}
    for index in range(default_count):
        field_index, default_type_index, data_index = struct.unpack_from("<iHi", data, default_offset + 10 * index)
        defaults[field_index] = (default_type_index, data_index)
    result = {}
    for index in range(type_count):
        position = type_offset + index * 76
        name_index, namespace_index = struct.unpack_from("<ii", data, position)
        name = string(name_index)
        if string(namespace_index) or name not in {"StatType", "EquipSlotType", "AttributeCategory", "ModifierType"}:
            continue
        if not struct.unpack_from("<I", data, position + 68)[0] & 2:
            raise ValueError(f"{name} is no longer an enum.")
        first_field = struct.unpack_from("<i", data, position + 20)[0]
        count = struct.unpack_from("<H", data, position + 56)[0]
        constants = []
        for field_index in range(first_field, first_field + count):
            field_name_index, field_type_index = struct.unpack_from("<iH", data, field_offset + 10 * field_index)
            field_name = string(field_name_index)
            if field_name == "value__":
                continue
            default_type_index, data_index = defaults[field_index]
            if default_type_index != ENUM_DEFAULT_TYPE_INDEX or not 0 <= data_index < default_data_size:
                raise ValueError(f"Unexpected enum constant representation: {name}.{field_name}.")
            value, raw = compressed_integer(data, default_data_offset + data_index)
            if data_index + len(raw) > default_data_size:
                raise ValueError("Enum default value extends past default-value data.")
            constants.append({"name": field_name, "value": value, "fieldIndex": field_index,
                              "defaultTypeIndex": default_type_index, "defaultDataIndex": data_index,
                              "compressedBytesHex": raw.hex()})
        if len({row["value"] for row in constants}) != len(constants):
            raise ValueError(f"Ambiguous duplicate enum numeric values in {name}.")
        result[name] = constants
    if set(result) != {"StatType", "EquipSlotType", "AttributeCategory", "ModifierType"}:
        raise ValueError("Required enum definitions were not found.")
    return result


def extract(game_data: Path) -> dict:
    version = importlib.metadata.version("UnityPy")
    if version != "1.25.2":
        raise RuntimeError(f"Expected UnityPy 1.25.2; found {version}.")
    metadata = game_data / "il2cpp_data/Metadata/global-metadata.dat"
    enums = enum_constants(metadata)
    stats = {row["value"]: row["name"] for row in enums["StatType"]}
    slots = {row["value"]: row["name"] for row in enums["EquipSlotType"]}

    scripts_path = game_data / "globalgamemanagers.assets"
    scripts = UnityPy.load(str(scripts_path))
    script = next(obj for obj in scripts.objects if obj.path_id == SCRIPT_PATH_ID)
    script_data = script.read()
    if script.type.name != "MonoScript" or script_data.m_ClassName != "ItemizationConfig" or script_data.m_AssemblyName != "Assembly-CSharp":
        raise ValueError("Expected ItemizationConfig MonoScript not found.")

    source = game_data / "sharedassets0.assets"
    environment = UnityPy.load(str(source))
    obj = next(obj for obj in environment.objects if obj.path_id == CONFIG_PATH_ID)
    if obj.type.name != "MonoBehaviour" or obj.byte_size != CONFIG_BYTES:
        raise ValueError("ItemizationConfig object size/class differs from the examined build.")
    externals = [entry.path for entry in obj.assets_file.externals]
    head = obj.read_typetree(check_read=False)
    script_ptr = head.get("m_Script", {})
    if head.get("m_Name") != "ItemizationConfig" or script_ptr != {"m_FileID": 1, "m_PathID": SCRIPT_PATH_ID}:
        raise ValueError("Unexpected ItemizationConfig object head.")
    if not externals or Path(externals[0]).name != "globalgamemanagers.assets":
        raise ValueError("ItemizationConfig script references an unexpected asset file.")
    data = obj.get_raw_data()
    name_length = struct.unpack_from("<i", data, 28)[0]
    if name_length != 17 or data[32:49] != b"ItemizationConfig":
        raise ValueError("Serialized MonoBehaviour head layout changed.")
    position = (32 + name_length + 3) & ~3

    def integer() -> int:
        nonlocal position
        if position + 4 > len(data):
            raise ValueError("Truncated serialized configuration.")
        result = struct.unpack_from("<i", data, position)[0]
        position += 4
        return result

    def array(limit: int) -> list[int]:
        count = integer()
        if not 0 <= count <= limit:
            raise ValueError("Invalid serialized array length.")
        return [integer() for _ in range(count)]

    count = integer()
    if count != 11:
        raise ValueError("Expected exactly 11 slot pools.")
    pools = []
    for _ in range(count):
        slot = integer()
        primary, secondary = array(len(stats)), array(len(stats))
        if slot not in slots or any(value not in stats for value in primary + secondary):
            raise ValueError("Pool contains unresolved enum constants.")
        if len(set(primary)) != len(primary) or len(set(secondary)) != len(secondary) or set(primary) & set(secondary):
            raise ValueError("Ambiguous primary/secondary pool contents.")
        pools.append({"slotValue": slot, "slot": slots[slot],
                      "primaryValues": primary, "primary": ["Stats." + stats[value] for value in primary],
                      "secondaryValues": secondary, "secondary": ["Stats." + stats[value] for value in secondary]})
    if len({row["slotValue"] for row in pools}) != len(pools):
        raise ValueError("Duplicate slot pools.")
    socket_count = integer()
    if socket_count != 11:
        raise ValueError("Expected exactly 11 socket entries.")
    sockets = [{"slotValue": integer(), "maxSockets": integer()} for _ in range(socket_count)]
    for row in sockets:
        if row["slotValue"] not in slots or not 0 <= row["maxSockets"] <= 10:
            raise ValueError("Unexpected socket configuration.")
        row["slot"] = slots[row["slotValue"]]
    minimum_level = integer()
    if position != len(data):
        raise ValueError("Configuration has unparsed trailing bytes; revalidate the layout.")

    return {"schema_version": 1,
            "scope": "Static shipped affix pool configuration only; eligible pool membership is not player affix provenance.",
            "metadataSha256": METADATA_HASH, "unitypyVersion": version,
            "source": {"path": str(source), "sha256": sha256_file(source), "configPathId": CONFIG_PATH_ID,
                       "configByteLength": len(data), "configObjectSha256": hashlib.sha256(data).hexdigest(),
                       "scriptFile": str(scripts_path), "scriptPathId": SCRIPT_PATH_ID,
                       "class": script_data.m_ClassName, "assembly": script_data.m_AssemblyName},
            "enumConstants": enums, "slotPools": pools,
            "gemSocketsBySlot": sockets, "minLevelForSockets": minimum_level,
            "limitations": ["The game can alter pool configuration at runtime; this is its shipped serialized configuration.",
                            "SavedStatModifier has Stat/Type/Value but no category/provenance; category must be resolved with item slot and validated source.",
                            "This script does not read saves, player items, process memory, runtime event sources, or game APIs.",
                            "Legendary effects, fixed stats, gem bonuses, sockets and hidden Black Mist choices must not count as proven random affixes.",
                            "Empty Back pools are preserved; no runtime fallback or pool construction is inferred."]}


def extract_ui_evidence(game_data: Path) -> dict:
    source = game_data / "resources.assets"
    wanted = {"UI.Primary", "UI.Secondary", "UI.RevealHint", "UI.BlackMistSteamDesc"}
    environment = UnityPy.load(str(source))
    entries = None
    malformed_rows = 0
    for obj in environment.objects:
        if obj.type.name != "TextAsset":
            continue
        asset = obj.read()
        if asset.m_Name == "UI":
            if entries is not None:
                raise ValueError("Multiple UI localization tables found.")
            entries = {}
            reader = csv.DictReader(io.StringIO(asset.m_Script.lstrip("\ufeff")))
            if not reader.fieldnames or not {"Key", "English", "Chinese (Simplified)"} <= set(reader.fieldnames):
                raise ValueError("UI table is missing required columns.")
            for row in reader:
                malformed = None in row or any(value is None for value in row.values())
                malformed_rows += bool(malformed)
                key = row.get("Key")
                if key not in wanted:
                    continue
                if malformed or key in entries:
                    raise ValueError("Required UI group label row is malformed or duplicated.")
                entries[key] = {"English": row["English"], "Chinese (Simplified)": row["Chinese (Simplified)"],
                                "path_id": obj.path_id}
    if entries is None or not wanted <= set(entries):
        raise ValueError("Required UI group labels were not found.")
    return {"scope": "Static shipped UI keys relevant to affix groups only", "source": str(source),
            "sourceSha256": sha256_file(source), "entries": entries,
            "unrelatedMalformedRowCount": malformed_rows,
            "validation": "Only required group-label rows were validated; this is not a validated export of the full UI table."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, default=Path(r"G:\SteamLibrary\steamapps\common\Deskrawl\Deskrawl_Data"))
    parser.add_argument("--output", type=Path, default=Path("data/affix-groups-static.json"))
    parser.add_argument("--ui-output", type=Path, default=Path("data/affix-ui-evidence.json"))
    args = parser.parse_args()
    source = args.game_data.resolve(strict=True)
    output = args.output.resolve()
    ui_output = args.ui_output.resolve()
    for target in (output, ui_output):
        if source == target.parent or source in target.parents:
            raise ValueError("Output must be outside the game installation directory.")
    result = extract(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ui_output.parent.mkdir(parents=True, exist_ok=True)
    ui_output.write_text(json.dumps(extract_ui_evidence(source), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(result['slotPools'])} static slot pools with decoded metadata enum constants to {output}.")


if __name__ == "__main__":
    main()
