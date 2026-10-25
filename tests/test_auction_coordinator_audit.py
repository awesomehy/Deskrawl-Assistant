"""Coordinator audit uses fakes only: no Steam session or game writes."""
from copy import deepcopy
import json
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.auction import DEFAULT_AUCTION
from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.models import ValidationError


STEAMID = '76561198000000001'
OTHER = '76561198000000002'


class TradingFake:
    def __init__(self):
        self.calls = []
        self.failure_keys = set()

    def prepare_upload(self, steamid, item_key, variant):
        self.calls.append(('prepare', steamid, item_key, variant))
        if item_key in self.failure_keys:
            raise RuntimeError('private upstream payload must not appear in status')
        return sha256(item_key.encode()).hexdigest()[:32]

    def complete_upload(self, ticket, count, *, expected_instance_id=None):
        self.calls.append(('complete', ticket, count, expected_instance_id))

    def sale_once(self, config, steamid, *, validate=None):
        if validate is not None and validate() is not True:
            return
        self.calls.append(('sale', deepcopy(config), steamid))

    def state(self):
        return {'verified': True, 'steamid': STEAMID, 'wallet_currency': 'CNY'}

    def close(self):
        pass


class CoordinatorAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.trading = TradingFake()
        self.running = True
        self.steamid = STEAMID
        self.config = {**deepcopy(DEFAULT_AUCTION), 'upload_enabled': True,
                       'sell_enabled': True, 'categories': ['divine_equipment'],
                       'retain_count': 3}
        self.now = 1000
        self.clock = patch('deskrawl_assistant.auction_service.time.monotonic', lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.coordinator = AuctionCoordinator(self.directory, self.trading,
            lambda: (self.running, deepcopy(self.config), self.steamid), start_worker=False)
        self.addCleanup(self.coordinator.close)

    @staticmethod
    def row(uid='uid-a', item_key='DivineBow1'):
        return {'item_uid': uid, 'instance_id': 'server-' + uid, 'internal_name': item_key}

    @staticmethod
    def receipt(row):
        return {'uploaded_uid': row['item_uid'], 'uploaded_instance_id': row['instance_id'],
                'uploaded_internal_name': row['internal_name']}

    def ready(self, row=None):
        row = row or self.row()
        self.assertIsNone(self.coordinator.prepare(row, STEAMID, self.config))
        self.coordinator.process_once()
        ticket = self.coordinator.prepare(row, STEAMID, self.config)
        self.assertTrue(ticket)
        return ticket

    def test_preparation_and_status_never_perform_network_on_the_calling_game_thread(self):
        self.assertIsNone(self.coordinator.prepare(self.row(), STEAMID, self.config))
        self.coordinator.state()
        self.coordinator.observe({'available': True, 'steamid': STEAMID})
        self.assertEqual(self.trading.calls, [])
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[0][:3], ('prepare', STEAMID, 'DivineBow1'))

    def test_intent_is_persisted_before_native_submission_and_restarts_block_same_uid(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        raw = json.loads(self.coordinator.path.read_text(encoding='utf-8'))
        self.assertEqual(raw['intents'][identity]['phase'], 'submitted')
        self.assertEqual(raw['intents'][identity]['uid'], 'uid-a')
        self.assertEqual(raw['intents'][identity]['instance_id'], 'server-uid-a')
        with self.assertRaises(ValidationError):
            self.coordinator.submitted(self.row(), STEAMID, ticket)
        reopened = AuctionCoordinator(self.directory, TradingFake(),
            lambda: (False, self.config, STEAMID), start_worker=False)
        self.addCleanup(reopened.close)
        self.assertIn('uid-a', reopened.observe({'steamid': STEAMID})['blocked_uids'])
        self.assertFalse(reopened.journal_error)
        self.assertIsNone(reopened.prepare(self.row(), STEAMID, self.config))

    def test_stop_finishes_exact_asset_mapping_but_does_not_begin_sale_or_preparation(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        self.coordinator.confirmed(identity, self.receipt(self.row()))
        self.running = False
        self.coordinator.prepare(self.row('uid-b', 'DivineHelm1'), STEAMID, self.config)
        self.coordinator.process_once()
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [
            ('prepare', STEAMID, 'DivineBow1', 'normal'), ('complete', ticket, 1, 'server-uid-a')])
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'resolved')

    def test_steam_account_or_config_change_prevents_preparing_a_stale_job(self):
        self.coordinator.prepare(self.row(), STEAMID, self.config)
        self.steamid = OTHER
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])
        self.assertNotIn('private', self.coordinator.state()['error'])
        self.steamid = STEAMID
        self.now += 31
        self.coordinator.prepare(self.row(), STEAMID, self.config)
        self.config['price_rule'] = 'highest_buy'
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])

    def test_failed_preparation_blocks_only_that_uid_and_another_kind_can_progress(self):
        self.trading.failure_keys.add('DivineBow1')
        self.coordinator.prepare(self.row(), STEAMID, self.config)
        self.coordinator.prepare(self.row('uid-b', 'DivineHelm1'), STEAMID, self.config)
        self.coordinator.process_once()
        blocked = self.coordinator.observe({'steamid': STEAMID})['blocked_uids']
        self.assertIn('uid-a', blocked)
        self.assertNotIn('uid-b', blocked)
        self.assertNotIn('private upstream', self.coordinator.state()['error'])
        self.coordinator.process_once()
        self.assertEqual(self.coordinator.prepare(self.row('uid-b', 'DivineHelm1'), STEAMID, self.config),
                         sha256(b'DivineHelm1').hexdigest()[:32])

    def test_native_pending_guards_are_combined_with_coordinator_guards(self):
        identity = self.coordinator.submitted(self.row(), STEAMID)
        result = self.coordinator.observe({'steamid': STEAMID, 'blocked_uids': ['native-pending'],
                                          'blocked_keys': ['NativeItem']})
        self.assertEqual(result['blocked_uids'], ['native-pending', 'uid-a'])
        self.assertIn('NativeItem', result['blocked_keys'])
        self.coordinator.confirmed(identity, self.receipt(self.row()))

    def test_mismatched_native_instance_keeps_the_fence_and_never_maps_or_sells(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        receipt = {**self.receipt(self.row()), 'uploaded_instance_id': 'different-server-instance'}
        with self.assertRaises(ValidationError):
            self.coordinator.confirmed(identity, receipt)
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'uncertain')
        self.assertNotIn('complete', [job['phase'] for job in self.coordinator.jobs.values()])
        self.assertIn('uid-a', self.coordinator.observe({'steamid': STEAMID})['blocked_uids'])

    def test_changed_server_instance_invalidates_an_already_ready_baseline(self):
        self.ready()
        changed = {**self.row(), 'instance_id': 'changed-server-instance'}
        self.assertIsNone(self.coordinator.prepare(changed, STEAMID, self.config))
        self.assertEqual(next(iter(self.coordinator.jobs.values()))['phase'], 'prepare')

    def test_stopping_during_network_lookup_cancels_sale_at_submit_boundary(self):
        original = self.trading.sale_once
        def delayed(config, steamid, *, validate=None):
            self.running = False
            return original(config, steamid, validate=validate)
        self.trading.sale_once = delayed
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])

    def test_sale_preserves_explicit_category_and_exclusion_policy(self):
        self.coordinator.process_once()
        sale = next(call for call in self.trading.calls if call[0] == 'sale')
        self.assertEqual(sale[1], self.config)

    def test_upload_off_blocks_new_preparation_and_sale_even_with_stale_sale_switch(self):
        self.config['upload_enabled'] = False
        self.coordinator.prepare(self.row(), STEAMID, self.config)
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])

    def test_upload_off_still_completes_submitted_asset_mapping_without_new_sale(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        self.coordinator.confirmed(identity, self.receipt(self.row()))
        self.config['upload_enabled'] = False
        self.coordinator.process_once()
        self.coordinator.process_once()
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'resolved')
        self.assertEqual(self.trading.calls, [
            ('prepare', STEAMID, 'DivineBow1', 'normal'), ('complete', ticket, 1, 'server-uid-a')])

    def test_disabling_upload_during_price_lookup_prevents_sale_submission(self):
        original = self.trading.sale_once
        def delayed(config, steamid, *, validate=None):
            self.config['upload_enabled'] = False
            return original(config, steamid, validate=validate)
        self.trading.sale_once = delayed
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])

    def test_expired_ready_baseline_is_refreshed_before_any_native_upload(self):
        self.ready()
        self.now += 601
        self.assertIsNone(self.coordinator.prepare(self.row(), STEAMID, self.config),
                          'Steam baseline tickets expire after 600 seconds; do not upload using an expired ticket')


if __name__ == '__main__':
    unittest.main()
