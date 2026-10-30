"""Pickup receipts use native receive-side state, without invoking the game."""
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions, _Read
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.native_dispatcher import NativeActionError
from tests import test_game_actions_review as review


class PotionReceiptTests(unittest.TestCase):
    def game(self, *, before=3, after=3, consumed=True, remaining=False, change=None):
        header = bytearray(216)
        struct.pack_into('<Q', header, 0, 0x1000)
        struct.pack_into('<Q', header, 16, 0x2000)
        header[133] = 1
        struct.pack_into('<i', header, 164, 1)
        selected = {'_address':0x3000, '_header':bytes(header), 'count':1,
            'observation_id':'potion-drop', 'internal_name':'HealthPotionDrop',
            'item_type':1, '_before_virtual':{'charges':before}}
        header[185] = consumed
        if change: change(header)
        game = review.GroundReceiptReviewTests.ground_game(False, [], [selected] if remaining else [])
        game.reader.memory.read = lambda address, size:bytes(header[address-0x3000:address-0x3000+size])
        read = _Read(game.reader, {})
        read.klass = Mock()
        game._context.return_value = (read, 0x100, None, 0)
        game._virtual_balance = Mock(return_value={'charges':after})
        return game, selected, read

    def confirm(self, game, selected):
        with patch('deskrawl_assistant.game_actions.time.monotonic', side_effect=review.GroundReceiptReviewTests.clock()), \
                patch('deskrawl_assistant.game_actions.time.sleep'):
            return game._confirm_effect('collect_ground', selected, {})

    def test_full_flask_drop_native_consumption_confirms_without_healing(self):
        game, selected, read = self.game()
        self.assertTrue(self.confirm(game, selected))
        self.assertTrue(selected['_pickup_receipt']['consumed'])
        self.assertEqual(selected['_confirmed_count'], 1)
        read.klass.assert_called_once_with(0x1000, 'LootDrop')
        game._context.assert_called_once_with(allow_busy=True)
        game.reader.snapshot.assert_not_called()

    def test_automatic_potion_use_can_spend_charge_before_receipt(self):
        game, selected, _ = self.game(before=1, after=0)
        self.assertTrue(self.confirm(game, selected))

    def test_destroyed_unity_object_keeps_same_native_consumption_receipt(self):
        game, selected, _ = self.game(change=lambda h:struct.pack_into('<Q', h, 16, 0))
        self.assertTrue(self.confirm(game, selected))

    def test_charge_increase_does_not_require_destroyed_object_memory_to_survive(self):
        game, selected, _ = self.game(before=1, after=2)
        game.reader.memory.read = Mock(side_effect=MemoryReadError('destroyed'))
        self.assertTrue(self.confirm(game, selected))
        game.reader.memory.read.assert_not_called()

    def test_unconsumed_or_still_present_drop_cannot_confirm(self):
        for options in ({'consumed':False}, {'remaining':True}, {'remaining':True, 'before':1, 'after':2}):
            with self.subTest(options=options):
                game, selected, _ = self.game(**options)
                self.assertFalse(self.confirm(game, selected))

    def test_reused_drop_identity_cannot_confirm_full_flask_consumption(self):
        for change in (lambda h:struct.pack_into('<Q', h, 0, 0x1111),
                       lambda h:struct.pack_into('<Q', h, 16, 0x2222),
                       lambda h:struct.pack_into('<Q', h, 120, 0x4444),
                       lambda h:struct.pack_into('<Q', h, 136, 0x5555),
                       lambda h:struct.pack_into('<i', h, 164, 2),
                       lambda h:h.__setitem__(132, 1)):
            with self.subTest(change=change):
                game, selected, _ = self.game(change=change)
                self.assertFalse(self.confirm(game, selected))

    def test_receive_balance_reads_potion_system_charges_and_never_player_health(self):
        game = object.__new__(GameActions)
        read = Mock()
        read.singleton.return_value = 0x1000
        read.integer.return_value = 2
        self.assertEqual(game._virtual_balance({'internal_name':'HealthPotionDrop'}, read), {'charges':2})
        read.singleton.assert_called_once_with('HealthPotionSystem')
        read.integer.assert_called_once_with(0x1000+72)
        read.read.assert_not_called()
        read.integer.return_value = -1
        with self.assertRaises(MemoryReadError):
            game._virtual_balance({'internal_name':'HealthPotionDrop'}, read)

    def test_changing_receipt_is_reread_without_submitting_another_action(self):
        game, selected, _ = self.game()
        game._confirm_pickup = Mock(side_effect=[SnapshotChangedError('changed'), True])
        game.dispatcher = Mock()
        with patch('deskrawl_assistant.game_actions.time.monotonic', side_effect=[0, 0, .1]), \
                patch('deskrawl_assistant.game_actions.time.sleep'):
            self.assertTrue(game._confirm_effect('collect_ground', selected, {}))
        self.assertEqual(game._confirm_pickup.call_count, 2)
        game.dispatcher.invoke.assert_not_called()

    def test_unreadable_receipt_exhaustion_remains_unconfirmed(self):
        game, selected, _ = self.game()
        game._confirm_pickup = Mock(side_effect=MemoryReadError('changing'))
        self.assertFalse(self.confirm(game, selected))
        self.assertEqual(selected['_pickup_receipt'], {'read_error':'changing'})

    def test_unconfirmed_action_logs_selected_item_and_is_never_retried(self):
        reader, dispatcher, read = Mock(), Mock(), Mock()
        game = GameActions(reader, dispatcher=dispatcher)
        read.guards = []
        read.singleton.return_value = 3
        read.boolean.return_value = False
        game._context = Mock(return_value=(read, 1, 2, 0))
        game.capabilities = Mock(return_value={'compatible':True})
        selected = {'internal_name':'HealthPotionDrop', 'count':1, 'selection_id':'chosen',
            '_address':123, '_pickup_receipt':{'before':{'charges':3}, 'after':{'charges':3}}}
        game._ground = Mock(return_value=[selected])
        game._before_pickup = Mock()
        game._confirm_effect = Mock(return_value=False)
        dispatcher.invoke.return_value = {}
        with patch('deskrawl_assistant.game_actions.record') as log:
            with self.assertRaises(NativeActionError) as failure:
                game.execute('collect_ground', {'selection_id':'chosen'})
        self.assertTrue(failure.exception.uncertain)
        dispatcher.invoke.assert_called_once()
        self.assertEqual([c.args[0] for c in log.call_args_list],
            ['game_action_requested', 'game_action_unconfirmed'])
        for call in log.call_args_list:
            self.assertEqual(call.kwargs['item']['internal_name'], 'HealthPotionDrop')
            self.assertEqual(call.kwargs['item']['selection_id'], 'chosen')
        self.assertEqual(log.call_args_list[-1].kwargs['receipt'], selected['_pickup_receipt'])


