"""Confirmed-upload sale recovery: fake Steam only, no game or real orders."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.steam_session import SteamSessionError
from deskrawl_assistant.steam_trading import SteamTradingService
from test_steam_trading import FakeSession, STEAMID, OTHER, catalog, inventory


class ConfirmedUploadSaleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.now = 1000
        self.session = FakeSession()
        self.service = self.reopen()
        self.settings = {'upload_enabled': True, 'sell_enabled': True,
                         'categories': ['gold_bar'], 'excluded_keys': [], 'price_rule': 'lowest_sell'}
        self.intents = [self.intent()]
        self.session.inventory = inventory(('2', 1, {'game_instance_id': 'game-guid'}))

    @staticmethod
    def intent(**changes):
        return {'upload_intent_id': 'a' * 64, 'steamid': STEAMID, 'phase': 'resolved',
                'managed_run': '2026-11-14T16:47:46+00:00', 'uid': 'native-uid',
                'instance_id': 'game-guid', 'item_key': 'GoldBar', 'variant': 'normal',
                'ticket': None, **changes}

    def reopen(self):
        return SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)

    def run_sale(self, **kwargs):
        return self.service.sale_once(self.settings, STEAMID, confirmed_uploads=self.intents,
                                      validate=lambda: True, **kwargs)

    def test_confirmed_managed_upload_recovers_exact_asset_without_new_baseline(self):
        self.run_sale()
        self.assertEqual(len(self.session.sales), 1)
        self.assertEqual(self.session.sales[0][1]['assetid'], '2')
        row = self.service._journal[0]
        self.assertEqual(row['game_instance_id'], 'game-guid')
        self.assertEqual(row['game_item_uid'], 'native-uid')
        self.assertEqual(row['upload_intent_id'], 'a' * 64)
        self.assertEqual(row['source'], 'verified_confirmed_upload')
        self.assertEqual(row['status'], 'listed')
        self.assertEqual(self.service._tickets, {})
        self.assertEqual(sum(path.startswith('/inventory/') for _, path in self.session.requests), 1)

    def test_same_name_or_numeric_steam_instance_is_never_exact_proof(self):
        self.session.inventory = inventory(('2', 1, {'instanceid': '0'}))
        result = self.run_sale()
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service._journal, [])
        self.assertIn('暂不能自动补卖', result['sale_message'])
        self.assertIn('定价上架', result['sale_message'])

    def test_missing_exact_property_can_recover_on_later_poll(self):
        self.session.inventory = inventory(('2', 1, {}))
        self.run_sale()
        self.now += 30
        self.session.inventory = inventory(('2', 1, {'properties': {'instanceId': 'game-guid'}}))
        self.run_sale()
        self.assertEqual(len(self.session.sales), 1)

    def test_recovery_captures_at_most_one_asset_per_sale_round(self):
        self.intents.append(self.intent(upload_intent_id='b' * 64, instance_id='other-guid', uid='other-uid'))
        self.session.inventory = inventory(('2', 1, {'game_instance_id': 'game-guid'}),
                                           ('3', 1, {'game_instance_id': 'other-guid'}))
        self.run_sale()
        self.assertEqual(len(self.service._journal), 1)
        self.assertEqual([data['assetid'] for _, data in self.session.sales], ['2'])
        self.run_sale()
        self.assertEqual(len(self.service._journal), 2)
        self.assertEqual([data['assetid'] for _, data in self.session.sales], ['2', '3'])

    def test_unconfirmed_manual_wrong_account_and_ambiguous_records_are_excluded(self):
        self.intents = [self.intent(**changes) for changes in (
            {'phase': 'submitted'}, {'phase': 'uncertain'}, {'phase': 'confirmed'},
            {'managed_run': None}, {'steamid': OTHER}, {'instance_id': None},
            {'upload_intent_id': 'invalid'}, {'market_skip_reason': 'ambiguous'})]
        self.run_sale()
        self.assertEqual(self.service._journal, [])
        self.assertEqual(self.session.sales, [])

    def test_current_exclusions_and_categories_override_historical_upload(self):
        for changes in ({'excluded_keys': ['GoldBar']}, {'categories': []},
                        {'upload_enabled': False}, {'sell_enabled': False}):
            with self.subTest(changes=changes):
                saved = deepcopy(self.settings)
                self.settings.update(changes)
                self.run_sale()
                self.assertEqual(self.session.sales, [])
                self.assertEqual(self.service._journal, [])
                self.settings = saved

    def test_expired_baseline_can_recover_only_with_independent_exact_guid(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.now += 601
        self.intents = [self.intent(ticket=ticket, market_skip_reason='baseline_expired')]
        # Original expired baselines stay expired, not recreated or extended.
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(ticket)
        self.run_sale()
        self.assertEqual(len(self.session.sales), 1)
        self.assertEqual(self.service._tickets[ticket]['created_at'], 1000)

    def test_duplicate_native_claims_do_not_choose_one(self):
        self.intents.append(self.intent(upload_intent_id='b' * 64))
        self.run_sale()
        self.assertEqual(self.session.sales, [])

    def test_known_proof_does_not_hide_a_duplicate_native_claim(self):
        self.service._journal = [{'id': 'old', 'steamid': STEAMID, 'assetid': '9',
            'item_key': 'GoldBar', 'variant': 'normal', 'amount': 1,
            'status': 'sold', 'proof': 'b' * 32}]
        self.intents = [self.intent(ticket='b' * 32), self.intent(upload_intent_id='b' * 64)]
        self.run_sale()
        self.assertEqual(len(self.service._journal), 1)
        self.assertEqual(self.session.sales, [])

    def test_saved_ambiguous_or_manual_baseline_cannot_be_overridden(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.intents = [self.intent(ticket=ticket)]
        for guard in ('ambiguous', 'manual_only'):
            with self.subTest(guard=guard):
                self.service._tickets[ticket] = {**self.service._tickets[ticket], guard: True}
                self.run_sale()
                self.assertEqual(self.service._journal, [])
                self.assertEqual(self.session.sales, [])
                self.service._tickets[ticket].pop(guard)

    def test_duplicate_assets_conflicting_properties_and_excess_quantity_stay_pending(self):
        for assets in (
            inventory(('2', 1, {'game_instance_id': 'game-guid'}),
                      ('3', 1, {'game_instance_id': 'game-guid'})),
            inventory(('2', 1, {'game_instance_id': 'game-guid',
                               'properties': {'instanceId': 'other-guid'}})),
            inventory(('2', 2, {'game_instance_id': 'game-guid'})),
            inventory(('2', 1, {'game_instance_id': 'wrong-guid'})),
        ):
            with self.subTest(assets=assets):
                self.session.inventory = assets
                self.run_sale()
                self.assertEqual(self.service._journal, [])
                self.assertEqual(self.session.sales, [])

    def test_every_previous_sale_state_is_a_permanent_recovery_fence(self):
        for status in ('captured', 'intent', 'uncertain', 'pending', 'listed', 'sold', 'missing'):
            with self.subTest(status=status):
                self.service._journal = [{'id': 'old', 'steamid': STEAMID, 'assetid': '2',
                    'item_key': 'GoldBar', 'variant': 'normal', 'amount': 1,
                    'status': status, 'manual_only': True}]
                self.run_sale()
                self.assertEqual(len(self.service._journal), 1)
                self.assertEqual(self.session.sales, [])

    def test_original_proof_or_game_instance_blocks_adoption_of_a_different_asset(self):
        for fields in ({'proof': 'b' * 32}, {'game_instance_id': 'game-guid'},
                       {'upload_intent_id': 'a' * 64}):
            with self.subTest(fields=fields):
                self.service._journal = [{'id': 'old', 'steamid': STEAMID, 'assetid': '9',
                    'item_key': 'GoldBar', 'variant': 'normal', 'amount': 1, 'status': 'sold', **fields}]
                self.intents = [self.intent(ticket='b' * 32)]
                self.run_sale()
                self.assertEqual(len(self.service._journal), 1)
                self.assertEqual(self.session.sales, [])

    def test_capture_is_durable_and_restart_does_not_recover_twice(self):
        self.session.on_quote = lambda: (_ for _ in ()).throw(SteamSessionError('temporary quote failure'))
        self.run_sale()
        raw = json.loads(self.service._journal_path.read_text(encoding='utf-8'))
        self.assertEqual(raw['records'][0]['status'], 'captured')
        self.assertEqual(raw['records'][0]['upload_intent_id'], 'a' * 64)
        self.assertEqual(raw['records'][0]['game_instance_id'], 'game-guid')
        self.session.on_quote = None
        self.service = self.reopen()
        self.run_sale()
        self.service = self.reopen()
        self.run_sale()
        self.assertEqual(len(self.service._journal), 1)
        self.assertEqual(len(self.session.sales), 1)

    def test_ambiguous_sell_result_never_retries_even_after_restart(self):
        self.session.result = TimeoutError('response lost')
        self.run_sale()
        self.service = self.reopen()
        self.run_sale()
        self.assertEqual(len(self.session.sales), 1)
        self.assertEqual(self.service._journal[0]['status'], 'uncertain')

    def test_no_capture_if_authorization_or_login_changes_during_inventory_read(self):
        for changes in ('policy', 'account', 'generation', 'logout'):
            with self.subTest(changes=changes):
                self.session = FakeSession()
                self.session.inventory = inventory(('2', 1, {'game_instance_id': 'game-guid'}))
                self.service = self.reopen()
                request = self.session.request
                authorized = [True]
                def changed_request(method, path, data=None):
                    response = request(method, path, data)
                    if path.startswith('/inventory/'):
                        if changes == 'policy':
                            authorized[0] = False
                        elif changes == 'account':
                            self.session.wallet['steamid'] = OTHER
                        elif changes == 'generation':
                            self.session.wallet['binding_generation'] = 'new'
                        else:
                            self.session.state = lambda: {'bound': False, 'verified': False}
                    return response
                self.session.request = changed_request
                self.service.sale_once(self.settings, STEAMID, confirmed_uploads=self.intents,
                                       validate=lambda: authorized[0])
                self.assertEqual(self.session.sales, [])
                self.assertEqual(self.service._journal, [])

    def test_policy_change_after_capture_prevents_post(self):
        authorized = [True]
        self.session.on_quote = lambda: authorized.__setitem__(0, False)
        self.service.sale_once(self.settings, STEAMID, confirmed_uploads=self.intents,
                               validate=lambda: authorized[0])
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service._journal[0]['status'], 'captured')

    def test_write_failure_cannot_send_a_sale(self):
        with patch.object(self.service, '_save', side_effect=OSError('disk full')):
            self.run_sale()
        self.assertEqual(self.session.sales, [])

    def test_manual_proof_scope_cannot_adopt_managed_history(self):
        self.run_sale(proof_scope=frozenset({'c' * 32}))
        self.assertEqual(self.service._journal, [])
        self.assertEqual(self.session.sales, [])

    def test_black_mist_catalog_variant_and_native_property_must_both_match(self):
        market_name = 'Example Ring (Black Mist)'
        self.service.catalog.items['LegendaryRing2'] = {'kind': 'equipment', 'rarity': 3,
            'black_mist': {'definition_id': 1000, 'market_hash_name': market_name}}
        self.settings['categories'] = ['black_mist_equipment']
        self.intents = [self.intent(item_key='LegendaryRing2', variant='black_mist')]
        self.session.inventory['descriptions'][0].update(market_hash_name=market_name, type='[Divine] Ring')
        self.run_sale()
        self.assertEqual(self.service._journal, [])
        self.session.inventory['descriptions'][0]['type'] = '[Black Mist] Ring'
        document = self.session.quote.replace('Gold Bar', market_name)
        ssr = json.loads(document.split('>', 1)[1].rsplit('<', 1)[0])
        data = json.loads(ssr['renderContext']['queryData'])
        data['queries'][0]['state']['data']['type'] = '[Black Mist] Ring'
        ssr['renderContext']['queryData'] = json.dumps(data)
        self.session.quote = '<script type="application/json" id="valve-ssr-data">' + json.dumps(ssr) + '</script>'
        self.run_sale()
        self.assertEqual(len(self.session.sales), 1)

    def test_coordinator_recovers_saved_confirmed_upload_after_restart(self):
        coordinator = AuctionCoordinator(self.path, self.service,
            lambda: (True, self.settings, STEAMID), start_worker=False)
        row = {'item_uid': 'native-uid', 'instance_id': 'game-guid', 'internal_name': 'GoldBar'}
        identity = coordinator.submitted(row, STEAMID, managed_run='previous-managed-run')
        coordinator.confirmed(identity, {'uploaded_uid': 'native-uid',
            'uploaded_instance_id': 'game-guid', 'uploaded_internal_name': 'GoldBar'})
        # Reopen both journals without closing them (close revokes fake session).
        self.service = self.reopen()
        restored = AuctionCoordinator(self.path, self.service,
            lambda: (True, self.settings, STEAMID), start_worker=False)
        restored.process_once()
        self.assertEqual(len(self.session.sales), 1)
        self.assertEqual(self.service._journal[0]['upload_intent_id'], identity)
        self.assertEqual(restored.intents[identity]['phase'], 'resolved')
        self.assertEqual(restored.jobs, {})

    def coordinator_with_intents(self, provider, **changes):
        row = self.intent(**changes)
        source = row.pop('upload_intent_id')
        (self.path / 'auction-upload-intents.json').write_text(json.dumps({
            'schema_version': 1, 'intents': {source: row}}), encoding='utf-8')
        return AuctionCoordinator(self.path, self.service, provider, start_worker=False)

    def test_coordinator_retries_missing_identity_at_normal_sale_interval(self):
        self.session.inventory = inventory(('2', 1, {}))
        mono = [1000]
        coordinator = self.coordinator_with_intents(lambda: (True, self.settings, STEAMID))
        with patch('deskrawl_assistant.auction_service.time.monotonic', lambda: mono[0]):
            coordinator.process_once()
            self.assertEqual(self.session.sales, [])
            self.session.inventory = inventory(('2', 1, {'game_instance_id': 'game-guid'}))
            mono[0] += 1
            coordinator.process_once()
            self.assertEqual(self.session.sales, [])
            mono[0] += 29
            coordinator.process_once()
        self.assertEqual(len(self.session.sales), 1)

    def test_coordinator_never_recovers_when_managed_is_stopped(self):
        coordinator = self.coordinator_with_intents(lambda: (False, self.settings, STEAMID))
        coordinator.process_once()
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service._journal, [])

    def test_coordinator_preserves_manual_upload_only_intent(self):
        coordinator = self.coordinator_with_intents(lambda: (True, self.settings, STEAMID), managed_run=None)
        coordinator.process_once()
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service._journal, [])

    def test_coordinator_does_not_recover_partial_corrupt_upload_journal(self):
        coordinator = self.coordinator_with_intents(lambda: (True, self.settings, STEAMID), uid=None)
        self.assertTrue(coordinator.journal_error)
        coordinator.process_once()
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service._journal, [])


if __name__ == '__main__':
    unittest.main()
