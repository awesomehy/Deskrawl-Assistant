"""Tray data uses accepted run loot and cached reads; no real game or Steam calls."""
from copy import deepcopy
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.desktop_tray import TraySnapshotCache
from deskrawl_assistant.managed_mode import ManagedMetrics, ManagedMode
from deskrawl_assistant.tray_status import TrayStatusMixin, dungeon_text, loot_lines
from deskrawl_assistant.world_boss import PUMPKIN
from tests.test_managed_mode import equipment, loot, observation, settings


def control(map_id='a', difficulty='Inferno', **changes):
    return {'available': True, 'scene': 'combat', 'map_id': map_id, 'difficulty': difficulty,
            'character_id': 'hero', 'entry_items': {'Skull': 10, 'SkullNightmare': 20, 'SkullInferno': 100},
            'maps_by_difficulty': {difficulty: [{'map_id': map_id, 'name': '灰烬之冠 7'}]}, **changes}


class DungeonTests(unittest.TestCase):
    def test_world_boss_displays_remaining_not_used_or_run_completions(self):
        state = control(PUMPKIN)
        state['maps_by_difficulty']['Inferno'][0]['world_boss'] = {
            'available': True, 'remaining': 20, 'daily_limit': 100}
        self.assertEqual(dungeon_text(state), '当前副本：南瓜王（今日剩余 20/100）')
        state['maps_by_difficulty']['Inferno'][0]['world_boss']['remaining'] = 0
        self.assertIn('0/100', dungeon_text(state))

    def test_unknown_quota_is_not_zero(self):
        self.assertIn('未知/100', dungeon_text(control(PUMPKIN)))
        for value in (True, -1, 101, '20'):
            state = control(PUMPKIN)
            state['maps_by_difficulty']['Inferno'][0]['world_boss'] = {'available': True, 'remaining': value}
            self.assertIn('未知/100', dungeon_text(state))

    def test_each_normal_tier_uses_its_own_skull_balance(self):
        for tier, label, count in (('Normal', '恶魔', 10), ('Nightmare', '噩梦', 20), ('Inferno', '炼狱', 100)):
            self.assertIn(f'灰烬之冠 7（{label}头骨：{count}）', dungeon_text(control(difficulty=tier)))

    def test_mythic_uses_entry_difficulty_instead_of_world_difficulty(self):
        state = control('MythicDungeon::mythic::22', 'Mythic',
                        mythic_entry_difficulty='Inferno', world_difficulty='Normal')
        self.assertEqual(dungeon_text(state), '当前副本：神话裂隙 · 第 22 层（炼狱头骨：100）')

    def test_stale_disconnected_switching_and_town_do_not_show_a_stale_target(self):
        self.assertEqual(dungeon_text(control(), fresh=False), '当前副本：等待读取')
        self.assertEqual(dungeon_text(control(available=False)), '当前副本：等待读取')
        self.assertEqual(dungeon_text(control(scene='town')), '当前副本：主城')
        self.assertEqual(dungeon_text(control(scene={'in_town': True})), '当前副本：主城')
        self.assertEqual(dungeon_text(control(teleporting=True)), '当前副本：场景切换中')
        self.assertIn('未在副本', dungeon_text(control(scene='fishing')))

    def test_missing_balances_are_unknown_but_omitted_zero_stack_is_zero(self):
        self.assertIn('头骨：未知', dungeon_text(control(entry_items=None)))
        self.assertIn('头骨：0', dungeon_text(control(entry_items={})))
        self.assertIn('头骨：未知', dungeon_text(control(entry_items={'SkullInferno': True})))


