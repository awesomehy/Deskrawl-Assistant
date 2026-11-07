"""Copied/native-profile fixtures only; no game attach or network operation."""
from copy import deepcopy
import json
import struct
import time
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.auction_runtime import AuctionRuntime, AuctionUploadPending, KINDS, auction_eligibility
from deskrawl_assistant.game_actions import GameActions, CAPABILITIES, GameBusyError
from deskrawl_assistant.native_dispatcher import NativeActionError, load_profile, compile_mailbox, REJECTED
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError


def item(**changes):
    return {**dict(container='inventory', slot_index=0, count=1, item_uid='uid-a',
                instance_id='instance-a', internal_name='DivineHelm', identity_verified=True,
                locked=False, slot_locked=False, bound=False, upgrade_level=0,
                socket_count=0, item_type=3, base_rarity=4, equip_slot=1,
                steam_retired=False, is_black_mist=False), **changes}


def ready(**changes):
    return {**dict(available=True, enabled=True, listings=[], listing_count=0,
                capacity=8, busy=False, opened=True, needs_refresh=False,
                selection_occupied=False, pending=False, blocked_uids=[], _ui=100, _slot=200,
                steamid='76561198000000000', steamid_verified=True,
                _refresh_key=('session-a', 100, '76561198000000000'),
                _list_versions=((500, 0), (600, 0))), **changes}


