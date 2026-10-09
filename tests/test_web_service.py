"""Application-level tests with a synthetic fixture and a fake game client."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from deskrawl_assistant.web_service import AssistantService, BASE
from deskrawl_assistant.rules import rules_from_dict
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.native_memory import SnapshotChangedError

SOURCE=json.loads((Path(__file__).parent/'fixtures/service-snapshot.json').read_text(encoding='utf-8'))
BELT=next(row for c in SOURCE['containers'].values() for row in c['slots'] if row.get('name_key')=='LegendaryBelt3')


def item(index,container='inventory',locked=False):
    row=copy.deepcopy(BELT)
    row.update(item_uid='test-uid-'+str(index),instance_id='test-instance-'+str(index),
               slot_index=index,container=container,locked=locked)
    return row


class FakeClient:
    def __init__(self,items):
        self.connected=True
        self.pid=999
        self.items=copy.deepcopy(items)
        self.actions=[]
        self.capture=0
    def snapshot(self):
        self.capture+=1
        snap=copy.deepcopy(SOURCE)
        snap.update(pid=self.pid,bridge_session='test-session',snapshot_id='test-'+str(self.capture))
        for name,c in snap['containers'].items():
            c['slots']=[copy.deepcopy(i) for i in self.items if i['container']==name]
            c['occupied_count']=len(c['slots'])
        return snap
    def connect(self): self.connected=True
    def close(self): self.connected=False
    def _set(self,row,validate,locked):
        actual=next(i for i in self.items if i['instance_id']==row['instance_id'])
        if not validate(actual): raise ValueError('Validation stopped this simulated write')
        actual['locked']=locked
        self.actions.append((actual['instance_id'],locked))
        return {'status':'locked' if locked else 'unlocked'}
    def lock_equipment(self,row,validate): return self._set(row,validate,True)
    def unlock_equipment(self,row,validate): return self._set(row,validate,False)


class WebServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config=Path(self.tmp.name)/'rules.json'
        self.client=FakeClient([item(1,locked=True),item(2,'storage',locked=True),item(3)])
        self.service=AssistantService(self.client,self.config)
        self.service._publish(self.client.snapshot())
        self.addCleanup(self.service.close)
        self.log=patch('deskrawl_assistant.web_service.record')
        self.log.start();self.addCleanup(self.log.stop)

    def rule(self):
        return {'id':'test-rule','name':'战饮腰带','equipment_keys':['LegendaryBelt3'],'enabled':True,
            'groups':{'primary':{'selected_stats':['Stats.CooldownReduction'],'operator':'>=','count':1}},'group_mode':'all'}

    def run_action(self,kind,**payload):
        self.service.start(kind,payload)
        self.service.worker.join(3)
        self.assertFalse(self.service.busy)
        self.assertFalse(self.service.error,self.service.error)

    def test_catalog_has_all_52_legendary_icons_and_real_class_masks(self):
        catalog=self.service.catalog_payload()
        legendaries=[i for i in catalog['equipment'] if i['legendary']]
        self.assertEqual(len(legendaries),52)
        for i in legendaries:
            self.assertTrue((BASE/'deskrawl_assistant/web'/i['icon'].lstrip('/')).is_file())
        values={c['key']:c['value'] for c in catalog['classes']}
        self.assertEqual(values,{'Barbarian':1,'Mage':2,'Hunter':4,'Monk':8})

    def publish_physical_items(self):
        gear = item(1,locked=True)
        gear['transfer_key'] = 'gear-token'
        extras = [dict(container='storage',slot_index=n+10,count=n+3,item_uid='',
                    is_equipment=False,internal_name=name,item_class=klass,locked=False,
                    modifiers=[],transfer_key='stack-'+str(n))
                  for n,(name,klass) in enumerate([('GemEmerald4','GemData'),('TreasureChest2','ItemData'),('Iron','ItemData')])]
        self.client.items = [gear,*extras]
        snapshot = self.client.snapshot()
        snapshot['transfer'] = {'available':True,'storage_indices':list(range(250))}
        self.service._publish(snapshot)
        return snapshot

    def test_material_gem_chest_are_listed_but_excluded_from_equipment_counts(self):
        self.publish_physical_items()
        state = self.service.state()
        self.assertEqual(len(state['items']),4)
        self.assertEqual(state['counts']['inventory']['equipment'],1)
        self.assertEqual(state['counts']['storage']['equipment'],0)
        self.assertEqual({i['kind'] for i in state['items']},{'equipment','gem','chest','material'})
        chest = next(i for i in state['items'] if i['kind']=='chest')
        self.assertEqual(chest['name'],'黄金宝箱')
        self.assertFalse(chest['review'])
        self.assertTrue(all(i['selection_id'] and i['movable'] for i in state['items']))

    def test_gem_and_rune_previews_include_bundled_icons_and_effects(self):
        self.publish_physical_items()
        self.client.items.append(dict(container='inventory',slot_index=3,count=1,item_uid='rune-test',is_equipment=False,
            internal_name='UncommonRune_DamageVsHealthy',item_class='RuneData',locked=False,modifiers=[],transfer_key='rune-token'))
        self.service._publish(self.client.snapshot())
        state=self.service.state()
        for kind in ('gem','rune'):
            row=next(i for i in state['items'] if i['kind']==kind)
            self.assertTrue(row['icon']);self.assertTrue(row['effect_groups']);self.assertFalse(row['matches'])
            self.assertTrue((BASE/'deskrawl_assistant/web'/row['icon'].lstrip('/')).exists())

    def test_selected_tokens_are_resolved_server_side_and_preserve_locked_gear(self):
        self.publish_physical_items()
        def move(expected,target,validate):
            self.assertTrue(validate())
            self.assertEqual(expected[0]['transfer_key'],'gear-token')
            self.assertTrue(expected[0]['locked'])
            self.assertEqual(target,'storage')
            self.client.items[0]['container'] = target
            return {'moved':1}
        with patch.object(self.client,'move_items',side_effect=move,create=True) as operation:
            self.run_action('move_items',items=['gear-token'],target='storage',expected=[{'arbitrary_address':123}])
            operation.assert_called_once()
        self.assertTrue(self.client.items[0]['locked'])
        self.assertIn('移入仓库 1 组',self.service.message)

    def test_moving_rejects_stale_duplicate_or_wrong_direction_selection(self):
        self.publish_physical_items()
        for tokens,target in [(['missing'],'storage'),(['gear-token','gear-token'],'storage'),(['stack-0'],'storage'),(['gear-token'],'invalid')]:
            with self.subTest(tokens=tokens,target=target),self.assertRaises(ValidationError):
                self.service.start('move_items',{'items':tokens,'target':target})
        self.assertFalse(self.service.busy)

    def test_lock_rules_never_process_physical_non_equipment(self):
        self.publish_physical_items()
        self.service.save_rule(self.rule())
        self.run_action('lock_rules')
        self.assertFalse(self.client.actions)

    def test_persist_and_export_round_trip(self):
        self.service.save_rule(self.rule())
        self.assertEqual(len(rules_from_dict(self.service.export_rules(),self.service.catalog)),1)
        saved=json.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(saved['rules'][0]['groups']['primary']['operator'],'>=')

    def test_invalid_threshold_or_ineligible_slot_stat_does_not_save(self):
        rule=self.rule();rule['groups']['primary']['count']=2
        with self.assertRaises(ValidationError):self.service.save_rule(rule)
        rule=self.rule();rule['groups']['primary']['selected_stats']=['Stats.WeaponDamage']
        with self.assertRaises(ValidationError):self.service.save_rule(rule)
        self.assertFalse(self.config.exists())

    def test_unlock_all_stops_monitor_and_unlocks_both_containers(self):
        self.service.monitoring=True
        self.run_action('unlock_all',scope='all')
        self.assertFalse(self.service.monitoring)
        self.assertEqual(self.client.actions,[('test-instance-1',False),('test-instance-2',False)])
        self.assertFalse(any(i['locked'] for i in self.client.items))

    def test_unlock_scope_does_not_touch_other_container(self):
        self.run_action('unlock_all',scope='inventory')
        self.assertEqual(self.client.actions,[('test-instance-1',False)])
        self.assertTrue(self.client.items[1]['locked'])

    def test_selected_lock_uses_identity_after_container_move(self):
        target=item(3)
        self.client.items[2]['container']='storage'
        self.run_action('lock_selected',items=[target])
        self.assertEqual(self.client.actions,[('test-instance-3',True)])

    def test_rule_lock_only_targets_matching_unlocked_items(self):
        self.service.save_rule(self.rule())
        self.run_action('lock_rules',scope='all')
        self.assertEqual(self.client.actions,[('test-instance-3',True)])

    def test_monitor_baselines_existing_and_locks_new_storage_equipment(self):
        self.service.save_rule(self.rule())
        self.service.set_monitoring(True)
        self.service._monitor_once()
        self.assertFalse(self.client.actions)
        self.client.items.append(item(4,'storage'))
        self.service._monitor_once()
        self.assertEqual(self.client.actions,[('test-instance-4',True)])
        self.service._monitor_once()
        self.assertEqual(len(self.client.actions),1)

    def test_monitor_moving_old_equipment_does_not_lock(self):
        self.service.save_rule(self.rule())
        self.service.set_monitoring(True);self.service._monitor_once()
        self.client.items[2]['container']='storage'
        self.service._monitor_once()
        self.assertFalse(self.client.actions)

    def test_transient_read_preserves_monitor_state_and_locks_new_item_next_tick(self):
        self.service.save_rule(self.rule())
        self.service.set_monitoring(True)
        self.service._monitor_once()
        previous = self.service.loot_state
        self.client.items.append(item(4, 'storage'))
        self.service.automation_running = True
        with patch.object(self.client, 'snapshot', side_effect=SnapshotChangedError('装备注册表在读取期间发生变化')):
            self.service._work('monitor', {})
        self.assertTrue(self.service.monitoring)
        self.assertTrue(self.service.automation_running)
        self.assertIs(self.service.loot_state, previous)
        self.assertFalse(self.service.error)
        self.assertFalse(self.client.actions)
        self.service.automation_running = False
        self.service._work('monitor', {})
        self.assertEqual(self.client.actions, [('test-instance-4', True)])
        self.assertFalse(self.client.items[2]['locked'])

    def test_transient_write_guard_preserves_pending_new_equipment(self):
        self.service.save_rule(self.rule())
        self.service.set_monitoring(True)
        self.service._monitor_once()
        self.client.items.append(item(4))
        with patch.object(self.client, 'lock_equipment', side_effect=SnapshotChangedError('写入前装备注册表发生变化')):
            self.service._work('monitor', {})
        self.assertTrue(self.service.monitoring)
        self.assertFalse(self.client.actions)
        self.service._work('monitor', {})
        self.assertEqual(self.client.actions, [('test-instance-4', True)])

    def test_import_preserves_existing_rules_and_disables_additions(self):
        self.service.save_rule(self.rule())
        self.service.import_rules({'version':2,'rules':[self.rule()]})
        self.assertEqual(len(self.service.rules),2)
        self.assertTrue(self.service.rules[0].enabled)
        self.assertFalse(self.service.rules[1].enabled)

    def test_cancelled_batch_does_not_write(self):
        self.service.cancelled.set()
        self.service._apply([item(1)],unlock=True)
        self.assertFalse(self.client.actions)

    def test_corrupt_config_is_preserved(self):
        path=Path(self.tmp.name)/'bad.json';path.write_text('{broken',encoding='utf-8')
        other=AssistantService(FakeClient([]),path)
        try:
            with self.assertRaises(ValidationError):other.save_rule(self.rule())
            self.assertEqual(path.read_text(encoding='utf-8'),'{broken')
        finally:other.close()

    def test_state_displays_rule_match_for_locked_equipment(self):
        self.service.save_rule(self.rule())
        state=self.service.state()
        self.assertTrue(all(i['matches'] for i in state['items']))
        self.assertEqual(state['counts']['inventory']['matches'],1)


if __name__=='__main__':unittest.main()
