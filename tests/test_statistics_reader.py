import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.statistics_reader import _totals, _gold_pickup_offset, read_gold_ledger, read_player_statistics


class NativeStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.fields = [{'name': 'duration', 'offset': 40, 'token': 1}, {'name': 'counts', 'offset': 72, 'token': 2}]
        self.layout = {'rva': 100, 'duration': 40, 'counts': 72, 'fields': self.fields,
                       'rarity': {'Common': 0, 'Uncommon': 1, 'Rare': 2, 'Legendary': 3, 'Divine': 4, 'RuneSet': 5}}
        self.reader = SimpleNamespace(module={'base': 1000}, session_id='current-session',
            class_name=lambda klass: 'PlayerStatistics', fields=lambda klass: self.fields,
            check_game_files=lambda: None)
        self.duration = 123456.9
        self.counts = [10000, 2000, 900, 30, 17, 9]
        self.pointers = {1100: 2000, 2184: 3000, 3000: 4000, 4000: 2000, 4072: 5000}
        self.copy = SimpleNamespace(ptr=lambda address: self.pointers[address],
            read=lambda address, size, stable=True: struct.pack('<d', self.duration),
            array=lambda *args, **kwargs: [struct.pack('<i', count) for count in self.counts])

    def test_game_seconds_and_exact_rarity_counters(self):
        self.assertEqual(_totals(self.reader, self.copy, self.layout),
                         {'play_seconds': 123456, 'divine_count': 17, 'rune_set_count': 9})

    def test_changed_live_fields_or_array_length_never_guess(self):
        self.reader.fields = lambda klass: self.fields[::-1]
        with self.assertRaises(MemoryReadError): _totals(self.reader, self.copy, self.layout)
        self.reader.fields = lambda klass: self.fields
        self.counts = self.counts[:-1]
        with self.assertRaises(MemoryReadError): _totals(self.reader, self.copy, self.layout)

    def test_null_instance_invalid_time_and_negative_counter_are_rejected(self):
        self.pointers[3000] = 0
        with self.assertRaises(MemoryReadError): _totals(self.reader, self.copy, self.layout)
        self.pointers[3000] = 4000
        for duration in (float('nan'), float('inf'), -1):
            self.duration = duration
            with self.subTest(duration=duration), self.assertRaises(MemoryReadError):
                _totals(self.reader, self.copy, self.layout)
        self.duration = 0
        self.counts[4] = -1
        with self.assertRaises(MemoryReadError): _totals(self.reader, self.copy, self.layout)

    def test_changing_character_retries_whole_read(self):
        result = {'available': True, 'session': 'current-session', 'divine_count': 17}
        with patch('deskrawl_assistant.statistics_reader._layout', return_value=self.layout), \
             patch('deskrawl_assistant.statistics_reader._snapshot', side_effect=[SnapshotChangedError('Switching'), result]) as read:
            self.assertEqual(read_player_statistics(self.reader), result)
            self.assertEqual(read.call_count, 2)

    def test_unavailable_or_changed_files_do_not_return_zero_totals(self):
        self.reader.check_game_files = lambda: (_ for _ in ()).throw(MemoryReadError('Updated files'))
        result = read_player_statistics(self.reader)
        self.assertFalse(result['available'])
        self.assertNotIn('play_seconds', result); self.assertNotIn('divine_count', result)

    def gold(self, total=9876543210, character='hero-1', finish=None):
        self.layout['gold'] = 48
        def read(address, size, stable=True):
            self.assertEqual((address, size), (4048, 8))
            return struct.pack('<q', total)
        self.copy.read = read
        self.copy.finish = finish or (lambda: None)
        with patch('deskrawl_assistant.statistics_reader._layout', return_value=self.layout), \
             patch('deskrawl_assistant.statistics_reader._Copy', return_value=self.copy):
            return read_gold_ledger(self.reader, character)

    def test_gold_reads_int64_pickup_total_and_has_character_specific_source(self):
        first = self.gold(); second = self.gold(character='hero-2')
        self.assertTrue(first['available'])
        self.assertEqual(first['total'], 9876543210)
        self.assertEqual(first['source'], 'PlayerStatistics.coin_pickups')
        self.assertEqual(first['id'], self.gold()['id'])
        self.assertNotEqual(first['id'], second['id'])
        self.reader.session_id = 'another-session'
        self.assertNotEqual(first['id'], self.gold()['id'])

    def test_gold_invalid_identity_fields_values_and_unstable_read_are_nonfatal(self):
        for character in ('', None):
            self.assertFalse(self.gold(character=character)['available'])
        self.assertFalse(self.gold(-1)['available'])
        self.reader.fields = lambda klass: self.fields[::-1]
        self.assertFalse(self.gold()['available'])
        self.reader.fields = lambda klass: self.fields
        def changed(): raise SnapshotChangedError('Changed during read')
        self.assertFalse(self.gold(finish=changed)['available'])
        self.assertTrue(self.gold()['available'])

    def test_missing_gold_signature_does_not_prevent_other_statistics(self):
        self.layout['gold_reason'] = '未确认金币统计'
        with patch('deskrawl_assistant.statistics_reader._layout', return_value=self.layout):
            result = read_gold_ledger(self.reader, 'hero-1')
        self.assertEqual(result, {'available': False, 'reason': '未确认金币统计'})
        self.assertEqual(_totals(self.reader, self.copy, self.layout)['divine_count'], 17)

    def test_gold_signature_selects_int32_pickup_not_int64_experience_increment(self):
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
        dis = Cs(CS_ARCH_X86, CS_MODE_64); dis.detail = True
        fields = [{'isStatic': False, 'resolvedType': 'System.Int64', 'rawRegistrationOffset': n} for n in (48, 56)]
        gold = {'isStatic': False, 'returnType': 'System.Void', 'parameters': [{'resolvedType': 'System.Int32'}]}
        xp = dict(gold, parameters=[{'resolvedType': 'System.Int64'}])
        instructions = lambda method: list(dis.disasm(bytes.fromhex('48 01 41 30' if method is gold else '48 01 51 38'), 0x1000))
        self.assertEqual(_gold_pickup_offset(fields, [gold, xp], instructions), 48)
        with self.assertRaises(MemoryReadError): _gold_pickup_offset(fields, [xp], instructions)
        with self.assertRaises(MemoryReadError): _gold_pickup_offset(fields, [gold, gold], instructions)


if __name__ == '__main__': unittest.main()
