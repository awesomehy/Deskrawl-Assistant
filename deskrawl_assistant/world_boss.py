"""Read-only WorldBoss entry gates mirrored from the verified 1.1.0 game.

Entry itself always uses TownPortalSystem and its native checks. Never change
the saved daily counter or consume an entry from this module.
"""
from datetime import datetime, timedelta, timezone
import struct
import time

from .native_memory import MemoryReadError

MAP_TYPE = 4
PUMPKIN = 'WorldBossPumpkin'
DAILY_LIMIT = 100
RESET_OFFSET_SECONDS = 32400
# MapData.PdtOffset is a fixed -7 hours, including the November event end.
WINDOW_TIMEZONE = timezone(timedelta(hours=-7))


def is_world_boss(map_id):
    return map_id == PUMPKIN


def entry_status(day, used, start='', end='', *, now=None):
    """Native day resets at 09:00 UTC; the event window uses MapData's PDT."""
    if type(day) is not int or type(used) is not int or day < 0 or not 0 <= used <= DAILY_LIMIT:
        raise MemoryReadError('世界首领入场次数未确认。')
    now = time.time() if now is None else now
    current_day = int((now - RESET_OFFSET_SECONDS) // 86400)
    remaining = DAILY_LIMIT - used if day == current_day else DAILY_LIMIT
    opened, reason = True, ''
    if start:
        try:
            begin = datetime.strptime(start, '%Y-%m-%d %H:%M').replace(tzinfo=WINDOW_TIMEZONE).timestamp()
            finish = datetime.strptime(end, '%Y-%m-%d %H:%M').replace(tzinfo=WINDOW_TIMEZONE).timestamp()
            if finish <= begin: raise ValueError('invalid event window')
        except (TypeError, ValueError) as exc:
            raise MemoryReadError('世界首领活动时间未确认。') from exc
        opened = begin <= now < finish
        if now < begin: reason = '南瓜王活动尚未开放。'
        elif now >= finish: reason = '南瓜王活动已结束。'
    if opened and remaining == 0: reason = '世界首领今日入场次数已用尽，等待每日重置。'
    return {'available':True, 'open':opened, 'remaining':remaining, 'daily_limit':DAILY_LIMIT,
            'reset_at':datetime.fromtimestamp((current_day+1)*86400+RESET_OFFSET_SECONDS, timezone.utc).isoformat(),
            'can_enter':opened and remaining > 0, 'reason':reason}


def _text(read, obj, offset):
    ptr = read.ptr(obj+offset)
    if not ptr: return ''
    length = read.integer(ptr+16)
    if not 0 <= length <= 64: raise MemoryReadError('世界首领活动时间格式异常。')
    return read.read(ptr+20,length*2).decode('utf-16le')


def read_entry(read, map_object):
    read.verify(map_object, 'MapData')
    if read.integer(map_object+32) != MAP_TYPE:
        raise MemoryReadError('世界首领地图类型未确认。')
    save = read.singleton('SaveSystem')
    now = time.time()
    if read.integer(save+32) == 1:
        cloud = read.singleton('CloudClient')
        # CloudClient's authoritative clock = UtcNow + its server TimeSpan.
        now += struct.unpack('<q', read.read(cloud+280,8))[0] / 10_000_000
        state = read.ptr(cloud+440)
    else:
        container = read.verify(read.ptr(save+40), 'SaveContainer')
        state = read.ptr(container+48)
    read.verify(state, 'WorldBossEntryState')
    day = struct.unpack('<q',read.read(state+16,8))[0]
    used = read.integer(state+24)
    return entry_status(day, used, _text(read,map_object,120), _text(read,map_object,128), now=now)


def target_blocked(scene, map_id, difficulty):
    """Only gate a future entry, never interrupt an already paid live run."""
    if not is_world_boss(map_id): return ''
    rows = (scene.get('maps_by_difficulty') or {}).get(difficulty, [])
    row = next((r for r in rows if r.get('map_id',r.get('id')) == map_id), None)
    if row is None: return '南瓜王入场状态尚未确认，请刷新游戏状态后继续。'
    if row.get('can_enter') is not True:
        return row.get('entry_reason') or '南瓜王尚未解锁、活动未开放或今日入场次数已用尽。'
    return ''