class ManagedDropTests(unittest.TestCase):
    def test_initial_holdings_excluded_and_transfer_does_not_duplicate(self):
        metrics = ManagedMetrics()
        old = equipment('old', is_black_mist=True)
        new = equipment('new', name='黑曜戒指', is_black_mist=True)
        metrics.observe(observation([old]))
        metrics.observe(observation([old, new]))
        self.assertEqual(len(metrics.high_value.state()), 1)
        self.assertEqual(metrics.high_value.state()[0]['locations'], {'inventory': 1})
        moved = dict(new, container='storage', slot_index=60)
        metrics.observe(observation([old, moved]))
        row = metrics.high_value.state()[0]
        self.assertEqual((row['item_uid'], row['count'], row['locations']), ('new', 1, {'storage': 1}))
        metrics.observe(observation([old, moved]))
        self.assertEqual(metrics.high_value.state()[0]['count'], 1)

    def test_ordinary_gear_potions_gems_excluded_but_all_marketable_materials_included(self):
        metrics = ManagedMetrics(); metrics.observe(observation())
        items = [equipment('ordinary'), equipment('divine', rarity=4),
                 equipment('minion', internal_name='LegendaryMinionGear1', equip_slot=12, is_black_mist=True)]
        for name in ('MovementPotion1', 'GemRuby1', 'GoldBar', 'UpgradeCommodity1', 'Rein_BeachCrabBoss'):
            items.append(loot(name, 'material', name, item_uid=name))
        metrics.observe(observation(items))
        self.assertEqual({r['item_uid'] for r in metrics.high_value.state()},
                         {'divine', 'minion', 'GoldBar', 'UpgradeCommodity1', 'Rein_BeachCrabBoss'})

    def test_crafted_high_value_rune_is_not_a_drop(self):
        metrics = ManagedMetrics(); metrics.observe(observation())
        crafted = loot('crafted', 'rune', 'LegendaryRune_Test', item_uid='crafted', rarity=3)
        metrics.synthesized('craft', [], [dict(crafted, quantity=1)])
        metrics.observe(observation([crafted]))
        self.assertEqual(metrics.high_value.state(), [])
        actual = dict(crafted, item_uid='actual')
        metrics.observe(observation([crafted, actual]))
        self.assertEqual([r['item_uid'] for r in metrics.high_value.state()], ['actual'])

    def test_stack_initial_quantity_is_subtracted_after_merging_into_storage(self):
        metrics = ManagedMetrics()
        old = loot('old', 'material', 'GoldBar', container='storage', count=10, rarity=3)
        new = loot('new', 'material', 'GoldBar', count=3, rarity=3)
        metrics.observe(observation([old]))
        metrics.observe(observation([old, new]))
        self.assertEqual(metrics.high_value.state()[0]['locations'], {'inventory': 3})
        metrics.observe(observation([dict(old, count=13)]))
        self.assertEqual(metrics.high_value.state()[0]['locations'], {'storage': 3})
        self.assertEqual(metrics.high_value.state()[0]['count'], 3)

    def test_ground_carriage_inventory_is_one_drop_and_disappearance_is_unknown(self):
        metrics = ManagedMetrics(); metrics.observe(observation())
        new = equipment('new', is_black_mist=True)
        for source in ('ground', 'carriage', 'inventory'):
            world = observation([new] if source == 'inventory' else [])
            if source != 'inventory': world[source]['items'] = [new]
            metrics.observe(world)
            self.assertEqual(metrics.high_value.state()[0]['locations'], {source: 1})
        metrics.observe(observation())
        self.assertEqual(metrics.high_value.state()[0]['locations'], {})
        self.assertEqual(metrics.high_value.state()[0]['count'], 1)

    def test_partial_read_does_not_invent_empty_or_new_stack_loot(self):
        metrics = ManagedMetrics(); metrics.observe(observation())
        new = equipment('new', is_black_mist=True)
        metrics.observe(observation([new]))
        world = observation(); world['ground']['complete'] = False
        metrics.observe(world)
        self.assertEqual(metrics.high_value.state()[0]['locations'], {'inventory': 1})

    def test_pause_keeps_run_and_new_start_clears_previous_drop_list(self):
        engine = ManagedMode(settings()); engine.start(observation())
        new = equipment('new', is_black_mist=True)
        world = observation([new]); engine.metrics.observe(world)
        engine.pause(); engine.resume(world)
        self.assertEqual(len(engine.metrics.high_value.state()), 1)
        engine.stop(); engine.start(world)
        self.assertEqual(engine.metrics.high_value.state(), [])

    def test_events_are_deduplicated_and_crafted_event_is_excluded(self):
        metrics = ManagedMetrics(); metrics.observe(observation(drop_events=[]))
        new = equipment('new', is_black_mist=True)
        event = {'event_id': 'one', 'item': new}
        world = observation([new], drop_events=[event]); metrics.observe(world); metrics.observe(world)
        self.assertEqual(metrics.high_value.state()[0]['count'], 1)

    def test_broken_optional_display_dictionary_never_stops_loot_accounting(self):
        metrics = ManagedMetrics(); metrics.observe(observation())
        with patch('deskrawl_assistant.high_value_loot.high_value_reason', side_effect=OSError('missing dictionary')):
            metrics.observe(observation([equipment('ordinary'), equipment('mist', is_black_mist=True)]))
        self.assertEqual(metrics.high_value.state()[0]['item_uid'], 'mist')


