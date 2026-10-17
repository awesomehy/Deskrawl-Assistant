"""Hard retention rules: synthetic observations, no live game or native writes."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions, _Read
from deskrawl_assistant.item_protection import (
    mandatory_storage, disposal_protection, divine_equipment, town_rewards_clear,
)
from deskrawl_assistant.managed_mode import ManagedMode, validate_settings
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.native_dispatcher import NativeActionError, compile_mailbox, Invocation
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.web_service import AssistantService
from test_managed_mode import equipment, loot, observation, settings, verified
from test_managed_service import FakeManagedClient, equipment as raw_equipment


def protected_rune(key='LegendaryRune_Dexterity', rarity=3, **extra):
    row = loot('rune', 'rune', key, item_class='RuneData', rarity=rarity, base_rarity=rarity)
    row.update(extra)
    return row


class ProtectionPolicyTests(unittest.TestCase):
    def test_every_bundled_set_and_grand_rune_has_nonconfigurable_protection(self):
        catalog = json.loads((Path(__file__).parents[1]/'data/item-ui.json').read_text(encoding='utf-8'))
        sets = grands = 0
        for key, row in catalog['items'].items():
            if row.get('kind') != 'rune': continue
            is_set = any(g['title'].startswith('符文套装') for g in row.get('effect_groups', []))
            is_grand = '(宏伟)' in row['name']
            if not is_set and not is_grand: continue
            sets += is_set; grands += is_grand
            with self.subTest(key=key):
                # Names still protect these assets if one quality field is stale.
                item = {'internal_name':key, 'item_class':'RuneData', 'base_rarity':2}
                self.assertEqual(mandatory_storage(item), '套装符文' if is_set else '宏伟符文')
                self.assertTrue(disposal_protection(item))
        self.assertGreater(sets, 0); self.assertGreater(grands, 0)

    def test_positive_protection_evidence_wins_and_unknown_runes_cannot_be_sold(self):
        cases = [({'is_equipment':True, 'rarity':3, 'base_rarity':4}, '神圣装备'),
                 ({'is_equipment':True, 'is_divine':True, 'rarity':0}, '神圣装备'),
                 ({'is_black_mist':True, 'rarity':0}, '黑雾装备'),
                 ({'item_class':'RuneData', 'rarity':2, 'base_rarity':5}, '套装符文'),
                 ({'kind':'rune', 'rarity':3}, '宏伟符文'),
                 ({'item_class':'RuneData', 'rarity':4}, '宏伟符文')]
        for row, reason in cases:
            with self.subTest(row=row):
                self.assertEqual(mandatory_storage(row), reason)
                self.assertTrue(disposal_protection(row))
        for rarity in (None, True, '2', -1, 6):
            self.assertTrue(disposal_protection({'item_class':'RuneData', 'rarity':rarity}))
        for rarity in (0, 1, 2):
            self.assertFalse(disposal_protection({'item_class':'RuneData', 'rarity':rarity}))

    def test_empty_town_requires_complete_ground_carriage_and_stored_protected_bag(self):
        clean = observation(town=True)
        self.assertTrue(town_rewards_clear(clean))
        variants = []
        for source in ('ground', 'carriage'):
            for value in ({'available':True, 'items':[]}, {'complete':False, 'items':[]},
                          {'complete':True, 'items':[{'kind':'material'}]}):
                world = deepcopy(clean); world[source] = value; variants.append(world)
        for row in (equipment('divine', rarity=4), equipment('mist', is_black_mist=True), protected_rune()):
            variants.append(observation([row], town=True))
        variants.append(observation(town=False))
        for world in variants: self.assertFalse(town_rewards_clear(world))
        self.assertTrue(town_rewards_clear(observation([protected_rune(container='storage')], town=True)))


class IronRulePlannerTests(unittest.TestCase):
    def test_divine_never_equipped_or_taken_from_storage_even_for_named_lock(self):
        for container in ('inventory', 'storage'):
            for named in (False, True):
                for evidence in ({'rarity':4}, {'rarity':3, 'base_rarity':4}, {'is_divine':True}):
                    with self.subTest(container=container, named=named, evidence=evidence):
                        cfg = settings(equip=True, farm=False)
                        if named: cfg['auto_equip']['slot_locks'] = {'8':'LegendaryNeck1'}
                        row = equipment('divine', container=container, **evidence,
                            power={'available':True, 'target':8, 'delta':100000})
                        engine = ManagedMode(cfg); world = observation([row]); engine.start(world)
                        self.assertTrue(divine_equipment(row))
                        self.assertIsNone(engine.next_action(world))

    def test_divine_highest_score_does_not_block_a_valid_nondivine_upgrade(self):
        divine = equipment('divine', rarity=4, power={'available':True, 'target':8, 'delta':100000})
        ordinary = equipment('ordinary', slot=1, rarity=2, power={'available':True, 'target':8, 'delta':20})
        engine = ManagedMode(settings(equip=True, farm=False)); world = observation([divine, ordinary]); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'equip'); self.assertEqual(action['item']['item_uid'], 'ordinary')

    def test_protected_runes_override_sell_setting_and_locked_flag(self):
        for key, rarity in (('LegendaryRune_Dexterity', 3), ('HunterSetRune1_1', 5), ('NewSetRune', 5)):
            for locked in (False, True):
                with self.subTest(key=key, locked=locked):
                    row = protected_rune(key, rarity, locked=locked)
                    cfg = settings(rune_action='sell', rune_pages=[3])
                    engine = ManagedMode(cfg); world = observation([row], town=True); engine.start(world)
                    action = engine.next_action(world)
                    self.assertEqual(action['kind'], 'transfer'); self.assertEqual(action['target'], 'storage')
                    self.assertEqual(action['target_indices'], list(range(100, 150)))

    def test_full_protected_rune_section_pauses_without_sale_fallback_or_entry(self):
        for key, rarity in (('LegendaryRune_Dexterity', 3), ('HunterSetRune1_1', 5)):
            row = protected_rune(key, rarity)
            occupied = [loot('gem-'+str(n), 'gem', 'GemRuby1', n, 'storage') for n in range(50)]
            engine = ManagedMode(settings(rune_action='sell', rune_pages=[1]))
            world = observation([row, *occupied], town=True); engine.start(world)
            self.assertIsNone(engine.next_action(world)); self.assertEqual(engine.phase, 'paused')
            self.assertIn('容量不足', engine.status); self.assertIsNone(engine.pending)

    def test_unreadable_mandatory_item_pauses_instead_of_leaving_it_in_bag_and_entering(self):
        for row in (protected_rune(movable=False), protected_rune(issues=['读取未确认']),
                    equipment('divine', rarity=4, locked=None)):
            engine = ManagedMode(settings(rune_action='sell')); world = observation([row], town=True)
            engine.start(world); self.assertIsNone(engine.next_action(world)); self.assertEqual(engine.phase, 'paused')

    def test_equipment_protection_locks_then_stores_even_with_all_background_switches_off(self):
        for evidence in ({'rarity':4}, {'is_black_mist':True}):
            row = equipment('keep', **evidence)
            cfg = settings(equip=True, equipment_action='salvage')
            cfg['background'] = {'rule_monitoring':False, 'inventory_automation':False}
            engine = ManagedMode(cfg); world = observation([row], town=True); engine.start(world)
            action = engine.next_action(world); self.assertEqual(action['kind'], 'lock'); verified(engine, action)
            row['locked'] = True
            action = engine.next_action(observation([row], town=True))
            self.assertEqual(action['kind'], 'transfer'); self.assertEqual(action['target'], 'storage')

    def test_ground_material_gold_and_chests_are_collected_before_travel(self):
        for source in ('ground', 'carriage'):
            for kind in ('equipment', 'rune', 'gem', 'chest', 'material', 'gold'):
                row = loot('drop', kind, 'TreasureChest1' if kind=='chest' else 'Drop', container=source)
                world = observation(town=True); world[source]['items'] = [row]
                engine = ManagedMode(settings()); engine.start(world); engine.phase = 'travel'
                action = engine.next_action(world)
                self.assertEqual(action['kind'], 'collect_'+source)

    def test_incomplete_or_missing_complete_flag_cannot_be_interpreted_as_empty(self):
        for source in ('ground', 'carriage'):
            for state in ({'available':True, 'items':[]}, {'complete':False, 'items':[]}):
                world = observation(town=True); world[source] = state
                engine = ManagedMode(settings()); engine.start(world)
                self.assertIsNone(engine.next_action(world)); self.assertTrue(engine.running)
                self.assertIn('等待完整读取', engine.status)

    def test_rune_pages_are_required_even_when_ordinary_runes_are_sold(self):
        cfg = settings(rune_action='sell', rune_pages=[])
        with self.assertRaises(ValidationError): validate_settings(cfg)


class IronRuleServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        for target in ('deskrawl_assistant.web_service.threading.Thread', 'deskrawl_assistant.web_service.record'):
            stub = patch(target); stub.start(); self.addCleanup(stub.stop)
        self.client = FakeManagedClient([])
        self.service = AssistantService(self.client, Path(tmp.name)/'rules.json')
        self.addCleanup(self.service.close)
        self.service._publish(self.client.snapshot()); self.service._managed_refresh_control()
        cfg = settings(rune_action='sell', equipment_action='salvage')
        cfg['farming']['dungeon_id'] = 'offline-map'; cfg['farming']['progression'] = []
        self.service.save_managed(cfg); self.service.set_managed_running(True)

    def raw_rune(self, key, rarity):
        return raw_equipment(key=key, name_key=None, is_equipment=False,
            item_class='RuneData', base_rarity=rarity, rarity=rarity)

    def test_fresh_rune_metadata_is_retained_and_ordinary_sell_becomes_mandatory_storage(self):
        for key, rarity in (('HunterSetRune1_1', 5), ('LegendaryRune_Dexterity', 3)):
            self.client.items = [self.raw_rune(key, rarity)]
            world = self.service._managed_observation(); row = world['items'][0]
            self.assertEqual(row['item_class'], 'RuneData'); self.assertEqual(row['base_rarity'], rarity)
            self.assertEqual(row['rarity'], rarity); self.assertTrue(mandatory_storage(row))
            self.service.managed.pending = None; self.service.managed.await_effect = None
            action = self.service.managed.next_action(world)
            self.assertEqual(action['kind'], 'transfer'); self.assertEqual(action['target'], 'storage')

    def test_stale_entry_or_difficulty_plan_rechecks_new_rewards_before_native_call(self):
        clean = self.service._managed_observation()
        for kind in ('enter_dungeon', 'switch_difficulty'):
            for source in ('ground', 'carriage', 'inventory'):
                self.client.items = []; self.client.ground = []; self.client.carriage = []
                if source=='inventory': self.client.items = [self.raw_rune('HunterSetRune1_1', 5)]
                else:
                    getattr(self.client, source).append(loot('new', 'material', 'NewReward', container=source))
                action = {'kind':kind, 'dungeon_id':'offline-map', 'difficulty':'Nightmare'}
                result = self.service._managed_execute(action, clean)
                self.assertFalse(result['verified']); self.assertTrue(result['retryable'])
                self.assertEqual(self.client.actions, [])

    def test_missing_complete_flag_stays_incomplete_through_service_adapter(self):
        state = self.client.control_snapshot(); state['ground'] = {'available':True, 'items':[]}
        with patch.object(self.client, 'control_snapshot', return_value=state):
            world = self.service._managed_observation()
            self.assertFalse(world['ground']['complete'])
            result = self.service._managed_execute({'kind':'enter_dungeon', 'dungeon_id':'offline-map'}, world)
        self.assertTrue(result['retryable']); self.assertEqual(self.client.actions, [])

    def test_forged_disposal_plan_cannot_sell_or_salvage_protected_inventory(self):
        cases = [self.raw_rune('HunterSetRune1_1', 5), self.raw_rune('LegendaryRune_Dexterity', 3),
                 raw_equipment(rarity=4, locked=True), raw_equipment(is_black_mist=True, locked=True)]
        for raw in cases:
            self.client.items = [raw]; world = self.service._managed_observation(); row = world['items'][0]
            for kind in ('sell', 'salvage'):
                with self.subTest(key=raw['internal_name'], kind=kind), self.assertRaises(ValidationError):
                    self.service._managed_execute({'kind':kind, 'item':row}, world)
                self.assertEqual(self.client.actions, [])

    def test_stale_ordinary_rune_sale_is_rejected_when_fresh_rarity_is_protected(self):
        self.client.items = [self.raw_rune('HunterRune1', 2)]
        old = self.service._managed_observation(); row = old['items'][0]
        self.assertFalse(disposal_protection(row))
        self.client.items[0].update(rarity=5, base_rarity=5)
        with self.assertRaisesRegex(ValidationError, '必须收纳'):
            self.service._managed_execute({'kind':'sell', 'item':row}, old)
        self.assertEqual(self.client.actions, [])

    def test_legacy_sale_settings_keep_all_preferences_until_required_rune_page_is_selected(self):
        self.service.stop_managed()
        original = deepcopy(self.service.managed.settings)
        original['cleanup'].update(rune_pages=[],rune_action='sell',gem_action='sell')
        original['auto_equip']['disabled_slots'] = [6]
        original['farming']['free_threshold'] = 11
        text = json.dumps(original,ensure_ascii=False)
        self.service.managed_config.write_text(text,encoding='utf-8')
        self.service._init_managed()
        self.service._managed_refresh_control()
        migrated = deepcopy(original)
        migrated['cleanup'].update(rune_action='store',gem_action='store')
        self.assertEqual(self.service.managed.settings,migrated)
        self.assertEqual(self.service.managed_config.read_text(encoding='utf-8'),text)
        self.assertFalse(self.service.managed_page()['can_start'])
        with self.assertRaises(ValidationError): self.service.set_managed_running(True)
        original['cleanup']['rune_pages'] = [5]
        self.service.save_managed(original)
        self.assertEqual(self.service.managed_config_error,'')
        self.assertEqual(self.service.managed.settings['farming']['free_threshold'],11)
        self.assertEqual(self.client.actions,[])


class IronRuleNativeBoundaryTests(unittest.TestCase):
    def action(self):
        reader, dispatcher, read = Mock(), Mock(), Mock()
        read.boolean.return_value = False; read.singleton.return_value = 100
        read.guards = []; dispatcher.invoke.return_value = {}
        action = GameActions(reader, dispatcher=dispatcher, allow_unverified=True)
        self.enterContext(patch.object(action, 'capabilities', return_value={'compatible':True}))
        self.enterContext(patch.object(action, '_context', return_value=(read, 1, 2, 1)))
        return action, reader, dispatcher, read

    def test_final_native_disposition_rejects_all_protected_items_even_with_authorized_payload(self):
        action, reader, dispatcher, read = self.action()
        cases = [protected_rune(), protected_rune('HunterSetRune1_1', 5),
                 equipment('divine', rarity=4), equipment('mist', is_black_mist=True)]
        for row in cases:
            with patch.object(action, '_selected_inventory', return_value=(row, 12, b'', 13, 14)):
                for kind in ('sell', 'salvage'):
                    with self.subTest(kind=kind, row=row), self.assertRaises(NativeActionError):
                        action.execute(kind, {'disposition_authorized':True, 'rule_pending':False, 'rule_matched':False})
                    dispatcher.invoke.assert_not_called()

    def test_final_native_equip_rejects_divine_even_when_browser_row_claims_normal(self):
        action, reader, dispatcher, read = self.action()
        for evidence in ({'rarity':4}, {'rarity':3, 'base_rarity':4}, {'is_divine':True}):
            row = equipment('divine', **evidence)
            with patch.object(action, '_selected_inventory', return_value=(row, 12, b'', 13, 14)):
                with self.assertRaisesRegex(NativeActionError, '神圣装备禁止自动穿戴'):
                    action.execute('equip', {'item':equipment('divine', rarity=0), 'target_slot':8})
            dispatcher.invoke.assert_not_called()

    def test_final_native_travel_rejects_ground_carriage_or_pending_offload(self):
        action, reader, dispatcher, read = self.action()
        for ground, carriage in (([{'kind':'gold'}], {'complete':True, 'items':[]}),
                                 ([], {'complete':True, 'items':[protected_rune()]}),
                                 ([], {'complete':False, 'items':[]}), ([], {'items':[]})):
            with patch.object(action, '_ground', return_value=ground), patch.object(action, '_carriage', return_value=carriage):
                for kind in ('enter_map', 'switch_difficulty'):
                    with self.assertRaises(NativeActionError): action.execute(kind, {})
                    dispatcher.invoke.assert_not_called()
        with patch.object(action, '_ground', side_effect=MemoryReadError('地面未确认')):
            with self.assertRaises(MemoryReadError): action.execute('enter_map', {})
        dispatcher.invoke.assert_not_called()

    def inventory(self, action, reader, read, rows, complete=True):
        reader.classes = {'InventorySlot':99, 'GeneratedItemData':199}
        reader.snapshot_stamps = [(17, b'original')]
        addresses = [2000+i*100 for i in range(max(2, len(rows)+1))]
        read.ptr.return_value = 1000; read.pointers.return_value = addresses
        memory = {a:struct.pack('<Q',99)+bytes(40) for a in addresses}
        bases, strings, registry = {}, {}, {}
        for i, row in enumerate(rows):
            item, uid, generated = 10000+i*256, 20000+i*256, 30000+i*256
            head = bytearray(memory[addresses[i]])
            struct.pack_into('<Q',head,16,item); struct.pack_into('<Q',head,32,uid)
            memory[addresses[i]] = bytes(head); bases[item] = row; strings[uid] = 'uid-'+str(i)
            if row.get('is_equipment'):
                registry[strings[uid]] = generated
                fresh = bytearray(128); struct.pack_into('<Q',fresh,0,199)
                struct.pack_into('<i',fresh,20,row.get('rarity',3)); fresh[105] = bool(row.get('is_black_mist'))
                memory[generated] = bytes(fresh)
        reader.memory.read.side_effect = lambda a, size:memory[a][:size]
        reader.base_item.side_effect = lambda a:deepcopy(bases[a])
        reader.string.side_effect = lambda a:strings[a]
        def registry_read():
            if not complete: raise MemoryReadError('背包注册表读取不完整')
            reader.snapshot_stamps.extend([(4000,b'rootptr1'),(5000,b'rootptr2'),(6000,b'header'*4),
                                           (7000,bytes(24000))])
            return registry, {}
        reader.registry.side_effect = registry_read

    def test_final_town_inventory_guard_prevents_unstored_protected_bag_or_incomplete_read(self):
        action, reader, dispatcher, read = self.action()
        for rows, complete in (([protected_rune()], True), ([equipment('divine', rarity=4)], True), ([], False)):
            self.inventory(action, reader, read, rows, complete)
            with self.assertRaises((NativeActionError, MemoryReadError)): action._require_stored_inventory(read)
            self.assertEqual(reader.snapshot_stamps, [(17, b'original')])
            dispatcher.invoke.assert_not_called()

    def test_empty_slot_headers_and_generated_guards_are_forwarded_to_main_thread(self):
        action, reader, dispatcher, read = self.action()
        self.inventory(action, reader, read, [])
        with patch.object(action, '_ground', return_value=[]), \
                patch.object(action, '_carriage', return_value={'complete':True, 'items':[]}), \
                patch.object(action, 'control_snapshot', return_value={'available':True, 'difficulty':'Normal',
                    'maps':[{'map_id':'a', 'can_enter':True}]}), \
                patch.object(action, '_map_objects', return_value={'a':5000}):
            result = action.execute('enter_map', {'map_id':'a', 'difficulty':'Normal'})
        reader.memory.read.assert_any_call(2000, 48); reader.memory.read.assert_any_call(2100, 48)
        self.assertIn((2016, bytes(8)), dispatcher.invoke.call_args.args[1])
        self.assertIn((2116, bytes(8)), dispatcher.invoke.call_args.args[1])
        self.assertIn((6000, b'header'*4), dispatcher.invoke.call_args.args[1])
        self.assertNotIn((7000, bytes(24000)), dispatcher.invoke.call_args.args[1])
        self.assertEqual(reader.snapshot_stamps, [(17, b'original')])
        self.assertEqual(dispatcher.invoke.call_args.args[0][0].method, 'TownPortalSystem.fri')
        self.assertTrue(result['transition_requested'])

    def test_town_guard_for_realistic_inventory_fits_bounded_mailbox(self):
        action, reader, dispatcher, read = self.action()
        self.inventory(action, reader, read, [equipment(str(n)) for n in range(39)])
        read.guards = [(1000+24, struct.pack('<Q',40)), (1032,bytes(40*8))]
        action._require_stored_inventory(read)
        # Live validation measured 276 records with 28 equipment and 12 other
        # items. Reserve more scene/reward/difficulty overhead for a gear-filled
        # bag; the former 25-record allowance missed this real boundary.
        guards = read.guards+[(90000+n*8,bytes(8)) for n in range(100)]
        encoded = compile_mailbox(0x100000,0x180000000,action.profile,0x300000,0x400000,100,
                                  [Invocation('TownPortalSystem.frj',1)],guards)
        self.assertGreater(struct.unpack_from('<I', encoded, 56)[0], 256)
        self.assertLessEqual(len(encoded), 16384)

    def test_empty_list_guards_count_and_version_without_unused_backing_capacity(self):
        reader = Mock(); reader.memory.u64.return_value = 99
        reader.class_name.return_value = 'List`1'
        reader.fields.return_value = [{'name':k, 'offset':v} for k,v in
                                     {'_items':16, '_size':24, '_version':28}.items()]
        reader.memory.read.return_value = struct.pack('<Qii',2000,0,5)
        reader.array.return_value = ([bytes(8)]*512, bytes(512*8))
        read = _Read(reader, {})
        self.assertEqual(read.list(1000,'LootDrop'), [])
        self.assertIn((1016, struct.pack('<Qii',2000,0,5)), read.guards)
        self.assertNotIn((2032,bytes(512*8)), read.guards)

    def test_empty_slot_becoming_occupied_invalidates_the_guard_before_dispatch(self):
        reader = Mock(); reader.memory.read.return_value = bytes(48)
        read = _Read(reader, {})
        read.read(2000, 48)
        reader.memory.read.return_value = bytes(16)+struct.pack('<Q',999)+bytes(24)
        with self.assertRaises(SnapshotChangedError): read.finish()


if __name__ == '__main__': unittest.main()
