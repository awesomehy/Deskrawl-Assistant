"""Chest pressure yields to protection, but not to endless outside pickups."""
from copy import deepcopy
import unittest

from deskrawl_assistant.managed_mode import CHEST_BATCH_LIMIT, ManagedMode
from test_managed_mode import equipment, loot, observation, settings, verified


def chest(uid, slot=0, count=1, container='inventory', **extra):
    row = loot(uid, 'chest', 'WorldBossPumpkinChest1Inferno', slot,
               count=count, container=container, item_type=2)
    row.update(extra)
    return row


class ChestBatchTests(unittest.TestCase):
    def test_twenty_counts_stack_quantities_only_in_inventory_and_forces_return(self):
        for count, expected in ((19, None), (20, 'return_town'), (35, 'return_town')):
            with self.subTest(count=count):
                world = observation([chest('a', count=count),
                    chest('warehouse', container='storage', count=99)])
                engine = ManagedMode(settings())
                engine.start(world)
                action = engine.next_action(world)
                self.assertEqual(action and action['kind'], expected)
                if action:
                    self.assertIn('20', action['reason'])
        world = observation([chest('a', count=12), chest('b', 1, count=8)])
        engine = ManagedMode(settings())
        engine.start(world)
        self.assertEqual(engine.next_action(world)['kind'], 'return_town')

    def test_threshold_also_returns_when_only_auto_equip_is_enabled(self):
        world = observation([chest('batch', count=20)])
        engine = ManagedMode(settings(equip=True, farm=False))
        engine.start(world)
        self.assertEqual(engine.next_action(world)['kind'], 'return_town')

    def test_town_twenty_chests_open_before_any_external_pickup(self):
        world = observation([chest('batch', count=20)], town=True)
        world['ground']['items'] = [equipment('outside', container='ground')]
        world['carriage']['items'] = [chest('carriage', container='carriage')]
        engine = ManagedMode(settings())
        engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'open_chest')
        self.assertEqual(action['item']['selection_id'], 'batch')

    def test_opening_receipt_must_be_visible_before_another_chest(self):
        world = observation([chest('batch', count=20)], town=True)
        engine = ManagedMode(settings())
        engine.start(world)
        action = engine.next_action(world)
        verified(engine, action)
        self.assertIsNone(engine.next_action(world))
        self.assertTrue(engine.running)

    def test_new_equipment_processed_before_next_chest_or_pickup(self):
        box = chest('batch', count=20)
        world = observation([box], town=True)
        engine = ManagedMode(settings(equipment_action='sell'))
        engine.start(world)
        verified(engine, engine.next_action(world))
        box['count'] -= 1
        keep, junk = equipment('keep', 1, matches=True), equipment('junk', 2)
        bag = [box, keep, junk]
        for kind in ('lock', 'transfer', 'sell', 'open_chest'):
            world = observation(bag, town=True)
            world['ground']['items'] = [equipment('outside', container='ground')]
            action = engine.next_action(world)
            self.assertEqual(action['kind'], kind)
            verified(engine, action)
            if kind == 'lock':
                keep['locked'] = True
            elif kind == 'transfer':
                keep.update(container='storage', slot_index=0)
            elif kind == 'sell':
                bag.remove(junk)

    def test_twentieth_collected_chest_starts_batch_before_twenty_first(self):
        box = chest('bag', count=19)
        outside = chest('drop', container='ground')
        world = observation([box], town=True)
        world['ground']['items'] = [outside]
        engine = ManagedMode(settings())
        engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_ground')
        verified(engine, action)
        outside.update(container='inventory', slot_index=1)
        world = observation([box, outside], town=True)
        world['ground']['items'] = [chest('next-drop', container='ground')]
        self.assertEqual(engine.next_action(world)['kind'], 'open_chest')

    def test_full_bag_with_chests_can_start_opening_even_below_twenty(self):
        world = observation([chest('only')], town=True, capacity=1)
        world['ground']['items'] = [equipment('outside', container='ground')]
        engine = ManagedMode(settings(equipment_action='sell'))
        engine.start(world)
        self.assertEqual(engine.next_action(world)['kind'], 'open_chest')

    def test_locked_or_unverified_chests_are_never_opened(self):
        for extra in ({'locked':True}, {'issues':['identity unconfirmed']}):
            with self.subTest(extra=extra):
                world = observation([chest('batch', count=20, **extra)], town=True)
                engine = ManagedMode(settings())
                engine.start(world)
                self.assertIsNone(engine.next_action(world))
                self.assertEqual(engine.phase, 'paused')
                self.assertIn('宝箱', engine.status)

    def test_locked_chests_below_threshold_remain_after_drain(self):
        locked, active = chest('locked', count=19, locked=True), chest('open', 1)
        world = observation([locked, active], town=True)
        engine = ManagedMode(settings())
        engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['item']['selection_id'], 'open')
        verified(engine, action)
        world = observation([locked], town=True)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'enter_dungeon')

    def test_incomplete_outside_read_waits_without_opening_or_pausing(self):
        for source in ('carriage', 'ground'):
            with self.subTest(source=source):
                world = observation([chest('batch', count=20)], town=True)
                world[source]['complete'] = False
                engine = ManagedMode(settings())
                engine.start(world)
                self.assertIsNone(engine.next_action(world))
                self.assertTrue(engine.running)
                world[source]['complete'] = True
                self.assertEqual(engine.next_action(world)['kind'], 'open_chest')

    def test_threshold_preserves_boss_ground_reward_collection_before_travel(self):
        world = observation([chest('batch', count=20)])
        world['scene']['run']['status'] = 'success'
        world['ground']['items'] = [equipment('boss', container='ground')]
        engine = ManagedMode(settings())
        engine.start(world)
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_ground')
        self.assertEqual(action['item']['item_uid'], 'boss')

    def test_full_bag_and_large_outside_chest_batches_finish_without_loss(self):
        bag = [chest(f'bag-{n}', n) for n in range(35)]
        potions = [loot(f'potion-{n}', 'material', 'MovementPotion1', n + 35, count=14)
                   for n in range(5)]
        bag.extend(deepcopy(potions))
        ground = [chest(f'ground-{n}', container='ground') for n in range(35)]
        carriage = [chest(f'carriage-{n}', container='carriage') for n in range(30)]
        ground.append(equipment('outside-gear', container='ground'))
        engine = ManagedMode(settings(equipment_action='sell'))
        engine.start(observation(bag, town=True))
        opened = sold = 0
        phases = []
        for _ in range(600):
            world = observation(bag, town=True)
            world['ground']['items'] = deepcopy(ground)
            world['carriage']['items'] = deepcopy(carriage)
            action = engine.next_action(world)
            self.assertTrue(engine.running, engine.status)
            self.assertIsNotNone(action, engine.status)
            kind = action['kind']
            phases.append(kind)
            if kind == 'enter_dungeon':
                break
            selected = action['item']['selection_id']
            if kind == 'open_chest':
                row = next(r for r in bag if r['selection_id'] == selected)
                row['count'] -= 1
                if not row['count']:
                    bag.remove(row)
                opened += 1
                ground.append(equipment(f'opened-{opened}', container='ground'))
            elif kind in ('collect_ground', 'collect_carriage'):
                self.assertLess(engine._bag_chest_count(bag), CHEST_BATCH_LIMIT)
                source = ground if kind == 'collect_ground' else carriage
                row = next(r for r in source if r['selection_id'] == selected)
                source.remove(row)
                row.update(container='inventory', slot_index=next(n for n in range(40)
                    if n not in {r['slot_index'] for r in bag}))
                bag.append(row)
            elif kind == 'sell':
                row = next(r for r in bag if r['selection_id'] == selected)
                self.assertEqual(row['kind'], 'equipment')
                bag.remove(row)
                sold += 1
            else:
                self.fail(f'Unexpected action: {kind}')
            verified(engine, action)
        else:
            self.fail('Cleanup did not finish within the bounded simulation')
        self.assertEqual(phases[:35], ['open_chest'] * 35)
        self.assertEqual((opened, sold), (100, 101))
        self.assertEqual(bag, potions)
        self.assertEqual((ground, carriage), ([], []))


if __name__ == '__main__':
    unittest.main()
