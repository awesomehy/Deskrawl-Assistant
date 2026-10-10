"""Passive, contiguous run observations and persistent recommendation settings.

This book does not drive gameplay. Attaching halfway through a run never yields
an invented full-run sample; terminal state must come from verified game flags.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import sqlite3
import threading
import time

DEFAULTS = {"minutes": 60, "difficulty": "current", "overhead_seconds": 8, "overhead_mode": "auto",
            "include_locked": True, "sort": "completed"}


def validate_settings(value, difficulties):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("刷图推荐设置无效。")
    result = DEFAULTS | value
    if 'overhead_mode' not in value and 'overhead_seconds' in value:
        result['overhead_mode']='manual'
    if result['overhead_mode'] not in ('auto','manual'):
        raise ValueError('重开耗时模式无效。')
    for key, maximum, positive in (("minutes", 10080, True), ("overhead_seconds", 300, False)):
        number = result[key]
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0 or number > maximum or (positive and number <= 0):
            raise ValueError("计划时长或额外耗时超出范围。")
    if result["difficulty"] not in {"current", *difficulties}:
        raise ValueError("难度选项无效。")
    if type(result["include_locked"]) is not bool or result["sort"] not in ("completed", "rate"):
        raise ValueError("比较口径或地图筛选无效。")
    return result


def finite(value, default=0):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else default


class RecommendationBook:
    def __init__(self, path, *, clock=time.monotonic, now=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())
        self.guard = threading.RLock()
        self.previous = None
        self.active = None
        self.last_complete = None
        self.status = "连接游戏后自动观察刷图；中途连接的场次从下一轮开始计时。"
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS recommendation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, character_id TEXT NOT NULL,
                map_id TEXT NOT NULL, difficulty TEXT NOT NULL, level INTEGER NOT NULL,
                xp INTEGER, run_seconds REAL NOT NULL,
                normal_seconds REAL NOT NULL, boss_seconds REAL NOT NULL,
                normal_health REAL NOT NULL, boss_health REAL NOT NULL,
                fixed_seconds REAL NOT NULL, outcome TEXT NOT NULL,
                overhead_seconds REAL, combat TEXT NOT NULL,
                recorded_at TEXT NOT NULL, excluded INTEGER NOT NULL DEFAULT 0)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(recommendation_runs)")}
            for name, definition in (("family", "TEXT NOT NULL DEFAULT ''"), ("normal_fixed_seconds", "REAL NOT NULL DEFAULT 0"), ("boss_fixed_seconds", "REAL NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute("ALTER TABLE recommendation_runs ADD COLUMN " + name + " " + definition)
            db.execute("CREATE TABLE IF NOT EXISTS recommendation_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def settings(self, difficulties):
        with self.guard, self._db() as db:
            row = db.execute("SELECT value FROM recommendation_settings WHERE key='settings'").fetchone()
        try:
            return validate_settings(json.loads(row[0]) if row else {}, difficulties)
        except (ValueError, TypeError):
            return deepcopy(DEFAULTS)

    def save_settings(self, value, difficulties):
        settings = validate_settings(value, difficulties)
        with self.guard, self._db() as db:
            db.execute("INSERT OR REPLACE INTO recommendation_settings VALUES ('settings',?)", (json.dumps(settings),))
        return settings

    def calibration(self, profile):
        fingerprint = profile.get("fingerprint", "")
        with self.guard, self._db() as db:
            db.row_factory = sqlite3.Row
            result = [dict(row) for row in db.execute(
                "SELECT * FROM recommendation_runs WHERE fingerprint=? AND excluded=0 ORDER BY id DESC LIMIT 80", (fingerprint,))]
            for row in result:
                row["combat"] = json.loads(row["combat"])
            active = self.active
            public_active = None if not active else {
                "map_id": active["map_id"], "phase": active["phase"],
                "elapsed_seconds": max(0, self.clock() - active["started_at"])}
            return {"runs": result, "samples": len(result), "active": public_active,
                    "last_recorded_at": result[0]["recorded_at"] if result else None, "status": self.status}

    def reset(self, profile):
        # Keep the old evidence recoverable in SQLite rather than deleting it.
        fingerprint = profile.get("fingerprint")
        if not fingerprint:
            raise ValueError("读取当前角色后才能重置校准。")
        with self.guard, self._db() as db:
            db.execute("UPDATE recommendation_runs SET excluded=1 WHERE fingerprint=?", (fingerprint,))
            self.active = None
            self.last_complete = None
            self.status = "已重新开始当前配装的校准，从下一轮完整刷图观察。"

    def invalidate(self, reason="游戏读取中断，等待下一轮完整刷图。"):
        with self.guard:
            self.active = None
            self.previous = None
            self.last_complete = None
            self.status = reason

    def observe(self, snapshot, experience=None):
        with self.guard:
            now = self.clock()
            if not snapshot.get("available"):
                self.invalidate(snapshot.get("reason") or "角色暂不可读取，等待下一轮完整刷图。")
                return
            profile = snapshot.get("profile") or {}
            run = snapshot.get("run") or {}
            identity = (profile.get("character_id"), snapshot.get("session"))
            fingerprint = profile.get("fingerprint")
            if not fingerprint or not identity[0]:
                self.invalidate("角色与配装身份尚未核验，等待下一轮完整刷图。")
                return
            previous = self.previous
            same_connection = previous and previous["identity"] == identity and now - previous["at"] <= 5
            run_id = run.get("id")
            if self.active and (not same_connection or self.active["fingerprint"] != fingerprint):
                self.active = None
                self.status = "连接或配装已变化，等待下一轮完整刷图。"
            changed = same_connection and run_id and (run_id != previous["run_id"] or not previous.get("started"))
            # New ID following a continuously observed scene/run is the start
            # boundary. A terminal snapshot can never start a fresh sample.
            terminal = run.get("finished") is True or run.get("dead") is True
            if changed and self.active and self.active["run_id"] != run_id:
                self.active = None
                self.status = "上轮结束状态未能完整读取，等待新一轮刷图。"
            if changed and run.get("started") is True and run.get("at_start") is True and not terminal:
                self.active = None
                if self.last_complete:
                    last = self.last_complete
                    gap = now - last["at"]
                    if 0 <= gap <= 30 and last["fingerprint"] == fingerprint and last["identity"] == identity:
                        with self._db() as db:
                            db.execute("UPDATE recommendation_runs SET overhead_seconds=? WHERE id=?", (gap, last["id"]))
                    self.last_complete = None
                xp = self._xp(experience, profile, run_id)
                self.active = {"run_id": run_id, "fingerprint": fingerprint,
                    "character_id": identity[0], "identity": identity, "map_id": run.get("map_id", ""),
                    "difficulty": run.get("difficulty", profile.get("difficulty", "Normal")),
                    "level": profile.get("level", 0), "xp_start": xp, "xp_session": (experience or {}).get("session"),
                    "started_at": now, "last_at": now, "phase": run.get("phase", "normal"),
                    "normal_seconds": 0.0, "boss_seconds": 0.0,
                    "normal_health": finite(run.get("planned_normal_health")),
                    "boss_health": finite(run.get("planned_boss_health")),
                    "fixed_seconds": finite(run.get("fixed_seconds")), "normal_fixed_seconds":finite(run.get("normal_fixed_seconds")),
                    "boss_fixed_seconds":finite(run.get("boss_fixed_seconds")), "family":run.get("family", ""), "combat": deepcopy(profile.get("combat", {}))}
                self.status = "正在观察完整刷图，结束后自动校准。"
            active = self.active
            if active and active["run_id"] == run_id:
                delta = max(0, now - active["last_at"])
                key = "boss_seconds" if active["phase"] == "boss" else "normal_seconds"
                active[key] += delta
                active["last_at"] = now
                active["phase"] = run.get("phase", active["phase"])
                if terminal:
                    seconds = now - active["started_at"]
                    xp_end = self._xp(experience, profile, run_id)
                    same_xp_session = (experience or {}).get("session") == active["xp_session"]
                    xp = None if xp_end is None or active["xp_start"] is None or not same_xp_session else xp_end - active["xp_start"]
                    if xp is not None and xp < 0:
                        xp = None
                    outcome = "failed" if run.get("dead") is True else "success"
                    if 1 <= seconds <= 86400 and active["map_id"]:
                        persistent_id = hashlib.sha256(json.dumps([active["identity"], active["run_id"]], ensure_ascii=False).encode("utf-8")).hexdigest()
                        fields = (persistent_id, active["fingerprint"], active["character_id"], active["map_id"],
                            active["difficulty"], active["level"], xp, seconds, active["normal_seconds"],
                            active["boss_seconds"], active["normal_health"], active["boss_health"], active["fixed_seconds"],
                            outcome, json.dumps(active["combat"], ensure_ascii=False), self.now(), active["family"], active["normal_fixed_seconds"], active["boss_fixed_seconds"])
                        with self._db() as db:
                            cursor = db.execute("""INSERT OR IGNORE INTO recommendation_runs
                            (run_id,fingerprint,character_id,map_id,difficulty,level,xp,run_seconds,
                             normal_seconds,boss_seconds,normal_health,boss_health,fixed_seconds,outcome,combat,recorded_at,family,normal_fixed_seconds,boss_fixed_seconds)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", fields)
                            if cursor.rowcount:
                                self.last_complete = {"at": now, "id": cursor.lastrowid, "fingerprint": fingerprint, "identity": identity}
                        self.status = "已记录完整通关并校准。" if outcome == "success" else "已记录失败耗时与实际经验，用于评估通关风险。"
                    self.active = None
            elif run.get("started") is True and not terminal and not self.active:
                self.status = "当前场次从中途连接，下一轮开始自动校准。"
            self.previous = {"identity": identity, "at": now, "run_id": run_id, "started":run.get("started") is True}

    @staticmethod
    def _xp(experience, profile, run_id):
        experience = experience or {}
        if experience.get("run_id") != run_id:
            return None
        value = experience.get("total_xp")
        if experience.get("available") is True and type(value) is int and value >= 0 and experience.get("character_id", experience.get("character")) == profile.get("character_id"):
            return value
        return None
