"""Build-restricted Deskrawl snapshots, entirely through external read-only memory.

No injected component, game method call, input operation or game field write.
Class offsets are verified against local public accessor machine code; field
offsets and tokens are checked again against live IL2CPP FieldInfo records.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct
import time
import uuid

from .catalog import load_catalog
from .native_memory import MemoryReadError, ProcessMemory, SnapshotChangedError
from .paths import RESOURCE_ROOT

BASE = RESOURCE_ROOT
REQUIRED_CLASSES = ("Inventory", "Storage", "InventorySlot", "GeneratedItemData", "StatModifier", "ItemData", "gj", "SaveSystem")


def decode_obscured_float(data: bytes) -> float:
    """Decode a copied modern ObscuredFloat value without touching game memory.

    Retains the native IEEE-754 value; does not infer percentage display units.
    The verified value layout is hash, hidden bits, per-value key, fake, legacy.
    """
    if len(data) != 20:
        raise MemoryReadError("词条数值包装长度不一致")
    saved_hash, hidden, key, _, _ = struct.unpack("<IIIfI", data)
    if saved_hash == hidden == key == 0:
        return 0.0
    # Verified from 1.0.2a ACTk.Runtime!op_Implicit -> kdh (RVA 0x453aa0):
    # exchange hidden bytes 1 and 2, then XOR with this value's key.
    encoded = bytearray(struct.pack("<I", hidden))
    encoded[1], encoded[2] = encoded[2], encoded[1]
    decoded_bits = int.from_bytes(encoded, "little") ^ key
    raw = struct.pack("<I", decoded_bits)
    checksum = 0x811C9DC6
    for byte in raw:
        checksum = ((checksum ^ byte) * 0x1000192) & 0xFFFFFFFF
    if (checksum | 1) != saved_hash:
        raise MemoryReadError("词条数值完整性不一致，请重新读取")
    value = struct.unpack("<f", raw)[0]
    if not math.isfinite(value):
        raise MemoryReadError("词条数值不是有限数")
    return value


class NativeReader:
    def __init__(self, pid: int, on_compatibility=lambda state: None):
        self.memory = ProcessMemory(pid)
        self.pid = pid
        self.session_id = f"{pid}-{uuid.uuid4().hex}"
        self.capture = 0
        self.classes = {}
        self.field_cache = {}
        self.item_cache = {}
        self.snapshot_stamps = []
        self.on_compatibility = on_compatibility
        self.catalog = load_catalog(BASE / "data/game-catalog.json")
        self.profile = json.loads((BASE / "data/runtime-type-hints.json").read_text(encoding="utf-8"))
        self.types = {entry["name"]: entry for entry in self.profile["types"] if not entry["namespace"]}
        self.types.update({entry["name"]: entry for entry in json.loads((BASE / "data/ui-runtime-hints.json").read_text(encoding="utf-8"))})
        self.stat_names = {entry["value"]: entry["name"] for entry in json.loads((BASE / "data/affix-groups-static.json").read_text(encoding="utf-8"))["enumConstants"]["StatType"]}
        try:
            self._verify_build()
            self._locate_classes()
            self._verify_fields()
        except Exception:
            self.close()
            raise

    def _verify_build(self):
        module = self.memory.modules.get("gameassembly.dll")
        if not module:
            raise MemoryReadError("游戏尚未载入装备数据，请进入主城后重试")
        root = self.memory.path.parent
        if Path(module["path"]).parent != root:
            raise MemoryReadError("游戏模块路径与进程不一致")
        from .game_compatibility import resolve, type_rows
        result = resolve(root, Path(module['path']), self.on_compatibility)
        self.metadata = result['metadata']
        self.resolved_profiles = result['profiles']
        self.profile = result['profiles']['runtime-type-hints.json']
        self.types = {t.get('logicalName',t['name']):t for t in self.profile['types'] if not t['namespace']}
        self.types.update({t.get('logicalName',t['name']):t for t in type_rows(result['profiles']['ui-runtime-hints.json'])})
        self._transfer_profiles = {t.get('logicalName',t['name']):t for t in type_rows(result['profiles']['container-transfer-types.json'])}
        self._transfer_verified = set()
        self._carriage_profiles = {t.get('logicalName',t['name']):t for t in type_rows(result['profiles']['carriage-types.json'])}
        self.native_layout = result['native_layout']
        self.compatibility_bindings = result['bindings']
        self.compatibility_status = result['status']
        self.file_stamps = result['file_stamps']
        self.module = module

    def check_game_files(self):
        from .game_compatibility import CompatibilityError
        assets = {p.name for p in (self.memory.path.parent/'Deskrawl_Data').glob('*.assets')}
        expected_assets = {Path(p).name for p in self.file_stamps if p.endswith('.assets')}
        if assets != expected_assets:
            self.compatibility_status = {'phase':'changed','message':'游戏资源文件已变化，请重新连接检测。'}
            self.on_compatibility(self.compatibility_status)
            raise CompatibilityError('连接期间游戏资源文件有增减，请重新连接')
        for filename, expected in self.file_stamps.items():
            try:
                current = Path(filename).stat()
                unchanged = (current.st_size,current.st_mtime_ns) == expected
            except OSError:
                unchanged = False
            if not unchanged:
                self.compatibility_status = {'phase':'changed','message':'游戏文件已更新，请断开并重新连接，自动检测兼容性。'}
                self.on_compatibility(self.compatibility_status)
                raise CompatibilityError('连接期间游戏文件已变化，请重新连接')

    def _locate_classes(self):
        p = self.memory
        regions = list(p.regions())
        meta_bases = []
        for region in regions:
            if region.kind != 0x1000000 and region.size >= len(self.metadata):
                try:
                    if p.read(region.base, 64) == self.metadata[:64]:
                        meta_bases.append(region.base)
                except MemoryReadError:
                    pass
        if len(meta_bases) != 1:
            raise MemoryReadError("尚未找到唯一的游戏类型表，请进入角色后重试")
        self.metadata_base = meta_bases[0]
        string_offset = struct.unpack_from("<I", self.metadata, 32)[0]
        type_offset = struct.unpack_from("<I", self.metadata, 236)[0]
        names = {}
        for name in getattr(self,'required_classes',REQUIRED_CLASSES):
            index = self.types[name]["typeDefinitionIndex"]
            name_index = struct.unpack_from("<I", self.metadata, type_offset + index * 76)[0]
            names[name] = self.metadata_base + string_offset + name_index
        candidates = {name: set() for name in names}
        scanned = 0
        start = time.monotonic()
        for region in regions:
            if region.kind != 0x20000:
                continue
            for offset in range(0, region.size, 8 * 1024 * 1024):
                if time.monotonic() - start > 20 or scanned > 1024 * 1024 * 1024:
                    raise MemoryReadError("装备对象定位超时；请打开背包后重新连接")
                size = min(region.size - offset, 8 * 1024 * 1024)
                try:
                    block = p.read(region.base + offset, size)
                except MemoryReadError:
                    continue
                scanned += size
                for name, name_pointer in names.items():
                    needle = struct.pack("<Q", name_pointer)
                    pos = -1
                    while True:
                        pos = block.find(needle, pos + 1)
                        if pos < 0:
                            break
                        address = region.base + offset + pos - 16
                        if address & 7:
                            continue
                        try:
                            namespace = p.cstring(p.u64(address + 24))
                            image_name = p.cstring(p.u64(p.u64(address)))
                            if namespace == "" and image_name == "Assembly-CSharp.dll":
                                # Reject pointers to unrelated data with a matching name.
                                live = self.fields(address)
                                expected = {f["name"]: (f["rawRegistrationOffset"], int(f["token"], 16)) for f in self.types[name]["fields"]}
                                actual = {f["name"]: (f["offset"], f["token"]) for f in live}
                                if actual == expected:
                                    candidates[name].add(address)
                        except (MemoryReadError, UnicodeError):
                            pass
            if all(candidates.values()):
                break
        if any(len(rows) != 1 for rows in candidates.values()):
            missing = ", ".join(name for name, rows in candidates.items() if len(rows) != 1)
            raise MemoryReadError(f"装备对象入口未唯一确认：{missing}")
        self.classes = {name: next(iter(rows)) for name, rows in candidates.items()}
        self.discovery_seconds = time.monotonic() - start

    def fields(self, klass):
        if klass in self.field_cache:
            return self.field_cache[klass]
        p = self.memory
        count = struct.unpack("<H", p.read(klass + 0x124, 2))[0]
        if count > 256:
            raise MemoryReadError("类型字段数量异常")
        table = p.u64(klass + 128)
        out = []
        for i in range(count):
            name, type_pointer, parent, offset, token = struct.unpack("<QQQiI", p.read(table + 32 * i, 32))
            if parent != klass:
                raise MemoryReadError("类型字段声明对象不一致")
            out.append({"name": p.cstring(name), "offset": offset, "token": token})
        self.field_cache[klass] = out
        return out

    def _verify_fields(self):
        self.offsets = {}
        for name, klass in self.classes.items():
            live = {entry["name"]: entry for entry in self.fields(klass)}
            for field in self.types[name]["fields"]:
                actual = live.get(field["name"])
                if not actual or actual["offset"] != field["rawRegistrationOffset"] or actual["token"] != int(field["token"], 16):
                    raise MemoryReadError(f"{name} 的运行字段与已验证结构不一致")
            self.offsets[name] = {key: value["offset"] for key, value in live.items()}

    def class_name(self, klass):
        return self.memory.cstring(self.memory.u64(klass + 16))

    def string(self, address, max_length=256):
        if not address:
            return None
        if self.class_name(self.memory.u64(address)) != "String":
            raise MemoryReadError("字符串对象类型不一致")
        length = self.memory.i32(address + 16)
        if not 0 <= length <= max_length:
            raise MemoryReadError("物品字符串长度异常")
        return self.memory.read(address + 20, length * 2).decode("utf-16-le")

    def array(self, address, element_name, *, limit, stride):
        if not address:
            return [], b""
        p = self.memory
        klass = p.u64(address)
        if self.class_name(p.u64(klass + 64)) != element_name or p.i32(klass + 0x104) != stride:
            raise MemoryReadError(f"{element_name} 数组类型或元素长度不一致")
        count = p.u64(address + 24)
        if count > limit:
            raise MemoryReadError("物品集合超出读取上限")
        block = p.read(address + 32, count * stride)
        return [block[i:i + stride] for i in range(0, len(block), stride)], block

    def singleton(self, name):
        p = self.memory
        static = p.u64(self.classes[name] + 184)
        if not static:
            raise MemoryReadError(f"{name} 数据尚未初始化")
        obj = p.u64(static)
        if not obj or p.u64(obj) != self.classes[name]:
            raise MemoryReadError(f"{name} 当前实例尚未加载")
        return obj

    def registry(self):
        p = self.memory
        static = p.u64(self.classes["gj"] + 184)
        if not static:
            raise MemoryReadError("装备注册表尚未加载")
        root = p.u64(static)
        if not root or self.class_name(p.u64(root)) != "Dictionary`2":
            raise MemoryReadError("装备注册表类型不一致")
        offsets = {f["name"]: f["offset"] for f in self.fields(p.u64(root))}
        expected = {"_entries": 24, "_count": 32, "_freeCount": 40, "_version": 44}
        if any(offsets.get(k) != v for k, v in expected.items()):
            raise MemoryReadError("装备注册表字段布局不一致")
        # Copy all mutable dictionary counters together. In particular, do not
        # compare old entries with a freeCount read after decoding every UID.
        header = p.read(root + 24, 24)
        entries, count, free_list, free_count, version = struct.unpack("<Qiiii", header)

        def ensure_stable(block=None):
            if (p.u64(self.classes["gj"] + 184) != static or p.u64(static) != root or
                    p.read(root + 24, 24) != header or
                    (block is not None and p.read(entries + 32, len(block)) != block)):
                raise SnapshotChangedError("装备注册表在读取期间发生变化，请重新读取")

        if not 0 <= count <= 8192:
            ensure_stable()
            raise MemoryReadError("装备注册表大小异常")
        if not 0 <= free_count <= count:
            ensure_stable()
            raise MemoryReadError("装备注册表空闲条目数异常")
        stamp = {"object": root, "entries": entries, "version": version,
                 "count": count, "free_count": free_count, "header": header}
        if not entries:
            ensure_stable()
            if count:
                raise MemoryReadError("装备注册表缺少条目数组")
            self.snapshot_stamps.extend([(self.classes["gj"] + 184, struct.pack("<Q", static)),
                                         (static, struct.pack("<Q", root)), (root + 24, header)])
            return {}, stamp
        array_class = p.u64(entries)
        element = p.u64(array_class + 64)
        fields = {f["name"]: f["offset"] for f in self.fields(element)}
        if fields != {"hashCode": 16, "next": 20, "key": 24, "value": 32} or p.i32(array_class + 0x104) != 24:
            raise MemoryReadError("装备注册表条目布局不一致")
        if p.u64(entries + 24) < count:
            ensure_stable()
            raise MemoryReadError("装备注册表条目数超出数组长度")
        block = p.read(entries + 32, count * 24)
        ensure_stable(block)
        records = {}
        try:
            for index in range(count):
                hash_code, next_, key, value = struct.unpack_from("<iiQQ", block, index * 24)
                if hash_code < 0:
                    continue
                if not key or not value or p.u64(value) != self.classes["GeneratedItemData"]:
                    raise MemoryReadError("装备注册表含未确认的条目对象")
                uid = self.string(key)
                if not uid or uid in records:
                    raise MemoryReadError("装备注册表唯一标识为空或重复")
                records[uid] = value
        except (MemoryReadError, UnicodeError):
            ensure_stable(block)
            raise
        ensure_stable(block)
        if len(records) != count - free_count:
            raise MemoryReadError("装备注册表有效条目数不一致")
        self.snapshot_stamps.extend([(self.classes["gj"] + 184, struct.pack("<Q", static)),
                                     (static, struct.pack("<Q", root)), (root + 24, header)])
        if block:
            self.snapshot_stamps.append((entries + 32, block))
        return records, stamp

    def asset_name(self, item):
        p = self.memory
        native = p.u64(item + 16)
        if not native:
            raise MemoryReadError("物品基础对象已被卸载")
        # Unity native object name starts at +56 in this verified build.
        # Confirm the asset key against the shipped catalog before using it.
        block = p.read(native + 56, 32)
        inline = block.split(b"\0", 1)[0]
        try:
            name = inline.decode("ascii")
            if name and all(c.isalnum() or c in "_-." for c in name):
                return name
        except UnicodeError:
            pass
        pointer = struct.unpack_from("<Q", block)[0]
        name = p.cstring(pointer, 128)
        if name and all(c.isalnum() or c in "_-." for c in name):
            return name
        raise MemoryReadError("物品资源名称尚未解析")

    def base_item(self, item):
        p = self.memory
        if item in self.item_cache:
            return self.item_cache[item]
        klass = p.u64(item)
        parent = klass
        for _ in range(8):
            if parent == self.classes["ItemData"]:
                break
            parent = p.u64(parent + 88)
            if not parent:
                raise MemoryReadError("槽位中的物品基础类型不一致")
        else:
            raise MemoryReadError("物品继承层次超出限制")
        name = self.asset_name(item)
        out = {"item_class": self.class_name(klass), "internal_name": name,
            "name_key": name if name in self.catalog.equipment else None,
            "is_equipment": name in self.catalog.equipment, "item_type": p.i32(item + 32),
            "equip_slot": p.i32(item + 144), "base_rarity": p.i32(item + 36),
            "max_stack": p.i32(item + 136), "class_mask": p.i32(item + 152)}
        self.item_cache[item] = out
        return out

    def generated(self, obj):
        p = self.memory
        header = p.read(obj, 128)
        if struct.unpack_from("<Q", header)[0] != self.classes["GeneratedItemData"]:
            raise MemoryReadError("装备实例类型不一致")
        mods_ptr = struct.unpack_from("<Q", header, 24)[0]
        records, block = self.array(mods_ptr, "StatModifier", limit=64, stride=28)
        modifiers = []
        for record in records:
            stat, kind = struct.unpack_from("<ii", record)
            if stat not in self.stat_names or kind not in (0, 1, 2):
                raise MemoryReadError("装备词条枚举不在已验证版本中")
            modifiers.append({"stat": stat, "stat_name": self.stat_names[stat], "type": kind,
                "value": decode_obscured_float(record[8:28]), "source": "generated_modifiers", "display_unit": None})
        if mods_ptr and p.read(mods_ptr + 32, len(block)) != block:
            raise SnapshotChangedError("装备词条在读取期间发生变化")
        instance = self.string(struct.unpack_from("<Q", header, 80)[0])
        gem_ptr = struct.unpack_from("<Q", header, 64)[0]
        gems, gem_block = self.array(gem_ptr, "String", limit=64, stride=8)
        gem_ids = [self.string(struct.unpack('<Q', raw)[0]) or '' for raw in gems]
        if gem_ptr:
            self.snapshot_stamps.append((gem_ptr + 24, struct.pack('<Q', len(gems))))
            if gem_block:
                self.snapshot_stamps.append((gem_ptr + 32, gem_block))
        if p.read(obj, 128) != header:
            raise SnapshotChangedError("装备实例在读取期间发生变化")
        self.snapshot_stamps.append((obj, header))
        if mods_ptr and block:
            self.snapshot_stamps.append((mods_ptr + 32, block))
        return {"instance_id": instance, "item_level": struct.unpack_from("<i", header, 16)[0],
            "rarity": struct.unpack_from("<i", header, 20)[0], "modifiers": modifiers, "modifiers_complete": True,
            "upgrade_level": struct.unpack_from("<i", header, 32)[0], "socket_count": struct.unpack_from("<i", header, 56)[0],
            "socketed_gems": gem_ids,
            "locked": bool(header[73]), "is_ancient": bool(header[72]), "is_black_mist": bool(header[105]),
            "bound": bool(header[120])}

    def container(self, name, registry, max_items):
        p = self.memory
        root = self.singleton(name)
        offset = 56 if name == "Inventory" else 48
        array_ptr = p.u64(root + offset)
        entries, block = self.array(array_ptr, "InventorySlot", limit=max_items, stride=8)
        out = {"available": True, "slot_count": len(entries), "complete": True, "slots": [], "issues": []}
        observations = []
        for index, raw in enumerate(entries):
            slot = struct.unpack("<Q", raw)[0]
            if not slot:
                continue
            if p.u64(slot) != self.classes["InventorySlot"]:
                raise MemoryReadError("物品槽位类型不一致")
            header = p.read(slot, 48)
            item = struct.unpack_from("<Q", header, 16)[0]
            if not item:
                continue
            row = {"container": "inventory" if name == "Inventory" else "storage", "slot_index": index,
                "count": struct.unpack_from("<i", header, 24)[0], "item_uid": self.string(struct.unpack_from("<Q", header, 32)[0]),
                "slot_locked": bool(header[40])}
            try:
                row.update(self.base_item(item))
                if row["item_uid"] in registry:
                    row.update(self.generated(registry[row["item_uid"]]))
                elif row["is_equipment"]:
                    raise MemoryReadError("装备唯一 ID 未在当前注册表中找到")
                else:
                    row["locked"] = row["slot_locked"]
                    row["modifiers"] = []
                row["transfer_key"] = self.transfer_key(row['container'],index,slot,header,row)
                if p.read(slot, 48) != header:
                    raise SnapshotChangedError("槽位在读取期间发生变化")
                observations.append((slot, header))
            except SnapshotChangedError:
                raise
            except (MemoryReadError, UnicodeError) as exc:
                row["issues"] = [str(exc)]
                out["complete"] = False
                out["issues"].append(f"格子 {index + 1}：{exc}")
            out["slots"].append(row)
        # Detect sorting/moving/replacing throughout the entire snapshot read.
        if p.u64(root + offset) != array_ptr or (array_ptr and p.read(array_ptr + 32, len(block)) != block):
            raise SnapshotChangedError("物品集合在读取期间发生变化，请重新读取")
        if any(p.read(slot, len(header)) != header for slot, header in observations):
            raise SnapshotChangedError("物品槽位在读取期间发生变化，请重新读取")
        out["occupied_count"] = len(out["slots"])
        out["equipment_count"] = sum(row.get("is_equipment") is True for row in out["slots"])
        self.snapshot_stamps.extend(observations)
        self.snapshot_stamps.append((root + offset, struct.pack("<Q", array_ptr)))
        if array_ptr and block:
            self.snapshot_stamps.append((array_ptr + 32, block))
        out["slots_complete"] = out["complete"]
        return out

    def transfer_key(self, container, index, slot, header, row):
        """Session-scoped identity for stacks without generated equipment IDs."""
        identity = f"{self.session_id}:{container}:{index}:{slot}:{row.get('internal_name')}:{row.get('instance_id','')}"
        return hashlib.sha256(identity.encode('utf-8')+header).hexdigest()

    def diagnostics(self):
        return {"pid": self.pid, "backend": "external_read_only", "bridge_version": "0.2.0-external",
            "bridge_session": self.session_id, "module": {"path": self.module["path"]},
            "metadata_sha256": self.profile["metadataSha256"], "classes": list(self.classes),
            "compatibility": self.compatibility_status,
            "discovery_seconds": self.discovery_seconds, "capabilities": {"read_only": True, "injection": False, "writes": False}}

    def snapshot(self, max_items=2048):
        self.check_game_files()
        started = time.monotonic()
        p = self.memory
        self.capture += 1
        self.snapshot_stamps = []
        # Asset references are cached only within this snapshot, so unloading
        # and reusing an address across map/character changes cannot reuse a name.
        self.item_cache = {}
        registry, stamp = self.registry()
        result = {"schema_version": 1, "pid": self.pid, "bridge_session": self.session_id,
            "snapshot_id": f"{self.session_id}-{self.capture}", "backend": "external_read_only",
            "metadata_sha256": self.profile["metadataSha256"], "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "containers": {}, "issues": [], "read_evidence": {"enum_values_verified": True}}
        for name, key in (("Inventory", "inventory"), ("Storage", "storage")):
            try:
                result["containers"][key] = self.container(name, registry, max_items)
                result["issues"].extend(f"{key}: {issue}" for issue in result["containers"][key]["issues"])
            except SnapshotChangedError:
                raise
            except (MemoryReadError, UnicodeError) as exc:
                result["containers"][key] = {"available": False, "complete": False, "slots": [], "issues": [str(exc)]}
                result["issues"].append(f"{key}: {exc}")
        if p.read(stamp["object"] + 24, 24) != stamp["header"]:
            raise SnapshotChangedError("装备注册表在读取期间发生变化，请重新读取")
        result["registry_count"] = len(registry)
        if any(p.read(address, len(block)) != block for address, block in self.snapshot_stamps):
            raise SnapshotChangedError("物品内容在读取期间发生变化，请重新读取")
        result["complete"] = not result["issues"]
        try:
            from .container_transfer import access_state
            result['transfer'], _ = access_state(self)
        except (MemoryReadError, KeyError, OSError, ValueError) as exc:
            result['transfer'] = {'available':False,'reason':str(exc)}
        for container in result["containers"].values():
            for row in container["slots"]:
                ready = result["complete"] and row.get("is_equipment") is True and bool(row.get("instance_id")) and not row.get("issues")
                row["read_evidence"] = {"identity_verified": ready, "slot_verified": ready, "lock_verified": ready,
                    "enum_values_verified": True, "modifiers_complete": ready and row.get("modifiers_complete") is True,
                    "native_affixes_verified": ready and not row.get("is_black_mist", True),
                    "snapshot_consistent": result["complete"]}
        result["duration_seconds"] = time.monotonic() - started
        return result

    def describe_class(self, name):
        if name not in self.classes:
            raise MemoryReadError("只允许查看装备读取所需的已验证类型")
        return {"class_name": name, "fields": self.fields(self.classes[name])}

    def hovered_inventory(self):
        """Read the UI's actual hover selection, not a guessed screen location."""
        p = self.memory
        self.snapshot_stamps = []
        static = p.u64(self.classes["InventorySlotUI"] + 184)
        hover = p.u64(static) if static else 0
        if not hover:
            return None
        if p.u64(hover) != self.classes["InventorySlotUI"]:
            raise MemoryReadError("当前悬停格子类型不一致")
        owner = p.u64(hover + 128)
        inventory_ui = self.singleton("InventoryUI")
        if owner != inventory_ui:
            raise MemoryReadError("当前悬停格子不属于角色背包")
        index = p.i32(hover + 120)
        root = self.singleton("Inventory")
        array = p.u64(root + 56)
        count = p.u64(array + 24)
        if not 0 <= index < count:
            raise MemoryReadError("悬停格子索引超出背包")
        slot = p.u64(array + 32 + index * 8)
        if not slot or p.u64(slot) != self.classes["InventorySlot"]:
            raise MemoryReadError("悬停格子与物品槽位不对应")
        slot_header = p.read(slot, 48)
        item = struct.unpack_from("<Q", slot_header, 16)[0]
        if not item:
            return {"slot_index": index, "empty": True}
        uid = self.string(struct.unpack_from("<Q", slot_header, 32)[0])
        registry, _ = self.registry()
        if uid not in registry:
            return {"slot_index": index, "item_uid": uid, **self.base_item(item), "locked": bool(p.read(slot + 40, 1)[0])}
        generated = self.generated(registry[uid])
        if p.u64(static) != hover or p.i32(hover + 120) != index or p.read(slot, 48) != slot_header:
            raise MemoryReadError("悬停目标在读取期间改变")
        return {"slot_index": index, "item_uid": uid, **self.base_item(item), **generated}

    def close(self):
        self.memory.close()
