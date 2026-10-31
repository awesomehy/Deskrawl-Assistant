"""Native repeat totals survive a missed, short-lived completion snapshot."""
import struct
from types import SimpleNamespace
import unittest

from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.recommendation_reader import _Copy, _campaign_run
from deskrawl_assistant.world_boss import PUMPKIN


class CampaignCopy(_Copy):
    """Use the real copied primitive/guard methods with a small native fixture."""
    manager, plan, active_map, wave = 0x1000, 0x3000, 0x4000, 0x5000

    def __init__(self):
        self.values = {}
        self.guards = []
        self.memory = SimpleNamespace(read=lambda address, size: self.values[address][:size])
        self.reader = SimpleNamespace(session_id='process-session', memory=self.memory,
            string=lambda address: self.strings[address], asset_name=lambda obj: self.map_name)
        self.strings = {0x9000:'round-one', 0x9100:'MapA', 0x9200:'Normal'}
        self.map_name = 'MapA'
        for address, pointer in ((self.manager+120,self.plan), (self.manager+112,self.active_map),
                (self.manager+104,self.wave), (self.wave+88,self.plan),
                (self.plan+16,0x9000), (self.plan+32,0x9100), (self.plan+56,0x9200),
                (self.plan+104,0x6000), (self.manager+200,0)):
            self.values[address] = struct.pack('<Q',pointer)
        for address, value in ((self.active_map+32,0), (self.plan+72,1),
                (self.wave+304,1), (self.wave+48,1), (self.wave+152,4)):
            self.integer_value(address,value)
        for address in (self.wave+64,self.wave+248,self.wave+249):
            self.values[address] = b'\0'

    def integer_value(self, address, value):
        self.values[address] = struct.pack('<i',value)

    def verify(self, obj, name):
        if not obj: raise MemoryReadError('Missing fixture object')
        return obj

    def array(self, obj, stride, expected_name=None, count=None, limit=4096):
        return [bytes(stride)]*2

    def list(self, obj, stride=8, element=None, limit=4096):
        return []

    def run(self, difficulty='Normal'):
        return _campaign_run(self,self.manager,difficulty)


class CampaignRepeatCounterTests(unittest.TestCase):
    def test_native_repeat_guid_and_terminal_flag_reset_keep_counter_identity(self):
        copy=CampaignCopy()
        first=copy.run()
        copy.strings[0x9000]='round-two'
        copy.integer_value(copy.wave+152,5)
        # The second round is already fighting; no terminal snapshot was read.
        copy.values[copy.wave+64]=b'\1'
        second=copy.run()
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(first['completion_counter']['id'],second['completion_counter']['id'])
        self.assertEqual(second['completion_counter']['count']-first['completion_counter']['count'],1)
        self.assertFalse(second['finished'])
        self.assertFalse(second['final_kill_settled'])

    def test_counter_is_nonnegative_and_bounded(self):
        for value in (-1,10000001):
            with self.subTest(value=value):
                copy=CampaignCopy();copy.integer_value(copy.wave+152,value)
                with self.assertRaisesRegex(MemoryReadError,'累计通关'):
                    copy.run()
        for value in (0,10000000):
            with self.subTest(value=value):
                copy=CampaignCopy();copy.integer_value(copy.wave+152,value)
                self.assertEqual(copy.run()['completion_counter']['count'],value)

    def test_completion_counter_is_part_of_snapshot_guards(self):
        copy=CampaignCopy();copy.run()
        self.assertIn((copy.wave+152,struct.pack('<i',4)),copy.guards)
        copy.finish()
        copy.integer_value(copy.wave+152,5)
        with self.assertRaises(SnapshotChangedError):copy.finish()

    def test_counter_identity_changes_for_map_difficulty_and_process_session(self):
        original=CampaignCopy().run()['completion_counter']['id']
        for changed in ('map','difficulty','session','active_map','wave'):
            with self.subTest(changed=changed):
                copy=CampaignCopy();difficulty='Normal'
                if changed=='map':copy.map_name=copy.strings[0x9100]='MapB'
                elif changed=='difficulty':
                    copy.strings[0x9200]='Inferno1';difficulty='Inferno'
                elif changed=='session':copy.reader.session_id='other-process-session'
                else:
                    old=getattr(copy,changed);new=old+0x10000
                    for address,value in list(copy.values.items()):
                        if old <= address < old+512:copy.values[new+(address-old)]=value
                    offset=112 if changed=='active_map' else 104
                    copy.values[copy.manager+offset]=struct.pack('<Q',new)
                    setattr(copy,changed,new)
                self.assertNotEqual(copy.run(difficulty)['completion_counter']['id'],original)

    def test_mythic_floors_and_underlying_world_difficulty_are_isolated(self):
        copy=CampaignCopy();copy.map_name=copy.strings[0x9100]='MythicDungeon'
        copy.integer_value(copy.active_map+32,1)
        first=copy.run()
        self.assertEqual((first['map_id'],first['difficulty']),('MythicDungeon::mythic::1','Mythic'))
        copy.integer_value(copy.plan+72,2);copy.integer_value(copy.wave+304,2)
        second=copy.run()
        self.assertNotEqual(first['completion_counter']['id'],second['completion_counter']['id'])
        copy.strings[0x9200]='Inferno1'
        third=copy.run('Inferno')
        self.assertEqual(third['difficulty'],'Mythic')
        self.assertNotEqual(second['completion_counter']['id'],third['completion_counter']['id'])

    def test_unstable_map_and_mythic_floor_fail_before_exposing_counter(self):
        copy=CampaignCopy();copy.map_name='OtherMap'
        with self.assertRaises(SnapshotChangedError):copy.run()
        copy.map_name=copy.strings[0x9100]='MythicDungeon'
        copy.integer_value(copy.active_map+32,1);copy.integer_value(copy.wave+304,2)
        with self.assertRaises(SnapshotChangedError):copy.run()

    def test_nonwinning_exit_and_new_round_do_not_infer_a_success(self):
        copy=CampaignCopy();first=copy.run()
        copy.strings[0x9000]='round-after-abandon-or-death'
        second=copy.run()
        self.assertEqual(first['completion_counter'],second['completion_counter'])
        copy.integer_value(copy.wave+152,0)
        reset=copy.run()
        self.assertEqual(reset['completion_counter']['id'],second['completion_counter']['id'])
        self.assertEqual(reset['completion_counter']['count'],0)


