"""Policy and service checks: finite capacity, selection, cancellation and persistence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from deskrawl_assistant.automation import DEFAULT_SETTINGS,validate_settings,pressure_candidates,storage_targets
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
        self.storage_indices=list(range(250));self.destinations=[]
    def snapshot(self):
        for row in self.items:
            row['transfer_key']=f"{row['container']}:{row['slot_index']}:{row['internal_name']}:{row.get('item_uid')}:{row.get('count',1)}"
        snap=super().snapshot()
        snap['containers']['inventory']['slot_count']=self.capacity
        snap['transfer']={'available':True,'page_size':50,'storage_indices':list(self.storage_indices)}
        return snap
    def move_items(self,expected,target,validate,*,target_indices=None,allow_partial=False):
        if self.storage_full: raise MemoryReadError('目标堆叠容量及可用空格不足；未移动任何物品')
        if not validate(): raise MemoryReadError('移动已取消，未更改物品')
        used={r['slot_index'] for r in self.items if r['container']==target}
        destinations=[n for n in (target_indices if target_indices is not None else range(250)) if n not in used]
        if len(destinations)<len(expected) and not allow_partial:
            raise MemoryReadError('目标堆叠容量及可用空格不足；未移动任何物品')
        selected=[next(r for r in self.items if r['transfer_key']==s['transfer_key']) for s in expected]
        moved=min(len(selected),len(destinations))
        for row,destination in zip(selected,destinations):
            self.events.append(('move',row['internal_name']));self.destinations.append(destination)
            row['container']=target;row['slot_index']=destination
        return {'moved':moved,'remaining':len(expected)-moved,'stacked_count':0}
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
        threads=patch('deskrawl_assistant.web_service.threading.Thread');threads.start();self.addCleanup(threads.stop)
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
        for mutate in [lambda s:s['carriage'].update(enabled=True,types=[]),lambda s:s['carriage'].update(types=['arbitrary_address']),
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

    def test_eight_gems_share_one_transfer_and_two_fresh_reads(self):
        self.client.items=[gem(n) for n in range(9)]
        self.enable(gems={'enabled':True})
        with patch.object(self.client,'move_items',wraps=self.client.move_items) as move, \
             patch.object(self.client,'snapshot',wraps=self.client.snapshot) as read, \
             patch.object(self.service,'_publish',wraps=self.service._publish) as publish:
            self.service._automation_once()
        self.assertEqual(move.call_count,1)
        self.assertEqual(len(move.call_args.args[0]),8)
        self.assertTrue(move.call_args.kwargs['allow_partial'])
        self.assertEqual(read.call_count,2)
        self.assertEqual(publish.call_count,2)
        self.assertEqual(sum(r['container']=='inventory' for r in self.client.items),1)

    def test_partial_batch_moves_only_fitting_prefix_on_selected_page(self):
        self.client.items=[gem(n) for n in range(4)]+[dict(gem(n),container='storage') for n in range(48)]
        self.enable(gems={'enabled':True,'pages':[1]})
        self.service._automation_once()
        self.assertEqual(self.client.destinations,[48,49])
        self.assertEqual([r['slot_index'] for r in self.client.items if r['container']=='inventory'],[2,3])
        self.assertTrue(self.service.automation_running)
        self.assertIn('空格不足',self.service.automation_status)

    def test_batch_cancellation_before_write_keeps_every_source_stack(self):
        self.client.items=[gem(n,count=3) for n in range(3)]
        self.enable(gems={'enabled':True})
        original=self.client.move_items
        def cancel(*args,**kwargs):
            self.service.stop()
            return original(*args,**kwargs)
        with patch.object(self.client,'move_items',side_effect=cancel):
            self.service._automation_once()
        self.assertFalse(self.client.events)
        self.assertTrue(all(r['container']=='inventory' and r['count']==3 for r in self.client.items))

    def test_stop_after_atomic_batch_does_not_start_runes_or_carriage(self):
        self.client.items=[gem(0),gem(1),dict(gem(2,'Rune3'),item_class='RuneData',item_type=7)]
        self.client.drops=[dict(gem(0),selection_id='drop',collectable=True)]
        self.enable(gems={'enabled':True},runes={'enabled':True},carriage={'enabled':True,'types':['gem']})
        original=self.client.move_items
        def cancel_after(*args,**kwargs):
            result=original(*args,**kwargs)
            self.service.stop()
            return result
        with patch.object(self.client,'move_items',side_effect=cancel_after):
            self.service._automation_once()
        self.assertEqual(self.client.events,[('move','GemEmerald4')]*2)
        self.assertEqual(len(self.client.drops),1)
        self.assertEqual(self.client.items[2]['container'],'inventory')

    def test_incomplete_post_batch_read_defers_remaining_work(self):
        from deskrawl_assistant.carriage_transfer import CarriageUnavailable
        self.client.items=[gem(0),gem(1),dict(gem(2,'Rune3'),item_class='RuneData',item_type=7)]
        self.enable(gems={'enabled':True},runes={'enabled':True})
        original=self.client.snapshot
        def incomplete_after():
            result=original()
            if self.client.events:result['complete']=False
            return result
        with patch.object(self.client,'snapshot',side_effect=incomplete_after),self.assertRaises(CarriageUnavailable):
            self.service._automation_once()
        self.assertEqual(self.client.events,[('move','GemEmerald4')]*2)
        self.assertEqual(self.client.items[2]['container'],'inventory')

    def test_invalid_batch_receipt_stops_before_another_action(self):
        self.client.items=[gem(0),gem(1)]
        self.enable(gems={'enabled':True})
        with patch.object(self.client,'move_items',return_value={'moved':True}) as move, \
             self.assertRaisesRegex(MemoryReadError,'数量未核验'):
            self.service._automation_once()
        self.assertEqual(move.call_count,1)

    def test_background_noop_has_no_diagnostic_start_finish_noise(self):
        with patch.object(self.service,'_monitor_once',return_value='等待物品'), \
             patch.object(self.service,'_managed_refresh_control'), \
             patch.object(self.service,'_refresh_trade_tracking_if_needed'), \
             patch('deskrawl_assistant.web_service.record') as log:
            self.service._work('monitor',{})
        self.assertFalse(any(c.args[0] in ('web_operation_started','web_operation_finished') for c in log.call_args_list))

    def test_manual_read_retains_diagnostic_start_and_finish(self):
        with patch.object(self.service,'_managed_refresh_control'), \
             patch.object(self.service,'_refresh_trade_tracking_if_needed'), \
             patch('deskrawl_assistant.web_service.record') as log:
            self.service._work('read',{})
        self.assertIn('web_operation_started',[c.args[0] for c in log.call_args_list])
        self.assertIn('web_operation_finished',[c.args[0] for c in log.call_args_list])
    def test_capacity_does_not_trigger_early_and_respects_locked_filter(self):
        self.client.capacity=6;self.client.items=[item(0),item(1,locked=True)]
        self.enable(pressure={'enabled':True,'threshold':2,'target_free':4,'equipment_filter':'locked'})
        self.service._automation_once();self.assertFalse(self.client.events)
        self.client.items.extend([gem(2),gem(3),gem(4)])
        self.service._automation_once()
        self.assertEqual(len(self.client.events),1);self.assertEqual(self.client.items[1]['container'],'storage')
    def test_gems_free_space_before_carriage_and_unselected_types_remain(self):
        self.client.capacity=1;self.client.items=[gem(0)]
        self.client.drops=[{**gem(0),'selection_id':'chosen','collectable':True},
                           {**gem(0,'TreasureChest2'),'item_class':'ItemData','selection_id':'other','collectable':True}]
        self.enable(gems={'enabled':True},carriage={'enabled':True,'types':['gem']})
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
        self.enable(carriage={'enabled':True,'types':['gem']});self.service._automation_once()
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
        def change(expected,target,validate,*,target_indices=None):
            settings=copy.deepcopy(self.service.automation_settings);settings['gems']['enabled']=False
            self.service.save_automation(settings)
            return original(expected,target,validate,target_indices=target_indices)
        with patch.object(self.client,'move_items',side_effect=change):self.service._automation_once()
        self.assertFalse(self.client.events)
    def test_failure_stops_both_background_operations(self):
        self.client.items=[gem(0)];self.enable(gems={'enabled':True})
        with patch.object(self.client,'move_items',side_effect=MemoryReadError('回读不一致')):self.service._work('monitor',{})
        self.assertFalse(self.service.automation_running);self.assertFalse(self.service.monitoring)
        self.assertIn('回读不一致',self.service.error)

    def test_gems_runes_and_equipment_use_separate_noncontiguous_pages(self):
        self.client.storage_indices=list(range(50))+list(range(100,150))+list(range(200,250))
        rune=dict(gem(1,'Rune3'),item_class='RuneData',item_type=7)
        gear=item(2)
        self.client.capacity=4;self.client.items=[gem(0),rune,gear]
        self.enable(gems={'enabled':True,'pages':[5]},runes={'enabled':True,'pages':[3]},
                    pressure={'enabled':True,'pages':[1],'threshold':3,'target_free':4})
        self.service._automation_once()
        self.assertEqual(self.client.events,[('move','GemEmerald4'),('move','Rune3'),('move',gear['internal_name'])])
        self.assertEqual(self.client.destinations,[200,100,0])

    def test_full_selected_page_does_not_spill_into_other_empty_pages(self):
        self.client.items=[gem(0)]+[dict(gem(n),container='storage') for n in range(50)]
        self.enable(gems={'enabled':True,'pages':[1]})
        self.service._automation_once()
        self.assertFalse(self.client.events)
        self.assertEqual(self.client.items[0]['container'],'inventory')
        self.assertTrue(self.service.automation_running)
        self.assertIn('空格不足',self.service.automation_status)

    def test_closed_selected_page_waits_instead_of_using_open_page(self):
        self.client.storage_indices=list(range(50))+list(range(200,250))
        self.client.items=[gem(0)]
        self.enable(gems={'enabled':True,'pages':[2]})
        self.service._automation_once()
        self.assertFalse(self.client.events)
        self.assertIn('尚未开放',self.service.automation_status)

    def test_type_selection_collects_all_gem_variants_and_ignores_runes(self):
        keys=[key for key,meta in self.service.item_ui['collectibles'].items() if meta.get('kind')=='gem'][:2]
        self.assertEqual(len(keys),2)
        self.client.drops=[dict(gem(n,key),selection_id=f'gem-{n}',collectable=True) for n,key in enumerate(keys)]
        rune_key=next(key for key,meta in self.service.item_ui['collectibles'].items() if meta.get('kind')=='rune')
        self.client.drops.append(dict(gem(2,rune_key),selection_id='rune',collectable=True))
        self.enable(carriage={'enabled':True,'types':['gem']})
        self.service._automation_once()
        self.assertEqual(self.client.events,[('collect',key) for key in keys])
        self.assertEqual([row['selection_id'] for row in self.client.drops],['rune'])

    def test_legacy_preferences_migrate_only_in_memory_and_do_not_autostart(self):
        key=next(key for key,meta in self.service.item_ui['collectibles'].items() if meta.get('kind')=='gem')
        legacy={'carriage':{'enabled':True,'items':[key]},'gems':{'enabled':True},
                'pressure':{'enabled':True,'threshold':4,'target_free':12,'equipment_filter':'locked'}}
        original=json.dumps(legacy,ensure_ascii=False,indent=2)+'\n'
        self.service.automation_config.write_text(original,encoding='utf-8')
        other=AssistantService(AutomationClient([]),self.service.config)
        try:
            self.assertEqual(other.automation_settings['carriage'],{'enabled':True,'types':['gem']})
            self.assertEqual(other.automation_settings['gems'],{'enabled':True,'pages':[1]})
            self.assertFalse(other.automation_settings['runes']['enabled'])
            self.assertFalse(other.automation_running)
            self.assertEqual(other.automation_config.read_text(encoding='utf-8'),original)
        finally:other.close()

    def test_invalid_pages_and_types_are_rejected_without_saving(self):
        saved=self.enable(gems={'enabled':True})
        for field,value in [('pages',[True]),('pages',[0]),('pages',[1,1]),('pages',[]),('pages','1')]:
            raw=copy.deepcopy(saved);raw['gems'][field]=value
            with self.subTest(value=value),self.assertRaises(ValidationError):self.service.save_automation(raw)
        raw=copy.deepcopy(saved);raw['carriage']['types']=['gem','gem']
        with self.assertRaises(ValidationError):self.service.save_automation(raw)
        self.assertEqual(json.loads(self.service.automation_config.read_text()),saved)

    def test_target_slot_validation_rejects_malformed_permissions(self):
        for transfer in ({'page_size':True,'storage_indices':[0]}, {'page_size':50,'storage_indices':[0,0]},
                         {'page_size':50,'storage_indices':[-1]}, {'storage_indices':[0]}):
            with self.subTest(transfer=transfer),self.assertRaises(ValidationError):
                storage_targets({'transfer':transfer},[1])
        self.assertEqual(storage_targets({'transfer':{'page_size':50,'storage_indices':[0,49,200,249]}},[5]),[200,249])


if __name__=='__main__':unittest.main()