class ProjectionTests(unittest.TestCase):
    def drop(self, uid='one', **extra):
        return {'item_uid': uid, 'item_key': 'LegendaryNeck1', 'name': '相同戒指',
                'category': '黑雾装备', 'count': 1, 'locations': {'inventory': 1}, **extra}

    def test_same_template_instances_can_have_different_exact_market_states(self):
        drops = [self.drop(str(i)) for i in range(6)]
        sales = [{'item_uid': str(i), 'status': state} for i, state in enumerate(
            ('uploaded', 'pending_confirmation', 'listed', 'sold', 'uncertain'))]
        result = loot_lines(drops, sales, Mock(side_effect=AssertionError('Names already present')))
        for status in ('拍卖行已上架', 'Steam 市场待确认', 'Steam 市场已上架', 'Steam 市场已出售',
                       '上架状态待核验', '背包'):
            self.assertTrue(any(f'（{status}）' in r for r in result), result)
        self.assertTrue(all('黑雾' in r for r in result))

    def test_unknown_location_is_not_sold_and_stale_locations_not_reported_as_current(self):
        self.assertIn('位置待核验', loot_lines([self.drop(locations={})], [], Mock())[0])
        self.assertIn('位置待核验', loot_lines([self.drop()], [], Mock(), locations_fresh=False)[0])

    def test_groups_same_name_only_when_status_matches_and_keeps_counts(self):
        rows = [self.drop('a'), self.drop('b'), self.drop('c', locations={'storage': 1})]
        result = loot_lines(rows, [], Mock())
        self.assertEqual(result, ['相同戒指 · 黑雾 ×2（背包）', '相同戒指 · 黑雾（仓库）'])

    def test_partial_stack_upload_does_not_assign_a_sale_to_remaining_units(self):
        result = loot_lines([self.drop(count=3, locations={'inventory': 2})],
                            [{'item_uid': 'one', 'status': 'sold', 'count': 1}], Mock())
        self.assertEqual(result, ['相同戒指 · 黑雾（Steam 市场已出售）', '相同戒指 · 黑雾 ×2（背包）'])

    def service(self):
        service = TrayStatusMixin()
        service.guard = threading.RLock()
        service.client = SimpleNamespace(connected=True)
        service.managed = ManagedMode(settings())
        service.managed.character_id = 'hero'
        service.managed_started_at = 'current-run'
        service.managed_control = control()
        service.managed_control_read_at = 100
        service._describe_item = Mock(return_value=('金条', 'material'))
        service.managed_auction_tracking = Mock(return_value={'run_started_at': 'current-run', 'items': []})
        return service

    def test_service_uses_cache_only_and_checks_control_freshness(self):
        service = self.service()
        with patch('deskrawl_assistant.tray_status.time.monotonic', return_value=101):
            self.assertIn('炼狱头骨：100', service.tray_snapshot()['dungeon'])
        with patch('deskrawl_assistant.tray_status.time.monotonic', return_value=121):
            self.assertIn('等待读取', service.tray_snapshot()['dungeon'])
        service.client.connected = False
        with patch('deskrawl_assistant.tray_status.time.monotonic', return_value=101):
            self.assertIn('等待读取', service.tray_snapshot()['dungeon'])
        self.assertEqual(service.managed_auction_tracking.call_count, 3)

    def test_all_pending_trades_display_without_a_managed_run_and_sold_are_omitted(self):
        service = self.service()
        service.managed_started_at = None
        service.managed_auction_tracking.return_value = {'items': [
            {'name': '金条', 'status': 'waiting_town', 'container': 'storage'},
            {'name': '已售商品', 'status': 'sold'},
            {'name': '戒指', 'status': 'uncertain', 'previous_status': 'listed', 'my_price': 'CNY 20.00', 'lowest_price': 'CNY 19.89'}]}
        with patch('deskrawl_assistant.tray_status.time.monotonic', return_value=101):
            value = service.tray_snapshot()
        self.assertEqual(len(value['loot']), 2)
        self.assertIn('仓库中，待上架', value['loot'][0])
        self.assertIn('CNY 20.00', value['loot'][1])
        self.assertIn('待核验', value['loot'][1])

    def test_busy_service_lock_is_skipped_without_blocking(self):
        service = self.service()
        with patch.object(service, 'guard') as guard:
            guard.acquire.return_value = False
            self.assertIsNone(service.tray_snapshot())
            guard.acquire.assert_called_once_with(blocking=False)
            guard.release.assert_not_called()

    def test_nonautomatic_items_project_into_a_separate_collapsed_group(self):
        service = self.service()
        service.managed_auction_tracking.return_value = {'items': [
            {'name': '保留符文', 'status': 'waiting_town', 'container': 'storage', 'auto_eligible': False},
            {'name': '金条', 'status': 'listed', 'my_price': 'CNY 1.00'},
            {'name': '已售', 'status': 'sold', 'auto_eligible': False}]}
        value = service.tray_snapshot()
        self.assertEqual(len(value['loot']), 1)
        self.assertIn('金条', value['loot'][0])
        self.assertEqual(value['other_trades'], ['保留符文（仓库中，待上架）'])

    def test_listed_preview_actions_keep_exact_identity_and_annotate_cached_price(self):
        service = self.service()
        service.managed_auction_tracking.return_value = {'items': [
            {'name': '同名', 'status': 'uncertain', 'previous_status': 'listed', 'tracking_id': 'a' * 32,
             'lowest_price': 'CNY 2.00', 'lowest_price_cached': True, 'reprice_available': True},
            {'name': '同名', 'status': 'listed', 'tracking_id': 'b' * 32, 'can_reprice': True},
            {'name': '待确认', 'status': 'pending_confirmation', 'tracking_id': 'c' * 32},
            {'name': '保留', 'status': 'waiting_town', 'auto_eligible': False}]}
        value = service.tray_snapshot()
        self.assertEqual(value['loot_actions'], ['a' * 32, 'b' * 32, None])
        self.assertEqual(value['other_trade_actions'], [None])
        self.assertIn('CNY 2.00（缓存）', value['loot'][0])
        self.assertIn('待核验', value['loot'][0])

    def test_exact_inventory_sale_shortcut_includes_last_uploaded_state_but_not_unverified_native_ids(self):
        service = self.service()
        service.managed_auction_tracking.return_value = {'items': [
            {'name': '戒指', 'id': 'inventory-123', 'status': 'uncertain', 'previous_status': 'uploaded',
             'sale_preview_available': True},
            {'name': '未核实资产', 'id': 'auction-abc', 'status': 'uploaded', 'sale_available': True},
            {'name': '待确认', 'id': 'a' * 32, 'status': 'pending_confirmation', 'sale_available': True},
            {'name': '保留', 'id': 'b' * 32, 'status': 'uploaded', 'sale_available': True, 'auto_eligible': False}]}
        value = service.tray_snapshot()
        self.assertEqual(value['loot_actions'], [{'kind': 'sale', 'id': 'inventory-123'}, None, None])
        self.assertEqual(value['other_trade_actions'], [{'kind': 'sale', 'id': 'b' * 32}])
        self.assertIn('待核验', value['loot'][0])



