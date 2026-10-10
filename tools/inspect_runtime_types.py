"""Resolve Deskrawl v39 field types from on-disk IL2CPP registration.

Reads only installation files. Does not load DLLs, inspect processes, access
player values, or write to the game directory. This is restricted to the
previously verified metadata hash and PE64 structures. Runtime API lookups
remain the preferred authority for live field offsets and values.

Layout references:
https://github.com/SamboyCoding/Cpp2IL/blob/development/LibCpp2IL/BinaryStructures/Il2CppType.cs
https://github.com/SamboyCoding/Cpp2IL/blob/development/LibCpp2IL/BinaryStructures/Il2CppMetadataRegistration.cs
https://github.com/SamboyCoding/Cpp2IL/blob/development/LibCpp2IL/BinaryStructures/Il2CppGenericClass.cs
https://github.com/SamboyCoding/Cpp2IL/blob/development/LibCpp2IL/BinaryStructures/Il2CppGenericInst.cs
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct


METADATA_HASH = "57627070d0fe8a00f82e373100768f529c4829f02d6aeb8ac3057aaab13cacfc"
GAME_VERSION = "1.0.2a"
STEAM_BUILD = 25842017
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deskrawl_assistant.il2cpp_metadata import PE, MetadataInspector

SELECTED = {
    "Inventory", "InventorySlot", "Storage", "GeneratedItemData", "ItemData", "gj",
    "StatModifier", "LeveledStatModifier", "SaveSystem", "SaveData", "SavedSlot",
    "SavedRegistryEntry", "SavedStatModifier", "SavedStash", "SaveContainer",
    "ObscuredFloat", "ObscuredInt", "ObscuredLong", "ObscuredString",
}


class Inspector(MetadataInspector):
    def __init__(self, metadata_path, binary_path):
        if hashlib.sha256(metadata_path.read_bytes()).hexdigest() != METADATA_HASH:
            raise ValueError("Metadata differs from the verified build; revalidate layout first")
        super().__init__(metadata_path, binary_path)

    def inspect(self):
        selected = []
        references = []
        for index, d in enumerate(self.defs):
            fields = self.fields(index)
            hits = [f for f in fields if "GeneratedItemData" in f["resolvedType"]]
            if hits:
                references.append({"type": ".".join(x for x in (d["namespace"], d["name"]) if x), "fields": hits})
            if d["name"] in SELECTED or hits:
                selected.append({"name": d["name"], "namespace": d["namespace"], "typeDefinitionIndex": index,
                                 "isValueType": d["isValueType"], "byvalTypeIndex": d["byvalTypeIndex"],
                                 "fields": fields, "methods": self.methods(index)})
        return {
            "source": "Read-only on-disk metadata and GameAssembly PE; no process reads, runtime invocation, or player data",
            "metadataSha256": METADATA_HASH, "gameAssemblySha256": hashlib.sha256(self.pe.data).hexdigest(),
            "gameVersion": GAME_VERSION, "steamBuildId": str(STEAM_BUILD),
            "metadataVersion": 39, "typeDefinitionCount": self.type_count, "runtimeTypeCount": len(self.type_pointers),
            "metadataRegistration": {"rawFileOffset": f"0x{self.registration_raw:X}",
                                     "preferredVa": f"0x{self.pe.va(self.registration_raw):X}",
                                     "preferredImageBase": f"0x{self.pe.base:X}"},
            "readPathHints": {
                "inventory": {"class": "Inventory", "namespace": "", "singletonStaticField": "<nys>k__BackingField",
                              "slotsInstanceField": "<nyv>k__BackingField", "slotsType": "InventorySlot[]"},
                "storage": {"class": "Storage", "namespace": "", "singletonStaticField": "<oeo>k__BackingField",
                            "slotsInstanceField": "<oer>k__BackingField", "slotsType": "InventorySlot[]"},
                "registry": {"class": "gj", "namespace": "", "dictionaryStaticField": "ndp",
                             "dictionaryType": "System.Collections.Generic.Dictionary`2<System.String, GeneratedItemData>",
                             "candidateKeySlotField": "ItemUid", "keyAssociationValidatedAtRuntime": False},
                "modifiers": {"class": "GeneratedItemData", "field": "Modifiers", "type": "StatModifier[]",
                              "statField": "Stat", "modifierTypeField": "Type", "valueField": "Value",
                              "valueType": "CodeStage.AntiCheat.ObscuredTypes.ObscuredFloat",
                              "randomAffixSourceValidated": False, "displayUnitValidated": False},
            },
            "typeHints": [self.type_at(t['byvalTypeIndex']) for t in selected if t['name'] in SELECTED],
            "generatedItemDataReferences": references, "types": selected,
            "limitations": [
                "Obfuscated method behavior and live instance identity remain unverified.",
                "Offsets are static registration offsets, not ASLR-adjusted addresses or player values.",
                "For value types, raw registration offsets may include object header adjustment; use runtime field APIs.",
                "Obscured numeric types are not raw float/int; do not reinterpret their bytes as ordinary numbers.",
                "Modifiers storage does not prove random-affix source, UI category, or display unit.",
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=Path(r"G:\SteamLibrary\steamapps\common\Deskrawl"))
    parser.add_argument("--output", type=Path, default=Path("data/runtime-type-hints.json"))
    args = parser.parse_args()
    game = args.game_dir.resolve()
    output = args.output.resolve()
    if output == game or game in output.parents:
        parser.error("Output must be outside the game installation")
    inspector = Inspector(game / "Deskrawl_Data/il2cpp_data/Metadata/global-metadata.dat", game / "GameAssembly.dll")
    result = inspector.inspect()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Resolved {len(result['types'])} relevant types, {result['runtimeTypeCount']} registered type entries.")
    for row in result["typeHints"]:
        print(f"{row['typeIndex']}: {row['resolvedType']} (static={row['isStatic']})")


if __name__ == "__main__":
    main()