class GoldReceiptTests(unittest.TestCase):
    def game(self, *, amount=2312, increase=2312, consumed=True, remaining=False, change=None):
        game, selected, read = PotionReceiptTests().game(remaining=remaining)
        before = bytearray(selected['_header'])
        before[132:134] = b'\1\0'
        struct.pack_into('<i', before, 128, amount)
        selected.update(_header=bytes(before), internal_name='GoldDrop', count=amount,
            _before_virtual=23967614)
        after = bytearray(before)
        after[185] = consumed
        if change: change(after)
        game.reader.memory.read = lambda address, size:bytes(after[address-0x3000:address-0x3000+size])
        game._virtual_balance.return_value = selected['_before_virtual']+increase
        return game, selected

    def test_recorded_boss_gold_receipt_confirms_actual_2312_gold(self):
        game, selected = self.game()
        self.assertTrue(PotionReceiptTests().confirm(game, selected))
        self.assertEqual(selected['_confirmed_count'], 2312)
        self.assertEqual(selected['_pickup_receipt']['after'], 23969926)

    def test_other_gold_arriving_same_frame_does_not_reject_consumed_pile(self):
        game, selected = self.game(increase=2500)
        self.assertTrue(PotionReceiptTests().confirm(game, selected))
        self.assertEqual(selected['_confirmed_count'], 2312)

    def test_gold_requires_both_bound_consumption_and_sufficient_balance(self):
        for options in ({'increase':0}, {'increase':2311}, {'consumed':False},
                        {'remaining':True}, {'consumed':False, 'increase':5000},
                        {'change':lambda h:struct.pack_into('<i', h, 128, 9999)}):
            with self.subTest(options=options):
                game, selected = self.game(**options)
                self.assertFalse(PotionReceiptTests().confirm(game, selected))


class GroundCountTests(unittest.TestCase):
    def ground(self, *, name='GoldDrop', item_type=1, gold=2312, material=0, stack=0):
        header = bytearray(216)
        struct.pack_into('<Q', header, 0, 0x1000)
        struct.pack_into('<Q', header, 16, 0x2000)
        struct.pack_into('<iii', header, 164, material, stack, 0)
        if name == 'GoldDrop':
            header[132] = 1
            struct.pack_into('<i', header, 128, gold)
        elif name == 'HealthPotionDrop':
            header[133] = 1
        else:
            struct.pack_into('<Q', header, 120, 0x4000)
        game = object.__new__(GameActions)
        game.reader = SimpleNamespace(session_id='counts',
            class_name=lambda klass:'LootDrop', string=lambda ptr:'',
            memory=SimpleNamespace(u64=lambda address:0x1000, read=lambda address,size:bytes(header[:size])),
            base_item=lambda item:{'internal_name':name, 'item_type':item_type})
        game._enrich_drops = lambda read, rows:rows
        read = Mock()
        read.ptr.side_effect = lambda address:{0x100+224:0x5000, 0x100+88:0}[address]
        read.list.return_value = [0x3000]
        read.guards = []
        return game._ground(read, 0x100)

    def test_gold_amount_comes_from_gold_field_not_empty_material_count(self):
        row = self.ground(material=0, stack=0)[0]
        self.assertEqual(row['count'], 2312)

    def test_changed_gold_amount_changes_selection_token(self):
        first, second = self.ground(gold=2312)[0], self.ground(gold=2313)[0]
        self.assertNotEqual(first['selection_id'], second['selection_id'])
        self.assertEqual(first['observation_id'], second['observation_id'])

    def test_potion_native_pickup_always_refills_one_charge(self):
        self.assertEqual(self.ground(name='HealthPotionDrop', material=10, stack=4)[0]['count'], 1)

    def test_other_materials_and_stacks_keep_their_own_quantity_fields(self):
        self.assertEqual(self.ground(name='Iron', material=5, stack=9)[0]['count'], 5)
        self.assertEqual(self.ground(name='GemOriginal', item_type=6, material=5, stack=9)[0]['count'], 9)

    def test_uninitialised_or_invalid_gold_is_never_assumed_to_be_one(self):
        for gold in (0, -1):
            with self.subTest(gold=gold), self.assertRaises(MemoryReadError):
                self.ground(gold=gold)


if __name__ == '__main__':
    unittest.main()
