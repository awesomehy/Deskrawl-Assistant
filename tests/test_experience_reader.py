import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from deskrawl_assistant.experience_reader import (
    GAME_MANAGER_RVA, PLAYER_DATA_RVA, _hash32, _profile,
    cumulative_xp, decode_obscured_int, decode_obscured_long,
    read_experience, xp_requirement,
)
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError

CONFIG = {'MobsToLevelAtLevel1': 2, 'MobsToLevelAtLevel5': 35,
          'MobsToLevelAtLevel15': 900, 'MobsToLevelAtLevel50': 20000,
          'MobsToLevelAtMaxLevel': 50000, 'MaxPlayerLevel': 70,
          'XpPerBaseHealth': 1.0, 'XpPerLevelScale': 0.10000000149011612}


def obscured(value, width):
    bits = value & ((1 << width) - 1)
    key = 0xF001C033 if width == 32 else 0xEEDDC0FFEAF00123
    hidden = ((bits ^ key) + key) & ((1 << width) - 1)
    raw = bits.to_bytes(width // 8, 'little')
    checksum = _hash32(raw[:4])
    if width == 32:
        return struct.pack('<IIII', checksum | 1, hidden, key, 0)
    signed = checksum if checksum < 0x80000000 else checksum - 0x100000000
    checksum = ((signed >> 2) & 0xFFFFFFFF) ^ (_hash32(raw[4:]) | 1)
    return struct.pack('<I4xQQQ', checksum, hidden, key, 0)


class FakeMemory:
    def __init__(self):
        self.data = {}
    def put(self, address, raw):
        for offset, byte in enumerate(raw):
            self.data[address + offset] = byte
    def read(self, address, size):
        try:
            return bytes(self.data[address + offset] for offset in range(size))
        except KeyError as exc:
            raise MemoryReadError('synthetic address missing') from exc


class FakeReader:
    def __init__(self):
        profile = _profile()
        self.profile = profile
        self.session_id = 'test-session'
        self.module = {'base': 0x10000000}
        self.memory = FakeMemory()
        self.profiles = {entry['name']: entry for entry in profile['types']}
        self.classes = {name: 0x20000000 + index * 4096
                        for index, name in enumerate(self.profiles)}
        self.objects = {name: 0x30000000 + index * 4096
                        for index, name in enumerate(self.profiles)}
        p = self.memory
        for name, klass in self.classes.items():
            p.put(self.objects[name], struct.pack('<Q', klass))
        for rva, name in ((GAME_MANAGER_RVA, 'GameManager'), (PLAYER_DATA_RVA, 'PlayerData')):
            static = self.classes[name] + 512
            p.put(self.module['base'] + rva, struct.pack('<Q', self.classes[name]))
            p.put(self.classes[name] + 184, struct.pack('<Q', static))
            p.put(static, struct.pack('<Q', self.objects[name]))
        manager, player = self.objects['GameManager'], self.objects['PlayerData']
        p.put(manager + 32, struct.pack('<Q', self.objects['GameConfig']))
        p.put(manager + 168, struct.pack('<i', 1))
        p.put(player + 40, struct.pack('<Q', 0x40000000))
        p.put(player + 64, obscured(47, 32) + obscured(123456, 64))
        p.put(self.objects['SaveSystem'] + 32, struct.pack('<ii', 1, 0))
        p.put(self.objects['GameConfig'] + 244, struct.pack('<iiiiiiff', *CONFIG.values()))
        p.put(manager + 112, struct.pack('<QQ', self.objects['MapData'], self.objects['RunPlan']))
        p.put(self.objects['RunPlan'] + 16,
              struct.pack('<QQQiiQ', 0x40001000, 0x40002000, 0, 47, 0, 0x40003000))
        self.strings = {0x40000000: 'Test hero', 0x40001000: 'private-run-token',
                        0x40002000: 'RedForest4', 0x40003000: 'Normal'}
    def singleton(self, name):
        return self.objects[name]
    def class_name(self, klass):
        return next(name for name, value in self.classes.items() if value == klass)
    def fields(self, klass):
        name = self.class_name(klass)
        return [{'name': field['name'], 'offset': field['rawRegistrationOffset'],
                 'token': int(field['token'], 16)} for field in self.profiles[name]['fields']]
    def string(self, address):
        return self.strings[address]
    def asset_name(self, address):
        return 'RedForest4'


class ExperienceReaderTests(unittest.TestCase):
    def test_verified_copied_wrappers(self):
        self.assertEqual(decode_obscured_int(bytes.fromhex('91380f5daf630087ed31804300000000')), 47)
        self.assertEqual(decode_obscured_long(bytes.fromhex(
            'eefa3c2d0000000006b651d747c345cb3423bbeba3e1a2650000000000000000')), 2994662)

    def test_wraparound_and_signed_values(self):
        for value in (0, 1, -1, 0x7FFFFFFF, -0x80000000):
            self.assertEqual(decode_obscured_int(obscured(value, 32)), value)
        for value in (0, 1, -1, 0x7FFFFFFFFFFFFFFF, -0x8000000000000000):
            self.assertEqual(decode_obscured_long(obscured(value, 64)), value)

    def test_checksum_corruption_and_lengths(self):
        for decoder, raw in ((decode_obscured_int, obscured(47, 32)),
                             (decode_obscured_long, obscured(123456, 64))):
            with self.assertRaises(MemoryReadError):
                decoder(bytes([raw[0] ^ 1]) + raw[1:])
            with self.assertRaises(MemoryReadError):
                decoder(raw[:-1])
        self.assertEqual(decode_obscured_int(bytes(16)), 0)
        self.assertEqual(decode_obscured_long(bytes(32)), 0)

    def test_native_formula_anchors_and_interpolation(self):
        expected = {1: 200, 2: 450, 4: 2225, 5: 4900, 6: 7264,
                    14: 149607, 15: 216000, 16: 245845, 47: 8585767,
                    49: 10616426, 50: 11800000, 51: 12562563, 69: 37253545, 70: 0}
        for level, needed in expected.items():
            self.assertEqual(xp_requirement(level, CONFIG), needed)

    def test_float32_per_mob_rounding(self):
        config = {**CONFIG, 'XpPerBaseHealth': 0.1, 'XpPerLevelScale': 0.0031415926535}
        # The native mulss result is 11 at level 47; double-only multiplication
        # would use an unrounded per-mob value and alter the level requirement.
        self.assertEqual(xp_requirement(47, config), 168649)

    def test_upgrade_preserves_exact_gain(self):
        before = cumulative_xp(47, xp_requirement(47, CONFIG) - 100, CONFIG)
        after = cumulative_xp(48, 250, CONFIG)
        self.assertEqual(after - before, 350)
        after_two = cumulative_xp(49, 500, CONFIG)
        self.assertEqual(after_two - before, 600 + xp_requirement(48, CONFIG))

    def test_out_of_range_and_max_level_not_zero(self):
        with self.assertRaises(SnapshotChangedError):
            cumulative_xp(47, xp_requirement(47, CONFIG), CONFIG)
        for level, xp in ((0, 1), (70, 0), (71, 0), (47, -1)):
            with self.assertRaises(ValueError):
                cumulative_xp(level, xp, CONFIG)

    def test_public_snapshot_and_private_run_id(self):
        result = read_experience(FakeReader())
        self.assertTrue(result['available'], result)
        self.assertEqual(result['level'], 47)
        self.assertEqual(result['current_xp'], 123456)
        self.assertEqual(result['stage'], 'RedForest4')
        self.assertEqual(result['character'], '1:0:Test hero')
        self.assertNotIn('private-run-token', json.dumps(result))
        self.assertEqual(len(result['run_id']), 64)

    def test_main_menu_is_unavailable(self):
        reader = FakeReader()
        reader.memory.put(reader.objects['GameManager'] + 168, struct.pack('<i', 0))
        self.assertFalse(read_experience(reader)['available'])

    def test_game_version_rejected(self):
        reader = FakeReader()
        reader.profile = {**reader.profile, 'metadataSha256': 'different'}
        self.assertFalse(read_experience(reader)['available'])

    def test_type_identity_rejected(self):
        reader = FakeReader()
        reader.memory.put(reader.objects['PlayerData'], struct.pack('<Q', reader.classes['RunPlan']))
        self.assertFalse(read_experience(reader)['available'])

    def test_transition_not_counted_as_zero(self):
        reader = FakeReader()
        reader.asset_name = lambda address: 'DifferentMap'
        result = read_experience(reader)
        self.assertFalse(result['available'])
        self.assertNotIn('total_xp', result)

    def test_max_level_falls_back_without_fake_gain(self):
        reader = FakeReader()
        reader.memory.put(reader.objects['PlayerData'] + 64,
                          obscured(70, 32) + obscured(0, 64))
        result = read_experience(reader)
        self.assertFalse(result['available'])
        self.assertIn('巅峰', result['reason'])


if __name__ == '__main__':
    unittest.main()
