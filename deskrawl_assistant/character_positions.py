"""Read Unity transform positions externally, without invoking engine methods.

The native Component -> GameObject -> first Transform and hierarchy layout
are verified from UnityPlayer's Component.get_transform (0xba320) and position
getters (0x66d820 / 0x58a80). An unfamiliar engine refuses this feature.
"""
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import struct
from .native_memory import MemoryReadError

ENGINE_SHA256 = '9feedb4527a30fae4762bff45d204f8e85b90851b2c8ae8d24f436f7d474667b'


@lru_cache(maxsize=4)
def _engine_hash(path, size, modified):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _point(pose, point):
    """Parent translation + quaternion rotation of parent-scaled child point."""
    p = [point[i] * pose[8 + i] for i in range(3)]
    x, y, z, w = pose[4:8]
    u = (y * p[2] - z * p[1], z * p[0] - x * p[2], x * p[1] - y * p[0])
    v = (y * u[2] - z * u[1], z * u[0] - x * u[2], x * u[1] - y * u[0])
    return tuple(pose[i] + p[i] + 2 * (w * u[i] + v[i]) for i in range(3))


class Positions:
    def __init__(self, copy):
        self.copy, self.cache = copy, {}
        module = copy.reader.memory.modules.get('unityplayer.dll')
        if not module:
            raise MemoryReadError('角色距离读取需要已验证的 Unity 引擎。')
        path = Path(module['path'])
        if path.parent != copy.reader.memory.path.parent:
            raise MemoryReadError('Unity 引擎模块不在游戏目录。')
        stamp = path.stat()
        if _engine_hash(str(path), stamp.st_size, stamp.st_mtime_ns) != ENGINE_SHA256:
            raise MemoryReadError('Unity 引擎已变化，角色距离条件需要维护。')
        self.path, self.stamp = path, (stamp.st_size, stamp.st_mtime_ns)

    def position(self, obj):
        if obj in self.cache:
            return self.cache[obj]
        c = self.copy
        klass = c.ptr(obj)
        for _ in range(12):
            if c.reader.class_name(klass) == 'Object':
                c.klass(klass, 'UnityObject')
                break
            klass = c.ptr(klass + 88)
        else:
            raise MemoryReadError('角色对象没有已验证的 Unity 父类。')
        native = c.ptr(obj + 16)
        game_object = c.ptr(native + 32)
        components = c.ptr(game_object + 32)
        transform = c.ptr(components + 8)
        hierarchy, index = struct.unpack('<Qi', c.read(transform + 40, 12))
        matrices, parents = c.ptr(hierarchy + 24), c.ptr(hierarchy + 32)
        seen, point = set(), None
        while index >= 0:
            if index > 100000 or index in seen or len(seen) >= 128:
                raise MemoryReadError('角色坐标层级无效。')
            seen.add(index)
            # Coordinates move every frame: copy each pose once, guard pointers
            # and parent indices rather than requiring the character to stop.
            pose = struct.unpack('<12f', c.read(matrices + index * 48, 48, stable=False))
            if any(not math.isfinite(v) or abs(v) > 1e6 for v in pose):
                raise MemoryReadError('角色坐标数值无效。')
            point = pose[:3] if point is None else _point(pose, point)
            index = c.integer(parents + index * 4)
        if point is None:
            raise MemoryReadError('角色坐标索引无效。')
        if (self.path.stat().st_size, self.path.stat().st_mtime_ns) != self.stamp:
            raise MemoryReadError('读取角色时 Unity 引擎文件变化，请重新连接。')
        self.cache[obj] = point
        return point

    def nearby_count(self, player, enemies, radius, limit):
        if not math.isfinite(radius) or not 0 <= radius <= 1e6:
            raise MemoryReadError('距离条件半径无效。')
        here = self.position(player)
        count = 0
        for enemy in enemies:
            there = self.position(enemy)
            # Player.fdq ignores the vertical axis and includes the boundary.
            if math.hypot(there[0] - here[0], there[2] - here[2]) <= radius:
                count += 1
                if count >= limit:
                    break
        return count
