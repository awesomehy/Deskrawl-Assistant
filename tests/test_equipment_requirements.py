"""Equipment level gates, automatic selection and fresh execution checks.

All game reads/actions are synthetic. These tests cannot equip live gear.
"""
from copy import deepcopy
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.character import final_stats, panel_attack, preview
from deskrawl_assistant.equipment_requirements import equip_eligibility, required_level
from deskrawl_assistant.game_actions import GameActions
from deskrawl_assistant.managed_mode import ManagedMode, equip_candidate_allowed
from deskrawl_assistant.native_dispatcher import NativeActionError
from deskrawl_assistant.native_memory import SnapshotChangedError
from deskrawl_assistant.web_service import AssistantService
from test_managed_mode import equipment, observation, settings
from test_managed_service import FakeManagedClient, equipment as service_equipment


def candidate(uid='upgrade', *, item_level=100, delta=100, container='inventory', **extra):
    return equipment(uid, container=container, item_level=item_level, equip_slot=6,
        base_rarity=3, class_mask=15, power={'available':True,'target':5,'delta':delta},
        **extra)


def hero(level=10, **extra):
    return {'available':True,'level':level,'max_level':200,'equipment':[],**extra}


class EquipmentRequirementTests(unittest.TestCase):
    def test_native_normal_item_level_boundaries_and_clamping(self):
        # The raw item level is divided by ten, then clamped. Upgrade level
        # and a displayed rarity override do not change this native gate.
        for item_level, expected in ((-10,1),(0,1),(1,1),(9,1),(10,1),(19,1),
                                     (20,2),(99,9),(100,10),(1999,199),(2000,200),(99999,200)):
            with self.subTest(item_level=item_level):
                row = candidate(item_level=item_level, upgrade_level=999, rarity=4)
                self.assertEqual(required_level(row,200),expected)
        self.assertEqual(required_level(candidate(item_level=99999),80),80)

    def test_back_slot_has_level_one_gate_unless_black_mist(self):
        row = candidate(item_level=2000)
        row.update(equip_slot=11,base_rarity=4)
        self.assertEqual(required_level(row,200),1)
        row['is_black_mist'] = True
        row['black_mist_finalized'] = True
        self.assertEqual(required_level(row,200),200)
        self.assertFalse(equip_eligibility(row,hero(199))['can_equip'])
        self.assertTrue(equip_eligibility(row,hero(200))['can_equip'])

    def test_divine_gate_uses_verified_base_rarity(self):
        row = candidate(item_level=10,rarity=3)
        row['base_rarity'] = 4
        self.assertEqual(required_level(row,200),200)
        self.assertFalse(equip_eligibility(row,hero(199))['can_equip'])
        # Public rarity can describe protection/display quality; it is not
        # a substitute for the base item rarity used by the game formula.
        row.update(base_rarity=3,rarity=4)
        self.assertEqual(required_level(row,200),1)

    def test_black_mist_always_requires_configured_maximum_level(self):
        row = candidate(item_level=1,is_black_mist=True,black_mist_finalized=True)
        self.assertEqual(required_level(row,80),80)
        self.assertFalse(equip_eligibility(row,hero(79,max_level=80))['can_equip'])
        self.assertTrue(equip_eligibility(row,hero(80,max_level=80))['can_equip'])

    def test_public_level_alias_and_raw_level_preserve_the_same_threshold(self):
        row = candidate(item_level=100)
        row['level'] = row.pop('item_level')
        self.assertEqual(required_level(row,200),10)
        self.assertTrue(equip_eligibility(row,hero(10))['can_equip'])
        row['item_level'] = 200
        self.assertEqual(required_level(row,200),20)

    def test_valid_but_unmet_gate_has_a_specific_reason(self):
        result = equip_eligibility(candidate(item_level=100),hero(9))
        self.assertTrue(result['available'])
        self.assertFalse(result['can_equip'])
        self.assertEqual(result['required_level'],10)
        self.assertEqual(result['character_level'],9)
        self.assertEqual(result['reason'],'角色 Lv.9，装备需要 Lv.10，暂不能穿戴。')

    def test_missing_or_malformed_gate_evidence_never_allows_an_automatic_swap(self):
        good = candidate(item_level=100)
        malformed = []
        for key in ('item_level','is_black_mist','equip_slot','base_rarity'):
            missing = deepcopy(good); missing.pop(key)
            malformed.append((f'missing {key}',missing))
        for key,value in (('item_level',True),('item_level',100.0),
                          ('is_black_mist',None),('is_black_mist',1),
                          ('equip_slot',True),('equip_slot',12),
                          ('base_rarity',None),('base_rarity',True)):
            invalid = deepcopy(good); invalid[key] = value
            malformed.append((f'{key}={value!r}',invalid))
        for description,row in malformed:
            with self.subTest(description=description):
                result = equip_eligibility(row,hero(200))
                self.assertFalse(result['available'])
                self.assertFalse(result['can_equip'])
                self.assertTrue(result['reason'])
                self.assertFalse(equip_candidate_allowed(row,row['power'],
                    settings(equip=True,farm=False)['auto_equip'],hero(200)))
        for character in ({'available':False},hero(level=None),hero(level=True),
                          hero(level=0),hero(max_level=None),hero(max_level=True),
                          hero(max_level=0)):
            with self.subTest(character=character):
                result = equip_eligibility(good,character)
                self.assertFalse(result['available'])
                self.assertFalse(result['can_equip'])

    def test_preview_reports_level_block_instead_of_a_usable_power_upgrade(self):
        model = {'base':{1:100},'caches':{536:{20:100,21:1}},
                 'status':({}, {}, {}),'conditional':({}, {}),'conversions':[]}
        character = hero(9,class_mask=4,primary=1,comparison_model=model)
        row = candidate(item_level=100,modifiers_complete=True,
                        modifiers=[{'stat':1,'type':0,'value':50}],
                        upgrade_level=0,socket_count=0,socketed_gems=[])
        result = preview(row,character,{'items':{}})
        self.assertFalse(result['available'])
        self.assertTrue(result['level_blocked'])
        self.assertEqual(result['reason'],'角色 Lv.9，装备需要 Lv.10，暂不能穿戴。')


