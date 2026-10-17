"""No game process is read, opened, patched, or invoked by these tests."""
import ctypes as C
from ctypes import wintypes as W
import os
import struct
import threading
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions
from deskrawl_assistant.native_dispatcher import (
    Argument, Invocation, NativeActionError, compile_mailbox, build_trampoline,
    load_profile, MainThreadDispatcher, MAX_GUARDS, DONE, CANCELLED, GUARD_FAILED, REJECTED, INVALID,
)
from deskrawl_assistant.native_memory import MemoryReadError, GameBusyError, DeathRecoveryBusyError


class ActionBoundaryTests(unittest.TestCase):
    def test_stargazing_entry_only_allows_empty_combat_with_matching_source_and_keeps_normal_town_guard(self):
        from deskrawl_assistant.native_memory import SnapshotChangedError
        for case in ('clean','ground','carriage','unknown_carriage','new_run','same_map','ordinary','attacking','gathering'):
            with self.subTest(case=case):
                reader,dispatcher,read=Mock(),Mock(),Mock()
                read.guards=[];read.singleton.return_value=3;read.boolean.return_value=False
                dispatcher.invoke.return_value={}
                game=GameActions(reader,dispatcher=dispatcher,allow_unverified=True)
                state={'available':True,'difficulty':'Normal','map_id':'a',
                       'run':{'id':'new' if case=='new_run' else 'source','status':'running'},
                       'maps':[{'map_id':'b','can_enter':True},{'map_id':'a','can_enter':True}]}
                payload={'map_id':'a' if case=='same_map' else 'b','difficulty':'Normal',
                         'source_run_id':'source','reason_code':'stargazing_skip'}
                if case=='ordinary':payload.pop('reason_code')
                with patch.object(game,'capabilities',return_value={'compatible':True}), \
                        patch.object(game,'_context',return_value=(read,1,2,0)), \
                        patch.object(game,'_require_travel_ready'), \
                        patch.object(game,'control_snapshot',return_value=state), \
                        patch.object(game,'_map_objects',return_value={'b':1000,'a':2000}), \
                        patch.object(game,'_require_stargazing_run') as guard, \
                        patch.object(game,'_combat_travel_state',return_value={
                            'travel_blocked':case in ('attacking','gathering'), 'travel_wait_reason':'等待攻击或采集结束'}), \
                        patch.object(game,'_ground',return_value=[{'actual':True}] if case=='ground' else []), \
                        patch.object(game,'_carriage',return_value={'complete':case!='unknown_carriage',
                            'items':[{'actual':True}] if case=='carriage' else []}):
                    if case=='clean':
                        result=game.execute('enter_map',payload)
                        self.assertTrue(result['transition_requested'])
                        guard.assert_called_once_with(read,1,'source')
                        dispatcher.invoke.assert_called_once()
                    else:
                        with self.assertRaises((NativeActionError,SnapshotChangedError,GameBusyError)):
                            game.execute('enter_map',payload)
                        dispatcher.invoke.assert_not_called()

    def test_combat_travel_gates_match_native_attack_exception_and_guard_state_reads(self):
        from deskrawl_assistant.game_actions import _Read
        for state,gathering,map_type,blocked in ((0,False,0,False),(1,False,0,True),
                                               (1,False,2,False),(0,True,2,True)):
            with self.subTest(state=state,gathering=gathering,map_type=map_type):
                reader=Mock()
                memory={1064:struct.pack('<Q',2000),1112:struct.pack('<Q',3000),
                        2048:struct.pack('<i',state),2052:bytes([gathering]),3032:struct.pack('<i',map_type)}
                reader.memory.read.side_effect=lambda address,size:memory[address]
                read=_Read(reader,{})
                read.verify=Mock(side_effect=lambda address,name:address)
                result=GameActions(reader,dispatcher=Mock())._combat_travel_state(read,1000)
                self.assertEqual(result['travel_blocked'],blocked)
                self.assertEqual(result['gathering'],gathering)
                self.assertEqual(result['player_state'],state)
                self.assertEqual({address for address,_ in read.guards},set(memory))
                self.assertEqual(bool(result['travel_wait_reason']),blocked)

    def test_stargazing_native_guards_bind_plan_pointer_and_run_identity_before_dispatch(self):
        import hashlib
        from deskrawl_assistant.native_memory import SnapshotChangedError
        reader,read,copy=Mock(),Mock(),Mock();read.guards=[]
        reader.string.return_value='original-run'
        copy.ptr.side_effect=lambda address:{1104:2000,1120:3000,1112:4000,2088:3000,2040:4000,3016:5000}[address]
        copy.verify.side_effect=lambda obj,name:obj
        copy.boolean.side_effect=lambda address:address==2096
        copy.guards=[(1120,b'original-plan-pointer')]
        game=GameActions(reader,dispatcher=Mock())
        with patch('deskrawl_assistant.recommendation_reader._Copy',return_value=copy):
            game._require_stargazing_run(read,1000,hashlib.sha256(b'original-run').hexdigest())
            self.assertIn(copy.guards[0],read.guards)
            with self.assertRaises(SnapshotChangedError):game._require_stargazing_run(read,1000,'new-run')
            copy.boolean.return_value=True;copy.boolean.side_effect=None
            with self.assertRaisesRegex(SnapshotChangedError,'关卡已结束'):
                game._require_stargazing_run(read,1000,hashlib.sha256(b'original-run').hexdigest())
        game.dispatcher.invoke.assert_not_called()

    def test_inventory_context_reads_scene_and_online_gates_without_campaign(self):
        reader,dispatcher,read=Mock(),Mock(),Mock()
        game=GameActions(reader,dispatcher=dispatcher)
        game._context=Mock(return_value=(read,1,2000,1))
        game._online_state=Mock(return_value={'online_saving':True,'online_pending_requests':2})
        read.singleton.return_value=3000;read.boolean.side_effect=lambda address:address==3076
        game.control_snapshot=Mock()
        value=game.action_context_snapshot()
        self.assertTrue(value['scene']['in_town']);self.assertTrue(value['scene']['teleporting'])
        self.assertTrue(value['scene']['online_saving']);self.assertEqual(value['scene']['online_pending_requests'],2)
        game._context.assert_called_once_with(allow_busy=True);read.finish.assert_called_once()
        game.control_snapshot.assert_not_called();dispatcher.invoke.assert_not_called()

    def test_disposal_receipt_keeps_complete_post_action_snapshot(self):
        reader,dispatcher=Mock(),Mock();game=GameActions(reader,dispatcher=dispatcher)
        snapshot={'complete':True,'bridge_session':'same','containers':{'inventory':{'slots':[]}}}
        reader.snapshot.return_value=snapshot
        selected={'slot_index':0,'item_uid':'removed'}
        self.assertTrue(game._confirm_effect('salvage',selected,{}))
        self.assertIs(selected['_confirmed_snapshot'],snapshot)

    def test_online_queue_blocks_travel_before_any_call(self):
        reader,dispatcher,read=Mock(),Mock(),Mock()
        read.singleton.side_effect=lambda name:{'SaveSystem':1000,'CloudClient':2000}[name]
        read.boolean.return_value=False
        read.integer.side_effect=lambda address:{1032:1,2276:3}[address]
        actions=GameActions(reader,dispatcher=dispatcher)
        with self.assertRaisesRegex(GameBusyError,'待处理请求 3'):
            actions._return_town_call(read,5,6)
        dispatcher.invoke.assert_not_called()

    def test_offline_travel_gate_does_not_require_cloud_client(self):
        reader,dispatcher,read=Mock(),Mock(),Mock()
        read.singleton.side_effect=lambda name:1000 if name=='SaveSystem' else self.fail('Unexpected cloud read')
        read.boolean.return_value=False;read.integer.return_value=0
        actions=GameActions(reader,dispatcher=dispatcher)
        self.assertEqual(actions._require_travel_ready(read),{'online_saving':False,'online_pending_requests':0})
        dispatcher.invoke.assert_not_called()

    def test_invalid_queue_count_stays_a_hard_failure(self):
        reader,dispatcher,read=Mock(),Mock(),Mock()
        read.singleton.side_effect=lambda name:{'SaveSystem':1000,'CloudClient':2000}[name]
        read.boolean.return_value=False
        read.integer.side_effect=lambda address:{1032:1,2276:-1}[address]
        with self.assertRaisesRegex(NativeActionError,'队列状态异常'):
            GameActions(reader,dispatcher=dispatcher)._require_travel_ready(read)
        dispatcher.invoke.assert_not_called()

    def test_online_busy_blocks_mutation_context_but_allows_receipt_reads(self):
        reader, dispatcher, read = Mock(), Mock(), Mock()
        read.singleton.return_value=1000;read.integer.return_value=1;read.boolean.return_value=True
        actions=GameActions(reader,dispatcher=dispatcher)
        with patch('deskrawl_assistant.game_actions._Read',return_value=read):
            with self.assertRaisesRegex(GameBusyError,'在线同步'):actions._context()
            self.assertEqual(actions._context(allow_busy=True)[-1],1)
        dispatcher.invoke.assert_not_called()

    def unverified_profile(self):
        profile = load_profile()
        profile['verified_kinds'] = []
        profile.pop('enabled_kinds', None)
        return patch('deskrawl_assistant.game_actions.load_profile', return_value=profile)

    def test_release_profile_exposes_all_actions_without_overstating_recorded_validation(self):
        reader, dispatcher = Mock(), Mock()
        dispatcher.installed = False
        actions = GameActions(reader, dispatcher=dispatcher)
        self.assertEqual(actions.verified_kinds,
            {'return_town','enter_map','switch_difficulty','equip','sell','salvage','open_chest','collect_ground',
             'allocate_talent','merchant_open','merchant_buy','merchant_close','merchant_refresh',
             'synthesize_gems','synthesize_runes'})
        state = actions.capabilities()
        self.assertFalse(state['native_verified'])
        self.assertFalse(state['test_mode'])
        self.assertIn('尚未实机验收', state['reason'])
        for key in ('travel','difficulty','equip','sell','salvage','chests','ground_pickup','merchant','talents',
                    'gem_synthesis','rune_synthesis'):
            self.assertTrue(state[key]['available'])
            self.assertTrue(state[key]['native_verified'])
            self.assertFalse(state[key]['test_enabled'])
            self.assertEqual(state[key]['reason'], '')
        for key in ('life_skills', 'skills'):
            self.assertTrue(state[key]['available'])
            self.assertTrue(state[key]['release_enabled'])
            self.assertFalse(state[key]['test_enabled'])
            self.assertFalse(state[key]['native_verified'])
        dispatcher.install.assert_not_called()
        dispatcher.invoke.assert_not_called()

    def test_all_released_growth_dispatch_works_without_test_mode_and_preserves_validation_status(self):
        reader, dispatcher, growth = Mock(), Mock(), Mock()
        actions = GameActions(reader, dispatcher=dispatcher)
        with patch.object(actions, 'progression_runtime', return_value=growth):
            for kind in ('allocate_talent','merchant_open','merchant_buy','merchant_close','merchant_refresh','learn_life_skill','set_skill'):
                with self.subTest(kind=kind):
                    growth.execute.return_value = {'effect_confirmed': True, 'native_verified': False}
                    result = actions.execute(kind, {'class_mask': 4})
                    self.assertTrue(result['effect_confirmed'])
                    self.assertEqual(result['native_verified'], kind in actions.verified_kinds)
                    growth.execute.assert_called_with(kind, {'class_mask': 4}, cancel_event=None, validate=unittest.mock.ANY)
            self.assertEqual(growth.execute.call_count, 7)
        dispatcher.install.assert_not_called()
        dispatcher.invoke.assert_not_called()

    def test_released_synthesis_dispatches_without_test_mode_and_keeps_receipt_guards(self):
        reader, dispatcher, synthesis = Mock(), Mock(), Mock()
        actions = GameActions(reader, dispatcher=dispatcher)
        validate = Mock(return_value=True)
        with patch.object(actions, 'synthesis_runtime', return_value=synthesis):
            for kind in ('synthesize_gems', 'synthesize_runes'):
                synthesis.execute.return_value = {'verified': True}
                result = actions.execute(kind, {'recipe_id': 'recipe'}, validate=validate)
                self.assertTrue(result['verified'])
                self.assertTrue(result['native_verified'])
                synthesis.execute.assert_called_with(kind, {'recipe_id': 'recipe'},
                    cancel_event=None, validate=validate)
            self.assertEqual(synthesis.execute.call_count, 2)
            validate.return_value = False
            with self.assertRaisesRegex(NativeActionError, '托管操作已停止'):
                actions.execute('synthesize_gems', {}, validate=validate)
            self.assertEqual(synthesis.execute.call_count, 2)
        dispatcher.install.assert_not_called()
        dispatcher.invoke.assert_not_called()

    def test_fully_verified_release_still_blocks_changed_game_build(self):
        reader, dispatcher = Mock(), Mock()
        dispatcher.installed = False
        dispatcher.verify.side_effect = ValueError('构建指纹不同')
        actions = GameActions(reader, dispatcher=dispatcher)
        state = actions.capabilities()
        self.assertFalse(state['native_verified'])
        self.assertFalse(state['compatible'])
        for key in ('travel','difficulty','equip','sell','salvage','chests','ground_pickup',
                    'gem_synthesis','rune_synthesis'):
            self.assertFalse(state[key]['available'])
        for kind in ('sell','switch_difficulty','synthesize_gems','synthesize_runes'):
            with self.assertRaisesRegex(NativeActionError, '构建指纹不同'):
                actions.execute(kind, {})
        dispatcher.install.assert_not_called()
        dispatcher.invoke.assert_not_called()

    def test_carriage_offload_is_incomplete_until_coroutine_and_flights_finish(self):
        reader,read=Mock(),Mock();reader.memory.u64.return_value=42
        reader.class_name.return_value='List`1'
        reader.fields.return_value=[{'name':k,'offset':v} for k,v in {'_items':16,'_size':24,'_version':28}.items()]
        action=GameActions(reader,dispatcher=Mock())
        value={'items':[],'guards':[],'carriage':1000}
        for town,coroutine,flights,complete in ((False,99,5,True),(True,99,0,False),(True,0,2,False),(True,0,0,True)):
            with self.subTest(town=town,coroutine=coroutine,flights=flights):
                read.guards=[];read.ptr.side_effect=lambda address:coroutine if address==1176 else 2000
                read.integer.return_value=flights
                with patch('deskrawl_assistant.carriage_transfer.read_carriage',return_value=value),patch.object(action,'_enrich_drops'):
                    self.assertEqual(action._carriage(read,in_town=town)['complete'],complete)
        # Animation Clear() changes _version every frame. Settling guards only
        # its pointer/size, so an empty animation does not make scenes unreadable.
        read.read.assert_not_called()

    def test_construct_and_capabilities_do_not_install(self):
        reader, dispatcher = Mock(), Mock()
        dispatcher.installed = False
        with self.unverified_profile():
            actions = GameActions(reader, dispatcher=dispatcher)
        reader.check_game_files.assert_not_called()
        state = actions.capabilities()
        dispatcher.verify.assert_called_once()
        dispatcher.install.assert_not_called()
        dispatcher.invoke.assert_not_called()
        self.assertFalse(state['native_verified'])
        for key in ('travel','equip','sell','salvage','chests','ground_pickup'):
            self.assertTrue(state[key]['implemented'])
            self.assertFalse(state[key]['available'])

    def test_unverified_actions_stop_before_reading_or_installing(self):
        reader, dispatcher = Mock(), Mock()
        with self.unverified_profile():
            actions = GameActions(reader, dispatcher=dispatcher)
        for kind in ('equip','sell','salvage','open_chest','enter_map','return_town','collect_ground','switch_difficulty'):
            with self.assertRaisesRegex(NativeActionError, '尚未实机验收'):
                actions.execute(kind, {})
        reader.check_game_files.assert_not_called()
        dispatcher.install.assert_not_called()
        dispatcher.invoke.assert_not_called()

    def test_difficulty_change_uses_original_method_and_requires_town_unlocked_target(self):
        reader,dispatcher,read = Mock(),Mock(),Mock()
        read.boolean.return_value=False; read.singleton.return_value=3; read.guards=[]
        options=[{'id':'Nightmare','value':1,'unlocked':True}]
        for scene,unlocked in ((0,True),(1,False),(1,True)):
            dispatcher.reset_mock(); dispatcher.invoke.return_value={}
            options[0]['unlocked']=unlocked
            actions=GameActions(reader,dispatcher=dispatcher,allow_unverified=True)
            with patch.object(actions,'capabilities',return_value={'compatible':True}), \
                    patch.object(actions,'_context',return_value=(read,1,2,scene)), \
                    patch.object(actions,'_ground',return_value=[]), \
                    patch.object(actions,'_carriage',return_value={'complete':True,'items':[]}), \
                    patch.object(actions,'_require_stored_inventory'), \
                    patch.object(actions,'_difficulty_state',return_value=('Normal',options)), \
                    patch.object(actions,'_confirm_difficulty',return_value=True):
                if scene==0 or not unlocked:
                    with self.assertRaises(NativeActionError): actions.execute('switch_difficulty',{'difficulty':'Nightmare'})
                    dispatcher.invoke.assert_not_called()
                else:
                    result=actions.execute('switch_difficulty',{'difficulty':'Nightmare'})
                    self.assertTrue(result['effect_confirmed'])
                    call=dispatcher.invoke.call_args.args[0][0]
                    self.assertEqual(call.method,'ey.dcq')
                    self.assertEqual(call.arguments[0].raw,struct.pack('<i',1))


    def test_local_test_opt_in_exposes_capabilities_without_claiming_validation(self):
        reader, dispatcher = Mock(), Mock()
        dispatcher.installed = False
        with self.unverified_profile():
            actions = GameActions(reader, dispatcher=dispatcher, allow_unverified=True)
        state = actions.capabilities()
        self.assertTrue(state['test_mode'])
        self.assertFalse(state['native_verified'])
        for key in ('travel','equip','sell','salvage','chests','ground_pickup'):
            self.assertTrue(state[key]['available'])
            self.assertTrue(state[key]['test_enabled'])
            self.assertFalse(state[key]['native_verified'])
        dispatcher.install.assert_not_called()
        dispatcher.invoke.assert_not_called()

    def test_local_test_mode_still_requires_compatible_build(self):
        reader, dispatcher = Mock(), Mock()
        dispatcher.verify.side_effect = ValueError('构建指纹不同')
        state = GameActions(reader, dispatcher=dispatcher, allow_unverified=True).capabilities()
        self.assertFalse(state['compatible'])
        self.assertFalse(state['travel']['available'])
        self.assertFalse(state['travel']['test_enabled'])
        self.assertIn('构建指纹',state['travel']['reason'])
        dispatcher.install.assert_not_called()

    def test_disposition_never_accepts_protected_item(self):
        allowed = {'disposition_authorized':True}
        base = {'is_equipment':True, 'modifiers_complete':True, 'rarity':3}
        for patch in ({'slot_locked':True}, {'locked':True}, {'is_black_mist':True}, {'rarity':4},
                {'modifiers_complete':False}, {'issues':['unverified']}):
            with self.assertRaises(NativeActionError):
                GameActions._disposition({**base, **patch}, allowed)
        for patch in ({'rule_matched':True}, {'rule_pending':True}, {'disposition_authorized':False}):
            with self.assertRaises(NativeActionError):
                GameActions._disposition(base, {**allowed, **patch})

    def test_user_authorized_upgrades_and_gems_are_not_extra_protection(self):
        GameActions._disposition({'is_equipment':True, 'modifiers_complete':True,
            'rarity':3, 'is_ancient':True, 'upgrade_level':10, 'socketed_gems':['Gem1']},
            {'disposition_authorized':True})

    def test_preexisting_cancellation_never_reads_game(self):
        event = threading.Event(); event.set()
        reader, dispatcher = Mock(), Mock()
        actions = GameActions(reader, dispatcher=dispatcher, allow_unverified=True)
        with self.assertRaisesRegex(NativeActionError, '已停止'):
            actions.execute('return_town', cancel_event=event)
        reader.check_game_files.assert_not_called()

    def test_validation_change_never_reads_game(self):
        reader, dispatcher = Mock(), Mock()
        actions = GameActions(reader, dispatcher=dispatcher, allow_unverified=True)
        with self.assertRaisesRegex(NativeActionError, '配置已经改变'):
            actions.execute('return_town', validate=lambda:False)
        reader.check_game_files.assert_not_called()

    def test_return_town_uses_accepted_button_path_during_completion_or_death(self):
        for completion,dead,expected in (({},False,'TownPortalSystem.frk'),
                ({'CombatMapCompletionUI':True},False,'TownPortalSystem.frl'),
                ({'MythicCompletionUI':True},False,'TownPortalSystem.frl'),
                ({},True,'DeathUI.return_to_town')):
            with self.subTest(completion=completion,dead=dead):
                reader,dispatcher,read = Mock(),Mock(),Mock()
                reader.class_name.return_value='Player';reader.module={'base':1000}
                read.singleton.return_value=3;read.ptr.return_value=4;read.klass.return_value=100;read.verify.return_value=5;read.guards=[]
                read.boolean.side_effect=lambda address:dead if address==4+296 else False
                dispatcher.invoke.return_value={}
                game=GameActions(reader,dispatcher=dispatcher,allow_unverified=True)
                with patch.object(game,'capabilities',return_value={'compatible':True}), \
                        patch.object(game,'_context',return_value=(read,1,2,0)), \
                        patch.object(game,'_completion_state',return_value=completion):
                    result=game.execute('return_town')
                calls=dispatcher.invoke.call_args.args[0]
                self.assertEqual(len(calls),1);self.assertEqual(calls[0].method,expected)
                self.assertEqual(calls[0].require_true,not dead)
                if dead: read.verify.assert_any_call(4,'DeathUI')
                self.assertTrue(result['transition_requested']);self.assertFalse(result['effect_confirmed'])

    def test_death_return_waits_for_loading_or_already_submitted_death_ui(self):
        for loading,missing,submitted in ((True,False,False),(False,True,False),(False,False,True)):
            with self.subTest(loading=loading,missing=missing,submitted=submitted):
                reader,dispatcher,read=Mock(),Mock(),Mock()
                reader.module={'base':1000};reader.class_name.return_value='Player'
                read.verify.return_value=5;read.singleton.return_value=8;read.klass.return_value=100
                game=GameActions(reader,dispatcher=dispatcher)
                pointer=1000+game.profile['singletons']['DeathUI']
                token=game.profile['uninitialized_type_tokens']['DeathUI']
                read.ptr.side_effect=lambda address:token if address==pointer and loading else 0 if address==100+184 and missing else 4
                read.boolean.side_effect=lambda address:True if address==4+296 else submitted
                with patch.object(game,'_require_travel_ready'),patch.object(game,'_completion_state',return_value={}):
                    with self.assertRaises(DeathRecoveryBusyError):game._return_town_call(read,1,2)
                dispatcher.invoke.assert_not_called()

    def test_rejected_return_town_never_claims_a_requested_transition(self):
        reader,dispatcher,read=Mock(),Mock(),Mock()
        reader.class_name.return_value='Player'
        read.singleton.return_value=3;read.ptr.return_value=4;read.klass.return_value=100;read.verify.return_value=5;read.guards=[]
        read.boolean.return_value=False
        dispatcher.invoke.side_effect=NativeActionError('游戏原生操作拒绝本次请求',state=REJECTED)
        game=GameActions(reader,dispatcher=dispatcher,allow_unverified=True)
        with patch.object(game,'capabilities',return_value={'compatible':True}), \
                patch.object(game,'_context',return_value=(read,1,2,0)), \
                patch.object(game,'_completion_state',return_value={'CombatMapCompletionUI':True}):
            with self.assertRaisesRegex(NativeActionError,'拒绝'):
                game.execute('return_town')
        self.assertEqual(dispatcher.invoke.call_count,1)

    def test_already_in_town_does_not_submit_return_again(self):
        reader,dispatcher,read=Mock(),Mock(),Mock()
        read.boolean.return_value=False;read.singleton.return_value=3
        game=GameActions(reader,dispatcher=dispatcher,allow_unverified=True)
        with patch.object(game,'capabilities',return_value={'compatible':True}), \
                patch.object(game,'_context',return_value=(read,1,2,1)):
            self.assertTrue(game.execute('return_town')['already_in_town'])
        dispatcher.invoke.assert_not_called()

    def test_mailbox_refuses_forward_result_and_signature_mismatch(self):
        profile = load_profile()
        params = (0x100000, 0x180000000, profile, 0x300000, 0x400000, 100)
        with self.assertRaises(ValueError):
            compile_mailbox(*params, [Invocation('Inventory.fcc', 1,
                (Argument.previous(0), Argument.integer(1), Argument.reference(0)))], [])
        with self.assertRaises(ValueError):
            compile_mailbox(*params, [Invocation('hm.ecc', 1, (Argument.integer(0),))], [])
        with self.assertRaises(ValueError):
            compile_mailbox(*params, [Invocation('TownPortalSystem.frj', 1, require_true=True)], [])

    def test_expanded_guard_table_still_enforces_record_and_allocation_bounds(self):
        params = (0x100000, 0x180000000, load_profile(), 0x300000, 0x400000, 100)
        calls = [Invocation('hm.ecc', arguments=(Argument.integer(0),))]
        with self.assertRaisesRegex(ValueError, '前置校验数据超出限制'):
            compile_mailbox(*params, calls, [(0x500000, bytes((MAX_GUARDS + 1) * 8))])
        with self.assertRaisesRegex(ValueError, '后台指令数据超出限制'):
            compile_mailbox(*params, [Invocation('hm.ecc', arguments=(Argument(raw=bytes(16384)),))], [])

    def test_game_exit_after_submission_never_allows_retry(self):
        for unreadable in (False,True):
            with self.subTest(unreadable=unreadable):
                reader=Mock(); reader.classes={'Inventory':0x10000}; reader.module={'base':0x180000000}
                dispatcher=MainThreadDispatcher(reader)
                dispatcher.install=Mock(); dispatcher._write=Mock()
                dispatcher.mailbox=0x20000; dispatcher.iat=0x30000; dispatcher.code=0x40000
                reader.memory.u64.return_value=dispatcher.code
                running=bytearray(192); struct.pack_into('<I',running,0,2)
                reader.memory.read.side_effect=[bytes(192),MemoryReadError('process exited') if unreadable else bytes(running)]
                reader.memory.is_running.return_value=False
                calls=[Invocation('hm.ecc',arguments=(Argument.integer(3),))]
                with self.assertRaises(NativeActionError) as caught:
                    dispatcher.invoke(calls)
                self.assertTrue(caught.exception.uncertain)
                self.assertTrue(dispatcher.tainted)
                written=dispatcher._write.call_count
                with self.assertRaisesRegex(NativeActionError,'上一条后台指令'):
                    dispatcher.invoke(calls)
                self.assertEqual(dispatcher._write.call_count,written)


