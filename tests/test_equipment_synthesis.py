"""Equipment cube consent, material identity, staging and receipt tests.

All observations are copied fixtures; these tests never attach to the game.
"""
from copy import deepcopy
import json
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.equipment_synthesis import DEFAULT_EQUIPMENT, RECIPES, plan
from deskrawl_assistant.managed_mode import ManagedMode, ManagedMetrics, validate_settings, identity
from deskrawl_assistant.synthesis import validate_settings as validate_synthesis
from deskrawl_assistant.synthesis_runtime import equipment_inputs, SynthesisReceiptPending
from deskrawl_assistant.native_dispatcher import Argument, NativeActionError
from deskrawl_assistant.native_memory import SnapshotChangedError
from tests.test_managed_mode import equipment, observation, settings
from tests import test_synthesis_runtime as runtime_fixtures
from tests import test_managed_service as service_fixtures
from tests import test_managed_synthesis_service as synthesis_service_fixtures


def gear(i, quality='BlackMist', **extra):
    key = 'DivineRing1' if quality == 'Divine' else 'AncientDivineRing1' if quality == 'AncientDivine' else 'LegendaryNeck1'
    fields = dict(slot=i, instance_id=f'instance-{i}',
        identity_verified=True, rules_known=True, slot_locked=False,
        internal_name=key, name_key=key, item_level=845, upgrade_level=0,
        rarity=4 if quality in ('Divine','AncientDivine') else 3,
        is_black_mist=quality=='BlackMist', is_ancient=quality=='AncientLegendary')
    fields.update(extra)
    return equipment(f'equipment-{i}', **fields)


def recipe(option='black_mist_to_divine', **extra):
    source, target, amount = RECIPES[option]
    value = dict(recipe_id=f'cube:{source}:{target}', option=option,
        input_quality=source, output_quality=target, input_count=amount, output_count=1,
        chance=.5, gold_cost=1000, min_item_level=800 if source=='AncientLegendary' else 0,
        available=True, output_ancient_item_level=845 if source=='AncientLegendary' else 0,
        refund_key='', refund_from_level=11)
    value.update(extra)
    return value


def retired_recipe():
    value = recipe()
    value.update(recipe_id='cube:BlackMist:BlackMist', option='black_mist_reforge',
                 output_quality='BlackMist', input_count=3, chance=0)
    return value


def config(option='black_mist_to_divine', **extra):
    value = settings(farm=False)
    value['synthesis']['equipment'].update(enabled=True, **{option:True}, **extra)
    return value


def world(rows, recipes=None, **extra):
    value = observation(rows, town=True, **extra)
    value['character_id'] = 'role-a'
    value['synthesis'] = {'equipment': dict(available=True, gold=10000000,
        recipes=recipes or [recipe()], online=True, sequence=5, rules_version='rules-a')}
    return value


