"""Selected batch state transitions using fake game/Steam; no live writes."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.manual_auction import ManualAuctionServiceMixin
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.native_dispatcher import NativeActionError
from tests.test_auction_runtime import item, ready


STEAMID = '76561198000000000'


def selected(number, container='inventory', **extra):
    return item(item_uid='uid-' + str(number), instance_id='instance-' + str(number),
        transfer_key='selected-' + str(number), internal_name='DivineHelm1',
        slot_index=number, container=container, **extra)


class Trading:
    def __init__(self):
        self.calls, self.proofs = [], {}
        self.on_prepare = self.on_complete = self.on_sale = None

    def prepare_upload(self, steamid, name, variant, *, manual=False):
        assert manual is True
        ticket = format(len(self.proofs) + 1, '032x')
        self.proofs[ticket] = {'status': 'prepared'}
        self.calls.append(('prepare', ticket))
        if self.on_prepare: self.on_prepare()
        return ticket

    def complete_upload(self, ticket, count, *, expected_instance_id):
        self.calls.append(('complete', ticket))
        self.proofs[ticket] = {'status': 'captured'}
        if self.on_complete: self.on_complete()
        return self.state()

    def sale_once(self, settings, steamid, *, validate, proof_scope):
        assert isinstance(proof_scope, frozenset) and len(proof_scope) == 1
        if validate():
            ticket = next(iter(proof_scope))
            self.calls.append(('sale', ticket, settings['price_rule']))
            self.proofs[ticket] = {'status': 'pending'}
            if self.on_sale: self.on_sale()
        return self.state()

    def proof_state(self, proof, steamid): return deepcopy(self.proofs[proof])
    def upload_binding_current(self, ticket, steamid): return True
    def state(self): return {}
    def close(self): pass


class Client:
    connected = True

    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.identity = {'available': True, 'steamid_verified': True, 'steamid': STEAMID,
            'session': 'session-a', 'character_id': 'character-a', 'scene': 'town'}
        self.native = ready()
        self.capacity = 40
        self.actions = []
        self.on_upload = self.on_move = None

    def steam_identity_snapshot(self): return deepcopy(self.identity)
    def managed_capabilities(self): return {'auction_upload': {'available': True}}
    def auction_state(self): return deepcopy(self.native)
    def snapshot(self):
        return {'complete': True, 'bridge_session': self.identity['session'],
            'transfer': {'available': True, 'storage_indices': list(range(250))},
            'containers': {name: {'slot_count': self.capacity if name == 'inventory' else 250,
                'occupied_count': sum(row['container'] == name for row in self.rows),
                'slots': deepcopy([row for row in self.rows if row['container'] == name])}
                for name in ('inventory', 'storage')}}

    def move_items(self, rows, target, validate):
        assert validate()
        for expected in rows:
            row = next(row for row in self.rows if row['item_uid'] == expected['item_uid'])
            self.actions.append(('move', row['item_uid']))
            row.update(container=target, slot_index=30)
        if self.on_move: self.on_move()
        return {'moved': len(rows)}

    def execute_game_action(self, kind, payload, cancel_event, *, validate):
        if not validate(): raise NativeActionError('cancelled', mutation_started=False)
        self.actions.append((kind, payload.get('item', {}).get('item_uid')))
        if kind == 'auction_open':
            self.native['needs_refresh'] = False
            return {'effect_confirmed': True}
        row = payload['item']
        if self.on_upload:
            result = self.on_upload(payload)
            if result is not None: return result
        self.rows = [value for value in self.rows if value['item_uid'] != row['item_uid']]
        self.native['listing_count'] += 1
        return {'effect_confirmed': True, 'uploaded_uid': row['item_uid'],
            'uploaded_instance_id': row['instance_id'], 'uploaded_internal_name': row['internal_name']}


class Service(ManualAuctionServiceMixin):
    def __init__(self, directory, rows):
        self.guard = threading.RLock()
        self.closed = threading.Event()
        self.cancelled = threading.Event()
        self.updating = self.busy = False
        self.managed = SimpleNamespace(running=False, finishing=False, settings={'sentinel': 'unchanged'})
        self.client = Client(rows)
        self.snapshot = self.client.snapshot()
        self.trading = Trading()
        self.auction_coordinator = AuctionCoordinator(directory, self.trading,
            lambda: (False, {}, STEAMID), start_worker=False)
        self.journal = Mock()

    def _market_catalog(self): return SimpleNamespace(resolve=lambda row: (object(), ''))
    def _describe_item(self, key): return ('item ' + key, '')
    def game_actions_blocked(self): return False
    def _auction_service(self): return self.auction_coordinator
    def _publish(self, snapshot): self.snapshot = snapshot


class ManualAuctionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.service = Service(self.path, [selected(1), selected(2, 'storage')])
        self.addCleanup(self.service.auction_coordinator.close)

    def preview(self, ids=None, **extra):
        return self.service.manual_auction_preview({'items': ids or ['selected-1', 'selected-2'],
            'mode': 'upload', **extra})

    def start(self, preview):
        self.service.manual_auction_start({'token': preview['token']})
        self.service.worker.join(3)
        self.assertFalse(self.service.worker.is_alive())
        self.assertFalse(self.service.busy)
        return self.service.manual_auction_state()

    def test_upload_batch_preserves_settings_and_uses_exact_selected_instances(self):
        settings = deepcopy(self.service.managed.settings)
        self.service.client.rows.append(selected(3))
        result = self.start(self.preview())
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual([row['status'] for row in result['items']], ['uploaded', 'uploaded'])
        self.assertEqual([row['item_uid'] for row in self.service.client.rows], ['uid-3'])
        self.assertEqual(self.service.managed.settings, settings)
        self.assertEqual(self.service.trading.calls, [])

    def test_sell_maps_exact_ticket_and_passes_scoped_sale_only(self):
        result = self.start(self.preview(mode='sell', price_rule='highest_buy'))
        self.assertEqual(result['phase'], 'completed')
        self.assertTrue(all(row['status'] == 'pending_confirmation' for row in result['items']))
        self.assertEqual([row[0] for row in self.service.trading.calls],
                         ['prepare', 'complete', 'sale', 'prepare', 'complete', 'sale'])
        self.assertTrue(all(row[2] == 'highest_buy' for row in self.service.trading.calls if row[0] == 'sale'))

    def test_unknown_browser_tokens_duplicates_and_extra_parameters_rejected(self):
        for args in ({'ids': ['invented']}, {'ids': ['selected-1', 'selected-1']}, {'wrong': True}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                self.preview(**args)
        self.assertEqual(self.service.client.actions, [])

    def test_preview_supports_instance_fallback(self):
        self.service.client.rows[0].pop('transfer_key')
        self.service.snapshot = self.service.client.snapshot()
        result = self.preview(ids=['instance-1'])
        self.assertEqual(result['ready_count'], 1)

    def test_upload_ignores_item_and_slot_locks_without_a_separate_authorization(self):
        self.service.client.rows[0]['locked'] = True
        self.service.client.rows[1].update(locked=None, slot_locked=True)
        self.service.snapshot = self.service.client.snapshot()
        self.assertEqual(self.preview()['ready_count'], 2)
        for legacy_value in (False, True):
            self.assertEqual(self.preview(allow_locked=legacy_value)['ready_count'], 2)
        uploaded = []
        def inspect(payload):
            self.assertNotIn('permit_locked', payload)
            self.assertNotIn('allow_locked', payload)
            uploaded.append(deepcopy(payload['item']))
        self.service.client.on_upload = inspect
        result = self.start(self.preview())
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual([row['status'] for row in result['items']], ['uploaded', 'uploaded'])
        self.assertTrue(uploaded[0]['locked'])
        self.assertTrue(uploaded[1]['slot_locked'])
        self.assertEqual([kind for kind, _ in self.service.client.actions], ['auction_upload', 'move', 'auction_upload'])

    def test_uidless_stacks_skip_without_fabricating_identity(self):
        self.service.client.rows[0].update(internal_name='GoldBar', item_uid='', instance_id='', count=5)
        self.service.snapshot = self.service.client.snapshot()
        result = self.preview()
        self.assertEqual(result['skipped_count'], 1)
        self.assertIn('堆叠', result['items'][0]['reason'])

    def test_start_rejects_mode_price_changes_or_reused_token(self):
        preview = self.preview()
        with self.assertRaises(ValidationError):
            self.service.manual_auction_start({'token': preview['token'], 'mode': 'sell'})
        self.start(preview)
        with self.assertRaises(ValidationError): self.service.manual_auction_start({'token': preview['token']})

    def test_expired_preview_cannot_start(self):
        preview = self.preview()
        self.service.manual_auction_preview_value['expires'] = 0
        with self.assertRaises(ValidationError): self.service.manual_auction_start({'token': preview['token']})

    def test_arbitrary_unicode_or_oversized_confirmation_token_is_validation_error(self):
        self.preview()
        for token in ('无效', 'a' * 2048, '', 'has spaces'):
            with self.subTest(token=token[:10]), self.assertRaises(ValidationError):
                self.service.manual_auction_start({'token': token})

    def test_stale_last_item_rejects_whole_batch_before_first_upload(self):
        preview = self.preview()
        self.service.client.rows[1]['slot_index'] += 1
        result = self.start(preview)
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(self.service.client.actions, [])

    def test_same_uid_on_different_character_cannot_start(self):
        preview = self.preview()
        self.service.client.identity['character_id'] = 'character-b'
        self.assertEqual(self.start(preview)['phase'], 'failed')
        self.assertEqual(self.service.client.actions, [])

    def test_offline_or_combat_or_managed_or_busy_rejects_preview(self):
        for name, value, target in [('scene', 'combat', self.service.client.identity),
                                    ('running', True, self.service.managed), ('busy', True, self.service)]:
            old = target.get(name) if isinstance(target, dict) else getattr(target, name)
            if isinstance(target, dict): target[name] = value
            else: setattr(target, name, value)
            with self.subTest(name=name), self.assertRaises(ValidationError): self.preview()
            if isinstance(target, dict): target[name] = old
            else: setattr(target, name, old)

    def ui_context(self, **changes):
        self.service.managed_control_read_at = time.monotonic()
        self.service.managed_control = {'available': True, 'scene': 'town',
            'scene_changing': False, 'teleporting': False, 'online_saving': False,
            'online_pending_requests': 0, 'dead': False, 'in_battle': False,
            'character_id': '1:0:test-character', **changes}
        self.service.managed_capabilities = {'auction_upload': {'available': True}}

    def test_ui_readiness_uses_only_cached_verified_main_city_state(self):
        self.ui_context()
        with patch.object(self.service.client, 'steam_identity_snapshot', side_effect=AssertionError('GET cannot read game')), \
             patch.object(self.service.client, 'managed_capabilities', side_effect=AssertionError('GET cannot read game')):
            self.assertEqual(self.service.manual_auction_availability(), {'available': True, 'reason': ''})
            self.ui_context(scene='combat')
            self.assertIn('主城', self.service.manual_auction_availability()['reason'])
        self.assertEqual(self.service.client.actions, [])

    def test_ui_readiness_fails_closed_for_unknown_stale_or_unsafe_states(self):
        for changes in ({'available': False}, {'scene': 'combat'}, {'scene': 'fishing'},
                        {'scene': None}, {'scene_changing': True}, {'teleporting': True},
                        {'online_saving': True}, {'online_pending_requests': 1},
                        {'online_pending_requests': None}, {'online_pending_requests': False},
                        {'teleporting': None}, {'dead': True}, {'in_battle': True},
                        {'character_id': '0:0:offline'},
                        {'scene': {'available': False, 'in_town': True}}):
            with self.subTest(changes=changes):
                self.ui_context(**changes)
                result = self.service.manual_auction_availability()
                self.assertFalse(result['available'])
                self.assertTrue(result['reason'])
        self.ui_context()
        for read_at in (None, time.monotonic() - 11, time.monotonic() + 11):
            self.service.managed_control_read_at = read_at
            self.assertFalse(self.service.manual_auction_availability()['available'])
        self.ui_context()
        self.service.managed_capabilities = {}
        self.assertFalse(self.service.manual_auction_availability()['available'])

    def test_ui_readiness_keeps_operational_guards_and_recovers_after_returning_to_town(self):
        self.ui_context(scene='combat')
        self.assertFalse(self.service.manual_auction_availability()['available'])
        self.ui_context()
        self.assertTrue(self.service.manual_auction_availability()['available'])
        self.service.busy = True
        self.assertFalse(self.service.manual_auction_availability()['available'])
        self.service.busy = False
        self.service.managed.running = True
        self.assertFalse(self.service.manual_auction_availability()['available'])
        self.service.managed.running = False
        self.service.client.connected = False
        self.assertFalse(self.service.manual_auction_availability()['available'])

    def test_ui_readiness_accepts_nested_scene_flags_without_losing_online_state(self):
        self.ui_context(scene={'available': True, 'in_town': True})
        self.assertTrue(self.service.manual_auction_availability()['available'])

    def test_gate_never_bypasses_unverified_native_capability(self):
        self.service.client.managed_capabilities = lambda: {'auction_upload': {'available': False}}
        with self.assertRaises(ValidationError): self.preview()

    def test_storage_page_lock_skips(self):
        self.service.snapshot['transfer']['storage_indices'] = []
        result = self.preview()
        self.assertEqual(result['items'][1]['status'], 'skipped')

    def test_full_bag_inventory_selection_is_uploaded_before_storage(self):
        self.service.client.capacity = 1
        result = self.start(self.preview(ids=['selected-2', 'selected-1']))
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(self.service.client.actions[0], ('auction_upload', 'uid-1'))

    def test_storage_only_full_bag_skips_and_never_moves(self):
        self.service.client.capacity = 1
        result = self.start(self.preview(ids=['selected-2']))
        self.assertEqual(result['items'][0]['status'], 'skipped')
        self.assertEqual(self.service.client.actions, [])

    def test_capacity_is_rechecked_between_items(self):
        self.service.client.native['listing_count'] = 7
        result = self.start(self.preview())
        self.assertEqual(result['items'][0]['status'], 'uploaded')
        self.assertEqual(result['items'][1]['status'], 'failed')
        self.assertEqual(sum(row[0] == 'auction_upload' for row in self.service.client.actions), 1)

    def test_unrelated_native_pending_blocks_all_new_uploads(self):
        self.service.client.native['pending'] = True
        result = self.start(self.preview())
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(self.service.client.actions, [])

    def test_definite_prewrite_failure_skips_that_item_only(self):
        def reject_first(payload):
            if payload['item']['item_uid'] == 'uid-1':
                raise NativeActionError('fixture reject', mutation_started=False)
        self.service.client.on_upload = reject_first
        result = self.start(self.preview())
        self.assertEqual([row['status'] for row in result['items']], ['failed', 'uploaded'])
        self.assertEqual(len(self.service.auction_coordinator.intents), 1)

    def test_unknown_upload_stops_batch_and_fences_retry(self):
        def unknown(payload): raise NativeActionError('fixture unknown', uncertain=True)
        self.service.client.on_upload = unknown
        result = self.start(self.preview())
        self.assertEqual(result['items'][0]['status'], 'uncertain')
        self.assertEqual(len(self.service.client.actions), 1)
        self.assertEqual(next(iter(self.service.auction_coordinator.intents.values()))['phase'], 'submitted')
        self.assertEqual(self.preview()['items'][0]['status'], 'skipped')

    def test_mismatched_receipt_is_uncertain_and_never_maps_or_sells(self):
        self.service.client.on_upload = lambda payload: {'effect_confirmed': True, 'uploaded_uid': 'wrong'}
        result = self.start(self.preview(mode='sell'))
        self.assertEqual(result['items'][0]['status'], 'uncertain')
        self.assertEqual([row[0] for row in self.service.trading.calls], ['prepare'])

    def test_cancel_during_native_submit_reconciles_it_but_no_sale_or_next_upload(self):
        self.service.client.on_upload = lambda payload: self.service.manual_auction_cancel() and None
        result = self.start(self.preview(mode='sell'))
        self.assertEqual(result['phase'], 'cancelled')
        self.assertEqual(result['items'][0]['status'], 'uploaded')
        self.assertEqual(result['items'][1]['status'], 'cancelled')
        self.assertEqual([row[0] for row in self.service.trading.calls], ['prepare', 'complete'])
        self.assertEqual(next(iter(self.service.auction_coordinator.intents.values()))['phase'], 'resolved')

    def test_cancel_after_storage_move_leaves_exact_item_in_bag(self):
        self.service.client.on_move = self.service.manual_auction_cancel
        result = self.start(self.preview(ids=['selected-2']))
        self.assertEqual(result['phase'], 'cancelled')
        row = next(row for row in self.service.client.rows if row['item_uid'] == 'uid-2')
        self.assertEqual(row['container'], 'inventory')
        self.assertEqual(self.service.client.actions, [('move', 'uid-2')])

    def test_status_and_cancel_remain_responsive_while_network_lock_is_held(self):
        preview = self.preview()
        with self.service.auction_coordinator.operation:
            self.service.manual_auction_start({'token': preview['token']})
            self.assertTrue(self.service.manual_auction_state()['running'])
            self.assertEqual(self.service.manual_auction_cancel()['phase'], 'cancelling')
            self.service.worker.join(1)
            self.assertFalse(self.service.worker.is_alive())
        self.assertEqual(self.service.client.actions, [])

    def test_game_account_change_after_steam_baseline_prevents_upload(self):
        self.service.trading.on_prepare = lambda: self.service.client.identity.update(steamid='76561198000000009')
        result = self.start(self.preview(mode='sell'))
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(self.service.client.actions, [])

    def test_steam_logout_after_baseline_prevents_native_upload(self):
        self.service.trading.upload_binding_current = lambda ticket, steamid: False
        result = self.start(self.preview(mode='sell'))
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(self.service.client.actions, [])

    def test_unresolved_prior_asset_mapping_blocks_another_same_kind_upload(self):
        prior = selected(99)
        coordinator = self.service.auction_coordinator
        intent = coordinator.submitted(prior, STEAMID, 'a' * 32)
        coordinator.confirmed(intent, {'uploaded_uid': prior['item_uid'],
            'uploaded_instance_id': prior['instance_id'], 'uploaded_internal_name': prior['internal_name']})
        result = self.start(self.preview(mode='sell'))
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(self.service.client.actions, [])
        self.assertEqual(self.service.trading.calls, [])

    def test_cached_full_capacity_is_refreshed_before_rejecting(self):
        self.service.client.native['listing_count'] = 8
        execute = self.service.client.execute_game_action
        def refreshed(kind, *args, **kwargs):
            result = execute(kind, *args, **kwargs)
            if kind == 'auction_open': self.service.client.native['listing_count'] = 0
            return result
        self.service.client.execute_game_action = refreshed
        result = self.start(self.preview(ids=['selected-1']))
        self.assertEqual(result['items'][0]['status'], 'uploaded')
        self.assertEqual([row[0] for row in self.service.client.actions], ['auction_open', 'auction_upload'])


if __name__ == '__main__': unittest.main()
