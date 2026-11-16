"""Native sold-sync ordering and upload fences, without attaching to a game."""
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.auction import DEFAULT_AUCTION, next_action
from deskrawl_assistant.auction_runtime import AuctionRuntime, FULL_LIST_SYNC_INTERVAL
from deskrawl_assistant.game_actions import GameBusyError
from deskrawl_assistant.native_dispatcher import NativeActionError, compile_mailbox, load_profile
from deskrawl_assistant.native_memory import MemoryReadError
from test_auction import item, observation
from test_auction_runtime import item as native_item, ready


class SteamSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.actions = SimpleNamespace(reader=Mock(), profile=load_profile(), dispatcher=Mock(),
            _online_state=Mock(return_value={'online_saving': False, 'online_pending_requests': 0}))
        self.runtime = AuctionRuntime(self.actions, journal_path=Path(temp.name) / 'journal.json')
        self.read = Mock(guards=[])
        self.read.singleton.return_value = 1000
        self.read.read.return_value = b'\x01\0\0\0\x02\0\0\0'
        self.read.boolean.return_value = False
        self.runtime._context = Mock(return_value=self.read)
        self.runtime._state = Mock(return_value=ready())
        self.runtime._wait_open = Mock(return_value=ready())
        self.runtime._invoke = Mock(return_value={'completed': True})
        self.now = 100.0
        clock = patch('deskrawl_assistant.auction_runtime.time.monotonic', side_effect=lambda: self.now)
        sleep = patch('deskrawl_assistant.auction_runtime.time.sleep', side_effect=self.advance)
        clock.start(); self.addCleanup(clock.stop)
        sleep.start(); self.addCleanup(sleep.stop)

    def advance(self, seconds):
        self.now = round(self.now + seconds, 6)

    def methods(self):
        return [c.args[0][0].method for c in self.runtime._invoke.call_args_list]

    def test_complete_native_sync_settles_before_regenerating_list(self):
        states = iter([
            {'online_saving': False, 'online_pending_requests': 1},
            {'online_saving': True, 'online_pending_requests': 0},
            {'online_saving': False, 'online_pending_requests': 0},
            {'online_saving': False, 'online_pending_requests': 0},
        ])
        self.actions._online_state.side_effect = lambda _: next(states,
            {'online_saving': False, 'online_pending_requests': 0})
        self.runtime._open(reconcile_steam=True, expected_steamid=ready()['steamid'])
        self.assertEqual(self.methods(), ['SaveSystem.steam_sync',
            'PanelBase.auction_is_open', 'AuctionUI.refresh_list'])
        self.assertGreaterEqual(self.actions._online_state.call_count, 4)
        sync = self.runtime._invoke.call_args_list[0].args[0][0]
        self.assertTrue(sync.mutates)  # Native wrapper applies revocations and saves.
        self.assertEqual(sync.instance, 1000)
        self.assertEqual(sync.arguments[0].value, 0)  # Optional Action<bool> callback.
        compile_mailbox(0x10000000, 0x180000000, load_profile(), 0x20000000,
            0x30000000, 123, [c.args[0][0] for c in self.runtime._invoke.call_args_list], [])
        self.runtime._wait_open.assert_called_once()
        self.assertFalse(self.runtime._pending)

    def test_request_or_native_item_operation_never_settling_cannot_fetch_or_upload(self):
        for pending, item_busy in ((1, False), (0, True)):
            with self.subTest(pending=pending, item_busy=item_busy):
                self.runtime._invoke.reset_mock()
                self.runtime._wait_open.reset_mock()
                self.actions._online_state.return_value = {
                    'online_saving': False, 'online_pending_requests': pending}
                self.read.boolean.return_value = item_busy
                start = self.now
                with self.assertRaises(GameBusyError):
                    self.runtime._open(reconcile_steam=True)
                self.assertLess(self.now - start, 11)
                self.assertEqual(self.methods(), ['SaveSystem.steam_sync'])
                self.runtime._wait_open.assert_not_called()
                self.assertIsNone(self.runtime._refreshed)
                self.assertFalse(self.runtime._pending)

    def test_cancel_after_sync_does_not_refresh_or_upload(self):
        event = Event()
        self.runtime._invoke.side_effect = lambda *a, **kw: event.set()
        with self.assertRaises(NativeActionError):
            self.runtime._open(reconcile_steam=True, cancel_event=event)
        self.assertEqual(self.methods(), ['SaveSystem.steam_sync'])
        self.runtime._wait_open.assert_not_called()

    def test_lost_sync_ack_is_not_misreported_as_an_uncertain_item_upload(self):
        self.runtime._invoke.side_effect = NativeActionError('sync timeout',
            uncertain=True, mutation_started=True)
        with self.assertRaises(NativeActionError) as caught:
            self.runtime.execute('auction_upload', {'item': native_item()})
        self.assertFalse(caught.exception.uncertain)
        self.assertFalse(caught.exception.mutation_started)
        self.assertEqual(self.methods(), ['SaveSystem.steam_sync'])
        self.assertFalse(self.runtime._pending)
        self.runtime._wait_open.assert_not_called()

    def test_revoked_authorization_and_account_mismatch_do_not_start_sync(self):
        cases = ({'validate': lambda: False}, {'expected_steamid': '76561198000000001'})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((NativeActionError, GameBusyError)):
                    self.runtime._open(reconcile_steam=True, **kwargs)
                self.runtime._invoke.assert_not_called()
        self.runtime._state.return_value = ready(selection_occupied=True)
        with self.assertRaises(GameBusyError):
            self.runtime._open(reconcile_steam=True)
        self.runtime._invoke.assert_not_called()

    def test_account_session_or_character_change_during_sync_stops_before_get(self):
        for change in ({'_refresh_key': ('other', 100, ready()['steamid'])},
                       {'steamid_verified': False}, {'character': True}):
            with self.subTest(change=change):
                self.runtime._invoke.reset_mock()
                self.read.read.return_value = b'original'
                calls = 0
                def state(_):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return ready()
                    if change.get('character'):
                        self.read.read.return_value = b'changed'
                        return ready()
                    return ready(**change)
                self.runtime._state.side_effect = state
                with self.assertRaises(GameBusyError):
                    self.runtime._open(reconcile_steam=True)
                self.assertEqual(self.methods(), ['SaveSystem.steam_sync'])

    def test_account_or_character_change_between_sync_and_get_is_rejected(self):
        self.runtime._sync_steam = Mock()
        for character in (False, True):
            with self.subTest(character=character):
                self.runtime._invoke.reset_mock()
                self.read.read.return_value = b'original'
                calls = 0
                def state(_):
                    nonlocal calls
                    calls += 1
                    if calls < 3:
                        return ready()
                    if character:
                        self.read.read.return_value = b'changed'
                        return ready()
                    return ready(_refresh_key=('other', 100, ready()['steamid']))
                self.runtime._state.side_effect = state
                with self.assertRaises(GameBusyError):
                    self.runtime._open(reconcile_steam=True)
                self.assertEqual(self.methods(), ['PanelBase.auction_is_open'])
                self.runtime._wait_open.assert_not_called()

    def test_idle_is_not_a_server_list_receipt(self):
        self.runtime._wait_open.side_effect = MemoryReadError('GET callback missing')
        with self.assertRaises(MemoryReadError):
            self.runtime._open(reconcile_steam=True)
        self.assertIsNone(self.runtime._refreshed)
        self.assertIsNone(self.runtime._refresh_wait)
        self.assertFalse(self.runtime._pending)

    def test_pending_recovery_and_explicit_read_refresh_only_get_lists(self):
        self.runtime.execute('auction_open', {'refresh_only': True})
        self.assertEqual(self.methods(), ['AuctionUI.refresh_list'])
        self.runtime._invoke.reset_mock()
        self.runtime._pending = {'instance-a': {'item_uid': 'uid-a', 'steamid': ready()['steamid']}}
        self.runtime._clear_retained_selection = Mock(return_value=ready())
        self.runtime.execute('auction_open', {})
        self.assertEqual(self.methods(), ['AuctionUI.refresh_list'])
        self.runtime._invoke.reset_mock()
        with self.assertRaises(NativeActionError):
            self.runtime.execute('auction_upload', {'item': item()})
        self.runtime._invoke.assert_not_called()

    def test_sync_due_is_read_only_and_cooldown_is_account_scoped(self):
        self.runtime._state.side_effect = lambda _: ready()
        self.assertTrue(self.runtime.snapshot()['steam_sync_due'])
        self.runtime._last_steam_sync = (ready()['_refresh_key'], self.now)
        self.assertFalse(self.runtime.snapshot()['steam_sync_due'])
        self.advance(FULL_LIST_SYNC_INTERVAL)
        self.assertTrue(self.runtime.snapshot()['steam_sync_due'])
        self.runtime._last_steam_sync = (('other', 100, ready()['steamid']), self.now)
        self.assertTrue(self.runtime.snapshot()['steam_sync_due'])
        self.runtime._invoke.assert_not_called()


class FullListRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = {**deepcopy(DEFAULT_AUCTION), 'upload_enabled': True,
            'categories': ['divine_equipment']}
        self.value = observation([item()])
        self.value['auction'].update(listing_count=8, steam_sync_due=True)

    def test_full_cached_list_can_schedule_sync_before_trying_upload(self):
        action = next_action(self.config, self.value)
        self.assertEqual(action['kind'], 'auction_open')
        self.assertEqual(action['reason_code'], 'auction_reconcile_capacity')
        self.value['auction'].update(listing_count=7, steam_sync_due=False)
        self.assertEqual(next_action(self.config, self.value)['kind'], 'auction_upload')

    def test_full_list_refresh_respects_cooldown_scope_and_existing_fences(self):
        for changes in ({'steam_sync_due': False}, {'pending': True}, {'busy': True},
                        {'steamid_verified': False}, {'enabled': False}, {'capacity': 9}):
            with self.subTest(changes=changes):
                value = deepcopy(self.value)
                value['auction'].update(changes)
                self.assertIsNone(next_action(self.config, value))
        self.assertIsNone(next_action({**self.config, 'upload_enabled': False}, self.value))
        for rows in ([], [{**item(), 'internal_name': 'MovementPotion1', 'name_key': 'MovementPotion1'}]):
            value = deepcopy(self.value)
            value['items'] = rows
            self.assertIsNone(next_action(self.config, value))
        self.value['scene']['in_town'] = False
        self.assertIsNone(next_action(self.config, self.value))


if __name__ == '__main__':
    unittest.main()
