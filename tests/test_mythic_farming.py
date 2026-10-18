"""Unlocked floor selection and guarded native plans; no live game calls."""
from contextlib import ExitStack
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions, normalise_run
from deskrawl_assistant.managed_mode import ManagedMode, validate_settings
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.mythic import floor_id, floor_number, unlocked_floors
from deskrawl_assistant.native_dispatcher import NativeActionError, compile_mailbox, load_profile
from deskrawl_assistant.native_memory import GameBusyError
import test_difficulty_progress as campaign
from test_farming_cycles import scene
from test_managed_mode import settings, verified, after_rewards


class MythicProgressTests(unittest.TestCase):
    def test_only_native_predecessor_completions_unlock_floors(self):
        completed={floor_id(1),floor_id(3),floor_id(100),'Forest1'}
        self.assertEqual(unlocked_floors(completed,eligible=False),[])
        self.assertEqual(unlocked_floors(completed,eligible=True),[1,2,4])
        for value in (0,101,True,1.0,'1'):
            with self.assertRaises(ValueError):floor_id(value)
        for value in ('MythicDungeon','MythicDungeon::mythic::0','MythicDungeon::mythic::01','MythicDungeon::mythic::101'):
            self.assertIsNone(floor_number(value))

    def test_campaign_uses_unlocked_world_difficulty_and_completed_floor_keys(self):
        copy=campaign.TownCampaignCopy()
        copy.completed=lambda save:[floor_id(1),floor_id(2),'Forest1']
        fixture=campaign.CampaignReadTests()
        self.assertEqual(fixture.read_fixture(copy)['profile']['mythic_unlocked'],[])
        copy.integers[0x7004]=2  # Inferno unlocked, current world still Normal.
        progress=fixture.read_fixture(copy)['profile']
        self.assertEqual(progress['mythic_unlocked'],[1,2,3])
        self.assertEqual(progress['mythic_completed'],[floor_id(1),floor_id(2)])
        self.assertNotIn(floor_id(1),progress['unlocked_maps']['Normal'])

    def test_run_identity_includes_floor_and_requires_matching_native_floor(self):
        copy=campaign.TownCampaignCopy()
        copy.reader.asset_name=lambda obj:'MythicDungeon' if obj==0xB000 else 'Ticket'
        copy.reader.string=lambda addr:{0x8000:'Offline',0xD000:'run-1',0xD100:'MythicDungeon',0xD200:'Normal'}[addr]
        copy.pointers.update({0x1000+120:0xA000,0x1000+112:0xB000,0x1000+104:0xC000,
            0xC000+88:0xA000,0xA000+16:0xD000,0xA000+24:0xD100,0xA000+48:0xD200,
            0xA000+88:0xF000,0x1000+192:0xF300})
        copy.integers.update({0xB000+32:1,0xA000+64:22,0xC000+304:22,0xC000+48:2,0xC000+152:1})
        copy.flags.update({0xC000+64:False,0xC000+248:True,0xC000+249:True,0xF200+296:True})
        fixture=campaign.CampaignReadTests();result=fixture.read_fixture(copy)
        self.assertTrue(result['available'],result['reason'])
        run=normalise_run(result['run'],{floor_id(22)})
        self.assertEqual((run['map_id'],run['difficulty'],run['status']),(floor_id(22),'Mythic','success'))
        copy.integers[0xC000+304]=23
        result=fixture.read_fixture(copy)
        self.assertFalse(result['available']);self.assertIn('层数',result['reason'])


