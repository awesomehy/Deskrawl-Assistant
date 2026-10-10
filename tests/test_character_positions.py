"""Distance bonuses must use verified, finite world coordinates."""
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from deskrawl_assistant.character_positions import Positions, _point
from deskrawl_assistant.character_reader import _conditional
from deskrawl_assistant.character import boss_distance
from deskrawl_assistant.native_memory import MemoryReadError


class PositionTests(unittest.TestCase):
    def test_parent_rotation_scale_and_translation(self):
        s=math.sqrt(.5)
        pose=(10,20,30,0,0,s,0,s,2,3,4,0)
        point=_point(pose,(1,2,3))
        for a,b in zip(point,(22,26,28)):
            self.assertAlmostEqual(a,b)

    def test_distance_ignores_vertical_axis_and_includes_boundary(self):
        p=object.__new__(Positions)
        p.position=lambda obj:{1:(0,999,0),2:(3,0,4),3:(0,-999,4),4:(6,0,0)}[obj]
        self.assertEqual(2,p.nearby_count(1,[2,3,4],5,3))
        self.assertEqual(1,p.nearby_count(1,[2,3,4],5,1))
        with self.assertRaises(MemoryReadError):p.nearby_count(1,[2],float('nan'),1)

    def test_unknown_engine_is_rejected_before_any_coordinate_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'UnityPlayer.dll';path.write_bytes(b'unknown engine')
            c=Mock();c.reader.memory.modules={'unityplayer.dll':{'path':str(path)}}
            c.reader.memory.path=Path(tmp)/'Deskrawl.exe'
            with self.assertRaisesRegex(MemoryReadError,'引擎已变化'):Positions(c)
            c.read.assert_not_called()

    def test_native_hierarchy_and_cycle_rejection(self):
        p=object.__new__(Positions);p.cache={};p.path=Mock();p.path.stat.return_value=SimpleNamespace(st_size=1,st_mtime_ns=2);p.stamp=(1,2)
        c=Mock();c.reader.class_name.return_value='Object'
        pointers={100:200,116:300,332:400,432:500,508:600,724:800,732:900}
        c.ptr.side_effect=lambda at:pointers[at]
        c.read.side_effect=lambda at,n,**kw:struct.pack('<Qi',700,1) if at==640 else struct.pack('<12f',*(
            (2,0,0,0,0,0,0,1,1,1,1,0) if at==848 else (10,0,0,0,0,0,0,1,1,1,1,0)))
        c.integer.side_effect=lambda at:0 if at==904 else -1
        p.copy=c
        self.assertEqual((12,0,0),p.position(100));c.klass.assert_called_once_with(200,'UnityObject')
        p.cache={};c.integer.return_value=None;c.integer.side_effect=lambda at:1
        with self.assertRaisesRegex(MemoryReadError,'层级无效'):p.position(100)


