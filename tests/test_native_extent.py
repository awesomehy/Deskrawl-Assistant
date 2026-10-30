import struct
import unittest

from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from deskrawl_assistant.il2cpp_metadata import PE
from deskrawl_assistant.native_signatures import Signatures


class NativeExtentTests(unittest.TestCase):
    def pe(self, entries, parents):
        pe = PE.__new__(PE)
        data = bytearray(4096)
        pe.base, pe.image_size = 0x180000000, len(data)
        pe.directories = [(0,0)]*16
        pe.directories[3] = (0x100, len(entries)*12)
        pe.raw = lambda address, size=1: address-pe.base
        for index, row in enumerate(entries): struct.pack_into('<III',data,0x100+index*12,*row)
        for unwind, parent in parents.items():
            # Odd unwind-code count requires four-byte alignment for the chain.
            struct.pack_into('<4B',data,unwind,0x21,0,1,0)
            struct.pack_into('<III',data,unwind+8,*parent)
        pe.data = bytes(data)
        return pe

    def test_chained_unwind_includes_all_fragments_and_rejects_cycles(self):
        pe = self.pe([(0x500,0x540,0x300),(0x540,0x580,0x320),(0x580,0x600,0x340)],
            {0x320:(0x500,0x540,0x300), 0x340:(0x540,0x580,0x320)})
        self.assertEqual(pe.unwind_functions()[pe.base+0x500], pe.base+0x600)
        pe = self.pe([(0x500,0x540,0x300),(0x540,0x580,0x320)],
            {0x300:(0x540,0x580,0x320),0x320:(0x500,0x540,0x300)})
        with self.assertRaisesRegex(ValueError,'Cyclic'): pe.unwind_functions()

    def signatures(self, code):
        value = Signatures.__new__(Signatures)
        value.pe = self.pe([], {})
        value.pe.data = code
        value.pe.base = 0
        value.pe.raw = lambda address, size=1: address
        value.dis = Cs(CS_ARCH_X86,CS_MODE_64)
        value.ends = {0:10, 16:len(code)}
        value.starts = [0,16]
        value.body_cache, value.trap_stubs = {}, {}
        return value

    def test_trap_call_ends_reachable_code_but_returning_helpers_do_not(self):
        # Call 16; bytes after it resemble executable code / a jump table.
        head = b'\xe8\x0b\0\0\0\x90\xc3\xcc\xcc\xcc' + b'\xcc'*6
        trap = self.signatures(head+b'\x90\xcc')
        self.assertEqual([i.mnemonic for i in trap.instructions(0)],['call'])
        returning = self.signatures(head+b'\xc3\xcc')
        self.assertEqual([i.mnemonic for i in returning.instructions(0)],['call','nop','ret'])
        branching = self.signatures(head+b'\x75\0\xcc')
        self.assertFalse(branching.traps_without_return(16))
