import json
from pathlib import Path
import tempfile
import unittest

from deskrawl_assistant.steam_login_diagnostics import (
    LoginDiagnostics, diagnostic_target, response_summary,
)


class LoginDiagnosticsTests(unittest.TestCase):
    def test_targets_never_keep_query_identity_or_foreign_urls(self):
        self.assertEqual(diagnostic_target('https://login.steampowered.com/jwt/finalizelogin?nonce=secret'),
                         {'host': 'login.steampowered.com', 'path': '/jwt/finalizelogin'})
        self.assertIsNotNone(diagnostic_target('https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1/?token=secret'))
        for url in ('https://steamcommunity.com/profiles/76561198000000001',
                    'https://user:password@login.steampowered.com/jwt/finalizelogin',
                    'https://login.steampowered.com.evil.invalid/jwt/finalizelogin',
                    'https://login.steampowered.com:8443/jwt/finalizelogin', 'data:secret'):
            self.assertIsNone(diagnostic_target(url))

    def test_only_numeric_and_structural_response_fields_survive(self):
        raw = json.dumps({'steamID': '76561198000000001', 'refresh_token': 'secret',
            'message': 'sensitive', 'result': 1, 'eresult': 'secret', 'success': 'secret',
            'primary_domain': '.steamcommunity.com',
            'transfer_info': [{'url': 'https://steamcommunity.com/login/settoken?token=secret',
                               'params': {'auth': 'secret'}}]})
        self.assertEqual(response_summary(raw), {'result': 1, 'identity_present': True,
            'primary_domain': 'steamcommunity.com', 'transfer_list_present': True,
            'transfer_count': 1, 'transfer_hosts': ['steamcommunity.com']})
        self.assertEqual(response_summary('not json'), {})
        self.assertEqual(response_summary('["secret"]'), {})

    def test_log_is_bounded_and_query_and_credentials_are_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'login.json'
            diag = LoginDiagnostics(path)
            for _ in range(100):
                diag.record('response', diagnostic_target('https://login.steampowered.com/jwt/finalizelogin?nonce=secret'), status=200)
            diag.record('response', None, status=200)
            raw = path.read_text('utf-8')
            self.assertNotIn('secret', raw)
            self.assertNotIn('nonce', raw)
            self.assertEqual(len(json.loads(raw)['events']), 80)

    def test_unwritable_diagnostics_do_not_break_login(self):
        with tempfile.TemporaryDirectory() as directory:
            diag = LoginDiagnostics(Path(directory))
            diag.record('request', diagnostic_target('https://steamcommunity.com/login/home/'), method='GET')
