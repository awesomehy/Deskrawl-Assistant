"""Regression checks for copied combat values and passive run boundaries."""
import math
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.recommendation_reader import (
    _Copy, compose_stats, decode_obscured_double, fingerprint, read_recommendation,
)


class RecommendationReaderTests(unittest.TestCase):
    def test_real_obscured_double_fixture(self):
        # Copied native WeaponSpeed payload; contains no role/save identity.
        raw = bytes.fromhex('0a6991de00000000a11e94b3320a8abc1ea194d333f0673500000000000000000000000000000000')
        self.assertAlmostEqual(decode_obscured_double(raw), 0.9300000071525574)
        broken = bytearray(raw)
        broken[10] ^= 0x80
        with self.assertRaises(MemoryReadError):
            decode_obscured_double(broken)

    def test_zero_wrapper_and_truncated_input(self):
        self.assertEqual(decode_obscured_double(bytes(40)), 0)
        with self.assertRaises(MemoryReadError):
            decode_obscured_double(bytes(32))

    def test_final_stats_combine_equipment_talent_and_temporary_status(self):
        base = {7: 836, 1: 9, 10: 3, 4: .05, 5: 1.5}
        caches = {536: {7:1111, 1:66, 8:2034, 9:1750, 21:.93, 20:175,
                        6:.356, 5:.774, 41:.272, 42:.133, 44:.245, 57:.06},
                  568: {6:.24, 9:120}, 632:{41:1.2}}
        stats = compose_stats(base, caches, ({41:.15, 6:.3}, {8:.66}, {}), [(8,39,.1)])
        self.assertAlmostEqual(stats[6], .896)
        self.assertAlmostEqual(1+stats[41], 2.622)
        self.assertAlmostEqual(stats[5], 2.274)
        self.assertAlmostEqual(stats[10], 3.18)
        self.assertAlmostEqual(stats[8], 2034*1.245*1.66)
        self.assertAlmostEqual(stats[39], stats[8]*.1)
        self.assertAlmostEqual(stats[7], 1947*1.133)
        self.assertEqual(stats[20],175)

    def test_percent_adds_and_status_products_are_distinct(self):
        stats = compose_stats({3:100}, {536:{3:10},544:{3:.2},576:{3:.3}},
                              ({3:5},{3:.1},{3:2}))
        self.assertAlmostEqual(stats[3], ((100+10)*1.5+5)*1.1*2)

    def test_chances_are_native_fractions_and_capped(self):
        stats = compose_stats({4:.6,13:.9,22:.99,37:1.0}, {536:{4:.4}})
        for key in (4,13,22,37):
            self.assertEqual(stats[key],.85)

    def test_conversion_cycle_and_nonfinite_stats_fail_closed(self):
        with self.assertRaises(MemoryReadError):
            compose_stats({}, {}, conversions=[(3,20,1),(20,3,1)])
        with self.assertRaises(MemoryReadError):
            compose_stats({3:math.inf}, {})

    def test_loadout_fingerprint_changes_with_character_skill_and_gear(self):
        skills=[{'slot':1,'ability_id':'Basic','level':3}]
        original=fingerprint('0:1:role', {536:{20:175}}, skills)
        self.assertEqual(original, fingerprint('0:1:role', {536:{20:175}}, skills))
        self.assertNotEqual(original, fingerprint('0:2:role', {536:{20:175}}, skills))
        self.assertNotEqual(original, fingerprint('0:1:role', {536:{20:176}}, skills))
        self.assertNotEqual(original, fingerprint('0:1:role', {536:{20:175}}, [{'slot':1,'ability_id':'Other','level':3}]))

    def test_equal_numeric_gear_with_other_mechanisms_has_other_fingerprint(self):
        stats={536:{20:175}}
        skills=[{'slot':1,'ability_id':'Basic','level':3}]
        first=fingerprint('role',stats,skills,{'equipment':[{'asset_id':'LegendaryA','uid':'one'}], 'runes':[{'asset_id':'RuneA','uid':'r1'}], 'talents':[{'asset_id':'TalentA','points':1}]})
        for extra in ({'equipment':[{'asset_id':'LegendaryB','uid':'two'}]},
                      {'runes':[{'asset_id':'RuneB','uid':'r2'}]},
                      {'talents':[{'asset_id':'TalentB','points':1}]}):
            self.assertNotEqual(first,fingerprint('role',stats,skills,extra))

    def test_plan_summary_uses_complete_boss_wave_and_explicit_terminal(self):
        from deskrawl_assistant.recommendation_reader import _run
        class Copy:
            wave_index=2
            fallback_xp=20
            completed=False
            final_kill=False
            plan_difficulty='Normal'
            reader=SimpleNamespace(string=lambda value:value, asset_name=lambda obj:'Map')
            def ptr(self,address):
                return {1120:3000,1112:4000,2088:3000,3016:'run',3024:'Map',3048:self.plan_difficulty,3056:5000,
                        3088:9000,6024:'Normal',6040:9200,7024:'Boss',7040:9300,
                        8016:'NormalMob',8116:'BossMob',8216:'Minion',1192:0}[address]
            def verify(self,obj,name):return obj
            def integer(self,address):
                if address==2048:return self.wave_index
                return {3040:10,6032:1,7032:1,8024:2,8124:1,8224:1}[address]
            def boolean(self,address):
                return self.completed if address==2248 else self.final_kill if address==2249 else False
            def number(self,address):return 0.
            def read(self,address,size):
                if address==3072:return struct.pack('<d',0.)
                return struct.pack('<q',{8032:self.fallback_xp,8048:100,8132:14,8148:90,8232:10,8248:50}[address])
            def array(self,obj,*args,**kwargs):
                return [struct.pack('<Q',pointer) for pointer in {9000:[6000,7000],9200:[8000],9300:[8100,8200]}[obj]]
            def list(self,obj,*args,**kwargs):return []
        copy=Copy()
        catalog={'config':{'health_growth':[0,0,0],'EliteHealthMultiplier':3},
                 'difficulties':{'Normal':{'health_multiplier':1,'xp_multiplier':1}},'enemies':{}}
        run=_run(copy,1000,2000,catalog,'Normal',player_level=10,xp_gain_multiplier=1)
        self.assertEqual(run['planned_normal_health'],200)
        self.assertEqual(run['planned_boss_health'],140)  # Boss AND its minion.
        self.assertEqual(run['planned_xp'],64)
        self.assertEqual(run['phase'],'boss')
        self.assertEqual(run['wave_count'],2)
        self.assertEqual(run['enemy_count'],0)
        self.assertFalse(run['finished'])  # Empty alive list proves no terminal.
        self.assertFalse(run['at_start'])
        copy.final_kill=True
        self.assertFalse(_run(copy,1000,2000,catalog,'Normal',player_level=10,xp_gain_multiplier=1)['finished'])
        copy.completed=True
        self.assertTrue(_run(copy,1000,2000,catalog,'Normal',player_level=10,xp_gain_multiplier=1)['finished'])
        copy.completed=copy.final_kill=False
        copy.wave_index=0
        self.assertTrue(_run(copy,1000,2000,catalog,'Normal',player_level=10,xp_gain_multiplier=1)['at_start'])
        catalog['config'].update(XpPerBaseHealth=1,XpPerLevelScale=.1,XpZeroUnderLevel=6)
        copy.fallback_xp=0
        # The plan still reports level 10; only the copied current role matters.
        fresh=_run(copy,1000,2000,catalog,'Normal',player_level=1,xp_gain_multiplier=1)
        leveled=_run(copy,1000,2000,catalog,'Normal',player_level=7,xp_gain_multiplier=1)
        buffed=_run(copy,1000,2000,catalog,'Normal',player_level=1,xp_gain_multiplier=2)
        self.assertEqual(fresh['planned_xp'],224)
        self.assertEqual(leveled['planned_xp'],26)
        self.assertEqual(buffed['planned_xp'],424)
        self.assertEqual(fresh['xp_source'],'static_formula_fallback')
        copy.fallback_xp=-1
        self.assertEqual(_run(copy,1000,2000,catalog,'Normal',player_level=7,xp_gain_multiplier=1)['planned_xp'],26)
        copy.fallback_xp=20
        self.assertEqual(_run(copy,1000,2000,catalog,'Normal',player_level=1,xp_gain_multiplier=99)['planned_xp'],64)
        copy.plan_difficulty='Inferno1'
        catalog['difficulties']['Inferno']={'health_multiplier':6,'xp_multiplier':2}
        self.assertEqual(_run(copy,1000,2000,catalog,'Inferno',player_level=70,xp_gain_multiplier=1)['difficulty'],'Inferno')
        copy.plan_difficulty='Normal'
        copy.wave_index=1
        self.assertFalse(_run(copy,1000,2000,catalog,'Normal',player_level=10,xp_gain_multiplier=1)['at_start'])
    def test_version_mismatch_never_touches_memory(self):
        class ForbiddenMemory:
            def read(self,*_):
                raise AssertionError('Unsupported version must not read any address')
        reader=SimpleNamespace(profile={}, memory=ForbiddenMemory())
        result=read_recommendation(reader)
        self.assertFalse(result['available'])
        self.assertIsNone(result['profile'])
        self.assertIsNone(result['run'])

    def test_adapted_profile_verifies_current_class_names_and_singleton_rva(self):
        fields=[{'name':'renamed','rawRegistrationOffset':0,'token':'0x04009999'}]
        profile={'types':[{'name':'PlayerData','fields':fields},
                          {'name':'zz','logicalName':'ey','fields':fields}]}
        addresses={0x10000+0x1234:0x2000,0x2000+184:0x3000,0x3000:0x4000,0x4000:0x2000}
        reader=SimpleNamespace(resolved_profiles={'recommendation-types.json':profile},profile={},
            native_layout={'player_data_rva':0x1234},module={'base':0x10000},
            memory=SimpleNamespace(read=lambda address,size:struct.pack('<Q',addresses[address])),
            class_name=lambda klass:'PlayerData' if klass==0x2000 else 'zz',
            fields=lambda _:[{'name':'renamed','offset':0,'token':0x04009999}])
        copied=_Copy(reader)
        self.assertEqual(copied.singleton(0x9999,'PlayerData'),0x4000)
        self.assertEqual(copied.klass(0x5555,'ey'),0x5555)

    def test_changed_game_file_blocks_recommendation_before_memory_reads(self):
        def changed():raise MemoryReadError('游戏文件已更新')
        reader=SimpleNamespace(check_game_files=changed,
            memory=SimpleNamespace(read=lambda *_:self.fail('Must not read changed game')))
        self.assertFalse(read_recommendation(reader)['available'])

    def test_snapshot_change_retries_and_never_becomes_zero_observation(self):
        with patch('deskrawl_assistant.recommendation_reader._snapshot', side_effect=SnapshotChangedError('changing')) as probe:
            result=read_recommendation(object())
        self.assertEqual(probe.call_count,3)
        self.assertFalse(result['available'])
        self.assertIsNone(result['run'])

    def test_guard_detects_mid_copy_change(self):
        copy=object.__new__(_Copy)
        copy.guards=[(100,b'old')]
        copy.memory=SimpleNamespace(read=lambda address,size:b'new')
        with self.assertRaises(SnapshotChangedError):
            copy.finish()


if __name__ == '__main__':
    unittest.main()
