"""External Deskrawl reads with a separate, narrowly scoped lock action."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any
from .paths import runtime_dir



class RuntimeConnectionError(RuntimeError):
    pass


class RuntimeClient:
    def __init__(self):
        self._reader = None
        self._guard = threading.RLock()
        self.pid: int | None = None
        self.last_error: str | None = None
        self.item_events = None

    @property
    def connected(self) -> bool:
        reader = self._reader
        return reader is not None and reader.memory.is_running()

    def connect(self, pid: int | None = None) -> dict[str, Any]:
        from .native_memory import game_pids
        from .native_reader import NativeReader
        with self._guard:
            self.close()
            games = game_pids()
            if pid is not None:
                games = [value for value in games if value == pid]
            if len(games) != 1:
                raise RuntimeConnectionError("请先启动 Deskrawl，并只保留一个游戏进程。")
            try:
                self._reader = NativeReader(games[0])
                self.pid = games[0]
                self.last_error = None
                result = self._reader.diagnostics()
                result['lock_backend'] = 'direct_set_true'
                result['background_lock_available'] = True
                return result
            except Exception as exc:
                self.last_error = str(exc)
                self.close()
                raise

    def call(self, name: str, *args):
        if name not in {"diagnostics", "describeclass", "discoverregistry", "snapshot", "hover"}:
            raise RuntimeConnectionError("此版本只提供读取功能。")
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError(self.last_error or "尚未连接游戏。")
            if name == "diagnostics":
                return self._reader.diagnostics()
            if name == "describeclass":
                return self._reader.describe_class(args[0])
            if name == "discoverregistry":
                records, _ = self._reader.registry()
                return {"registry_count": len(records), "backend": "external_read_only"}
            if name == "hover":
                return self._reader.hovered_inventory()
            from .native_memory import SnapshotChangedError
            max_items = (args[0] if args else {}).get("maxItems", 2048)
            for attempt in range(3):
                try:
                    return self._reader.snapshot(max_items)
                except SnapshotChangedError:
                    if attempt == 2:
                        raise
                    time.sleep(0.02)

    def snapshot(self, *, max_items: int = 2048, save: bool = True):
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 8192:
            raise ValueError("读取上限必须在 1 到 8192 之间")
        result = self.call("snapshot", {"maxItems": max_items})
        result["captured_at"] = datetime.now(timezone.utc).isoformat()
        result["process_id"] = self.pid
        if save:
            write_runtime_json("latest-snapshot.json", result)
        return result

    def lock_equipment(self, expected, validate=lambda row: True):
        from .background_lock import lock_equipment
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError("尚未连接游戏。")
            return lock_equipment(self._reader, expected, validate)

    def unlock_equipment(self, expected, validate=lambda row: True):
        from .background_lock import unlock_equipment
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError("尚未连接游戏。")
            return unlock_equipment(self._reader, expected, validate)

    def move_items(self, expected, target, validate=lambda: True):
        from .container_transfer import move_items
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError("尚未连接游戏。")
            return move_items(self._reader, expected, target, validate,
                on_commit=lambda result: self.item_events('items_transferred',result) if self.item_events else None)

    def close(self):
        with self._guard:
            reader, self._reader = self._reader, None
            self.pid = None
            if reader is not None:
                reader.close()

    def carriage_snapshot(self):
        from .carriage_transfer import public_carriage
        with self._guard:
            if self._reader is None: raise RuntimeConnectionError('尚未连接游戏。')
            return public_carriage(self._reader)

    def collect_carriage(self,selection_id,validate=lambda:True):
        from .carriage_transfer import collect_item
        with self._guard:
            if self._reader is None: raise RuntimeConnectionError('尚未连接游戏。')
            return collect_item(self._reader,selection_id,validate,
                on_commit=lambda result: self.item_events('carriage_collected',result) if self.item_events else None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def write_runtime_json(filename: str, payload: Any) -> Path:
    if Path(filename).name != filename:
        raise ValueError("输出必须位于运行记录目录")
    target = runtime_dir() / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
    return target