class ConditionsTests(unittest.TestCase):
    def copy(self, tuples=(), status_tuples=(), stacks=()):
        c=Mock();c.types={'bk':{'name':'bk'}}
        c.ptr.side_effect=lambda at:9000 if at==1064 else at
        lists={2032:[struct.pack('<Q',x) for x in stacks],856+100:[struct.pack('<ifiidQ',*x) for x in tuples],
            864+100:[struct.pack('<i4xQiid',*x) for x in status_tuples],1192:[struct.pack('<Q',3000),struct.pack('<Q',4000),struct.pack('<Q',5000)]}
        c.list.side_effect=lambda at,*args,**kw:lists.get(at,[])
        c.verify.side_effect=lambda obj,*args:obj
        c.boolean.side_effect=lambda at:at==9080
        c.integer.return_value=0
        return c

    def test_distance_condition_is_sampled_once_and_status_radius_defaults_to_six(self):
        c=self.copy([(2,6,6,0,.8,0),(5,6,65,0,.4,0)],[(2,0,34,0,1.5)])
        with patch('deskrawl_assistant.character_reader.Positions') as positions:
            positions.return_value.nearby_count.side_effect=[0,3,0]
            layers,_=_conditional(c,100,1000,2000,100,100,[])
            self.assertEqual({6:.8,65:.4,34:1.5},layers[0]);positions.assert_called_once_with(c)
            self.assertEqual([6,6,6],[call.args[2] for call in positions.return_value.nearby_count.call_args_list])

    def test_health_thresholds_are_strict(self):
        c=self.copy([(0,0,65,0,1,0),(1,0,6,0,1,0)])
        self.assertEqual({},_conditional(c,100,1000,2000,30,100,[])[0][0])
        self.assertEqual({},_conditional(c,100,1000,2000,80,100,[])[0][0])
        self.assertEqual({6:1},_conditional(c,100,1000,2000,81,100,[])[0][0])

    def test_unknown_condition_or_distance_failure_does_not_supply_zero_stat(self):
        c=self.copy([(999,6,6,0,.8,0)])
        with self.assertRaisesRegex(MemoryReadError,'尚未适配'):_conditional(c,100,1000,2000,100,100,[])
        c=self.copy([(2,6,6,0,.8,0)])
        with patch('deskrawl_assistant.character_reader.Positions',side_effect=MemoryReadError('不可读')):
            with self.assertRaises(MemoryReadError):_conditional(c,100,1000,2000,100,100,[])

    def test_fixed_boss_conditions_ignore_real_health_battle_and_enemy_count(self):
        tuples=[(kind,6,stat,0,1,0) for kind,stat in ((-1,3),(0,4),(1,5),(3,6),(4,65),(5,34))]
        c=self.copy(tuples,stacks=[6000]);c.boolean.side_effect=AssertionError('live battle read')
        with patch('deskrawl_assistant.character_reader.Positions',side_effect=AssertionError('live distance read')):
            layers,_=_conditional(c,100,1000,2000,0,100,[],7)
        self.assertEqual({3:1,5:1,6:1,65:1},layers[0])
        addresses=[call.args[0] for call in c.list.call_args_list]
        self.assertNotIn(2032,addresses);self.assertNotIn(1192,addresses)

    def test_fixed_boss_radius_boundary_and_role_distance(self):
        self.assertEqual([2,7,7,2],[boss_distance(mask) for mask in (1,2,4,8)])
        for distance,expected in ((2,{}),(6,{}),(7,{6:.8,34:1.5})):
            c=self.copy([(2,6,6,0,.8,0)],[(2,0,34,0,1.5)])
            self.assertEqual(expected,_conditional(c,100,1000,2000,100,100,[],distance)[0][0])

    def test_fixed_boss_excludes_status_stacks_gated_conversion_and_ability_bonus(self):
        c=self.copy([(1,0,65,0,5,5000)],[(1,5000,6,0,2)])
        normal_list=c.list.side_effect
        extra={892:[struct.pack('<iidQ',1,3,.5,0),struct.pack('<iidQ',1,3,5,5000)],
            916:[struct.pack('<Qi4xd',5000,3,2)],980:[struct.pack('<Qiid',5000,6,0,3)]}
        c.list.side_effect=lambda at,*args,**kw:extra.get(at,normal_list(at,*args,**kw))
        layers,conversions=_conditional(c,100,1000,2000,100,100,[],7)
        self.assertEqual([(1,3,.5)],conversions)
        self.assertEqual(0,layers[0].get(3,0));self.assertEqual(0,layers[0].get(6,0))
        self.assertNotIn(65,layers[0])

    def test_fixed_boss_rejects_unknown_conditions_and_invalid_radius(self):
        for kind,radius in ((999,6),(2,float('nan')),(2,-1)):
            with self.assertRaises(MemoryReadError):
                _conditional(self.copy([(kind,radius,6,0,1,0)]),100,1000,2000,100,100,[],7)


if __name__=='__main__':unittest.main()
