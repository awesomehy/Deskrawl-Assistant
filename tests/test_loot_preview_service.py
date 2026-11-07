"""Live previews must never survive a lost or replaced run."""
from copy import deepcopy
from datetime import datetime
import threading
import unittest
from unittest.mock import Mock
from deskrawl_assistant.web_service import AssistantService


def plan(key='first'):
    return {'available':True,'phase':'active','reason':'',
        'run':{'key':key,'map_name':'测试地图','current_wave':1,'total_waves':1},
        'totals':{'items':1,'equipment':1,'gold':5,'potion_charges':0},
        'waves':[{'number':1,'items':[{'name':'测试装备'}]}],'warnings':[]}


class LootPreviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = AssistantService.__new__(AssistantService)
        self.service.guard = threading.RLock()
        self.service.closed = threading.Event()
        self.service.updating = False
        self.service.client = Mock(connected=True)
        self.service.client.loot_preview_snapshot.return_value = plan()

    def assert_empty(self, value):
        self.assertFalse(value['available'])
        self.assertIsNone(value['run'])
        self.assertEqual(value['waves'],[])
        self.assertFalse(any(value['totals'].values()))

    def test_each_get_reads_current_run_without_mutating_reader_copy(self):
        raw = plan()
        original = deepcopy(raw)
        self.service.client.loot_preview_snapshot.return_value = raw
        first = self.service.loot_preview_page()
        self.service.client.loot_preview_snapshot.return_value = plan('second')
        second = self.service.loot_preview_page()
        self.assertEqual(first['run']['key'],'first')
        self.assertEqual(second['run']['key'],'second')
        self.assertEqual(raw,original)
        self.assertEqual(self.service.client.loot_preview_snapshot.call_count,2)
        self.assertNotIn('capture_monitor', first)
        self.service.client.loot_capture_snapshot.assert_not_called()
        self.assertIsNotNone(datetime.fromisoformat(second['updated_at']).tzinfo)

    def test_reader_failure_clears_previous_plan_and_hides_internal_details(self):
        self.assertTrue(self.service.loot_preview_page()['available'])
        self.service.client.loot_preview_snapshot.side_effect = RuntimeError('runId=secret address=0x123')
        failed = self.service.loot_preview_page()
        self.assert_empty(failed)
        self.assertEqual(failed['phase'],'unavailable')
        self.assertNotIn('secret',str(failed))
        self.assertNotIn('0x123',str(failed))

    def test_unavailable_reader_payload_cannot_retain_stale_waves(self):
        for phase in ('waiting','unsupported','unavailable'):
            with self.subTest(phase=phase):
                raw = plan()
                raw.update(available=False,phase=phase,reason='等待开局')
                self.service.client.loot_preview_snapshot.return_value = raw
                value = self.service.loot_preview_page()
                self.assert_empty(value)
                self.assertEqual(value['phase'],phase)

    def test_disconnection_during_read_discards_completed_copy(self):
        def disconnected():
            self.service.client.connected = False
            return plan()
        self.service.client.loot_preview_snapshot.side_effect = disconnected
        self.assert_empty(self.service.loot_preview_page())

    def test_disconnected_updating_and_closed_do_not_read_memory(self):
        for state in ('disconnected','updating','closed'):
            with self.subTest(state=state):
                self.service.client.connected = state != 'disconnected'
                self.service.updating = state == 'updating'
                self.service.closed.clear()
                if state == 'closed': self.service.closed.set()
                self.assert_empty(self.service.loot_preview_page())
        self.service.client.loot_preview_snapshot.assert_not_called()

    def test_empty_item_plan_is_available_and_preserves_gold(self):
        raw = plan()
        raw['totals'].update(items=0,equipment=0)
        raw['waves'][0]['items'] = []
        self.service.client.loot_preview_snapshot.return_value = raw
        value = self.service.loot_preview_page()
        self.assertTrue(value['available'])
        self.assertEqual(value['totals']['gold'],5)
        self.assertEqual(value['waves'][0]['items'],[])


if __name__ == '__main__': unittest.main()
