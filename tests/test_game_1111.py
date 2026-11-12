"""1.1.11 compatibility boundaries; copied data and shipped assets only."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from deskrawl_assistant.catalog import load_catalog
from deskrawl_assistant.chests import is_chest
from deskrawl_assistant.game_compatibility import CompatibilityError, enum_contract
from deskrawl_assistant.item_protection import disposal_protection, mandatory_storage
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.paths import RESOURCE_ROOT
from deskrawl_assistant.recommendation_reader import talent_ranks


class NewItemTests(unittest.TestCase):
    def test_grandmothers_bow_has_the_hotfix_effect_description(self):
        ui=json.loads((RESOURCE_ROOT/'data/loot-item-ui.json').read_text(encoding='utf-8'))['items']
        bow=ui['DivineBow2']
        self.assertEqual(bow['name'],'祖母的弓')
        self.assertIn('5%',bow['description'])
        self.assertIn('120%',bow['description'])
        self.assertTrue(bow['effect_groups'])
        self.assertFalse(bow['effect_note'])

    def test_ancient_divines_keep_exact_identity_and_own_effects(self):
        data=json.loads((RESOURCE_ROOT/'data/game-catalog.json').read_text(encoding='utf-8'))
        ui=json.loads((RESOURCE_ROOT/'data/loot-item-ui.json').read_text(encoding='utf-8'))['items']
        catalog=load_catalog(RESOURCE_ROOT/'data/game-catalog.json')
        self.assertEqual(len(data['equipment_asset_aliases']),11)
        for key, base in data['equipment_asset_aliases'].items():
            with self.subTest(key=key):
                entry=catalog.equipment[key]
                self.assertEqual(entry.key,key)
                self.assertEqual(entry.label,catalog.equipment[base].label)
                self.assertEqual(catalog.resolve_equipment(key),key)
                self.assertIsNone(catalog.resolve_equipment(entry.label))
                self.assertEqual(ui[key]['rarity'],4)
                self.assertNotEqual(ui[key]['effect_groups'],ui[base]['effect_groups'])
                row={'name_key':key,'is_equipment':True,'base_rarity':4}
                self.assertTrue(mandatory_storage(row))
                self.assertTrue(disposal_protection(row))

    def test_invalid_ancient_alias_cannot_replace_an_existing_item(self):
        data=json.loads((RESOURCE_ROOT/'data/game-catalog.json').read_text(encoding='utf-8'))
        for key, base in (('DivineBow1','DivineBow1'),('Future','Missing'),('Future',None)):
            with self.subTest(key=key,base=base), tempfile.TemporaryDirectory() as folder:
                value=deepcopy(data);value['equipment_asset_aliases']={key:base}
                path=Path(folder)/'catalog.json'
                path.write_text(json.dumps(value),encoding='utf-8')
                with self.assertRaises(ValidationError):load_catalog(path)

    def test_new_chests_join_existing_opening_and_chest_capacity_policy(self):
        for key in ('HeroicChest','RuneChest'):
            self.assertTrue(is_chest({'internal_name':key,'item_type':2,'is_equipment':False}))
            self.assertFalse(is_chest({'internal_name':key,'item_type':3,'is_equipment':True}))


class ActiveTalentLoadoutTests(unittest.TestCase):
    def test_only_selected_loadout_is_used_for_growth_and_character_identity(self):
        copy=Mock();copy.ptr.return_value=2000
        copy.array.return_value=[struct.pack('<Q',x) for x in (3000,4000,5000)]
        copy.dictionary.side_effect=lambda pointer,*args:{pointer:7}
        for index, pointer in enumerate((3000,4000,5000)):
            copy.integer.return_value=index
            self.assertEqual(talent_ranks(copy,1000),{pointer:7})
            copy.dictionary.assert_called_with(pointer,'int','object')

    def test_incomplete_or_out_of_range_loadout_never_defaults_to_first(self):
        for index, count in ((-1,3),(3,3),(0,2),(0,4)):
            copy=Mock();copy.integer.return_value=index
            copy.array.return_value=[struct.pack('<Q',x) for x in range(count)]
            with self.subTest(index=index,count=count), self.assertRaises(MemoryReadError):
                talent_ranks(copy,1000)
            copy.dictionary.assert_not_called()


class EnumIdentityTests(unittest.TestCase):
    def inspector(self, definitions):
        return SimpleNamespace(defs=definitions,meta=struct.pack('<iHi',0,0,0)+b'\x0e',
            section=lambda offset,*args:(0,10,1) if offset==92 else (10,1,1),
            fields=lambda index:[{'isLiteral':True,'fieldIndex':0,'name':'Commodity'}],
            type_at=lambda index:{'resolvedType':'System.Int32'})

    def test_nested_framework_namesake_never_overwrites_game_item_enum(self):
        game={'namespace':'','name':'ItemType','declaringTypeIndex':65535}
        nested={**game,'declaringTypeIndex':7}
        for defs in ([nested,game],[game,nested]):
            self.assertEqual(enum_contract(self.inspector(defs),['ItemType']),{'ItemType':[['Commodity',7]]})

    def test_duplicate_top_level_enum_fails_closed(self):
        game={'namespace':'','name':'ItemType','declaringTypeIndex':65535}
        with self.assertRaises(CompatibilityError):
            enum_contract(self.inspector([game,game]),['ItemType'])


class CubeSubmitContractTests(unittest.TestCase):
    def test_submit_and_cleanup_use_handlers_instead_of_animation_helpers(self):
        profile=json.loads((RESOURCE_ROOT/'data/game-actions-profile.json').read_text(encoding='utf-8'))
        submit=profile['methods']['CubePanelUI.gbp']
        cleanup=profile['methods']['CubePanelUI.gbu']
        self.assertEqual(submit['name'],'guk')
        self.assertEqual([p['resolvedType'] for p in submit['parameters']],['System.Boolean'])
        self.assertEqual(cleanup['name'],'guq')
        self.assertEqual(cleanup['parameters'],[])