class EligibilityTests(unittest.TestCase):
    def test_accepted_native_categories(self):
        categories = ({}, {'item_type': 2, 'effect': 1, 'base_rarity': 3},
                      {'item_type': 5, 'base_rarity': 3}, {'item_type': 5, 'base_rarity': 5},
                      {'item_type': 7, 'base_rarity': 3},
                      {'base_rarity': 3, 'is_black_mist': True, 'black_mist_revealed': False})
        for changes in categories:
            with self.subTest(changes=changes):
                self.assertTrue(auction_eligibility(item(**changes))['eligible'])

    def test_missing_uid_cannot_be_replaced_by_slot_or_name(self):
        row = item(item_uid='', instance_id='', item_type=7, base_rarity=3)
        state = auction_eligibility(row)
        self.assertFalse(state['eligible']); self.assertEqual(state['reason_code'], 'identity_unavailable')

    def test_uploads_ignore_lock_state_without_mutating_it(self):
        for changes in ({'locked': True}, {'slot_locked': True}, {'locked': None},
                        {'slot_locked': None}, {'locked': 1}):
            row = item(**changes); previous = deepcopy(row)
            self.assertTrue(auction_eligibility(row)['eligible'])
            self.assertEqual(row, previous)
        row = item(); row.pop('locked'); row.pop('slot_locked')
        self.assertTrue(auction_eligibility(row)['eligible'])

    def test_preliminary_only_defers_missing_immutable_resource_fields(self):
        row = item(item_type=2, base_rarity=3)
        row.pop('steam_retired')
        self.assertFalse(auction_eligibility(row)['eligible'])
        self.assertTrue(auction_eligibility(row, preliminary=True)['eligible'])
        for field in ('bound', 'upgrade_level', 'socket_count', 'instance_id'):
            missing = deepcopy(row); missing.pop(field)
            self.assertFalse(auction_eligibility(missing, preliminary=True)['eligible'])
        self.assertFalse(auction_eligibility(dict(row, steam_retired=None), preliminary=True)['eligible'])
        self.assertFalse(auction_eligibility(dict(row, effect=None), preliminary=True)['eligible'])

    def test_independent_fresh_snapshot_identity_marker_is_accepted(self):
        row = item(auction_identity_verified=True)
        row.pop('identity_verified')
        self.assertTrue(auction_eligibility(row, preliminary=True)['eligible'])
        row['auction_identity_verified'] = False
        self.assertFalse(auction_eligibility(row, preliminary=True)['eligible'])

    def test_bound_equipped_modified_retired_and_revealed_fail_closed(self):
        for changes in ({'bound': True}, {'bound': None}, {'is_equipped': True},
                        {'upgrade_level': 1}, {'upgrade_level': None}, {'socket_count': 1},
                        {'steam_retired': True}, {'steam_retired': None},
                        {'base_rarity': 3, 'is_black_mist': True, 'black_mist_revealed': True},
                        {'base_rarity': 3, 'is_black_mist': True, 'black_mist_revealed': None},
                        {'base_rarity': 3, 'is_black_mist': True, 'black_mist_revealed': False, 'equip_slot': 11},
                        {'count': 2}, {'identity_verified': False}, {'issues': ['incomplete']}):
            with self.subTest(changes=changes):
                self.assertFalse(auction_eligibility(item(**changes))['eligible'])
        self.assertFalse(auction_eligibility(item(), equipped_uids={'uid-a'})['eligible'])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'pending.json'
        self.actions = SimpleNamespace(reader=Mock(), profile=load_profile(), dispatcher=Mock())
        self.runtime = AuctionRuntime(self.actions, journal_path=self.path)
        self.read = Mock(guards=[])
        self.copy = Mock(action_guards=[])
        self.runtime._context = Mock(return_value=self.read)
        self.runtime._open = Mock(return_value=ready())
        self.runtime._state = Mock(return_value=ready())
        self.runtime._selected = Mock(return_value=(item(), 300, 400, 500, self.copy))
        self.actions.dispatcher.invoke.return_value = {'invoked': True, 'completed': True, 'sequence': 8}
        self.runtime._confirm = Mock(return_value={'uploaded_instance_id': 'instance-a'})

    def test_exact_full_ui_chain_and_native_eligibility_before_submit(self):
        result = self.runtime.execute('auction_upload', {'item': item()})
        self.assertTrue(result['effect_confirmed']); self.assertTrue(result['native_verified'])
        calls = self.actions.dispatcher.invoke.call_args.args[0]
        self.assertEqual([c.method for c in calls],
                         ['PanelBase.auction_is_open', 'ItemData.auction_tradable',
                          'AuctionUI.upload_select', 'AuctionUI.upload_submit'])
        self.assertTrue(all(c.require_true for c in calls[:3]))
        self.assertEqual([c.mutates for c in calls], [False, False, False, True])
        self.assertFalse(any('CloudClient' in c.method for c in calls))
        self.assertFalse(any('unlock' in c.method.casefold() for c in calls))
        self.assertEqual(json.loads(self.path.read_text())['pending'], {})
        # Full production signatures accept the exact selected managed pointers.
        compile_mailbox(0x10000000, 0x180000000, load_profile(), 0x20000000,
                        0x30000000, 123, calls, [])

    def test_inflight_is_durable_before_mutator_and_prevents_restart_retry(self):
        def submitted(*args, **kwargs):
            saved = json.loads(self.path.read_text())['pending']
            self.assertIn('instance-a', saved)
            raise NativeActionError('timeout', uncertain=True, mutation_started=True)
        self.actions.dispatcher.invoke.side_effect = submitted
        with self.assertRaises(NativeActionError):
            self.runtime.execute('auction_upload', {'item': item()})
        restarted = AuctionRuntime(self.actions, journal_path=self.path)
        self.assertIn('instance-a', restarted._pending)
        self.actions.dispatcher.invoke.reset_mock()
        with self.assertRaises(NativeActionError) as captured:
            restarted.execute('auction_upload', {'item': item()})
        self.assertTrue(captured.exception.uncertain)
        self.actions.dispatcher.invoke.assert_not_called()

    def test_invocation_ack_alone_does_not_confirm_upload(self):
        self.runtime._confirm.side_effect = NativeActionError('unconfirmed', uncertain=True)
        with self.assertRaises(NativeActionError):
            self.runtime.execute('auction_upload', {'item': item()})
        with self.assertRaises(NativeActionError):
            self.runtime.execute('auction_upload', {'item': item()})
        self.assertEqual(self.actions.dispatcher.invoke.call_count, 1)

    def test_receipt_timeout_retains_native_failure_and_durable_fence_after_restart(self):
        self.runtime._refreshed = ready()['_refresh_key']
        self.runtime._confirm.side_effect = AuctionUploadPending('拍卖行没有响应。 请重试。')
        with self.assertRaises(AuctionUploadPending) as caught:
            self.runtime.execute('auction_upload', {'item': item()})
        self.assertTrue(caught.exception.uncertain)
        self.assertTrue(caught.exception.mutation_started)
        self.assertIsNone(self.runtime._refreshed)
        saved = AuctionRuntime(self.actions, journal_path=self.path)
        self.assertEqual(saved._pending['instance-a']['last_status'], '拍卖行没有响应。 请重试。')
        with self.assertRaises(NativeActionError):
            saved.execute('auction_upload', {'item': item()})
        self.assertEqual(self.actions.dispatcher.invoke.call_count, 1)

    def test_non_mutating_rejections_can_replan_without_pending_consumption(self):
        self.actions.dispatcher.invoke.side_effect = NativeActionError('not tradable', mutation_started=False)
        with self.assertRaises(NativeActionError):
            self.runtime.execute('auction_upload', {'item': item()})
        self.assertFalse(self.runtime._pending)
        self.assertEqual(json.loads(self.path.read_text())['pending'], {})

    def test_stale_copies_before_publish_do_not_create_permanent_pending(self):
        self.read.finish.side_effect = [None, SnapshotChangedError('changed')]
        with self.assertRaises(NativeActionError) as captured:
            self.runtime.execute('auction_upload', {'item': item()})
        self.assertFalse(captured.exception.mutation_started)
        self.assertFalse(self.runtime._pending)
        self.actions.dispatcher.invoke.assert_not_called()

    def test_full_capacity_disabled_or_player_selection_never_submits(self):
        for changes in ({'listing_count': 8}, {'enabled': False}, {'busy': True},
                        {'selection_occupied': True}):
            self.runtime._state.return_value = ready(**changes)
            with self.assertRaises(NativeActionError) as captured:
                self.runtime.execute('auction_upload', {'item': item()})
            self.assertFalse(captured.exception.mutation_started)
            self.actions.dispatcher.invoke.assert_not_called()
            self.runtime._selected.assert_not_called()

    def test_no_uid_is_rejected_before_opening_or_native_calls(self):
        with self.assertRaises(NativeActionError):
            self.runtime.execute('auction_upload', {'item': item(item_uid='')})
        self.runtime._open.assert_not_called(); self.actions.dispatcher.invoke.assert_not_called()

    def test_upload_rejects_quantity_stacks_before_opening_or_native_calls(self):
        for quantity in (0, 2, True, 1.0):
            with self.assertRaises(NativeActionError) as exc:
                self.runtime.execute('auction_upload', {'item': item(), 'quantity': quantity})
            self.assertFalse(exc.exception.mutation_started)
        self.runtime._open.assert_not_called(); self.actions.dispatcher.invoke.assert_not_called()

    def test_expected_account_is_checked_again_after_open_before_native_submit(self):
        expected = '76561198000000000'
        self.runtime._open.return_value = ready(steamid=expected, steamid_verified=True)
        for changes in ({'steamid': '76561198000000001', 'steamid_verified': True},
                        {'steamid': expected, 'steamid_verified': False},
                        {'steamid': None, 'steamid_verified': False}):
            with self.subTest(changes=changes):
                self.runtime._state.return_value = ready(**changes)
                with self.assertRaises(NativeActionError) as exc:
                    self.runtime.execute('auction_upload', {'item': item(), 'expected_steamid': expected})
                self.assertFalse(exc.exception.mutation_started)
                self.assertFalse(self.runtime._pending)
                self.assertFalse(self.path.exists())
                self.runtime._selected.assert_not_called()
                self.actions.dispatcher.invoke.assert_not_called()
        self.assertEqual(self.runtime._open.call_count, 3)

    def test_expected_matching_account_allows_complete_native_chain(self):
        expected = '76561198000000000'
        self.runtime._state.return_value = ready(steamid=expected, steamid_verified=True)
        result = self.runtime.execute('auction_upload', {'item': item(), 'expected_steamid': expected})
        self.assertTrue(result['effect_confirmed'])
        self.actions.dispatcher.invoke.assert_called_once()

    def test_upload_only_unknown_account_is_explicit_prewrite_rejection(self):
        self.runtime._state.return_value = ready(steamid=None, steamid_verified=False)
        with self.assertRaises(NativeActionError) as exc:
            self.runtime.execute('auction_upload', {'item': item(), 'expected_steamid': None})
        self.assertFalse(exc.exception.mutation_started)
        self.runtime._selected.assert_not_called()
        self.actions.dispatcher.invoke.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_upload_only_known_account_uses_native_eligibility(self):
        result = self.runtime.execute('auction_upload', {'item': item(), 'expected_steamid': None})
        self.assertTrue(result['effect_confirmed'])
        self.actions.dispatcher.invoke.assert_called_once()

    def test_invalid_expected_account_rejected_before_open(self):
        for expected in ('', 'not-a-steamid', 76561198000000000, '７６５６１１９８０００００００００'):
            with self.subTest(expected=expected), self.assertRaises(NativeActionError) as exc:
                self.runtime.execute('auction_upload', {'item': item(), 'expected_steamid': expected})
            self.assertFalse(exc.exception.mutation_started)
        self.runtime._open.assert_not_called()
        self.actions.dispatcher.invoke.assert_not_called()

    def test_corrupt_pending_journal_fails_closed_and_is_not_overwritten(self):
        self.path.write_text('not-json')
        runtime = AuctionRuntime(self.actions, journal_path=self.path)
        with self.assertRaises(NativeActionError):
            runtime.execute('auction_open', {})
        self.assertEqual(self.path.read_text(), 'not-json')

    def retained_case(self):
        row = item()
        self.runtime._pending = {'instance-a': dict(item_uid='uid-a', internal_name=row['internal_name'],
            steamid=ready()['steamid'])}
        snapshot = {'complete': True, 'containers': {'inventory': {'slots': [row]}}}
        return ready(server_checked_at=1000), snapshot

    def test_two_independent_server_responses_and_exact_item_release_failed_upload(self):
        state, snapshot = self.retained_case()
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1000):
            self.runtime._reconcile(state, snapshot)
        self.assertIn('instance-a', self.runtime._pending)
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1015):
            self.runtime._reconcile(dict(state, server_checked_at=1015), snapshot)
        self.assertFalse(self.runtime._pending)
        self.assertFalse(self.runtime._confirmed)  # Retained is not uploaded or sold.
        restored = AuctionRuntime(self.actions, journal_path=self.path)
        receipt = restored._retained['instance-a']
        self.assertEqual(receipt['outcome'], 'retained_in_game')
        self.assertEqual(receipt['retry_after'], 1315)
        self.actions.dispatcher.invoke.assert_not_called()

    def test_polling_one_cached_response_never_releases_upload(self):
        state, snapshot = self.retained_case()
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1000):
            self.runtime._reconcile(state, snapshot)
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1015):
            self.runtime._reconcile(state, snapshot)
        self.assertIn('instance-a', self.runtime._pending)

    def test_snapshot_preserves_first_receipt_while_second_owned_get_is_loading(self):
        state, snapshot = self.retained_case()
        self.actions.reader.snapshot.return_value = snapshot
        self.runtime._refresh_wait = {'key': state['_refresh_key']}
        self.runtime._state.side_effect = [
            dict(state, _response_ready=True),
            dict(state, loading=True, action_busy=False, busy=True, needs_refresh=True,
                 pending=True, listings=None),
            dict(state, _response_ready=True)]
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1000):
            self.runtime.snapshot()
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1014):
            self.assertTrue(self.runtime.snapshot()['pending'])
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1015):
            result = self.runtime.snapshot()
        self.assertFalse(result['pending'])
        self.assertEqual(result['pending_uploads'], [])
        self.assertIn('uid-a', result['blocked_uids'])
        self.assertEqual(result['retained_uploads'][0]['outcome'], 'retained_in_game')
        self.assertEqual(result['confirmed_uploads'], [])
        self.actions.dispatcher.invoke.assert_not_called()

    def test_upload_busy_or_unrelated_get_invalidates_first_retained_receipt(self):
        for action_busy, owned in ((True, True), (False, False)):
            with self.subTest(action_busy=action_busy, owned=owned):
                state, snapshot = self.retained_case()
                self.runtime._negative_checks.clear()
                self.actions.reader.snapshot.return_value = snapshot
                self.runtime._refresh_wait = {'key': state['_refresh_key']} if owned else None
                self.runtime._state.side_effect = [
                    dict(state, _response_ready=True),
                    dict(state, loading=True, action_busy=action_busy, busy=True,
                         needs_refresh=True, pending=True, listings=None),
                    dict(state, _response_ready=True)]
                with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1000):
                    self.runtime.snapshot()
                with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1014):
                    self.runtime.snapshot()
                with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1015):
                    self.assertTrue(self.runtime.snapshot()['pending'])

    def test_incomplete_conflicting_or_unknown_results_do_not_release_upload(self):
        variants = [({'busy': True}, {}), ({'needs_refresh': True}, {}),
            ({'selection_occupied': True}, {}), ({'steamid_verified': False}, {}),
            ({'steamid': '76561198000000001'}, {}), ({'server_checked_at': 900}, {}),
            ({'listings': None}, {}), ({'listings': [{'instance_id': 'instance-a'}]}, {}),
            ({}, {'complete': False}), ({}, {'containers': {}})]
        for changes, snapshot_changes in variants:
            with self.subTest(changes=changes, snapshot_changes=snapshot_changes):
                state, snapshot = self.retained_case()
                self.runtime._negative_checks.clear()
                with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1000):
                    self.runtime._reconcile(state, snapshot)
                with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1015):
                    self.runtime._reconcile({**state, 'server_checked_at': 1015, **changes},
                                            dict(snapshot, **snapshot_changes))
                self.assertIn('instance-a', self.runtime._pending)

    def test_same_name_uid_or_instance_alone_is_not_retained_identity(self):
        for changes in ({'item_uid': 'other'}, {'instance_id': 'other'},
                        {'internal_name': 'other'}, {'count': 2}, {'identity_verified': False},
                        {'issues': ['incomplete']}):
            with self.subTest(changes=changes):
                state, snapshot = self.retained_case()
                self.runtime._negative_checks.clear()
                snapshot['containers']['inventory']['slots'][0].update(changes)
                with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1000):
                    self.runtime._reconcile(state, snapshot)
                with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1015):
                    self.runtime._reconcile(dict(state, server_checked_at=1015), snapshot)
                self.assertIn('instance-a', self.runtime._pending)

    def test_retained_cooldown_prevents_native_resubmission_after_restart(self):
        self.runtime._retained['instance-a'] = dict(item_uid='uid-a', instance_id='instance-a',
            internal_name=item()['internal_name'], steamid=ready()['steamid'],
            outcome='retained_in_game', checked_at=1000, retry_after=1300)
        self.runtime._persist()
        restored = AuctionRuntime(self.actions, journal_path=self.path)
        restored._open = self.runtime._open
        restored._context = self.runtime._context
        restored._state = self.runtime._state
        restored._selected = self.runtime._selected
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1015):
            with self.assertRaises(NativeActionError) as caught:
                restored.execute('auction_upload', {'item': item()})
        self.assertFalse(caught.exception.mutation_started)
        self.actions.dispatcher.invoke.assert_not_called()

    def test_pre_submit_list_and_interrupted_proof_cannot_authorize_recovery(self):
        state, snapshot = self.retained_case()
        self.runtime._pending['instance-a']['submitted_at'] = 1005
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1005):
            self.runtime._reconcile(state, snapshot)
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1015):
            self.runtime._reconcile(dict(state, server_checked_at=1015), snapshot)
        self.assertIn('instance-a', self.runtime._pending)
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1016):
            self.runtime._reconcile(dict(state, busy=True, server_checked_at=1015), snapshot)
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1030):
            self.runtime._reconcile(dict(state, server_checked_at=1030), snapshot)
        self.assertIn('instance-a', self.runtime._pending)