class CacheTests(unittest.TestCase):
    def test_menu_read_never_calls_provider_and_expires_stale_cached_state(self):
        now = [100]
        provider = Mock(return_value={'dungeon': '南瓜王', 'loot': ['金条（仓库）']})
        cache = TraySnapshotCache(provider, clock=lambda: now[0])
        self.assertTrue(cache.read()['unavailable']); provider.assert_not_called()
        cache.refresh(); self.assertEqual(cache.read()['dungeon'], '南瓜王')
        for _ in range(20): cache.read()
        provider.assert_called_once()
        now[0] = 111; self.assertTrue(cache.read()['unavailable'])
        provider.side_effect = RuntimeError('unreadable')
        cache.refresh(); self.assertTrue(cache.read()['unavailable'])

    def test_close_prevents_late_worker_result_from_being_published(self):
        cache = TraySnapshotCache(lambda: {'loot': []})
        cache.close(); cache.refresh()
        self.assertIsNone(cache.value)

    def test_worker_can_be_slow_without_blocking_menu_reads_or_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set(); release.wait(1)
            return {'loot': []}
        cache = TraySnapshotCache(slow)
        try:
            cache.start(); self.assertTrue(entered.wait(1))
            self.assertTrue(cache.read()['unavailable'])
            cache.close()
        finally:
            release.set(); cache.thread.join(1)
        self.assertFalse(cache.thread.is_alive())
        self.assertIsNone(cache.value)


if __name__ == '__main__':
    unittest.main()
