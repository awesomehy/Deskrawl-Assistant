"""Stale scene flags must neither strand rewards nor authorize other actions."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions, _Read
from deskrawl_assistant.native_memory import GameBusyError, SnapshotChangedError
from deskrawl_assistant.native_dispatcher import NativeActionError
from deskrawl_assistant.town_cleanup_recovery import StableTown, cleanup_ready
import test_managed_service as service_tests


class StableTownTests(unittest.TestCase):
    def test_requires_multiple_samples_ten_seconds_and_current_evidence(self):
        town = StableTown()
        self.assertFalse(town.observe(('town',), 'role', 100))
        self.assertFalse(town.observe(('town',), 'role', 105))
        self.assertFalse(town.observe(('town',), 'role', 109))
        self.assertTrue(town.observe(('town',), 'role', 110))
        self.assertFalse(town.check(('town',), 141))
        self.assertFalse(town.check(('other',), 111))

    def test_role_roots_gap_clock_and_unsafe_reads_restart_stability(self):
        for change in ('role', 'root', 'gap', 'clock', 'unsafe'):
            with self.subTest(change=change):
                town = StableTown()
                for now in (100, 105, 110): town.observe(('town',), 'role', now)
                self.assertTrue(town.check(('town',), 110))
                args = {'role':(('town',), 'other', 111), 'root':(('other',), 'role', 111),
                        'gap':(('town',), 'role', 141), 'clock':(('town',), 'role', 109),
                        'unsafe':(None, 'role', 111)}[change]
                self.assertFalse(town.observe(*args))


class NativeCleanupBoundaryTests(unittest.TestCase):
    def test_stability_guards_are_bounded_and_retain_carriage_roots_and_offload_gates(self):
        reader=Mock()
        reader.memory.read.side_effect=lambda address,size:b'\0'*size
        # A fully unlocked warehouse must not consume 250 mailbox entries.
        reader.array.return_value=([b'\0'*8]*250,b'\0'*2000)
        read=_Read(reader,{})
        self.assertEqual(len(read.pointers(1000,'InventorySlot',contents=False)),250)
        self.assertEqual(read.guards,[(1024,(250).to_bytes(8,'little'))])
        game=GameActions(reader,dispatcher=Mock())
        reader.class_name.return_value='List`1'
        reader.fields.return_value=[{'name':k,'offset':v} for k,v in {'_items':16,'_size':24,'_version':28}.items()]
        reader.memory.read.side_effect=None
        memory={1176:b'\0'*8,1160:(2000).to_bytes(8,'little'),2024:b'\0'*4}
        reader.memory.read.side_effect=lambda address,size:memory[address]
        value={'items':[],'guards':[(3000,b'\0'*8192)],'context_guards':[(4000,b'class')],'carriage':1000}
        with patch('deskrawl_assistant.carriage_transfer.read_carriage',return_value=value), \
                patch.object(game,'_enrich_drops') as enrich:
            state=game._carriage(read,in_town=True,offload_only=True)
        self.assertTrue(state['complete'])
        self.assertIn((4000,b'class'),read.guards)
        self.assertIn((1176,b'\0'*8),read.guards)
        self.assertIn((2024,b'\0'*4),read.guards)
        self.assertNotIn((3000,b'\0'*8192),read.guards)
        self.assertLess(sum((len(raw)+7)//8 for _,raw in read.guards),10)
        enrich.assert_not_called()

    def test_native_candidate_requires_idle_town_roots_containers_and_finished_offload(self):
        reader, read = Mock(), Mock()
        reader.classes = {'Inventory':9000, 'Storage':8000}
        reader.session_id = 'session'
        reader.class_name.return_value = 'GameObject'
        pointers = {1072:4000,4032:5000,2128:6000,2040:7000,7000:17000,
                    9184:10000,10000:12000,12000:9000,12056:14000,
                    8184:11000,11000:13000,13000:8000,13048:15000,1096:18000}
        flags = {2176:True}
        read.ptr.side_effect = lambda a: pointers.get(a,0)
        read.boolean.side_effect = lambda a: flags.get(a,False)
        read.verify.side_effect = lambda obj,name: obj
        read.singleton.side_effect = lambda name: {'TownPortalSystem':3000,'SaveSystem':16000}[name]
        read.read.return_value = b'role-and-save'
        read.pointers.return_value = [19000,20000]
        game = GameActions(reader, dispatcher=Mock())
        online = {'online_saving':False,'online_pending_requests':0}
        game._online_state = Mock(return_value=online)
        carriage = {'complete':True,'settling':False}
        game._carriage = Mock(return_value=carriage)
        self.assertIsNotNone(game._town_cleanup_signature(read,1000,2000,1))
        for flag in (3076,1284,5296,4080):
            flags[flag] = True
            self.assertIsNone(game._town_cleanup_signature(read,1000,2000,1))
            flags[flag] = False
        for key,value in (('online_saving',True),('online_pending_requests',1)):
            before = online[key];online[key] = value
            self.assertIsNone(game._town_cleanup_signature(read,1000,2000,1))
            online[key] = before
        carriage.update(complete=False,settling=True)
        self.assertIsNone(game._town_cleanup_signature(read,1000,2000,1))
        carriage.update(complete=True,settling=False)
        read.pointers.return_value = []
        self.assertIsNone(game._town_cleanup_signature(read,1000,2000,1))
        game.dispatcher.invoke.assert_not_called()

    def ready(self):
        game = GameActions(Mock(), dispatcher=Mock(), allow_unverified=True)
        read = Mock(guards=[(123, b'native-state')])
        read.boolean.side_effect = lambda a: a == 2+176
        read.singleton.return_value = 3
        game._context = Mock(return_value=(read, 1, 2, 1))
        game._town_cleanup_signature = Mock(return_value=('town',))
        game.capabilities = Mock(return_value={'compatible':True})
        for now in (100,105,110): game._stable_town.observe(('town',), 'role', now)
        return game, read

    def test_native_grant_keeps_guards_and_rejects_changed_roots_before_mutation(self):
        game, read = self.ready()
        with patch('deskrawl_assistant.game_actions.time.monotonic', return_value=111):
            self.assertEqual(game.town_cleanup_guards('transfer'), read.guards)
            game._town_cleanup_signature.return_value = ('different-town',)
            with self.assertRaises(GameBusyError): game.town_cleanup_guards('transfer')
        self.assertIsNone(game._stable_town.signature)
        game.dispatcher.invoke.assert_not_called()

    def test_no_request_flag_can_bypass_missing_grant_or_expand_allowed_actions(self):
        game, read = self.ready()
        game._stable_town.reset()
        with self.assertRaises(GameBusyError):
            game.execute('open_chest', {'_town_cleanup':True})
        for kind in ('enter_map', 'equip', 'merchant_buy', 'synthesize_runes', 'auction_upload'):
            with self.subTest(kind=kind), self.assertRaises(GameBusyError):
                game.execute(kind, {'_town_cleanup':True})
        game.dispatcher.invoke.assert_not_called()

    def test_grant_still_requires_actual_item_identity_and_native_result(self):
        game, read = self.ready()
        game._selected_inventory = Mock(side_effect=NativeActionError('物品身份改变'))
        with patch('deskrawl_assistant.game_actions.time.monotonic', return_value=111):
            with self.assertRaisesRegex(NativeActionError, '物品身份改变'):
                game.execute('open_chest', {'_town_cleanup':True})
        game.dispatcher.invoke.assert_not_called()

    def test_failed_native_guard_read_revokes_grant(self):
        game, read = self.ready()
        read.finish.side_effect = SnapshotChangedError('scene changed')
        with self.assertRaises(SnapshotChangedError): game.town_cleanup_guards('lock')
        self.assertIsNone(game._stable_town.signature)

    def test_failed_action_context_and_normal_scene_revoke_grant(self):
        for outcome in ('context_failure', 'signature_failure', 'normal_scene', 'changed_roots'):
            with self.subTest(outcome=outcome):
                game, read = self.ready()
                game._online_state = Mock(return_value={'online_saving':False,'online_pending_requests':0})
                if outcome == 'context_failure':
                    game._context.side_effect = SnapshotChangedError('new scene')
                elif outcome == 'signature_failure':
                    game._town_cleanup_signature.side_effect = SnapshotChangedError('new roots')
                elif outcome == 'changed_roots':
                    game._town_cleanup_signature.return_value = ('other-town',)
                else:
                    read.boolean.return_value = False
                    read.boolean.side_effect = None
                with patch('deskrawl_assistant.game_actions.time.monotonic', return_value=111):
                    if outcome in ('normal_scene','changed_roots'):
                        self.assertIsNone(game.action_context_snapshot()['scene']['town_cleanup_recovery'])
                    else:
                        with self.assertRaises(SnapshotChangedError): game.action_context_snapshot()
                self.assertIsNone(game._stable_town.signature)

    def test_native_submission_still_requires_receive_side_confirmation(self):
        game, read = self.ready()
        row = {'selection_id':'drop', '_address':4000, 'internal_name':'GoldBar', 'count':1}
        game._ground = Mock(return_value=[row])
        game._before_pickup = Mock()
        game._confirm_effect = Mock(return_value=False)
        game.dispatcher.invoke.return_value = {}
        with patch('deskrawl_assistant.game_actions.time.monotonic', return_value=111):
            with self.assertRaises(NativeActionError) as failed:
                game.execute('collect_ground', {'_town_cleanup':True,'selection_id':'drop'})
        self.assertTrue(failed.exception.uncertain)
        game.dispatcher.invoke.assert_called_once()
        self.assertIn((123,b'native-state'), game.dispatcher.invoke.call_args.args[1])


class CleanupChestCycleTests(unittest.TestCase):
    @staticmethod
    def world(items, **kwargs):
        from test_managed_mode import observation
        obs = observation(items,town=True,character_id='role',**kwargs)
        obs['scene'].update(scene_changing=True,teleporting=False,online_saving=False,
            online_pending_requests=0,dead=False,in_battle=False,
            town_cleanup_recovery={'eligible':True,'session':obs['session'],'character_id':'role'})
        return obs

    def test_upgrade_and_auction_candidates_are_preserved_until_normal_operations_resume(self):
        from test_managed_mode import ManagedMode, settings, equipment, verified
        for purpose in ('upgrade', 'auction'):
            with self.subTest(purpose=purpose):
                s = settings(equip=purpose=='upgrade',equipment_action='sell',minion_equipment_action='salvage')
                row = equipment('keep',power={'available':True,'target':8,'delta':100})
                if purpose == 'auction':
                    s['auction'].update(upload_enabled=True,categories=['minion_equipment'])
                    row.update(internal_name='LegendaryMinionGear1',name_key='LegendaryMinionGear1',equip_slot=12,
                        auction_candidate=True,auction_identity_verified=True)
                obs = self.world([row]);engine = ManagedMode(s);engine.start(obs)
                action = engine.next_action(obs)
                self.assertEqual((action['kind'],action['target']),('transfer','storage'))
                self.assertEqual(action['item']['item_uid'],'keep')
                verified(engine,action)
                row.update(container='storage',slot_index=0)
                self.assertIsNone(engine.next_action(self.world([row])))
                # Removing the stale flag restores the original equip/upload
                # planner, including withdrawing this exact stored candidate.
                normal = self.world([row]);normal['scene']['scene_changing'] = False
                normal['auction'] = {'available':True,'steamid_verified':True,'needs_refresh':True}
                resumed = engine.next_action(normal)
                if purpose == 'upgrade':
                    self.assertEqual((resumed['kind'],resumed['target'],resumed['reason_code']),
                                     ('transfer','inventory','equip_prepare'))
                else:
                    self.assertEqual(resumed['kind'],'auction_open')

    def test_full_storage_retains_upgrade_and_disposes_only_unrelated_junk(self):
        from test_managed_mode import ManagedMode, settings, equipment, verified
        upgrade = equipment('upgrade',power={'available':True,'target':8,'delta':100})
        junk = equipment('junk',1)
        stored = equipment('stored',0,'storage',locked=True)
        engine = ManagedMode(settings(equip=True,equipment_action='sell'))
        obs = self.world([upgrade,junk,stored],allowed=[0]);engine.start(obs)
        action = engine.next_action(obs)
        self.assertEqual((action['kind'],action['item']['item_uid']),('sell','junk'))
        verified(engine,action)
        self.assertIsNone(engine.next_action(self.world([upgrade,stored],allowed=[0])))
        self.assertTrue(engine.running)

    def test_full_bag_opens_chest_then_collects_protects_and_stores_without_departing(self):
        from test_managed_mode import ManagedMode, settings, observation, equipment, loot, verified
        box = loot('box','chest','WorldBossPumpkinChest1Inferno', item_type=2)
        potion = loot('potion','material','MovementPotion1',1,count=14)
        bag = [box,potion]
        ground = []
        def world():
            obs = observation(bag,town=True,capacity=2,character_id='role')
            obs['scene'].update(scene_changing=True,teleporting=False,online_saving=False,
                online_pending_requests=0,dead=False,in_battle=False,
                town_cleanup_recovery={'eligible':True,'session':obs['session'],'character_id':'role'})
            obs['ground']['items'] = deepcopy(ground)
            return obs
        engine = ManagedMode(settings(equip=True))
        engine.start(world())
        kinds = []
        for expected in ('open_chest','collect_ground','lock','transfer'):
            action = engine.next_action(world())
            self.assertIsNotNone(action, engine.status)
            self.assertEqual(action['kind'], expected)
            kinds.append(action['kind'])
            verified(engine,action)
            if expected == 'open_chest':
                self.assertIsNone(engine.next_action(world()))  # stale receipt cannot open twice
                bag.remove(box)
                ground.append(equipment('keep',container='ground',matches=True))
            elif expected == 'collect_ground':
                row = ground.pop();row.update(container='inventory',slot_index=0);bag.append(row)
            elif expected == 'lock': bag[-1]['locked'] = True
            else: bag[-1].update(container='storage',slot_index=0)
        self.assertIsNone(engine.next_action(world()))
        self.assertIn('受限整理已完成',engine.status)
        self.assertEqual(potion['count'],14)
        self.assertTrue(engine.running)


class ManagedCleanupRecoveryTests(unittest.TestCase):
    setUp = service_tests.ManagedServiceTests.setUp
    settings = service_tests.ManagedServiceTests.settings
    ready = service_tests.ManagedServiceTests.ready

    def grant(self):
        original = self.client.control_snapshot
        def control():
            value = original()
            value.update(character_id='offline-role', scene_changing=True, dead=False, in_battle=False,
                town_cleanup_recovery={'eligible':True,'character_id':'offline-role','session':'offline-session'})
            return value
        self.client.control_snapshot = control
        self.service.managed.character_id = 'offline-role'
        self.client.action_context_snapshot = lambda: {'available':True,'character_id':'offline-role','scene':{
            **control()['scene'], **{k:control()[k] for k in ('scene_changing','teleporting','dead',
                'in_battle','online_saving','online_pending_requests','town_cleanup_recovery')}}}
        for name in ('move_items', 'lock_equipment', 'collect_carriage'):
            original_action = getattr(self.client, name)
            def guarded(*args, _original=original_action, **kwargs):
                self.assertTrue(kwargs.pop('town_cleanup'))
                return _original(*args, **kwargs)
            setattr(self.client, name, guarded)

    def test_protects_and_stores_rewards_while_preserving_potions_and_run_count(self):
        self.client.items = [service_tests.equipment(is_black_mist=True, locked=False), service_tests.potion(1)]
        self.ready(farming=True, auto_equip=True)
        self.grant()
        self.service.managed.repeat_completed = 33
        with patch('deskrawl_assistant.managed_mode.auction_action', side_effect=AssertionError('auction')), \
                patch('deskrawl_assistant.managed_mode.synthesis_action', side_effect=AssertionError('synthesis')), \
                patch.object(self.service.managed, '_equip', side_effect=AssertionError('equip')):
            for _ in range(7): self.service._managed_once()
        self.assertEqual([a[0] for a in self.client.actions], ['lock','transfer'])
        self.assertEqual(self.client.items[0]['container'], 'storage')
        self.assertTrue(self.client.items[0]['locked'])
        self.assertEqual(self.client.items[1]['container'], 'inventory')
        self.assertEqual(self.client.items[1]['count'], 14)
        self.assertEqual(self.service.managed.repeat_completed, 33)
        self.assertTrue(self.service.managed.running)
        self.assertIn('受限整理已完成', self.service.managed.status)
        self.assertTrue(self.service._managed_observation()['scene']['scene_changing'])

    def test_proof_is_bound_to_identity_sync_and_actual_native_transition(self):
        self.ready()
        self.grant()
        observation = self.service._managed_observation()
        self.assertTrue(cleanup_ready(observation))
        changes = [('session','other'),('character_id','other'),('complete',False)]
        for field,value in changes:
            bad = deepcopy(observation); bad[field] = value
            self.assertFalse(cleanup_ready(bad))
        for field,value in (('in_town',False),('teleporting',True),('online_saving',True),
                            ('dead',True),('in_battle',True),('online_pending_requests',1),
                            ('online_pending_requests',None),('town_cleanup_recovery',None)):
            bad = deepcopy(observation); bad['scene'][field] = value
            self.assertFalse(cleanup_ready(bad))

    def test_lost_native_proof_between_plan_and_execution_does_not_write(self):
        self.ready()
        self.grant()
        observation = self.service._managed_observation()
        action = self.service.managed.next_action(observation)
        self.assertIsNotNone(action)
        self.client.action_context_snapshot = lambda: {'available':True,'scene':{
            **observation['scene'], 'town_cleanup_recovery':None}}
        with self.assertRaises(GameBusyError): self.service._managed_execute(action, observation)
        self.assertEqual(self.client.actions, [])

    def test_service_rejects_non_cleanup_actions_even_with_valid_proof(self):
        self.ready(farming=True)
        self.grant()
        observation = self.service._managed_observation()
        for action in ({'kind':'enter_dungeon'}, {'kind':'merchant_buy'}, {'kind':'synthesize_runes'},
                       {'kind':'equip'}, {'kind':'transfer','target':'inventory'}):
            with self.subTest(action=action), self.assertRaises(GameBusyError):
                self.service._managed_execute(action, observation)
        self.assertEqual(self.client.actions, [])

    def test_stable_town_closes_old_return_timer_but_keeps_cleanup_backoff(self):
        self.ready()
        self.grant()
        self.service.managed_return_recovery = {'started':1}
        self.service.managed_retry = {'key':('game_busy','return_town'),'started':1,
            'next_at':9999,'action_kind':'return_town','reason':'old return','attempts':1}
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=500), \
                patch.object(self.service.managed,'next_action',return_value=None) as planner:
            self.service._managed_once()
        planner.assert_called_once()
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_return_recovery)
        self.assertIsNone(self.service.managed_retry)
        action = {'kind':'transfer','target':'storage'}
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=501):
            self.service._managed_defer(action,'retry storage','game_busy')
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=502), \
                patch.object(self.service.managed,'next_action') as planner:
            self.service._managed_once()
        planner.assert_not_called()
        self.assertEqual(self.service.managed_retry['action_kind'],'transfer')
        self.assertEqual(self.client.actions, [])


if __name__ == '__main__': unittest.main()
