"""Chinese equipment viewer with guarded background locking."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .runtime_client import RuntimeClient
from .runtime_adapter import RuntimeAdapter
from .models import Rule
from .rules import LockPlanner
from .loot_monitor import LootState
from .action_log import record

BASE = Path(__file__).resolve().parents[1]


def snapshot_rows(snapshot):
    containers = snapshot.get("containers", {})
    for key, label in (("inventory", "背包"), ("storage", "仓库")):
        container = containers.get(key, {})
        for row in container.get("slots", []):
            if isinstance(row, dict):
                yield key, label, row


class RuntimePanel:
    def __init__(self, parent, catalog, status, get_rules=lambda: []):
        self.frame = parent
        self.catalog = catalog
        self.status = status
        self.get_rules = get_rules
        self.adapter = RuntimeAdapter(catalog, BASE / "data/affix-groups-static.json")
        self.planner = LockPlanner(catalog)
        self.client = RuntimeClient()
        self.events = queue.Queue()
        self.busy = False
        self.closed = False
        self.snapshot = None
        self.rows = {}
        self.watching = False
        self.watch_timer = None
        self.seen = None
        self.loot_state = None
        self.cancelled = threading.Event()
        self.auto_lock = tk.BooleanVar(value=False)
        self.notice = tk.StringVar(value="点击连接游戏，读取背包和仓库。支持后台锁定，无需打开背包或校准位置；持续自动锁定默认关闭。")
        self.build()
        self.buttons()
        self.timer = parent.after(100, self.poll)

    def build(self):
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", pady=(0, 8))
        self.connect_button = ttk.Button(bar, text="连接游戏", command=self.connect)
        self.connect_button.pack(side="left")
        self.read_button = ttk.Button(bar, text="一键读取背包 / 仓库", command=self.read, state="disabled")
        self.read_button.pack(side="left", padx=8)
        self.disconnect_button = ttk.Button(bar, text="断开", command=self.disconnect, state="disabled")
        self.disconnect_button.pack(side="left")
        self.watch_button = ttk.Button(bar, text="持续读取新装备", command=self.toggle_watch, state="disabled")
        self.watch_button.pack(side="left", padx=8)
        self.export_button = ttk.Button(bar, text="导出清单…", command=self.export, state="disabled")
        self.export_button.pack(side="right")
        actions = ttk.Frame(self.frame)
        actions.pack(fill="x", pady=(0, 8))
        self.lock_button = ttk.Button(actions, text="后台锁定背包 / 仓库匹配装备", command=self.lock_matches)
        self.lock_button.pack(side="left", padx=(0,8))
        self.test_lock_button = ttk.Button(actions, text="锁定所选装备", command=self.test_lock)
        self.test_lock_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="停止操作", command=self.cancel_operation, state="disabled")
        self.cancel_button.pack(side="right")
        ttk.Checkbutton(self.frame, text="持续监控时后台锁定新装备（按启用规则）", variable=self.auto_lock, command=self.change_auto_lock).pack(anchor="w", pady=(0,6))
        ttk.Label(self.frame, textvariable=self.notice, wraplength=980).pack(anchor="w", pady=(0, 8))
        pane = ttk.Panedwindow(self.frame, orient="vertical")
        pane.pack(fill="both", expand=True)
        table_frame = ttk.Frame(pane)
        detail_frame = ttk.Frame(pane)
        pane.add(table_frame, weight=3)
        pane.add(detail_frame, weight=2)
        self.table = ttk.Treeview(table_frame, columns=("container", "slot", "name", "level", "lock", "affixes", "rule"), show="headings")
        for col, title, width in (("container", "位置", 65), ("slot", "格子", 60), ("name", "装备名称", 300),
                ("level", "等级", 60), ("lock", "锁定", 70), ("affixes", "词条类型", 300), ("rule", "筛选结果", 160)):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, minwidth=50, stretch=col in {"name", "affixes"})
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self.select)
        ttk.Label(detail_frame, text="所选装备的读取明细").pack(anchor="w", pady=(8, 4))
        self.detail = tk.Text(detail_frame, height=9, wrap="word", font=("Microsoft YaHei UI", 10))
        self.detail.pack(fill="both", expand=True)
        self.detail.configure(state="disabled")
        data = json.loads((BASE / "data" / "affix-groups-static.json").read_text(encoding="utf-8"))
        self.stat_names = {entry["value"]: entry["name"] for entry in data["enumConstants"]["StatType"]}

    def work(self, title, action):
        if self.busy or self.closed:
            return
        self.busy = True
        self.notice.set(title)
        self.buttons()
        record('operation_started', title=title)
        def run():
            try:
                result = action()
                record('operation_finished', title=title)
                self.events.put(("success", title, result))
            except Exception as exc:
                record('operation_failed', title=title, error=str(exc), traceback=traceback.format_exc())
                self.events.put(("error", title, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def connect(self):
        self.work("正在连接游戏…", self.client.connect)

    def read(self):
        watching = self.watching
        auto_mode = watching and self.auto_lock.get()
        previous = self.loot_state
        rules = [Rule.from_dict(raw, self.catalog) for raw in self.get_rules()]
        def run():
            snapshot = self.client.snapshot()
            state = LootState.observe(snapshot, previous) if watching else None
            def finish(value):
                return {'watch_result': value, 'loot_state': state} if watching else value
            if not auto_mode or not snapshot.get('complete'):
                return finish(snapshot)
            if not any(r.enabled for r in rules):
                snapshot['auto_status'] = '请先保存至少一条启用的筛选规则'
                return finish(snapshot)
            if previous is None or state is None or previous.session != state.session:
                snapshot['auto_status'] = '已记录现有装备，等待新装备；现有装备可用一键锁定处理'
                return finish(snapshot)
            raw_items = {(key,row['slot_index']): row for key,_,row in snapshot_rows(snapshot)}
            candidates = [raw_items[(item.container,item.index)] for item in self.adapter.adapt_snapshot(snapshot).items
                if raw_items[(item.container,item.index)].get('instance_id') in state.pending
                and self.planner.plan(item.observation,rules).action=='press_l']
            if not candidates:
                return finish(snapshot)
            result = self.apply_locks(snapshot,candidates,rules)
            state = LootState.observe(result, state)
            return finish(result)
        self.work("正在直接读取装备数据…", run)

    def disconnect(self):
        self.stop_watch()
        self.work("正在断开读取连接…", self.client.close)

    def test_lock(self):
        selection = self.table.selection()
        if not selection:
            self.notice.set("请先在清单中选择一件未锁定装备。")
            return
        row = self.rows[selection[0]]
        if not row.get("is_equipment") or row.get("locked") is not False:
            self.notice.set("请选择一件未锁定装备；已锁定装备会跳过。")
            return
        self.begin_locks([dict(row)], [], manual=True)

    def lock_matches(self):
        rules = [Rule.from_dict(raw, self.catalog) for raw in self.get_rules()]
        if not any(rule.enabled for rule in rules):
            self.notice.set("请先保存至少一条启用的装备筛选规则。")
            return
        self.begin_locks(None, rules)

    def begin_locks(self, selected, rules, manual=False):
        self.stop_watch()
        self.cancelled.clear()
        def run():
            snapshot = self.client.snapshot()
            if not snapshot.get("complete"):
                raise RuntimeError("当前物品快照不完整，停止锁定")
            raw_items = {(key,row['slot_index']): row for key,_,row in snapshot_rows(snapshot)}
            if selected is None:
                candidates = []
                for item in self.adapter.adapt_snapshot(snapshot).items:
                    if self.planner.plan(item.observation, rules).action == 'press_l':
                        candidates.append(raw_items[(item.container, item.index)])
            else:
                candidates = []
                for target in selected:
                    matches = [row for row in raw_items.values() if row.get('item_uid')==target.get('item_uid')
                        and row.get('instance_id')==target.get('instance_id') and row.get('name_key')==target.get('name_key')]
                    if len(matches)!=1:
                        raise RuntimeError("所选装备已移出背包和仓库或身份发生变化，请重新读取")
                    now = matches[0]
                    if now.get('locked') is False:
                        candidates.append(now)
            return self.apply_locks(snapshot,candidates,rules,manual)
        self.work("正在后台核对并锁定装备…", run)

    def apply_locks(self, snapshot, candidates, rules, manual=False):
        locked = 0
        skipped = 0
        for row in candidates:
            if self.cancelled.is_set():
                break
            self.events.put(('progress','',f"正在后台核对并锁定：{self.item_label(row)}。"))
            def validate(actual):
                if self.cancelled.is_set():
                    return False
                if manual:
                    return True
                observation = self.adapter.adapt_item(actual,snapshot_token=snapshot['snapshot_id'],session_id=snapshot['bridge_session']).observation
                return self.planner.plan(observation,rules).action=='press_l'
            outcome = self.client.lock_equipment(row,validate)
            locked += outcome['status']=='locked'
            skipped += outcome['status']=='already_locked'
        result = self.client.snapshot()
        result['lock_result'] = {'locked':locked,'skipped':skipped,'candidate_count':len(candidates),'cancelled':self.cancelled.is_set()}
        return result

    def change_auto_lock(self):
        if self.auto_lock.get():
            self.cancelled.clear()
        else:
            self.cancelled.set()

    def cancel_operation(self):
        self.cancelled.set()
        self.stop_watch()
        self.notice.set("正在停止操作，已确认的锁定会保留。")

    def stop_watch(self):
        self.watching = False
        self.watch_button.configure(text="持续读取新装备")
        if self.watch_timer is not None:
            self.frame.after_cancel(self.watch_timer)
            self.watch_timer = None

    def toggle_watch(self):
        if self.watching:
            self.cancelled.set()
            self.stop_watch()
            self.notice.set("已停止持续读取。")
            return
        self.watching = True
        self.cancelled.clear()
        self.seen = None
        self.loot_state = None
        self.watch_button.configure(text="停止持续读取")
        self.read()

    def watch_tick(self):
        self.watch_timer = None
        if self.watching and self.client.connected and not self.busy:
            self.read()

    def buttons(self):
        state = "disabled" if self.busy else "normal"
        self.connect_button.configure(state="disabled" if self.busy or self.client.connected else "normal")
        self.read_button.configure(state=state if self.client.connected else "disabled")
        self.disconnect_button.configure(state=state if self.client.connected else "disabled")
        self.watch_button.configure(state="normal" if self.client.connected and (self.watching or not self.busy) else "disabled")
        self.export_button.configure(state=state if self.snapshot is not None else "disabled")
        self.lock_button.configure(state=state if self.client.connected else "disabled")
        self.test_lock_button.configure(state=state if self.client.connected and self.snapshot is not None else "disabled")
        self.cancel_button.configure(state="normal" if self.busy else "disabled")

    def poll(self):
        if self.closed:
            return
        while not self.events.empty():
            kind, title, result = self.events.get_nowait()
            if kind == "progress":
                self.notice.set(result)
                continue
            self.busy = False
            if kind == "error":
                self.notice.set(f"操作未完成：{result}")
                self.stop_watch()
                if not self.cancelled.is_set():
                    messagebox.showerror("操作未完成", str(result), parent=self.frame)
            elif isinstance(result, dict) and 'watch_result' in result:
                self.loot_state = result['loot_state']
                self.show_snapshot(result['watch_result'])
            elif isinstance(result, dict) and "containers" in result:
                self.show_snapshot(result)
            elif self.client.connected:
                self.notice.set("已连接游戏。点击一键读取，查看背包与仓库中的物品。")
            else:
                self.notice.set("已断开读取连接。")
            self.buttons()
            if self.watching and self.watch_timer is None:
                self.watch_timer = self.frame.after(2000, self.watch_tick)
        self.timer = self.frame.after(100, self.poll)

    def item_label(self, row):
        key = row.get("name_key")
        name = row.get("internal_name") or row.get("name") or ""
        if key not in self.catalog.equipment:
            key = self.catalog.resolve_equipment(name)
        if key:
            return self.catalog.equipment[key].label
        return name or "名称尚未解析"

    def affix_label(self, modifier):
        name = modifier.get("stat_name") or self.stat_names.get(modifier.get("stat"), "未知词条")
        key = "Stats." + name
        label = self.catalog.stats[key].label if key in self.catalog.stats else name
        return label

    def show_snapshot(self, snapshot):
        self.snapshot = snapshot
        selection = self.table.selection()
        selected_row = self.rows.get(selection[0],{}) if selection else {}
        selected_id = (selected_row.get('item_uid'),selected_row.get('instance_id'))
        scroll_position = self.table.yview()[0]
        self.table.delete(*self.table.get_children())
        self.rows = {}
        self.descriptions = {}
        adapted = self.adapter.adapt_snapshot(snapshot)
        decisions = {}
        observations = {}
        rules = [Rule.from_dict(raw, self.catalog) for raw in self.get_rules()]
        for item in adapted.items:
            decisions[(item.container, item.index)] = self.planner.plan(item.observation, rules)
            observations[(item.container,item.index)] = item.observation
        count = 0
        equipment_ids = set()
        for key, container, row in snapshot_rows(snapshot):
            # Empty slots are diagnostics, not equipment records.
            if row.get("empty") is True:
                continue
            iid = str(count)
            self.rows[iid] = row
            locked = row.get("locked")
            lock_text = "已锁定" if locked is True else "未锁定" if locked is False else "未确认"
            modifiers = row.get("modifiers") or []
            affixes = "；".join(self.affix_label(m) for m in modifiers if isinstance(m, dict))
            decision = decisions.get((key, row.get("slot_index")))
            rule_text = "非装备" if row.get("is_equipment") is False else "未配置规则" if not rules else "待核验"
            if decision is not None and rules:
                rule_text = {"press_l": "符合规则，等待锁定", "skip": "已锁定" if locked else "未命中", "review": "待核验"}[decision.action]
                row["rule_preview"] = {"action": decision.action, "reasons": list(decision.reasons), "executes_lock": False}
            if row.get("is_equipment") and row.get("instance_id"):
                equipment_ids.add(row["instance_id"])
            slot = row.get('slot_index')
            display_slot = slot+1 if isinstance(slot,int) else ''
            self.table.insert("", "end", iid=iid, values=(container, display_slot, self.item_label(row),
                row.get("item_level", ""), lock_text, affixes, rule_text))
            lines = [self.item_label(row),f"位置：{container}第 {display_slot} 格",f"锁定状态：{lock_text}"]
            if row.get('is_equipment'):
                lines.extend([f"物品等级：{row.get('item_level','未确认')}",f"强化等级：{row.get('upgrade_level','未确认')}"])
                observation = observations.get((key,slot))
                if observation:
                    for group,label in (('primary','主词条'),('secondary','副词条'),('unknown','待核验词条')):
                        labels = [self.catalog.stats[stat].label if stat in self.catalog.stats else '未知词条'
                            for stat,values in observation.affixes.items() if any(v.group==group for v in values)]
                        if labels:
                            lines.append(label+'：'+'；'.join(labels))
                if decision:
                    lines.append('筛选结果：'+rule_text)
                    lines.extend(decision.reasons)
            else:
                lines.append(f"数量：{row.get('count',1)}")
            lines.extend(row.get('issues',[]))
            self.descriptions[iid]='\n'.join(lines)
            if all(selected_id) and (row.get('item_uid'),row.get('instance_id'))==selected_id:
                self.table.selection_set(iid)
            count += 1
        issues = snapshot.get("issues", [])
        extra = "；存在未完成的读取项，请查看明细" if issues else ""
        delta = ""
        if self.watching and snapshot.get("complete"):
            if self.seen is not None:
                added = equipment_ids - self.seen
                delta = f"；发现 {len(added)} 件新装备" if added else "；持续读取中"
            self.seen = equipment_ids
        self.notice.set(f"本次读到 {count} 个有物品的格子、{len(equipment_ids)} 件装备{extra}{delta}。可按规则后台锁定。")
        if snapshot.get('lock_result'):
            done = snapshot['lock_result']
            ending = '；操作已停止' if done.get('cancelled') else ''
            self.notice.set(f"本次后台锁定 {done['locked']} 件装备，跳过 {done.get('skipped',0)} 件已锁装备；已回读并通知游戏保存{ending}。")
        elif snapshot.get('auto_status'):
            self.notice.set(f"持续读取中；{snapshot['auto_status']}。")
        self.status.set(f"直接读取：{count} 件物品")
        self.table.yview_moveto(scroll_position)
        self.set_detail('请选择一件装备，查看主、副词条和筛选结果。'+ ('\n\n读取提示：\n'+'\n'.join(issues) if issues else ''))
        self.select()

    def select(self, event=None):
        selection = self.table.selection()
        if selection:
            self.set_detail(self.descriptions.get(selection[0],''))

    def set_detail(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def export(self):
        if self.snapshot is None:
            return
        path = filedialog.asksaveasfilename(title="导出装备清单", defaultextension=".json", initialfile="Deskrawl装备清单.json", filetypes=[("装备清单", "*.json")])
        if path:
            try:
                Path(path).write_text(json.dumps(self.snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc))

    def close(self):
        self.closed = True
        self.cancelled.set()
        self.stop_watch()
        self.frame.after_cancel(self.timer)
        threading.Thread(target=self.client.close, daemon=True).start()
