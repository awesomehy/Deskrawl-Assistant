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


METADATA_HASH = "2c0ae47e1ee26b6c787d5294f04680b6b875d84e8d5f6db1e574446892e3f2ad"
PRIMITIVES = {
    1: "System.Void", 2: "System.Boolean", 3: "System.Char", 4: "System.SByte",
    5: "System.Byte", 6: "System.Int16", 7: "System.UInt16", 8: "System.Int32",
    9: "System.UInt32", 10: "System.Int64", 11: "System.UInt64",
    12: "System.Single", 13: "System.Double", 14: "System.String",
    24: "System.IntPtr", 25: "System.UIntPtr", 28: "System.Object",
}
SELECTED = {
    "Inventory", "InventorySlot", "Storage", "GeneratedItemData", "ItemData", "gj",
    "StatModifier", "LeveledStatModifier", "SaveSystem", "SaveData", "SavedSlot",
    "SavedRegistryEntry", "SavedStatModifier", "SavedStash", "SaveContainer",
    "ObscuredFloat", "ObscuredInt", "ObscuredLong", "ObscuredString",
}


class PE:
    def __init__(self, path: Path):
        self.data = path.read_bytes()
        if self.data[:2] != b"MZ":
            raise ValueError("Not a PE file")
        p = struct.unpack_from("<I", self.data, 0x3C)[0]
        if self.data[p:p + 4] != b"PE\0\0":
            raise ValueError("Invalid PE signature")
        machine, sections = struct.unpack_from("<HH", self.data, p + 4)
        optional_size = struct.unpack_from("<H", self.data, p + 20)[0]
        optional = p + 24
        if machine != 0x8664 or struct.unpack_from("<H", self.data, optional)[0] != 0x20B:
            raise ValueError("Only verified Windows PE64 is supported")
        self.base = struct.unpack_from("<Q", self.data, optional + 24)[0]
        self.sections = []
        for index in range(sections):
            position = optional + optional_size + index * 40
            virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", self.data, position + 8)
            if raw_offset + raw_size > len(self.data):
                raise ValueError("PE section outside file")
            self.sections.append((rva, virtual_size, raw_offset, raw_size))

    def raw(self, va: int, length: int = 1) -> int:
        rva = va - self.base
        for start, _, offset, size in self.sections:
            if start <= rva and rva + length <= start + size:
                return offset + rva - start
        raise ValueError(f"VA 0x{va:X} does not refer to raw file data")

    def unpack(self, fmt: str, va: int):
        return struct.unpack_from(fmt, self.data, self.raw(va, struct.calcsize(fmt)))

    def va(self, raw: int) -> int:
        for rva, _, offset, size in self.sections:
            if offset <= raw < offset + size:
                return self.base + rva + raw - offset
        raise ValueError("Raw position is outside sections")


