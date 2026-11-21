"""Boss-death, delayed-loot and native-repeat races, with no game process."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions
from deskrawl_assistant.combat_rewards import CompletionRewards
from deskrawl_assistant.managed_mode import ManagedMode
from deskrawl_assistant.native_memory import GameBusyError, SnapshotChangedError
from tests import test_ground_pickup_receipts as receipts
from test_managed_mode import equipment, loot, observation, settings, verified


class CompletionRewardTests(unittest.TestCase):
    def setUp(self):
        cfg = settings()
        cfg['farming']['repeat_maps'] = [
            {'dungeon_id': 'a', 'difficulty': 'Normal', 'runs': 1},
            {'dungeon_id': 'b', 'difficulty': 'Nightmare', 'runs': 1}]
        self.world = observation()
        self.world['scene'].update(difficulty='Normal', run={
            'id': 'boss-run', 'dungeon_id': 'a', 'difficulty': 'Normal',
            'status': 'settling', 'completion_confirmed': True, 'finished': False})
        self.engine = ManagedMode(cfg)
        self.engine.start(self.world)

    def step(self, now):
        with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=now):
            return self.engine.next_action(self.world)

    def add_ground(self, *rows):
        self.world['ground']['items'] = list(rows)

    def test_last_win_counts_once_but_delayed_boss_drop_is_received_before_departure(self):
        self.assertIsNone(self.step(0))
        self.assertEqual(self.engine.target_dungeon, 'b')
        self.assertEqual(self.engine.repeat_index, 1)
        self.assertIsNone(self.step(30))  # Time alone cannot prove settlement.
        drop = equipment('boss-divine', container='ground', rarity=4)
        self.add_ground(drop)
        action = self.step(31)
        self.assertEqual((action['kind'], action['item']['item_uid']), ('collect_ground', 'boss-divine'))
        verified(self.engine, action)
        self.add_ground()
        self.world['items'] = [dict(drop, container='inventory')]
        self.world['counts']['inventory']['occupied'] = 1
        self.assertIsNone(self.step(32))
        self.world['scene']['completion_visible'] = {'CombatMapCompletionUI': True}
        self.assertIsNone(self.step(33))
        self.assertIsNone(self.step(33.5))
        action = self.step(34)
        self.assertEqual(action['kind'], 'return_town')
        self.assertTrue(action['protect_completion_rewards'])
        self.assertEqual(self.engine.handled_runs, {'boss-run'})
        self.assertEqual(self.engine.repeat_completed, 0)

    def test_native_repeat_new_plan_is_settlement_evidence_without_losing_old_reward_fence(self):
        self.assertIsNone(self.step(0))
        self.world['scene']['run'].update(id='native-next', status='running', completion_confirmed=False)
        self.add_ground(equipment('late', container='ground'))
        action = self.step(10)
        self.assertEqual(action['kind'], 'collect_ground')
        verified(self.engine, action)
        self.add_ground()
        self.assertIsNone(self.step(11))
        self.assertEqual(self.step(12)['kind'], 'return_town')
        self.assertEqual(self.engine.handled_runs, {'boss-run'})

    def test_win_during_online_save_is_not_lost_when_native_repeat_replaces_the_plan(self):
        self.world['scene']['online_saving'] = True
        self.assertIsNone(self.step(0))
        self.assertEqual(self.engine.target_dungeon, 'b')
        self.world['scene']['online_saving'] = False
        self.world['scene']['run'].update(id='native-next', status='running', completion_confirmed=False)
        self.add_ground(equipment('boss-during-save', container='ground'))
        action = self.step(20)
        self.assertEqual((action['kind'], action['item']['item_uid']), ('collect_ground', 'boss-during-save'))
        self.assertEqual(self.engine.handled_runs, {'boss-run'})

    def test_retained_terminal_and_popup_do_not_require_a_live_wave_plan(self):
        self.assertIsNone(self.step(0))
        self.world['scene']['run']['retained_terminal'] = True
        self.world['scene']['completion_visible'] = {'MythicCompletionUI': True}
        self.assertIsNone(self.step(5))
        self.world['scene']['completion_visible'] = {'MythicCompletionUI': False}
        self.assertEqual(self.step(6)['kind'], 'return_town')

    def test_late_spawn_and_incomplete_reads_restart_empty_confirmation(self):
        self.world['scene']['run'].update(status='success', finished=True)
        self.assertIsNone(self.step(0))
        self.world['ground']['complete'] = False
        self.assertIsNone(self.step(20))
        self.world['ground']['complete'] = True
        self.assertIsNone(self.step(21))
        self.world['complete'] = False
        self.assertIsNone(self.step(22))
        self.world['complete'] = True
        self.assertIsNone(self.step(23))
        drop = equipment('late', container='ground', issues=['identity pending'])
        self.add_ground(drop)
        self.assertIsNone(self.step(24))
        self.assertTrue(self.engine.running)
        self.add_ground()  # The game itself collected it.
        self.assertIsNone(self.step(25))
        self.assertEqual(self.step(26)['kind'], 'return_town')

    def test_full_bag_preserves_uncollected_loot_and_resumes_after_making_room(self):
        self.world['counts']['inventory']['occupied'] = 40
        self.add_ground(equipment('boss', container='ground'))
        self.assertIsNone(self.step(0))
        self.assertEqual(self.engine.phase, 'paused')
        self.assertIn('未切图', self.engine.status)
        self.world['counts']['inventory']['occupied'] = 39
        self.engine.resume(self.world)
        self.assertEqual(self.step(1)['kind'], 'collect_ground')

    def test_full_bag_still_collects_currency_and_keeps_chests_until_last(self):
        self.world['counts']['inventory']['occupied'] = 40
        self.add_ground(loot('chest', 'chest', 'TreasureChest3', container='ground'),
                        loot('gold', 'currency', 'GoldDrop', container='ground', item_type=1))
        action = self.step(0)
        self.assertEqual(action['item']['selection_id'], 'gold')
        verified(self.engine, action)
        self.world['counts']['inventory']['occupied'] = 38
        self.add_ground(loot('chest', 'chest', 'TreasureChest3', container='ground'),
                        equipment('gear', container='ground'))
        action = self.step(1)
        self.assertEqual(action['item']['selection_id'], 'gear')
        verified(self.engine, action)
        self.add_ground(loot('chest', 'chest', 'TreasureChest3', container='ground'))
        self.assertEqual(self.step(2)['kind'], 'collect_ground')  # Never open it here.

    def test_gold_receipt_continues_to_equipment_receipt_before_returning_town(self):
        gold = loot('boss-gold', 'currency', 'GoldDrop', container='ground', item_type=1, count=2312)
        gear = equipment('boss-gear', container='ground')
        self.add_ground(gear, gold)
        action = self.step(0)
        self.assertEqual(action['item']['selection_id'], 'boss-gold')
        game, selected = receipts.GoldReceiptTests().game()
        confirmed = receipts.PotionReceiptTests().confirm(game, selected)
        self.assertTrue(confirmed)
        self.engine.complete(action['action_id'], result={'verified':confirmed})

        self.add_ground(gear)
        action = self.step(1)
        self.assertEqual((action['kind'], action['item']['item_uid']), ('collect_ground', 'boss-gear'))
        selected = dict(gear, observation_id='gear-drop', item_type=3, _before_total_count=0)
        owned = dict(gear, container='inventory')
        game = receipts.review.GroundReceiptReviewTests.ground_game(False, [owned], [])
        confirmed = receipts.PotionReceiptTests().confirm(game, selected)
        self.assertTrue(confirmed)
        self.engine.complete(action['action_id'], result={'verified':confirmed})
        self.assertTrue(self.engine.running)

        self.add_ground()
        self.world['items'] = [owned]
        self.world['counts']['inventory']['occupied'] = 1
        self.assertIsNone(self.step(2))  # No departure before settlement.
        self.world['scene']['completion_visible'] = {'CombatMapCompletionUI':True}
        self.assertIsNone(self.step(3))
        action = self.step(4)
        self.assertEqual(action['kind'], 'return_town')
        self.assertTrue(action['protect_completion_rewards'])

    def test_full_bag_uses_configured_background_storage_before_pausing(self):
        from deskrawl_assistant.automation import DEFAULT_SETTINGS as AUTO
        config = deepcopy(AUTO)
        config['pressure'].update(enabled=True, threshold=4, target_free=10, pages=[1], equipment_filter='all')
        self.world['automation_settings'] = config
        self.world['counts']['inventory']['occupied'] = 40
        self.world['items'] = [equipment('bag-gear')]
        self.add_ground(equipment('boss', container='ground'))
        action = self.step(0)
        self.assertEqual((action['kind'], action['target']), ('transfer', 'storage'))
        self.assertTrue(self.engine.running)

    def test_explicit_finish_shares_the_reward_gate(self):
        self.engine.request_finish()
        self.assertIsNone(self.step(0))
        self.add_ground(equipment('boss', container='ground'))
        self.assertEqual(self.step(1)['kind'], 'collect_ground')

    def test_death_waits_for_native_revival_with_ground_rewards_retained(self):
        self.assertIsNone(self.step(0))
        self.world['scene']['dead'] = True
        self.add_ground(equipment('boss', container='ground'))
        self.assertIsNone(self.step(1))
        self.assertTrue(self.engine.running)
        self.assertIsNone(self.engine.pending)
        self.assertIn('复活', self.engine.status)
        self.world['scene']['dead'] = False
        self.assertEqual(self.step(2)['kind'], 'collect_ground')

    def test_partial_batch_remains_on_native_repeat_and_does_not_force_return(self):
        self.engine.repeat_maps[0]['runs'] = 2
        self.assertIsNone(self.step(0))
        self.assertEqual(self.engine.repeat_completed, 1)
        self.assertEqual(self.engine.target_dungeon, 'a')
        self.assertIsNone(self.step(10))


class PreparedCompletionRewardTests(unittest.TestCase):
    def world(self):
        world = observation()
        world['scene'].update(in_dungeon=True, dead=False, difficulty='Normal',
            run={'id':'boss','dungeon_id':'a','difficulty':'Normal','status':'settling',
                 'completion_confirmed':True,'final_kill_settled':True,'finished':False,
                 'enemy_count':0,'wave_active':False,'wave_index':5,'wave_count':5})
        return world

    def ready(self, rewards, world, now):
        with patch('deskrawl_assistant.combat_rewards.time.monotonic',return_value=now):
            return rewards.ready(world)

    def test_quiet_ground_during_final_kill_never_authorizes_departure_without_settlement(self):
        world = self.world()
        rewards = CompletionRewards()
        for now in (0,.2,2,30):
            self.assertFalse(self.ready(rewards,world,now))
        self.assertEqual(rewards.empty_since,0)
        self.assertFalse(rewards.settled)
        world['scene']['run'].update(status='success',finished=True)
        self.assertTrue(self.ready(rewards,world,30.05))

    def test_unknown_or_regressing_kill_evidence_cannot_prepare_empty_interval(self):
        changes = ({'final_kill_settled':False}, {'final_kill_settled':None},
                   {'completion_confirmed':False}, {'enemy_count':1}, {'enemy_count':None},
                   {'wave_active':True}, {'wave_active':None}, {'wave_index':4},
                   {'wave_count':0}, {'wave_count':None}, {'wave_count':True},
                   {'retained_terminal':True})
        for change in changes:
            with self.subTest(change=change):
                world = self.world()
                rewards = CompletionRewards()
                self.assertFalse(self.ready(rewards,world,0))
                world['scene']['run'].update(change)
                self.assertFalse(self.ready(rewards,world,5))
                self.assertIsNone(rewards.empty_since)

    def test_drop_scene_save_or_partial_read_resets_prepared_quiet_interval(self):
        for change in ('drop','partial','scene','ground','teleport','save','dead','unknown_dead'):
            with self.subTest(change=change):
                world = self.world()
                rewards = CompletionRewards()
                self.assertFalse(self.ready(rewards,world,0))
                changed = deepcopy(world)
                if change=='drop': changed['ground']['items']=[equipment('late',container='ground')]
                elif change=='partial': changed['complete']=False
                elif change=='scene': changed['scene']['available']=False
                elif change=='ground': changed['ground']['complete']=False
                else:
                    key = {'teleport':'teleporting','save':'online_saving','dead':'dead','unknown_dead':'dead'}[change]
                    changed['scene'][key] = None if change=='unknown_dead' else True
                self.assertFalse(self.ready(rewards,changed,10))
                self.assertIsNone(rewards.empty_since)
                self.assertFalse(self.ready(rewards,world,11))
                world['scene']['run'].update(status='success',finished=True)
                self.assertFalse(self.ready(rewards,world,11.9))
                self.assertTrue(self.ready(rewards,world,12))

    def test_another_run_cannot_inherit_prepared_interval(self):
        for change in ('id','location'):
            with self.subTest(change=change):
                world = self.world()
                rewards = CompletionRewards()
                self.assertFalse(self.ready(rewards,world,0))
                if change=='id': world['scene']['run']['id']='new'
                else:
                    world['scene']['dungeon_id']='b'
                    world['scene']['run']['dungeon_id']='b'
                world['scene']['run'].update(status='success',finished=True)
                self.assertFalse(self.ready(rewards,world,10))
                self.assertEqual(rewards.empty_since,10)
                self.assertTrue(self.ready(rewards,world,11))


class NativeSettledRewardTests(unittest.TestCase):
    world = PreparedCompletionRewardTests.world

    def settled(self, mythic=False):
        world = self.world()
        world['scene']['difficulty'] = difficulty = 'Mythic' if mythic else 'Normal'
        world['scene']['run'].update(difficulty=difficulty, status='success', finished=True,
            final_kill_settled=False, rewards_settled=True,
            reward_evidence='verified_WaveManager_finished_and_LootManager_idle')
        world['scene']['completion_visible'] = {('Mythic' if mythic else 'CombatMap')+'CompletionUI':True}
        return world

    def test_native_finished_clears_kill_flag_but_can_replay_on_first_empty_read(self):
        for mythic in (False, True):
            with self.subTest(mythic=mythic):
                world, rewards = self.settled(mythic), CompletionRewards()
                self.assertTrue(rewards.ready(world, native_replay=True))
                self.assertIsNone(rewards.empty_since)
                # The immediate proof is specific to original native replay;
                # returns/other travel retain the existing quiet barrier.
                self.assertFalse(rewards.ready(world))

    def test_incomplete_native_proof_or_late_drop_cannot_skip_quiet_barrier(self):
        for change in ('proof','source','unfinished','active','enemy','wave','unknown_wave',
                       'retained','popup','wrong_popup','dead','unknown_dead','drop','ground',
                       'partial','save','teleport','changing','unavailable'):
            with self.subTest(change=change):
                world, rewards = self.settled(), CompletionRewards()
                run = world['scene']['run']
                if change=='proof': run.pop('rewards_settled')
                elif change=='source': run['reward_evidence']='old_cached_proof'
                elif change=='unfinished': run['finished']=False
                elif change=='active': run['wave_active']=True
                elif change=='enemy': run['enemy_count']=1
                elif change=='wave': run['wave_index']=4
                elif change=='unknown_wave': run['wave_count']=None
                elif change=='retained': run['retained_terminal']=True
                elif change=='popup': world['scene']['completion_visible']={}
                elif change=='wrong_popup': world['scene']['completion_visible']={'MythicCompletionUI':True}
                elif change in ('dead','unknown_dead'): world['scene']['dead']=True if change=='dead' else None
                elif change=='drop': world['ground']['items']=[equipment('late',container='ground')]
                elif change=='ground': world['ground']['complete']=False
                elif change=='partial': world['complete']=False
                elif change=='unavailable': world['scene']['available']=False
                else: world['scene'][{'save':'online_saving','teleport':'teleporting','changing':'scene_changing'}[change]]=True
                self.assertFalse(rewards.ready(world, native_replay=True))


class NativeRewardGuardsTests(unittest.TestCase):
    def game(self):
        game = GameActions(Mock(), dispatcher=Mock(), allow_unverified=True)
        read = Mock(); read.boolean.return_value = False; read.guards = []
        read.singleton.return_value = 3
        game.capabilities = Mock(return_value={'compatible': True})
        game._context = Mock(return_value=(read, 1, 2, 0))
        return game

    def test_ground_spawn_at_native_submission_cancels_return_before_dispatch(self):
        game = self.game(); game._ground = Mock(return_value=[{'selection_id': 'late'}])
        with self.assertRaises(GameBusyError):
            game.execute('return_town', {'protect_completion_rewards': True})
        game.dispatcher.invoke.assert_not_called()

    def test_game_auto_pickup_before_our_call_is_a_prewrite_retry(self):
        game = self.game(); game._ground = Mock(return_value=[])
        with self.assertRaises(SnapshotChangedError):
            game.execute('collect_ground', {'selection_id': 'already-picked'})
        game.dispatcher.invoke.assert_not_called()
