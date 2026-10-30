"""World-boss chests use the same guarded cleanup cycle as standard chests."""
import unittest
from unittest.mock import Mock

from deskrawl_assistant.chests import chest_keys
from deskrawl_assistant.game_actions import GameActions
from deskrawl_assistant.native_dispatcher import NativeActionError
from deskrawl_assistant.managed_mode import ManagedMode, item_kind
from deskrawl_assistant.web_service import item_kind as inventory_kind
from test_managed_mode import equipment, loot, observation, settings, verified


PUMPKIN = tuple('WorldBossPumpkinChest1' + difficulty
                for difficulty in ('Normal', 'Nightmare', 'Inferno'))


class ChestCompatibilityTests(unittest.TestCase):
    def test_real_inventory_rows_classify_all_verified_chests_without_name_prefix(self):
        self.assertTrue(set(PUMPKIN) <= chest_keys())
        for name in chest_keys():
            with self.subTest(name=name):
                row = {'internal_name': name, 'item_type': 2, 'item_class': 'ItemData'}
                self.assertEqual(item_kind(row), 'chest')
                self.assertEqual(inventory_kind(row), ('chest', '宝箱'))
        for name in ('MovementPotion1', 'Rein_WorldBossPumpkin', 'UnknownChest'):
            row = {'internal_name': name, 'item_type': 2, 'item_class': 'ItemData'}
            self.assertEqual(inventory_kind(row)[0], 'material')

    def test_cleanup_opens_pumpkin_chest_then_processes_its_new_equipment(self):
        for name in PUMPKIN:
            with self.subTest(name=name):
                # Inventory snapshots do not already have a preview's kind tag.
                chest = loot('chest', 'material', name, item_type=2)
                chest.pop('kind')
                engine = ManagedMode(settings(equipment_action='salvage'))
                world = observation([chest], town=True)
                engine.start(world)
                action = engine.next_action(world)
                self.assertEqual(action['kind'], 'open_chest')
                verified(engine, action)
                action = engine.next_action(observation([equipment('new-drop')], town=True))
                self.assertEqual(action['kind'], 'salvage')

    def test_locked_pumpkin_chest_remains_in_bag(self):
        row = loot('chest', 'chest', PUMPKIN[0])
        row['locked'] = True
        engine = ManagedMode(settings())
        world = observation([row], town=True)
        engine.start(world)
        action = engine.next_action(world)
        self.assertTrue(action is None or action['kind'] != 'open_chest')

    def native(self, name, effect=2):
        reader, dispatcher, read = Mock(), Mock(), Mock()
        read.guards = []
        read.singleton.return_value = 1000
        read.boolean.return_value = False
        read.integer.return_value = effect
        dispatcher.invoke.return_value = {}
        game = GameActions(reader, dispatcher=dispatcher, allow_unverified=True)
        game.capabilities = Mock(return_value={'compatible': True})
        game._context = Mock(return_value=(read, 1000, 2000, 1))
        selected = {'internal_name': name, 'item_type': 2, 'slot_index': 0, 'count': 1}
        game._selected_inventory = Mock(return_value=(selected, 3000, b'', 4000, 0))
        game._confirm_effect = Mock(return_value=True)
        return game, dispatcher, read

    def test_native_chest_entry_accepts_three_tiers_and_rechecks_actual_effect(self):
        for name in PUMPKIN:
            with self.subTest(name=name):
                game, dispatcher, read = self.native(name)
                self.assertTrue(game.execute('open_chest', {})['effect_confirmed'])
                read.integer.assert_called_with(4056)
                calls = dispatcher.invoke.call_args.args[0]
                self.assertEqual(calls[0].method, 'ConsumableItemSystem.eyu')
        for name, effect in ((PUMPKIN[0], 0), ('MovementPotion1', 2), ('UnknownChest', 2)):
            with self.subTest(name=name, effect=effect):
                game, dispatcher, _ = self.native(name, effect)
                with self.assertRaisesRegex(NativeActionError, '只接受已确认的宝箱'):
                    game.execute('open_chest', {})
                dispatcher.invoke.assert_not_called()