class PendingSelectionRecoveryTests(unittest.TestCase):
    """A failed native upload leaves its own selection in place."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'pending.json'
        self.actions = SimpleNamespace(reader=Mock(), profile=load_profile(), dispatcher=Mock())
        self.runtime = AuctionRuntime(self.actions, journal_path=self.path)
        self.read = Mock(guards=[])
        self.read.ptr.return_value = 300
        self.read.verify.side_effect = lambda pointer, name: pointer
        self.actions.reader.asset_name.return_value = item()['internal_name']
        self.runtime._context = Mock(return_value=self.read)
        self.state = ready(selection_occupied=True, selected_uid='uid-a', pending=True)
        self.runtime._state = Mock(side_effect=lambda read: dict(self.state))
        self.row = item()
        self.snapshot = {'complete': True, 'containers': {'inventory': {'slots': [self.row]}}}
        self.actions.reader.snapshot.side_effect = lambda: deepcopy(self.snapshot)
        self.runtime._pending = {'instance-a': dict(item_uid='uid-a',
            internal_name=self.row['internal_name'], steamid=self.state['steamid'], submitted_at=990)}
        self.runtime._persist()
        self.runtime._wait_open = Mock(side_effect=self.fresh_response)
        self.actions.dispatcher.invoke.side_effect = self.clear_selection

    def fresh_response(self, *args):
        self.runtime._refreshed = self.state['_refresh_key']
        self.runtime._refreshed_at = time.time()
        return dict(self.runtime.snapshot(), kind='auction_open', effect_confirmed=True)

    def clear_selection(self, calls, *args, **kwargs):
        self.assertEqual(len(calls), 1)
        self.assertIn(calls[0].method, ('AuctionUI.refresh_list', 'AuctionUploadSlotUI.clear'))
        if calls[0].method == 'AuctionUploadSlotUI.clear':
            self.state.update(selection_occupied=False, selected_uid='')
        return {'completed': True}

    def refresh(self, now):
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=now):
            return self.runtime._open(refresh_only=True)

    def clear_calls(self):
        return [c for call in self.actions.dispatcher.invoke.call_args_list
                for c in call.args[0] if c.method == 'AuctionUploadSlotUI.clear']

    def test_two_server_receipts_clear_only_own_failed_selection_then_settle(self):
        self.assertTrue(self.refresh(1000)['pending'])
        self.assertFalse(self.clear_calls())
        result = self.refresh(1015)
        self.assertFalse(result['pending'])
        self.assertEqual([c.method for call in self.actions.dispatcher.invoke.call_args_list
                          for c in call.args[0]],
                         ['AuctionUI.refresh_list', 'AuctionUI.refresh_list', 'AuctionUploadSlotUI.clear'])
        self.assertFalse(self.clear_calls()[0].mutates)
        self.assertEqual(self.clear_calls()[0].instance, self.state['_slot'])
        self.assertEqual(self.snapshot['containers']['inventory']['slots'], [item()])
        self.assertFalse(self.runtime._confirmed)
        restored = AuctionRuntime(self.actions, journal_path=self.path)
        self.assertFalse(restored._pending)
        self.assertEqual(restored._retained['instance-a']['outcome'], 'retained_in_game')
        self.assertEqual(restored._retained['instance-a']['retry_after'], 1315)

    def test_read_only_snapshots_collect_proof_but_never_clear_or_release_occupied_slot(self):
        for now in (1000, 1015):
            with patch('deskrawl_assistant.auction_runtime.time.time', return_value=now):
                result = self.fresh_response()
                self.assertTrue(result['pending'])
        self.assertIn('instance-a', self.runtime._pending)
        self.actions.dispatcher.invoke.assert_not_called()

    def test_one_cached_response_never_authorizes_slot_cleanup(self):
        self.refresh(1000)
        self.runtime._wait_open.side_effect = lambda *args: self.fresh_response_at(1000)
        self.assertTrue(self.refresh(1015)['pending'])
        self.assertFalse(self.clear_calls())

    def fresh_response_at(self, checked):
        self.runtime._refreshed = self.state['_refresh_key']
        self.runtime._refreshed_at = checked
        return dict(self.runtime.snapshot(), kind='auction_open', effect_confirmed=True)

    def test_conflicting_incomplete_or_missing_identity_never_clears_selection(self):
        variants = [({'complete': False}, {}, {}),
            ({}, {'item_uid': 'other'}, {}), ({}, {'instance_id': 'other'}, {}),
            ({}, {'count': 2}, {}), ({}, {'identity_verified': False}, {}),
            ({}, {'container': 'equipped'}, {}),
            ({}, {}, {'listings': [{'instance_id': 'instance-a'}]})]
        for snapshot_change, item_change, state_change in variants:
            with self.subTest(change=(snapshot_change, item_change, state_change)):
                self.runtime._negative_checks.clear()
                self.snapshot = {'complete': True, 'containers': {'inventory': {'slots': [item()]}}}
                self.state.update(listings=[])
                self.refresh(1000)
                self.snapshot.update(snapshot_change)
                self.snapshot['containers']['inventory']['slots'][0].update(item_change)
                self.state.update(state_change)
                self.assertTrue(self.refresh(1015)['pending'])
                self.assertFalse(self.clear_calls())

    def test_selection_or_account_changed_before_clear_is_not_touched(self):
        for change in ({'selected_uid': 'other'}, {'steamid': '76561198000000001'},
                       {'steamid_verified': False}):
            with self.subTest(change=change):
                self.runtime._negative_checks.clear()
                self.state = ready(selection_occupied=True, selected_uid='uid-a', pending=True)
                self.refresh(1000)
                self.state.update(change)
                with self.assertRaises(GameBusyError):
                    self.refresh(1015)
                self.assertFalse(self.clear_calls())

    def test_changed_native_item_or_failed_clear_does_not_settle_receipt(self):
        self.refresh(1000)
        self.actions.reader.asset_name.return_value = 'other'
        with self.assertRaises(GameBusyError):
            self.refresh(1015)
        self.assertFalse(self.clear_calls())
        self.actions.reader.asset_name.return_value = self.row['internal_name']
        self.actions.dispatcher.invoke.side_effect = lambda *a, **k: {'completed': True}
        self.assertTrue(self.refresh(1016)['pending'])  # Native slot did not actually clear.
        self.assertEqual(len(self.clear_calls()), 1)
        self.assertFalse(self.runtime._retained)

    def test_result_must_still_match_after_clear(self):
        self.refresh(1000)
        def clear_and_move(calls, *args, **kwargs):
            if calls[0].method == 'AuctionUploadSlotUI.clear':
                self.state.update(selection_occupied=False, selected_uid='')
                self.snapshot['containers']['inventory']['slots'] = []
            return {'completed': True}
        self.actions.dispatcher.invoke.side_effect = clear_and_move
        self.assertTrue(self.refresh(1015)['pending'])
        self.assertFalse(self.runtime._retained)
        self.assertFalse(self.runtime._confirmed)

    def test_delayed_success_is_confirmed_without_clearing_or_repeating_upload(self):
        self.refresh(1000)
        self.snapshot['containers']['inventory']['slots'] = []
        self.state['listings'] = [dict(instance_id='instance-a', internal_name=self.row['internal_name'],
            received=False, pending=True, listed_at=1005)]
        result = self.refresh(1015)
        self.assertFalse(result['pending'])
        self.assertFalse(self.clear_calls())
        self.assertIn('instance-a', self.runtime._confirmed)
        self.assertFalse(self.runtime._retained)

    def test_failed_clear_keeps_durable_fence_across_restart(self):
        self.refresh(1000)
        def reject_clear(calls, *args, **kwargs):
            if calls[0].method == 'AuctionUploadSlotUI.clear':
                raise NativeActionError('clear unconfirmed', uncertain=True)
            return {'completed': True}
        self.actions.dispatcher.invoke.side_effect = reject_clear
        with self.assertRaises(NativeActionError):
            self.refresh(1015)
        restored = AuctionRuntime(self.actions, journal_path=self.path)
        self.assertIn('instance-a', restored._pending)
        self.assertFalse(restored._confirmed)
        self.assertFalse(restored._retained)

    def test_slot_guard_change_before_dispatch_preserves_pending_receipt(self):
        self.refresh(1000)
        def changed_item(pointer):
            self.read.finish.side_effect = SnapshotChangedError('slot changed')
            return self.row['internal_name']
        self.actions.reader.asset_name.side_effect = changed_item
        with self.assertRaises(SnapshotChangedError):
            self.refresh(1015)
        self.assertFalse(self.clear_calls())
        self.assertIn('instance-a', self.runtime._pending)
        self.assertFalse(self.runtime._retained)


class ReadOnlyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.actions = SimpleNamespace(reader=Mock(), profile=load_profile())
        self.actions.reader.session_id = 'session-a'
        self.runtime = AuctionRuntime(self.actions, journal_path=Path(self.temp.name) / 'journal.json')
        self.read = Mock()
        self.read.singleton.side_effect = lambda name: {'AuctionUI': 1000, 'CloudClient': 3000}[name]
        self.read.verify.side_effect = lambda p, name: p
        self.read.ptr.side_effect = lambda addr: {1104: 2000, 1200: 0, 2072: 0, 2064: 0, 3128: 4000}[addr]
        self.actions.reader.string.side_effect = lambda p: '76561198000000000' if p == 4000 else ''
        self.runtime._listings = Mock(return_value=[])
        self.runtime._list_versions = Mock(return_value=((5000, 0), (6000, 0)))

    def test_unopened_state_is_unknown_not_empty_and_does_not_open_ui(self):
        self.read.boolean.return_value = False
        state = self.runtime._state(self.read)
        self.assertTrue(state['available']); self.assertIsNone(state['enabled'])
        self.assertIsNone(state['listing_count']); self.assertIsNone(state['listings'])
        self.assertTrue(state['needs_refresh'])
        self.runtime._listings.assert_not_called()
        self.actions.reader.memory.write.assert_not_called()

    def test_constructor_empty_lists_and_default_enabled_are_not_server_receipts(self):
        self.read.boolean.side_effect = lambda address: address in (1040, 1195)
        state = self.runtime._state(self.read)
        self.assertIsNone(state['opened'])
        self.assertIsNone(state['listing_count'])
        self.assertIsNone(state['enabled'])
        self.assertTrue(state['needs_refresh'])
        self.assertNotIn(1040, [call.args[0] for call in self.read.boolean.call_args_list])

    def test_empty_success_response_is_confirmed_by_both_list_versions(self):
        key = ('session-a', 1000, '76561198000000000')
        self.runtime._refresh_wait = {'key': key, 'versions': ((5000, 0), (6000, 0))}
        self.runtime._list_versions.return_value = ((5000, 1), (6000, 1))
        self.read.boolean.side_effect = lambda address: address == 1195
        self.runtime._context = Mock(return_value=self.read)
        state = self.runtime.snapshot()
        self.assertEqual(state['listings'], [])
        self.assertEqual(state['listing_count'], 0)
        self.assertTrue(state['enabled'])
        self.assertFalse(state['needs_refresh'])
        self.assertEqual(self.runtime._refreshed, key)
        self.assertFalse(any(key.startswith('_') for key in state))

    def test_read_polling_never_renews_the_last_server_refresh_time(self):
        self.runtime._refresh_wait = {'key': ('session-a', 1000, '76561198000000000'),
                                      'versions': ((5000, 0), (6000, 0))}
        self.runtime._list_versions.return_value = ((5000, 1), (6000, 1))
        self.read.boolean.side_effect = lambda address: address == 1195
        self.runtime._context = Mock(return_value=self.read)
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1000):
            self.assertEqual(self.runtime.snapshot()['server_checked_at'], 1000)
        self.runtime._refresh_wait = None  # Explicit request has completed.
        with patch('deskrawl_assistant.auction_runtime.time.time', return_value=1060):
            self.assertEqual(self.runtime.snapshot()['server_checked_at'], 1000)
        self.read.boolean.side_effect = lambda address: address in (1194, 1195)
        self.assertEqual(self.runtime.snapshot()['server_checked_at'], 0)
        self.read.boolean.side_effect = lambda address: address == 1195
        self.assertEqual(self.runtime.snapshot()['server_checked_at'], 0)

    def test_failure_clearing_loading_cannot_confirm_constructor_lists(self):
        key = ('session-a', 1000, '76561198000000000')
        self.runtime._refresh_wait = {'key': key, 'versions': ((5000, 0), (6000, 0))}
        self.read.boolean.side_effect = lambda address: address == 1195
        state = self.runtime._state(self.read)
        self.assertIsNone(state['listings'])
        self.assertIsNone(state['enabled'])
        self.assertFalse(state['_response_ready'])
        self.runtime._list_versions.return_value = ((5000, 1), (6000, 0))
        self.assertFalse(self.runtime._state(self.read)['_response_ready'])

    def test_guard_failure_never_commits_refresh_receipt(self):
        key = ('session-a', 1000, '76561198000000000')
        self.runtime._refresh_wait = {'key': key, 'versions': ((5000, 0), (6000, 0))}
        self.runtime._list_versions.return_value = ((5000, 1), (6000, 1))
        self.read.boolean.side_effect = lambda address: address == 1195
        self.read.finish.side_effect = SnapshotChangedError('changed')
        self.runtime._context = Mock(return_value=self.read)
        self.assertFalse(self.runtime.snapshot()['available'])
        self.assertIsNone(self.runtime._refreshed)

    def test_refresh_cache_is_not_reused_after_reader_or_account_changes(self):
        self.read.boolean.side_effect = lambda address: address == 1195
        self.runtime._refreshed = ('session-a', 1000, '76561198000000000')
        self.assertFalse(self.runtime._state(self.read)['needs_refresh'])
        self.actions.reader.session_id = 'session-b'
        self.assertTrue(self.runtime._state(self.read)['needs_refresh'])
        self.actions.reader.session_id = 'session-a'
        self.actions.reader.string.side_effect = lambda p: '76561198000000001' if p == 4000 else ''
        self.assertTrue(self.runtime._state(self.read)['needs_refresh'])

    def test_switching_away_and_back_cannot_reuse_an_old_server_receipt(self):
        self.read.boolean.side_effect = lambda address: address == 1195
        self.runtime._context = Mock(return_value=self.read)
        self.runtime._refreshed = ('session-a', 1000, '76561198000000000')
        self.runtime._refreshed_at = 1000
        self.actions.reader.string.side_effect = lambda p: '76561198000000001' if p == 4000 else ''
        self.assertEqual(self.runtime.snapshot()['server_checked_at'], 0)
        self.actions.reader.string.side_effect = lambda p: '76561198000000000' if p == 4000 else ''
        state = self.runtime.snapshot()
        self.assertEqual(state['server_checked_at'], 0)
        self.assertTrue(state['needs_refresh'])

    def test_received_history_does_not_consume_eight_active_upload_slots(self):
        self.runtime._refreshed = ('session-a', 1000, '76561198000000000')
        self.read.boolean.side_effect = lambda address: address == 1195
        self.runtime._listings.return_value = [{'received': True}] * 8 + [{'received': False}] * 2
        state = self.runtime._state(self.read)
        self.assertEqual(state['listing_count'], 2)
        self.assertEqual(state['received_count'], 8)
        self.assertEqual(len(state['listings']), 10)

    def test_account_reads_native_steamid_only_never_auth_token(self):
        self.read.boolean.side_effect = lambda addr: addr in (1040, 1195)
        state = self.runtime._state(self.read)
        self.assertTrue(state['steamid_verified'])
        self.assertEqual(state['steamid'], '76561198000000000')
        self.assertNotIn(3120, [call.args[0] for call in self.read.ptr.call_args_list])
        self.assertNotIn('token', state)

    def test_invalid_native_account_not_guessed(self):
        self.read.boolean.return_value = False
        self.actions.reader.string.side_effect = None
        self.actions.reader.string.return_value = 'not-a-steamid'
        state = self.runtime._state(self.read)
        self.assertIsNone(state['steamid']); self.assertFalse(state['steamid_verified'])


class OpenRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.actions = SimpleNamespace(reader=Mock(), profile=load_profile(), dispatcher=Mock())
        self.runtime = AuctionRuntime(self.actions, journal_path=Path(self.temp.name) / 'journal.json')
        self.read = Mock(guards=[])
        self.runtime._context = Mock(return_value=self.read)
        self.runtime._state = Mock(return_value=ready())
        self.runtime._wait_open = Mock(return_value=ready())
        self.runtime._invoke = Mock(return_value={'completed': True})

    def test_already_open_action_explicitly_refreshes_using_native_method(self):
        self.runtime._open()
        calls = [call.args[0][0] for call in self.runtime._invoke.call_args_list]
        self.assertEqual([call.method for call in calls],
                         ['PanelBase.auction_is_open', 'AuctionUI.refresh_list'])
        self.assertTrue(calls[0].require_true)
        self.assertTrue(all(call.mutates is False for call in calls))
        self.assertIsNone(self.runtime._refresh_wait)
        compile_mailbox(0x10000000, 0x180000000, load_profile(), 0x20000000,
                        0x30000000, 123, calls, [])

    def test_only_list_refresh_may_pass_own_exact_pending_selection(self):
        self.runtime._pending = {'instance-a': {'item_uid': 'uid-a',
            'internal_name': 'DivineHelm', 'steamid': ready()['steamid']}}
        self.runtime._state.return_value = ready(selection_occupied=True, selected_uid='uid-a')
        self.runtime._open(refresh_only=True)
        calls = [c.args[0][0] for c in self.runtime._invoke.call_args_list]
        self.assertEqual([c.method for c in calls], ['AuctionUI.refresh_list'])
        self.assertFalse(calls[0].mutates)
        self.assertTrue(self.runtime._pending)
        self.runtime._invoke.reset_mock()
        with self.assertRaises(GameBusyError):
            self.runtime._open()
        self.runtime._invoke.assert_not_called()

    def test_refresh_never_clears_or_replaces_another_selection_or_account(self):
        self.runtime._pending = {'instance-a': {'item_uid': 'uid-a',
            'internal_name': 'DivineHelm', 'steamid': ready()['steamid']}}
        for change in ({'selected_uid': 'other'}, {'steamid': '76561198000000001'},
                       {'steamid_verified': False}, {'selected_uid': ''}):
            with self.subTest(change=change):
                self.runtime._state.return_value = ready(selection_occupied=True, selected_uid='uid-a')
                self.runtime._state.return_value.update(change)
                with self.assertRaises(GameBusyError):
                    self.runtime._open(refresh_only=True)
                self.runtime._invoke.assert_not_called()

    def test_background_fetch_always_requests_server_refresh_without_opening_window(self):
        self.runtime._refreshed = ('session-a', 100, '76561198000000000')
        self.runtime._open(refresh_only=True)
        calls = [call.args[0][0] for call in self.runtime._invoke.call_args_list]
        self.assertEqual([call.method for call in calls], ['AuctionUI.refresh_list'])
        self.assertFalse(calls[0].mutates)
        self.runtime._wait_open.assert_called_once()
        self.assertIsNone(self.runtime._refreshed)

    def test_busy_preflight_also_invalidates_old_server_observation(self):
        self.runtime._refreshed = ('session-a', 100, '76561198000000000')
        self.runtime._refreshed_at = 1000
        self.runtime._state.return_value = ready(busy=True)
        with self.assertRaises(GameBusyError):
            self.runtime._open(refresh_only=True)
        self.assertIsNone(self.runtime._refreshed)
        self.assertEqual(self.runtime._refreshed_at, 0)
        self.runtime._invoke.assert_not_called()

    def test_only_explicit_false_query_selects_show_full_refresh_chain(self):
        self.runtime._invoke.side_effect = [NativeActionError('false', state=REJECTED,
            last_call=0, mutation_started=False), {'completed': True}]
        self.runtime._open()
        methods = [call.args[0][0].method for call in self.runtime._invoke.call_args_list]
        self.assertEqual(methods, ['PanelBase.auction_is_open', 'AuctionUI.Show'])

    def test_unknown_query_outcome_never_falls_back_to_show(self):
        for changes in ({'state': REJECTED, 'last_call': None, 'mutation_started': False},
                        {'state': REJECTED, 'last_call': 0, 'mutation_started': None},
                        {'state': REJECTED, 'last_call': 0, 'mutation_started': False, 'uncertain': True},
                        {'state': 6, 'last_call': 0, 'mutation_started': False}):
            with self.subTest(changes=changes):
                self.runtime._invoke.reset_mock()
                self.runtime._invoke.side_effect = NativeActionError('failed', **changes)
                with self.assertRaises(NativeActionError):
                    self.runtime._open()
                self.assertEqual(self.runtime._invoke.call_count, 1)

    def test_failed_refresh_invalidates_previous_success(self):
        self.runtime._refreshed = ('session-a', 100, '76561198000000000')
        self.runtime._wait_open.side_effect = MemoryReadError('fetch failed')
        with self.assertRaises(MemoryReadError):
            self.runtime._open()
        self.assertIsNone(self.runtime._refreshed)
        self.assertIsNone(self.runtime._refresh_wait)

    def test_wait_accepts_fresh_empty_success_without_inventing_visibility(self):
        self.runtime.snapshot = Mock(return_value=ready(opened=None))
        state = AuctionRuntime._wait_open(self.runtime, None, lambda: True)
        self.assertTrue(state['effect_confirmed'])
        self.assertIsNone(state['opened'])


class IdentitySnapshotTests(unittest.TestCase):
    """Use real _Read guards over copied bytes, never a process handle."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.memory_bytes, self.addresses = {}, []
        self.singletons = {'GameManager': 1000, 'GameAreaManager': 2000,
                           'SaveSystem': 3000, 'TownPortalSystem': 4000,
                           'PlayerData': 5000, 'CloudClient': 6000}

        def read(address, size):
            self.addresses.append((address, size))
            return bytes(self.memory_bytes[address + i] for i in range(size))

        self.memory = SimpleNamespace(read=read, write=Mock())
        self.strings = {7000: 'Hunter', 8000: '76561198000000000'}
        self.reader = Mock(memory=self.memory, session_id='reader-session-a')
        self.reader.string.side_effect = lambda pointer: self.strings.get(pointer)
        self.actions = GameActions(self.reader, dispatcher=Mock())
        self.runtime = AuctionRuntime(self.actions, journal_path=Path(self.temp.name) / 'journal.json')
        self.actions._auction = self.runtime
        singleton = patch('deskrawl_assistant.game_actions._Read.singleton',
                          side_effect=lambda name: self.singletons[name])
        self.singleton = singleton.start(); self.addCleanup(singleton.stop)
        self.put(1176, struct.pack('<i', 1))  # entered character
        self.put(2120, struct.pack('<i', 0))  # combat, not main city
        self.put(3032, struct.pack('<ii', 1, 2))  # online / character slot
        self.put(3194, b'\x01')  # item-save busy is allowed for readonly identity
        self.put(4076, b'\x00')  # teleport inactive
        self.put(2176, b'\x00')  # scene-changing inactive
        self.put(5040, struct.pack('<Q', 7000))
        self.put(6128, struct.pack('<Q', 8000))

    def put(self, address, raw):
        self.memory_bytes.update((address + i, value) for i, value in enumerate(raw))

    def test_readable_outside_main_city_and_does_not_open_ui_or_read_token(self):
        for scene, expected in ((0, 'combat'), (1, 'town'), (2, 'fishing')):
            self.put(2120, struct.pack('<i', scene))
            state = self.actions.steam_identity_snapshot()
            self.assertTrue(state['available']); self.assertTrue(state['steamid_verified'])
            self.assertEqual(state['steamid'], '76561198000000000')
            self.assertEqual(state['character_id'], '1:2:Hunter')
            self.assertEqual(state['session'], 'reader-session-a')
            self.assertEqual(state['scene'], expected)
        self.assertNotIn('AuctionUI', [call.args[0] for call in self.singleton.call_args_list])
        self.assertFalse(any(address <= 6120 < address + size for address, size in self.addresses))
        self.assertNotIn('token', state)
        self.memory.write.assert_not_called(); self.actions.dispatcher.invoke.assert_not_called()

    def test_unknown_account_never_returns_cached_identity(self):
        self.assertTrue(self.runtime.identity_snapshot()['available'])
        for unknown in (None, '', 'not-a-steamid', '７６５６１１９８０００００００００'):
            self.strings[8000] = unknown
            state = self.runtime.identity_snapshot()
            self.assertFalse(state['available']); self.assertFalse(state['steamid_verified'])
            for key in ('steamid', 'character_id', 'session'):
                self.assertIsNone(state[key])

    def test_changed_character_or_account_is_fresh_not_a_previous_lease(self):
        previous = self.runtime.identity_snapshot()
        self.put(3032, struct.pack('<ii', 1, 3))
        self.strings[7000] = 'Monk'
        self.strings[8000] = '76561198000000001'
        current = self.runtime.identity_snapshot()
        self.assertTrue(current['available'])
        self.assertEqual(current['character_id'], '1:3:Monk')
        self.assertNotEqual(current['character_id'], previous['character_id'])
        self.assertNotEqual(current['steamid'], previous['steamid'])

    def test_character_scene_account_and_session_changes_during_read_fail_closed(self):
        cases = ((3036, struct.pack('<i', 3)), (6128, struct.pack('<Q', 9000)),
                 (5040, struct.pack('<Q', 9000)), (2176, b'\x01'),
                 (4076, b'\x01'), (1176, struct.pack('<i', 0)),
                 (2120, struct.pack('<i', 1)), (None, None))
        for address, raw in cases:
            with self.subTest(address=address):
                original = dict(self.memory_bytes)
                def changing(pointer):
                    if pointer == 8000:
                        if address is None:
                            self.reader.session_id = 'reader-session-b'
                        else:
                            self.put(address, raw)
                    return self.strings.get(pointer)
                self.reader.string.side_effect = changing
                state = self.runtime.identity_snapshot()
                self.assertFalse(state['available']); self.assertFalse(state['steamid_verified'])
                self.assertIsNone(state['steamid']); self.assertIsNone(state['character_id'])
                self.memory_bytes = original
                self.reader.session_id = 'reader-session-a'
        self.memory.write.assert_not_called(); self.actions.dispatcher.invoke.assert_not_called()

    def test_not_entered_offline_invalid_slot_or_transition_is_unavailable(self):
        cases = ((1176, struct.pack('<i', 0)), (3032, struct.pack('<ii', 0, 2)),
                 (3032, struct.pack('<ii', 1, -1)), (3032, struct.pack('<ii', 1, 100)),
                 (4076, b'\x01'), (2176, b'\x01'), (2120, struct.pack('<i', 3)))
        for address, raw in cases:
            with self.subTest(address=address, raw=raw):
                original = dict(self.memory_bytes); self.put(address, raw)
                state = self.runtime.identity_snapshot()
                self.assertFalse(state['available']); self.assertIsNone(state['steamid'])
                self.memory_bytes = original

    def test_unknown_or_changed_reader_and_build_never_return_identity(self):
        self.reader.session_id = None
        self.assertFalse(self.runtime.identity_snapshot()['available'])
        self.reader.session_id = 'reader-session-a'
        self.reader.check_game_files.side_effect = MemoryReadError('incompatible build')
        self.assertFalse(self.runtime.identity_snapshot()['available'])


