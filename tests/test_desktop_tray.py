"""Real WinForms menu components, never showing a window or a second tray icon."""
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from deskrawl_assistant.desktop_tray import DesktopTray


@unittest.skipUnless(os.name == 'nt', 'Windows tray menu components')
class TrayMenuTests(unittest.TestCase):
    def setUp(self):
        import clr
        clr.AddReference('System.Windows.Forms')
        from System.Windows.Forms import ContextMenuStrip
        self.provider = Mock(side_effect=AssertionError('UI must not call a provider'))
        self.tray = DesktopTray(SimpleNamespace(native=None), 'unused.ico', Mock(), Mock(), snapshot=self.provider)
        self.tray.menu = ContextMenuStrip()
        self.tray.dungeon_item = self.tray._item('当前副本', enabled=False)
        self.tray.loot_item = self.tray._item('待交易物品')
        self.tray.menu.Items.Add(self.tray.dungeon_item)
        self.tray.menu.Items.Add(self.tray.loot_item)
        self.addCleanup(self.tray.close)

    def update(self, **values):
        state = {'dungeon': '当前副本：南瓜王（今日剩余 20/100）', 'has_run': True, 'loot': [], **values}
        self.tray.status_cache.value = (time.monotonic(), state)
        self.tray._refresh_menu()

    def test_real_menu_is_read_only_and_reopening_disposes_old_children(self):
        self.update(loot=['金条（仓库）', '开光石（Steam 市场待确认）'])
        self.assertIn('今日剩余 20/100', self.tray.dungeon_item.Text)
        items = self.tray.loot_item.DropDownItems
        self.assertEqual(items.Count, 2)
        old = items[0]
        disposed = []
        old.Disposed += lambda *_: disposed.append(True)
        self.assertFalse(old.Enabled)
        self.assertEqual(items[1].Text, '开光石（Steam 市场待确认）')
        self.update(loot=['金条（Steam 市场已出售）'])
        self.assertEqual(disposed, [True])
        self.assertEqual(items.Count, 1)
        self.assertEqual(items[0].Text, '金条（Steam 市场已出售）')
        self.provider.assert_not_called()

    def test_large_lists_are_paged_without_silently_dropping_records(self):
        rows = [f'掉落 {index}（背包）' for index in range(61)]
        self.update(loot=rows)
        pages = self.tray.loot_item.DropDownItems
        self.assertEqual(pages.Count, 3)
        flattened = [r.Text for page in pages for r in page.DropDownItems]
        self.assertEqual(flattened, rows)
        old_page = pages[0]; old_child = old_page.DropDownItems[0]
        disposed = []
        old_page.Disposed += lambda *_: disposed.append(True)
        self.update()
        self.assertEqual(disposed, [True]); self.assertTrue(old_child.IsDisposed)
        self.assertIn('当前暂无', pages[0].Text)

    def test_names_escape_mnemonics_and_bound_long_menu_labels(self):
        text = '装备 & 测试 ' + '超长名字' * 40 + '（背包）'
        self.update(loot=[text])
        item = self.tray.loot_item.DropDownItems[0]
        self.assertIn('&&', item.Text)
        self.assertTrue(item.Text.endswith('…'))
        self.assertLessEqual(len(item.Text), 91)
        self.assertEqual(item.ToolTipText, text)

    def test_nonautomatic_items_are_nested_at_the_bottom_and_remain_paged(self):
        other = [f'保留符文 {i}（仓库）' for i in range(28)]
        self.update(loot=['金条（Steam 市场已上架）'], other_trades=other)
        items = self.tray.loot_item.DropDownItems
        self.assertEqual(items.Count, 2)
        self.assertIn('金条', items[0].Text)
        self.assertIn('未满足自动上架条件（28 条）', items[1].Text)
        self.assertTrue(items[1].Enabled)
        self.assertEqual([r.Text for page in items[1].DropDownItems for r in page.DropDownItems], other)
        self.assertIn('29 条', self.tray.loot_item.Text)
        self.update(other_trades=['只保留此物品'])
        self.assertEqual(items.Count, 1)
        self.assertIn('未满足自动上架条件', items[0].Text)

    def test_not_started_and_unavailable_have_distinct_placeholders(self):
        self.update(has_run=False)
        self.assertEqual(self.tray.loot_item.DropDownItems[0].Text, '当前暂无待交易物品')
        self.update(unavailable=True)
        self.assertIn('信息正在更新', self.tray.loot_item.DropDownItems[0].Text)

    def test_listed_rows_open_exact_shared_preview_without_blocking_or_mutating_on_menu_open(self):
        callback = Mock()
        self.tray.reprice_callback = callback
        ids = ['a' * 32, 'b' * 32]
        self.update(loot=['在售一', '在售二', '待确认'], loot_actions=[*ids, None])
        items = self.tray.loot_item.DropDownItems
        callback.assert_not_called()
        self.assertIn('点击改价', items[0].Text)
        self.assertTrue(items[0].Enabled)
        self.assertFalse(items[2].Enabled)
        items[0].PerformClick(); items[1].PerformClick()
        self.assertEqual([c.args for c in callback.call_args_list], [(ids[0],), (ids[1],)])
        self.provider.assert_not_called()

    def test_reprice_identity_stays_aligned_across_pages_and_collapsed_rows(self):
        callback = Mock(); self.tray.reprice_callback = callback
        rows = ['同名物品'] * 28
        ids = [f'{i:032x}' for i in range(28)]
        self.update(loot=rows, loot_actions=ids, other_trades=['保留商品'], other_trade_actions=['f' * 32])
        pages = self.tray.loot_item.DropDownItems
        pages[1].DropDownItems[1].PerformClick()
        callback.assert_called_once_with(ids[26])
        pages[2].DropDownItems[0].PerformClick()
        self.assertEqual(callback.call_args.args, ('f' * 32,))
        self.update(loot=['无效编号'], loot_actions=['");doBadThings()'])
        self.assertFalse(self.tray.loot_item.DropDownItems[0].Enabled)

    def test_sale_shortcut_opens_only_the_exact_shared_editor_in_both_groups(self):
        sale, reprice = Mock(), Mock()
        self.tray.sale_callback, self.tray.reprice_callback = sale, reprice
        action = {'kind': 'sale', 'id': 'inventory-123'}
        self.update(loot=['库存 · 待核验', '在售'], loot_actions=[action, 'a' * 32],
                    other_trades=['保留商品'], other_trade_actions=[{'kind': 'sale', 'id': 'b' * 32}])
        items = self.tray.loot_item.DropDownItems
        sale.assert_not_called(); reprice.assert_not_called()
        self.assertIn('点击定价上架', items[0].Text)
        items[0].PerformClick(); items[2].DropDownItems[0].PerformClick()
        self.assertEqual([c.args for c in sale.call_args_list], [('inventory-123',), ('b' * 32,)])
        items[1].PerformClick(); reprice.assert_called_once_with('a' * 32)
        for invalid in ({'kind': 'sale', 'id': 'auction-1'}, {'kind': 'sale', 'id': '\");bad()'},
                        {'kind': 'sale', 'id': 'inventory-0'}, {'kind': 'other', 'id': 'a' * 32}):
            self.update(loot=['无效'], loot_actions=[invalid])
            self.assertFalse(self.tray.loot_item.DropDownItems[0].Enabled)
        self.provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
