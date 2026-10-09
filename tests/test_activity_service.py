"""Real service workflows: combinations, journals and background observation."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from deskrawl_assistant.automation import DEFAULT_SETTINGS
from deskrawl_assistant.activity_journal import operation_id
from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.web_service import AssistantService
from test_automation import AutomationClient,gem
from test_web_service import item


class ReceiptClient(AutomationClient):
    """Fake transactions emit the same committed receipt as the native client."""
    def move_items(self,expected,target,validate):
        source=copy.deepcopy(expected[0])
        result=super().move_items(expected,target,validate)
        changed=next(r for r in self.items if r['internal_name']==source['internal_name'] and r['container']==target)
        result.update(operation_id=operation_id(),movements=[dict(name=source['internal_name'],
            identity=source.get('item_uid') or '',count=source['count'],merged=False,
            source=dict(container=source['container'],slot_index=source['slot_index'],count_before=source['count'],count_after=0),
            destination=dict(container=target,slot_index=changed['slot_index'],count_before=0,count_after=source['count']))])
        self.item_events('items_transferred',result)
        return result

    def collect_carriage(self,token,validate):
        source=copy.deepcopy(next(r for r in self.drops if r['selection_id']==token))
        result=super().collect_carriage(token,validate)
        changed=self.items[-1]
        result.update(operation_id=operation_id(),movements=[dict(name=source['internal_name'],
            observation_id=source['observation_id'],identity=source.get('item_uid') or '',count=source['count'],merged=False,
            source=dict(container='carriage',slot_index=source['slot_index'],count_before=source['count'],count_after=0),
            destination=dict(container='inventory',slot_index=changed['slot_index'],count_before=0,count_after=source['count']))])
        self.item_events('carriage_collected',result)
        return result


class ActivityServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=ReceiptClient([])
        self.service=AssistantService(self.client,Path(self.tmp.name)/'rules.json')
        self.addCleanup(self.service.close)
        self.service._publish(self.client.snapshot())
        log=patch('deskrawl_assistant.web_service.record');log.start();self.addCleanup(log.stop)

    def events(self,event):return [r for r in self.service.log_page(limit=200)['entries'] if r['event']==event]

    def rule(self,id='first',name='冰伤流',stats=None,enabled=True,secondary=None):
        groups={'primary':dict(selected_stats=stats or ['Stats.CooldownReduction'],operator='>=',count=1)}
        if secondary:groups['secondary']=dict(selected_stats=secondary,operator='>=',count=1)
        return dict(id=id,name=name,equipment_keys=['LegendaryBelt3'],enabled=enabled,groups=groups,group_mode='all')

    def action(self,kind,**payload):
        self.service.start(kind,payload);self.service.worker.join(3)
        self.assertFalse(self.service.busy);self.assertFalse(self.service.error,self.service.error)

    def test_independent_named_combinations_match_any_and_disabled_are_ignored(self):
        self.client.items=[item(0)]
        self.service._publish(self.client.snapshot())
        pool=self.service.catalog_payload()['pools']['Belt']['primary']
        absent=next(s for s in pool if s!='Stats.CooldownReduction')
        self.service.save_rule(self.rule(stats=[absent]))
        second=self.service.save_rule(self.rule('second','冷却流'))
        shown=self.service.state()['items'][0]
        self.assertTrue(shown['matches']);self.assertEqual(shown['matched_combinations'],['冷却流'])
        self.assertTrue(any(reason.startswith('冷却流 · ') for reason in shown['reasons']))
        self.action('lock_rules');self.assertTrue(self.client.items[0]['locked'])
        second['enabled']=False;self.service.save_rule(second)
        self.assertFalse(self.service.state()['items'][0]['matches'])
        self.service.delete_rule('first')
        self.assertEqual([r.id for r in self.service.rules],['second'])

    def test_import_export_and_restart_keep_all_names_conditions_and_enabled_flags(self):
        self.service.save_rule(self.rule())
        self.service.save_rule(self.rule('second','暴击流',enabled=False))
        exported=self.service.export_rules()
        self.assertEqual(len(exported['rules']),2)
        other=AssistantService(ReceiptClient([]),self.service.config)
        try:self.assertEqual(other.export_rules(),exported)
        finally:other.close()
        self.service.import_rules(exported)
        self.assertEqual(len(self.service.rules),4)
        self.assertTrue(all(not r.enabled for r in self.service.rules[2:]))

    def test_primary_and_secondary_hits_cannot_be_combined_between_combinations(self):
        gear=item(0)
        secondary_stat=next(value for value,name in self.service.adapter.stat_names.items() if name=='HpPotionFind')
        gear['modifiers'].append(dict(stat=secondary_stat,stat_name='HpPotionFind',type=0,value=1,source='generated_modifiers'))
        self.client.items=[gear];self.service._publish(self.client.snapshot())
        self.service.save_rule(self.rule('primary-hit','主达标副未达标',secondary=['Stats.LifeRegen']))
        self.service.save_rule(self.rule('secondary-hit','副达标主未达标',stats=['Stats.Strength'],secondary=['Stats.HpPotionFind']))
        self.assertFalse(self.service.state()['items'][0]['matches'])
        self.action('lock_rules');self.assertFalse(self.client.actions)
        self.service.save_rule(self.rule('third','第三组合完整达标'))
        self.action('lock_rules');self.assertEqual(len(self.client.actions),1)

    def test_all_observed_additions_are_logged_without_enabling_automation(self):
        self.client.items=[gem(5,count=7)]
        self.client.drops=[dict(gem(3,count=12),selection_id='drop',observation_id='stable-drop',collectable=True)]
        self.service._work('observe',{})
        self.assertFalse(self.service.monitoring);self.assertFalse(self.service.automation_running)
        self.assertEqual(self.events('item_acquired')[0]['quantity'],7)
        self.assertEqual(self.events('carriage_acquired')[0]['quantity'],12)
        self.assertEqual(self.events('carriage_acquired')[0]['destination']['slot_index'],3)
        self.service._work('observe',{})
        self.assertEqual(len(self.events('item_acquired')),1)

    def test_idle_connection_observer_actually_runs_in_background(self):
        self.client.items=[gem(1,count=2)]
        deadline=time.monotonic()+3.5
        while time.monotonic()<deadline and not self.events('item_acquired'):time.sleep(.03)
        self.assertEqual(self.events('item_acquired')[0]['quantity'],2)
        self.assertFalse(self.client.events)

    def test_tool_carriage_to_bag_then_storage_logged_once_with_exact_counts(self):
        settings=copy.deepcopy(DEFAULT_SETTINGS)
        settings['carriage'].update(enabled=True,items=['GemEmerald4']);settings['gems']['enabled']=True
        self.service.save_automation(settings);self.service.set_automation_running(True)
        self.client.drops=[dict(gem(4,count=8),selection_id='drop',observation_id='stable-drop',collectable=True)]
        self.service.operation_kind='monitor'
        self.service._automation_once();self.service._automation_once()
        collected=self.events('carriage_collected');deposited=self.events('items_transferred')
        self.assertEqual(len(collected),1);self.assertEqual(len(deposited),1)
        self.assertEqual(collected[0]['quantity'],8);self.assertEqual(deposited[0]['quantity'],8)
        self.assertEqual(collected[0]['source']['slot_index'],4)
        self.assertEqual(collected[0]['destination']['container'],'inventory')
        self.assertEqual(deposited[0]['source']['count_after'],0)
        self.assertEqual(deposited[0]['destination']['container'],'storage')
        self.assertTrue(collected[0]['context']['automatic'])
        self.assertEqual(len(self.events('carriage_acquired')),1)
        self.assertFalse(self.events('item_acquired'));self.assertFalse(self.events('game_item_moved'))

    def test_lock_and_unlock_receipts_include_identity_position_and_quantity(self):
        self.client.items=[item(6,'storage')]
        self.service._publish(self.client.snapshot());self.service.save_rule(self.rule())
        self.action('lock_rules')
        locked=self.events('equipment_locked')[0]
        self.assertEqual(locked['quantity'],1)
        self.assertEqual(locked['destination'],dict(container='storage',slot_index=6))
        self.assertEqual(locked['context']['matched_combinations'],['冰伤流'])
        self.action('unlock_all')
        self.assertFalse(self.events('equipment_unlocked')[0]['context']['locked_after'])

    def test_partial_lock_batch_keeps_success_and_specific_failure_events(self):
        self.client.items=[item(2),item(3)];self.service._publish(self.client.snapshot());self.service.save_rule(self.rule())
        original=self.client.lock_equipment
        def lock(row,validate):
            if row['slot_index']==3:raise MemoryReadError('simulated second failure')
            return original(row,validate)
        with patch.object(self.client,'lock_equipment',side_effect=lock):
            self.service.start('lock_rules');self.service.worker.join(3)
        self.assertEqual(len(self.events('equipment_locked')),1)
        self.assertEqual(self.events('lock_failed')[0]['source']['slot_index'],3)
        self.assertTrue(self.events('operation_failed'))
        self.assertTrue(self.client.items[0]['locked']);self.assertFalse(self.client.items[1]['locked'])

    def test_confirmed_transfer_is_logged_even_if_following_read_fails(self):
        self.client.items=[gem(0,count=2)];self.service._publish(self.client.snapshot())
        token=self.client.items[0]['transfer_key'];original=self.client.move_items
        def commit_then_fail(*args):
            original(*args)
            raise MemoryReadError('read after commit failed')
        with patch.object(self.client,'move_items',side_effect=commit_then_fail):
            self.service.start('move_items',dict(items=[token],target='storage'));self.service.worker.join(3)
        self.assertEqual(len(self.events('items_transferred')),1)
        self.assertEqual(self.events('items_transferred')[0]['quantity'],2)
        self.assertEqual(len(self.events('item_acquired')),1)
        self.assertFalse(self.events('game_item_moved'));self.assertTrue(self.events('operation_failed'))
        failed=self.events('item_transfer_failed')[0]
        self.assertEqual(failed['quantity'],2)
        self.assertEqual(failed['source']['slot_index'],0)
        self.assertEqual(failed['destination']['container'],'storage')


if __name__=='__main__':unittest.main()
