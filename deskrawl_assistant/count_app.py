"""Primary-first equipment rules with background read and lock actions."""
from __future__ import annotations

import json
import tkinter as tk
import uuid
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .catalog import load_catalog
from .models import Rule
from .rules import LockPlanner, observations_from_dict, rules_from_dict
from .runtime_panel import RuntimePanel

BASE = Path(__file__).resolve().parent.parent
CONFIG = BASE / "config" / "lock-rules.json"


class AffixGroupEditor:
    def __init__(self, parent, title, entries, optional=False):
        self.entries = entries
        self.optional = optional
        self.enabled = tk.BooleanVar(value=not optional)
        self.count = tk.StringVar(value="1")
        self.query = tk.StringVar()
        self.selected = {key: tk.BooleanVar(value=False) for key in entries}
        self.total = tk.StringVar(value="已选 0 类")
        self.frame = ttk.LabelFrame(parent, text=title, padding=10)
        if optional:
            ttk.Checkbutton(self.frame, text="启用副词条筛选", variable=self.enabled).pack(anchor="w")
        else:
            ttk.Label(self.frame, text="必需条件：达标后才检查副词条").pack(anchor="w")
        quantity = ttk.Frame(self.frame)
        quantity.pack(fill="x", pady=(8, 4))
        ttk.Label(quantity, text="选中的类型，命中至少 ").pack(side="left")
        ttk.Spinbox(quantity, from_=1, to=len(entries), textvariable=self.count, width=4).pack(side="left")
        ttk.Label(quantity, text=" 类").pack(side="left")
        ttk.Label(self.frame, textvariable=self.total).pack(anchor="w", pady=(2, 6))
        ttk.Entry(self.frame, textvariable=self.query).pack(fill="x", pady=(0, 6))
        ttk.Label(self.frame, text="搜索词条；隐藏的勾选仍保留。", foreground="#666666").pack(anchor="w", pady=(0, 6))
        holder = ttk.Frame(self.frame)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, highlightthickness=0, background="#eeeeee", height=250)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.rows = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.query.trace_add("write", lambda *args: self.render())
        self.enabled.trace_add("write", lambda *args: self.render())
        for value in self.selected.values():
            value.trace_add("write", lambda *args: self.update_total())
        self.render()

    def render(self):
        for child in self.rows.winfo_children():
            child.destroy()
        query = self.query.get().strip().casefold()
        for key, entry in sorted(self.entries.items(), key=lambda pair: pair[1].zh or pair[1].en):
            if query and query not in (entry.zh + " " + entry.en).casefold():
                continue
            button = ttk.Checkbutton(self.rows, text=f"{entry.zh or entry.en}  ·  {entry.en}", variable=self.selected[key])
            button.pack(anchor="w", fill="x", pady=3)
            if not self.enabled.get():
                button.state(["disabled"])

    def update_total(self):
        self.total.set(f"已选 {sum(v.get() for v in self.selected.values())} 类；同类词条只计一次")

    def reset(self):
        self.enabled.set(not self.optional)
        self.count.set("1")
        self.query.set("")
        for value in self.selected.values():
            value.set(False)

    def load(self, group):
        self.reset()
        self.enabled.set(bool(group) if self.optional else True)
        if group:
            if group["operator"] != ">=":
                raise ValueError("此界面只配置“至少 n 类”；严格大于规则请保留原文件。")
            self.count.set(str(group["count"]))
            for key in group["selected_stats"]:
                self.selected[key].set(True)

    def to_dict(self):
        if self.optional and not self.enabled.get():
            return None
        try:
            count = int(self.count.get())
        except ValueError as exc:
            raise ValueError("词条命中数量必须是整数。") from exc
        return {"selected_stats": [key for key, value in self.selected.items() if value.get()], "operator": ">=", "count": count}


