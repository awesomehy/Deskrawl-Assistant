"""Preview affix grouping uses the same slot pools/count rules as inventory."""
import json
from pathlib import Path
import struct
import unittest

from deskrawl_assistant.catalog import load_catalog
from deskrawl_assistant.loot_plan_reader import _labels, describe_item, read_loot_plan
from deskrawl_assistant.loot_preview import decorate_equipment
from deskrawl_assistant.models import Rule
from deskrawl_assistant.paths import RESOURCE_ROOT
from deskrawl_assistant.runtime_adapter import RuntimeAdapter
from tests.test_loot_plan_reader import Fixture


class PreviewDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog(RESOURCE_ROOT/'data/game-catalog.json')
        cls.adapter = RuntimeAdapter(cls.catalog,RESOURCE_ROOT/'data/affix-groups-static.json')

    def item(self,mods,key='LegendaryChestArmor2',slot='Chest'):
        f=Fixture()
        obj=f.item(key,rarity='Legendary',modifiers=mods)
        f.text(obj+32,slot)
        f.set_waves([[obj]])
        return read_loot_plan(f.reader)['waves'][0]['items'][0]

    def rule(self,keys,n=3,enabled=True,key='LegendaryChestArmor2',name='测试组合'):
        return Rule.from_dict({'id':name,'name':name,'enabled':enabled,'equipment_keys':[key],
            'groups':{'primary':{'selected_stats':['Stats.'+k for k in keys],'count':n,'operator':'>='}}},self.catalog)

    def test_armour_is_base_and_does_not_raise_two_matches_to_three(self):
        item=self.item([('Armor','Flat',100),('SpecialAbilityDamage','Flat',.1),
            ('DamageVsInjured','Flat',.1),('DamageVsHealthy','Flat',.1),('PhysicalDamageReduction','Flat',.1)])
        rule=self.rule(['Armor','DamageVsHealthy','PhysicalDamageReduction'])
        decorate_equipment(item,[rule],self.adapter,self.catalog)
        self.assertEqual([r['key'] for r in item['groups']['base']],['Stats.Armor'])
        self.assertEqual(sum(r['matched'] for r in item['groups']['primary']),2)
        self.assertFalse(item['groups']['base'][0]['matched'])
        self.assertFalse(item['matches'])

    def test_weapon_headlines_and_primary_secondary_are_separate(self):
        key='LegendaryBow1'
        if key not in self.catalog.equipment:
            key=next(k for k,v in _labels()['equipment'].items() if v['slot']=='Weapon' and v['rarity_value']==3)
        item=self.item([('WeaponSpeed','Flat',1),('WeaponDamage','Flat',300),
            ('Dexterity','Flat',60),('LifeOnKill','Flat',15)],key=key,slot='Weapon')
        rule=self.rule(['Dexterity'],1,key=key)
        decorate_equipment(item,[rule],self.adapter,self.catalog)
        self.assertEqual(len(item['groups']['base']),2)
        self.assertEqual(item['groups']['primary'][0]['key'],'Stats.Dexterity')
        self.assertEqual(item['groups']['secondary'][0]['key'],'Stats.LifeOnKill')
        self.assertTrue(item['groups']['primary'][0]['matched'])
        self.assertTrue(item['matches'])
        self.assertNotIn('selection_id',item)
        self.assertNotIn('instance_id',item)

    def test_disabled_rules_do_not_highlight_and_unknown_enum_stays_unconfirmed(self):
        item=self.item([('Dexterity','Flat',60),('UnknownStat','Flat',1)])
        decorate_equipment(item,[self.rule(['Dexterity'],1,False)],self.adapter,self.catalog)
        self.assertTrue(item['groups']['unknown'])
        self.assertFalse(any(r['matched'] for rows in item['groups'].values() for r in rows))
        self.assertFalse(item['matches'])

    def test_four_primary_perfect_requires_one_combination_covering_all(self):
        keys=['Dexterity','MaxHealth','MagicResist','Dodge']
        item=self.item([(k,'Flat',.1) for k in keys])
        decorate_equipment(item,[self.rule(keys)],self.adapter,self.catalog)
        self.assertTrue(item['primary_perfect'])
        other=self.item([(k,'Flat',.1) for k in keys])
        decorate_equipment(other,[self.rule(keys[:2],1,name='甲'),self.rule(keys[2:],1,name='乙')],self.adapter,self.catalog)
        self.assertFalse(other['primary_perfect'])

    def test_complete_shipped_catalogue_has_local_icons_and_effects_or_explicit_note(self):
        assets=json.loads((RESOURCE_ROOT/'data/loot-item-ui.json').read_text(encoding='utf-8'))
        items=assets['items']
        self.assertEqual(len(items),602)
        self.assertEqual(sum(r['kind']=='gem' for r in items.values()),36)
        self.assertEqual(sum(r['kind']=='rune' for r in items.values()),199)
        for key,row in items.items():
            with self.subTest(key=key):
                self.assertTrue(row['name'])
                self.assertTrue(row['effect_groups'] or row['effect_note'])
                image=RESOURCE_ROOT/'deskrawl_assistant/web'/row['icon'].lstrip('/')
                raw=image.read_bytes()
                self.assertEqual(raw[:8],b'\x89PNG\r\n\x1a\n')
                self.assertTrue(all(v>0 for v in struct.unpack('>II',raw[16:24])))
        self.assertEqual(assets['rarity_colors']['rune_set'],'#33ff4c')

    def test_werewolf_material_is_resolved_with_shipped_description_and_icon(self):
        fixture = Fixture()
        fixture.set_waves([[fixture.item('WerewolfMat', rarity='Common')]])
        row = read_loot_plan(fixture.reader)['waves'][0]['items'][0]
        self.assertEqual(row['name'], '狼人毛皮')
        self.assertEqual(row['kind'], 'material')
        self.assertEqual(row['rarity'], 2)
        texts = [text for group in row['effect_groups'] for text in group['texts']]
        self.assertIn('取自血月狼人的厚实毛皮。', texts)
        image = RESOURCE_ROOT/'deskrawl_assistant/web'/row['icon'].lstrip('/')
        self.assertTrue(image.is_file())

    def test_dynamic_equipment_descriptions_are_exported_with_resolved_parameters(self):
        items = _labels()['items']
        expected = {'LegendaryBoots4':('10%','1 秒','立即饮用'),
                    'DivineHelm1':('30%',), 'DivineHelm3':('20%','40%'),
                    'DivineRing2':('健康和受伤',), 'LegendaryNeck4':('30%','烈焰雷电'),
                    'LegendaryShoulder3':('10%','击杀效果')}
        for key,phrases in expected.items():
            with self.subTest(key=key):
                row = items[key]
                self.assertTrue(row['description'])
                self.assertFalse(row['effect_note'])
                self.assertIn(row['description'], [t for g in row['effect_groups'] for t in g['texts']])
                self.assertNotIn('{', row['description'])
                for phrase in phrases:
                    self.assertIn(phrase, row['description'])

    def test_rune_quality_uses_base_asset_even_if_protocol_says_common(self):
        for rarity,label in ((1,'优秀符文'),(2,'稀有符文'),(3,'宏伟符文'),(5,'符文套装')):
            key=next(k for k,v in _labels()['items'].items() if v.get('kind')=='rune' and v.get('rarity')==rarity)
            for wire in ('','Common'):
                f=Fixture();f.set_waves([[f.item(key,rarity=wire)]])
                item=read_loot_plan(f.reader)['waves'][0]['items'][0]
                self.assertEqual(item['rarity'],rarity)
                self.assertEqual(item['rarity_label'],label)
                self.assertEqual(item['rarity_source'],'base_item')
                self.assertTrue(item['effect_groups'])

    def test_private_raw_capture_is_complete_and_never_exposed_in_public_preview(self):
        f=Fixture();f.set_waves([[f.item(modifiers=[('Strength','Flat',33)])]])
        raw={}
        public=read_loot_plan(f.reader,raw_sink=raw)
        self.assertTrue(public['available'])
        self.assertEqual(raw['runId'],'private-run-identity')
        item=raw['waves'][0]['loot']['items'][0]
        self.assertEqual(set(item),{'dropId','baseItemId','slot','rarity','itemLevel','ancient','blackMist',
            'modifiers','sockets','qty','chestGold'})
        self.assertEqual(item['modifiers'][0],{'stat':'Strength','type':'Flat','value':33.0})
        self.assertNotIn('private-run',json.dumps(public))
        self.assertNotIn('private-drop',json.dumps(public))
        f.integer(f.area+120,1)
        self.assertFalse(read_loot_plan(f.reader,raw_sink=raw)['available'])
        self.assertEqual(raw,{})

    def test_invalid_snapshot_cannot_publish_a_partial_raw_capture(self):
        f=Fixture();f.set_waves([[f.item()],[]])
        raw={'previous':'must be cleared'}
        def transition(address,size):
            if address==f.loot_objects[-1]+16:f.integer(f.area+120,1)
        f.memory.after_read=transition
        result=read_loot_plan(f.reader,raw_sink=raw)
        self.assertFalse(result['available'])
        self.assertEqual(raw,{})


if __name__=='__main__': unittest.main()