class EquipmentLevelPlanningTests(unittest.TestCase):
    def test_unwearable_stronger_item_does_not_hide_a_wearable_upgrade(self):
        too_high = candidate('stronger',item_level=200,delta=10000)
        wearable = candidate('wearable',item_level=100,delta=20,slot=1)
        world = observation([too_high,wearable]); world['character'] = hero(10)
        engine = ManagedMode(settings(equip=True,farm=False)); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'],'equip')
        self.assertEqual(action['item']['item_uid'],'wearable')

    def test_upgrade_is_available_as_soon_as_player_reaches_required_level(self):
        world = observation([candidate(item_level=100)]); world['character'] = hero(9)
        engine = ManagedMode(settings(equip=True,farm=False)); engine.start(world)
        self.assertIsNone(engine.next_action(world))
        self.assertTrue(engine.running)
        world['character']['level'] = 10
        action = engine.next_action(world)
        self.assertEqual(action['kind'],'equip')
        self.assertEqual(action['item']['item_uid'],'upgrade')

    def test_storage_upgrade_is_not_transferred_before_it_can_be_worn(self):
        world = observation([candidate(container='storage',item_level=100)])
        world['character'] = hero(9)
        engine = ManagedMode(settings(equip=True,farm=False)); engine.start(world)
        self.assertIsNone(engine.next_action(world))
        world['character']['level'] = 10
        action = engine.next_action(world)
        self.assertEqual(action['kind'],'transfer')
        self.assertEqual(action['reason_code'],'equip_prepare')

    def test_unwearable_stored_upgrade_does_not_stop_farming(self):
        world = observation([candidate(container='storage',item_level=100)],town=True)
        world['character'] = hero(9)
        engine = ManagedMode(settings(equip=True,farm=True)); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'],'enter_dungeon')
        self.assertEqual(action['dungeon_id'],'a')
        self.assertTrue(engine.running)


class EquipmentLevelFreshExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target in ('deskrawl_assistant.web_service.threading.Thread',
                       'deskrawl_assistant.web_service.record',
                       'deskrawl_assistant.managed_service.record'):
            blocker = patch(target); blocker.start(); self.addCleanup(blocker.stop)
        self.client = FakeManagedClient()
        self.service = AssistantService(self.client,Path(self.tmp.name)/'rules.json')
        self.addCleanup(self.service.close)
        model = {'base':{1:100,4:.2,5:1.5},'caches':{536:{20:100,21:1}},
                 'status':({}, {}, {}),'conditional':({}, {}),'conversions':[]}
        stats = final_stats(model)
        self.character = hero(5,session='offline-session',captured_at='synthetic',
            class_mask=4,primary=1,model=model,comparison_model=model,stats=stats,
            attack=panel_attack(stats,1))
        self.client.character_snapshot = Mock(side_effect=lambda:deepcopy(self.character))

    def plan(self,container='inventory'):
        self.client.items = [service_equipment(container=container,key='CommonBelt1',
            rarity=0,base_rarity=0,item_level=50,equip_slot=6,class_mask=15,
            socket_count=0,socketed_gems=[],modifiers=[{'stat':1,'type':0,'value':50}])]
        self.service._publish(self.client.snapshot())
        self.service._managed_refresh_control()
        config = settings(equip=True,farm=False)
        self.service.save_managed(config); self.service.set_managed_running(True)
        world = self.service._managed_observation()
        action = self.service.managed.next_action(world)
        self.assertIsNotNone(action,str(world['items']))
        return world,action

    def test_execution_rechecks_player_level_before_submitting_equip(self):
        world,action = self.plan()
        self.assertEqual(action['kind'],'equip')
        self.character['level'] = 4
        result = self.service._managed_execute(action,world)
        self.assertFalse(result['verified'])
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_execution_rechecks_player_level_before_storage_preparation(self):
        world,action = self.plan('storage')
        self.assertEqual(action['kind'],'transfer')
        self.assertEqual(action['reason_code'],'equip_prepare')
        self.character['level'] = 4
        result = self.service._managed_execute(action,world)
        self.assertFalse(result['verified'])
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_execution_rechecks_fresh_item_requirement_instead_of_planned_item(self):
        world,action = self.plan()
        self.assertEqual(action['kind'],'equip')
        self.client.items[0]['item_level'] = 100
        result = self.service._managed_execute(action,world)
        self.assertFalse(result['verified'])
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions,[])

    def test_unmodified_valid_gate_reaches_the_synthetic_native_action(self):
        world,action = self.plan()
        result = self.service._managed_execute(action,world)
        self.assertTrue(result['verified'])
        self.assertEqual([item[0] for item in self.client.actions],['equip'])


class GuardedLevelCopy:
    """Byte fixture for the fields re-read immediately before native equip."""
    manager, combat, player, player_class, config, item = 1000,2000,3000,4000,6000,7000

    def __init__(self, level=5, maximum=200, *, changed=False):
        self.guards = []
        self.changed = changed
        self.finished = False
        self.bytes = {
            self.manager+64:struct.pack('<Q',self.combat),
            self.combat+32:struct.pack('<Q',self.player),
            self.player:struct.pack('<Q',self.player_class),
            self.player_class+88:struct.pack('<Q',5000),
            self.manager+32:struct.pack('<Q',self.config),
            self.player+32:struct.pack('<i',level)+bytes(12),
            self.config+256:struct.pack('<i',maximum),
            self.item+36:struct.pack('<i',3),
            self.item+160:struct.pack('<i',6),
            8000+40:struct.pack('<Q',9000),
            8000+48:struct.pack('<Q',9100),
        }

    def read(self,address,size):
        raw = self.bytes[address]
        if len(raw) != size: raise AssertionError('Fixture read size differs from the native field.')
        self.guards.append((address,raw))
        return raw

    def ptr(self,address): return struct.unpack('<Q',self.read(address,8))[0]
    def integer(self,address): return struct.unpack('<i',self.read(address,4))[0]
    def verify(self,obj,name): return obj
    def klass(self,obj,name): return obj
    def dictionary(self,obj,kind): return {}

    def finish(self):
        self.finished = True
        if self.changed: raise SnapshotChangedError('Fixture player level changed during copying.')


class EquipmentNativeLevelBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.reader,self.dispatcher = Mock(),Mock()
        self.reader.string.return_value = ''
        self.dispatcher.invoke.return_value = {}
        self.actions = GameActions(self.reader,dispatcher=self.dispatcher,allow_unverified=True)
        self.read = Mock()
        self.read.guards = [(123,b'original guard')]
        self.read.boolean.return_value = False
        self.read.singleton.return_value = 8000

    def guarded_copy(self,level=5,maximum=200,changed=False):
        self.copies = []
        def make_copy(reader):
            self.assertIs(reader,self.reader)
            copy = GuardedLevelCopy(level,maximum,changed=changed)
            self.copies.append(copy)
            return copy
        factory = patch('deskrawl_assistant.recommendation_reader._Copy',side_effect=make_copy)
        decoder = patch('deskrawl_assistant.recommendation_reader.decode_obscured_int',
                        side_effect=lambda raw:struct.unpack_from('<i',raw)[0])
        factory.start(); self.addCleanup(factory.stop)
        decoder.start(); self.addCleanup(decoder.stop)

    def execute(self):
        selected = candidate(item_level=50)
        payload = {'target_slot':5,'expected_replaced_uid':None}
        with patch.object(self.actions,'capabilities',return_value={'compatible':True}), \
                patch.object(self.actions,'_context',return_value=(self.read,1000,1500,1)), \
                patch.object(self.actions,'_selected_inventory',
                             return_value=(selected,9000,b'',7000,9200)), \
                patch.object(self.actions,'_confirm_effect',return_value=True), \
                patch('deskrawl_assistant.game_actions.record'):
            return self.actions.execute('equip',payload)

    def test_underleveled_native_equip_stops_before_dispatcher_invocation(self):
        self.guarded_copy(level=4)
        with self.assertRaisesRegex(NativeActionError,'角色 Lv.4，装备需要 Lv.5'):
            self.execute()
        self.dispatcher.invoke.assert_not_called()
        self.assertTrue(self.copies[0].finished)

    def test_unknown_fresh_player_level_never_reaches_dispatcher(self):
        self.guarded_copy(level=0)
        with self.assertRaisesRegex(NativeActionError,'角色等级尚未完整读取'):
            self.execute()
        self.dispatcher.invoke.assert_not_called()

    def test_changed_native_level_snapshot_never_reaches_dispatcher(self):
        self.guarded_copy(changed=True)
        with self.assertRaises(SnapshotChangedError): self.execute()
        self.dispatcher.invoke.assert_not_called()

    def test_valid_level_check_returns_gate_and_retains_player_config_guards(self):
        self.guarded_copy()
        result = self.actions._equip_level(self.read,1000,candidate(item_level=50),7000)
        self.assertTrue(result['can_equip'])
        self.assertEqual(result['character_level'],5)
        self.assertEqual(result['required_level'],5)
        self.assertTrue(self.copies[0].finished)
        self.assertIn((3032,struct.pack('<i',5)+bytes(12)),self.read.guards)
        self.assertIn((6256,struct.pack('<i',200)),self.read.guards)
        self.assertIn((7036,struct.pack('<i',3)),self.read.guards)
        self.assertIn((7160,struct.pack('<i',6)),self.read.guards)
        self.assertIn((123,b'original guard'),self.read.guards)

    def test_valid_native_equip_passes_fresh_level_guards_to_dispatcher(self):
        self.guarded_copy()
        result = self.execute()
        self.assertTrue(result['effect_confirmed'])
        self.dispatcher.invoke.assert_called_once()
        calls,guards = self.dispatcher.invoke.call_args.args[:2]
        self.assertIn('EquipmentManager.ddt',[call.method for call in calls])
        self.assertIn((3032,struct.pack('<i',5)+bytes(12)),guards)
        self.assertIn((6256,struct.pack('<i',200)),guards)


if __name__ == '__main__':
    unittest.main()
