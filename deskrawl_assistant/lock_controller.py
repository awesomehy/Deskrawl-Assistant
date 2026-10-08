"""Guarded normal L actions. Hardware is supplied by the user-facing app only."""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from .action_log import record

BASE = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GridCalibration:
    origin: tuple[int, int]
    dx: int
    dy: int
    columns: int
    window_bounds: tuple[int, int, int, int]

    def __post_init__(self):
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.dx, self.dy, self.columns)):
            raise ValueError("格子间距和列数必须是整数")
        if not 12 <= self.dx <= 200 or not 12 <= self.dy <= 200 or not 2 <= self.columns <= 20:
            raise ValueError("背包格子间距或列数异常，请重新校准")
        if len(self.origin) != 2 or len(self.window_bounds) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in (*self.origin, *self.window_bounds)):
            raise ValueError("背包位置数据无效")

    def point(self, index):
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 1024:
            raise ValueError("背包格子索引无效")
        point = (self.origin[0] + self.dx * (index % self.columns), self.origin[1] + self.dy * (index // self.columns))
        left, top, right, bottom = self.window_bounds
        if not left <= point[0] < right or not top <= point[1] < bottom:
            raise ValueError("目标格子已超出游戏窗口，请重新校准")
        return point

    def to_dict(self):
        return {"origin": list(self.origin), "dx": self.dx, "dy": self.dy, "columns": self.columns, "window_bounds": list(self.window_bounds)}

    @classmethod
    def from_dict(cls, data):
        return cls(tuple(data['origin']), data['dx'], data['dy'], data['columns'], tuple(data['window_bounds']))


class LockController:
    def __init__(self, client, io, cancelled=None, progress=lambda text: None):
        self.client = client
        self.io = io
        self.cancelled = cancelled or threading.Event()
        self.progress = progress
        self.uncertain = set()

    def pause(self, seconds):
        if self.cancelled.wait(seconds) or self.io.key_down(0x1b):
            raise RuntimeError("操作已停止")

    def capture_calibration(self):
        points = []
        bounds = None
        stages = ("第一行第一个格子", "第一行第二个格子", "第二行第一个格子")
        for stage, description in enumerate(stages):
            self.progress(f"校准 {stage+1}/3：切回游戏，鼠标放在{description}中心，按 F8。")
            deadline = time.monotonic() + 90
            was_down = self.io.key_down(0x77)
            while time.monotonic() < deadline:
                self.pause(0.05)
                if self.io.key_down(0x1b):
                    raise RuntimeError("已取消校准")
                down = self.io.key_down(0x77)
                if down and not was_down and self.io.foreground():
                    hover = self.client.call("hover")
                    if hover is None:
                        self.progress("未读到悬停格子，请移动到背包格子中心后再按 F8。")
                    else:
                        index = hover['slot_index']
                        if (stage==0 and index!=0) or (stage==1 and index!=1) or (stage==2 and not 2 <= index <=20):
                            self.progress(f"当前格子是第 {index+1} 格，请按校准提示选择格子。")
                        else:
                            now_bounds = self.io.bounds()
                            if bounds is not None and now_bounds != bounds:
                                raise RuntimeError("校准期间游戏窗口发生移动，请重新校准")
                            bounds = now_bounds
                            points.append((self.io.position(), index))
                            break
                was_down = down
            else:
                raise RuntimeError("校准等待超时，请重试")
        origin, second, row = [point for point, index in points]
        if abs(second[1]-origin[1])>8 or abs(row[0]-origin[0])>8:
            raise ValueError("请选择每个格子的中心；三点没有对齐")
        return GridCalibration(origin, second[0]-origin[0], row[1]-origin[1], points[2][1], bounds)

    @staticmethod
    def same_target(expected, actual):
        return bool(actual and actual.get('item_uid') == expected.get('item_uid') and actual.get('instance_id') == expected.get('instance_id')
                    and actual.get('name_key') == expected.get('name_key') and actual.get('slot_index') == expected.get('slot_index'))

    def lock_one(self, expected, calibration, validate_actual=lambda item: True):
        identity = (expected.get('item_uid'), expected.get('instance_id'))
        if not all(identity) or not expected.get('is_equipment'):
            raise RuntimeError("装备身份不完整，未发送按键")
        if identity in self.uncertain:
            raise RuntimeError("这件装备有一次未确认的锁定操作，不会重复按 L")
        if tuple(self.io.bounds()) != calibration.window_bounds:
            raise RuntimeError("游戏窗口位置或尺寸已变化，请重新校准")
        point = calibration.point(expected['slot_index'])
        self.progress(f"正在定位背包第 {expected['slot_index']+1} 格。")
        self.io.move(point)
        self.pause(0.15)
        actual = self.client.call('hover')
        record('hover_checked', point=list(point), expected={key:expected.get(key) for key in ('slot_index','item_uid','instance_id','name_key')},
            actual={key:actual.get(key) for key in ('slot_index','item_uid','instance_id','name_key','locked')} if actual else None)
        if not self.same_target(expected, actual):
            raise RuntimeError("悬停装备与目标唯一 ID 不一致，未发送 L；请核对背包页和校准位置")
        if actual.get('locked') is True:
            return 'already_locked'
        if actual.get('locked') is not False:
            raise RuntimeError("锁定状态未确认，未发送 L")
        if not validate_actual(actual):
            raise RuntimeError("当前装备词条已不满足规则，未发送 L")
        if self.io.position() != point or not self.io.foreground() or self.io.key_down(0x1b):
            raise RuntimeError("鼠标、窗口焦点或停止键发生变化，未发送 L")
        # Reserve before sending; any exception after this point prevents an
        # unsafe retry of a toggle action, even if SendInput partially succeeds.
        self.uncertain.add(identity)
        self.progress("已核对悬停装备与未锁状态，正在发送一次 L。")
        self.io.press_l()
        self.progress("L 已发送，正在检查游戏锁标志。")
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            self.pause(0.05)
            after = self.client.call('hover')
            if not self.same_target(expected, after):
                raise RuntimeError("锁定后悬停目标变化，停止操作；不会重复按 L")
            if after.get('locked') is True:
                self.uncertain.discard(identity)
                self.progress("游戏已确认装备锁定。")
                return 'locked'
        raise RuntimeError("未读到锁定成功，停止操作；不会重复按 L")


def save_calibration(value):
    path = BASE / 'config/inventory-grid.json'
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def load_calibration():
    path = BASE / 'config/inventory-grid.json'
    try:
        return GridCalibration.from_dict(json.loads(path.read_text(encoding='utf-8')))
    except (FileNotFoundError, ValueError, TypeError, KeyError):
        return None
