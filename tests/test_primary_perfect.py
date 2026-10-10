"""Four primary affixes must fit one enabled combination; secondary is ignored."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from deskrawl_assistant.catalog import load_catalog
from deskrawl_assistant.models import AffixObservation, ItemObservation, Rule
from deskrawl_assistant.rules import evaluate, primary_perfect_rules
from deskrawl_assistant.web_service import AssistantService, BASE
from test_web_service import FakeClient, item

KEYS = ['Stats.Strength','Stats.Dexterity','Stats.Intelligence','Stats.MaxHealth']


class PrimaryPerfectTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(BASE / 'data/game-catalog.json')
        value = AffixObservation(Decimal(10),None,Decimal(1),'primary',Decimal(1))
        self.observation = ItemObservation(equipment_key='LegendaryChestArmor1',name_confidence=Decimal(1),
            groups_complete={'primary':True,'secondary':True},affixes={key:(value,) for key in KEYS})

    def rule(self, keys=KEYS, *, secondary=False, enabled=True, name='组合 A', key='LegendaryChestArmor1'):
        groups = {'primary':{'selected_stats':keys,'operator':'>=','count':3}}
        if secondary:
            groups['secondary'] = {'selected_stats':['Stats.LifeOnHit'],'operator':'>=','count':1}
        return Rule.from_dict({'id':name,'name':name,'equipment_keys':[key],
            'enabled':enabled,'groups':groups,'group_mode':'all'},self.catalog)

    def test_primary_mark_does_not_require_secondary_match_or_change_lock_evaluation(self):
        rule = self.rule(secondary=True)
        self.assertEqual(evaluate(rule,self.observation,self.catalog).status,'miss')
        self.assertEqual(primary_perfect_rules(self.observation,[rule],self.catalog),[rule])

    def test_four_affixes_cannot_be_combined_across_two_pools(self):
        rules = [self.rule(KEYS[:3]),self.rule(KEYS[1:],name='组合 B')]
        self.assertTrue(all(evaluate(r,self.observation,self.catalog).status=='match' for r in rules))
        self.assertEqual(primary_perfect_rules(self.observation,rules,self.catalog),[])

    def test_one_full_enabled_combination_is_sufficient(self):
        rules=[self.rule(KEYS[:3]),self.rule(name='组合 B')]
        self.assertEqual([r.name for r in primary_perfect_rules(self.observation,rules,self.catalog)],['组合 B'])

    def test_disabled_or_different_equipment_is_not_marked(self):
        for rule in (self.rule(enabled=False),self.rule(key='LegendaryChestArmor2')):
            self.assertFalse(primary_perfect_rules(self.observation,[rule],self.catalog))

    def test_incomplete_low_confidence_or_three_primaries_is_not_marked(self):
        for obs in (replace(self.observation,groups_complete={'primary':False}),
                    replace(self.observation,name_confidence=Decimal('0.5')),
                    replace(self.observation,affixes={k:v for k,v in self.observation.affixes.items() if k!=KEYS[-1]})):
            self.assertFalse(primary_perfect_rules(obs,[self.rule()],self.catalog))
        values=dict(self.observation.affixes)
        values[KEYS[-1]]=(replace(values[KEYS[-1]][0],group_confidence=Decimal('0.5')),)
        self.assertFalse(primary_perfect_rules(replace(self.observation,affixes=values),[self.rule()],self.catalog))

    def test_secondary_group_can_be_incomplete_and_is_ignored(self):
        obs=replace(self.observation,groups_complete={'primary':True,'secondary':False})
        self.assertEqual(primary_perfect_rules(obs,[self.rule(secondary=True)],self.catalog),[self.rule(secondary=True)])

    def test_service_preview_excludes_base_armor_from_four_random_affixes(self):
        with tempfile.TemporaryDirectory() as folder:
            row=item(1)
            row.update(internal_name='LegendaryChestArmor1',name_key='LegendaryChestArmor1',equip_slot=3,
                modifiers=[{'stat':8,'stat_name':'Armor','type':0,'value':300,'source':'generated_modifiers'}]+
                    [{'stat':value,'stat_name':name,'type':0,'value':10,'source':'generated_modifiers'}
                     for value,name in ((0,'Strength'),(1,'Dexterity'),(2,'Intelligence'),(7,'MaxHealth'))])
            service=AssistantService(FakeClient([row]),Path(folder)/'rules.json')
            try:
                service.save_rule(self.rule(secondary=True).to_dict())
                service._publish(service.client.snapshot())
                result=service.state()['items'][0]
                self.assertTrue(result['primary_perfect'])
                self.assertEqual(result['primary_perfect_combinations'],['组合 A'])
                self.assertFalse(result['matches'])
                self.assertEqual(len(result['groups']['base']),1)
                self.assertEqual(len(result['groups']['primary']),4)
            finally: service.close()


if __name__ == '__main__': unittest.main()
