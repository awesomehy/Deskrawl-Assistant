"""Commodity transaction tests use copied data only; never attach or sell."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from deskrawl_assistant.auction import DEFAULT_AUCTION, next_action, eligible
from deskrawl_assistant.auction_commodities import (
    commodity_candidate, commodity_receipt, upload_receipt_matches)
from deskrawl_assistant.auction_runtime import AuctionRuntime, AuctionUploadPending, auction_candidate_fields
from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.native_dispatcher import NativeActionError, compile_mailbox, load_profile
from deskrawl_assistant.native_memory import SnapshotChangedError
from deskrawl_assistant.models import ValidationError
from tests.test_auction_runtime import ready
from tests.test_auction_coordinator_audit import TradingFake, STEAMID


OP = 'commodity:' + 'a' * 32


def gold(**changes):
    return {**dict(internal_name='GoldBar', item_uid=None, instance_id=None,
        item_type=7, base_rarity=3, count=3, container='inventory', slot_index=0,
        transfer_key='copied-slot-proof', issues=[],
        read_evidence={'snapshot_consistent': True}), **changes}


def snapshot(count=2, **changes):
    return {**dict(complete=True, containers={
        'inventory': {'complete': True, 'slots': [gold(count=count)] if count else []},
        'storage': {'complete': True, 'slots': []}}), **changes}


def pending(**changes):
    return {**dict(auction_operation_id=OP, item_uid=None, internal_name='GoldBar',
        steamid=STEAMID, submitted_at=time.time() - 2, before_count=3,
        before_listing_ids=['old']), **changes}


def server(**changes):
    return ready(**{**dict(steamid=STEAMID, server_checked_at=time.time(), listing_count=2,
        listings=[{'instance_id': 'old', 'internal_name': 'GoldBar', 'received': False, 'pending': False},
                  {'instance_id': 'new-server-instance', 'internal_name': 'GoldBar',
                   'received': False, 'pending': True, 'listed_at': 123}]), **changes})


class CommodityPolicyTests(unittest.TestCase):
    def test_candidate_requires_known_native_type_and_guarded_stack(self):
        catalog = Mock(); catalog.resolve.return_value = (object(), '')
        for name in ('GoldBar', 'UpgradeCommodity1'):
            for count in (1, 3):
                row = gold(internal_name=name, count=count)
                fields = auction_candidate_fields(row, catalog)
                self.assertTrue(fields['auction_commodity_candidate'])
                self.assertTrue(fields['auction_candidate'])
                self.assertFalse(fields['auction_identity_verified'])  # Never invent a generated identity.
        for change in ({'internal_name': 'Unknown'}, {'item_type': 3}, {'base_rarity': 4},
                       {'count': 0}, {'count': True}, {'transfer_key': None},
                       {'issues': ['incomplete']}, {'read_evidence': {}}, {'read_evidence': None},
                       {'item_uid': 'some-generated-uid'}, {'instance_id': 'instance'},
                       {'steam_retired': True}, {'is_equipped': True}):
            with self.subTest(change=change):
                self.assertFalse(commodity_candidate(gold(**change)))

    def test_planner_withdraws_stack_then_uploads_one_with_capacity_and_policy_gates(self):
        config = {**deepcopy(DEFAULT_AUCTION), 'upload_enabled': True, 'categories': ['gold_bar']}
        row = gold(container='storage', auction_candidate=True, auction_commodity_candidate=True)
        world = {'complete': True, 'scene': {'in_town': True}, 'items': [row],
                 'counts': {'inventory': {'capacity': 40, 'occupied': 39}},
                 'storage': {'allowed_indices': [0]}, 'auction': ready()}
        self.assertEqual(next_action(config, world)['kind'], 'transfer')
        row['container'] = 'inventory'
        action = next_action(config, world)
        self.assertEqual(action['kind'], 'auction_upload'); self.assertEqual(action['quantity'], 1)
        self.assertIsNone(action['item']['item_uid'])
        for state in ({'pending': True}, {'blocked_keys': ['GoldBar']}, {'listing_count': 8}, {'busy': True}):
            world['auction'] = ready(**state)
            self.assertIsNone(next_action(config, world))
        self.assertFalse(eligible(row, {**config, 'excluded_keys': ['GoldBar']}))
        self.assertFalse(eligible(row, {**config, 'categories': []}))
        row['container'] = 'storage'; world['auction'] = ready()
        world['counts']['inventory']['occupied'] = 40
        self.assertIsNone(next_action(config, world))


class CommodityReceiptTests(unittest.TestCase):
    def test_requires_exact_native_decrement_and_one_new_server_instance(self):
        receipt = commodity_receipt(pending(), server(), snapshot())
        self.assertEqual(receipt['uploaded_instance_id'], 'new-server-instance')
        self.assertIsNone(receipt['uploaded_uid'])
        self.assertTrue(upload_receipt_matches(pending(), receipt))
        for count in (0, 1, 3, 4):
            self.assertIsNone(commodity_receipt(pending(), server(), snapshot(count)))
        for state in ({'steamid': '76561198000000002'}, {'steamid_verified': False},
                      {'busy': True}, {'needs_refresh': True}, {'server_checked_at': time.time() - 40},
                      {'server_checked_at': time.time() - 10}, {'server_checked_at': time.time() + 10},
                      {'listings': []}, {'listings': server()['listings'] * 2}):
            with self.subTest(state=state):
                self.assertIsNone(commodity_receipt(pending(), server(**state), snapshot()))
        wrong = server(); wrong['listings'][1]['internal_name'] = 'UpgradeCommodity1'
        self.assertIsNone(commodity_receipt(pending(), wrong, snapshot()))
        self.assertIsNone(commodity_receipt(pending(), server(), snapshot(complete=False)))
        split = snapshot(1); split['containers']['storage']['slots'] = [gold(count=1, container='storage')]
        self.assertIsNotNone(commodity_receipt(pending(), server(), split))
        self.assertFalse(upload_receipt_matches(pending(auction_operation_id='commodity:' + 'b'*32), receipt))

    def test_last_unit_requires_empty_material_snapshot_and_valid_server_identity(self):
        one = pending(before_count=1)
        self.assertIsNotNone(commodity_receipt(one, server(), snapshot(0)))
        self.assertIsNone(commodity_receipt(one, server(), snapshot(1)))
        for submitted in (None, 'invalid', 0, True):
            self.assertIsNone(commodity_receipt(pending(submitted_at=submitted), server(), snapshot()))
        for instance in (None, '', 123, 'x' * 129):
            state = server()
            state['listings'][1]['instance_id'] = instance
            self.assertIsNone(commodity_receipt(pending(), state, snapshot()))


class CommodityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'pending.json'
        self.actions = SimpleNamespace(reader=Mock(), profile=load_profile(), dispatcher=Mock())
        self.runtime = AuctionRuntime(self.actions, journal_path=self.path)
        self.read = Mock(guards=[])
        self.runtime._context = Mock(return_value=self.read)
        self.runtime._open = Mock()
        self.runtime._state = Mock(return_value=ready(steamid=STEAMID))
        self.runtime._commodity_selected = Mock(return_value=(gold(), 300, 3))
        self.actions.dispatcher.invoke.return_value = {'invoked': True}
        self.receipt = commodity_receipt(pending(), server(), snapshot())
        self.runtime._confirm_commodity = Mock(return_value=self.receipt)
        self.payload = {'item': gold(auction_operation_id=OP), 'quantity': 1, 'expected_steamid': STEAMID}

    def test_full_native_chain_has_null_uid_and_durable_intent_before_consumption(self):
        def invoke(calls, *args, **kwargs):
            saved = json.loads(self.path.read_text())['pending'][OP]
            self.assertIsNone(saved['item_uid']); self.assertEqual(saved['before_count'], 3)
            self.assertEqual([c.method for c in calls], ['PanelBase.auction_is_open',
                             'AuctionUI.upload_select', 'AuctionUI.upload_submit'])
            self.assertEqual([c.mutates for c in calls], [False, False, True])
            compile_mailbox(0x10000000, 0x180000000, load_profile(), 0x20000000,
                            0x30000000, 123, calls, [])
            return {'invoked': True}
        self.actions.dispatcher.invoke.side_effect = invoke
        result = self.runtime.execute('auction_upload', self.payload)
        self.assertTrue(result['effect_confirmed']); self.assertEqual(result['quantity'], 1)
        self.assertFalse(self.runtime._pending)
        self.assertEqual(self.runtime._confirmed[OP]['uploaded_instance_id'], 'new-server-instance')

    def test_prewrite_failure_releases_intent_postwrite_unknown_blocks_restart(self):
        for error in (NativeActionError('preflight', mutation_started=False), SnapshotChangedError('changed')):
            self.actions.dispatcher.invoke.side_effect = error
            with self.assertRaises(NativeActionError): self.runtime.execute('auction_upload', self.payload)
            self.assertFalse(self.runtime._pending)
        self.actions.dispatcher.invoke.side_effect = NativeActionError('late', uncertain=True, mutation_started=True)
        with self.assertRaises(AuctionUploadPending): self.runtime.execute('auction_upload', self.payload)
        restarted = AuctionRuntime(self.actions, journal_path=self.path)
        self.assertIsNone(restarted._pending_rows()[0]['instance_id'])
        before = self.actions.dispatcher.invoke.call_count
        with self.assertRaises(NativeActionError): restarted.execute('auction_upload', self.payload)
        self.assertEqual(self.actions.dispatcher.invoke.call_count, before)
        restarted._reconcile(server(listings=[server()['listings'][1]], listing_count=1), snapshot())
        self.assertFalse(restarted._pending)
        self.assertEqual(restarted._confirmed[OP]['uploaded_instance_id'], 'new-server-instance')

    def test_no_uidless_retained_identity_fallback_and_no_missing_operation(self):
        self.runtime._pending[OP] = pending()
        self.runtime._retained_receipt = Mock(side_effect=AssertionError('generated proof forbidden'))
        self.runtime._reconcile(server(listings=[]), snapshot(3))
        self.assertIn(OP, self.runtime._pending)
        self.runtime._pending.clear()
        with self.assertRaises(NativeActionError):
            self.runtime.execute('auction_upload', {'item': gold()})
        self.actions.dispatcher.invoke.assert_not_called()

    def test_native_slot_proof_guards_every_bag_stack_and_checks_total(self):
        # Use the real stack preflight rather than the chain fixture stub.
        self.runtime = AuctionRuntime(self.actions, journal_path=self.path)
        self.actions._selected_inventory = Mock(return_value=(gold(_before_total_count=3), 200, b'', 300, 0))
        read = Mock(guards=[])
        read.integer.side_effect = [7, 3]; read.boolean.return_value = False
        read.singleton.return_value = 100; read.ptr.return_value = 120
        self.actions.reader.array.return_value = ([struct.pack('<Q', x) for x in (200, 250)], b'x'*16)
        self.actions.reader.snapshot.return_value = snapshot(3)
        self.runtime._commodity_selected(self.payload, read)
        self.assertEqual(read.read.call_args_list[-2].args, (200+16, 24))
        self.assertEqual(read.read.call_args_list[-1].args, (250+16, 24))
        self.assertIn((120+32, b'x'*16), read.guards)


class CommodityCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.trading = TradingFake()
        self.config = {**deepcopy(DEFAULT_AUCTION), 'upload_enabled': True, 'sell_enabled': True,
                       'categories': ['gold_bar']}
        self.coordinator = AuctionCoordinator(Path(self.directory.name), self.trading,
            lambda: (True, self.config, STEAMID), start_worker=False)
        self.addCleanup(self.coordinator.close)

    def test_prepare_reference_stable_receipt_maps_real_instance_and_journal_restarts(self):
        c = self.coordinator
        row = c.materialize_upload(gold(), STEAMID)
        self.assertIsNone(c.prepare(row, STEAMID, self.config))
        again = c.materialize_upload(gold(), STEAMID)
        self.assertEqual(row['auction_operation_id'], again['auction_operation_id'])
        c.process_once()
        ticket = c.prepare(again, STEAMID, self.config)
        self.assertTrue(ticket)
        identity = c.submitted(again, STEAMID, ticket)
        restarted = AuctionCoordinator(Path(self.directory.name), self.trading,
            lambda: (True, self.config, STEAMID), start_worker=False)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.journal_error)
        receipt = commodity_receipt(pending(auction_operation_id=row['auction_operation_id']), server(), snapshot())
        blocked = restarted.observe(ready(steamid=STEAMID))
        self.assertEqual(blocked['blocked_keys'], ['GoldBar']); self.assertEqual(blocked['blocked_uids'], [])
        restarted.observe(ready(steamid=STEAMID, confirmed_uploads=[receipt]))
        self.assertEqual(restarted.intents[identity]['instance_id'], 'new-server-instance')
        restarted.process_once()
        self.assertIn(('complete', ticket, 1, 'new-server-instance'), self.trading.calls)
        self.assertEqual(restarted.intents[identity]['phase'], 'resolved')

    def test_wrong_operation_or_instance_never_authorizes_sale(self):
        c = self.coordinator; row = c.materialize_upload(gold(), STEAMID)
        identity = c.submitted(row, STEAMID)
        receipt = commodity_receipt(pending(), server(), snapshot())
        with self.assertRaises(ValidationError): c.confirmed(identity, receipt)
        self.assertEqual(c.intents[identity]['phase'], 'uncertain')
        receipt['uploaded_operation_id'] = row['auction_operation_id']
        c.confirmed(identity, receipt)
        receipt['uploaded_instance_id'] = 'different'
        with self.assertRaises(ValidationError): c.confirmed(identity, receipt)
