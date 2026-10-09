"""Policy and service checks: finite capacity, selection, cancellation and persistence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from deskrawl_assistant.automation import DEFAULT_SETTINGS,validate_settings,pressure_candidates
from deskrawl_assistant.web_service import AssistantService
from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.models import ValidationError
from test_web_service import FakeClient,item


def gem(index,name='GemEmerald4',count=1):
    return dict(container='inventory',slot_index=index,is_equipment=False,internal_name=name,
                item_class='GemData',item_type=6,item_uid=None,locked=False,count=count,max_stack=50,modifiers=[])


class AutomationClient(FakeClient):
    def __init__(self,items,capacity=40):
        super().__init__(items);self.capacity=capacity;self.events=[];self.drops=[];self.storage_full=False
    def snapshot(self):
        for row in self.items:
            row['transfer_key']=f"{row['container']}:{row['slot_index']}:{row['internal_name']}:{row.get('item_uid')}:{row.get('count',1)}"
        snap=super().snapshot()
        snap['containers']['inventory']['slot_count']=self.capacity
        snap['transfer']={'available':True,'storage_indices':list(range(250))}
        return snap
    def move_items(self,expected,target,validate):
        if self.storage_full: raise MemoryReadError('目标堆叠容量及可用空格不足；未移动任何物品')
        if not validate(): raise MemoryReadError('移动已取消，未更改物品')
        row=next(r for r in self.items if r['transfer_key']==expected[0]['transfer_key'])
        self.events.append(('move',row['internal_name']))
        used={r['slot_index'] for r in self.items if r['container']==target}
        row['container']=target;row['slot_index']=next(n for n in range(250) if n not in used)
        return {'moved':1,'stacked_count':0}
    def carriage_snapshot(self):return {'available':True,'count':len(self.drops),'items':copy.deepcopy(self.drops)}
    def collect_carriage(self,token,validate):
        assert validate()
        row=next(r for r in self.drops if r['selection_id']==token)
        self.events.append(('collect',row['internal_name']))
        self.drops.remove(row)
        row=copy.deepcopy(row);row['container']='inventory'
        used={r['slot_index'] for r in self.items if r['container']=='inventory'}
        row['slot_index']=next(n for n in range(self.capacity) if n not in used)
        self.items.append(row)
        return {'moved':1,'name':row['internal_name']}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=AutomationClient([])
        self.service=AssistantService(self.client,Path(self.tmp.name)/'rules.json')
        self.service._publish(self.client.snapshot());self.addCleanup(self.service.close)
        self.log=patch('deskrawl_assistant.web_service.record');self.log.start();self.addCleanup(self.log.stop)
    def enable(self,**groups):
        settings=copy.deepcopy(DEFAULT_SETTINGS)
        for key,value in groups.items():settings[key].update(value)
        self.service.save_automation(settings)
        self.service._publish(self.client.snapshot());self.service.set_automation_running(True)
        return settings
    def test_invalid_configuration_cannot_overwrite_saved_preferences(self):
        saved=self.enable(gems={'enabled':True})
        for mutate in [lambda s:s['carriage'].update(enabled=True,items=[]),lambda s:s['carriage'].update(items=['arbitrary_address']),
                       lambda s:s['pressure'].update(threshold=12,target_free=12),lambda s:s['pressure'].update(threshold=True),
                       lambda s:s['gems'].update(enabled=1)]:
            invalid=copy.deepcopy(saved);mutate(invalid)
            with self.assertRaises(ValidationError):self.service.save_automation(invalid)
        self.assertEqual(json.loads(self.service.automation_config.read_text()),saved)
    def test_restarting_keeps_preferences_but_does_not_start_writes(self):
        saved=self.enable(gems={'enabled':True})
        other=AssistantService(AutomationClient([]),self.service.config)
        try:
            self.assertEqual(other.automation_settings,saved);self.assertFalse(other.automation_running)
        finally:other.close()
    def test_capacity_uses_all_physical_items_and_moves_only_equipment(self):
        self.client.capacity=6
        gear=item(0,locked=True);self.client.items=[gear,gem(1),gem(2),gem(3),gem(4)]
        self.enable(pressure={'enabled':True,'threshold':1,'target_free':3})
        self.service._automation_once()
        self.assertEqual(self.client.events,[('move',gear['internal_name'])])
        self.assertEqual(gear['container'],'storage');self.assertTrue(gear['locked'])
        self.assertTrue(all(r['container']=='inventory' for r in self.client.items if not r['is_equipment']))
    def test_capacity_goal_continues_across_cycles_without_retriggering(self):
        self.client.capacity=20;self.client.items=[item(n) for n in range(19)]
        self.enable(pressure={'enabled':True,'threshold':1,'target_free':15})
        self.service._automation_once();self.assertEqual(len(self.client.events),8)
        self.service._automation_once();self.assertEqual(len(self.client.events),14)
        self.assertFalse(self.service.pressure_active)
    def test_capacity_does_not_trigger_early_and_respects_locked_filter(self):
        self.client.capacity=6;self.client.items=[item(0),item(1,locked=True)]
        self.enable(pressure={'enabled':True,'threshold':2,'target_free':4,'equipment_filter':'locked'})
        self.service._automation_once();self.assertFalse(self.client.events)
        self.client.items.extend([gem(2),gem(3),gem(4)])
        self.service._automation_once()
        self.assertEqual(len(self.client.events),1);self.assertEqual(self.client.items[1]['container'],'storage')
    def test_gems_free_space_before_carriage_and_unselected_drops_remain(self):
        self.client.capacity=1;self.client.items=[gem(0)]
        self.client.drops=[{**gem(0),'selection_id':'chosen','collectable':True},
                           {**gem(0,'GemRuby4'),'selection_id':'other','collectable':True}]
        self.enable(gems={'enabled':True},carriage={'enabled':True,'items':['GemEmerald4']})
        self.service._automation_once()
        self.assertEqual(self.client.events,[('move','GemEmerald4'),('collect','GemEmerald4')])
        self.assertEqual([r['selection_id'] for r in self.client.drops],['other'])
    def test_full_storage_waits_without_disabling_or_losing_gems(self):
        self.client.items=[gem(0,count=2)];self.client.storage_full=True
        self.enable(gems={'enabled':True});self.service._automation_once()
        self.assertTrue(self.service.automation_running);self.assertFalse(self.client.events)
        self.assertEqual(self.client.items[0]['count'],2);self.assertIn('空格不足',self.service.automation_status)
    def test_full_inventory_keeps_carriage_item(self):
        self.client.capacity=1;self.client.items=[item(0)]
        self.client.drops=[{**gem(0),'selection_id':'chosen','collectable':True}]
        self.enable(carriage={'enabled':True,'items':['GemEmerald4']});self.service._automation_once()
        self.assertFalse(self.client.events);self.assertEqual(len(self.client.drops),1)
        self.assertTrue(self.service.automation_running)
    def test_stop_and_disabling_lock_monitor_leave_independent_switches_consistent(self):
        self.client.items=[gem(0)]
        self.enable(gems={'enabled':True});self.service.set_monitoring(False)
        self.service._automation_once();self.assertEqual(len(self.client.events),1)
        self.service.stop();self.assertFalse(self.service.automation_running)
        self.client.items=[gem(0)];self.service._automation_once();self.assertEqual(len(self.client.events),1)
    def test_changing_preferences_cancels_a_prepared_transfer(self):
        self.client.items=[gem(0)];self.enable(gems={'enabled':True})
        original=self.client.move_items
        def change(expected,target,validate):
            settings=copy.deepcopy(self.service.automation_settings);settings['gems']['enabled']=False
            self.service.save_automation(settings)
            return original(expected,target,validate)
        with patch.object(self.client,'move_items',side_effect=change):self.service._automation_once()
        self.assertFalse(self.client.events)
    def test_failure_stops_both_background_operations(self):
        self.client.items=[gem(0)];self.enable(gems={'enabled':True})
        with patch.object(self.client,'move_items',side_effect=MemoryReadError('回读不一致')):self.service._work('monitor',{})
        self.assertFalse(self.service.automation_running);self.assertFalse(self.service.monitoring)
        self.assertIn('回读不一致',self.service.error)


if __name__=='__main__':unittest.main()