class RuleEditor:
    def __init__(self, root):
        self.root = root
        self.catalog = load_catalog(BASE / "data" / "game-catalog.json")
        pools = json.loads((BASE / "data" / "affix-groups-static.json").read_text(encoding="utf-8"))
        if pools.get("schema_version") != 1 or not isinstance(pools.get("slotPools"), list):
            raise ValueError("主 / 副词条池文件无效。")
        self.group_choices = {}
        for group in ("primary", "secondary"):
            keys = set().union(*(set(pool[group]) for pool in pools["slotPools"]))
            if not keys or any(key not in self.catalog.stats for key in keys):
                raise ValueError("主 / 副词条池与词典不一致。")
            self.group_choices[group] = {key: self.catalog.stats[key] for key in sorted(keys)}
        self.rules = []
        self.selected_id = None
        self.config_error = False
        self.equipment_labels = {self.label(entry): key for key, entry in self.catalog.equipment.items()}
        self.equipment_names = sorted(self.equipment_labels)
        root.title("Deskrawl · 主副词条自动锁定规则")
        root.geometry("1160x800")
        root.minsize(1020, 680)
        self.status = tk.StringVar(value="装备读取尚未连接")
        self.build()
        self.read_config()
        self.new_rule()
        root.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        self.runtime_panel.close()
        self.root.destroy()

    @staticmethod
    def label(entry):
        return f"{entry.zh or entry.en}  ·  {entry.en or entry.key}"

    def build(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="主 / 副词条筛选", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="主词条至少命中 n 类；可选副词条条件，主词条达标后再检查。", padding=(0, 4)).pack(anchor="w")
        ttk.Label(outer, text="支持直接读取与后台锁定背包、仓库装备，无需打开背包或校准位置。", foreground="#855400").pack(anchor="w", pady=3)
        ttk.Label(outer, text="要保留的装备品质，请先在马车筛选中设为“收集”。").pack(anchor="w", pady=(0, 10))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        editor = ttk.Frame(tabs, padding=12)
        preview = ttk.Frame(tabs, padding=12)
        live = ttk.Frame(tabs, padding=12)
        tabs.add(editor, text="规则配置")
        tabs.add(preview, text="测试规则")
        tabs.add(live, text="背包 / 仓库清单")
        self.build_editor(editor)
        self.build_preview(preview)
        self.runtime_panel = RuntimePanel(live, self.catalog, self.status, lambda: self.rules)
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, textvariable=self.status).pack(side="left")
        ttk.Label(footer, text="后台自动锁定默认关闭").pack(side="right")

    def build_editor(self, parent):
        pane = ttk.Panedwindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane, padding=(0, 0, 14, 0))
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=4)
        self.tree = ttk.Treeview(left, columns=("enabled",), show="tree headings", height=14)
        self.tree.heading("#0", text="已保存的规则")
        self.tree.heading("enabled", text="状态")
        self.tree.column("#0", width=180, minwidth=100)
        self.tree.column("enabled", width=55, stretch=False)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select_rule)
        for text, handler in (("新增规则", self.new_rule), ("删除所选规则", self.delete_rule), ("导入规则…", self.import_rules), ("导出规则…", self.export_rules)):
            ttk.Button(left, text=text, command=handler).pack(fill="x", pady=(6, 0))
        form = ttk.Frame(right)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.name = tk.StringVar()
        self.enabled = tk.BooleanVar(value=True)
        self.equipment = tk.StringVar()
        ttk.Label(form, text="规则名称").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=5)
        ttk.Entry(form, textvariable=self.name).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Checkbutton(form, text="启用规则", variable=self.enabled).grid(row=0, column=2, padx=8)
        ttk.Label(form, text="指定装备").grid(row=1, column=0, sticky="w", pady=5)
        combo = ttk.Combobox(form, textvariable=self.equipment, values=self.equipment_names)
        combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        combo.bind("<KeyRelease>", lambda e: combo.configure(values=[label for label in self.equipment_names if combo.get().casefold() in label.casefold()]))
        groups = ttk.Frame(right)
        groups.pack(fill="both", expand=True, pady=(10, 0))
        groups.columnconfigure(0, weight=1)
        groups.columnconfigure(1, weight=1)
        groups.rowconfigure(0, weight=1)
        self.primary = AffixGroupEditor(groups, "主词条", self.group_choices["primary"])
        self.secondary = AffixGroupEditor(groups, "副词条（可关闭）", self.group_choices["secondary"], optional=True)
        self.primary.frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.secondary.frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        footer = ttk.Frame(right)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="仅统计随机主 / 副词条，宝石和特殊效果不计入。", wraplength=540).pack(side="left")
        ttk.Button(footer, text="保存规则", command=self.save_rule).pack(side="right")

    def new_rule(self):
        self.selected_id = None
        self.tree.selection_remove(self.tree.selection())
        self.name.set("")
        self.enabled.set(True)
        self.equipment.set("")
        self.primary.reset()
        self.secondary.reset()

    def collect_rule(self):
        if self.equipment.get() not in self.equipment_labels:
            raise ValueError("请选择列表中的确切装备名称。")
        groups = {"primary": self.primary.to_dict()}
        secondary = self.secondary.to_dict()
        if secondary is not None:
            groups["secondary"] = secondary
        raw = {"id": self.selected_id or uuid.uuid4().hex, "name": self.name.get().strip() or self.equipment.get().split("  ·  ")[0],
               "enabled": self.enabled.get(), "equipment_keys": [self.equipment_labels[self.equipment.get()]], "groups": groups, "group_mode": "all"}
        return Rule.from_dict(raw, self.catalog).to_dict()

    def validated_payload(self, payload):
        rules = rules_from_dict(payload, self.catalog)
        if any(not rule.is_count_rule for rule in rules):
            raise ValueError("此界面使用主 / 副词条数量规则；旧版数值规则请保留原文件。")
        if any(group.operator != ">=" for rule in rules for group in rule.groups.values()):
            raise ValueError("此界面使用“至少 n 类”；严格大于规则请保留原文件。")
        for rule in rules:
            for name, group in rule.groups.items():
                if any(key not in self.group_choices[name] for key in group.selected_stats):
                    raise ValueError("规则选择了不在相应主 / 副词条池中的类型，请核对原文件。")
        return [rule.to_dict() for rule in rules]

    def persist(self, rules):
        if self.config_error:
            raise ValueError("原规则文件读取失败。请先备份并修正文件，程序不会覆盖它。")
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 2, "rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CONFIG)

    def read_config(self):
        if CONFIG.exists():
            try:
                self.rules = self.validated_payload(json.loads(CONFIG.read_text(encoding="utf-8-sig"), parse_float=Decimal))
                self.refresh()
            except (ValueError, TypeError, OSError) as exc:
                self.config_error = True
                messagebox.showerror("规则读取失败", str(exc), parent=self.root)

    def save_rule(self):
        try:
            rule = self.collect_rule()
            updated = [rule if item["id"] == rule["id"] else item for item in self.rules]
            if not any(item["id"] == rule["id"] for item in self.rules):
                updated.append(rule)
            self.persist(updated)
            self.rules = updated
            self.selected_id = rule["id"]
            self.refresh()
            self.status.set("规则已保存；可在“背包 / 仓库清单”中读取并筛选装备")
        except (ValueError, TypeError, OSError) as exc:
            messagebox.showerror("无法保存规则", str(exc), parent=self.root)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for rule in self.rules:
            self.tree.insert("", "end", iid=rule["id"], text=rule["name"], values=("启用" if rule["enabled"] else "停用",))

    def select_rule(self, event=None):
        selected = self.tree.selection()
        if not selected:
            return
        rule = next(item for item in self.rules if item["id"] == selected[0])
        if len(rule["equipment_keys"]) != 1:
            messagebox.showinfo("多装备规则", "此规则含多个装备；请在原文件中编辑，界面不会覆盖它。", parent=self.root)
            self.tree.selection_remove(selected)
            return
        self.selected_id = rule["id"]
        self.name.set(rule["name"])
        self.enabled.set(rule["enabled"])
        self.equipment.set(self.label(self.catalog.equipment[rule["equipment_keys"][0]]))
        self.primary.load(rule["groups"]["primary"])
        self.secondary.load(rule["groups"].get("secondary"))

    def delete_rule(self):
        selected = self.tree.selection()
        if not selected:
            return
        try:
            updated = [rule for rule in self.rules if rule["id"] != selected[0]]
            self.persist(updated)
            self.rules = updated
            self.refresh()
            self.new_rule()
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法删除规则", str(exc), parent=self.root)

    def import_rules(self):
        path = filedialog.askopenfilename(title="导入规则", filetypes=(("规则文件", "*.json"),), parent=self.root)
        if path:
            try:
                rules = self.validated_payload(json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_float=Decimal))
                imported = [{**rule, "id": uuid.uuid4().hex, "enabled": False} for rule in rules]
                updated = self.rules + imported
                self.persist(updated)
                self.rules = updated
                self.refresh()
                self.status.set(f"已导入 {len(imported)} 条规则，当前停用")
            except (ValueError, TypeError, OSError) as exc:
                messagebox.showerror("无法导入规则", str(exc), parent=self.root)

    def export_rules(self):
        path = filedialog.asksaveasfilename(title="导出规则", defaultextension=".json", filetypes=(("规则文件", "*.json"),), parent=self.root)
        if path:
            try:
                Path(path).write_text(json.dumps({"version": 2, "rules": self.rules}, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("无法导出规则", str(exc), parent=self.root)

    def build_preview(self, parent):
        ttk.Label(parent, text="查看每组命中了多少种选中词条；主词条不达标时直接跳过。测试不会操作游戏。", wraplength=1000).pack(anchor="w", pady=(0, 12))
        ttk.Button(parent, text="打开装备测试数据…", command=self.preview_file).pack(anchor="w")
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True, pady=(12, 0))
        self.output = tk.Text(holder, wrap="word", font=("Microsoft YaHei UI", 10), background="#fafafa", relief="flat", state="disabled")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def preview_payload(self, payload):
        items = observations_from_dict(payload)
        rules = [Rule.from_dict(raw, self.catalog) for raw in self.rules]
        planner = LockPlanner(self.catalog)
        sections = []
        for item in items:
            decision = planner.plan(item, rules)
            state = {"press_l": "满足规则，可锁定（本次仅测试）", "skip": "跳过", "review": "需要核对"}[decision.action]
            sections.append(f"{item.name or item.equipment_key or '未识别装备'}\n{state}\n" + "\n".join(f"  • {reason}" for reason in decision.reasons))
        return "\n\n".join(sections) or "没有装备测试数据。"

    def preview_file(self):
        path = filedialog.askopenfilename(title="打开装备测试数据", initialdir=BASE / "examples", filetypes=(("装备测试数据", "*.json"),), parent=self.root)
        if path:
            try:
                text = self.preview_payload(json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_float=Decimal))
                self.output.configure(state="normal")
                self.output.delete("1.0", "end")
                self.output.insert("1.0", text)
                self.output.configure(state="disabled")
            except (ValueError, TypeError, OSError) as exc:
                messagebox.showerror("无法测试规则", str(exc), parent=self.root)


def main():
    root = tk.Tk()
    RuleEditor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
