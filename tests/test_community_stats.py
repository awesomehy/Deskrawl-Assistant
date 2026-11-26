import copy
import json
from pathlib import Path
import tempfile
import unittest
from http.server import ThreadingHTTPServer
import threading
from types import SimpleNamespace
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from deskrawl_assistant.community_stats import CommunityStats, DEFAULT_URL, LEGACY_DEFAULT_URL, validate_url

STATISTICS = {'available': True, 'session': 'one', 'source': 'verified_game_statistics',
              'class_mask': 4, 'level': 70, 'play_seconds': 123456, 'divine_count': 17, 'rune_set_count': 9}


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'stats.sqlite3'
        self.calls, self.fail = [], False
        def transport(url, data, token=''):
            self.calls.append((url, copy.deepcopy(data), token))
            if url.endswith('register'):
                return {'schema': 1, 'client_id': data['client_id'], 'token': 'a' * 64}
            if self.fail:
                return {'accepted': False}
            return {'accepted': True, 'report_id': data['report_id'], 'sequence': data['sequence'],
                    'accepted_at': '2026-10-14T00:00:00+00:00'}
        self.client = CommunityStats(self.path, '1.3.0', lambda: True, start=False, transport=transport)
        self.addCleanup(self.client.close)

    def enable(self):
        self.client.configure({'url': 'http://127.0.0.1:46293'})

    def test_startup_reports_automatically_with_game_history(self):
        calls = []
        self.client.statistics_provider = lambda: calls.append(True) or STATISTICS
        self.assertTrue(self.client.status()['enabled'])
        self.assertEqual(self.client.status()['url'], DEFAULT_URL)
        self.assertTrue(self.client.report_once())
        self.assertEqual(calls, [True])
        self.assertEqual(self.calls[-1][0], DEFAULT_URL+'/v1/report')
        self.assertEqual(self.client.status()['divine_count'], 17)

    def test_first_read_reports_game_history_without_inventory(self):
        self.enable(); self.client.observe(STATISTICS)
        self.assertTrue(self.client.report_once())
        report = self.calls[-1][1]
        self.assertEqual((report['play_seconds'], report['divine_count'], report['rune_set_count']), (123456, 17, 9))
        self.assertTrue(report['connected']); self.assertEqual(report['player_class'], 'hunter')
        self.assertNotIn('session', report); self.assertNotIn('source', report)
        self.assertNotIn('character_id', report); self.assertNotIn('item_uid', report)

    def test_reconnect_and_restart_dont_recount_or_lose_config(self):
        self.enable(); self.client.observe(STATISTICS)
        self.client.observe({**STATISTICS, 'session': 'two'})
        self.assertEqual(self.client.status()['divine_count'], 17)
        reopened = CommunityStats(self.path, '1.3.0', start=False); self.addCleanup(reopened.close)
        self.assertTrue(reopened.status()['enabled'])
        self.assertEqual(reopened.status()['url'], 'http://127.0.0.1:46293')
        reopened.observe(STATISTICS)
        self.assertEqual(reopened.status()['divine_count'], 17)

    def test_receipt_must_match_and_retries_exact_payload(self):
        self.enable(); self.client.observe(STATISTICS); self.fail = True
        with self.assertRaises(ValueError): self.client.report_once()
        self.assertEqual(self.client.status()['acknowledged'], 0)
        previous = self.calls[-1][1]
        self.client.observe({**STATISTICS, 'divine_count': 18})
        self.fail = False; self.assertTrue(self.client.report_once())
        self.assertEqual(self.calls[-1][1], previous)
        self.client.report_once()
        self.assertEqual(self.calls[-1][1]['divine_count'], 18)
        self.assertEqual(self.client.status()['acknowledged'], 2)

    def test_first_unavailable_read_reports_presence_without_claiming_a_known_character(self):
        self.enable(); self.client.observe({'available': False})
        self.assertTrue(self.client.report_once())
        self.assertFalse(self.calls[-1][1]['connected'])
        self.assertEqual(self.calls[-1][1]['player_class'], 'unknown')
        self.assertIsNone(self.calls[-1][1]['level'])
        self.assertFalse(self.client.status()['statistics_valid'])

    def test_failed_read_preserves_totals_and_omits_stale_character(self):
        self.enable(); self.client.observe(STATISTICS)
        for invalid in ({'available': False}, {**STATISTICS, 'play_seconds': -1},
                        {**STATISTICS, 'source': 'inventory_estimate'}, {**STATISTICS, 'rune_set_count': True}):
            self.client.observe(invalid)
            self.assertEqual(self.client.status()['rune_set_count'], 9)
        self.assertTrue(self.client.report_once())
        self.assertFalse(self.calls[-1][1]['connected'])
        self.assertIsNone(self.calls[-1][1]['level'])
        self.assertEqual(self.calls[-1][1]['play_seconds'], 123456)

    def test_switching_character_replaces_previous_totals(self):
        self.enable(); self.client.observe(STATISTICS)
        self.client.observe({**STATISTICS, 'class_mask': 8, 'level': 10, 'play_seconds': 120,
                             'divine_count': 0, 'rune_set_count': 0})
        self.client.report_once()
        self.assertEqual((self.calls[-1][1]['player_class'], self.calls[-1][1]['play_seconds'],
                          self.calls[-1][1]['divine_count']), ('monk', 120, 0))

    def test_background_read_does_not_depend_on_inventory_monitoring(self):
        self.enable(); calls = []
        self.client.statistics_provider = lambda: calls.append(True) or STATISTICS
        self.assertTrue(self.client.report_once())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.calls[-1][1]['divine_count'], 17)
        self.client.statistics_provider = lambda: (_ for _ in ()).throw(RuntimeError('Temporary read failure'))
        self.assertTrue(self.client.report_once())
        self.assertEqual(self.calls[-1][1]['divine_count'], 17)
        self.assertFalse(self.calls[-1][1]['connected'])

    def test_legacy_cache_and_pending_are_invalidated(self):
        with self.client.lock, self.client.db:
            self.client._set('statistics_source', 'old_inventory_estimate')
            self.client._set('divine_count', 999)
            self.client._set('pending', {'divine_count': 999})
        reopened = CommunityStats(self.path, '1.3.0', start=False); self.addCleanup(reopened.close)
        self.assertFalse(reopened.status()['statistics_valid'])
        self.assertEqual(reopened.status()['divine_count'], 0)
        self.assertIsNone(reopened.values['pending'])

    def test_only_https_is_allowed_for_public_listeners(self):
        for url in ('http://hhyan.me:46293', 'http://other.example:46293', 'http://hhyan.me:80',
                    'http://hhyan.me.evil.example:46293', 'https://user:secret@hhyan.me:46293',
                    'https://hhyan.me/a', 'https://hhyan.me?token=x'):
            with self.subTest(url=url), self.assertRaises(ValueError): validate_url(url)
        self.assertEqual(validate_url('https://hhyan.me:46293/'), DEFAULT_URL)
        self.assertEqual(validate_url('http://192.168.50.90:46293'), 'http://192.168.50.90:46293')

    def test_upgrade_enables_reporting_and_migrates_legacy_http_default_to_https(self):
        with self.client.lock, self.client.db:
            self.client._set('enabled', False)
            self.client._set('url', LEGACY_DEFAULT_URL)
            self.client._set('token', 'a'*64)
            self.client._set('pending', {'old': True})
        reopened = CommunityStats(self.path, '1.3.0', start=False); self.addCleanup(reopened.close)
        self.assertTrue(reopened.status()['enabled'])
        self.assertEqual(reopened.status()['url'], DEFAULT_URL)
        self.assertEqual(reopened.values['token'], '')
        self.assertIsNone(reopened.values['pending'])

    def test_upgrade_preserves_custom_https_server(self):
        with self.client.lock, self.client.db:
            self.client._set('url', 'https://example.com'); self.client._set('acknowledged', 0)
        reopened = CommunityStats(self.path, '1.3.0', start=False)
        try:self.assertEqual(reopened.status()['url'], 'https://example.com')
        finally:reopened.close()

    def test_configuration_cannot_disable_automatic_reporting(self):
        with self.assertRaises(ValueError):self.client.configure({'enabled': False, 'url': DEFAULT_URL})
        self.assertTrue(self.client.status()['enabled'])


