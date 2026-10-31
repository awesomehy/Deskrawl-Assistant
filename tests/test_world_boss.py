"""World-boss availability, repeat quotas and native pre-dispatch gates."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions, _Read
from deskrawl_assistant.native_dispatcher import NativeActionError
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.managed_mode import ManagedMode, validate_settings
from deskrawl_assistant.models import ValidationError
import test_managed_service as service_fixture
from deskrawl_assistant.world_boss import PUMPKIN, entry_status, read_entry, target_blocked
from tests.test_completion_type_initialization import CompletionFixture
from tests.test_managed_mode import settings, verified, after_rewards
from tests.test_farming_cycles import scene
from tests.test_stargazing import plan


NOW = datetime(2026,10,30,8,59,59,tzinfo=timezone.utc).timestamp()
DAY = int((NOW-32400)//86400)


def options(remaining=1, **extra):
    return {'Normal':[{'map_id':PUMPKIN,'map_type':4,'can_enter':remaining>0,
        'world_boss':{'remaining':remaining,'daily_limit':100,'available':True},
        'entry_reason':'' if remaining else '今日入场次数已用尽。',**extra}]}


class EntryStateTests(unittest.TestCase):
    def test_quota_resets_at_native_utc_boundary_and_is_shared_between_difficulties(self):
        self.assertEqual(entry_status(DAY,100,now=NOW)['remaining'],0)
        reset=entry_status(DAY,100,now=NOW+1)
        self.assertEqual(reset['remaining'],100)
        self.assertEqual(reset['reset_at'],'2026-10-31T09:00:00+00:00')
        self.assertTrue(reset['can_enter'])

    def test_event_uses_fixed_pdt_and_excludes_end_minute(self):
        start,end='2026-10-21 13:00','2026-11-15 09:00'
        begin=datetime(2026,10,21,20,tzinfo=timezone.utc).timestamp()
        finish=datetime(2026,11,15,16,tzinfo=timezone.utc).timestamp()
        self.assertFalse(entry_status(DAY,0,start,end,now=begin-1)['open'])
        self.assertTrue(entry_status(DAY,0,start,end,now=begin)['open'])
        self.assertTrue(entry_status(DAY,0,start,end,now=finish-1)['open'])
        self.assertFalse(entry_status(DAY,0,start,end,now=finish)['open'])

    def test_bad_counter_or_window_cannot_authorize_entry(self):
        for day,used,start,end in ((DAY,-1,'',''),(DAY,101,'',''),(True,1,'',''),
                                  (DAY,0,'invalid',''),(DAY,0,'2026-11-15 09:00','')):
            with self.subTest(day=day,used=used,start=start),self.assertRaises(MemoryReadError):
                entry_status(day,used,start,end,now=NOW)

    def fixture(self, online):
        fixture=CompletionFixture()
        save=fixture.singleton('SaveSystem')
        obj=fixture.object('MapData')
        state=fixture.object('WorldBossEntryState')
        fixture.memory.put(obj+32,struct.pack('<i',4))
        fixture.memory.put(state+16,struct.pack('<qi',DAY,100))
        fixture.memory.put(save+32,struct.pack('<i',1 if online else 0))
        if online:
            cloud=fixture.singleton('CloudClient')
            fixture.memory.pointer(cloud+440,state)
            fixture.memory.put(cloud+280,struct.pack('<q',10_000_000))
        else:
            container=fixture.object('SaveContainer')
            fixture.memory.pointer(save+40,container)
            fixture.memory.pointer(container+48,state)
        return fixture,obj,state

    def test_verified_live_layout_uses_server_clock_online_and_local_save_offline(self):
        for online in (False,True):
            with self.subTest(online=online),patch('deskrawl_assistant.world_boss.time.time',return_value=NOW):
                f,obj,_=self.fixture(online)
                read=_Read(f.reader,f.profile)
                result=read_entry(read,obj);read.finish()
                self.assertEqual(result['remaining'],100 if online else 0)
                self.assertFalse(f.dispatcher.installed)

    def test_quota_changed_while_reading_is_rejected_before_dispatch(self):
        f,obj,state=self.fixture(False)
        read=_Read(f.reader,f.profile)
        with patch('deskrawl_assistant.world_boss.time.time',return_value=NOW):read_entry(read,obj)
        f.memory.put(state+24,struct.pack('<i',99))
        with self.assertRaises(SnapshotChangedError):read.finish()

    def test_availability_gate_does_not_apply_to_ordinary_or_mythic_maps(self):
        self.assertEqual(target_blocked({},'Volcano7','Normal'),'')
        self.assertEqual(target_blocked({},'MythicDungeon::mythic::1','Mythic'),'')
        self.assertTrue(target_blocked({},PUMPKIN,'Normal'))
        self.assertTrue(target_blocked({'maps_by_difficulty':options(0)},PUMPKIN,'Normal'))


class BossManagedTests(unittest.TestCase):
    def engine(self, *, remaining=1, farming=True, mode='repeat', town=False, stargazing=False):
        cfg=settings()
        cfg['pumpkin']={'enabled':True,'difficulty':'Normal'}
        cfg['farming'].update(enabled=farming, mode=mode, dungeon_id='a',difficulty='Normal',
            repeat_maps=[{'dungeon_id':'a','difficulty':'Normal','runs':2},
                         {'dungeon_id':'b','difficulty':'Normal','runs':3}])
        cfg['farming']['stargazing']['enabled']=stargazing
        world=scene(None if town else PUMPKIN, town=town)
        world['scene']['maps_by_difficulty']=options(remaining)
        engine=ManagedMode(cfg);engine.start(world)
        return engine,world

    def test_boss_success_and_failure_do_not_change_original_repeat_quotas(self):
        engine,world=self.engine(remaining=5)
        for run_id,status,count in [('loss','failed',0),('win1','success',1),('win2','success',2)]:
            world['scene']['run'].update(id=run_id,status=status)
            world['scene']['completion_visible'] = {'CombatMapCompletionUI': True}
            action=after_rewards(engine,world)
            self.assertEqual(action['kind'],'return_town' if status == 'failed' else 'enter_dungeon')
            verified(engine,action)
            self.assertEqual((engine.pumpkin_completed,engine.repeat_completed,engine.repeat_index),(count,0,0))
            if status == 'failed':
                town=scene(None,town=True);town['scene']['maps_by_difficulty']=options()
                entry=engine.next_action(town)
                self.assertEqual(entry['kind'],'enter_dungeon');verified(engine,entry)
                self.assertEqual(entry['dungeon_id'],PUMPKIN)
            world=scene(PUMPKIN,run_id=run_id+'-fresh')
            world['scene']['maps_by_difficulty']=options()

    def test_last_paid_run_finishes_then_resumes_original_target_after_cleanup(self):
        engine,world=self.engine()
        world['scene']['maps_by_difficulty']=options(0)
        self.assertIsNone(engine.next_action(world));self.assertTrue(engine.running)
        world['scene']['run'].update(id='last-win',status='success')
        action=after_rewards(engine,world)
        self.assertEqual(action['kind'],'return_town');verified(engine,action)
        town=scene(None,town=True);town['scene']['maps_by_difficulty']=options(0)
        entry=engine.next_action(town)
        self.assertEqual((entry['kind'],entry['dungeon_id']),('enter_dungeon','a'))
        self.assertTrue(engine.running);self.assertFalse(engine.pumpkin_active)
        self.assertEqual((engine.pumpkin_completed,engine.repeat_completed),(1,0))

    def test_boss_never_uses_original_stargazing_or_repeat_counter(self):
        engine,world=self.engine(remaining=5,stargazing=True)
        world['loot_plan']=plan(world)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.stargazing_state['verdict'],'idle')
        self.assertEqual(engine.repeat_completed,0)

    def test_starts_with_boss_before_original_maps_and_restores_partial_second_map(self):
        engine,town=self.engine(town=True)
        engine.repeat_index=1;engine.repeat_completed=2
        engine.target_dungeon='b'
        action=engine.next_action(town)
        self.assertEqual((action['kind'],action['dungeon_id']),('enter_dungeon',PUMPKIN))
        verified(engine,action)
        world=scene(PUMPKIN,status='success',run_id='boss-last');world['scene']['maps_by_difficulty']=options(0)
        action=after_rewards(engine,world);self.assertEqual(action['kind'],'return_town');verified(engine,action)
        town['scene']['maps_by_difficulty']=options(0)
        action=engine.next_action(town)
        self.assertEqual(action['dungeon_id'],'b')
        self.assertEqual((engine.repeat_index,engine.repeat_completed),(1,2))

    def test_push_target_is_preserved_by_boss_success_and_failure(self):
        for status in ('success','failed'):
            engine,world=self.engine(mode='push')
            world['scene']['maps_by_difficulty'] = options(0)
            world['scene']['run'].update(id='boss-'+status,status=status)
            action=after_rewards(engine,world);verified(engine,action)
            town=scene(None,town=True);town['scene']['maps_by_difficulty']=options(0)
            self.assertEqual(engine.next_action(town)['dungeon_id'],'a')
            self.assertFalse(engine.push_failed)

    def test_boss_only_waits_without_pausing_and_resumes_after_daily_reset(self):
        engine,town=self.engine(remaining=0,farming=False,town=True)
        self.assertIsNone(engine.next_action(town))
        self.assertTrue(engine.running);self.assertEqual(engine.phase,'waiting')
        self.assertIn('次数',engine.status)
        town['scene']['maps_by_difficulty']=options(100)
        self.assertEqual(engine.next_action(town)['dungeon_id'],PUMPKIN)

    def test_unknown_quota_waits_but_known_season_end_or_locked_boss_falls_back(self):
        engine,town=self.engine(town=True)
        town['scene']['maps_by_difficulty']={}
        self.assertIsNone(engine.next_action(town));self.assertTrue(engine.running)
        self.assertIn('未确认',engine.status)
        for extra in ({'unlocked':False}, {'world_boss':{'available':True,'open':False,'remaining':100}}):
            engine,town=self.engine(town=True)
            town['scene']['maps_by_difficulty']=options(1,**extra)
            self.assertEqual(engine.next_action(town)['dungeon_id'],'a')

    def test_ordinary_run_is_completed_before_daily_reset_priority_takes_effect(self):
        engine,town=self.engine(remaining=0,town=True)
        entry=engine.next_action(town);verified(engine,entry)
        world=scene('a',run_id='ordinary');world['scene']['maps_by_difficulty']=options(100)
        self.assertIsNone(engine.next_action(world))
        world['scene']['run']['status']='success'
        action=after_rewards(engine,world);verified(engine,action)
        town['scene']['maps_by_difficulty']=options(100)
        self.assertEqual(engine.next_action(town)['dungeon_id'],PUMPKIN)
        self.assertEqual(engine.repeat_completed,1)

    def test_boss_only_does_not_count_or_crash_on_an_existing_ordinary_run(self):
        cfg=settings(farm=False);cfg['farming'].update(dungeon_id=None,repeat_maps=[])
        cfg['pumpkin']={'enabled':True,'difficulty':'Normal'}
        world=scene('a');world['scene']['maps_by_difficulty']=options()
        engine=ManagedMode(cfg);engine.start(world)
        world['scene']['run'].update(id='ordinary-win',status='success')
        self.assertEqual(after_rewards(engine,world)['kind'],'return_town')
        self.assertEqual(engine.repeat_completed,0)

    def test_existing_paid_boss_run_finishes_before_switching_to_configured_difficulty(self):
        cfg=settings();cfg['pumpkin']={'enabled':True,'difficulty':'Normal'}
        world=scene(PUMPKIN,difficulty='Inferno')
        engine=ManagedMode(cfg);engine.start(world)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.target_difficulty,'Inferno')


class BossMigrationTests(unittest.TestCase):
    def test_old_boss_selection_moves_to_independent_switch_and_keeps_other_quota(self):
        cfg=settings();cfg.pop('pumpkin')
        cfg['farming'].update(dungeon_id=PUMPKIN,difficulty='Inferno',repeat_maps=[
            {'dungeon_id':PUMPKIN,'difficulty':'Inferno','runs':100},
            {'dungeon_id':'a','difficulty':'Normal','runs':300}])
        before=deepcopy(cfg);result=validate_settings(cfg)
        self.assertEqual(result['pumpkin'],{'enabled':True,'difficulty':'Inferno'})
        self.assertEqual(result['farming']['repeat_maps'],[{'dungeon_id':'a','difficulty':'Normal','runs':300}])
        self.assertEqual(result['farming']['dungeon_id'],'a')
        self.assertEqual(validate_settings(result),result);self.assertEqual(cfg,before)

    def test_legacy_boss_only_and_disabled_plans_do_not_require_an_original_map(self):
        for enabled in (True,False):
            cfg=settings();cfg.pop('pumpkin');cfg['farming'].update(enabled=enabled,dungeon_id=PUMPKIN,repeat_maps=[])
            result=validate_settings(cfg)
            self.assertEqual(result['pumpkin']['enabled'],enabled)
            self.assertFalse(result['farming']['enabled']);self.assertIsNone(result['farming']['dungeon_id'])
            self.assertEqual(result['farming']['repeat_maps'],[])

    def test_new_user_default_is_off_and_difficulty_is_limited(self):
        self.assertEqual(validate_settings(settings())['pumpkin'],{'enabled':False,'difficulty':'current'})
        for value in ({'enabled':'yes','difficulty':'Normal'},{'enabled':True,'difficulty':'Mythic'}):
            cfg=settings();cfg['pumpkin']=value
            with self.assertRaises(ValidationError):validate_settings(cfg)


class BossServiceTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp
    settings = service_fixture.ManagedServiceTests.settings

    def test_boss_only_can_start_without_original_map_and_preserves_saved_switch(self):
        self.client.items = []
        self.service._publish(self.client.snapshot())
        cfg = self.settings(); cfg['pumpkin'] = {'enabled':True, 'difficulty':'Normal'}
        self.service.save_managed(cfg)
        result = self.service.set_managed_running(True)
        self.assertTrue(result['running'])
        self.assertFalse(result['settings']['farming']['enabled'])
        self.service.managed.pause(); self.service._init_managed()
        self.assertEqual(self.service.managed.settings['pumpkin'], cfg['pumpkin'])

    def test_quota_exhausted_between_plan_and_dispatch_replans_without_native_call(self):
        self.client.items = []; self.service._publish(self.client.snapshot())
        cfg = self.settings(); cfg['pumpkin'] = {'enabled':True, 'difficulty':'Normal'}
        self.service.save_managed(cfg); self.service.set_managed_running(True)
        world = self.service._managed_observation()
        world['scene']['maps_by_difficulty'] = options(0)
        result = self.service._managed_execute({'kind':'enter_dungeon', 'dungeon_id':PUMPKIN, 'difficulty':'Normal'}, world)
        self.assertTrue(result['retryable']); self.assertFalse(result['verified'])
        self.assertEqual(self.client.actions, [])


class BossNativeEntryTests(unittest.TestCase):
    def test_fresh_quota_is_checked_before_original_portal_request(self):
        for allowed in (True,False):
            with self.subTest(allowed=allowed),ExitStack() as stack:
                read,reader,dispatcher=Mock(),Mock(),Mock()
                read.guards=[]
                read.singleton.return_value=3
                read.boolean.return_value=False
                dispatcher.invoke.return_value={}
                game=GameActions(reader,dispatcher=dispatcher,allow_unverified=True)
                control={'available':True,'difficulty':'Normal','maps':options()['Normal']}
                for name,value in (('capabilities',{'compatible':True}),('_context',(read,1,2,1)),
                    ('control_snapshot',control),('_map_objects',{PUMPKIN:1000})):
                    stack.enter_context(patch.object(game,name,return_value=value))
                stack.enter_context(patch.object(game,'_require_travel_ready'))
                stack.enter_context(patch.object(game,'_require_empty_town'))
                stack.enter_context(patch('deskrawl_assistant.world_boss.read_entry',return_value={
                    'can_enter':allowed,'reason':'' if allowed else '次数已用尽'}))
                if not allowed:
                    with self.assertRaisesRegex(NativeActionError,'次数'):
                        game.execute('enter_map',{'map_id':PUMPKIN,'difficulty':'Normal'})
                    dispatcher.invoke.assert_not_called()
                else:
                    game.execute('enter_map',{'map_id':PUMPKIN,'difficulty':'Normal'})
                    calls=dispatcher.invoke.call_args.args[0]
                    self.assertEqual(calls[0].method,'TownPortalSystem.fri')
                    self.assertTrue(calls[0].require_true)


class BossControlTests(unittest.TestCase):
    def control(self, boss_state=None, *, locked=False, failure=None):
        fixture = CompletionFixture()
        fixture.context()
        tiers = ['Normal', 'Nightmare', 'Inferno']
        progress = {'character_id':'fixture', 'difficulty':'Normal',
            'unlocked_maps':{tier:['Forest1'] + ([] if locked else [PUMPKIN]) for tier in tiers},
            'completed_maps':{tier:[] for tier in tiers}, 'entry_items':{}, 'is_demo':False,
            'mythic_min_difficulty':'Inferno', 'mythic_unlocked':[], 'mythic_completed':[]}
        maps = [{'map_id':PUMPKIN,'stage':'南瓜王','map_type':4,'prerequisite':'Volcano7'},
                {'map_id':'Forest1','stage':'森林 1','map_type':0}]
        with ExitStack() as stack:
            for name, value in (('capabilities',{'compatible':True}),
                ('_difficulty_state',('Normal',[{'id':tier,'value':i,'unlocked':True} for i,tier in enumerate(tiers)])),
                ('_map_objects',{PUMPKIN:1000,'Forest1':2000}), ('_ground',[]),
                ('_carriage',{'complete':True,'items':[]})):
                stack.enter_context(patch.object(fixture.game,name,return_value=value))
            for name in ('deskrawl_assistant.game_loot_ledger.read_loot_ledger',
                         'deskrawl_assistant.game_panel_log.read_panel_log',
                         'deskrawl_assistant.statistics_reader.read_gold_ledger'):
                stack.enter_context(patch(name,return_value={}))
            stack.enter_context(patch('deskrawl_assistant.recommendation_reader._catalog',return_value={'maps':maps}))
            stack.enter_context(patch('deskrawl_assistant.world_boss.read_entry',
                return_value=boss_state,side_effect=failure))
            result = fixture.game.control_snapshot({'available':True,'run':None,'profile':progress})
        self.assertTrue(result['available'],result['reason'])
        fixture.dispatcher.invoke.assert_not_called()
        return result

    def test_all_world_difficulties_share_quota_without_disabling_ordinary_maps(self):
        for used, locked in ((64,False),(100,False),(0,True)):
            with self.subTest(used=used,locked=locked):
                state = entry_status(DAY,used,now=NOW)
                control = self.control(state,locked=locked)
                for tier in ('Normal','Nightmare','Inferno'):
                    boss, ordinary = control['maps_by_difficulty'][tier]
                    self.assertEqual(boss['world_boss']['remaining'],100-used)
                    self.assertEqual(boss['can_enter'],used<100 and not locked)
                    self.assertTrue(ordinary['can_enter'])
                    if used==100:self.assertIn('次数',boss['entry_reason'])
                    if locked:self.assertIn('灰烬之冠',boss['entry_reason'])

    def test_unknown_boss_state_only_disables_boss(self):
        control = self.control(failure=MemoryReadError('世界首领入场次数未确认。'))
        for tier in ('Normal','Nightmare','Inferno'):
            boss, ordinary = control['maps_by_difficulty'][tier]
            self.assertFalse(boss['can_enter'])
            self.assertFalse(boss['world_boss']['available'])
            self.assertIn('未确认',boss['entry_reason'])
            self.assertTrue(ordinary['can_enter'])


if __name__=='__main__':unittest.main()
