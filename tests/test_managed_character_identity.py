"""Same-connection character changes use offline control snapshots only."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.managed_mode import ManagedMode
from deskrawl_assistant.models import ValidationError
from tests import test_managed_service as service_fixtures
from tests import test_managed_synthesis_service as synthesis_fixtures
from tests.test_managed_mode import observation, settings
from tests.test_synthesis import config, drop_count, gem, world


class ManagedCharacterIdentityTests(unittest.TestCase):
    def test_same_connection_character_change_pauses_before_accounting(self):
        before = world()
        engine = ManagedMode(config())
        engine.start(before)
        after = world([gem()])
        after['character_id'] = '0:1:another-character'
        self.assertEqual(after['session'], before['session'])
        self.assertIsNone(engine.next_action(after))
        self.assertEqual(engine.phase, 'paused')
        self.assertFalse(engine.running)
        self.assertEqual(drop_count(engine.metrics), 0)

    def test_missing_actual_identity_waits_without_using_display_character(self):
        before = world([gem()])
        before['character_id'] = None
        before['character']['character_id'] = '0:0:stale-display'
        engine = ManagedMode(config())
        engine.start(before)
        self.assertIsNone(engine.character_id)
        self.assertIsNone(engine.next_action(before))
        self.assertTrue(engine.running)
        self.assertIsNone(engine.pending)
        self.assertIn('身份', engine.status)

    def test_first_actual_identity_can_bind_after_an_unknown_start(self):
        before = world([gem()])
        before['character_id'] = None
        engine = ManagedMode(config())
        engine.start(before)
        self.assertIsNone(engine.next_action(before))
        known = world([gem()])
        self.assertEqual(engine.next_action(known)['kind'], 'synthesize_gems')
        self.assertEqual(engine.character_id, known['character_id'])

    def test_missing_identity_also_blocks_ingredient_withdrawal(self):
        before = world([gem(container='storage', slot=50)])
        before['character_id'] = None
        engine = ManagedMode(config())
        engine.start(before)
        self.assertIsNone(engine.next_action(before))
        self.assertIsNone(engine.pending)
        self.assertTrue(engine.running)

    def test_existing_ordinary_mode_does_not_gain_an_identity_requirement(self):
        before = observation()
        engine = ManagedMode(settings())
        engine.start(before)
        engine.next_action(before)
        self.assertTrue(engine.running)
        self.assertIsNone(engine.character_id)

    def confirmed_craft(self):
        before = world([gem(100)])
        engine = ManagedMode(config())
        engine.start(before)
        action = engine.next_action(before)
        output = dict(gem(10, grade=2, slot=1), quantity=10)
        engine.complete(action['action_id'], result={'verified':True, 'effect_confirmed':True,
            'inputs':action['inputs'], 'outputs':[output], 'receipt_id':'role-bound-craft'})
        return engine, before

    def test_role_change_preserves_confirmed_craft_fence_and_rejects_resume(self):
        engine, before = self.confirmed_craft()
        fence = deepcopy(engine.await_effect)
        changed = deepcopy(before)
        changed['character_id'] = '1:0:another-character'
        self.assertIsNone(engine.next_action(changed))
        self.assertEqual(engine.phase, 'paused')
        self.assertEqual(engine.await_effect, fence)
        with self.assertRaisesRegex(ValidationError, '角色已改变'):
            engine.resume(changed)
        engine.resume(before)
        self.assertEqual(engine.await_effect, fence)
        self.assertIsNone(engine.next_action(before))
        self.assertEqual(engine.await_effect, fence)
        self.assertEqual(engine.sequence, 1)

    def test_unknown_identity_does_not_age_or_clear_confirmed_craft_fence(self):
        engine, before = self.confirmed_craft()
        fence = deepcopy(engine.await_effect)
        unknown = deepcopy(before)
        unknown['character_id'] = None
        for _ in range(6):
            self.assertIsNone(engine.next_action(unknown))
        self.assertTrue(engine.running)
        self.assertEqual(engine.await_count, 0)
        self.assertEqual(engine.await_effect, fence)


class ManagedSynthesisIdentityServiceTests(unittest.TestCase):
    setUp = service_fixtures.ManagedServiceTests.setUp
    enable = synthesis_fixtures.ManagedSynthesisServiceTests.enable
    craft = synthesis_fixtures.ManagedSynthesisServiceTests.craft

    def setup_craft(self):
        self.enable(gems=True)
        self.client.synthesis_snapshot = Mock(return_value={'gems':{'available':True, 'recipes':[]}})
        return self.craft(), self.service._managed_observation()

    def test_observation_uses_only_successful_actual_control_identity(self):
        self.enable(gems=True)
        self.client.character_snapshot = Mock(return_value={'available':False, 'session':'offline-session',
            'character_id':'0:8:display', 'equipment':[]})
        self.client.character_id = '0:2:actual'
        value = self.service._managed_observation()
        self.assertEqual(value['character_id'], '0:2:actual')
        self.assertEqual(value['character']['character_id'], '0:8:display')
        self.client.character_id = None
        self.assertIsNone(self.service._managed_observation()['character_id'])
        self.client.control_snapshot = Mock(return_value={'available':False,
            'character_id':'0:2:stale', 'scene':{'in_town':True}})
        self.assertIsNone(self.service._managed_observation()['character_id'])

    def test_lightweight_context_does_not_reuse_cached_character_identity(self):
        self.enable(gems=True)
        self.service._managed_observation()
        self.client.action_context_snapshot = Mock(return_value={'available':True,
            'scene':{'available':True, 'in_town':True}})
        self.assertIsNone(self.service._managed_observation(inventory_only=True)['character_id'])

    def test_fresh_observation_character_change_blocks_old_craft(self):
        action, before = self.setup_craft()
        self.client.character_id = '0:1:another-character'
        with patch('deskrawl_assistant.synthesis.next_action', return_value=deepcopy(action)) as planner:
            result = self.service._managed_execute(action, before)
        planner.assert_not_called()
        self.assertTrue(result['retryable'])
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.actions, [])

    def test_final_submit_identity_read_blocks_character_changed_during_replanning(self):
        action, before = self.setup_craft()
        original = self.client.control_snapshot()
        changed = {**original, 'character_id':'0:1:another-character'}
        self.client.control_snapshot = Mock(side_effect=[original, changed])
        with patch('deskrawl_assistant.synthesis.next_action', return_value=deepcopy(action)):
            result = self.service._managed_execute(action, before)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertEqual(self.client.actions, [])

    def test_final_unknown_identity_waits_and_does_not_submit(self):
        action, before = self.setup_craft()
        original = self.client.control_snapshot()
        for last in ({'available':False}, {**original, 'character_id':None}, RuntimeError('login changing')):
            with self.subTest(last=last):
                self.client.control_snapshot = Mock(side_effect=[original, last])
                with patch('deskrawl_assistant.synthesis.next_action', return_value=deepcopy(action)):
                    result = self.service._managed_execute(action, before)
                self.assertTrue(result['retryable'])
                self.assertTrue(self.service.managed.running)
                self.assertEqual(self.client.actions, [])

    def test_changed_character_blocks_uidless_ingredient_withdrawal(self):
        self.enable(gems=True)
        self.client.synthesis_snapshot = Mock(return_value={'gems':{'available':True, 'recipes':[]}})
        self.client.items = [service_fixtures.equipment(0, container='storage', key='GemRuby1',
            is_equipment=False, item_class='GemData', kind='gem', count=10, item_uid='', instance_id='')]
        before = self.service._managed_observation()
        action = {'kind':'transfer', 'reason_code':'synthesis_prepare',
            'item':before['items'][0], 'target':'inventory'}
        original = self.client.control_snapshot()
        changed = {**original, 'character_id':'0:1:another-character'}
        self.client.control_snapshot = Mock(side_effect=[original, changed])
        with patch('deskrawl_assistant.synthesis.next_action', return_value=deepcopy(action)):
            result = self.service._managed_execute(action, before)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertEqual(self.client.items[0]['container'], 'storage')
        self.assertEqual(self.client.actions, [])

    def test_finish_requested_during_final_identity_read_blocks_new_craft(self):
        action, before = self.setup_craft()
        original = self.client.control_snapshot()
        count = 0
        def read():
            nonlocal count
            count += 1
            if count == 2: self.service.managed.finishing = True
            return deepcopy(original)
        self.client.control_snapshot = read
        with patch('deskrawl_assistant.synthesis.next_action', return_value=deepcopy(action)):
            result = self.service._managed_execute(action, before)
        self.assertTrue(result['retryable'])
        self.assertEqual(self.client.actions, [])

    def test_character_change_is_checked_before_transition_completion(self):
        self.setup_craft()
        action = self.service.managed._action('return_town')
        self.service.managed_transition = {'action':action, 'session':'offline-session'}
        self.client.character_id = '0:1:another-character'
        self.service._managed_once()
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertTrue(self.service.managed_can_resume)
        self.assertIsNone(self.service.managed.await_effect)
        self.assertEqual(self.client.actions, [])


if __name__ == '__main__':
    unittest.main()