@unittest.skipUnless(os.name == 'nt' and C.sizeof(C.c_void_p) == 8, 'Windows x64 ABI simulation')
class LocalMainThreadMailboxTests(unittest.TestCase):
    """The actual generated x64 mailbox executes against local fake callbacks.

    This allocates code in this test's own Python process only. It never uses
    OpenProcess, VirtualAllocEx, WriteProcessMemory, or a Deskrawl process.
    """
    def setUp(self):
        self.profile = load_profile()
        self.memory = C.create_string_buffer(16384)
        self.address = C.addressof(self.memory)
        self.keep, self.callbacks, self.methods, self.seen = [], [], {}, []
        self.handles, self.freed = {}, []
        self.handle_override = None
        self.return_values, self.peek_args = {}, []
        self.assembly = 0x180000000
        self.klasses, self.method_lists = {}, {}
        self.nested = False
        for key, method in self.profile['methods'].items():
            name = method.get('declaring_type', key.rsplit('.', 1)[0])
            if name not in self.klasses:
                buf = C.create_string_buffer(32); self.keep.append(buf)
                self.klasses[name] = C.addressof(buf)
                self.method_lists[C.addressof(buf)] = []
            buf = C.create_string_buffer(88); self.keep.append(buf)
            struct.pack_into('<Q', buf, 0, self.assembly+method['rva'])
            struct.pack_into('<I', buf, 72, int(method['token'],16))
            struct.pack_into('<B', buf, 82, len(method['parameters']))
            self.methods[C.addressof(buf)] = key
            self.method_lists[self.klasses[name]].append(C.addressof(buf))

        def callback(restype, args, function):
            value = C.WINFUNCTYPE(restype, *args)(function)
            self.callbacks.append(value)
            return C.cast(value, C.c_void_p).value

        def peek(a,b,c,d,e):
            self.peek_args.append((a,b,c,d,e))
            return 17

        def from_name(image, namespace, name):
            return self.klasses.get(C.string_at(name).decode('ascii'), 0)

        def methods(klass, iterator):
            index = C.c_uint64.from_address(iterator)
            values = self.method_lists.get(klass, [])
            found = values[index.value] if index.value < len(values) else 0
            index.value += 1
            return found

        def invoke(method, instance, params, exception):
            key = self.methods[method]
            count = len(self.profile['methods'][key]['parameters'])
            values = [C.c_uint64.from_address(params+n*8).value for n in range(count)]
            self.seen.append((key, instance, values))
            if self.nested:
                self.nested = False
                self.assertEqual(self.entry(1,2,3,4,5), 17)
            result = self.return_values.get(key, True)
            if result is None:
                return 0
            buf = C.create_string_buffer(24); self.keep.append(buf)
            buf[16] = bytes([bool(result)])
            return C.addressof(buf)

        def pin(obj, pinned):
            # Unity 6 uses pointer-sized GC handles. Use actual 64-bit local
            # addresses so the fake runtime detects truncated handles too.
            buf = C.create_string_buffer(8); self.keep.append(buf)
            handle = self.handle_override if self.handle_override is not None else C.addressof(buf)
            self.handles[handle] = obj
            return handle

        def free(handle):
            self.freed.append(handle)

        self.functions = {
            80:callback(W.BOOL, (C.c_void_p,C.c_void_p,W.UINT,W.UINT,W.UINT), peek),
            88:callback(C.c_void_p, (C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p), invoke),
            96:callback(C.c_void_p, (C.c_void_p,C.c_void_p), methods),
            104:callback(C.c_void_p, (C.c_void_p,C.c_void_p,C.c_void_p), from_name),
            112:callback(C.c_void_p, (), lambda:1),
            120:callback(C.c_void_p, (C.c_void_p,W.BOOL), pin),
            128:callback(None, (C.c_void_p,), free),
        }
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.kernel.VirtualAlloc.argtypes = (C.c_void_p, C.c_size_t, W.DWORD, W.DWORD)
        self.kernel.VirtualAlloc.restype = C.c_void_p
        self.kernel.VirtualProtect.argtypes = (C.c_void_p,C.c_size_t,W.DWORD,C.POINTER(W.DWORD))
        self.kernel.VirtualFree.argtypes = (C.c_void_p,C.c_size_t,W.DWORD)
        self.kernel.GetCurrentThreadId.restype = W.DWORD
        code = build_trampoline(self.address)
        self.code = self.kernel.VirtualAlloc(None,4096,0x3000,4)
        self.assertTrue(self.code)
        C.memmove(self.code, code, len(code))
        old = W.DWORD()
        self.assertTrue(self.kernel.VirtualProtect(self.code,4096,0x20,C.byref(old)))
        self.entry = C.WINFUNCTYPE(W.BOOL,C.c_void_p,C.c_void_p,W.UINT,W.UINT,W.UINT)(self.code)

    def tearDown(self):
        if getattr(self, 'code', 0):
            self.kernel.VirtualFree(self.code,0,0x8000)

    def run_command(self, calls, guards=(), cancelled=False, wrong_thread=False):
        tid = self.kernel.GetCurrentThreadId() + int(wrong_thread)
        raw = bytearray(compile_mailbox(self.address,self.assembly,self.profile,1,self.functions[80],tid,calls,guards))
        struct.pack_into('<IIQQ',raw,0,1,int(cancelled),0,1)
        for offset, address in self.functions.items():
            struct.pack_into('<Q',raw,offset,address)
        C.memmove(self.address,bytes(raw),len(raw))
        self.assertEqual(self.entry(1,2,3,4,5),17)
        self.assertEqual(self.peek_args[-1],(1,2,3,4,5))
        self.assertEqual(C.c_uint32.from_address(self.address+64).value,0)
        return C.c_uint32.from_address(self.address).value

    def test_guards_checked_before_any_game_call(self):
        guard = C.create_string_buffer(b'unchanged')
        status = self.run_command([Invocation('hm.ecc',arguments=(Argument.integer(3),))],
            [(C.addressof(guard),b'changed!')])
        self.assertEqual(status,GUARD_FAILED)
        self.assertFalse(self.seen)

    def test_full_bag_budget_checks_guards_beyond_record_256_before_any_call(self):
        contents = bytes((n % 251 for n in range(300 * 8)))
        guard = C.create_string_buffer(contents)
        address = C.addressof(guard)
        guards = [(address + n * 8, contents[n * 8:(n + 1) * 8]) for n in range(300)]
        calls = [Invocation('hm.ecc', arguments=(Argument.integer(3),))]
        self.assertEqual(self.run_command(calls, guards), DONE)
        self.assertEqual(len(self.seen), 1)
        self.seen.clear()
        guard[299 * 8] = bytes([contents[299 * 8] ^ 1])
        self.assertEqual(self.run_command(calls, guards), GUARD_FAILED)
        self.assertFalse(self.seen)

    def test_cancelled_and_wrong_thread_never_invoke(self):
        calls = [Invocation('hm.ecc',arguments=(Argument.integer(3),))]
        self.assertEqual(self.run_command(calls,cancelled=True),CANCELLED)
        self.assertFalse(self.seen)
        self.assertEqual(self.run_command(calls,wrong_thread=True),1)
        self.assertFalse(self.seen)

    def test_false_gate_stops_remaining_calls_and_frees_root(self):
        self.return_values['EquipmentManager.ddq'] = False
        calls = [Invocation('EquipmentManager.ddq',arguments=(Argument.reference(9),Argument.integer(0)),require_true=True),
            Invocation('hm.ecc',arguments=(Argument.integer(3),))]
        self.assertEqual(self.run_command(calls),REJECTED)
        self.assertEqual(len(self.seen),1)
        self.assertEqual(self.freed,list(self.handles))

    def test_false_is_allowed_for_duplicate_ring_gate(self):
        self.return_values['EquipmentManager.ddw'] = False
        calls = [Invocation('EquipmentManager.ddw',1,(Argument.reference(9),Argument.reference(10),Argument.integer(6)),require_false=True)]
        self.assertEqual(self.run_command(calls),DONE)
        self.assertEqual(self.freed,list(self.handles))

    def test_nested_message_pump_cannot_execute_twice(self):
        self.nested = True
        calls = [Invocation('hm.ecc',arguments=(Argument.integer(3),))]
        self.assertEqual(self.run_command(calls),DONE)
        self.assertEqual(len(self.seen),1)
        self.assertEqual(C.c_uint64.from_address(self.address+72).value,2)

    def test_batch_keeps_prior_object_results_for_reference_arguments(self):
        calls = [Invocation('EquipmentManager.deh',1,(Argument.integer(0),)),
            Invocation('EquipmentManager.ded',1,(Argument.reference(9),Argument.integer(0),Argument.reference(10))),
            Invocation('Inventory.fcc',2,(Argument.previous(1),Argument.integer(1),Argument.previous(0)),require_true=True)]
        self.assertEqual(self.run_command(calls),DONE)
        last = self.seen[-1][2]
        rooted=list(self.handles.values())
        self.assertEqual(last[0],rooted[1])
        self.assertEqual(last[2],rooted[0])
        self.assertEqual(C.c_int32.from_address(last[1]).value,1)
        self.assertEqual(self.freed,list(self.handles))

    def test_unity6_pointer_sized_gc_handles_survive_release(self):
        calls = [Invocation('TownPortalSystem.fri',1,(Argument.reference(9),),require_true=True)]
        self.assertEqual(self.run_command(calls),DONE)
        self.assertTrue(all(handle > 0xffffffff for handle in self.handles))
        self.assertEqual(self.freed,list(self.handles))

    def test_gc_handle_with_zero_low_bits_is_still_released(self):
        self.handle_override = 0x0000023400000000
        calls = [Invocation('hm.ecc',arguments=(Argument.integer(3),))]
        self.assertEqual(self.run_command(calls),DONE)
        self.assertEqual(self.freed,[self.handle_override])

    def test_missing_old_item_skips_return_without_losing_batch(self):
        self.return_values['EquipmentManager.ded'] = None
        calls = [Invocation('EquipmentManager.ded',1,(Argument.reference(9),Argument.integer(0),Argument.reference(10))),
            Invocation('Inventory.fcc',2,(Argument.previous(0),Argument.integer(1),Argument.reference(10)),skip_null_first=True,require_true=True)]
        self.assertEqual(self.run_command(calls),DONE)
        self.assertEqual(len(self.seen),1)


if __name__ == '__main__':
    unittest.main()
