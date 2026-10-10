"""Bounded, read-only IL2CPP v39 and PE64 parsing; never loads the binary.

Format references: LibCpp2IL Metadata and BinaryStructures in Cpp2IL.
"""
from __future__ import annotations
from pathlib import Path
import struct

PRIMITIVES = {
    1: "System.Void", 2: "System.Boolean", 3: "System.Char", 4: "System.SByte",
    5: "System.Byte", 6: "System.Int16", 7: "System.UInt16", 8: "System.Int32",
    9: "System.UInt32", 10: "System.Int64", 11: "System.UInt64",
    12: "System.Single", 13: "System.Double", 14: "System.String",
    24: "System.IntPtr", 25: "System.UIntPtr", 28: "System.Object",
}
class PE:
    def __init__(self, path: Path):
        self.data = bytes(path) if isinstance(path, (bytes, bytearray)) else path.read_bytes()
        if len(self.data) < 256 or self.data[:2] != b"MZ":
            raise ValueError("Not a PE file")
        p = struct.unpack_from("<I", self.data, 0x3C)[0]
        if p > len(self.data)-256 or self.data[p:p + 4] != b"PE\0\0":
            raise ValueError("Invalid PE signature")
        machine, sections = struct.unpack_from("<HH", self.data, p + 4)
        optional_size = struct.unpack_from("<H", self.data, p + 20)[0]
        optional = p + 24
        if not 1<=sections<=96 or optional_size<240 or optional+optional_size+sections*40>len(self.data):
            raise ValueError('Invalid PE section table')
        if machine != 0x8664 or struct.unpack_from("<H", self.data, optional)[0] != 0x20B:
            raise ValueError("Only verified Windows PE64 is supported")
        self.base = struct.unpack_from("<Q", self.data, optional + 24)[0]
        self.image_size = struct.unpack_from('<I', self.data, optional+56)[0]
        self.headers_size = struct.unpack_from('<I',self.data,optional+60)[0]
        self.directories = [struct.unpack_from('<II',self.data,optional+112+j*8) for j in range(16)]
        self.sections = []
        self.section_flags = {}
        for index in range(sections):
            position = optional + optional_size + index * 40
            virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", self.data, position + 8)
            if raw_offset + raw_size > len(self.data):
                raise ValueError("PE section outside file")
            self.sections.append((rva, virtual_size, raw_offset, raw_size))
            self.section_flags[rva] = struct.unpack_from('<I',self.data,position+36)[0]

    def raw(self, va: int, length: int = 1) -> int:
        if length<1:raise ValueError('Invalid PE read size')
        rva = va - self.base
        if 0<=rva and rva+length<=min(self.headers_size,len(self.data)):return rva
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

    def contains(self, va, size=1):
        if self.base<=va and va+size<=self.base+self.headers_size:return True
        return any(self.base+s<=va and va+size<=self.base+s+max(v,n) for s,v,_,n in self.sections)

    def cstring(self, va):
        at=self.raw(va);end=self.data.find(b'\0',at,at+1024)
        if end<0:raise ValueError('Unterminated PE string')
        return self.data[at:end].decode('utf-8')

    def exports(self):
        rva,size=self.directories[0]
        if not rva or not size:raise ValueError('Missing exports')
        at=self.raw(self.base+rva,40)
        count,names,functions,name_table,ordinals=struct.unpack_from('<IIIII',self.data,at+20)
        if not 0<names<=count<=100000:raise ValueError('Invalid export count')
        result={}
        for index in range(names):
            name=self.cstring(self.base+self.unpack('<I',self.base+name_table+index*4)[0])
            ordinal=self.unpack('<H',self.base+ordinals+index*2)[0]
            if ordinal>=count:raise ValueError('Invalid export ordinal')
            function=self.unpack('<I',self.base+functions+ordinal*4)[0]
            if rva<=function<rva+size:raise ValueError('Forwarded exports need maintenance')
            result[name]=self.base+function
        return result

    def unwind_functions(self):
        rva,size=self.directories[3]
        if not rva or size%12:raise ValueError('Invalid x64 unwind directory')
        at=self.raw(self.base+rva,size)
        result={}
        for start,end,_ in struct.iter_unpack('<III',self.data[at:at+size]):
            if not 0<start<end<=self.image_size:raise ValueError('Invalid native function range')
            result[self.base+start]=self.base+end
        return result

    def imports(self):
        rva,size=self.directories[1]
        result={}
        if not rva:return result
        for n in range(min(size//20,4096)):
            lookup,_,_,name,iat=self.unpack('<5I',self.base+rva+n*20)
            if not any((lookup,name,iat)):return result
            library=self.cstring(self.base+name).lower()
            for j in range(100000):
                entry=self.unpack('<Q',self.base+(lookup or iat)+j*8)[0]
                if not entry:break
                symbol=str(entry&0xffff) if entry>>63 else self.cstring(self.base+entry+2)
                result[self.base+iat+j*8]=library+'!'+symbol
            else:raise ValueError('Import table exceeds bounds')
        raise ValueError('Unterminated import descriptors')


class MetadataInspector:
    def __init__(self, metadata_path: Path, binary_path: Path):
        self.meta = bytes(metadata_path) if isinstance(metadata_path, (bytes, bytearray)) else metadata_path.read_bytes()
        if len(self.meta) < 380 or struct.unpack_from("<II", self.meta) != (0xFAB11BAF, 39):
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
        self.field_cache = {}
        self.method_cache = {}
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
        if index in self.field_cache:return self.field_cache[index]
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
        self.field_cache[index]=result
        return result

    def methods(self, index: int):
        if index in self.method_cache:return self.method_cache[index]
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
        self.method_cache[index]=result
        return result

    def images(self):
        offset,_,count=self.section(248,36)
        result=[]
        for index in range(count):
            at=offset+index*36
            name=self.string(struct.unpack_from('<i',self.meta,at)[0])
            start,size=struct.unpack_from('<HI',self.meta,at+8)
            if not 0<=start<=self.type_count-size:raise ValueError('Image type range changed')
            result.append({'name':name,'start':start,'count':size})
        return result

    def literal(self, index):
        table,_,count=self.section(8,4);data,size,_=self.section(20)
        if not 0<=index<count-1:raise ValueError('String literal index outside table')
        start,end=struct.unpack_from('<II',self.meta,table+index*4)
        if not 0<=start<=end<=size:raise ValueError('String literal outside data')
        return self.meta[data+start:data+end]
