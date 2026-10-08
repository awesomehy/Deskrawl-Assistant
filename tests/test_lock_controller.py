"""Wrong target, locked target and unknown outcomes must never toggle an item."""
import unittest
from unittest.mock import patch
from deskrawl_assistant.lock_controller import GridCalibration, LockController

TARGET = {'item_uid':'uid-a','instance_id':'instance-a','name_key':'LegendaryPants1','slot_index':10,'is_equipment':True,'locked':False}
GRID = GridCalibration((100,100),40,40,8,(0,0,1000,1000))


class FakeIO:
    def __init__(self):
        self.sent = 0
        self.point = (100,100)
        self.in_game = True
    def key_down(self,key): return False
    def foreground(self): return self.in_game
    def bounds(self): return (0,0,1000,1000)
    def position(self): return self.point
    def move(self,point): self.point=point
    def press_l(self): self.sent+=1


class FakeClient:
    def __init__(self, rows): self.rows=iter(rows)
    def call(self,name): return next(self.rows)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        logger = patch('deskrawl_assistant.lock_controller.record')
        logger.start()
        self.addCleanup(logger.stop)
    def controller(self,rows):
        io = FakeIO()
        ctl = LockController(FakeClient(rows),io)
        ctl.pause = lambda seconds: None
        return ctl,io

    def test_wrong_uid_never_sends_l(self):
        ctl,io=self.controller([{**TARGET,'item_uid':'different'}])
        with self.assertRaises(RuntimeError): ctl.lock_one(TARGET,GRID)
        self.assertEqual(io.sent,0)

    def test_same_base_name_different_instance_never_sends_l(self):
        ctl,io=self.controller([{**TARGET,'instance_id':'other'}])
        with self.assertRaises(RuntimeError): ctl.lock_one(TARGET,GRID)
        self.assertEqual(io.sent,0)

    def test_already_locked_never_toggles(self):
        ctl,io=self.controller([{**TARGET,'locked':True}])
        self.assertEqual(ctl.lock_one(TARGET,GRID),'already_locked')
        self.assertEqual(io.sent,0)

    def test_unknown_lock_never_sends_l(self):
        ctl,io=self.controller([{**TARGET,'locked':None}])
        with self.assertRaises(RuntimeError): ctl.lock_one(TARGET,GRID)
        self.assertEqual(io.sent,0)

    def test_changed_affixes_fail_final_rule_recheck(self):
        ctl,io=self.controller([TARGET])
        with self.assertRaises(RuntimeError): ctl.lock_one(TARGET,GRID,lambda item: False)
        self.assertEqual(io.sent,0)

    def test_confirmed_lock_is_one_press(self):
        ctl,io=self.controller([TARGET,{**TARGET,'locked':True}])
        self.assertEqual(ctl.lock_one(TARGET,GRID),'locked')
        self.assertEqual(io.sent,1)

    def test_ambiguous_result_is_not_retried(self):
        ctl,io=self.controller([TARGET,{**TARGET,'item_uid':'different'}])
        with self.assertRaises(RuntimeError): ctl.lock_one(TARGET,GRID)
        with self.assertRaises(RuntimeError): ctl.lock_one(TARGET,GRID)
        self.assertEqual(io.sent,1)

    def test_invalid_grid_does_not_escape_window(self):
        with self.assertRaises(ValueError): GRID.point(999)
        with self.assertRaises(ValueError): GridCalibration((100,100),40.5,40,8,(0,0,1000,1000))


if __name__=='__main__': unittest.main()
