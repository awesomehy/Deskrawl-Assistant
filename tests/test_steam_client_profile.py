"""Local-only Steam display, with no Steam login or network requests."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deskrawl_assistant.steam_client_profile import (
    ClientIdentity, MAX_LOCAL_FILE, STEAM_ID_BASE, SteamClientProfile, WindowsSteamProfile, _keyvalues,
)


def png(width=184, height=184):
    return b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', width, height) + b'\x08\x06\x00\x00\x00' + b'\x00' * 4


class SteamClientProfileTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.identity = ClientIdentity('online', 42, 123, 7, Path('fake-steam'))
        self.backend = Mock()
        self.backend.identity.return_value = self.identity
        self.backend.profile.return_value = ('昵称', png())
        self.profile = SteamClientProfile(self.backend, clock=lambda: self.now)

    def test_public_state_exposes_only_display_fields_and_versioned_local_avatar(self):
        state = self.profile.state()
        self.assertEqual(set(state), {'status', 'nickname', 'avatar_url'})
        self.assertEqual(state['status'], 'online')
        version = state['avatar_url'].split('=')[1]
        self.assertEqual(self.profile.avatar(version), png())
        self.assertNotIn('fake-steam', json.dumps(state))
        self.assertEqual(self.backend.profile.call_count, 1)

    def test_identity_refresh_is_independent_of_profile_file_refresh(self):
        first = self.profile.state()
        self.now = 1
        self.assertEqual(self.profile.state(), first)
        self.assertEqual(self.backend.identity.call_count, 2)
        self.now = 2
        self.profile.state()
        self.assertEqual(self.backend.identity.call_count, 3)
        self.backend.profile.assert_called_once()
        self.backend.profile.return_value = ('新昵称', b'')
        self.now = 30
        state = self.profile.state()
        self.assertEqual(state['nickname'], '新昵称')
        self.assertEqual(state['avatar_url'], '')

    def test_signout_or_stop_clears_old_nickname_and_image(self):
        version = self.profile.state()['avatar_url'].split('=')[1]
        for status in ('logged_out', 'not_running', 'unknown'):
            self.now += 2
            self.backend.identity.return_value = ClientIdentity(status)
            self.assertEqual(self.profile.state(), {'status':status, 'nickname':'', 'avatar_url':''})
            self.assertIsNone(self.profile.avatar(version))

    def test_switching_account_immediately_reloads_profile_not_most_recent(self):
        self.profile.state()
        self.backend.identity.return_value = ClientIdentity('online', 42, 123, 8, Path('fake-steam'))
        self.backend.profile.return_value = ('另一个账号', b'')
        self.now = 2
        self.assertEqual(self.profile.state()['nickname'], '另一个账号')
        self.assertEqual(self.backend.profile.call_count, 2)

    def test_race_during_read_does_not_publish_wrong_account(self):
        self.backend.identity.side_effect = [self.identity, ClientIdentity('logged_out')]
        self.assertEqual(self.profile.state(), {'status':'unknown','nickname':'','avatar_url':''})
        self.assertEqual(self.profile.image, b'')

    def test_permission_failure_drops_previously_cached_account(self):
        self.profile.state()
        self.now = 2
        self.backend.identity.side_effect = PermissionError('private path and identifier')
        self.assertEqual(self.profile.state(), {'status':'unknown','nickname':'','avatar_url':''})

    def test_missing_profile_and_avatar_failure_remain_logged_in(self):
        self.backend.profile.side_effect = FileNotFoundError('private path')
        self.assertEqual(self.profile.state()['status'], 'profile_unavailable')
        self.now = 30
        self.backend.profile.side_effect = None
        self.backend.profile.return_value = ('昵称', b'')
        self.assertEqual(self.profile.state(), {'status':'online','nickname':'昵称','avatar_url':''})

    def test_avatar_rejects_arbitrary_paths_and_stale_versions(self):
        self.profile.state()
        for value in ('../loginusers.vdf', '', None, 'a'*64, ['a']):
            self.assertIsNone(self.profile.avatar(value))


class WindowsSteamProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'config'/'avatarcache').mkdir(parents=True)
        self.account = 123
        self.sid = str(STEAM_ID_BASE + self.account)
        self.identity = ClientIdentity('online',42,123,self.account,self.root)
        self.backend = WindowsSteamProfile()
        self.vdf = self.root/'config'/'loginusers.vdf'
        self.avatar = self.root/'config'/'avatarcache'/(self.sid+'.png')

    def write(self, nickname='名字 😀'):
        self.vdf.write_text('"users" { "9999" {"PersonaName" "wrong" "MostRecent" "1"} "'+self.sid+'" {"PersonaName" "'+nickname+'" "AccountName" "never-show-this"}}',encoding='utf-8-sig')

    def test_exact_active_user_only_and_avatar_png(self):
        self.write()
        self.avatar.write_bytes(png())
        self.assertEqual(self.backend.profile(self.identity), ('名字 😀', png()))

    def test_stale_loginusers_never_uses_previous_or_account_name(self):
        self.vdf.write_text('"users" {"9999" {"PersonaName" "old" "MostRecent" "1"} "'+self.sid+'" {"AccountName" "private-login"}}',encoding='utf-8')
        self.assertEqual(self.backend.profile(self.identity), ('', b''))

    def test_quoted_name_escapes_and_comments_are_supported(self):
        self.write(r'quote \" slash \\ name')
        self.assertEqual(self.backend.profile(self.identity)[0], 'quote " slash \\ name')
        self.assertEqual(_keyvalues('// example\n "users" {"active" "x"}'), {'users':{'active':'x'}})

    def test_malformed_oversized_or_ambiguous_profile_is_not_used(self):
        for text in ('"users" {', '"users" {} }', '"users" {} "USERS" {}', '"users" {"x"', 'x', '{"users"{}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                _keyvalues(text)
        self.vdf.write_bytes(b' '*(MAX_LOCAL_FILE+1))
        with self.assertRaises(ValueError): self.backend.profile(self.identity)

    def test_missing_oversized_or_wrong_format_avatar_does_not_hide_name(self):
        self.write('可保留的昵称')
        for data in (b'', b'<svg/>', b'x'*(MAX_LOCAL_FILE+1), png(2000), png(0)):
            self.avatar.write_bytes(data)
            self.assertEqual(self.backend.profile(self.identity), ('可保留的昵称', b''))

    def test_no_active_pid_distinguishes_steam_absent_from_unknown_login(self):
        with patch.object(self.backend, '_active_process', return_value=(0,None)), patch.object(self.backend, '_steam_running', return_value=False) as running:
            self.assertEqual(self.backend.identity().status, 'not_running')
            running.return_value = True
            self.assertEqual(self.backend.identity().status, 'unknown')

    def test_active_process_with_zero_user_is_logged_out(self):
        with patch.object(self.backend, '_active_process', return_value=(42,0)), patch.object(self.backend, '_process', return_value=(self.root,123)):
            self.assertEqual(self.backend.identity().status, 'logged_out')

    def test_missing_account_cannot_use_persisted_most_recent_account(self):
        for account in (None, '123', -1, True, 2**32):
            with patch.object(self.backend, '_active_process', return_value=(42,account)), patch.object(self.backend, '_process', return_value=(self.root,123)):
                self.assertEqual(self.backend.identity().status, 'unknown')

    def test_exited_steam_process_is_not_treated_as_logged_in(self):
        error=OSError('exited');error.winerror=1168
        with patch.object(self.backend, '_active_process', return_value=(42,123)), patch.object(self.backend, '_process', side_effect=error), patch.object(self.backend, '_steam_running', return_value=False):
            self.assertEqual(self.backend.identity().status, 'not_running')


if __name__ == '__main__':
    unittest.main()
