"""Independent real raw/view-shape regressions; all game data are offline mocks."""
from copy import deepcopy
import unittest

from deskrawl_assistant.managed_mode import ManagedMetrics, ManagedMode
from tests.test_synthesis import config, drop_count, gem, world


def native_gem(count=10, slot=0, grade=1, **extra):
    # NativeReader.base_item returns base_rarity; UID-less GemData has no
    # GeneratedItemData override and therefore no `rarity` member.
    value = gem(count, slot=slot, grade=grade, base_rarity=0, item_uid='', **extra)
    value.pop('rarity', None)
    return value


def managed_gem(*args, **kwargs):
    # ManagedService._managed_observation decorates raw non-equipment rows.
    value = native_gem(*args, **kwargs)
    return {**value, 'rarity': value['base_rarity'], 'rune_level': None, 'rune_xp': None}


class ManagedNativeReceiptAudit(unittest.TestCase):
    def test_native_gem_receipt_matches_normalized_managed_effect_fence(self):
        before = world([managed_gem(100)])
        engine = ManagedMode(config())
        engine.start(before)
        action = engine.next_action(before)
        self.assertEqual(action['kind'], 'synthesize_gems')
        receipt = {'verified': True, 'effect_confirmed': True, 'receipt_id': 'native-shape',
                   'inputs': [{**native_gem(100), 'quantity': 100}],
                   'outputs': [{**native_gem(10, slot=1, grade=2), 'quantity': 10}]}
        engine.complete(action['action_id'], result=receipt)
        after = world([managed_gem(10, slot=1, grade=2)])
        next_action = engine.next_action(after)
        self.assertIsNotNone(next_action, engine.status)
        self.assertEqual(next_action['kind'], 'synthesize_gems')
        self.assertEqual(next_action['recipe_id'], '2')
        self.assertTrue(engine.running)

    def test_native_receipt_base_rarity_does_not_turn_crafted_gem_into_loot(self):
        metrics = ManagedMetrics()
        metrics.observe(world([managed_gem(10)]))
        metrics.synthesized('native-shape',
            [{**native_gem(10), 'quantity': 10}],
            [{**native_gem(1, grade=2), 'quantity': 1}])
        metrics.observe(world([managed_gem(1, grade=2)]))
        self.assertEqual(drop_count(metrics), 0)


if __name__ == '__main__':
    unittest.main()
