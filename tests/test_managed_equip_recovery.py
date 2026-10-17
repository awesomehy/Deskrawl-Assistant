"""Retry only an equip rejection proven to precede every native write."""
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.native_dispatcher import NativeActionError, REJECTED
from tests import test_managed_service as fixture


class ManagedEquipRecoveryTests(unittest.TestCase):
    setUp = fixture.ManagedServiceTests.setUp
    settings = fixture.ManagedServiceTests.settings
    early_game = fixture.ManagedServiceTests.early_game

    def test_known_read_only_native_rejection_replans_without_stopping(self):
        self.early_game(1, worn_key='CommonBelt2')
        self.client.native_handler=Mock(side_effect=NativeActionError('游戏原生操作拒绝本次请求',
            state=REJECTED,uncertain=True,last_call=3,failed_method='EquipmentManager.ddv',mutation_started=False))
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed.pending)
        self.assertEqual(self.service.managed_retry['key'],('equip_precheck_rejected','equip'))
        self.assertEqual(self.client.native_handler.call_count,1)
        # Fresh level state is now ineligible; do not retry the old native swap.
        self.client.items[0]['item_level']=100
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=103):
            self.service._work('managed',{})
        self.assertEqual(self.client.native_handler.call_count,1)
        self.assertTrue(self.service.managed.running)

    def test_unknown_or_started_native_write_is_never_replayed(self):
        for mutation in (None,True):
            with self.subTest(mutation=mutation):
                self.service.stop_managed()
                self.early_game(1,worn_key='CommonBelt2')
                self.client.native_handler=Mock(side_effect=NativeActionError('换装未确认',state=REJECTED,
                    uncertain=True,last_call=6,mutation_started=mutation))
                self.service._work('managed',{})
                self.assertFalse(self.service.managed.running)
                self.assertIsNone(self.service.managed_retry)
                self.assertEqual(self.client.native_handler.call_count,1)

    def test_persistent_precheck_rejection_pauses_with_statistics_preserved(self):
        self.early_game(1,worn_key='CommonBelt2')
        self.client.native_handler=Mock(side_effect=NativeActionError('拒绝',state=REJECTED,
            uncertain=True,mutation_started=False))
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=100):
            self.service._work('managed',{})
        metrics=self.service.managed.metrics.state()
        with patch('deskrawl_assistant.managed_service.time.monotonic',return_value=221):
            self.service._work('managed',{})
        self.assertEqual(self.service.managed.phase,'paused')
        self.assertEqual(self.service.managed.metrics.state(),metrics)
        self.assertTrue(self.service.managed_can_resume)


if __name__=='__main__':unittest.main()
