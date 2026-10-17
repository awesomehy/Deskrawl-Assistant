"""Managed-mode policy tests run without a game, runtime client or native calls."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from deskrawl_assistant.managed_mode import (DEFAULT_SETTINGS, ManagedMode, ManagedMetrics,
    validate_settings, equipment_bucket, equip_candidate_allowed)
from deskrawl_assistant.models import ValidationError


def equipment(uid, slot=0, container='inventory', **extra):
    row = dict(item_uid=uid, selection_id=uid, name_key='LegendaryNeck1', internal_name='LegendaryNeck1',
                container=container, slot_index=slot, kind='equipment', is_equipment=True,
                locked=False, movable=True, matches=False, review=False, count=1, rarity=3,
                is_ancient=False, is_black_mist=False)
    row.update(item_level=1, equip_slot=8, base_rarity=3)
    row.update(extra)
    return row


def loot(uid, kind, name, slot=0, container='inventory', count=1, **extra):
    return dict(selection_id=uid, internal_name=name, container=container, slot_index=slot,
                kind=kind, locked=False, movable=True, count=count, **extra)


def observation(items=(), town=False, capacity=40, allowed=range(150), **extra):
    values = deepcopy(list(items))
    return dict(complete=True, session='same-player', items=values,
                counts={'inventory': {'capacity': capacity, 'occupied': sum(r['container'] == 'inventory' for r in values)}},
                scene={'available': True, 'in_town': town, 'dungeon_id': 'a', 'run': {'status': 'running', 'id': 'run1'}},
                character={'available': True, 'level':1, 'max_level':100, 'equipment': []}, storage={'page_size': 50, 'allowed_indices': list(allowed)},
                carriage={'available': True, 'complete': True, 'items': []},
                ground={'available': True, 'complete': True, 'items': []}, **extra)


def settings(equip=False, farm=True, **cleanup):
    s = deepcopy(DEFAULT_SETTINGS)
    # Shared-page fixtures intentionally model the old explicitly saved layout.
    s['cleanup'].update(equipment_pages=[1], rune_pages=[1], gem_pages=[1])
    s['auto_equip']['enabled'] = equip
    s['farming'].update(enabled=farm, dungeon_id='a', progression=['a', 'b', 'c'])
    s['cleanup'].update(cleanup)
    return s


def verified(engine, action):
    return engine.complete(action['action_id'], result={'verified': True})


def after_rewards(engine, world):
    """Advance two empty, settled samples without a real-time sleep."""
    with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=100):
        first = engine.next_action(world)
    if first is not None:
        return first
    with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=102):
        return engine.next_action(world)


class BatchCleanupTests(unittest.TestCase):
    def test_full_batches_are_processed_before_refilling(self):
        engine = ManagedMode(settings(equipment_action='sell'))
        engine.settings['farming']['free_threshold'] = 1
        bag = []
        ground = [equipment(str(n), container='ground') for n in range(7)]
        world = observation([], town=True, capacity=3)
        world['ground']['items'] = deepcopy(ground)
        engine.start(world)
        kinds = []
        for _ in range(20):
            world = observation(bag, town=True, capacity=3)
            world['ground']['items'] = deepcopy(ground)
            action = engine.next_action(world)
            self.assertIsNotNone(action)
            kinds.append(action['kind'])
            if action['kind'] == 'enter_dungeon': break
            uid = action['item']['item_uid']
            if action['kind'] == 'collect_ground':
                row = next(r for r in ground if r['item_uid'] == uid)
                ground.remove(row)
                row.update(container='inventory', slot_index=next(n for n in range(3)
                    if n not in {r['slot_index'] for r in bag}))
                bag.append(row)
            else:
                self.assertEqual(action['kind'], 'sell')
                bag = [r for r in bag if r['item_uid'] != uid]
            verified(engine, action)
        self.assertEqual(kinds, ['collect_ground'] * 3 + ['sell'] * 3
            + ['collect_ground'] * 3 + ['sell'] * 3
            + ['collect_ground', 'sell', 'enter_dungeon'])

    def test_pickups_continue_before_pending_affix_review_then_all_locks_precede_sale(self):
        engine = ManagedMode(settings(equipment_action='sell'))
        keep = equipment('keep', matches=True)
        junk = equipment('junk', 1)
        ground = equipment('ground', matches=True)
        world = observation([], town=True)
        world['carriage']['items'] = [keep, junk]
        world['ground']['items'] = [ground]
        engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_carriage'); verified(engine, action)
        keep['review'] = True
        world = observation([keep], town=True)
        world['carriage']['items'] = [junk]; world['ground']['items'] = [ground]
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_carriage'); verified(engine, action)
        world = observation([keep, junk], town=True)
        world['ground']['items'] = [ground]
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_ground'); verified(engine, action)
        ground['slot_index'] = 2
        world = observation([keep, junk, ground], town=True)
        self.assertIsNone(engine.next_action(world))
        self.assertIn('词条', engine.status)
        keep['review'] = False
        for row in (keep, ground):
            action = engine.next_action(observation([keep, junk, ground], town=True))
            self.assertEqual(action['kind'], 'lock')
            self.assertEqual(action['item']['item_uid'], row['item_uid'])
            verified(engine, action); row['locked'] = True
        stored = equipment('stored', 0, 'storage', locked=True)
        action = engine.next_action(observation([keep, junk, ground, stored], town=True, capacity=3, allowed=[0]))
        self.assertEqual(action['kind'], 'sell')
        self.assertEqual(action['item']['item_uid'], 'junk')

    def test_all_external_rewards_precede_bag_chests_and_non_chests_collect_first(self):
        engine = ManagedMode(settings(equipment_action='sell', gem_action='sell'))
        first = loot('first', 'chest', 'TreasureChest1')
        second = loot('second', 'chest', 'TreasureChest2', 1)
        carriage_chest = loot('carriage-chest', 'chest', 'TreasureChest1', 2)
        gear = equipment('ground', 3)
        world = observation([first], town=True)
        world['carriage']['items'] = [carriage_chest]
        world['ground']['items'] = [second, gear]
        engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_ground')
        self.assertEqual(action['item']['item_uid'], 'ground'); verified(engine, action)
        world = observation([first, gear], town=True)
        world['carriage']['items'] = [carriage_chest]; world['ground']['items'] = [second]
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_carriage'); verified(engine, action)
        world = observation([first, gear, carriage_chest], town=True)
        world['ground']['items'] = [second]
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_ground'); verified(engine, action)
        world = observation([first, second, carriage_chest, gear], town=True)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'sell'); verified(engine, action)
        world = observation([first, second, carriage_chest], town=True)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'open_chest'); verified(engine, action)
        # Opening produces a fresh outside batch before any subsequent chest.
        gem = loot('reward-gem', 'gem', 'Gem1', 3)
        world = observation([second, carriage_chest], town=True)
        world['ground']['items'] = [gem]
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_ground'); verified(engine, action)
        world = observation([second, carriage_chest, gem], town=True)
        action = engine.next_action(world)
        self.assertEqual((action['kind'], action['target']), ('transfer', 'storage'))
        self.assertEqual(action['item']['internal_name'], 'Gem1')
        verified(engine, action)
        gem.update(container='storage', slot_index=0)
        next_action = engine.next_action(observation([second, carriage_chest, gem], town=True))
        self.assertEqual(next_action['kind'], 'open_chest')
        gem_stat = engine.state()['statistics']['gems'][0]
        self.assertEqual((gem_stat['stored'], gem_stat['sold']), (1, 0))

    def test_unfinished_offload_cannot_open_chests_or_finish_collection(self):
        engine = ManagedMode(settings())
        chest = loot('chest', 'chest', 'TreasureChest1')
        world = observation([chest], town=True)
        world['carriage']['complete'] = False
        engine.start(world)
        self.assertIsNone(engine.next_action(world))
        self.assertTrue(engine.running)
        engine.request_finish()
        self.assertIsNone(engine.next_action(world))
        self.assertTrue(engine.running)
        self.assertEqual(engine.finish_empty_reads, 0)


class SettingsTests(unittest.TestCase):
    def test_excluded_slots_migrate_and_roundtrip_without_changing_legendary_locks(self):
        raw = settings(equip=True)
        raw['auto_equip'].pop('disabled_slots')
        raw['auto_equip']['slot_locks'] = {'6':'LegendaryRing1'}
        before = deepcopy(raw)
        self.assertEqual(validate_settings(raw)['auto_equip']['disabled_slots'],[])
        self.assertEqual(raw,before)
        raw['auto_equip']['disabled_slots'] = [8,6]
        actual = validate_settings(raw)
        self.assertEqual(actual['auto_equip']['disabled_slots'],[6,8])
        self.assertEqual(actual['auto_equip']['slot_locks'],{'6':'LegendaryRing1'})
        self.assertEqual(validate_settings(actual),actual)
        raw['auto_equip']['disabled_slots'].clear()
        self.assertEqual(actual['auto_equip']['disabled_slots'],[6,8])
        for value in ([True],[12],[-1],['6'],[6,6],None,{}):
            with self.subTest(value=value):
                raw['auto_equip']['disabled_slots'] = value
                with self.assertRaises(ValidationError): validate_settings(raw)

    def test_old_difficulty_settings_migrate_in_memory_without_mutation(self):
        raw = settings(); raw['farming'].pop('difficulty')
        before = deepcopy(raw)
        self.assertEqual(validate_settings(raw)['farming']['difficulty'],'current')
        self.assertEqual(raw,before)
        raw['farming']['difficulty']='Hell'
        with self.assertRaises(ValidationError): validate_settings(raw)

    def test_preferences_roundtrip_and_no_mutable_aliases(self):
        raw = settings(equip=True)
        raw['auto_equip']['slot_locks'] = {'8': 'LegendaryNeck1', '6': 'LegendaryRing1'}
        validated = validate_settings(raw, equipment_keys={'LegendaryNeck1', 'LegendaryRing1'}, available_pages={1, 2, 5})
        raw['cleanup']['equipment_pages'].append(2)
        self.assertEqual(validated['cleanup']['equipment_pages'], [1])

    def test_reject_unknown_actions_bad_locks_or_empty_protected_pages(self):
        for group, field, value in [('cleanup', 'equipment_fallback', 'store'), ('cleanup', 'equipment_pages', []),
                                     ('farming', 'free_threshold', True), ('auto_equip', 'slot_locks', {'12': 'x'}),
                                     ('auto_equip', 'minimum_delta', float('nan'))]:
            with self.subTest(field=field):
                raw = settings(); raw[group][field] = value
                with self.assertRaises(ValidationError): validate_settings(raw)

    def test_page_numbers_do_not_assume_five_pages(self):
        raw = settings()
        for name in ('equipment_pages', 'rune_pages', 'gem_pages', 'gold_bar_pages', 'legendary_rein_pages'):
            raw['cleanup'][name] = [8]
        validate_settings(raw, available_pages={8, 10})
        with self.assertRaisesRegex(ValidationError, '已解锁'):
            validate_settings(raw, available_pages={1, 2})
        self.assertEqual(validate_settings(raw)['cleanup']['rune_pages'], [8])


class EquipTests(unittest.TestCase):
    def test_excluded_slot_blocks_every_quality_even_matching_named_upgrade_and_storage_preparation(self):
        for rarity in range(5):
            for container in ('inventory','storage'):
                with self.subTest(rarity=rarity,container=container):
                    s = settings(equip=True,farm=False)
                    s['auto_equip'].update(disabled_slots=[8],slot_locks={'8':'LegendaryNeck1'})
                    row = equipment('upgrade',rarity=rarity,container=container,
                        power={'available':True,'target':8,'delta':10000})
                    engine = ManagedMode(s); world = observation([row]); engine.start(world)
                    self.assertIsNone(engine.next_action(world))

    def test_ring_exclusion_is_independent_and_reenabled_slot_competes_again(self):
        s = settings(equip=True,farm=False); s['auto_equip']['disabled_slots'] = [6]
        ring = equipment('ring',power={'available':True,'comparisons':[
            {'target':6,'delta':100},{'target':7,'delta':25}]})
        world = observation([ring]); engine = ManagedMode(s); engine.start(world)
        self.assertEqual(engine.next_action(world)['target_slot'],7)
        s['auto_equip']['disabled_slots'] = [6,7]
        engine = ManagedMode(s); engine.start(world)
        self.assertIsNone(engine.next_action(world))
        s['auto_equip']['disabled_slots'] = []
        engine = ManagedMode(s); engine.start(world)
        self.assertEqual(engine.next_action(world)['target_slot'],6)

    def test_unrestricted_slot_uses_gain_across_every_non_divine_equipment_quality(self):
        for rarity in range(4):
            with self.subTest(rarity=rarity):
                row = equipment('upgrade', rarity=rarity, name_key='OtherItem', matches=False,
                                rule_status='no_rule', rules_known=True,
                                power={'available': True, 'target': 8, 'delta': 30})
                worse = equipment('legendary', 1, power={'available': True, 'target': 8, 'delta': 10})
                engine = ManagedMode(settings(equip=True, farm=False)); world = observation([row, worse]); engine.start(world)
                action = engine.next_action(world)
                self.assertEqual(action['kind'], 'equip'); self.assertEqual(action['item']['item_uid'], 'upgrade')

    def test_named_legendary_allows_early_game_upgrades_in_empty_or_other_slot(self):
        for rarity in (0, 1, 2):
            for current_key in (None, 'CommonNeck1', 'LegendaryNeck2'):
                with self.subTest(rarity=rarity, current_key=current_key):
                    s = settings(equip=True, farm=False); s['auto_equip']['slot_locks'] = {'8': 'LegendaryNeck1'}
                    uid = 'worn' if current_key else None
                    row = equipment('upgrade', rarity=rarity, name_key='EarlyNeck',
                        power={'available': True, 'target': 8, 'delta': 20, 'replaced_uid': uid})
                    world = observation([row])
                    if current_key:
                        world['character']['equipment'] = [equipment(uid, name_key=current_key, equipped_slot=8)]
                    engine = ManagedMode(s); engine.start(world)
                    action = engine.next_action(world)
                    self.assertEqual(action['kind'], 'equip'); self.assertIn('过渡装备', action['reason'])

    def test_wearing_named_legendary_excludes_all_other_names_and_lower_qualities(self):
        s = settings(equip=True, farm=False); s['auto_equip']['slot_locks'] = {'8': 'LegendaryNeck1'}
        for rarity in range(5):
            with self.subTest(rarity=rarity):
                row = equipment('other', rarity=rarity, name_key='OtherNeck',
                    power={'available': True, 'target': 8, 'delta': 10000, 'replaced_uid': 'worn'})
                world = observation([row]); world['character']['equipment'] = [equipment('worn', equipped_slot=8)]
                engine = ManagedMode(s); engine.start(world)
                self.assertIsNone(engine.next_action(world))
                wanted = equipment('wanted', 1, is_ancient=True,
                    power={'available': True, 'target': 8, 'delta': 5, 'replaced_uid': 'worn'})
                world['items'].append(wanted)
                self.assertEqual(engine.next_action(world)['item']['item_uid'], 'wanted')

    def test_each_ring_independently_stops_transitional_upgrades_after_target_is_worn(self):
        s = settings(equip=True, farm=False); s['auto_equip']['slot_locks'] = {'6': 'LegendaryRing1', '7': 'LegendaryRing2'}
        ring = equipment('ring', rarity=2, name_key='RareRing1', power={'available': True,
            'comparisons': [{'target': 6, 'delta': 100, 'replaced_uid': 'worn1'},
                            {'target': 7, 'delta': 25, 'replaced_uid': 'worn2'}]})
        world = observation([ring]); world['character']['equipment'] = [
            equipment('worn1', name_key='LegendaryRing1', equipped_slot=6),
            equipment('worn2', name_key='CommonRing1', equipped_slot=7)]
        engine = ManagedMode(s); engine.start(world)
        self.assertEqual(engine.next_action(world)['target_slot'], 7)

    def test_transitional_upgrade_needs_confirmed_worn_identity_and_positive_gain(self):
        s = settings(equip=True, farm=False); s['auto_equip']['slot_locks'] = {'8': 'LegendaryNeck1'}
        good = {'available': True, 'equipment': [equipment('worn', name_key='CommonNeck1', equipped_slot=8)]}
        invalid = [{}, {'available': False, 'equipment': []}, {'available': True},
            {'available': True, 'equipment': [equipment('worn', name_key=None, equipped_slot=8)]},
            {'available': True, 'equipment': good['equipment'] * 2},
            {'available': True, 'equipment': [equipment('other_uid', name_key='CommonNeck1', equipped_slot=8)]}]
        for character in invalid:
            with self.subTest(character=character):
                row = equipment('upgrade', rarity=0, name_key='CommonNeck2',
                    power={'available': True, 'target': 8, 'delta': 10, 'replaced_uid': 'worn'})
                world = observation([row]); world['character'] = character
                engine = ManagedMode(s); engine.start(world)
                self.assertIsNone(engine.next_action(world))
        self.assertFalse(equip_candidate_allowed(equipment('u', rarity=0, name_key='CommonNeck1'),
            {'target':8,'delta':10,'replaced_uid':None},s['auto_equip'],None))
        for rarity, delta in ((None, 10), (True, 10), (2, 0), (2, -1), (2, float('nan'))):
            with self.subTest(rarity=rarity, delta=delta):
                row = equipment('upgrade', rarity=rarity, name_key='CommonNeck2',
                    power={'available': True, 'target': 8, 'delta': delta, 'replaced_uid': 'worn'})
                world = observation([row]); world['character'] = good
                engine = ManagedMode(s); engine.start(world)
                self.assertIsNone(engine.next_action(world))

    def test_storage_transitional_upgrade_is_equipped_before_disposal(self):
        s = settings(equip=True, farm=True, equipment_action='salvage')
        s['auto_equip']['slot_locks'] = {'8': 'LegendaryNeck1'}
        row = equipment('upgrade', container='storage', rarity=1, name_key='UncommonNeck1',
            power={'available': True, 'target': 8, 'delta': 20, 'replaced_uid': None})
        world = observation([row, equipment('junk', 1)], town=True)
        engine = ManagedMode(s); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'transfer'); self.assertEqual(action['item']['item_uid'], 'upgrade')
        verified(engine, action)
        world['items'][0].update(container='inventory', slot_index=0); world['counts']['inventory']['occupied'] += 1
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'equip'); self.assertEqual(action['item']['item_uid'], 'upgrade')

    def test_necklace_identity_lock_rejects_higher_other_legendary(self):
        s = settings(equip=True, farm=False)
        s['auto_equip']['slot_locks'] = {'8': 'LegendaryNeck1'}
        wanted = equipment('wanted'); wanted['power'] = {'available': True, 'target': 8, 'delta': 10}
        other = equipment('other', 1); other['name_key'] = 'LegendaryNeck2'; other['power'] = {'available': True, 'target': 8, 'delta': 10000}
        world = observation([wanted, other]); engine = ManagedMode(s); engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'equip'); self.assertEqual(action['item']['item_uid'], 'wanted')

    def test_ring_chooses_allowed_target_not_unconstrained_best(self):
        s = settings(equip=True, farm=False); s['auto_equip']['slot_locks'] = {'6': 'LockedRing'}
        ring = equipment('ring'); ring['power'] = {'available': True, 'comparisons': [{'target': 6, 'delta': 100}, {'target': 7, 'delta': 25}]}
        engine = ManagedMode(s); world = observation([ring]); engine.start(world)
        self.assertEqual(engine.next_action(world)['target_slot'], 7)

    def test_storage_candidate_requires_free_slot_and_transfer_before_equip(self):
        row = equipment('u', container='storage'); row['power'] = {'available': True, 'target': 8, 'delta': 10}
        engine = ManagedMode(settings(equip=True, farm=False)); world = observation([row]); engine.start(world)
        action = engine.next_action(world); self.assertEqual(action['kind'], 'transfer'); self.assertEqual(action['target'], 'inventory')
        verified(engine, action)
        self.assertIsNone(engine.next_action(world))  # An old source read cannot issue the move twice.
        row['container'] = 'inventory'; world = observation([row])
        self.assertEqual(engine.next_action(world)['kind'], 'equip')

    def test_no_zero_negative_unknown_or_incompatible_equips(self):
        rows = []
        for n, power in enumerate([{'available': False}, {'available': True, 'target': 8, 'delta': 0},
                                   {'available': True, 'target': 8, 'delta': -1}, {'available': True, 'target': 8, 'delta': float('inf')}]):
            row = equipment(str(n), n); row['power'] = power; rows.append(row)
        world = observation(rows); engine = ManagedMode(settings(equip=True, farm=False)); engine.start(world)
        self.assertIsNone(engine.next_action(world))


class LifecycleTests(unittest.TestCase):
    def _native_repeat(self, runs=2):
        s = settings()
        s['farming']['repeat_maps'] = [
            {'dungeon_id': 'a', 'difficulty': 'Normal', 'runs': runs},
            {'dungeon_id': 'b', 'difficulty': 'Normal', 'runs': runs}]
        world = observation()
        world['scene']['difficulty'] = 'Normal'
        world['scene']['run'].update(dungeon_id='a', difficulty='Normal')
        engine = ManagedMode(s)
        engine.start(world)
        return engine, world

    def test_native_repeat_counts_confirmed_win_without_returning_every_run(self):
        engine, world = self._native_repeat()
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.repeat_completed, 1)
        self.assertEqual(engine.last_success, 'a')
        world['scene']['run'].update(id='run2', status='running', completion_confirmed=False)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.repeat_completed, 1)
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        self.assertIsNone(engine.next_action(world))
        world['scene']['completion_visible'] = {'CombatMapCompletionUI': True}
        action = after_rewards(engine, world)
        self.assertEqual(action['kind'], 'return_town')
        self.assertEqual((engine.repeat_index, engine.repeat_completed, engine.target_dungeon), (1, 0, 'b'))

    def test_native_repeat_receipt_and_later_completion_are_counted_once(self):
        engine, world = self._native_repeat()
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        for _ in range(5):
            self.assertIsNone(engine.next_action(world))
        world['scene']['run'].update(status='success')
        engine._runs(world)
        world['scene'].update(in_town=True)
        world['scene']['run']['retained_terminal'] = True
        engine._runs(world)
        self.assertEqual(engine.repeat_completed, 1)

    def test_native_repeat_300_run_batches_cycle_both_maps(self):
        engine, world = self._native_repeat(runs=300)
        for batch, dungeon in enumerate(('a', 'b')):
            for n in range(300):
                world['scene']['run'].update(id=f'{batch}-{n}', dungeon_id=dungeon,
                    status='settling', completion_confirmed=True)
                engine._runs(world)
                self.assertEqual(engine.repeat_completed, (n + 1) % 300)
            self.assertEqual((engine.repeat_index, engine.target_dungeon), ((batch + 1) % 2, 'b' if batch == 0 else 'a'))

    def test_native_repeat_does_not_count_other_maps_or_unconfirmed_settlement(self):
        engine, world = self._native_repeat()
        world['scene']['run'].update(status='settling')
        self.assertIsNone(engine.next_action(world))
        world['scene']['run'].update(id='other-map', dungeon_id='b', completion_confirmed=True)
        engine._runs(world)
        world['scene']['run'].update(id='other-difficulty', dungeon_id='a', difficulty='Inferno')
        engine._runs(world)
        self.assertEqual(engine.repeat_completed, 0)

    def test_native_repeat_counts_before_online_sync_wait_and_never_twice(self):
        engine, world = self._native_repeat()
        world['scene'].update(online_saving=True)
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.repeat_completed, 1)
        world['scene']['online_saving'] = False
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.repeat_completed, 1)

    def test_repeat_switches_difficulty_only_after_town_cleanup(self):
        s = settings(equipment_action='salvage'); s['farming']['difficulty']='Nightmare'
        world = observation([equipment('junk')]); world['scene']['difficulty']='Normal'
        engine = ManagedMode(s); engine.start(world)
        action = engine.next_action(world); self.assertEqual(action['kind'],'return_town'); verified(engine,action)
        world['scene'].update(in_town=True,run=None)
        action = engine.next_action(world); self.assertEqual(action['kind'],'salvage'); verified(engine,action)
        world = observation([],town=True); world['scene'].update(difficulty='Normal',run=None)
        action = engine.next_action(world); self.assertEqual(action['kind'],'switch_difficulty')
        self.assertEqual(action['difficulty'],'Nightmare'); verified(engine,action)
        self.assertIsNone(engine.next_action(world))
        world['scene']['difficulty']='Nightmare'
        action = engine.next_action(world); self.assertEqual(action['kind'],'enter_dungeon')
        self.assertEqual(action['difficulty'],'Nightmare')

    def test_push_crosses_difficulty_and_failure_returns_to_previous_tier(self):
        s = settings(); s['farming'].update(mode='push',dungeon_id='last',difficulty='Normal',progression=[
            {'dungeon_id':'last','difficulty':'Normal'}, {'dungeon_id':'a','difficulty':'Nightmare'},
            {'dungeon_id':'last','difficulty':'Nightmare'}, {'dungeon_id':'a','difficulty':'Inferno'}])
        world = observation(); world['scene'].update(dungeon_id='last',difficulty='Normal',
            run={'id':'normal-final','status':'success','dungeon_id':'last','difficulty':'Normal'})
        engine = ManagedMode(s); engine.start(world)
        action = after_rewards(engine, world); self.assertEqual(action['kind'],'return_town'); verified(engine,action)
        self.assertEqual((engine.target_dungeon,engine.target_difficulty),('a','Nightmare'))
        town = observation([],town=True); town['scene'].update(difficulty='Normal',run=None)
        action = engine.next_action(town); self.assertEqual(action['kind'],'switch_difficulty'); verified(engine,action)
        town['scene']['difficulty']='Nightmare'
        action = engine.next_action(town); self.assertEqual(action['dungeon_id'],'a'); verified(engine,action)
        world['scene'].update(dungeon_id='a',difficulty='Nightmare',run={'id':'nightmare-first','status':'running','dungeon_id':'a','difficulty':'Nightmare'})
        self.assertIsNone(engine.next_action(world))
        world['scene']['run']['status']='failed'
        action = engine.next_action(world); self.assertEqual(action['kind'],'return_town'); verified(engine,action)
        self.assertEqual((engine.target_dungeon,engine.target_difficulty),('last','Normal'))
        action = engine.next_action(town); self.assertEqual(action['difficulty'],'Normal'); verified(engine,action)
        town['scene']['difficulty']='Normal'
        action = engine.next_action(town); self.assertEqual((action['dungeon_id'],action['difficulty']),('last','Normal'))

    def test_death_returns_even_without_run_or_merchant_growth_data(self):
        for run in (None,{'id':'dead-run','status':'running'}):
            with self.subTest(run=run):
                config=settings();config['merchant']['enabled']=True
                config['merchant']['weights']['2']=1
                world=observation();world['scene'].update(dead=True,run=run)
                world['progression']={'available':False,'reason':'死亡期间成长数据未加载'}
                engine=ManagedMode(config);engine.start(world)
                action=engine.next_action(world)
                self.assertEqual(action['kind'],'return_town');self.assertTrue(engine.running)
                self.assertIn('死亡',action['reason'])

    def test_repeat_death_returns_then_enters_configured_map_once(self):
        engine,world=self._native_repeat(runs=2)
        world['scene'].update(dead=True)
        world['scene']['run']['status']='failed'
        action=engine.next_action(world);self.assertEqual(action['kind'],'return_town')
        verified(engine,action)
        town=observation([],town=True);town['scene'].update(difficulty='Normal',dead=False,run=None)
        for _ in range(6):
            action=engine.next_action(town)
            if action:break
        self.assertEqual(action['kind'],'enter_dungeon');self.assertEqual(action['dungeon_id'],'a')
        self.assertEqual(engine.repeat_completed,0)
        verified(engine,action)
        world['scene'].update(dead=False);world['scene']['run'].update(id='next-run',status='running')
        self.assertIsNone(engine.next_action(world));self.assertTrue(engine.running)
        self.assertEqual(engine.repeat_completed,0)

    def test_current_difficulty_is_fixed_at_start_not_changed_by_later_reads(self):
        world = observation(); world['scene']['difficulty']='Nightmare'
        engine = ManagedMode(settings()); engine.start(world)
        world['scene']['difficulty']='Normal'
        self.assertEqual(engine.next_action(world)['kind'],'return_town')
        self.assertEqual(engine.target_difficulty,'Nightmare')

    def test_town_without_run_can_clean_rewards_and_enter_map(self):
        for items,kind in (([], 'enter_dungeon'),([equipment('junk')], 'salvage')):
            with self.subTest(kind=kind):
                world = observation(items,town=True)
                world['scene']['run'] = None
                engine = ManagedMode(settings(equipment_action='salvage'))
                engine.start(world)
                action = engine.next_action(world)
                self.assertEqual(action['kind'],kind)
                self.assertTrue(engine.running)
                self.assertEqual(engine.handled_runs,set())

    def test_temporarily_missing_combat_run_does_not_raise_or_count_completion(self):
        world = observation()
        world['scene']['run'] = None
        engine = ManagedMode(settings()); engine.start(world)
        self.assertIsNone(engine.next_action(world))
        self.assertTrue(engine.running)
        self.assertIsNone(engine.last_success)
        self.assertEqual(engine.handled_runs,set())

    def test_entry_effect_requires_active_run_and_does_not_repeat_request(self):
        world = observation([],town=True)
        world['scene']['run'] = None
        engine = ManagedMode(settings()); engine.start(world)
        action = engine.next_action(world); verified(engine,action)
        world['scene']['in_town'] = False
        self.assertIsNone(engine.next_action(world))
        self.assertTrue(engine.running)
        self.assertIsNotNone(engine.await_effect)
        world['scene']['run'] = {'status':'running','id':'entered','dungeon_id':'a'}
        self.assertIsNone(engine.next_action(world))
        self.assertIsNone(engine.await_effect)
        self.assertTrue(engine.running)

    def test_malformed_run_pauses_instead_of_becoming_empty_record(self):
        world = observation([],town=True)
        world['scene']['run'] = ['unexpected']
        engine = ManagedMode(settings()); engine.start(world)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.phase,'paused')
        self.assertIn('关卡运行记录',engine.status)

    def test_verified_flag_cannot_hide_invalid_operation_quantity(self):
        world = observation([equipment('junk')], town=True)
        engine = ManagedMode(settings(equipment_action='sell')); engine.start(world)
        action = engine.next_action(world)
        engine.complete(action['action_id'], result={'verified': True, 'quantity': -1})
        self.assertFalse(engine.running)
        self.assertEqual(engine.state()['statistics']['equipment']['legendary']['sold'], 0)

    def test_starting_in_another_map_returns_then_switches_to_selection(self):
        engine = ManagedMode(settings()); world = observation(); world['scene']['dungeon_id'] = 'other'
        engine.start(world); self.assertEqual(engine.next_action(world)['kind'], 'return_town')
    def test_pause_resume_preserves_stats_but_new_start_resets(self):
        engine = ManagedMode(settings(equip=True, farm=False)); old = observation([]); engine.start(old)
        world = observation([equipment('new')]); engine.next_action(world)
        engine.pause(); self.assertFalse(engine.running)
        engine.resume(world); self.assertEqual(engine.state()['statistics']['equipment']['legendary']['count'], 1)
        engine.pause(); different = deepcopy(world); different['session'] = 'changed'
        with self.assertRaises(ValidationError): engine.resume(different)
        engine.stop(); engine.start(world)
        self.assertEqual(engine.state()['statistics']['equipment']['legendary']['count'], 0)

    def test_one_pending_action_cancel_and_unverified_failure(self):
        engine = ManagedMode(settings()); world = observation([], town=True); engine.start(world)
        action = engine.next_action(world)
        self.assertIsNone(engine.next_action(world))
        engine.complete(action['action_id'], result={'verified': False})
        self.assertFalse(engine.running); self.assertEqual(engine.phase, 'stopped')
        engine.start(world); engine.next_action(world); engine.stop()
        self.assertIsNone(engine.next_action(world))

    def test_strict_less_than_pressure_threshold(self):
        rows = [loot(str(n), 'material', 'Material', n) for n in range(6)]
        engine = ManagedMode(settings()); world = observation(rows, capacity=10); engine.start(world)
        self.assertIsNone(engine.next_action(world))  # 4 free = threshold; do not return.
        world = observation(rows + [loot('7', 'material', 'Material', 7)], capacity=10)
        self.assertEqual(engine.next_action(world)['kind'], 'return_town')

    def test_push_failure_falls_back_to_last_success(self):
        s = settings(); s['farming']['mode'] = 'push'
        engine = ManagedMode(s); world = observation(); engine.start(world)
        world['scene']['run'] = {'id': 'a1', 'status': 'success', 'dungeon_id': 'a'}
        action = after_rewards(engine, world); self.assertEqual(action['kind'], 'return_town'); verified(engine, action)
        town = observation([], town=True)
        action = engine.next_action(town); self.assertEqual(action['dungeon_id'], 'b'); verified(engine, action)
        world['scene'].update(dungeon_id='b', run={'id': 'b1', 'status': 'running'}); self.assertIsNone(engine.next_action(world))
        world['scene']['run']['status'] = 'failed'
        action = engine.next_action(world); self.assertEqual(action['kind'], 'return_town'); self.assertTrue(engine.push_failed)
        verified(engine, action)
        action = engine.next_action(town); self.assertEqual(action['dungeon_id'], 'a'); verified(engine, action)
        world['scene'].update(dungeon_id='a', run={'id': 'a2', 'status': 'running'}); engine.next_action(world)
        world['scene']['run']['status'] = 'success'
        action = after_rewards(engine, world); self.assertEqual(action['kind'], 'return_town'); verified(engine, action)
        self.assertEqual(engine.next_action(town)['dungeon_id'], 'b')

    def test_repeat_finished_map_always_returns_and_cleans_before_same_map_entry(self):
        engine = ManagedMode(settings()); world = observation(); engine.start(world)
        world['scene']['run'] = {'id': 'a1', 'status': 'success', 'dungeon_id': 'a'}
        action = after_rewards(engine, world); self.assertEqual(action['kind'], 'return_town'); verified(engine, action)
        self.assertIsNone(engine.next_action(world))  # Combat map cannot be entered again yet.
        town = observation([equipment('junk')], town=True)
        action = engine.next_action(town); self.assertEqual(action['kind'], 'transfer'); verified(engine, action)
        town['items'][0]['container'] = 'storage'; town['counts']['inventory']['occupied'] = 0
        action = engine.next_action(town); self.assertEqual(action['kind'], 'enter_dungeon'); self.assertEqual(action['dungeon_id'], 'a')

    def test_first_campaign_map_failure_repeats_first_map(self):
        s = settings(); s['farming']['mode'] = 'push'
        engine = ManagedMode(s); world = observation(); engine.start(world)
        world['scene']['run']['status'] = 'failed'
        self.assertEqual(engine.next_action(world)['kind'],'return_town')
        self.assertTrue(engine.running);self.assertEqual(engine.target_dungeon,'a')

    def test_character_change_and_incomplete_reads_never_execute(self):
        engine = ManagedMode(settings()); world = observation(); engine.start(world)
        world['complete'] = False; self.assertIsNone(engine.next_action(world)); self.assertTrue(engine.running)
        world['complete'] = True; world['session'] = 'other-player'; self.assertIsNone(engine.next_action(world)); self.assertFalse(engine.running)


class CleanupTests(unittest.TestCase):
    def test_hidden_black_mist_locks_and_stores_before_disposing_junk(self):
        keep=equipment('hidden',is_black_mist=True,review=True,rule_status='review',matches=None)
        junk=equipment('junk',1)
        engine=ManagedMode(settings(equipment_action='salvage'))
        engine.start(observation([keep,junk],town=True))
        action=engine.next_action(observation([keep,junk],town=True))
        self.assertEqual((action['kind'],action['item']['item_uid']),('lock','hidden'))
        verified(engine,action)
        self.assertIsNone(engine.next_action(observation([keep,junk],town=True)))
        keep['locked']=True
        action=engine.next_action(observation([keep,junk],town=True))
        self.assertEqual((action['kind'],action['target'],action['item']['item_uid']),('transfer','storage','hidden'))
        verified(engine,action);keep.update(container='storage',slot_index=0)
        action=engine.next_action(observation([keep,junk],town=True))
        self.assertEqual((action['kind'],action['item']['item_uid']),('salvage','junk'))

    def test_hidden_black_mist_does_not_bypass_other_rewards_or_identity_checks(self):
        for extra in ({'locked':None},{'issues':['身份待核验']},{'movable':False}):
            keep=equipment('hidden',is_black_mist=True,review=True,**extra)
            engine=ManagedMode(settings());world=observation([keep],town=True);engine.start(world)
            self.assertIsNone(engine.next_action(world));self.assertEqual(engine.phase,'paused')
        keep=equipment('hidden',is_black_mist=True,review=True)
        uncertain=equipment('ordinary',1,review=True)
        engine=ManagedMode(settings());world=observation([keep,uncertain],town=True);engine.start(world)
        for _ in range(5):self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.phase,'paused')
        engine.resume(world);self.assertEqual(engine.barrier_reads,0)
        self.assertIsNone(engine.next_action(world));self.assertTrue(engine.running)

    def test_stale_after_storage_neither_requeues_nor_recounts_gems(self):
        engine = ManagedMode(settings(gem_action='sell')); empty = observation([], town=True); engine.start(empty)
        gem = loot('newgem', 'gem', 'Gem1', count=3); world = observation([gem], town=True)
        action = engine.next_action(world)
        self.assertEqual((action['kind'], action['target']), ('transfer', 'storage')); verified(engine, action)
        # A delayed source snapshot must neither resubmit a transfer nor count
        # the same stack again. Completion is accepted only once it is stored.
        self.assertIsNone(engine.next_action(world))
        stats = engine.state()['statistics']['gems'][0]
        self.assertEqual((stats['count'], stats['stored'], stats['sold']), (3, 3, 0))
        gem.update(container='storage', slot_index=0)
        self.assertEqual(engine.next_action(observation([gem], town=True))['kind'], 'enter_dungeon')
        self.assertEqual(engine.state()['statistics']['gems'][0]['count'], 3)

    def test_divine_mist_matches_always_lock_before_storage(self):
        for extra in ({'rarity': 4}, {'is_black_mist': True}, {'matches': True}):
            with self.subTest(extra=extra):
                row = equipment('keep'); row.update(extra)
                world = observation([row], town=True); engine = ManagedMode(settings(equipment_action='sell')); engine.start(world)
                action = engine.next_action(world); self.assertEqual(action['kind'], 'lock'); verified(engine, action)
                row['locked'] = True; world = observation([row], town=True)
                action = engine.next_action(world); self.assertEqual(action['kind'], 'transfer'); self.assertEqual(action['target'], 'storage')

    def test_ancient_unconfigured_and_upgraded_socketed_garbage_can_be_salvaged(self):
        row = equipment('junk'); row.update(is_ancient=True, upgrade_level=20, socketed_gems=['Gem1'], review=True,
                                           rule_status='no_rule', rules_known=True)
        engine = ManagedMode(settings(equipment_action='salvage')); world = observation([row], town=True); engine.start(world)
        self.assertEqual(engine.next_action(world)['kind'], 'salvage')

    def test_locked_review_and_unknown_lock_never_disposed(self):
        rows = [equipment('locked'), equipment('review', 1), equipment('unknown', 2)]
        rows[0]['locked'] = True; rows[1]['review'] = True; rows[2]['locked'] = None
        engine = ManagedMode(settings(equipment_action='salvage')); world = observation(rows, town=True); engine.start(world)
        self.assertIsNone(engine.next_action(world))
        self.assertTrue(engine.running)

    def test_prelocked_nonmatching_gear_is_stored_instead_of_disposed_or_left_in_bag(self):
        row=equipment('manual-keep',locked=True)
        world=observation([row],town=True)
        engine=ManagedMode(settings(equipment_action='salvage'));engine.start(world)
        action=engine.next_action(world)
        self.assertEqual(action['kind'],'transfer')
        self.assertEqual(action['target'],'storage')
        self.assertEqual(action['item']['item_uid'],'manual-keep')

    def test_unknown_equipment_quality_flags_are_preserved(self):
        for field in ('rarity', 'is_black_mist', 'is_ancient'):
            row = equipment('unknown'); row[field] = None
            world = observation([row], town=True); engine = ManagedMode(settings(equipment_action='sell')); engine.start(world)
            self.assertIsNone(engine.next_action(world))
            self.assertTrue(engine.running)

    def test_ground_batch_all_protected_locks_precede_any_capacity_disposal(self):
        engine = ManagedMode(settings(equipment_action='sell')); empty = observation([], town=True); engine.start(empty)
        ground = equipment('drop'); empty['ground']['items'] = [ground]
        action = engine.next_action(empty); self.assertEqual(action['kind'], 'collect_ground'); verified(engine, action)
        first, second, junk = equipment('first', 0, matches=True), equipment('second', 1, is_black_mist=True), equipment('junk', 2)
        stored = equipment('storedjunk', 0, 'storage')
        world = observation([first, second, junk, stored], town=True, capacity=3, allowed=[0])
        action = engine.next_action(world); self.assertEqual(action['kind'], 'lock'); self.assertEqual(action['item']['item_uid'], 'first'); verified(engine, action)
        self.assertIsNone(engine.next_action(world))  # The lock write is not yet reflected; no sale.
        first['locked'] = True; world = observation([first, second, junk, stored], town=True, capacity=3, allowed=[0])
        action = engine.next_action(world); self.assertEqual(action['kind'], 'lock'); self.assertEqual(action['item']['item_uid'], 'second'); verified(engine, action)
        second['locked'] = True; world = observation([first, second, junk, stored], town=True, capacity=3, allowed=[0])
        action = engine.next_action(world); self.assertEqual(action['kind'], 'sell'); self.assertEqual(action['item']['item_uid'], 'junk')

    def test_new_chest_batch_unknown_affixes_blocks_even_known_garbage(self):
        engine = ManagedMode(settings(equipment_action='salvage')); chest = loot('c', 'chest', 'TreasureChest')
        world = observation([chest], town=True); engine.start(world)
        action = engine.next_action(world); self.assertEqual(action['kind'], 'open_chest'); verified(engine, action)
        uncertain, junk = equipment('uncertain', review=True), equipment('junk', 1)
        world = observation([uncertain, junk], town=True)
        self.assertIsNone(engine.next_action(world)); self.assertTrue(engine.running)
        self.assertEqual(engine.state()['statistics']['equipment']['legendary']['salvaged'], 0)
        uncertain.update(review=False, matches=True); world = observation([uncertain, junk], town=True)
        self.assertEqual(engine.next_action(world)['kind'], 'lock')

    def test_full_selected_page_eviction_does_not_use_other_pages(self):
        keep = equipment('keep'); keep.update(locked=True, matches=True)
        junk = equipment('junk', 100, 'storage')
        locked = equipment('locked', 101, 'storage'); locked['locked'] = True
        other_page_junk = equipment('other', 50, 'storage')
        s = settings(); s['cleanup']['equipment_pages'] = [3]
        engine = ManagedMode(s); world = observation([keep, junk, locked, other_page_junk], town=True, allowed=[50, 100, 101]); engine.start(world)
        action = engine.next_action(world); self.assertEqual(action['item']['item_uid'], 'junk'); self.assertEqual(action['target'], 'inventory')
        verified(engine, action); junk['container'] = 'inventory'; junk['slot_index'] = 1
        world = observation([keep, junk, locked, other_page_junk], town=True, allowed=[50, 100, 101])
        action = engine.next_action(world); self.assertEqual(action['kind'], 'sell'); self.assertEqual(action['item']['item_uid'], 'junk')
        verified(engine, action)
        world = observation([keep, locked, other_page_junk], town=True, allowed=[50, 100, 101])
        action = engine.next_action(world); self.assertEqual(action['target_indices'], [100, 101])

    def test_no_bag_slot_and_no_safe_junk_pauses_preserving_keep(self):
        keep = equipment('keep'); keep.update(locked=True, matches=True)
        locked = equipment('locked', 0, 'storage'); locked['locked'] = True
        engine = ManagedMode(settings()); world = observation([keep, locked], town=True, capacity=1, allowed=[0]); engine.start(world)
        self.assertIsNone(engine.next_action(world)); self.assertEqual(engine.phase, 'paused')
        self.assertIn('容量不足', engine.status)

    def test_full_bag_first_disposes_bag_junk_then_can_evict(self):
        keep = equipment('keep'); keep.update(locked=True, matches=True)
        junk = equipment('bagjunk', 1); storagejunk = equipment('storagejunk', 0, 'storage')
        engine = ManagedMode(settings(equipment_fallback='salvage')); world = observation([keep, junk, storagejunk], town=True, capacity=2, allowed=[0]); engine.start(world)
        action = engine.next_action(world); self.assertEqual(action['kind'], 'salvage'); self.assertEqual(action['item']['item_uid'], 'bagjunk')

    def test_gem_stack_capacity_only_in_selected_pages(self):
        gem = loot('g', 'gem', 'GemEmerald4', count=2, rarity=3, max_stack=99)
        stack = loot('s', 'gem', 'GemEmerald4', container='storage', count=97, rarity=3, max_stack=99)
        engine = ManagedMode(settings()); world = observation([gem, stack], town=True, allowed=[0]); engine.start(world)
        action = engine.next_action(world); self.assertEqual(action['kind'], 'transfer'); self.assertEqual(action['target_indices'], [0])
        engine = ManagedMode(settings()); stack['count'] = 98; world = observation([gem, stack], town=True, allowed=[0]); engine.start(world)
        self.assertIsNone(engine.next_action(world)); self.assertEqual(engine.phase, 'paused')

    def test_complete_cleanup_chest_carriage_ground_rescan_simulation(self):
        engine = ManagedMode(settings(equipment_action='sell', rune_action='sell', gem_action='sell'))
        chest = loot('chest', 'chest', 'TreasureChestGold')
        rune = loot('rune', 'rune', 'HunterRune1', 1, rarity=2)
        engine.start(observation([], town=True))
        world = observation([chest, rune], town=True)
        action = engine.next_action(world)
        self.assertEqual((action['kind'], action['target']), ('transfer', 'storage')); verified(engine, action)
        rune.update(container='storage', slot_index=0)
        world = observation([chest, rune], town=True)
        action = engine.next_action(world); self.assertEqual(action['kind'], 'open_chest'); verified(engine, action)
        newgear = equipment('new', 0); world = observation([newgear, rune], town=True)
        action = engine.next_action(world); self.assertEqual(action['kind'], 'sell'); verified(engine, action)
        world = observation([rune], town=True)
        drop = loot('carriage-gem', 'gem', 'Gem1'); world['carriage']['items'] = [drop]
        action = engine.next_action(world); self.assertEqual(action['kind'], 'collect_carriage'); verified(engine, action)
        world = observation([drop, rune], town=True)
        action = engine.next_action(world)
        self.assertEqual((action['kind'], action['target']), ('transfer', 'storage')); verified(engine, action)
        drop.update(container='storage', slot_index=1)
        world = observation([rune, drop], town=True)
        groundgear = equipment('groundgear'); world['ground']['items'] = [groundgear]
        action = engine.next_action(world); self.assertEqual(action['kind'], 'collect_ground'); verified(engine, action)
        world = observation([groundgear, rune, drop], town=True)
        action = engine.next_action(world); self.assertEqual(action['kind'], 'sell'); verified(engine, action)
        world = observation([rune, drop], town=True)
        self.assertEqual(engine.next_action(world)['kind'], 'enter_dungeon')
        stats = engine.state()['statistics']
        self.assertEqual(stats['equipment']['legendary']['count'], 2)
        self.assertEqual(stats['equipment']['legendary']['sold'], 2)
        self.assertEqual((stats['runes'][0]['stored'], stats['runes'][0]['sold']), (1, 0))
        self.assertEqual((stats['gems'][0]['stored'], stats['gems'][0]['sold']), (1, 0))

    def test_unknown_ground_scan_never_claims_cleanup_complete(self):
        engine = ManagedMode(settings()); world = observation([], town=True); world['ground']['complete'] = False; engine.start(world)
        self.assertIsNone(engine.next_action(world)); self.assertTrue(engine.running); self.assertEqual(engine.phase, 'cleanup')


class MetricsTests(unittest.TestCase):
    def test_other_drops_exclude_potions_and_coins_without_changing_known_categories(self):
        metric = ManagedMetrics(); metric.observe(observation())
        others = [loot('chest', 'chest', 'TreasureChest2', count=2),
                  loot('bar', 'material', 'GoldBar', count=3),
                  loot('rein', 'material', 'LegendaryRein', count=1),
                  loot('ticket', 'material', 'Ticket', count=4)]
        excluded = [loot(str(n), 'material', name, count=14) for n, name in enumerate(
            ('MovementPotion1', 'MovementPotion2', 'AttackPotion1', 'HealthPotionDrop', 'GoldDrop'))]
        world = observation(others + excluded + [equipment('new'), loot('rune', 'rune', 'Rune1'),
                                                  loot('gem', 'gem', 'Gem1')])
        for _ in range(3): metric.observe(world)
        state = metric.state()
        self.assertEqual({r['name_key']: r['count'] for r in state['others']},
                         {'TreasureChest2': 2, 'GoldBar': 3, 'LegendaryRein': 1, 'Ticket': 4})
        self.assertEqual(state['equipment']['legendary']['count'], 1)
        self.assertEqual((state['runes'][0]['count'], state['gems'][0]['count']), (1, 1))

    def test_other_initial_stacks_transfers_and_storage_credit_are_conserved(self):
        metric = ManagedMetrics(); old = loot('old', 'material', 'GoldBar', container='storage', count=5)
        metric.observe(observation([old]))
        new = loot('new', 'material', 'GoldBar', count=2); new['name'] = '金条'
        world = observation([old]); world['ground']['items'] = [dict(new, container='ground')]
        metric.observe(world)
        metric.observe(observation([old, new])); metric.committed('store-new', new, 'transfer')
        old['count'] = 7
        for _ in range(3): metric.observe(observation([old]))
        state = metric.state()['others'][0]
        self.assertEqual((state['name'], state['count'], state['stored']), ('金条', 2, 2))

    def test_other_event_dedup_and_uids_never_inflate_native_equipment_counts(self):
        metric = ManagedMetrics(); metric.observe(observation(drop_events=[], loot_ledger=self.ledger()))
        chest = loot('chest', 'chest', 'TreasureChest2', item_uid='unique-chest', rarity=3, container='ground')
        event = {'event_id': 17, 'item': chest, 'quantity': 1}
        world = observation(drop_events=[event], loot_ledger=self.ledger())
        world['ground']['items'] = [chest]
        for _ in range(3): metric.observe(world)
        self.assertEqual(metric.state()['others'][0]['count'], 1)
        self.assertTrue(all(r['count'] == 0 for r in metric.state()['equipment'].values()))

    def test_other_incomplete_scans_and_existing_stock_are_not_new_drops(self):
        metric = ManagedMetrics(); row = loot('old', 'chest', 'TreasureChest2', count=3)
        metric.observe(observation([row]))
        partial = observation(); partial['ground']['complete'] = False
        metric.observe(partial); metric.observe(observation([row]))
        self.assertEqual(metric.state()['others'], [])

    def test_native_gold_survives_map_repeats_and_missing_reads_without_sale_income(self):
        metric = ManagedMetrics()
        def world(total, **extra):
            return observation(gold_ledger={'available': True, 'id': 'player-statistics', 'total': total}, **extra)
        metric.observe(world(10000))
        for _ in range(3): metric.observe(world(10333))
        self.assertEqual(metric.state()['gold']['count'], 333)
        metric.observe(observation(gold_ledger={'available': False, 'reason': '切图中'}))
        self.assertEqual(metric.state()['gold'], {'count': 333, 'available': False, 'reason': '切图中'})
        resumed = world(11000, loot_ledger=self.ledger(run='another-map'))
        resumed['scene']['run']['id'] = 'next-round'
        metric.observe(resumed)
        # Selling/spending changes the wallet, but never changes this pickup total.
        resumed['character']['gold'] = 99999999
        metric.observe(resumed)
        self.assertEqual(metric.state()['gold']['count'], 1000)
        self.assertTrue(metric.state()['gold']['available'])

    def test_gold_character_changes_rollbacks_and_invalid_values_do_not_backfill_history(self):
        metric = ManagedMetrics(); metric.observe(observation())
        def observe(source, total):
            metric.observe(observation(gold_ledger={'available': True, 'id': source, 'total': total}))
        observe('first', 100000); observe('first', 100100)
        observe('second', 200000); observe('second', 200050)
        observe('second', 10); observe('second', 20)
        self.assertEqual(metric.state()['gold']['count'], 160)
        for invalid in (-1, True, 1.5, '999', 2**63):
            observe('second', invalid)
            self.assertFalse(metric.state()['gold']['available'])
        observe('second', 30)
        self.assertEqual(metric.state()['gold']['count'], 170)
        self.assertEqual(ManagedMetrics().state()['gold']['count'], 0)

    def ledger(self, counts=None, receipts=(), policy=None, run='ledger-1'):
        return {'available':True, 'complete':True, 'id':run,
                'counts':{str(i):(counts or {}).get(i,0) for i in range(6)},
                'receipts':list(receipts), 'policy':policy or {'0':1,'1':1,'2':2}}

    def test_native_carriage_receipts_exclude_baseline_and_deduplicate_polls(self):
        old = {'index':0,'action':'sell','item':equipment('old')}
        metric = ManagedMetrics(); metric.observe(observation(loot_ledger=self.ledger({3:1},[old])))
        event = {'index':1,'action':'salvage','item':equipment('new',is_ancient=True)}
        world = observation(loot_ledger=self.ledger({3:2},[old,event]))
        for _ in range(3): metric.observe(world)
        state = metric.state()['equipment']
        self.assertEqual((state['ancient']['count'],state['ancient']['salvaged']),(1,1))
        self.assertEqual(state['legendary']['sold'],0)
        next_event = {'index':0,'action':'sell','item':equipment('other')}
        metric.observe(observation(loot_ledger=self.ledger({3:1},[next_event],run='ledger-2')))
        self.assertEqual(metric.state()['equipment']['legendary']['sold'],1)

    def test_native_pickup_counter_and_later_item_snapshot_do_not_double_count(self):
        metric = ManagedMetrics(); metric.observe(observation(loot_ledger=self.ledger()))
        ledger = self.ledger({3:1},[{'index':0,'action':'pickup','item':equipment('new')}])
        metric.observe(observation(loot_ledger=ledger))
        metric.observe(observation([equipment('new')],loot_ledger=ledger))
        self.assertEqual(metric.state()['equipment']['legendary']['count'],1)
        world = observation([equipment('new')],loot_ledger=ledger)
        world['ground']['items'] = [equipment('pending',container='ground')]
        metric.observe(world)
        self.assertEqual(metric.state()['equipment']['legendary']['count'],2)

    def test_builtin_repeat_new_round_does_not_recount_cumulative_history(self):
        metric=ManagedMetrics();ledger=self.ledger({0:50,3:2});ledger['run_id']='round-1'
        metric.observe(observation(loot_ledger=ledger))
        ledger['run_id']='round-2'
        metric.observe(observation(loot_ledger=ledger));metric.observe(observation(loot_ledger=ledger))
        self.assertTrue(all(r['count']==0 and r['sold']==0 for r in metric.state()['equipment'].values()))
        ledger['counts']['0']+=1;metric.observe(observation(loot_ledger=ledger));metric.observe(observation(loot_ledger=ledger))
        self.assertEqual(metric.state()['equipment']['normal']['sold'],1)

    def test_actual_new_round_reset_can_reuse_receipt_indices(self):
        metric=ManagedMetrics();ledger=self.ledger();ledger['run_id']='round-1'
        metric.observe(observation(loot_ledger=ledger))
        event={'index':0,'action':'sell','item':equipment('one')}
        ledger.update(counts={'3':2},receipts=[event]);metric.observe(observation(loot_ledger=ledger))
        ledger.update(run_id='round-2',counts={'3':1},receipts=[event]);metric.observe(observation(loot_ledger=ledger))
        self.assertEqual(metric.state()['equipment']['legendary']['sold'],2)

    def test_low_quality_estimates_wait_a_read_and_subtract_delayed_bag_pickup(self):
        metric = ManagedMetrics(); metric.observe(observation(loot_ledger=self.ledger()))
        ledger = self.ledger({0:3,2:2})
        metric.observe(observation(loot_ledger=ledger))
        self.assertEqual(metric.state()['equipment']['normal']['sold'],0)
        bag = equipment('manual',rarity=0)
        metric.observe(observation([bag],loot_ledger=ledger))
        values = metric.state()['equipment']
        self.assertEqual((values['normal']['sold'],values['normal']['estimated_sold']),(2,2))
        self.assertEqual((values['rare']['salvaged'],values['rare']['estimated_salvaged']),(2,2))
        metric.observe(observation([bag],loot_ledger=ledger))
        self.assertEqual(metric.state()['equipment']['normal']['sold'],2)

    def test_changed_policy_and_late_first_read_do_not_claim_confirmed_disposals(self):
        metric = ManagedMetrics(); metric.observe(observation(loot_ledger=self.ledger()))
        metric.observe(observation(loot_ledger=self.ledger({0:2})))
        metric.observe(observation(loot_ledger=self.ledger({0:4},policy={'0':2,'1':1,'2':2})))
        metric.observe(observation(loot_ledger=self.ledger({0:4},policy={'0':2,'1':1,'2':2})))
        self.assertEqual(metric.state()['equipment']['normal']['sold'],0)
        self.assertEqual(metric.state()['equipment']['normal']['salvaged'],0)
        late = ManagedMetrics(); late.observe(observation())
        late.observe(observation(loot_ledger=self.ledger({3:10},[{'index':0,'action':'sell','item':equipment('old')}])) )
        self.assertEqual(late.state()['equipment']['legendary']['sold'],0)

    def test_initial_warehouse_stack_does_not_hide_sale_of_new_bag_drops(self):
        metric = ManagedMetrics(); initial = loot('s', 'gem', 'Gem1', container='storage', count=5)
        metric.observe(observation([initial]))
        new = loot('n', 'gem', 'Gem1', count=2)
        metric.observe(observation([initial, new])); metric.committed('sellnew', new, 'sell')
        metric.observe(observation([initial]))
        values = metric.state()['gems'][0]
        self.assertEqual((values['count'], values['sold']), (2, 2))

    def test_initial_stack_origin_follows_transfer_and_new_drops_keep_store_credit(self):
        metric = ManagedMetrics(); initial = loot('old', 'gem', 'Gem1', count=5)
        metric.observe(observation([initial])); metric.committed('oldstore', initial, 'transfer')
        initial['container'] = 'storage'; metric.observe(observation([initial]))
        new = loot('n', 'gem', 'Gem1', count=3)
        metric.observe(observation([initial, new])); metric.committed('newstore', new, 'transfer')
        initial['count'] = 8; metric.observe(observation([initial]))
        self.assertEqual(metric.state()['gems'][0]['stored'], 3)

    def test_mutually_exclusive_quality_priority(self):
        row = equipment('all'); row.update(rarity=4, is_ancient=True, is_black_mist=True)
        self.assertEqual(equipment_bucket(row), 'divine'); row['rarity'] = 3
        self.assertEqual(equipment_bucket(row), 'black_mist'); row['is_black_mist'] = False
        self.assertEqual(equipment_bucket(row), 'ancient')

    def test_initial_items_not_counted_unique_moves_and_reappearance_not_drops(self):
        metric = ManagedMetrics(); old = equipment('old'); metric.observe(observation([old]))
        new = equipment('new', 1); metric.observe(observation([old, new]))
        new['container'] = 'storage'; metric.observe(observation([old, new])); metric.observe(observation([old])); metric.observe(observation([old, new]))
        metric.committed('move1', new, 'transfer'); metric.committed('move1', new, 'transfer')
        metric.committed('sell1', old, 'sell'); metric.committed('sell2', new, 'sell')
        values = metric.state()['equipment']['legendary']
        self.assertEqual({k: values[k] for k in ('count', 'sold', 'stored')}, {'count': 1, 'sold': 1, 'stored': 1})

    def test_gem_split_merge_cross_source_conservation_and_new_post_sale_drops(self):
        metric = ManagedMetrics(); stack = loot('old', 'gem', 'Gem1', count=5); metric.observe(observation([stack]))
        stack['count'] = 7; world = observation([stack]); metric.observe(world)
        a, b = deepcopy(stack), deepcopy(stack); a.update(count=3, container='storage'); b.update(count=4)
        metric.observe(observation([a, b])); self.assertEqual(metric.state()['gems'][0]['count'], 2)
        metric.committed('sale1', a, 'sell', 3); metric.observe(observation([b]))
        metric.committed('sale2', b, 'sell', 4); metric.observe(observation([]))
        stack['count'] = 2; metric.observe(observation([stack]))
        self.assertEqual(metric.state()['gems'][0]['count'], 4)
        self.assertEqual(metric.state()['gems'][0]['sold'], 2)

    def test_explicit_loot_events_are_idempotent_not_double_snapshot_counted(self):
        metric = ManagedMetrics(); metric.observe(observation(drop_events=[]))
        new = equipment('new'); event = {'event_id': 17, 'item': new, 'quantity': 1}
        metric.observe(observation([new], drop_events=[event])); metric.observe(observation([new], drop_events=[event]))
        metric.committed('sold', new, 'sell')
        self.assertEqual(metric.state()['equipment']['legendary']['count'], 1)
        self.assertEqual(metric.state()['equipment']['legendary']['sold'], 1)

    def test_partial_source_read_cannot_infer_fungible_loot(self):
        metric = ManagedMetrics(); stack = loot('old', 'gem', 'Gem1', count=5); metric.observe(observation([stack]))
        partial = observation(); partial['ground']['complete'] = False; metric.observe(partial)
        metric.observe(observation([stack])); self.assertEqual(metric.state()['gems'], [])


class ManagedFinishAndBackgroundTests(unittest.TestCase):
    def test_stop_waits_for_existing_travel_then_returns_and_never_reenters(self):
        engine = ManagedMode(settings()); town = observation(town=True); engine.start(town)
        entering = engine.next_action(town); self.assertEqual(entering['kind'],'enter_dungeon')
        engine.request_finish(); self.assertEqual(engine.pending,entering)
        verified(engine,entering)
        self.assertIsNone(engine.next_action(town))
        combat = observation(); combat['scene']['difficulty']=entering.get('difficulty')
        returning = engine.next_action(combat); self.assertEqual(returning['kind'],'return_town')
        verified(engine,returning)
        self.assertIsNone(engine.next_action(combat))
        self.assertIsNone(engine.next_action(town)); self.assertTrue(engine.running)
        self.assertIsNone(engine.next_action(town)); self.assertFalse(engine.running)
        self.assertIn('已回城并完成',engine.status)

    def test_finishing_locks_before_disposal_even_with_background_switches_off(self):
        s=settings(equipment_action='salvage');s['background']={'rule_monitoring':False,'inventory_automation':False}
        engine=ManagedMode(s);engine.start(observation());engine.request_finish()
        world=observation([equipment('keep',matches=True),equipment('junk',1)],town=True)
        action=engine.next_action(world);self.assertEqual(action['kind'],'lock');verified(engine,action)
        world['items'][0]['locked']=True
        action=engine.next_action(world);self.assertEqual(action['kind'],'transfer');verified(engine,action)
        world['items'][0]['container']='storage'
        action=engine.next_action(world);self.assertEqual(action['kind'],'salvage')

    def test_finish_pause_resume_preserves_intent_and_ignores_farming_free_threshold(self):
        engine=ManagedMode(settings(farm=False,equip=True));world=observation(town=True,capacity=0)
        engine.start(world);engine.request_finish();engine.pause();engine.resume(world)
        self.assertTrue(engine.finishing)
        for _ in range(2):self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.phase,'stopped')

    def test_background_rule_monitoring_scans_old_storage_and_can_be_disabled(self):
        row=equipment('stored',container='storage',matches=True)
        world=observation([row]);engine=ManagedMode(settings());engine.start(world)
        action=engine.next_action(world);self.assertEqual(action['kind'],'lock')
        s=settings();s['background']['rule_monitoring']=False
        engine=ManagedMode(s);engine.start(world);self.assertIsNone(engine.next_action(world))

    def test_organize_uses_saved_pages_and_disabled_switch_moves_nothing(self):
        from deskrawl_assistant.automation import DEFAULT_SETTINGS as AUTO
        auto=deepcopy(AUTO);auto['gems'].update(enabled=True,pages=[3])
        world=observation([loot('gem','gem','Gem1')],automation_settings=auto)
        engine=ManagedMode(settings());engine.start(world)
        action=engine.next_action(world);self.assertEqual(action['reason_code'],'background_organize')
        self.assertEqual(action['target_indices'],list(range(100,150)))
        s=settings();s['background']['inventory_automation']=False
        engine=ManagedMode(s);engine.start(world);self.assertIsNone(engine.next_action(world))

    def test_old_config_defaults_both_recommended_switches_without_mutating_file_data(self):
        s=settings();s.pop('background');old=deepcopy(s)
        self.assertEqual(validate_settings(s)['background'],{'rule_monitoring':True,'inventory_automation':True})
        self.assertEqual(s,old)
        s['background']={'rule_monitoring':1,'inventory_automation':True}
        with self.assertRaises(ValidationError):validate_settings(s)


if __name__ == '__main__': unittest.main()