class EquipmentPolicyTests(unittest.TestCase):
    def test_retired_reforge_configuration_is_discarded_without_changing_other_choices(self):
        for selected in (False, True):
            with self.subTest(selected=selected):
                old = config(include_matched=False, black_mist_reforge=selected)
                original = deepcopy(old)
                migrated = validate_settings(old)
                expected = deepcopy(original['synthesis']['equipment'])
                expected.pop('black_mist_reforge')
                self.assertEqual(migrated['synthesis']['equipment'], expected)
                self.assertEqual(old, original)
                self.assertEqual(validate_settings(json.loads(json.dumps(migrated))), migrated)

    def test_retired_reforge_never_withdraws_unlocks_or_consumes_materials(self):
        cfg = config('black_mist_reforge')
        for rows in ([gear(i) for i in range(3)],
                     [gear(i, locked=True, container='storage') for i in range(3)]):
            with self.subTest(container=rows[0]['container']):
                self.assertIsNone(plan(cfg, world(rows, [retired_recipe()])))
        migrated = validate_settings(cfg)
        self.assertTrue(migrated['synthesis']['equipment']['enabled'])
        self.assertFalse(any(migrated['synthesis']['equipment'][key] for key in RECIPES))

    def test_old_configuration_stays_off_but_matched_preference_is_on(self):
        old = settings(); old['synthesis'].pop('equipment')
        migrated = validate_settings(old)
        self.assertEqual(migrated['synthesis']['equipment'], DEFAULT_EQUIPMENT)
        self.assertTrue(migrated['synthesis']['equipment']['include_matched'])
        self.assertNotIn('equipment', old['synthesis'])
        legacy = deepcopy(migrated['synthesis']); legacy['equipment'].pop('include_matched')
        self.assertTrue(validate_synthesis(legacy)['equipment']['include_matched'])

    def test_matched_preference_survives_save_reload_and_controls_consumption(self):
        rows = [gear(i, matches=True, locked=True) for i in range(9)]
        for enabled in (True, False):
            with self.subTest(include_matched=enabled):
                cfg = validate_settings(json.loads(json.dumps(config(include_matched=enabled))))
                self.assertEqual(cfg['synthesis']['equipment']['include_matched'], enabled)
                action = plan(cfg, world(rows))
                if enabled:
                    self.assertEqual(action['kind'], 'unlock')
                    self.assertTrue(action['include_matched'])
                    self.assertEqual(len(action['input_uids']), 9)
                else:
                    self.assertIsNone(action)

    def test_opt_out_preserves_unknown_black_mist_rule_result(self):
        rows = [gear(i, review=True, matches=False) for i in range(9)]
        self.assertIsNotNone(plan(config(), world(rows)))
        self.assertIsNone(plan(config(include_matched=False), world(rows)))
        rows = [gear(i, matches=False) for i in range(9)]
        self.assertEqual(plan(config(include_matched=False), world(rows))['kind'], 'synthesize_equipment')

    def test_equipped_forced_keep_and_slot_locks_are_never_materials(self):
        for protection in ('equipped', 'worn_uid', 'excluded', 'slot_lock'):
            with self.subTest(protection=protection):
                rows = [gear(i, matches=True) for i in range(9)]
                cfg, state = config(), world(rows)
                if protection == 'equipped': state['items'][0]['is_equipped'] = True
                elif protection == 'worn_uid': state['character']['equipment'] = [rows[0]]
                elif protection == 'excluded': cfg['auction']['excluded_keys'] = [rows[0]['internal_name']]
                else: cfg['auto_equip']['slot_locks'] = {'8': rows[0]['internal_name']}
                self.assertIsNone(plan(cfg, state))

    def test_unknown_identity_slot_lock_and_duplicate_uid_never_get_unlocked(self):
        for extra in ({'identity_verified':False}, {'instance_id':''}, {'locked':None},
                      {'slot_locked':True}, {'issues':['stale']}, {'rules_known':False},
                      {'upgrade_level':None}, {'movable':False}, {'item_uid':'equipment-1'}):
            with self.subTest(extra=extra):
                rows = [gear(i) for i in range(9)]; rows[0].update(extra)
                self.assertIsNone(plan(config(), world(rows)))

    def test_recipe_disabled_locked_or_insufficient_gold_is_skipped(self):
        cfg = config(); rows = [gear(i) for i in range(9)]
        cfg['synthesis']['equipment']['enabled'] = False
        self.assertIsNone(plan(cfg, world(rows)))
        cfg = config(); cfg['synthesis']['equipment']['black_mist_to_divine'] = False
        self.assertIsNone(plan(cfg, world(rows)))
        state = world(rows); state['synthesis']['equipment']['gold'] = 999
        self.assertIsNone(plan(config(), state))
        self.assertIsNone(plan(config(), world(rows, [recipe(available=False)])))

    def test_any_unlocked_storage_page_can_supply_and_full_bag_cannot_loop(self):
        rows = [gear(i) for i in range(8)] + [gear(149, container='storage', locked=True)]
        action = plan(config(), world(rows))
        self.assertEqual((action['kind'], action['target'], action['item']['slot_index']), ('transfer','inventory',149))
        self.assertIsNone(plan(config(), world(rows, capacity=8)))
        self.assertIsNone(plan(config(), world(rows, allowed=range(50))))
        rows[-1].update(container='inventory', slot_index=8)
        self.assertEqual(plan(config(), world(rows))['kind'], 'unlock')
        rows[-1]['locked'] = False
        self.assertEqual(plan(config(), world(rows))['kind'], 'synthesize_equipment')

    def test_divine_needs_same_name_and_a_real_ancient_counterpart(self):
        cfg = config('divine_to_ancient'); recipes = [recipe('divine_to_ancient')]
        rows = [gear(i, 'Divine') for i in range(3)]
        self.assertIsNotNone(plan(cfg, world(rows, recipes)))
        rows[0]['internal_name'] = rows[0]['name_key'] = 'DivineRing2'
        self.assertIsNone(plan(cfg, world(rows, recipes)))
        for row in rows: row.update(internal_name='DivineNotExisting',name_key='DivineNotExisting')
        self.assertIsNone(plan(cfg, world(rows, recipes)))

    def test_live_level_limit_and_remaining_upgrade_are_respected(self):
        rows = [gear(i, 'AncientLegendary') for i in range(9)]
        rows[0]['item_level'] = 799
        self.assertIsNone(plan(config('ancient_to_black_mist'), world(rows,[recipe('ancient_to_black_mist')])))
        cfg = config(); cfg['synthesis']['equipment']['black_mist_reforge'] = True
        state = world([gear(i) for i in range(9)], [retired_recipe(),recipe()])
        self.assertEqual(plan(cfg,state)['recipe']['option'], 'black_mist_to_divine')

    def test_town_only_and_pause_finish_never_start_new_unlock(self):
        rows = [gear(i, locked=True, matches=True) for i in range(9)]
        state = world(rows); state['scene']['in_town'] = False
        self.assertIsNone(plan(config(), state))
        state = world(rows); engine = ManagedMode(config()); engine.start(state)
        self.assertEqual(engine.next_action(state)['kind'], 'unlock')
        engine = ManagedMode(config()); engine.start(state); engine.request_finish()
        action = engine.next_action(state)
        self.assertTrue(action is None or action['kind'] not in ('unlock','synthesize_equipment'))

    def test_opt_out_uses_normal_lock_and_storage_protection(self):
        state = world([gear(i, matches=True) for i in range(9)])
        engine = ManagedMode(config(include_matched=False)); engine.start(state)
        action = engine.next_action(state)
        self.assertEqual(action['kind'],'lock')
        self.assertNotIn('synthesis_group',action)

    def test_selected_materials_are_not_disposed_by_previous_warehouse_eviction(self):
        state = world([gear(i, 'AncientLegendary') for i in range(9)], [recipe('ancient_to_black_mist')])
        engine = ManagedMode(config('ancient_to_black_mist')); engine.start(state)
        engine.force_dispose.add(identity(state['items'][0]))
        self.assertEqual(engine.next_action(state)['kind'], 'synthesize_equipment')

    def test_scheduler_withdraws_unlocks_crafts_then_protects_output_without_relocking_loop(self):
        rows = [gear(i, matches=True, locked=True) for i in range(8)] + [
            gear(149, container='storage', matches=True, locked=True)]
        state = world(rows)
        engine = ManagedMode(config()); engine.start(state)
        kinds = []
        for _ in range(30):
            state = world(rows)
            action = engine.next_action(state)
            if action is None: continue
            kind = action['kind']; kinds.append(kind)
            if kind == 'synthesize_equipment':
                inputs = deepcopy(rows)
                output = gear(20, 'Divine', matches=True)
                rows = [output]
                result = {'verified':True, 'effect_confirmed':True, 'receipt_id':'one-craft',
                    'inputs':[dict(r,quantity=1) for r in inputs], 'outputs':[dict(output,quantity=1)]}
            else:
                row = next(r for r in rows if r['item_uid'] == action['item']['item_uid'])
                result = {'verified':True}
                if kind == 'transfer':
                    row.update(container=action['target'], slot_index=8 if action['target']=='inventory' else 0)
                elif kind in ('lock','unlock'): row['locked'] = kind == 'lock'
                else: self.fail(f'Unexpected material operation: {kind}')
            engine.complete(action['action_id'], result=result)
            if len(rows)==1 and rows[0]['container']=='storage': break
        self.assertEqual(kinds, ['transfer'] + ['unlock'] * 9 + ['synthesize_equipment','lock','transfer'])
        self.assertEqual(rows[0]['locked'],True)
        engine.metrics.observe(world(rows))
        self.assertIn(rows[0]['item_uid'], engine.metrics.crafted_uids)
        self.assertFalse(any(row['count'] for row in engine.metrics.state()['equipment'].values()))