class GateTests(unittest.TestCase):
    def test_constructor_never_requests_identity_or_initializes_auction(self):
        reader, dispatcher = Mock(), Mock()
        actions = GameActions(reader, dispatcher=dispatcher)
        self.assertIsNone(actions._auction)
        reader.check_game_files.assert_not_called(); reader.memory.read.assert_not_called()
        dispatcher.invoke.assert_not_called()

    def test_release_profile_enables_validated_auction_actions_without_test_mode(self):
        profile = load_profile()
        self.assertEqual(CAPABILITIES['auction_upload'], KINDS)
        for kind in KINDS:
            self.assertIn(kind, profile['verified_kinds'])
            self.assertNotIn(kind, profile['enabled_kinds'])
        actions = GameActions(Mock(), dispatcher=Mock())
        actions._auction = Mock()
        self.assertFalse(actions.allow_unverified)
        capability = actions.capabilities()['auction_upload']
        self.assertTrue(capability['available'])
        self.assertTrue(capability['native_verified'])
        self.assertTrue(capability['release_enabled'])
        self.assertFalse(capability['test_enabled'])
        actions.auction_upload(item())
        actions._auction.execute.assert_called_once()

    def test_missing_native_authorization_still_blocks_auction(self):
        profile = load_profile()
        profile['verified_kinds'] = [kind for kind in profile['verified_kinds'] if kind not in KINDS]
        with patch('deskrawl_assistant.game_actions.load_profile', return_value=profile):
            actions = GameActions(Mock(), dispatcher=Mock())
        actions._auction = Mock()
        with self.assertRaises(NativeActionError):
            actions.auction_upload(item())
        actions._auction.execute.assert_not_called()


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.actions = SimpleNamespace(reader=Mock(), profile=load_profile())
        self.runtime = AuctionRuntime(self.actions, journal_path=Path(self.temp.name) / 'pending.json')
        self.runtime._context = Mock(return_value=Mock())
        self.listing = {'instance_id': 'instance-a', 'internal_name': 'DivineHelm',
                        'listed_at': 123, 'pending': False, 'received': False}
        self.runtime._state = Mock(return_value=ready(listings=[self.listing], listing_count=1))
        self.actions.reader.snapshot.return_value = {'complete': True, 'containers': {
            'inventory': {'slots': []}, 'storage': {'slots': []}}}

    def test_confirm_requires_server_listing_and_original_callback_removal(self):
        receipt = self.runtime._confirm(item())
        self.assertEqual(receipt['uploaded_instance_id'], 'instance-a')
        self.assertEqual(receipt['receipt_id'], 'auction:instance-a:123')
        self.assertEqual(receipt['listing_count'], 1)
        self.assertFalse(receipt['steam_pending'])

    def test_exact_pending_listing_and_native_removal_confirm_game_upload_only(self):
        self.listing['pending'] = True
        receipt = self.runtime._confirm(item())
        self.assertTrue(receipt['steam_pending'])
        self.assertEqual(receipt['uploaded_instance_id'], 'instance-a')

    def test_pending_listing_with_original_item_still_present_cannot_confirm(self):
        self.listing['pending'] = True
        self.actions.reader.snapshot.return_value['containers']['inventory']['slots'] = [item()]
        with patch('deskrawl_assistant.auction_runtime.time.monotonic', side_effect=[0, 1, 30]), \
             patch('deskrawl_assistant.auction_runtime.time.sleep'):
            with self.assertRaises(NativeActionError) as exc:
                self.runtime._confirm(item())
        self.assertTrue(exc.exception.uncertain)

    def test_listing_alone_missing_removal_pending_or_received_cannot_confirm(self):
        cases = ('received', 'bag_item', 'storage_item', 'incomplete', 'no_listing', 'busy', 'wrong_item')
        for case in cases:
            with self.subTest(case=case):
                listing = dict(self.listing)
                state = ready(listings=[listing], listing_count=1)
                snapshot = {'complete': True, 'containers': {
                    'inventory': {'slots': []}, 'storage': {'slots': []}}}
                if case == 'received': listing[case] = True
                elif case == 'bag_item': snapshot['containers']['inventory']['slots'] = [item()]
                elif case == 'storage_item': snapshot['containers']['storage']['slots'] = [item(container='storage')]
                elif case == 'incomplete': snapshot['complete'] = False
                elif case == 'no_listing': state['listings'] = []
                elif case == 'busy': state['busy'] = True
                elif case == 'wrong_item': listing['internal_name'] = 'AnotherDivineItem'
                self.runtime._state.return_value = state
                self.actions.reader.snapshot.return_value = snapshot
                with patch('deskrawl_assistant.auction_runtime.time.monotonic', side_effect=[0, 1, 30]), \
                     patch('deskrawl_assistant.auction_runtime.time.sleep'):
                    with self.assertRaises(NativeActionError) as exc:
                        self.runtime._confirm(item())
                self.assertTrue(exc.exception.uncertain)


if __name__ == '__main__':
    unittest.main()
