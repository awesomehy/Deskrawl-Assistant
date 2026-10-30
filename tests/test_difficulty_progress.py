import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from deskrawl_assistant.recommendation_reader import completed_by_difficulty,read_campaign,DIFFICULTY_RVA
from deskrawl_assistant.game_actions import normalise_run
from deskrawl_assistant.native_memory import MemoryReadError


class DifficultyProgressTests(unittest.TestCase):
    def test_normal_completion_does_not_unlock_nightmare_or_inferno(self):
        result=completed_by_difficulty(['Forest1','Forest2','Forest1::difficulty::Nightmare',
                                        'Forest1::difficulty::Inferno1'])
        self.assertEqual(result,{'Normal':['Forest1','Forest2'],'Nightmare':['Forest1'],'Inferno':['Forest1']})

    def test_unknown_future_tiers_do_not_count_as_current_tier(self):
        result=completed_by_difficulty(['Forest2::difficulty::Future'])
        self.assertEqual(result,{'Normal':[],'Nightmare':[],'Inferno':[]})

    def test_invalid_save_keys_are_rejected(self):
        for values in ([None],['a::difficulty::Nightmare::difficulty::Inferno1']):
            with self.assertRaises(MemoryReadError): completed_by_difficulty(values)


class TownCampaignCopy:
    """Login fixture has no combat stats or ability controller at all."""
    def __init__(self):
        self.reader = SimpleNamespace(session_id='campaign-test',module={'base':0},singleton=lambda name:{'SaveSystem':0x3000,'Inventory':0x5000}[name],
            string=lambda address:{0x8000:'Offline',0xD000:'run-1',0xD100:'Forest2',0xD200:'Normal'}[address],
            asset_name=lambda obj:{0xB000:'Forest2',0xE000:'Ticket'}[obj])
        self.pointers = {0x1000+32:0x4000,0x1000+120:0,0x1000+112:0,0x2000+40:0x8000,
            DIFFICULTY_RVA:0x6000,0x6000+184:0x7000,0x5000+64:0x9000}
        self.integers = {0x1000+176:1,0x7000:0,0x7004:1,0x4000+208:2,0xB000+32:0}
        self.flags = {0x4000+48:False}
        self.finished = False
    def singleton(self, rva, name): return {'GameManager':0x1000,'PlayerData':0x2000}[name]
    def ptr(self,address): return self.pointers[address]
    def integer(self,address): return self.integers[address]
    def read(self,address,size):
        if (address,size)==(0x3000+32,8): return struct.pack('<ii',1,0)
        raise AssertionError('unexpected live combat field')
    def klass(self,address,name): return address
    def verify(self,address,name):
        if name in ('Player','PlayerAbilityController','StatusEffectController'):
            raise AssertionError('campaign must not depend on combat/DPS components')
        return address
    def completed(self,save): return ['Forest1','Forest2','Forest1::difficulty::Nightmare']
    def boolean(self,address): return self.flags[address]
    def dictionary(self,obj,kind,key): return {0xE000:3}
    def array(self,obj,stride,name,**kwargs): return [struct.pack('<Q',0xF000),struct.pack('<Q',0xF100)]
    def list(self,obj,**kwargs): return [struct.pack('<Q',0xF200)]
    def finish(self): self.finished = True


class CampaignReadTests(unittest.TestCase):
    def read_fixture(self,copy):
        catalog={'maps':[{'map_id':'Forest1'},{'map_id':'Forest2','prerequisite':'Forest1'},
                        {'map_id':'Forest3','prerequisite':'Forest2'}],
                 'difficulties':{key:{'value':n} for n,key in enumerate(('Normal','Nightmare','Inferno'))}}
        with patch('deskrawl_assistant.recommendation_reader._Copy',return_value=copy), \
             patch('deskrawl_assistant.recommendation_reader._catalog',return_value=catalog), \
             patch('deskrawl_assistant.recommendation_reader.native_rva',side_effect=lambda reader,key,value:value):
            return read_campaign(copy.reader)

    def test_town_login_reads_maps_without_skills_or_combat_attributes(self):
        copy=TownCampaignCopy();result=self.read_fixture(copy)
        self.assertTrue(result['available'])
        self.assertIsNone(result['run'])
        self.assertEqual(result['profile']['character_id'],'1:0:Offline')
        self.assertEqual(result['profile']['unlocked_maps']['Normal'],['Forest1','Forest2','Forest3'])
        self.assertEqual(result['profile']['unlocked_maps']['Nightmare'],['Forest1','Forest2'])
        self.assertEqual(result['profile']['unlocked_maps']['Inferno'],[])
        self.assertEqual(result['profile']['entry_items'],{'Ticket':3})
        self.assertTrue(copy.finished)

    def test_combat_completion_keeps_verified_wave_evidence_without_dps(self):
        copy=TownCampaignCopy()
        copy.pointers.update({0x1000+120:0xA000,0x1000+112:0xB000,0x1000+104:0xC000,
            0xC000+88:0xA000,0xA000+16:0xD000,0xA000+32:0xD100,0xA000+56:0xD200,
            0xA000+104:0xF000,0x1000+200:0xF300})
        copy.integers.update({0xC000+48:2,0xC000+152:1})
        copy.flags.update({0xC000+64:False,0xC000+248:True,0xC000+249:False,0xF200+296:True})
        result=self.read_fixture(copy)
        self.assertTrue(result['available'])
        run=normalise_run(result['run'],result['profile']['completed_maps']['Normal'])
        self.assertEqual(run['status'],'success')
        self.assertEqual((run['wave_count'],run['enemy_count']),(2,0))

    def test_unknown_difficulty_is_reported_without_fabricating_maps(self):
        copy=TownCampaignCopy();copy.integers[0x7000]=99
        result=self.read_fixture(copy)
        self.assertFalse(result['available'])
        self.assertIsNone(result['profile'])
        self.assertIn('难度',result['reason'])


if __name__=='__main__': unittest.main()
