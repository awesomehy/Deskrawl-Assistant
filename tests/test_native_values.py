"""Golden native values checked against the user's in-game tooltip."""
import unittest
from deskrawl_assistant.native_reader import decode_obscured_float
from deskrawl_assistant.native_memory import MemoryReadError


class NativeValueTests(unittest.TestCase):
    def test_tooltip_verified_dexterity(self):
        self.assertEqual(decode_obscured_float(bytes.fromhex('9da9f843b4e2e40ab4e464480000000000000000')), 67.0)

    def test_tooltip_verified_life_on_hit(self):
        self.assertEqual(decode_obscured_float(bytes.fromhex('f3ccd56b0c735b220c5b93630000000000000000')), 28.0)

    def test_changed_value_is_rejected_without_matching_checksum(self):
        data = bytearray.fromhex('9da9f843b4e2e40ab4e464480000000000000000')
        data[4] ^= 1
        with self.assertRaises(MemoryReadError):
            decode_obscured_float(bytes(data))

    def test_partial_value_is_rejected(self):
        with self.assertRaises(MemoryReadError):
            decode_obscured_float(b'\x00' * 16)


if __name__ == '__main__':
    unittest.main()
