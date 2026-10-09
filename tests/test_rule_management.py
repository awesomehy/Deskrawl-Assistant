"""Rule management must preserve unrelated configurations and source settings."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.web_service import AssistantService
from test_web_service import FakeClient

SOURCE='LegendaryRing2'
TARGET='LegendaryRing1'


class RuleManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.config=Path(self.tmp.name)/'rules.json'
        self.client=FakeClient([])
        self.service=AssistantService(self.client,self.config);self.addCleanup(self.service.close)

    def rule(self,id='source',key=SOURCE,enabled=True,secondary=False):
        data={'id':id,'name':'暴击流','equipment_keys':[key],'enabled':enabled,
            'groups':{'primary':{'selected_stats':['Stats.Dexterity','Stats.CritChance'],'operator':'>=','count':2}},
            'group_mode':'all'}
        if secondary:
            pool=next(p for p in self.service.pools['slotPools'] if p['slot']=='Ring')['secondary']
            data['groups']['secondary']={'selected_stats':pool[:1],'operator':'>=','count':1}
        return data

    def test_delete_selected_preserves_other_combinations_and_persists(self):
        for id,key in [('first',SOURCE),('second',SOURCE),('other',TARGET)]:
            self.service.save_rule(self.rule(id,key))
        self.assertEqual(self.service.delete_rules(['first'],SOURCE),1)
        self.assertEqual([r.id for r in self.service.rules],['second','other'])
        self.assertEqual([r['id'] for r in json.loads(self.config.read_text())['rules']],['second','other'])
        self.assertFalse(self.client.actions)

    def test_delete_all_leaves_a_valid_empty_file(self):
        self.service.save_rule(self.rule())
        self.service.delete_rules(['source'],SOURCE)
        self.assertEqual(json.loads(self.config.read_text()),{'version':2,'rules':[]})

    def test_invalid_batch_delete_is_atomic(self):
        self.service.save_rule(self.rule())
        before=self.config.read_bytes()
        for ids,key in [(['source','missing'],SOURCE),(['source','source'],SOURCE),([],SOURCE),(['source'],TARGET)]:
            with self.assertRaises(ValidationError):self.service.delete_rules(ids,key)
            self.assertEqual(self.config.read_bytes(),before)

    def test_scoped_deletion_preserves_legacy_rule_on_other_equipment(self):
        legacy={'id':'legacy','name':'旧规则','equipment_keys':[SOURCE,TARGET],'enabled':False,
            'conditions':[{'stat_key':'Stats.Dexterity','operator':'>=','value':'10','unit':'flat'}],'mode':'all'}
        self.service.import_rules({'version':1,'rules':[legacy]})
        id=self.service.rules[0].id
        self.service.delete_rules([id],SOURCE)
        self.assertEqual(self.service.rules[0].equipment_keys,(TARGET,))

    def test_import_can_enable_every_incoming_rule_without_changing_existing(self):
        self.service.save_rule(self.rule('existing',enabled=False))
        incoming=[self.rule('incoming',enabled=False),self.rule('incoming-2',enabled=True)]
        self.service.import_rules({'version':2,'rules':incoming},True)
        self.assertFalse(self.service.rules[0].enabled)
        self.assertTrue(all(r.enabled for r in self.service.rules[1:]))
        self.assertEqual(len({r.id for r in self.service.rules}),3)
        self.assertFalse(self.client.actions)

    def test_import_default_or_explicit_disabled_disables_incoming_only(self):
        self.service.save_rule(self.rule())
        self.service.import_rules({'version':2,'rules':[self.rule('a')]})
        self.service.import_rules({'version':2,'rules':[self.rule('b')]},False)
        self.assertEqual([r.enabled for r in self.service.rules],[True,False,False])

    def test_invalid_import_state_or_payload_never_changes_file(self):
        self.service.save_rule(self.rule());before=self.config.read_bytes()
        for enabled in ('true',1,None):
            with self.assertRaises(ValidationError):self.service.import_rules({'version':2,'rules':[self.rule()]},enabled)
        with self.assertRaises(ValidationError):self.service.import_rules({'version':2,'rules':[{}]},True)
        self.assertEqual(self.config.read_bytes(),before)

    def test_copy_all_preserves_name_primary_secondary_and_enabled_state(self):
        source=[self.rule('one',secondary=True),self.rule('two',enabled=False)]
        source[1]['name']='另一个流派'
        for rule in source:self.service.save_rule(rule)
        original=[r.to_dict() for r in self.service.rules]
        result=self.service.copy_rules(SOURCE,TARGET)
        self.assertEqual(result['copied'],2)
        self.assertEqual([r.to_dict() for r in self.service.rules[:2]],original)
        for original,copied in zip(original,self.service.rules[2:]):
            self.assertNotEqual(copied.id,original['id'])
            self.assertEqual(copied.equipment_keys,(TARGET,))
            for key in ('name','enabled','groups','group_mode'):self.assertEqual(copied.to_dict()[key],original[key])
        self.assertFalse(self.client.actions)

    def test_copy_one_and_append_preserves_target_and_unrelated_rules(self):
        for rule in [self.rule('one'),self.rule('two',enabled=False),self.rule('target',TARGET,False),self.rule('other','LegendaryRing3')]:
            self.service.save_rule(rule)
        before=[r.to_dict() for r in self.service.rules]
        result=self.service.copy_rules(SOURCE,TARGET,['one'])
        self.assertEqual(result['copied'],1)
        self.assertEqual([r.to_dict() for r in self.service.rules[:4]],before)

    def test_repeat_copy_and_reordered_stat_pool_do_not_create_redundant_records(self):
        self.service.save_rule(self.rule())
        self.service.copy_rules(SOURCE,TARGET)
        source=self.rule();source['groups']['primary']['selected_stats'].reverse()
        self.service.save_rule(source)
        result=self.service.copy_rules(SOURCE,TARGET)
        self.assertEqual((result['copied'],result['skipped']),(0,1))
        self.assertEqual(len(self.service.rules),2)

    def test_replace_removes_target_only_and_preserves_source(self):
        self.service.save_rule(self.rule())
        self.service.save_rule(self.rule('old',TARGET,False))
        self.service.save_rule(self.rule('unrelated','LegendaryRing3'))
        original=self.service.rules[0].to_dict()
        result=self.service.copy_rules(SOURCE,TARGET,replace_existing=True,expected_target_ids=['old'])
        self.assertEqual(result['replaced'],1)
        self.assertNotIn('old',[r.id for r in self.service.rules])
        self.assertEqual(self.service.rules[0].to_dict(),original)
        self.assertIn('unrelated',[r.id for r in self.service.rules])

    def test_copy_legacy_rule_keeps_numeric_conditions(self):
        legacy={'id':'legacy','name':'旧规则','equipment_keys':[SOURCE],'enabled':False,
            'conditions':[{'stat_key':'Stats.Dexterity','operator':'>=','value':'10','unit':'flat'}],'mode':'all'}
        self.service.import_rules({'version':1,'rules':[legacy]})
        original=self.service.rules[0]
        self.service.copy_rules(SOURCE,TARGET)
        self.assertEqual(self.service.rules[1].conditions,original.conditions)
        self.assertFalse(self.service.rules[1].enabled)

    def test_copy_wrong_slot_self_missing_or_stale_selection_is_atomic(self):
        self.service.save_rule(self.rule());before=self.config.read_bytes()
        for source,target,ids in [(SOURCE,'LegendaryBelt3',None),(SOURCE,SOURCE,None),
            ('missing',TARGET,None),(SOURCE,'missing',None),(SOURCE,TARGET,['missing']),
            (SOURCE,TARGET,['source','source']),(TARGET,SOURCE,None)]:
            with self.assertRaises(ValidationError):self.service.copy_rules(source,target,ids)
            self.assertEqual(self.config.read_bytes(),before)

    def test_replace_rejects_changed_target_and_non_boolean_mode(self):
        self.service.save_rule(self.rule());self.service.save_rule(self.rule('old',TARGET,False))
        before=self.config.read_bytes()
        with self.assertRaises(ValidationError):
            self.service.copy_rules(SOURCE,TARGET,replace_existing=True,expected_target_ids=[])
        with self.assertRaises(ValidationError):self.service.copy_rules(SOURCE,TARGET,replace_existing='true')
        self.assertEqual(self.config.read_bytes(),before)

    def test_failed_persistence_keeps_in_memory_rules(self):
        self.service.save_rule(self.rule());before=[r.to_dict() for r in self.service.rules]
        with patch.object(Path,'replace',side_effect=OSError('access denied')):
            with self.assertRaises(OSError):self.service.copy_rules(SOURCE,TARGET)
        self.assertEqual([r.to_dict() for r in self.service.rules],before)


if __name__=='__main__':unittest.main()