class SettledWorldBossPlanTests(unittest.TestCase):
    def fixture(self):
        copy = CampaignCopy()
        copy.map_name = copy.strings[0x9100] = PUMPKIN
        copy.integer_value(copy.active_map+32, 4)
        copy.values[copy.manager+120] = struct.pack('<Q', 0)
        copy.values[copy.wave+40] = struct.pack('<Q', copy.active_map)
        copy.values[copy.wave+248] = b'\1'
        copy.integer_value(copy.wave+48, 2)
        return copy

    def read(self, copy):
        return _campaign_run(copy, copy.manager, 'Normal', allow_settled_world_boss=True)

    def test_finished_wave_plan_recovers_the_real_identity_without_campaign_plan(self):
        copy = self.fixture()
        self.assertIsNone(copy.run())
        recovered = self.read(copy)
        self.assertEqual(recovered['map_id'], PUMPKIN)
        self.assertTrue(recovered['finished'])
        copy.values[copy.manager+120] = struct.pack('<Q', copy.plan)
        self.assertEqual(recovered, copy.run())

    def test_missing_or_nonterminal_or_other_map_plan_is_not_recovered(self):
        for change in ('ordinary', 'wrong_type', 'wave_map', 'running', 'active',
                       'early_wave', 'no_plan', 'no_waves', 'enemy'):
            with self.subTest(change=change):
                copy = self.fixture()
                if change == 'ordinary': copy.map_name = copy.strings[0x9100] = 'MapA'
                elif change == 'wrong_type': copy.integer_value(copy.active_map+32, 0)
                elif change == 'wave_map': copy.values[copy.wave+40] = struct.pack('<Q', 0x9876)
                elif change == 'running': copy.values[copy.wave+248] = b'\0'
                elif change == 'active': copy.values[copy.wave+64] = b'\1'
                elif change == 'early_wave': copy.integer_value(copy.wave+48, 1)
                elif change == 'no_plan': copy.values[copy.wave+88] = struct.pack('<Q', 0)
                elif change == 'no_waves': copy.array = lambda *a, **k: []
                elif change == 'enemy':
                    copy.list = lambda *a, **k: [struct.pack('<Q', 0xA000)]
                    copy.values[0xA000+296] = b'\0'
                self.assertIsNone(self.read(copy))

    def test_different_plan_map_or_difficulty_is_rejected(self):
        for string, value in ((0x9100, 'OtherMap'), (0x9200, 'Inferno1'), (0x9000, '')):
            with self.subTest(field=string):
                copy = self.fixture(); copy.strings[string] = value
                with self.assertRaises(SnapshotChangedError): self.read(copy)

    def test_plan_and_terminal_state_changes_invalidate_the_guarded_read(self):
        for address, raw in ((CampaignCopy.manager+120, struct.pack('<Q', CampaignCopy.plan)),
                             (CampaignCopy.wave+88, struct.pack('<Q', 0)),
                             (CampaignCopy.wave+40, struct.pack('<Q', 0)),
                             (CampaignCopy.wave+248, b'\0'), (CampaignCopy.wave+64, b'\1')):
            with self.subTest(address=address):
                copy = self.fixture(); self.assertIsNotNone(self.read(copy)); copy.finish()
                copy.values[address] = raw
                with self.assertRaises(SnapshotChangedError): copy.finish()


if __name__=='__main__':unittest.main()
