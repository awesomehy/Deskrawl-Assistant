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
    def __init__(self, *, managed_test_mode=False):
        if type(managed_test_mode) is not bool:
            raise ValueError('托管测试模式必须由本地启动参数指定。')
        self.managed_test_mode = managed_test_mode
        self._reader = None
        self._actions = None
        self._guard = threading.RLock()
        self.pid: int | None = None
        self.last_error: str | None = None
        self.item_events = None
        self.compatibility = {'phase':'idle','message':'连接时自动检测游戏兼容性'}

    def _compatibility_changed(self, state):
        self.compatibility = dict(state)

    @property
    def connected(self) -> bool:
        reader = self._reader
        return reader is not None and reader.memory.is_running()

    def connect(self, pid: int | None = None) -> dict[str, Any]:
        from .native_memory import game_pids
        from .native_reader import NativeReader
        with self._guard:
            self.close()
            self._compatibility_changed({'phase':'checking','message':'正在检测游戏兼容性…'})
            games = game_pids()
            if pid is not None:
                games = [value for value in games if value == pid]
            if len(games) != 1:
                self._compatibility_changed({'phase':'idle','message':'请启动游戏后连接，自动检测兼容性。'})
                raise RuntimeConnectionError("请先启动 Deskrawl，并只保留一个游戏进程。")
            try:
                self._reader = NativeReader(games[0], on_compatibility=self._compatibility_changed)
                self.pid = games[0]
                self.last_error = None
                result = self._reader.diagnostics()
                result['lock_backend'] = 'direct_set_true'
                result['background_lock_available'] = True
                return result
            except Exception as exc:
                from .game_compatibility import CompatibilityError
                self._compatibility_changed({'phase':'blocked' if isinstance(exc,CompatibilityError) else 'error',
                    'message':str(exc)})
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

    def experience_snapshot(self):
        from . import features
        if not features.REPORTING_ENABLED: return features.reporting_disabled()
        from .experience_reader import read_experience
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError('尚未连接游戏。')
            value = read_experience(self._reader)
            if not value.get('available'):
                value['error'] = value.get('reason') or '经验读取尚不可用。'
                return value
            value['stage_id'] = value['stage']
            difficulty = {'Normal':'普通', 'Hard':'困难', 'Nightmare':'噩梦', 'Hell':'地狱', 'Inferno':'炼狱', 'Inferno1':'炼狱'}.get(value['difficulty'],value['difficulty'])
            if value['stage'] and difficulty:
                value['stage'] += ' · ' + difficulty
            raw_character = value['character']
            value['character_id'] = raw_character
            parts = raw_character.split(':',2)
            value['character'] = (parts[-1] or '未命名角色') + (' · 存档 ' + str(int(parts[1])+1) if len(parts)==3 else '')
            value['profile'] = value['character'] + ' · 等级 ' + str(value['level']) + (' · 模式 ' + parts[0] if len(parts)==3 else '')
            return value

    def managed_snapshot(self):
        """Fresh inventory data without writing a debug JSON on every step."""
        return self.snapshot(save=False)

    def recommendation_snapshot(self):
        from . import features
        if not features.REPORTING_ENABLED: return features.reporting_disabled()
        from .recommendation_reader import read_recommendation
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError('尚未连接游戏。')
            return read_recommendation(self._reader)

    def character_snapshot(self):
        from .character_reader import read_character
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError('尚未连接游戏。')
            return read_character(self._reader)

    def statistics_snapshot(self):
        """Native cumulative playtime and rewards, bound to the current character."""
        from .statistics_reader import read_player_statistics
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError('尚未连接游戏。')
            return read_player_statistics(self._reader)

    def loot_preview_snapshot(self):
        from .loot_plan_reader import read_loot_plan
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError('尚未连接游戏。')
            return read_loot_plan(self._reader)

    def loot_capture_snapshot(self):
        """Private, read-only evidence. Raw identifiers never go to HTTP state."""
        from .loot_plan_reader import read_loot_plan
        with self._guard:
            if self._reader is None: raise RuntimeConnectionError('尚未连接游戏。')
            raw = {}
            session = self._reader.session_id
            preview = read_loot_plan(self._reader, raw_sink=raw)
            # A temporarily unreadable inventory/control must not suppress a
            # valid planned high-value capture. Actual detection waits for a
            # complete baseline; errors never trigger a game operation.
            try: snapshot = self.snapshot(save=False)
            except Exception: snapshot = {}
            try: control = self.control_snapshot()
            except Exception: control = {}
            ground, carriage = control.get('ground') or [], control.get('carriage') or {}
            complete = (snapshot.get('complete') is True and control.get('available') is True
                        and isinstance(ground,list) and control.get('ground_complete') is True
                        and carriage.get('complete') is True
                        and control.get('scene_changing') is not True
                        and control.get('teleporting') is not True)
            scene = control.get('scene') if control.get('available') is True else None
            run = control.get('run') or {}
            observation_run = None
            if (scene == 'combat' and run.get('retained_terminal') is not True
                    and control.get('scene_changing') is not True
                    and control.get('teleporting') is not True):
                # Terminal runs are retained for completion accounting, not
                # loot provenance. A plan/control transition also cannot bind
                # newly observed objects to either run's cached plan.
                planned_run = preview.get('run') or {}
                if (preview.get('available') is not True or
                        (planned_run.get('id') == run.get('id')
                         and planned_run.get('map_id') == control.get('map_id')
                         and planned_run.get('difficulty') == control.get('difficulty'))):
                    observation_run = run.get('id')
            observations = []
            for container in ('inventory','storage'):
                for row in snapshot.get('containers',{}).get(container,{}).get('slots',[]):
                    if row.get('item_uid'): observations.append(dict(row,capture_source=container))
            for source, collection in (('ground',ground),('carriage',carriage.get('items',[]))):
                if isinstance(collection,list): observations.extend(dict(row,capture_source=source) for row in collection)
            return {'session':session, 'preview':preview, 'raw_plan':raw or None,
                'observations_complete':complete, 'observations':observations,
                'observation_run':observation_run, 'observation_scene':scene,
                'character_key':control.get('character_id')}

    def managed_loot_snapshot(self):
        """One atomic plan read, bound to this connection; no inventory scan."""
        from .loot_plan_reader import read_loot_plan
        with self._guard:
            if self._reader is None: raise RuntimeConnectionError('尚未连接游戏。')
            return {**read_loot_plan(self._reader), 'session': self._reader.session_id}

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

    def move_items(self, expected, target, validate=lambda: True, *, target_indices=None):
        from .container_transfer import move_items
        with self._guard:
            if self._reader is None:
                raise RuntimeConnectionError("尚未连接游戏。")
            return move_items(self._reader, expected, target, validate,
                on_commit=lambda result: self.item_events('items_transferred',result) if self.item_events else None,
                target_indices=target_indices)

    def _game_actions(self):
        # Construction and capability discovery never install the dispatcher.
        if self._reader is None: raise RuntimeConnectionError('尚未连接游戏。')
        if self._actions is None:
            from .game_actions import GameActions
            self._actions = GameActions(self._reader, allow_unverified=self.managed_test_mode)
        return self._actions

    def managed_capabilities(self):
        with self._guard: return self._game_actions().capabilities()

    def control_snapshot(self):
        with self._guard: return self._game_actions().control_snapshot()

    def reconnect_snapshot(self):
        with self._guard: return self._game_actions().reconnect_runtime().snapshot()

    def execute_reconnect_action(self, kind, character_index, expected_character, *, validate, cancel_event=None):
        with self._guard:
            return self._game_actions().reconnect_runtime().execute(kind, character_index, expected_character,
                validate=validate, cancel_event=cancel_event)

    def action_context_snapshot(self):
        with self._guard: return self._game_actions().action_context_snapshot()

    def progression_snapshot(self, *, merchant=False):
        with self._guard: return self._game_actions().progression_snapshot(merchant=merchant)

    def synthesis_snapshot(self, *, gems=True, runes=True):
        with self._guard: return self._game_actions().synthesis_snapshot(gems=gems, runes=runes)

    def execute_game_action(self, kind, payload, cancel_event=None, *, validate=lambda:True):
        with self._guard:
            result = self._game_actions().execute(kind,payload,cancel_event=cancel_event,validate=validate)
            receipt = result.get('_receipt_snapshot')
            if isinstance(receipt,dict):
                receipt['captured_at'] = datetime.now(timezone.utc).isoformat()
                receipt['process_id'] = self.pid
            return result

    def close(self):
        with self._guard:
            if self._actions is not None:
                self._actions.close()
                self._actions = None
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
