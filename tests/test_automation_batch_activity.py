"""Offline batch cancellation and committed-receipt accounting regressions."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_automation as automation


class AutomationBatchActivityTests(unittest.TestCase):
    # Reuse only fixture helpers; inheriting AutomationTests would rerun its
    # unrelated tests. The fixture disables threads and uses a temporary DB.
    def setUp(self):
        automation.AutomationTests.setUp(self)

    enable = automation.AutomationTests.enable

    def test_setting_change_cancels_every_stack_in_prepared_batch(self):
        self.client.items = [automation.gem(index, count=index + 2) for index in range(3)]
        self.enable(gems={'enabled': True})
        before = deepcopy(self.client.items)
        original = self.client.move_items

        def change_settings(expected, target, validate, **options):
            self.assertEqual(len(expected), 3)
            self.assertTrue(options['allow_partial'])
            settings = deepcopy(self.service.automation_settings)
            settings['gems']['enabled'] = False
            self.service.save_automation(settings)
            self.assertFalse(validate())
            return original(expected, target, validate, **options)

        with patch.object(self.client, 'move_items', side_effect=change_settings) as move:
            self.service._automation_once()

        self.assertEqual(move.call_count, 1)
        self.assertEqual(self.client.items, before)
        self.assertEqual(self.client.events, [])
        self.assertFalse(self.service.automation_settings['gems']['enabled'])

    def test_callback_and_service_record_each_batch_movement_once_without_new_loot(self):
        self.client.items = [automation.gem(index, count=index + 1) for index in range(8)]
        self.enable(gems={'enabled': True})
        self.service.operation_kind = 'monitor'
        before_id = self.service.journal.page()['latest_id']
        original = self.client.move_items
        receipts = []

        def committed_receipt(expected, target, validate, **options):
            sources = deepcopy(expected)
            selected = [next(row for row in self.client.items
                             if row['transfer_key'] == source['transfer_key'])
                        for source in sources]
            result = original(expected, target, validate, **options)
            result.update(operation_id='synthetic-eight-stack-receipt', movements=[
                {'name': source['internal_name'], 'identity': '',
                 'count': source['count'], 'merged': False,
                 'source': {'container': source['container'], 'slot_index': source['slot_index'],
                            'count_before': source['count'], 'count_after': 0},
                 'destination': {'container': target, 'slot_index': destination['slot_index'],
                                 'count_before': 0, 'count_after': source['count']}}
                for source, destination in zip(sources[:result['moved']], selected)])
            # Match RuntimeClient: its native on_commit callback runs before
            # _automation_once receives and records the same result again.
            self.client.item_events('items_transferred', result)
            receipts.append(deepcopy(result))
            return result

        with patch.object(self.client, 'move_items', side_effect=committed_receipt) as move, \
             patch.object(self.service.activity, 'apply_movements',
                          wraps=self.service.activity.apply_movements) as apply:
            self.service._automation_once()
            self.service._publish(self.client.snapshot())

        self.assertEqual(move.call_count, 1)
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0]['journal_recorded'])
        self.assertEqual(len(receipts[0]['movements']), 8)
        self.assertEqual(apply.call_count, 1)
        events = [row for row in self.service.journal.page(limit=200)['entries']
                  if row['id'] > before_id]
        transfers = [row for row in events if row['event'] == 'items_transferred']
        self.assertEqual(len(transfers), 8)
        self.assertEqual(sum(row['quantity'] for row in transfers), 36)
        self.assertEqual({row['source']['slot_index'] for row in transfers}, set(range(8)))
        self.assertTrue(all(row['source']['count_after'] == 0 for row in transfers))
        self.assertTrue(all(row['destination']['count_after'] == row['quantity'] for row in transfers))
        self.assertTrue(all(row['context']['automatic'] is True for row in transfers))
        self.assertEqual({row['context']['operation_id'] for row in transfers},
                         {'synthetic-eight-stack-receipt'})
        self.assertFalse(any(row['event'] in ('item_acquired', 'game_item_moved') for row in events))


if __name__ == '__main__':
    unittest.main()
