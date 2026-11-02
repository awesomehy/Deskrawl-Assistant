import json
import unittest
from unittest.mock import Mock
from deskrawl_assistant.assistant_settings import validate_preferences
from tests import test_web_server as fixtures


class SettingsRoutesTests(unittest.TestCase):
    setUp = fixtures.HttpTests.setUp
    finish = fixtures.HttpTests.finish
    request = fixtures.HttpTests.request

    def test_settings_require_session_origin_and_do_not_accept_extra_fields(self):
        self.service.assistant_settings_info = Mock(return_value={'steam_acceleration_enabled': False})
        self.service.save_assistant_settings = Mock(side_effect=lambda data, desktop: validate_preferences(data))
        headers = {'Origin': self.url, 'X-Assistant-Token': 'test-session-token'}
        payload = {'steam_acceleration_enabled': True, 'boss_hotkey': ''}
        self.assertEqual(self.request('/api/settings')[0], 403)
        self.assertEqual(self.request('/api/settings', payload)[0], 403)
        self.assertEqual(self.request('/api/settings', payload, {**headers, 'Origin': 'https://other.invalid'})[0], 403)
        self.assertEqual(self.request('/api/settings?path=other', headers=headers)[0], 400)
        self.assertEqual(self.request('/api/settings', {**payload, 'data_directory': 'other'}, headers)[0], 400)
        code, _, body = self.request('/api/settings', payload, headers)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['steam_acceleration_enabled'])

    def test_open_folder_rejects_arbitrary_paths_queries_and_unauthorized_calls(self):
        self.service.open_assistant_data_directory = Mock(return_value={'opened': True})
        headers = {'Origin': self.url, 'X-Assistant-Token': 'test-session-token'}
        self.assertEqual(self.request('/api/settings/open-folder', {})[0], 403)
        self.assertEqual(self.request('/api/settings/open-folder', {'path': 'C:/other'}, headers)[0], 400)
        self.assertEqual(self.request('/api/settings/open-folder?path=other', {}, headers)[0], 400)
        self.service.open_assistant_data_directory.assert_not_called()
        self.assertEqual(self.request('/api/settings/open-folder', {}, headers)[0], 200)

    def test_reset_requires_token_origin_and_strict_category_payload(self):
        self.service.reset_assistant_configuration=Mock(return_value={'reset':True})
        headers={'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        payload={'section':'equipment_rules'}
        self.assertEqual(self.request('/api/settings/reset',payload)[0],403)
        self.assertEqual(self.request('/api/settings/reset',payload,{**headers,'Origin':'https://other.invalid'})[0],403)
        self.assertEqual(self.request('/api/settings/reset',{**payload,'path':'C:/other'},headers)[0],400)
        self.assertEqual(self.request('/api/settings/reset',{'section':[]},headers)[0],400)
        self.assertEqual(self.request('/api/settings/reset?path=other',payload,headers)[0],400)
        self.service.reset_assistant_configuration.assert_not_called()
        self.assertEqual(self.request('/api/settings/reset',payload,headers)[0],200)


if __name__ == '__main__': unittest.main()
