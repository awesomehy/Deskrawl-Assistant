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
        self.auth = {'bound': True, 'expired': False, 'verified': True, 'steamid': STEAMID,
                     'wallet_currency': 'CNY'}

    def prepare_upload(self, steamid, item_key, variant):
        self.calls.append(('prepare', steamid, item_key, variant))
        if item_key in self.failure_keys:
            raise RuntimeError('private upstream payload must not appear in status')
        return sha256(item_key.encode()).hexdigest()[:32]

    def complete_upload(self, ticket, count, *, expected_instance_id=None):
        self.calls.append(('complete', ticket, count, expected_instance_id))

    def discard_upload_baseline(self, ticket):
        self.calls.append(('discard', ticket))

    def sale_once(self, config, steamid, *, validate=None):
        if validate is not None and validate() is not True:
            return
        self.calls.append(('sale', deepcopy(config), steamid))

    def state(self):
        return deepcopy(self.auth)

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

    def test_queued_upload_and_due_sale_run_before_optional_tracking_reads(self):
        self.trading.refresh_confirmation = lambda: self.trading.calls.append(('refresh',))
        row = self.row()
        ticket = self.ready(row)
        self.assertEqual([call[0] for call in self.trading.calls], ['prepare'])
        identity = self.coordinator.submitted(row, STEAMID, ticket=ticket)
        self.coordinator.confirmed(identity, self.receipt(row))
        self.coordinator.next_sale = self.now + 30
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1][0], 'complete')
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1][0], 'sale')
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1][0], 'refresh')

    def test_idle_tracking_continues_with_automatic_sales_disabled(self):
        self.trading.refresh_confirmation = lambda: self.trading.calls.append(('refresh',))
        self.running = False
        self.config['sell_enabled'] = False
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [('refresh',)])

    def test_visible_editor_quote_precedes_optional_history_but_not_due_sale(self):
        self.trading.refresh_price_previews = lambda: self.trading.calls.append(('price',)) or True
        self.trading.refresh_confirmation = lambda: self.trading.calls.append(('refresh',))
        self.coordinator.process_once()
        self.assertEqual([call[0] for call in self.trading.calls], ['sale'])
        self.coordinator.process_once()
        self.assertEqual([call[0] for call in self.trading.calls], ['sale', 'price'])
        self.trading.refresh_price_previews = lambda: False
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1][0], 'refresh')

    def test_visible_editor_quote_cannot_delay_short_sale_pacing(self):
        self.trading.refresh_price_previews = lambda: self.trading.calls.append(('price',)) or True
        self.coordinator.next_sale = self.now + 2
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])
        self.now += 2
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1][0], 'sale')

    def test_short_sale_pacing_gap_cannot_start_slow_optional_reads(self):
        self.trading.refresh_confirmation = lambda: self.trading.calls.append(('refresh',))
        self.coordinator.next_sale = self.now + 2
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, [])
        self.now += 2
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1][0], 'sale')
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1][0], 'refresh')

    def test_only_successful_sale_progress_shortens_queue_delay(self):
        self.trading.sale_once = lambda *a, **kw: {'sale_submitted': True}
        self.coordinator.process_once()
        self.assertEqual(self.coordinator.next_sale, self.now + 2)
        self.now += 2
        self.trading.sale_once = lambda *a, **kw: {'status': 'waiting'}
        self.coordinator.process_once()
        self.assertEqual(self.coordinator.next_sale, self.now + 30)
        self.now += 30
        self.trading.sale_once = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('timeout'))
        self.coordinator.process_once()
        self.assertEqual(self.coordinator.next_sale, self.now + 30)

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

    def test_expiry_during_preparation_releases_only_the_steam_preparation_gate(self):
        self.coordinator.prepare(self.row(), STEAMID, self.config)
        def expired(*args):
            self.trading.auth.update(verified=False, steamid=None)
            raise RuntimeError('sensitive expired cookie response')
        self.trading.prepare_upload = expired
        self.coordinator.process_once()
        self.assertFalse(self.coordinator.market_preparation_required(STEAMID))
        observed = self.coordinator.observe({'steamid': STEAMID, 'blocked_uids': ['native-pending']})
        self.assertEqual(observed['blocked_uids'], ['native-pending'])
        self.assertNotIn('sensitive', self.coordinator.state()['error'])
        self.assertIn('游戏拍卖行上传继续', self.coordinator.state()['error'])
        identity = self.coordinator.submitted(self.row(), STEAMID)
        self.assertNotIn(identity, self.coordinator.jobs)
        self.assertIn('uid-a', self.coordinator.observe({'steamid': STEAMID})['blocked_uids'])
        self.coordinator.confirmed(identity, self.receipt(self.row()))
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'resolved')
        self.trading.auth.update(verified=True, steamid=STEAMID)
        self.coordinator.process_once()
        # Re-login cannot create a late baseline or map an unproven asset.
        self.assertFalse(any(call[0] in ('prepare', 'complete') for call in self.trading.calls))

    def test_game_prewrite_rejection_remains_blocked_with_expired_web_login(self):
        self.trading.auth.update(verified=False, steamid=None)
        self.coordinator.process_once()
        identity = self.coordinator.submitted(self.row(), STEAMID)
        self.coordinator.rejected(identity)
        self.assertIn('uid-a', self.coordinator.observe({'steamid': STEAMID})['blocked_uids'])

    def test_expiry_after_game_receipt_releases_kind_only_after_retiring_market_baseline(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        self.coordinator.confirmed(identity, self.receipt(self.row()))
        self.assertIn('DivineBow1', self.coordinator.observe({'steamid': STEAMID})['blocked_keys'])
        original = self.trading.discard_upload_baseline
        def retire(value):
            # The native-confirmed item still fences its kind until durable discard.
            self.assertIn('DivineBow1', self.coordinator.observe({'steamid': STEAMID})['blocked_keys'])
            original(value)
        self.trading.discard_upload_baseline = retire
        self.trading.auth.update(verified=False, steamid=None, expired=True)
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1], ('discard', ticket))
        self.assertEqual(self.coordinator.observe({'steamid': STEAMID})['blocked_keys'], [])
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'resolved')
        self.assertTrue(self.coordinator.intents[identity]['market_skipped'])
        self.assertEqual(self.coordinator.jobs, {})
        self.trading.auth.update(verified=True, steamid=STEAMID)
        self.coordinator.process_once()
        self.assertNotIn('complete', [call[0] for call in self.trading.calls])

    def test_expiry_never_releases_an_unconfirmed_game_write(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        self.trading.auth.update(verified=False, steamid=None)
        self.coordinator.process_once()
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'submitted')
        observed = self.coordinator.observe({'steamid': STEAMID})
        self.assertIn('uid-a', observed['blocked_uids'])
        self.assertIn('DivineBow1', observed['blocked_keys'])
        self.assertNotIn('discard', [call[0] for call in self.trading.calls])

    def test_failed_baseline_discard_preserves_the_game_upload_fence(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        self.coordinator.confirmed(identity, self.receipt(self.row()))
        self.trading.auth.update(verified=False, steamid=None, expired=True)
        self.trading.discard_upload_baseline = lambda ticket: (_ for _ in ()).throw(OSError('disk unavailable'))
        with self.assertRaises(OSError):
            self.coordinator.process_once()
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'confirmed')
        self.assertIn('DivineBow1', self.coordinator.observe({'steamid': STEAMID})['blocked_keys'])

    def test_temporary_wallet_failure_preserves_original_baseline_and_recovers_mapping(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        self.coordinator.confirmed(identity, self.receipt(self.row()))
        before = list(self.trading.calls)
        self.trading.auth.update(verified=False, steamid=None)
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls, before)
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'confirmed')
        self.assertIn('DivineBow1', self.coordinator.observe({'steamid': STEAMID})['blocked_keys'])
        self.trading.auth.update(verified=True, steamid=STEAMID)
        self.coordinator.process_once()
        self.assertEqual(self.trading.calls[-1], ('complete', ticket, 1, 'server-uid-a'))
        self.assertFalse(any(call[0] == 'discard' for call in self.trading.calls))
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'resolved')

    def test_explicit_logout_or_account_change_never_resumes_original_mapping(self):
        for change in ({'verified': False, 'steamid': None, 'bound': False},
                       {'verified': True, 'steamid': OTHER}):
            with self.subTest(change=change):
                self.setUp()
                ticket = self.ready()
                identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
                self.coordinator.confirmed(identity, self.receipt(self.row()))
                self.trading.auth.update(change)
                self.coordinator.process_once()
                self.assertTrue(self.coordinator.intents[identity]['market_skipped'])
                self.assertEqual(self.coordinator.jobs, {})
                self.trading.auth.update(bound=True, expired=False, verified=True, steamid=STEAMID)
                self.coordinator.process_once()
                self.assertFalse(any(call[0] == 'complete' for call in self.trading.calls))

    def test_five_failed_mapping_reads_recover_at_bounded_lower_frequency_without_restart(self):
        ticket = self.ready()
        identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
        self.coordinator.confirmed(identity, self.receipt(self.row()))
        original = self.trading.complete_upload
        calls = []
        def delayed(*args, **kwargs):
            calls.append(1)
            if len(calls) <= 5:
                raise RuntimeError('temporary inventory read failure')
            return original(*args, **kwargs)
        self.trading.complete_upload = delayed
        self.running = False  # Finishing exact mapping remains read-only after stop.
        for attempt in range(5):
            self.coordinator.process_once()
            if attempt < 4:
                self.now += 30
        self.assertEqual(len(calls), 5)
        self.assertEqual(self.coordinator.jobs[identity]['phase'], 'complete')
        self.now += 119
        self.coordinator.process_once()
        self.assertEqual(len(calls), 5)
        self.now += 1
        self.coordinator.process_once()
        self.assertEqual(len(calls), 6)
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'resolved')
        self.assertFalse(any(call[0] in ('discard', 'sale') for call in self.trading.calls))
        self.assertEqual(sum(call[0] == 'prepare' for call in self.trading.calls), 1)

    def test_terminal_baseline_releases_only_confirmed_market_gate_and_keeps_game_receipt(self):
        for reason in ('baseline_expired', 'ambiguous', 'binding_changed'):
            with self.subTest(reason=reason):
                self.setUp()
                ticket = self.ready()
                identity = self.coordinator.submitted(self.row(), STEAMID, ticket)
                self.coordinator.confirmed(identity, self.receipt(self.row()))
                self.trading.upload_mapping_state = lambda *args: {'status': 'terminal', 'reason': reason}
                self.coordinator.process_once()
                receipt = self.coordinator.intents[identity]
                self.assertEqual(receipt['phase'], 'resolved')
                self.assertEqual(receipt['instance_id'], 'server-uid-a')
                self.assertEqual(receipt['market_skip_reason'], reason)
                self.assertEqual(self.coordinator.observe({'steamid': STEAMID})['blocked_keys'], [])
                self.assertIn(('discard', ticket), self.trading.calls)
                self.assertFalse(any(call[0] == 'complete' for call in self.trading.calls))

    def test_late_preparation_result_cannot_resurrect_an_upload_only_job(self):
        self.coordinator.prepare(self.row(), STEAMID, self.config)
        original = self.trading.prepare_upload
        def delayed(*args):
            identity = self.coordinator.submitted(self.row(), STEAMID)
            self.coordinator.confirmed(identity, self.receipt(self.row()))
            return original(*args)
        self.trading.prepare_upload = delayed
        self.coordinator.process_once()
        self.assertEqual(self.coordinator.jobs, {})
        self.assertEqual(next(iter(self.coordinator.intents.values()))['phase'], 'resolved')
        self.assertEqual(self.trading.calls[-1][0], 'discard')
        self.coordinator.process_once()
        self.assertNotIn('complete', [call[0] for call in self.trading.calls])

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