class EquipmentNativeTests(unittest.TestCase):
    def test_retired_reforge_recipe_is_rejected_before_material_selection(self):
        rows = [gear(i) for i in range(3)]
        with self.assertRaisesRegex(NativeActionError, '配方已移除'):
            equipment_inputs(rows, [row['item_uid'] for row in rows], retired_recipe())

    def test_validator_rechecks_exact_material_identities_after_unlock(self):
        rows = [gear(i) for i in range(9)]; chosen = [r['item_uid'] for r in rows]
        self.assertEqual(len(equipment_inputs(rows, chosen, recipe())),9)
        for extra in ({'locked':True}, {'identity_verified':False}, {'slot_locked':True},
                      {'container':'storage'}, {'is_equipped':True}, {'count':2}, {'is_black_mist':False}):
            changed = deepcopy(rows); changed[0].update(extra)
            with self.subTest(extra=extra), self.assertRaises(NativeActionError):
                equipment_inputs(changed,chosen,recipe())
        with self.assertRaises(SnapshotChangedError): equipment_inputs(rows+[rows[0]],chosen,recipe())

    def receipt_runtime(self, before, after, gold=9999000, sequence=6):
        runtime, _ = runtime_fixtures.SnapshotAndReceiptTests().runtime()
        runtime._context = Mock(return_value=(Mock(),1,2)); runtime._copy = Mock(return_value=Mock())
        runtime._bag = Mock(return_value=(deepcopy(after),{},100,60))
        runtime._equipment = Mock(return_value={'gold':gold,'sequence':sequence})
        return runtime

    def test_probability_failure_requires_new_uid_exact_fee_and_online_sequence(self):
        before = [gear(i) for i in range(9)]; output = gear(20)
        runtime = self.receipt_runtime(before, [output])
        state = dict(gold=10000000,sequence=5,online=True)
        receipt = runtime._receipt('synthesize_equipment',before,before,recipe(),1,state,{'sequence':17})
        self.assertEqual(receipt['outputs'][0]['item_uid'],output['item_uid'])
        for after, gold, sequence in ((before,9999000,6),([output],9999000,5),([output],10000000,6)):
            runtime = self.receipt_runtime(before,after,gold,sequence)
            self.assertIsNone(runtime._receipt('synthesize_equipment',before,before,recipe(),1,state,{'sequence':17}))

    def test_divine_failure_or_success_cannot_return_another_base_equipment(self):
        before = [gear(i,'Divine') for i in range(3)]; rec=recipe('divine_to_ancient')
        for quality,key,ok in [('AncientDivine','AncientDivineRing1',True),('Divine','DivineRing1',True),
                               ('AncientDivine','AncientDivineRing2',False),('Divine','DivineRing2',False)]:
            output=gear(20,quality); output.update(internal_name=key,name_key=key)
            runtime=self.receipt_runtime(before,[output])
            result=runtime._receipt('synthesize_equipment',before,before,rec,1,dict(gold=10000000,sequence=5,online=True),{'sequence':17})
            self.assertEqual(result is not None,ok)

    def test_refund_across_bag_and_storage_same_slot_is_not_double_counted_as_loot(self):
        before=[gear(i,'Divine',upgrade_level=12) for i in range(3)]
        bag=dict(container='inventory',slot_index=10,count=5,internal_name='UpgradeCommodity1')
        stored=dict(container='storage',slot_index=10,count=100,internal_name='UpgradeCommodity1')
        output=gear(20,'AncientDivine')
        runtime=self.receipt_runtime(before+[bag,stored],[output,dict(bag,count=11),stored])
        result=runtime._receipt('synthesize_equipment',before+[bag,stored],before,
            recipe('divine_to_ancient',refund_key='UpgradeCommodity1'),1,
            dict(gold=10000000,sequence=5,online=True),{'sequence':17})
        self.assertIsNotNone(result)
        self.assertEqual(sum(r['quantity'] for r in result['outputs'] if r.get('internal_name')=='UpgradeCommodity1'),6)
        metrics=ManagedMetrics(); metrics.observe(world(before+[bag,stored]))
        metrics.synthesized(result['receipt_id'],result['inputs'],result['outputs'])
        metrics.observe(world([output,dict(bag,count=11),stored]))
        self.assertFalse(any(row['count'] for row in metrics.state()['equipment'].values()))
        self.assertFalse(any(row['count'] for row in metrics.state()['others']))

    def test_online_and_offline_equipment_recipes_are_read_without_mutation(self):
        for online in (False,True):
            for option,(source,target,amount) in RECIPES.items():
                with self.subTest(online=online,option=option):
                    runtime,actions=runtime_fixtures.SnapshotAndReceiptTests().runtime()
                    actions.reader.module={'base':0}
                    runtime._singleton=Mock(return_value=2000)
                    runtime._gold=Mock(return_value=4000000)
                    copy=Mock(); copy.verify.side_effect=lambda obj,name:obj
                    copy.klass.side_effect=lambda obj,name:obj
                    copy.ptr.side_effect={132:1000,1080:6000,2448:3000,3016:12000,
                        3032:13000,1040:13000,actions.profile['statics']['ey']:8000,8184:9000,
                        4016:15000,4024:16000,4048:17000,4040:17000,
                        5016:18000,5024:19000}.__getitem__
                    kinds={'BlackMist':4,'Divine':5,'AncientDivine':6,'AncientLegendary':7}
                    integers={232:int(online),3024:5,9004:2,6152:1,6156:2,
                        4016:kinds[source],4020:amount,4024:kinds[target],4036:amount,
                        4032:1000,4040:1000,4052:800 if source=='AncientLegendary' else 0,
                        4064:800 if source=='AncientLegendary' else 0,4056:11,4048:11,
                        4068:845 if source=='AncientLegendary' else 0,5016:4,5024:4}
                    # Offline +56 is the failure item level, online +56 the
                    # refund threshold. They must never be interchanged.
                    if not online: integers[4056]=845 if source=='AncientLegendary' else 0
                    copy.integer.side_effect=integers.__getitem__
                    copy.number.return_value=0 if source==target else .5
                    # A server/local config may still advertise the removed
                    # BlackMist -> BlackMist recipe. Do not expose it for use.
                    copy.array.return_value=[struct.pack('<Q',4000),struct.pack('<Q',5000)]
                    actions.reader.string.side_effect={12000:'rules-a',15000:source,16000:target,
                        17000:'UpgradeCommodity1',18000:'BlackMist',19000:'BlackMist'}.__getitem__
                    actions.reader.asset_name.return_value='UpgradeCommodity1'
                    state=runtime._equipment(copy,100,200,include_objects=True)
                    self.assertEqual(len(state['recipes']),1)
                    actual=state['recipes'][0]
                    self.assertEqual((actual['option'],actual['input_count'],actual['gold_cost']), (option,amount,1000))
                    self.assertTrue(actual['available'])
                    self.assertEqual(state['_recipe_objects'][actual['recipe_id']],4000 if online else None)
                    actions.reader.memory.write.assert_not_called()

    def test_equipment_submit_accepts_authorized_unlock_and_delayed_receipt_is_not_resubmitted(self):
        fixture=runtime_fixtures.SnapshotAndReceiptTests()
        runtime,_,_=fixture.rune_execution()
        rec=recipe(); rows=[gear(i) for i in range(9)]
        runtime._bag.return_value=(rows,{},300,60)
        runtime._equipment=Mock(return_value=dict(available=True,recipes=[rec],gold=1000000,
            online=True,sequence=5,rules_version='rules-a',_recipe_objects={rec['recipe_id']:900}))
        runtime._confirm=Mock(side_effect=SynthesisReceiptPending('delayed',uncertain=True))
        payload=dict(kind='synthesize_equipment',recipe_id=rec['recipe_id'],count=1,
            inputs=[dict(r,quantity=1) for r in rows],input_uids=[r['item_uid'] for r in rows],character_id='role-a')
        self.assertTrue(runtime.execute('synthesize_equipment',payload)['synthesis_pending'])
        calls=runtime._invoke.call_args.args[0]
        submit=next(c for c in calls if c.method=='CubePanelUI.gbp')
        self.assertEqual(submit.arguments, (Argument.boolean(True),))
        self.assertEqual(sum(c.method=='CubePanelUI.gbh' for c in calls),9)
        from deskrawl_assistant.native_memory import GameBusyError
        with self.assertRaises(GameBusyError): runtime.execute('synthesize_equipment',payload)
        runtime._bag.return_value=([gear(20)],{},300,60)
        runtime._equipment.return_value.update(gold=999000,sequence=6)
        runtime._cleanup_cube=Mock(return_value=True)
        self.assertTrue(runtime.reconcile('role-a')['effect_confirmed'])
        self.assertEqual(runtime._invoke.call_count,1)