class MythicEntryTests(unittest.TestCase):
    def test_native_plan_keeps_busy_unlock_and_material_gates_before_floor_entry(self):
        for case in ('ready','locked','materials','combat','busy','wrong_type','invalid_floor'):
            with self.subTest(case=case),ExitStack() as stack:
                read,reader,dispatcher=Mock(),Mock(),Mock()
                dispatcher.invoke.return_value={}
                read.guards=[];read.singleton.return_value=3
                read.boolean.side_effect=lambda addr:addr==261 and case=='busy'
                read.integer.return_value=0 if case=='wrong_type' else 1
                key=floor_id(22) if case!='invalid_floor' else 'MythicDungeon::mythic::101'
                state={'available':True,'difficulty':'Inferno','maps':[],
                    'maps_by_difficulty':{'Mythic':[] if case=='locked' else [{'map_id':key,'can_enter':case!='materials'}]}}
                game=GameActions(reader,dispatcher=dispatcher,allow_unverified=True)
                for name,value in (('capabilities',{'compatible':True}),('_context',(read,1,2,0 if case=='combat' else 1)),
                                   ('control_snapshot',state),('_map_objects',{'MythicDungeon':1000})):
                    stack.enter_context(patch.object(game,name,return_value=value))
                stack.enter_context(patch.object(game,'_require_travel_ready'))
                stack.enter_context(patch.object(game,'_require_empty_town'))
                if case!='ready':
                    with self.assertRaises((NativeActionError,GameBusyError)):
                        game.execute('enter_map',{'map_id':key,'difficulty':'Mythic'})
                    dispatcher.invoke.assert_not_called();continue
                game.execute('enter_map',{'map_id':key,'difficulty':'Mythic'})
                calls=dispatcher.invoke.call_args.args[0]
                self.assertEqual([c.method for c in calls],['GameManager.travel_busy','SaveSystem.mythic_unlocked',
                    'TownPortalSystem.entry_materials','GameManager.enter_mythic'])
                self.assertTrue(calls[0].require_false)
                self.assertTrue(calls[1].require_true);self.assertTrue(calls[2].require_true)
                self.assertEqual(calls[-1].arguments[0].value,1000)
                self.assertEqual(calls[-1].arguments[1].raw,struct.pack('<i',22))
                # The real mailbox compiler checks static/instance ABI and types.
                compile_mailbox(0x100000,0x180000000,load_profile(),0x300000,0x400000,100,calls,[])

    def test_snapshot_exposes_only_unlocked_floors_and_keeps_material_cost(self):
        from test_completion_type_initialization import CompletionFixture
        fixture=CompletionFixture();fixture.context()
        progress={'character_id':'fixture','difficulty':'Normal','unlocked_maps':{'Normal':[]},
                  'completed_maps':{'Normal':[]},'entry_items':{'Ticket':2},'is_demo':False,
                  'mythic_min_difficulty':'Inferno','mythic_unlocked':[1,2,22],'mythic_completed':[floor_id(1)]}
        maps=[{'map_id':'MythicDungeon','map_type':1,'stage':'神话裂隙',
               'requirements':[{'item_id':'Ticket','count':3,'difficulty_mask':4}]}]
        with ExitStack() as stack:
            game=fixture.game
            for name,value in (('capabilities',{'compatible':True}),
                    ('_difficulty_state',('Normal',[{'id':'Normal','value':0,'unlocked':True}])),
                    ('_map_objects',{'MythicDungeon':999}),('_ground',[]),
                    ('_carriage',{'complete':True,'items':[]})):
                stack.enter_context(patch.object(game,name,return_value=value))
            stack.enter_context(patch('deskrawl_assistant.recommendation_reader._catalog',return_value={'maps':maps}))
            stack.enter_context(patch('deskrawl_assistant.game_loot_ledger.read_loot_ledger',return_value={}))
            stack.enter_context(patch('deskrawl_assistant.game_panel_log.read_panel_log',return_value={}))
            stack.enter_context(patch('deskrawl_assistant.statistics_reader.read_gold_ledger',return_value={}))
            result=game.control_snapshot({'available':True,'run':None,'profile':progress})
        self.assertTrue(result['available'],result['reason'])
        floors=result['maps_by_difficulty']['Mythic']
        self.assertEqual([r['mythic_floor'] for r in floors],[1,2,22])
        self.assertFalse(any(r['can_enter'] for r in floors))
        self.assertEqual(result['mythic_entry_difficulty'],'Inferno')
        self.assertEqual(result['maps_by_difficulty']['Normal'],[])