class Inspector:
    def __init__(self, metadata_path: Path, binary_path: Path):
        self.meta = metadata_path.read_bytes()
        if hashlib.sha256(self.meta).hexdigest() != METADATA_HASH:
            raise ValueError("Metadata differs from the verified build; revalidate layout first")
        if struct.unpack_from("<II", self.meta) != (0xFAB11BAF, 39):
            raise ValueError("Only metadata v39 is supported")
        self.pe = PE(binary_path)
        self.string_offset, self.string_size, _ = self.section(32)
        self.method_offset, _, self.method_count = self.section(68, 30)
        self.parameter_offset, _, self.parameter_count = self.section(128, 10)
        self.field_offset, _, self.field_count = self.section(140, 10)
        self.type_offset, _, self.type_count = self.section(236, 76)
        self.defs = []
        for index in range(self.type_count):
            p = self.type_offset + index * 76
            name_index, namespace_index = struct.unpack_from("<ii", self.meta, p)
            byval, declaring, parent = struct.unpack_from("<HHH", self.meta, p + 8)
            first_field, first_method = struct.unpack_from("<ii", self.meta, p + 20)
            method_count, _, field_count = struct.unpack_from("<HHH", self.meta, p + 52)
            self.defs.append({"name": self.string(name_index), "namespace": self.string(namespace_index),
                              "byvalTypeIndex": byval, "declaringTypeIndex": declaring,
                              "parentTypeIndex": parent, "firstField": first_field,
                              "firstMethod": first_method, "methodCount": method_count,
                              "fieldCount": field_count, "isValueType": bool(struct.unpack_from("<I", self.meta, p + 68)[0] & 1)})
        self.registration_raw, self.registration = self.find_registration()
        self.type_pointers = self.pe.unpack(f"<{self.registration[6]}Q", self.registration[7])
        self.field_offset_pointers = self.pe.unpack(f"<{self.type_count}Q", self.registration[11])
        self.resolved = {}
        # Validate every direct byval definition, not only a small sample.
        for index, definition in enumerate(self.defs):
            data, bits = self.pe.unpack("<QI", self.type_pointers[definition["byvalTypeIndex"]])
            kind = bits >> 16 & 255
            if kind in (17, 18) and data != index:
                raise ValueError(f"Type definition/registration mismatch at {index}")

    def section(self, p: int, size: int | None = None):
        offset, byte_length, count = struct.unpack_from("<III", self.meta, p)
        if offset < 380 or offset + byte_length > len(self.meta) or (size and byte_length != size * count):
            raise ValueError("Metadata table layout changed")
        return offset, byte_length, count

    def string(self, index: int) -> str:
        if not 0 <= index < self.string_size:
            raise ValueError("Metadata string index outside table")
        p = self.string_offset + index
        end = self.meta.find(b"\0", p, self.string_offset + self.string_size)
        if end < p:
            raise ValueError("Metadata string is unterminated")
        return self.meta[p:end].decode("utf-8")

    def find_registration(self):
        needle = struct.pack("<Q", self.type_count)
        matches = []
        p = 0
        while True:
            p = self.pe.data.find(needle, p)
            if p < 0:
                break
            if p >= 80 and self.pe.data[p + 16:p + 24] == needle:
                values = struct.unpack_from("<16Q", self.pe.data, p - 80)
                try:
                    if values[6] < max(d["byvalTypeIndex"] for d in self.defs) + 1 or values[6] > 1_000_000:
                        raise ValueError("Unreasonable type count")
                    self.pe.raw(values[7], values[6] * 8)
                    self.pe.raw(values[11], self.type_count * 8)
                    self.pe.raw(values[13], self.type_count * 8)
                    matches.append((p - 80, values))
                except ValueError:
                    pass
            p += 1
        if len(matches) != 1:
            raise ValueError(f"Expected one validated registration, found {len(matches)}")
        return matches[0]

    def type_name(self, pointer: int, depth: int = 0) -> str:
        if depth > 12:
            raise ValueError("Type recursion limit")
        if pointer in self.resolved:
            return self.resolved[pointer]
        data, bits = self.pe.unpack("<QI", pointer)
        kind = bits >> 16 & 255
        if kind in PRIMITIVES:
            name = PRIMITIVES[kind]
        elif kind in (17, 18):
            if data >= self.type_count:
                raise ValueError("Type definition index outside table")
            definition = self.defs[data]
            name = ".".join(s for s in (definition["namespace"], definition["name"]) if s)
        elif kind in (15, 29):
            name = self.type_name(data, depth + 1) + ("*" if kind == 15 else "[]")
        elif kind == 21:
            base_pointer, class_inst, method_inst, _ = self.pe.unpack("<4Q", data)
            base = self.type_name(base_pointer, depth + 1)
            count, arguments_pointer = self.pe.unpack("<2Q", class_inst)
            if not 1 <= count <= 64 or method_inst != 0:
                raise ValueError("Invalid class generic context")
            arguments = self.pe.unpack(f"<{count}Q", arguments_pointer)
            name = base + "<" + ", ".join(self.type_name(x, depth + 1) for x in arguments) + ">"
        elif kind in (19, 30):
            name = f"{'!' if kind == 19 else '!!'}generic_parameter_{data}"
        else:
            name = f"unresolved_kind_0x{kind:02X}"
        if bits >> 29 & 1:
            name += "&"
        self.resolved[pointer] = name
        return name

    def type_at(self, index: int):
        if not 0 <= index < len(self.type_pointers):
            raise ValueError("Type index outside registration")
        pointer = self.type_pointers[index]
        _, bits = self.pe.unpack("<QI", pointer)
        attrs = bits & 65535
        return {"typeIndex": index, "resolvedType": self.type_name(pointer),
                "typeKind": bits >> 16 & 255, "attributes": f"0x{attrs:04X}",
                "isStatic": bool(attrs & 16), "isLiteral": bool(attrs & 64),
                "isInitOnly": bool(attrs & 32)}

    def fields(self, index: int):
        d = self.defs[index]
        first, count = d["firstField"], d["fieldCount"]
        if count and not 0 <= first <= self.field_count - count:
            raise ValueError("Field range outside metadata")
        offset_ptr = self.field_offset_pointers[index]
        offsets = self.pe.unpack(f"<{count}i", offset_ptr) if count and offset_ptr else None
        result = []
        for order in range(count):
            field_index = first + order
            name_index, type_index, token = struct.unpack_from("<iHI", self.meta, self.field_offset + field_index * 10)
            result.append({"name": self.string(name_index), "fieldIndex": field_index,
                           "token": f"0x{token:08X}", **self.type_at(type_index),
                           "rawRegistrationOffset": offsets[order] if offsets else None})
        return result

    def methods(self, index: int):
        d = self.defs[index]
        first, count = d["firstMethod"], d["methodCount"]
        if count and not 0 <= first <= self.method_count - count:
            raise ValueError("Method range outside metadata")
        result = []
        for method_index in range(first, first + count):
            p = self.method_offset + method_index * 30
            name, declaring, return_index, _, first_param, _, token, flags, _, _, param_count = struct.unpack_from("<iHHIiHIHHHH", self.meta, p)
            if declaring != index or (param_count and not 0 <= first_param <= self.parameter_count - param_count):
                raise ValueError("Method declaration or parameter range mismatch")
            parameters = []
            for pi in range(first_param, first_param + param_count):
                n, pt, ti = struct.unpack_from("<iIH", self.meta, self.parameter_offset + pi * 10)
                parameters.append({"name": self.string(n), "typeIndex": ti,
                                   "resolvedType": self.type_at(ti)["resolvedType"]})
            result.append({"name": self.string(name), "token": f"0x{token:08X}", "isStatic": bool(flags & 16),
                           "returnType": self.type_at(return_index)["resolvedType"], "parameters": parameters})
        return result

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
            "metadataVersion": 39, "typeDefinitionCount": self.type_count, "runtimeTypeCount": len(self.type_pointers),
            "metadataRegistration": {"rawFileOffset": f"0x{self.registration_raw:X}",
                                     "preferredVa": f"0x{self.pe.va(self.registration_raw):X}",
                                     "preferredImageBase": f"0x{self.pe.base:X}"},
            "readPathHints": {
                "inventory": {"class": "Inventory", "namespace": "", "singletonStaticField": "<nyh>k__BackingField",
                              "slotsInstanceField": "<nyk>k__BackingField", "slotsType": "InventorySlot[]"},
                "storage": {"class": "Storage", "namespace": "", "singletonStaticField": "<oed>k__BackingField",
                            "slotsInstanceField": "<oeg>k__BackingField", "slotsType": "InventorySlot[]"},
                "registry": {"class": "gj", "namespace": "", "dictionaryStaticField": "ndg",
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