class EquipmentServiceTests(unittest.TestCase):
    setUp=service_fixtures.ManagedServiceTests.setUp

    def enable(self, **extra):
        synthesis_service_fixtures.ManagedSynthesisServiceTests.enable(self)
        self.service.managed.settings['synthesis']['equipment']=dict(DEFAULT_EQUIPMENT,enabled=True,
            black_mist_to_divine=True,**extra)
        self.client.capabilities['equipment_synthesis']={'available':True,'native_verified':False,'test_enabled':True}
        self.client.synthesis_snapshot=Mock(return_value=world([])['synthesis'])

    def test_only_equipment_enabled_reads_equipment_and_keeps_material_facts(self):
        self.enable()
        self.client.items=[service_fixtures.equipment(item_level=845,upgrade_level=12,socket_count=2,
            is_ancient=True,is_black_mist=True)]
        state=self.service._managed_observation()
        self.client.synthesis_snapshot.assert_called_once_with(gems=False,runes=False,equipment=True)
        for key in ('item_level','upgrade_level','socket_count','is_ancient','is_black_mist'):
            self.assertEqual(state['items'][0][key],self.client.items[0][key])

    def native_ancient_materials(self):
        self.enable()
        self.service.managed.settings['synthesis']['equipment'].update(
            black_mist_to_divine=False, ancient_to_black_mist=True)
        self.client.synthesis_snapshot.return_value = world(
            [], recipes=[recipe('ancient_to_black_mist')])['synthesis']
        # NativeReader reports generated identity proof inside read_evidence,
        # not at the top level used by the pure planner fixtures above.
        self.client.items = [service_fixtures.equipment(i, container='storage',
            item_level=845, upgrade_level=0, slot_locked=False, is_ancient=True)
            for i in range(9)]
        for row in self.client.items:
            row.pop('identity_verified', None)

    def test_native_nested_identity_proof_reaches_ancient_material_planner(self):
        self.native_ancient_materials()
        state = self.service._managed_observation()
        self.assertTrue(all(row['identity_verified'] is True for row in state['items']))
        action = plan(self.service.managed.settings, state)
        self.assertIsNotNone(action)
        self.assertEqual(action['kind'], 'transfer')
        self.assertEqual(action['recipe']['option'], 'ancient_to_black_mist')
        self.assertEqual(set(action['input_uids']), {row['item_uid'] for row in self.client.items})
        self.assertEqual(self.client.actions, [])

    def test_bridge_does_not_promote_missing_or_rejected_material_identity(self):
        for proof in ('missing', False, None, 'explicit_false', 'explicit_unknown'):
            with self.subTest(proof=proof):
                self.native_ancient_materials()
                material = self.client.items[-1]
                if proof == 'missing':
                    material['read_evidence'].pop('identity_verified')
                elif proof == 'explicit_false':
                    material['identity_verified'] = False
                elif proof == 'explicit_unknown':
                    material['identity_verified'] = None
                else:
                    material['read_evidence']['identity_verified'] = proof
                state = self.service._managed_observation()
                self.assertIsNone(plan(self.service.managed.settings, state))
                self.assertEqual(self.client.actions, [])

    def test_native_verified_ancient_below_server_level_still_cannot_complete_recipe(self):
        self.native_ancient_materials()
        self.client.items[-1]['item_level'] = 700
        state = self.service._managed_observation()
        self.assertIsNone(plan(self.service.managed.settings, state))
        self.assertEqual(self.client.actions, [])

    def test_changed_matched_preference_stops_previously_planned_unlock_or_craft(self):
        for locked in (False,True):
            with self.subTest(locked=locked):
                self.enable()
                state=world([gear(i,locked=locked,matches=True) for i in range(9)])
                state.update(session='offline-session',character_id=self.client.character_id)
                self.service.managed.character_id=self.client.character_id
                action=plan(self.service.managed.settings,state)
                self.service.managed.settings['synthesis']['equipment']['include_matched']=False
                with patch.object(self.service,'_managed_observation',return_value=state):
                    result=self.service._managed_execute(action,state)
                self.assertTrue(result['retryable'])
                self.assertFalse(result['verified'])
                self.assertEqual(self.client.actions,[])

    def test_unlock_without_current_synthesis_plan_is_rejected(self):
        self.enable()
        from deskrawl_assistant.models import ValidationError
        with self.assertRaises(ValidationError):
            self.service._managed_execute({'kind':'unlock','synthesis_group':'equipment','item':gear(0)},world([gear(0)]))

    def test_authorized_matched_material_unlock_resolves_native_identity(self):
        self.enable()
        self.client.items = [gear(i, locked=True, matches=True) for i in range(9)]
        state = world(self.client.items)
        state.update(session='offline-session', character_id=self.client.character_id)
        self.service.snapshot = {'complete': True, 'containers': {'inventory': {'slots': deepcopy(self.client.items)}}}
        def unlock(source, validate):
            actual = next(r for r in self.client.items if r['item_uid'] == source['item_uid'])
            self.assertTrue(validate(actual))
            actual['locked'] = False
            return {'status':'unlocked'}
        self.client.unlock_equipment = Mock(side_effect=unlock)
        with patch.object(self.service, '_managed_observation', return_value=state):
            result = self.service._managed_execute(plan(self.service.managed.settings,state),state)
        self.assertTrue(result['verified'])
        self.client.unlock_equipment.assert_called_once()
        self.assertFalse(self.client.items[0]['locked'])

    def test_equipment_preference_persists_in_managed_configuration(self):
        self.enable(include_matched=False)
        saved=validate_settings(self.service.managed.settings)
        self.service.managed_config.write_text(json.dumps(saved),encoding='utf-8')
        self.service._init_managed()
        self.assertFalse(self.service.managed_config_load_failed)
        self.assertFalse(self.service.managed.settings['synthesis']['equipment']['include_matched'])
