"""Application bridge for the serial planner; no startup-time game operations."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import time

from .managed_mode import (ManagedMode, DEFAULT_SETTINGS, validate_settings, identity,
    run_record, DIFFICULTIES, FARM_DIFFICULTIES, equip_candidate_allowed, equipment_actions, equipment_policy, storage_material_kind)
from .models import ValidationError
from .rules import evaluate, DEFAULT_MIN_CONFIDENCE
from .native_memory import SnapshotChangedError, GameBusyError
from .native_dispatcher import NativeActionError, GUARD_FAILED, REJECTED
from .paths import RESOURCE_ROOT
from .stargazing import enabled as stargazing_enabled, decide as stargazing_decide, combat_rewards_clear, combat_rewards_known
from .loot_preview import decorate_equipment
from .item_protection import disposal_protection, mandatory_storage, town_rewards_clear
from .action_log import record
from .progression import catalog as growth_catalog, growth_action, next_purchase, NEW_ACTIONS, PURCHASE_BATCH_LIMIT, enabled as growth_enabled


def required_capabilities(settings):
    required = {'transfer'}
    if settings['auto_equip']['enabled']: required.add('equip')
    if settings['background']['rule_monitoring']: required.add('lock')
    if settings.get('progression', {}).get('talents_enabled'): required.add('talents')
    if settings.get('progression', {}).get('skills_enabled'): required.add('skills')
    if settings.get('life_skills', {}).get('enabled'): required.add('life_skills')
    if settings.get('merchant', {}).get('enabled'): required.update(('merchant', 'travel', 'lock', 'chests', 'ground_pickup'))
    if settings['farming']['enabled'] or settings.get('merchant', {}).get('enabled'):
        required.update(('lock','travel','chests','ground_pickup'))
        if (settings['farming']['mode']=='push' or settings['farming']['difficulty']!='current'
                or any(row['difficulty']!='current' for row in settings['farming'].get('repeat_maps',[]))):
            required.add('difficulty')
        cleanup = settings['cleanup']
        required.update(equipment_actions(cleanup))
        required.update((cleanup['rune_action'],cleanup['gem_action']))
    return required - {'store'}


def available_capability(value):
    return value is True or (isinstance(value,dict) and value.get('available') is True
                            and (value.get('native_verified') is not False or value.get('test_enabled') is True
                                 or value.get('release_enabled') is True))


def progression_from_catalog(maps, start):
    """Follow explicit prerequisites along the ordinary campaign, not name order."""
    current = str(start)
    by_id = {str(m.get('map_id',m.get('id'))):m for m in maps}
    if current not in by_id: raise ValidationError('目标关卡未收录。')
    route = [current]
    while True:
        following = [m for m in maps if str(m.get('prerequisite')) == current
                     and m.get('map_type') == 0 and m.get('supported') is not False
                     and not m.get('unavailable')]
        if len(following) != 1: break
        current = str(following[0].get('map_id',following[0].get('id')))
        if current in route: raise ValidationError('关卡前置关系存在循环。')
        route.append(current)
    return route


def campaign_route(maps, start, difficulty):
    if difficulty not in DIFFICULTIES: raise ValidationError('推图起始难度未确认。')
    first = progression_from_catalog(maps,start)
    result = [{'dungeon_id':key,'difficulty':difficulty} for key in first]
    ordinary = [m for m in maps if m.get('map_type')==0 and not m.get('unavailable') and m.get('supported') is not False]
    roots = [m for m in ordinary if not m.get('prerequisite')]
    if len(roots)!=1: raise ValidationError('普通关卡起始节点未唯一确认。')
    by_id = {str(m.get('map_id',m.get('id'))):m for m in ordinary}
    if any(str(key) not in by_id for key in first): return result
    root_id = roots[0].get('map_id',roots[0].get('id'))
    full = progression_from_catalog(ordinary,root_id)
    if str(first[-1])!=str(full[-1]): raise ValidationError('推图路线未连接到常规关卡终点。')
    for tier in DIFFICULTIES[DIFFICULTIES.index(difficulty)+1:]:
        result.extend({'dungeon_id':key,'difficulty':tier} for key in full)
    return result


def full_campaign_route(maps):
    ordinary = [m for m in maps if m.get('map_type')==0 and not m.get('unavailable') and m.get('supported') is not False]
    roots = [m for m in ordinary if not m.get('prerequisite')]
    if len(roots)!=1: raise ValidationError('普通关卡起始节点未唯一确认。')
    return campaign_route(maps,roots[0].get('map_id',roots[0].get('id')),'Normal')


def highest_unlocked_map(maps, control):
    """Use explicit campaign order and native unlocks, never map-name sorting."""
    unlocked = {row['id'] for row in control.get('difficulties',[]) if row.get('unlocked') is True}
    for point in reversed(full_campaign_route(maps)):
        tier = point['difficulty']
        if tier not in unlocked: continue
        rows = control.get('maps_by_difficulty',{}).get(tier,control.get('maps',[]) if tier==control.get('difficulty') else [])
        row = next((r for r in rows if str(r.get('id',r.get('map_id')))==str(point['dungeon_id'])),None)
        if row and row.get('unlocked') is True and row.get('supported') is not False and row.get('can_enter') is not False:
            return deepcopy(point)
    raise ValidationError('没有读取到已解锁且可进入的常规推图关卡。')


class ManagedServiceMixin:
    def _init_managed(self):
        self.managed_config = self.config.with_name('managed-mode.json')
        self.managed = ManagedMode()
        self.managed_merchant_uids = self.journal.merchant_item_uids()
        self.managed_control = {}
        self.managed_growth = {'available': False, 'reason': '连接游戏后读取成长状态。'}
        self.managed_capabilities = {}
        self.managed_history = []
        self.managed_started_at = None
        self.managed_started_clock = None
        self.managed_can_resume = False
        self.managed_stop_request = None
        self.managed_fast_followup = False
        self.managed_transition = None
        self.managed_retry = None
        self.managed_return_recovery = None
        self.managed_elapsed = 0
        self.managed_config_error = ''
        self.managed_config_load_failed = False
        loaded = False
        self.managed_catalog = json.loads((RESOURCE_ROOT/'data/recommendation-catalog.json').read_text(encoding='utf-8'))['maps']
        try:
            if self.managed_config.exists():
                settings = validate_settings(json.loads(self.managed_config.read_text(encoding='utf-8')),
                                             equipment_keys=self.ui['equipment'],allow_empty_rune_pages=True)
                self._validate_slot_locks(settings)
                loaded = True
                if not settings['cleanup']['rune_pages']:
                    # Keep every old preference editable; never silently replace
                    # a sale-policy configuration with the default preset.
                    self.managed.settings = settings
                    self.managed_config_error = '请补选符文存放页，用于强制收纳套装／宏伟符文；其他设置与原文件已保留。'
                else:
                    self.managed = ManagedMode(settings)
        except Exception as exc:
            self.managed_config_load_failed = True
            self.managed_config_error = '托管配置未载入，原文件已保留：'+str(exc)
        from .storage_preferences import SHARED_STORAGE_GROUPS, organizer_with_shared_pages
        if loaded:
            # Upgrade old installations in memory. Even unavailable pages and
            # empty legacy choices win over the organizer's conflicting pages.
            self.automation_settings = organizer_with_shared_pages(self.automation_settings,self.managed.settings['cleanup'])
        elif not self.managed_config_load_failed:
            # Users who never configured managed mode keep their organizer's
            # destinations. Original legacy files are not rewritten on startup.
            for kind,group in SHARED_STORAGE_GROUPS:
                self.managed.settings['cleanup'][kind+'_pages'] = list(self.automation_settings[group]['pages'])

    def _validate_slot_locks(self, settings):
        from .character import EQUIPPED_SLOTS
        for slot,key in settings['auto_equip']['slot_locks'].items():
            meta = self.ui['equipment'].get(key,{})
            target = EQUIPPED_SLOTS[int(slot)]
            if target.startswith('Ring'): target = 'Ring'
            if not key.startswith('Legendary') or meta.get('slot') != target:
                raise ValidationError('指定暗金必须属于对应的穿戴部位。')

    def _managed_refresh_control(self):
        """Called only by a new assistant's read worker, never by an HTTP GET."""
        if not self.client.connected: return
        if not hasattr(self.client,'control_snapshot'): return
        try:
            value = self.client.control_snapshot()
            capabilities = self.client.managed_capabilities()
        except SnapshotChangedError:
            raise
        except Exception as exc:
            value = {'available':False,'reason':str(exc)}
            capabilities = {}
        growth = {'available': False, 'reason': '成长读取入口尚未载入。'}
        if hasattr(self.client, 'progression_snapshot'):
            # Price previews are read-only and independent of enabling purchases.
            try: growth = self.client.progression_snapshot(merchant=True)
            except SnapshotChangedError: raise
            except Exception as exc: growth = {'available': False, 'reason': str(exc)}
        with self.guard:
            self.managed_growth = deepcopy(growth)
            self.managed_control = deepcopy(value)
            self.managed_capabilities = deepcopy(capabilities)

    def _managed_scene(self):
        control = deepcopy(self.managed_control)
        scene = control.get('scene',{})
        if not isinstance(scene,dict):
            scene = {'in_town':scene=='town','in_dungeon':scene=='combat'}
        scene.setdefault('available',control.get('available') is True)
        scene.setdefault('dungeon_id',control.get('map_id'))
        scene.setdefault('difficulty',control.get('difficulty'))
        for key in ('world_difficulty','mythic_entry_difficulty','scene_changing','teleporting','dead','completion_visible','online_saving','online_pending_requests',
                    'player_state','gathering','travel_blocked','travel_wait_reason'):
            scene.setdefault(key,control.get(key))
        run = deepcopy(run_record(scene.get('run',control.get('run'))))
        if run:
            run.setdefault('id',run.get('run_id'))
            run.setdefault('dungeon_id',run.get('map_id',scene['dungeon_id']))
        if scene.get('dead') is True and scene.get('in_town') is not True and run:
            # The death flag is independent of the online wave-plan lifecycle.
            # Never treat a stale running/completion plan as a successful run.
            run.update(status='failed', completion_confirmed=False)
        scene['run'] = run
        return scene

    def _managed_snapshot(self):
        capture = getattr(self.client,'managed_snapshot',self.client.snapshot)
        return capture()

    def _managed_observation(self, *, inventory_only=False):
        # Every write is planned from the latest server-side read, not browser rows.
        snap = self._managed_snapshot()
        self._publish(snap,refresh_character=not inventory_only)
        if not inventory_only:
            self._managed_refresh_control()
        else:
            # Recheck scene, online sync and capability changes. Inventory
            # actions do not need a second campaign/growth/character scan.
            context_reader = getattr(self.client,'action_context_snapshot',self.client.control_snapshot)
            value = context_reader()
            if value.get('available') is not True:
                raise SnapshotChangedError(value.get('reason') or '场景读取尚未确认。')
            capabilities = self.client.managed_capabilities()
            with self.guard:
                scene = value.get('scene',{})
                if not isinstance(scene,dict):
                    scene = {'in_town':scene=='town','in_dungeon':scene=='combat','available':True}
                previous = self.managed_control.get('scene',{})
                self.managed_control['scene'] = {**(previous if isinstance(previous,dict) else {}),**scene}
                self.managed_capabilities = deepcopy(capabilities)
        payload = self.state()
        raw = {(r.get('container'),r.get('slot_index')):r for c in snap.get('containers',{}).values() for r in c.get('slots',[])}
        observations = {(a.container,a.index):a.observation for a in self.adapter.adapt_snapshot(snap).items}
        for item in payload['items']:
            source = raw.get((item['container'],item['slot_index']),{})
            item['issues'] = list(source.get('issues',[]))
            item['item_type'] = source.get('item_type')
            item['item_class'] = source.get('item_class')
            item['base_rarity'] = source.get('base_rarity')
            item['equip_slot'] = source.get('equip_slot')
            if not item['is_equipment']:
                item['rarity'] = source.get('rarity',source.get('base_rarity'))
            item['rune_level'] = source.get('rune_level')
            item['rules_known'] = not bool(self.config_error)
            obs = observations.get((item['container'],item['slot_index']))
            if item['is_equipment']:
                identity_known = bool(obs and obs.name_confidence >= DEFAULT_MIN_CONFIDENCE
                                      and obs.lock_confidence >= DEFAULT_MIN_CONFIDENCE)
                if not identity_known:
                    item['issues'].append('装备身份或锁状态读取尚未核验。')
                if source.get('modifiers_complete') is not True:
                    item['issues'].append('装备实例读取不完整。')
                known = identity_known and all(obs.groups_complete.get(g) is True for g in ('primary','secondary'))
                active_rules = [r for r in self._rules() if r.enabled and item['name_key'] in r.equipment_keys]
                if not known or not item['rules_known']:
                    item['review'] = True; item['rule_status'] = 'review'
                elif not active_rules:
                    item['matches'] = False; item['review'] = False; item['rule_status'] = 'no_rule'
                else:
                    evaluations = [evaluate(r,obs,self.catalog) for r in active_rules]
                    item['review'] = any(e.status=='uncertain' for e in evaluations) and not item['matches']
                    item['rule_status'] = 'review' if item['review'] else 'match' if item['matches'] else 'miss'
        carriage = deepcopy(self.managed_control.get('carriage',self._carriage_payload()))
        ground = deepcopy(self.managed_control.get('ground',{}))
        if isinstance(ground,list):
            ground = {'items':ground,'complete':self.managed_control.get('ground_complete') is True}
        ground.setdefault('complete',False)
        for container in (carriage,ground):
            for item in container.get('items',[]):
                name,kind = self._describe_item(item.get('name_key') or item.get('internal_name'))
                item.setdefault('name',name); item.setdefault('kind',kind)
        scene = self._managed_scene()
        loot_plan = None
        if (not inventory_only and stargazing_enabled(self.managed.settings)
                and scene.get('in_town') is False
                and run_record(scene.get('run')).get('id') not in self.managed.stargazing_seen):
            try:
                loot_plan = self.client.managed_loot_snapshot()
                loot_plan['rules_known'] = not bool(self.config_error)
                rules = self._rules() if not self.config_error else []
                for wave in loot_plan.get('waves', []):
                    for item in wave.get('items', []):
                        decorate_equipment(item, rules, self.adapter, self.catalog)
            except (OSError, ValueError, RuntimeError, AttributeError, ValidationError):
                loot_plan = {'available': False, 'reason': '本局掉落计划暂不可读，等待重新核验。'}
        return {'complete':payload['complete'], 'session':snap.get('bridge_session'), 'loot_plan':loot_plan,
                'merchant_uids':list(self.managed_merchant_uids),
                'items':payload['items'],'counts':payload['counts'],'character':deepcopy(self.character),
                'progression':deepcopy(self.managed_growth), 'scene':scene,'carriage':carriage,'ground':ground,
                'automation_settings':deepcopy(self.automation_settings),
                'loot_ledger':deepcopy(self.managed_control.get('loot_ledger', {})),
                'system_log':deepcopy(self.managed_control.get('system_log', {})),
                'storage':{'page_size':snap.get('transfer',{}).get('page_size'),
                           'allowed_indices':snap.get('transfer',{}).get('storage_indices',[])}}

    def managed_page(self):
        with self.guard:
            engine = self.managed.state()
            transfer = (self.snapshot or {}).get('transfer',{})
            capabilities = deepcopy(self.managed_capabilities)
            capabilities['transfer'] = transfer.get('available') is True
            capabilities['lock'] = bool(self.client.connected and (self.snapshot or {}).get('complete'))
            missing = [k for k in sorted(required_capabilities(engine['settings']))
                       if not available_capability(capabilities.get(k))]
            blocked = [f'{key}：'+(capabilities.get(key,{}).get('reason','尚未核验')
                       if isinstance(capabilities.get(key),dict) else '尚未核验') for key in missing]
            control_available = self.managed_control.get('available') is True
            control_error = (self.managed_control.get('reason') or '等待关卡与场景完整读取。') if not control_available else ''
            if engine['settings']['farming']['enabled'] and not control_available:
                blocked.append('关卡读取：'+control_error)
            if growth_enabled(engine['settings']) and self.managed_growth.get('available') is not True:
                blocked.append('成长读取：'+(self.managed_growth.get('reason') or '等待成长状态完整读取。'))
            if engine['settings']['merchant']['enabled'] and self.managed_growth.get('available') is True:
                shop=self.managed_growth.get('merchant',{})
                if shop.get('unlock_known') is not True or shop.get('unlocked') is not True:
                    blocked.append('神秘商人：尚未解锁或解锁状态未确认，请关闭自动购买后再启用其他托管功能。')
            size = transfer.get('page_size')
            indices = transfer.get('storage_indices',[])
            pages = []
            warehouse_ready = bool(self.client.connected and (self.snapshot or {}).get('complete') is True
                and transfer.get('available') is True and type(size) is int and size > 0
                and isinstance(indices,list) and all(type(n) is int and n>=0 for n in indices)
                and len(set(indices))==len(indices))
            if warehouse_ready:
                occupied = {r.get('slot_index') for r in (self.snapshot or {}).get('containers',{}).get('storage',{}).get('slots',[])}
                for index in sorted({n//size+1 for n in indices}):
                    slots = [n for n in indices if n//size+1==index]
                    pages.append({'index':index,'name':f'仓库第 {index} 页',
                                  'capacity':len(slots),'free':sum(n not in occupied for n in slots)})
            maps = deepcopy(self.managed_control.get('maps',[]))
            maps_by_difficulty = deepcopy(self.managed_control.get('maps_by_difficulty',{}))
            for row in [*maps,*(row for rows in maps_by_difficulty.values() for row in rows)]:
                row.setdefault('id',row.get('map_id')); row.setdefault('name',row.get('stage',row['id']))
                row['automation_supported'] = row.get('supported',True) and row.get('unlocked') is True
                try: row['progression'] = progression_from_catalog(self.managed_catalog,row['id'])
                except ValidationError: row['progression'] = []
            elapsed = self.managed_elapsed + (max(0,time.monotonic()-self.managed_started_clock) if self.managed_started_clock else 0)
            try: automatic_start = highest_unlocked_map(self.managed_catalog,self.managed_control) if control_available else None
            except ValidationError: automatic_start = None
            return {**engine,'running':engine['running'] and self.managed_stop_request is None,
                    'paused':self.managed_can_resume or engine['phase']=='paused',
                    'message':self.managed_config_error or engine['status'], 'stats':engine['statistics'],
                    'started_at':self.managed_started_at,'elapsed_seconds':elapsed,
                    'capabilities':capabilities,'can_start':not blocked and not self.managed_config_error,
                    'control_available':control_available,'control_error':control_error,'config_error':self.managed_config_error,
                    'blocked_reasons':blocked,'maps':maps,'warehouse_pages':pages,'warehouse_ready':warehouse_ready,
                    'maps_by_difficulty':maps_by_difficulty,'current_difficulty':self.managed_control.get('world_difficulty') or self.managed_control.get('difficulty'),
                    'automatic_start':automatic_start,
                    'difficulties':deepcopy(self.managed_control.get('difficulties',[])),
                    'progression_catalog': deepcopy(growth_catalog()), 'progression_state': deepcopy(self.managed_growth),
                    'history':deepcopy(self.managed_history),'connected':self.client.connected}

    def save_managed(self, raw):
        with self.guard:
            if self.closed.is_set() or self.updating: raise ValidationError('助手正在退出或更新。')
            if getattr(self,'game_transition',False): raise ValidationError('游戏正在启动或退出，请稍候。')
            if self.managed.running or self.operation_kind=='managed': raise ValidationError('请先暂停托管再修改设置。')
            settings = validate_settings(raw,equipment_keys=self.ui['equipment'])
            if settings['merchant']['enabled']:
                shop=self.managed_growth.get('merchant',{})
                if (not self.client.connected or self.managed_growth.get('available') is not True
                        or shop.get('unlock_known') is not True or shop.get('unlocked') is not True):
                    raise ValidationError('神秘商人尚未解锁或解锁状态未确认，不能启用自动购买。')
            self._validate_slot_locks(settings)
            farming = settings['farming']
            if farming['enabled']:
                farming['progression'] = full_campaign_route(self.managed_catalog) if farming['mode']=='push' else []
            from .storage_preferences import organizer_with_shared_pages, validate_shared_pages, write_settings_files
            automation = organizer_with_shared_pages(self.automation_settings,settings['cleanup'])
            validate_shared_pages(automation,settings['cleanup'])
            files = {self.managed_config:settings}
            # A damaged independent config must not be silently replaced by its
            # default switches. It can be repaired through its own save action.
            if not self.automation_config_load_failed: files[self.automation_config] = automation
            write_settings_files(files)
            self.managed.settings = settings
            self.automation_settings = automation
            self.managed_config_load_failed = False
            self.managed_can_resume = False
            self.managed_config_error = ''
            self.revision += 1
            self.journal.append('settings','managed_settings_saved','保存托管模式设置',context={'settings':settings})
            return deepcopy(settings)

    def set_managed_running(self, enabled):
        if type(enabled) is not bool: raise ValidationError('托管开关必须为布尔值。')
        if not enabled:
            return self.stop_managed(pause=True)
        with self.guard:
            if self.closed.is_set() or self.updating: raise ValidationError('助手正在退出或更新。')
            if self.game_actions_blocked(): raise ValidationError('游戏正在启动或退出，请稍候。')
            if self.managed.running: return self.managed_page()
            if self.busy: raise ValidationError('请等待当前操作完成。')
            if not self.client.connected: raise ValidationError('请先连接游戏。')
            page = self.managed_page()
            if not page['can_start']: raise ValidationError('托管入口尚未核验：'+'；'.join(page['blocked_reasons']))
            # Native callbacks and old monitor writers never run concurrently.
            self.monitoring = False; self.automation_running = False; self.loot_state = None
            self.automation_status = '自动整理已暂停，操作交由托管模式。'
            self.busy = True; self.operation_kind = 'managed'
            self.cancelled.clear()
        try:
            observation = self._managed_observation()
            with self.guard:
                if self.cancelled.is_set(): raise ValidationError('托管启动已取消。')
                refreshed = self.managed_page()
                if not refreshed['can_start']:
                    raise ValidationError('托管能力发生变化：'+'；'.join(refreshed['blocked_reasons']))
                if self.managed.settings['farming']['enabled']:
                    validate_settings(self.managed.settings)
                    farming = self.managed.settings['farming']
                    if self.managed_can_resume:
                        targets = [{'dungeon_id':self.managed.target_dungeon,'difficulty':self.managed.target_difficulty}]
                    elif farming['mode']=='push':
                        targets = [highest_unlocked_map(self.managed_catalog,self.managed_control)]
                        observation['farming_start'] = targets[0]
                        farming['progression'] = full_campaign_route(self.managed_catalog)
                    else:
                        targets = [dict(row,difficulty=refreshed['current_difficulty'] if row['difficulty']=='current' else row['difficulty'])
                                   for row in validate_settings(self.managed.settings)['farming']['repeat_maps']]
                    seen = set()
                    for target in targets:
                        difficulty,target_map = target['difficulty'],target['dungeon_id']
                        if difficulty not in FARM_DIFFICULTIES: raise ValidationError('当前游戏难度未确认。')
                        token = (str(target_map),difficulty)
                        if token in seen: raise ValidationError('重复刷图的两张关卡不能相同。')
                        seen.add(token)
                        option = next((row for row in refreshed['difficulties'] if row['id']==difficulty),None)
                        if not option or option.get('unlocked') is not True: raise ValidationError('目标难度尚未解锁。')
                        selected_maps = refreshed['maps_by_difficulty'].get(difficulty,[] if difficulty=='Mythic' else refreshed['maps'])
                        selected = next((m for m in selected_maps if str(m['id'])==str(target_map)),None)
                        if selected is None or selected.get('unlocked') is not True or selected.get('can_enter') is False:
                            raise ValidationError('目标关卡尚未解锁或入场材料不足。')
                if self.managed_can_resume and hasattr(self.managed,'resume'):
                    self.managed_retry = None
                    self.managed_return_recovery = None
                    self.managed.resume(observation)
                else:
                    self.managed_transition = None
                    self.managed_retry = None
                    self.managed_return_recovery = None
                    self.managed.start(observation)
                    self.managed_started_at = datetime.now(timezone.utc).isoformat()
                    self.managed_elapsed = 0
                    self.managed_history = []
                self.managed_started_clock = time.monotonic()
                self.managed_can_resume = False
                self.journal.append('system','managed_started','启动托管模式')
        finally:
            with self.guard:
                self._managed_finish_stop()
                self.busy=False; self.operation_kind=None
        return self.managed_page()

    def stop_managed(self, *, pause=False, reason=None):
        with self.guard:
            was_running = self.managed.running
            self.managed_can_resume = pause and (was_running or self.managed_can_resume)
            if was_running or self.operation_kind=='managed': self.cancelled.set()
            if self.operation_kind=='managed':
                self.managed_stop_request = {'pause':pause,'reason':reason}
                self.managed.status = '正在停止托管，等待已提交动作的执行结果。'
            elif pause and was_running:
                self.managed_transition = None
                self.managed.pause(reason or '托管已暂停。')
            else:
                self.managed_transition = None
                self.managed.stop(reason or '托管已停止。')
            if self.operation_kind!='managed': self._managed_freeze_clock()
            if self.operation_kind!='managed': self.managed_retry = None
            if self.operation_kind!='managed': self.managed_return_recovery = None
        return self.managed_page()

    def finish_managed(self):
        """User stop is graceful; error/disconnect/shutdown use stop_managed."""
        with self.guard:
            if self.closed.is_set() or self.updating: raise ValidationError('助手正在退出或更新。')
            if self.managed_stop_request is not None: raise ValidationError('正在等待当前动作停止，请稍后再试。')
            if not self.managed.running and self.managed.phase != 'paused': return self.managed_page()
            if not self.client.connected: raise ValidationError('游戏已断开，无法回城整理。')
            self.monitoring = False; self.automation_running = False; self.loot_state = None
            self.cancelled.clear()
            self.managed_can_resume = False
            self.managed.request_finish()
            if self.managed_started_clock is None: self.managed_started_clock = time.monotonic()
            self.journal.append('system','managed_finish_requested','主动停止：回城整理后结束托管。')
        return self.managed_page()

    def _managed_freeze_clock(self):
        if self.managed_started_clock is not None:
            self.managed_elapsed += max(0,time.monotonic()-self.managed_started_clock)
            self.managed_started_clock = None

    def _managed_finish_stop(self):
        request,self.managed_stop_request = self.managed_stop_request,None
        if request is not None:
            self.managed_transition = None
            if request['pause']: self.managed.pause(request['reason'] or '托管已暂停。')
            else: self.managed.stop(request['reason'] or '托管已停止。')
        if not self.managed.running:
            self.managed_transition = None
            self.managed_retry = None
            self.managed_return_recovery = None
            self._managed_freeze_clock()

    def reset_managed(self):
        with self.guard:
            if self.managed.running or self.operation_kind=='managed': raise ValidationError('请先停止托管再清空统计。')
            self.managed = ManagedMode(self.managed.settings)
            self.managed_can_resume=False; self.managed_started_at=None; self.managed_started_clock=None
            self.managed_transition=None; self.managed_elapsed=0
            self.managed_retry=None
            self.managed_return_recovery=None
            self.managed_history=[]
        return self.managed_page()

    def _managed_execute(self, action, observation):
        kind,item = action['kind'],action.get('item')
        def active(): return self.managed.running and not self.cancelled.is_set()
        if not active(): raise ValidationError('托管已取消。')
        if self.managed.finishing and kind in ('enter_dungeon','switch_difficulty','merchant_buy','merchant_open','merchant_refresh','allocate_talent','learn_life_skill','set_skill'):
            return {'verified':False,'retryable':True,'reason':'已请求停止，改为回城整理。'}
        # Inventory writes need fresh rule/equip checks. Pickup backends already
        # resolve the drop, verify receiving capacity/identity and confirm the
        # receipt themselves; do not scan campaign/character/rules twice here.
        if kind not in ('collect_ground', 'collect_carriage'):
            observation = self._managed_observation(inventory_only=kind in ('lock','transfer','sell','salvage')
                and action.get('reason_code')!='equip_prepare')
        if observation.get('complete') is not True: raise SnapshotChangedError('物品正在变化，等待重新规划。')
        scene = observation.get('scene',{})
        if scene.get('scene_changing') is True or scene.get('teleporting') is True:
            raise GameBusyError('游戏正在切换场景，等待完成。')
        if scene.get('online_saving') is True:
            raise GameBusyError('等待游戏完成在线同步。')
        if kind in ('sell','salvage') and scene.get('in_town') is not True:
            return {'verified':False,'retryable':True,'reason':'当前场景已变化，回城重新整理后再处置。'}
        pending = scene.get('online_pending_requests')
        if kind in ('return_town','enter_dungeon','switch_difficulty') and type(pending) is int and pending > 0:
            raise GameBusyError(f'等待在线请求处理完成后切换场景（待处理请求 {pending}）。')
        if kind == 'return_town' and scene.get('dead') is not True:
            # A boss can die or another drop can spawn between planning and this
            # fresh read. Re-plan pickup rather than committing a stale return.
            if not self.managed.completion_rewards.ready(observation):
                return {'verified':False, 'retryable':True, 'reason':self.managed.completion_rewards.reason}
            if self.managed.completion_rewards.run_id is not None:
                action['protect_completion_rewards'] = True
        elif kind == 'return_town':
            action.pop('protect_completion_rewards', None)
        stargazing_entry = kind == 'enter_dungeon' and action.get('reason_code') == 'stargazing_skip'
        if action.get('reason_code') == 'stargazing_skip':
            if scene.get('travel_blocked') is True:
                raise GameBusyError(scene.get('travel_wait_reason') or '等待角色恢复可切图状态。')
            current_run = run_record(scene.get('run'))
            verdict = stargazing_decide(observation.get('loot_plan'), observation, self.managed.settings['farming']['stargazing'])
            index = action.get('next_repeat_index')
            target_allowed = type(index) is int and 0 <= index < len(self.managed.repeat_maps)
            if stargazing_entry and target_allowed:
                row = self.managed.repeat_maps[index]
                target_allowed = str(row['dungeon_id']) == str(action.get('dungeon_id')) and row['difficulty'] == action.get('difficulty')
            if (not stargazing_enabled(self.managed.settings) or not target_allowed
                    or action.get('source_run_id') != current_run.get('id')
                    or current_run.get('status') not in ('running', 'settling')
                    or current_run.get('completion_confirmed') is True
                    or verdict['verdict'] != 'skip'
                    or not (combat_rewards_clear(observation) if stargazing_entry else combat_rewards_known(observation))):
                return {'verified':False, 'retryable':True, 'reason':'观星状态或实际奖励已变化，重新核验后再切图。'}
        if kind in ('enter_dungeon','switch_difficulty','merchant_buy','merchant_open','merchant_refresh') and not stargazing_entry and not town_rewards_clear(observation):
            return {'verified':False,'retryable':True,'reason':'主城奖励尚未收取或收纳完成，继续整理后再进入地图。'}
        if self.managed.finishing and kind in ('enter_dungeon','switch_difficulty','merchant_buy','merchant_open','merchant_refresh','allocate_talent','learn_life_skill','set_skill'):
            return {'verified':False,'retryable':True,'reason':'已请求停止，改为回城整理。'}
        if action.get('reason_code') == 'background_organize':
            if (not self.managed.settings['background']['inventory_automation']
                    or action.get('automation_settings') != observation.get('automation_settings')):
                return {'verified':False,'retryable':True,'reason':'自动整理设置已变化，重新规划。'}
        capability = {'enter_dungeon':'travel','return_town':'travel','open_chest':'chests',
                      'switch_difficulty':'difficulty', 'allocate_talent':'talents', 'learn_life_skill':'life_skills',
                      'set_skill':'skills', 'merchant_buy':'merchant', 'merchant_open':'merchant', 'merchant_close':'merchant', 'merchant_refresh':'merchant',
                      'collect_ground':'ground_pickup','collect_carriage':'transfer'}.get(kind,kind)
        if not available_capability(self.managed_page()['capabilities'].get(capability)):
            raise ValidationError('当前托管动作能力尚未核验：'+capability)
        if kind in NEW_ACTIONS:
            state = observation.get('progression', {})
            if state.get('available') is not True: raise ValidationError('成长状态尚未完整读取。')
            if kind in ('allocate_talent', 'learn_life_skill', 'set_skill'):
                planned, _ = growth_action(self.managed.settings, state)
                if not planned or any(action.get(k) != v for k, v in planned.items() if k != 'reason'):
                    return {'verified': False, 'retryable': True, 'reason': '成长目标或角色状态已变化，重新规划。'}
            elif kind == 'merchant_buy':
                if not self.managed.merchant_batch_ready(observation):
                    return {'verified':False, 'retryable':True, 'reason':'先完成新装备的筛选和整理，再继续购买。'}
                planned = next_purchase(self.managed.settings['merchant'], state, self.managed.merchant_counts,
                    max_quantity=min(PURCHASE_BATCH_LIMIT,self.managed._bag_free(observation)))
                if (not self.managed.merchant_pending or not planned
                        or action.get('quantity',1)!=planned.get('quantity',1)
                        or any(action.get(k) != v for k, v in planned.items() if k != 'reason')):
                    return {'verified': False, 'retryable': True, 'reason': '购买比例或商人报价已变化，重新规划。'}
            elif kind == 'merchant_open' and not self.managed.merchant_pending:
                raise ValidationError('尚未触发灵魂碎片消费。')
            elif kind == 'merchant_refresh':
                selected={int(k) for k,v in self.managed.settings['merchant']['weights'].items() if v>0}
                missing=sorted(selected-{r['slot'] for r in state.get('merchant',{}).get('stock',[])})
                if not self.managed.merchant_pending or not missing or action.get('shop_slots')!=missing:
                    return {'verified':False,'retryable':True,'reason':'商人库存已变化，重新读取报价。'}
        if item and kind in ('transfer','lock','equip','sell','salvage','open_chest'):
            current = next((r for r in observation['items'] if identity(r)==identity(item)
                            and r.get('container')==item.get('container')),None)
            if not current: raise SnapshotChangedError('物品已移动或变化。')
            if current.get('slot_index')!=item.get('slot_index') or current.get('count')!=item.get('count'):
                raise SnapshotChangedError('物品格位或数量已变化。')
            item = current
        if kind in ('sell','salvage'):
            # The locking stage is a barrier for the whole current bag, even
            # when a game-side pickup arrived after the plan was generated.
            for candidate in observation['items']:
                if candidate.get('container')!='inventory' or candidate.get('kind')!='equipment': continue
                if (self.managed._reward_pending(candidate)
                        or ((mandatory_storage(candidate)
                             or candidate.get('matches') is True) and candidate.get('locked') is not True)):
                    return {'verified':False,'retryable':True,'reason':'先完成新装备筛选与锁定。'}
        if kind=='transfer':
            if action.get('target')=='storage':
                if (item.get('item_uid') in self.managed_merchant_uids and item.get('kind')=='equipment'
                        and item.get('matches') is False and not self.managed._protected(item)):
                    return {'verified':False,'retryable':True,'reason':'商人未命中装备直接出售或分解，重新规划。'}
                if action.get('reason_code') == 'background_organize':
                    pages = observation['automation_settings'][action['automation_group']]['pages']
                elif item.get('kind') == 'material':
                    group = storage_material_kind(item)
                    if (group is None or action.get('storage_group') != group
                            or self.managed.settings['cleanup'][group+'_action'] != 'store'
                            or not self.managed._safe(item)):
                        return {'verified':False,'retryable':True,'reason':'金条或传说缰绳的身份、品质或收纳设置已变化，重新规划。'}
                    pages = self.managed.settings['cleanup'][group+'_pages']
                else:
                    pages = self.managed.settings['cleanup'][item['kind']+'_pages']
                approved = set(self.managed._targets(observation,pages))
                if not set(action.get('target_indices',[])) <= approved or not action.get('target_indices'):
                    raise ValidationError('目标仓库页不在托管配置范围内。')
        # Resolve one trusted identity against a fresh snapshot before execution.
        snap = self.snapshot
        source = next((r for c in (snap or {}).get('containers',{}).values() for r in c.get('slots',[])
                       if identity(r)==identity(item) and r.get('container')==item.get('container')) ,None) if item else None
        if kind in ('sell','salvage'):
            if reason := disposal_protection(item): raise ValidationError(reason)
            if item.get('kind')=='equipment':
                if not self.managed._disposable(item): raise ValidationError('保留装备不能出售或分解。')
                expected = equipment_policy(self.managed.settings['cleanup'],item,
                    fallback=action.get('reason_code') == 'equipment_fallback')
                if (item.get('item_uid') in self.managed_merchant_uids and expected=='store'):
                    expected=equipment_policy(self.managed.settings['cleanup'],item,fallback=True)
                if kind != expected:
                    return {'verified':False,'retryable':True,'reason':'装备品质或处置策略已变化，重新规划。'}
            elif (kind=='salvage' or item.get('locked') is not False
                    or item.get('kind') not in ('rune','gem')
                    or self.managed.settings['cleanup'][item['kind']+'_action'] != 'sell'):
                raise ValidationError('当前物品不能按此策略处置。')
        if kind in ('transfer','lock','equip','sell','salvage','open_chest'):
            if not source: raise SnapshotChangedError('物品已移动或变化，请重新扫描。')
            if source.get('slot_index')!=item.get('slot_index') or source.get('count')!=item.get('count'):
                raise SnapshotChangedError('物品格位或数量已变化。')
        if kind=='equip' or kind=='transfer' and action.get('reason_code')=='equip_prepare':
            scores = item.get('power',{}).get('comparisons',[])
            current_score = next((s for s in scores if s.get('target')==action.get('target_slot')),None)
            if kind=='transfer':
                current_score = next((s for s in scores if equip_candidate_allowed(item,s,
                    self.managed.settings['auto_equip'],observation.get('character'))),None)
            if not equip_candidate_allowed(item,current_score,self.managed.settings['auto_equip'],
                                           observation.get('character')):
                return {'verified':False,'retryable':True,'reason':'换装比较已变化，重新计算。'}
        if kind=='transfer':
            record('managed_transfer_preflight',source_container=item.get('container'),
                   source_slot=item.get('slot_index'),item_uid=item.get('item_uid'),
                   item_key=item.get('internal_name') or item.get('name_key'),
                   quantity=item.get('count'),target=action['target'],reason_code=action.get('reason_code'))
            result = self.client.move_items([source],action['target'],active,target_indices=action.get('target_indices'))
            result = {**result,'verified':result.get('moved')==1,'quantity':item.get('count',1)}
        elif kind=='lock':
            result = self.client.lock_equipment(source,lambda row:active())
            result = {**result,'verified':result.get('status') in ('locked','already_locked')}
        elif kind=='collect_carriage':
            result = self.client.collect_carriage(item['selection_id'],active)
            result = {**result,'verified':result.get('moved')==1}
        else:
            native_kind = 'enter_map' if kind=='enter_dungeon' else kind
            payload = {**action,'item':source or item}
            if kind=='equip': payload['expected_replaced_uid']=current_score.get('replaced_uid')
            if kind=='enter_dungeon': payload['map_id']=action['dungeon_id']
            if kind=='collect_ground': payload['selection_id']=item['selection_id']
            if kind in ('sell','salvage'):
                payload.update(disposition_authorized=True,rule_matched=item.get('matches') is True,
                               rule_pending=item.get('review') is True or bool(item.get('issues')))
            result = self.client.execute_game_action(native_kind,payload,self.cancelled,validate=active)
            if result.get('transition_requested'):
                started = time.monotonic()
                self.managed_transition = {'action':action,'deadline':started+45,'hard_deadline':started+120,
                                           'started':started,'session':observation.get('session'),
                                           'requested_scene':self._transition_scene(observation['scene'])}
                record('managed_transition_requested',kind=kind,
                       target_map=action.get('dungeon_id'),difficulty=action.get('difficulty'),
                       scene=self.managed_transition['requested_scene'])
            else:
                result = {**result,'verified':result.get('effect_confirmed') is True or result.get('already_in_town') is True,
                          'quantity':result.get('quantity',item.get('count',1) if item else None)}
        return result

    @staticmethod
    def _transition_scene(scene):
        run = run_record(scene.get('run'))
        return {**{k:scene.get(k) for k in ('available','in_town','in_dungeon','dungeon_id',
                    'difficulty','scene_changing','teleporting','dead','completion_visible',
                    'online_saving','online_pending_requests','player_state','gathering','travel_blocked')},
                'run_id':run.get('id'),'run_status':run.get('status')}

    def _managed_defer(self, action, reason, code, *, native_state=None):
        """Only known pre-write gates and explicitly refused single calls retry."""
        with self.guard:
            if not self.managed.running or self.cancelled.is_set(): return self.managed.status
            now = time.monotonic()
            action = action or {}
            key = (code,action.get('kind'))
            old = self.managed_retry
            attempts = old['attempts']+1 if old and old['key']==key else 1
            started = old['started'] if old and old['key']==key else now
            if action.get('kind') == 'return_town' and self.managed_return_recovery:
                started = min(started,self.managed_return_recovery['started'])
            elapsed = now-started
            self.managed.pending = None
            self.error = ''
            if elapsed >= 120:
                self.managed.pause('游戏状态连续 2 分钟未恢复，托管已暂停；统计已保留。'+reason)
                self.managed_can_resume = True
                self.managed_retry = None
                self._managed_freeze_clock()
                record('managed_retry_paused',kind=action.get('kind'),code=code,
                       attempts=attempts,elapsed_seconds=round(elapsed,2),reason=reason)
                return self.managed.status
            delay = min(15,2**min(attempts,4))
            if code == 'stargazing_entry_rejected': delay = min(3, attempts)
            self.managed_retry = {'key':key,'started':started,'attempts':attempts,
                'next_at':now+delay,'action_kind':action.get('kind'),'reason':reason}
            self.managed.status = reason+f' {delay} 秒后重新检查，托管继续运行。'
            record('managed_action_deferred',kind=action.get('kind'),code=code,
                   native_state=native_state,attempts=attempts,delay_seconds=delay,
                   scene=self._transition_scene(self._managed_scene()),reason=reason)
            return self.managed.status

    def _managed_pause_return(self, reason):
        """An unconfirmed town visit never authorizes inventory cleanup."""
        self.managed_transition = None
        self.managed_retry = None
        self.managed_return_recovery = None
        self.managed.pause(reason+' 托管已暂停，统计已保留；继续时会重新读取游戏状态。')
        self.managed_can_resume = True
        self._managed_freeze_clock()
        return self.managed.status

    def _managed_wait_return(self, transition, observation):
        """Recover a cancelled portal only after two explicit idle observations.

        Acceptance is not arrival. Unknown flags or a live portal keep the
        receipt fence; only a stable dungeon with no outstanding native work
        can release it for the planner to reconsider the need to return.
        """
        now = time.monotonic()
        scene = observation['scene']
        action = transition['action']
        if observation.get('session') != transition.get('session'):
            return self._managed_pause_return('游戏连接或角色已改变，未确认原回城结果。')
        recovery = self.managed_return_recovery
        if recovery is None:
            recovery = self.managed_return_recovery = {'started':transition['started'],'interruptions':0}
        pending = scene.get('online_pending_requests')
        active = (scene.get('scene_changing') is True or scene.get('teleporting') is True
            or scene.get('online_saving') is True or type(pending) is int and pending > 0)
        stable = (observation.get('complete') is True and scene.get('available') is True
            and scene.get('in_town') is False and scene.get('in_dungeon') is True
            and all(scene.get(key) is False for key in ('scene_changing','teleporting','online_saving'))
            and type(pending) is int and pending == 0)
        if stable:
            signature = (scene.get('dungeon_id'),scene.get('difficulty'),run_record(scene.get('run')).get('id'))
            if transition.get('idle_signature') != signature:
                transition.update(idle_signature=signature,idle_since=now)
        else:
            transition.pop('idle_signature',None); transition.pop('idle_since',None)
        if now-recovery['started'] >= 120:
            record('managed_return_recovery_paused',scene=self._transition_scene(scene),
                   interruptions=recovery['interruptions'],reason='timeout')
            return self._managed_pause_return('连续 2 分钟未确认回到主城。')
        if (stable and now-transition['started'] >= 8
                and now-transition.get('idle_since',now) >= 2):
            recovery['interruptions'] += 1
            record('managed_return_interrupted',scene=self._transition_scene(scene),
                   requested_scene=transition['requested_scene'],interruptions=recovery['interruptions'])
            if recovery['interruptions'] >= 3:
                return self._managed_pause_return('回城连续 3 次被打断，尚未回到主城。')
            self.managed_transition = None
            self.managed.pending = None
            # The native repeat may already have started a new/high-value run.
            # Do not retain cleanup intent from the interrupted old request.
            self.managed.phase = 'farming'
            return self._managed_defer(action,'回城未完成，已确认传送结束；重新读取奖励与角色状态后规划。',
                                       'return_town_interrupted')
        if now > transition['deadline']:
            if not active and not stable:
                record('managed_transition_timeout',kind='return_town',requested_scene=transition['requested_scene'],
                       scene=self._transition_scene(scene),control_error=self.managed_control.get('reason'))
                return self._managed_pause_return('回城结果与传送状态暂时无法确认，未执行回城整理。')
            if not transition.get('extended'):
                transition['extended'] = True
                record('managed_transition_wait_extended',kind='return_town',scene=self._transition_scene(scene))
        self.managed.status = '等待游戏完成回城；传送或在线同步期间不重复请求。'
        return self.managed.status

    def _managed_once(self):
        self.managed_fast_followup = False
        observation = self._managed_observation()
        with self.guard:
            retry = self.managed_retry
            if retry:
                scene = observation['scene']
                # Native repeat can finish and start a new plan during backoff.
                # Record the short final-wave receipt even when no action is due.
                if (self.managed.settings['farming']['enabled'] and scene.get('available') is True
                        and scene.get('scene_changing') is not True and scene.get('teleporting') is not True):
                    self.managed._runs(observation)
                if retry['action_kind']=='return_town' and scene.get('in_town') is True:
                    self.managed_retry = None
                    self.managed_return_recovery = None
                elif time.monotonic() < retry['next_at']:
                    return self.managed.status
            transition = self.managed_transition
            if transition:
                action = transition['action']
                scene = observation['scene']
                if action['kind']=='return_town' and observation.get('session') != transition.get('session'):
                    return self._managed_pause_return('游戏连接或角色已改变，未确认原回城结果。')
                if action['kind']=='return_town' and self.managed.settings['farming']['enabled']:
                    if (observation.get('complete') is True and scene.get('available') is True and scene.get('scene_changing') is not True
                            and scene.get('teleporting') is not True):
                        self.managed._runs(observation)
                    source = action.get('source_run_id')
                    run = run_record(scene.get('run'))
                    if source and (str(source) in self.managed.handled_runs
                            or scene.get('in_dungeon') is True and run.get('id') and run['id'] != source):
                        transition['skip_cancelled'] = True
                    if not self.managed.running: return self.managed.status
                confirmed = observation.get('complete') is True and scene.get('available') is True and (scene.get('in_town') is True if action['kind']=='return_town'
                     else scene.get('in_dungeon') is True and str(scene.get('dungeon_id'))==str(action['dungeon_id'])
                     and (not action.get('difficulty') or scene.get('difficulty')==action['difficulty'])
                     and run_record(scene.get('run')).get('status') in ('running','settling'))
                if confirmed and action['kind'] == 'enter_dungeon' and action.get('reason_code') == 'stargazing_skip':
                    new_id = run_record(scene.get('run')).get('id')
                    confirmed = bool(new_id and new_id != action.get('source_run_id'))
                if confirmed:
                    self.managed_transition = None
                    if action['kind']=='return_town':
                        self.managed_return_recovery = None
                        if transition.get('skip_cancelled'):
                            # The requested run finished/changed while returning.
                            # Town arrival still authorizes cleanup, not a skip.
                            action = {k:v for k,v in action.items() if k not in
                                      ('reason_code','source_run_id','next_repeat_index')}
                            self.managed.pending = action
                            self.managed.stargazing_return = None
                else:
                    if action['kind']=='return_town':
                        return self._managed_wait_return(transition,observation)
                    now = time.monotonic()
                    pending = scene.get('online_pending_requests')
                    active = (scene.get('scene_changing') is True or scene.get('teleporting') is True
                        or scene.get('online_saving') is True or type(pending) is int and pending > 0)
                    extended = now>transition['deadline'] and active and now<=transition.get('hard_deadline',transition['deadline'])
                    if extended and not transition.get('extended'):
                        transition['extended'] = True
                        record('managed_transition_wait_extended',kind=action['kind'],scene=self._transition_scene(scene))
                    if now>transition['deadline'] and not extended:
                        last_scene = self._transition_scene(scene)
                        record('managed_transition_timeout',kind=action['kind'],
                               target_map=action.get('dungeon_id'),difficulty=action.get('difficulty'),
                               requested_scene=transition.get('requested_scene'),scene=last_scene,
                               control_error=self.managed_control.get('reason'))
                        label = '回城' if action['kind']=='return_town' else '进入关卡'
                        location = ('主城' if scene.get('in_town') is True else
                            '关卡 '+str(scene.get('dungeon_id') or '未知') if scene.get('in_dungeon') is True else '场景不可读')
                        raise ValidationError('关卡切换结果未确认（请求'+label+'；当前'+location+'），已停止托管。')
                    self.managed.status='等待游戏完成场景切换。'
                    return self.managed.status
            else:
                action = self.managed.next_action(observation)
                if self.managed.phase=='paused': self.managed_can_resume=True
                if action and action['kind']=='return_town':
                    now = time.monotonic()
                    if self.managed_return_recovery is None:
                        self.managed_return_recovery = {'started':now,'interruptions':0}
                elif ((action is not None or self.managed.phase != 'returning')
                        and observation.get('complete') is True and observation['scene'].get('available') is True
                        and not any(observation['scene'].get(key) is True
                                    for key in ('scene_changing','teleporting','online_saving'))
                        and not (type(observation['scene'].get('online_pending_requests')) is int
                                 and observation['scene']['online_pending_requests'] > 0)):
                    self.managed_return_recovery = None
                if (self.managed.running and self.managed_return_recovery
                        and time.monotonic()-self.managed_return_recovery['started'] >= 120):
                    return self._managed_pause_return('连续 2 分钟未确认回到主城。')
        if not action: return self.managed.status
        try:
            result = {'verified':True,'scene_confirmed':True} if transition else self._managed_execute(action,observation)
        except GameBusyError as exc:
            return self._managed_defer(action,str(exc),'game_busy')
        except NativeActionError as exc:
            if (not exc.uncertain and exc.state == REJECTED and action['kind'] == 'enter_dungeon'
                    and action.get('reason_code') == 'stargazing_skip'):
                with self.guard:
                    fallback = self.managed.stargazing_rejected(action)
                    self.managed_history.append({'time': datetime.now(timezone.utc).isoformat(),
                        'message': self.managed.status, 'level': 'info'})
                record('managed_stargazing_entry_rejected', source_run=action['source_run_id'], target_map=action['dungeon_id'], fallback_to_town=fallback)
                if not fallback:
                    return self._managed_defer(action, self.managed.status, 'stargazing_entry_rejected', native_state=exc.state)
                self.managed_retry = None
                return self.managed.status
            # GUARD_FAILED is emitted before the mailbox invokes any method.
            # Other rejections may follow partial batches or spend resources;
            # The explicit single-call stargazing entry is handled above;
            # otherwise only the one-call town return can re-plan a rejection.
            if not exc.uncertain and (exc.state==GUARD_FAILED
                    or action['kind']=='return_town' and exc.state==REJECTED):
                code = 'native_guard' if exc.state==GUARD_FAILED else 'return_town_rejected'
                reason = ('物品或角色状态变化，重新读取后规划。' if exc.state==GUARD_FAILED
                    else '游戏暂未接受回城请求，等待结算或同步完成。')
                return self._managed_defer(action,reason,code,native_state=exc.state)
            raise
        receipt = result.pop('_receipt_snapshot',None)
        with self.guard:
            if result.get('transition_requested'):
                self.managed_retry = None
                self.managed.status = '已请求场景切换，等待游戏确认。'
                return self.managed.status
            if result.get('retryable'):
                self.managed.pending = None
                self.managed.status = result.get('reason','等待下一轮读取。')
                return self.managed.status
            self.managed_retry = None
            # A cancelled in-flight action may already have completed. Log its
            # receipt but never resume the planner or submit another action.
            if self.managed.pending:
                self.managed.complete(action['action_id'],result=result)
            if action['kind']=='merchant_buy' and result.get('verified') is True:
                self.managed_merchant_uids.update(uid for uid in (result.get('received_uids') or [result.get('received_uid')])
                                                  if isinstance(uid,str) and uid)
            message = action.get('reason','托管动作')+(' 已确认。' if result.get('verified') else ' 未确认，已停止。')
            self.managed_history.append({'time':datetime.now(timezone.utc).isoformat(),'message':message,
                                         'level':'info' if result.get('verified') else 'error'})
            self.managed_history = self.managed_history[-100:]
            self.journal.append('system','managed_action',message,context={'action':action,'result':result})
        inventory_action = action['kind'] in ('collect_ground','collect_carriage','lock','transfer','sell','salvage')
        use_receipt = (isinstance(receipt,dict) and receipt.get('complete') is True
                       and receipt.get('bridge_session') == observation.get('session'))
        self._publish(receipt if use_receipt else self._managed_snapshot(), refresh_character=not inventory_action)
        self.managed_fast_followup = (result.get('verified') is True and action['kind'] in
            ('collect_ground','collect_carriage','lock','transfer','sell','salvage','merchant_open','merchant_close','merchant_buy','merchant_refresh'))
        return message