class CommunityAPITests(unittest.TestCase):
    def test_status_requires_local_session_hides_identity_and_has_no_toggle_endpoint(self):
        from deskrawl_assistant.web_server import make_handler
        with tempfile.TemporaryDirectory() as directory:
            stats = CommunityStats(Path(directory) / 'stats.sqlite3', '1.3.0', start=False)
            service = SimpleNamespace(community_status=stats.status)
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service, 'test-session'))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with self.assertRaises(HTTPError) as error: urlopen(base + '/api/community')
                self.assertEqual(error.exception.code, 403)
                headers = {'X-Assistant-Token': 'test-session', 'Content-Type': 'application/json', 'Origin': base}
                with urlopen(Request(base + '/api/community', headers=headers)) as response:
                    state = json.load(response)
                self.assertTrue(state['enabled']); self.assertNotIn('token', state); self.assertNotIn('client_id', state)
                data = json.dumps({'enabled': False, 'url': 'http://192.168.50.90:46293'}).encode()
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + '/api/community/settings', data, headers))
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + '/api/community/settings', data, {**headers, 'Origin': 'https://foreign.example'}))
                self.assertEqual(error.exception.code, 403)
            finally:
                server.shutdown(); server.server_close(); worker.join(2); stats.close()


if __name__ == '__main__': unittest.main()