class MythicControlReadTests(unittest.TestCase):
    """Exercise real profile/field verification after arriving inside a rift."""
    def fixture(self):
        from test_completion_type_initialization import CompletionFixture
        fixture = CompletionFixture()
        manager, _, _ = fixture.context()
        profile = json.loads((Path(__file__).parents[1]/'data/recommendation-types.json').read_text(encoding='utf-8'))
        fixture.reader.resolved_profiles = {'recommendation-types.json':profile}
        wave_type = next(row for row in profile['types'] if row.get('logicalName',row['name'])=='WaveManager')
        # Build the synthetic native Class from the read schema, while leaving
        # the real action schema without WaveManager, as in the reported crash.
        fixture.profile['types']['WaveManager'] = wave_type
        wave = fixture.object('WaveManager',336)
        del fixture.profile['types']['WaveManager']
        active = fixture.object('MapData')
        fixture.memory.pointer(manager+104,wave)
        fixture.memory.pointer(manager+112,active)
        fixture.memory.put(active+32,struct.pack('<i',1))
        fixture.memory.put(wave+304,struct.pack('<i',9))
        fixture.reader.asset_name = lambda obj:'MythicDungeon' if obj==active else ''
        fixture.recommendation = {'available':True,'run':{
            'id':'floor-run-9','map_id':floor_id(9),'difficulty':'Mythic',
            'world_difficulty':'Inferno','wave_count':12,'wave_index':1,
            'wave_active':True,'enemy_count':1,'finished':False,'final_kill_settled':False},
            'profile':{'character_id':'fixture','difficulty':'Inferno',
                'unlocked_maps':{'Inferno':[]},'completed_maps':{'Inferno':[]},
                'entry_items':{},'is_demo':False,'mythic_min_difficulty':'Inferno',
                'mythic_unlocked':[9],'mythic_completed':[]}}
        return fixture,wave

    def control(self,fixture):
        with ExitStack() as stack:
            for name,value in (('capabilities',{'compatible':True}),
                    ('_difficulty_state',('Inferno',[{'id':'Inferno','value':2,'unlocked':True}])),
                    ('_map_objects',{'MythicDungeon':fixture.objects['MapData']}),
                    ('_ground',[]),('_carriage',{'complete':True,'items':[]})):
                stack.enter_context(patch.object(fixture.game,name,return_value=value))
            stack.enter_context(patch('deskrawl_assistant.recommendation_reader._catalog',return_value={
                'maps':[{'map_id':'MythicDungeon','map_type':1,'requirements':[]}]}))
            stack.enter_context(patch('deskrawl_assistant.game_loot_ledger.read_loot_ledger',return_value={}))
            stack.enter_context(patch('deskrawl_assistant.game_panel_log.read_panel_log',return_value={}))
            stack.enter_context(patch('deskrawl_assistant.statistics_reader.read_gold_ledger',return_value={}))
            return fixture.game.control_snapshot(fixture.recommendation)

    def test_arrival_uses_verified_read_schema_and_returns_floor_and_running_run(self):
        fixture,_ = self.fixture()
        result = self.control(fixture)
        self.assertTrue(result['available'],result['reason'])
        self.assertEqual((result['scene'],result['map_id'],result['difficulty']),
                         ('combat',floor_id(9),'Mythic'))
        self.assertEqual(result['world_difficulty'],'Inferno')
        self.assertEqual(result['run']['status'],'running')
        self.assertEqual(result['run']['dungeon_id'],result['map_id'])
        fixture.dispatcher.invoke.assert_not_called()

    def test_wave_field_mismatch_still_rejects_the_snapshot(self):
        fixture,_ = self.fixture()
        fixture.memory.put(fixture.tables['WaveManager']+24,struct.pack('<i',999))
        result = self.control(fixture)
        self.assertFalse(result['available'])
        self.assertIn('WaveManager 的推荐字段',result['reason'])
        fixture.dispatcher.invoke.assert_not_called()

    def test_floor_changing_during_read_is_not_reported_as_confirmed_arrival(self):
        fixture,wave = self.fixture()
        def change_floor(address,size,raw):
            if address==wave+304:
                fixture.memory.after_read=None
                fixture.memory.put(wave+304,struct.pack('<i',10))
        fixture.memory.after_read=change_floor
        result = self.control(fixture)
        self.assertFalse(result['available'])
        self.assertIn('正在变化',result['reason'])
        fixture.dispatcher.invoke.assert_not_called()


class MythicCycleTests(unittest.TestCase):
    def config(self):
        cfg=settings();cfg['farming']['repeat_maps']=[{'dungeon_id':floor_id(22),'difficulty':'Mythic','runs':2},
                                                     {'dungeon_id':'a','difficulty':'Normal','runs':1}]
        return cfg

    def town(self,world='Inferno'):
        value=scene(None,world,town=True)
        value['scene'].update(world_difficulty=world,mythic_entry_difficulty='Inferno')
        return value

    def test_mixed_cycle_switches_real_world_tier_and_counts_success_only(self):
        engine=ManagedMode(self.config());engine.start(self.town('Normal'))
        action=engine.next_action(self.town('Normal'))
        self.assertEqual((action['kind'],action['difficulty']),('switch_difficulty','Inferno'));verified(engine,action)
        action=engine.next_action(self.town());self.assertEqual(action['difficulty'],'Mythic');verified(engine,action)
        for n,status in enumerate(('failed','success','success')):
            running=scene(floor_id(22),'Mythic',run_id=f'floor-run-{n}')
            running['scene']['world_difficulty']='Inferno'
            self.assertIsNone(engine.next_action(running))
            world=scene(floor_id(22),'Mythic',status,f'floor-run-{n}')
            world['scene']['world_difficulty']='Inferno'
            action=after_rewards(engine,world);self.assertEqual(action['kind'],'return_town');verified(engine,action)
            self.assertEqual(engine.repeat_completed,1 if n==1 else 0)
            town=self.town();action=engine.next_action(town)
            if n==2:
                self.assertEqual((action['kind'],action['difficulty']),('switch_difficulty','Normal'))
                verified(engine,action);town=self.town('Normal');action=engine.next_action(town)
            self.assertEqual(action['kind'],'enter_dungeon');verified(engine,action)
        self.assertEqual((engine.target_dungeon,engine.target_difficulty),('a','Normal'))

    def test_current_normal_target_started_inside_rift_uses_world_tier(self):
        cfg=settings();world=scene(floor_id(22),'Mythic');world['scene']['world_difficulty']='Inferno'
        engine=ManagedMode(cfg);engine.start(world)
        self.assertEqual(engine.target_difficulty,'Inferno')

    def test_invalid_floor_and_type_pair_cannot_be_saved(self):
        for key,difficulty in ((floor_id(1),'Normal'),('MythicDungeon','Mythic'),('MythicDungeon::mythic::101','Mythic')):
            cfg=self.config();cfg['farming']['repeat_maps'][0].update(dungeon_id=key,difficulty=difficulty)
            with self.assertRaises(ValidationError):validate_settings(cfg)


if __name__=='__main__':unittest.main()
