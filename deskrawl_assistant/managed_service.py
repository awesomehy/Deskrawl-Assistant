"""Application bridge for the serial planner; no startup-time game operations."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import time

from .managed_mode import (ManagedMode, DEFAULT_SETTINGS, validate_settings, identity,
    run_record, run_entry_confirmed, DIFFICULTIES, FARM_DIFFICULTIES, equip_candidate_allowed, equipment_actions, equipment_policy, storage_material_kind, item_kind,
    managed_required_storage, managed_town_rewards_clear, farming_enabled)
from .world_boss import PUMPKIN, pumpkin_status
from .models import ValidationError
from .storage_preferences import available_storage_pages, validate_available_storage_pages
from .rules import evaluate, DEFAULT_MIN_CONFIDENCE
from .native_memory import SnapshotChangedError, GameBusyError, DeathRecoveryBusyError, OnlineSyncBusyError
from .native_dispatcher import NativeActionError, GUARD_FAILED, REJECTED
from .paths import RESOURCE_ROOT
from .stargazing import enabled as stargazing_enabled, decide as stargazing_decide, combat_rewards_clear, combat_rewards_known
from .loot_preview import decorate_equipment
from .reconnect import ReconnectFlow
from .item_protection import disposal_protection, mandatory_storage, is_minion_equipment
from .action_log import record
from .progression import catalog as growth_catalog, growth_action, next_purchase, NEW_ACTIONS, PURCHASE_BATCH_LIMIT, enabled as growth_enabled
from .auction import next_action as auction_action, validate_settings as validate_auction
from .auction_runtime import auction_eligibility
from .carriage_transfer import CarriageUnavailable

SYNTHESIS_ACTIONS = ('synthesize_gems', 'synthesize_runes')


def _synthesis_action_key(action):
    """Compare recipe and material identities without transient view fields."""
    inputs = action.get('inputs', [])
    def material(row):
        return (identity(row), row.get('container'), row.get('slot_index'),
                row.get('internal_name') or row.get('name_key'), row.get('count'), row.get('quantity'))
    return {**{key: deepcopy(action.get(key)) for key in ('kind', 'recipe_id', 'recipe', 'input_keys',
             'input_uids', 'count', 'max_quality', 'excluded_keys', 'target', 'target_indices', 'reason_code', 'synthesis_group')},
            'inputs': [material(row) for row in inputs],
            'item': material(action['item']) if action.get('item') else None}


def required_capabilities(settings):
    required = {'transfer'}
    synthesis_enabled = False
    if settings.get('auction', {}).get('upload_enabled'):
        required.update(('auction_upload', 'lock', 'chests', 'ground_pickup'))
    if settings['auto_equip']['enabled']: required.add('equip')
    if settings['background']['rule_monitoring']: required.add('lock')
    if settings.get('progression', {}).get('talents_enabled'): required.add('talents')
    if settings.get('progression', {}).get('skills_enabled'): required.add('skills')
    if settings.get('life_skills', {}).get('enabled'): required.add('life_skills')
    for group, capability in (('gems', 'gem_synthesis'), ('runes', 'rune_synthesis')):
        if settings.get('synthesis', {}).get(group, {}).get('enabled'):
            required.add(capability)
            synthesis_enabled = True
    if settings.get('merchant', {}).get('enabled'): required.update(('merchant', 'travel', 'lock', 'chests', 'ground_pickup'))
    if farming_enabled(settings) or settings.get('merchant', {}).get('enabled'):
        required.update(('lock','travel','chests','ground_pickup'))
        if settings['farming']['enabled'] and (settings['farming']['mode']=='push' or settings['farming']['difficulty']!='current'
                or any(row['difficulty']!='current' for row in settings['farming'].get('repeat_maps',[]))):
            required.add('difficulty')
        if settings.get('pumpkin', {}).get('enabled') and settings['pumpkin']['difficulty'] != 'current':
            required.add('difficulty')
    if (farming_enabled(settings) or settings.get('merchant', {}).get('enabled') or synthesis_enabled
            or settings.get('auction', {}).get('upload_enabled')):
        required.update(('lock', 'chests', 'ground_pickup'))
        required.update(equipment_actions(settings['cleanup']))
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
        self.managed_control_read_at = None
        self.managed_growth = {'available': False, 'reason': '连接游戏后读取成长状态。'}
        self.managed_auction = {}
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
        # Keep submitted craft receipts across user pause/stop, independently
        # of planner resets. They must settle before any later inventory work.
        self.managed_synthesis_wait = None
        self.managed_reconnect = ReconnectFlow()
        self._managed_reconnect_read_since = None
        self.managed_elapsed = 0
        self.managed_config_error = ''
        self.managed_config_load_failed = False
        loaded = False
        self.managed_catalog = json.loads((RESOURCE_ROOT/'data/recommendation-catalog.json').read_text(encoding='utf-8'))['maps']
        try:
            if self.managed_config.exists():
                settings = validate_settings(json.loads(self.managed_config.read_text(encoding='utf-8')),
                                             equipment_keys=self.ui['equipment'],allow_empty_storage_pages=True)
                # The entry is retired. Keep reward preferences, but never
                # let a hidden legacy switch continue abandoning runs.
                settings['farming']['stargazing']['enabled'] = False
                self._validate_slot_locks(settings)
                loaded = True
                missing_pages = [label for group, label in (('equipment', '装备'), ('rune', '符文'), ('gem', '宝石'),
                                 ('gold_bar', '金条'), ('legendary_rein', '传说缰绳'), ('minion_equipment', '仆从装备'))
                                 if not settings['cleanup'][group+'_pages']]
                if missing_pages:
                    # Keep every old preference editable; never silently replace
                    # a sale-policy configuration with the default preset.
                    self.managed.settings = settings
                    self.managed_config_error = '请补选'+ '、'.join(missing_pages)+'存放页，用于托管强制收纳；其他设置与原文件已保留。'
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
        with self.guard:
            self.managed_control_read_at = None
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
        read_at = time.monotonic()
        growth = {'available': False, 'reason': '成长读取入口尚未载入。'}
        if hasattr(self.client, 'progression_snapshot'):
            # Price previews are read-only and independent of enabling purchases.
            try: growth = self.client.progression_snapshot(merchant=True)
            except SnapshotChangedError: raise
            except Exception as exc: growth = {'available': False, 'reason': str(exc)}
        with self.guard:
            self.managed_growth = deepcopy(growth)
            self.managed_control = deepcopy(value)
            self.managed_control_read_at = read_at
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
                    'player_state','gathering','travel_blocked','travel_wait_reason','maps_by_difficulty'):
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

    def _managed_synthesis_state(self, scene):
        """Optional native data never becomes a dependency of normal farming."""
        config = self.managed.settings.get('synthesis', {})
        enabled = {group: config.get(group, {}).get('enabled') is True for group in ('gems', 'runes')}
        if scene.get('in_town') is not True or not any(enabled.values()): return {}
        reader = getattr(self.client, 'synthesis_snapshot', None)
        try:
            if not callable(reader): raise ValidationError('当前连接不支持自动合成读取。')
            value = reader(**enabled)
            if not isinstance(value, dict): raise ValidationError('合成状态格式未确认。')
        except (OSError, RuntimeError, ValueError, KeyError, AttributeError) as exc:
            value = {group: {'available': False, 'reason': str(exc)} for group in enabled if enabled[group]}
        return {group: deepcopy(value.get(group, {'available': False, 'reason': '合成状态尚未完整读取。'}))
                for group in enabled if enabled[group]}

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
        market_catalog = self._market_catalog() if self.managed.settings['auction']['upload_enabled'] else None
        for item in payload['items']:
            source = raw.get((item['container'],item['slot_index']),{})
            item['issues'] = list(source.get('issues',[]))
            item['item_type'] = source.get('item_type')
            item['item_class'] = source.get('item_class')
            item['slot_locked'] = source.get('slot_locked')
            item['identity_verified'] = source.get('identity_verified')
            item['base_rarity'] = source.get('base_rarity')
            item['bound'] = source.get('bound')
            item['is_equipped'] = source.get('is_equipped')
            evidence = source.get('read_evidence', {})
            item['auction_identity_verified'] = (source.get('identity_verified') is True
                or source.get('identity_verified') is not False and evidence.get('snapshot_consistent') is True
                and bool(source.get('item_uid')) and bool(source.get('instance_id')))
            item['auction_candidate'] = bool(market_catalog and market_catalog.resolve(source)[0]
                and auction_eligibility({**source, 'auction_identity_verified': item['auction_identity_verified']},
                    preliminary=True)['eligible'])
            item['equip_slot'] = source.get('equip_slot')
            if not item['is_equipment']:
                item['rarity'] = source.get('rarity',source.get('base_rarity'))
            item['rune_level'] = source.get('rune_level')
            item['rune_xp'] = source.get('rune_xp')
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
        # Never promote a cached/display character into write authority. The
        # lightweight inventory context may not contain an actual character ID.
        control = value if inventory_only else self.managed_control
        character_id = control.get('character_id') if control.get('available') is True else None
        return {'complete':payload['complete'], 'session':snap.get('bridge_session'), 'loot_plan':loot_plan,
                'character_id':character_id,
                'merchant_uids':list(self.managed_merchant_uids),
                'items':payload['items'],'counts':payload['counts'],'character':deepcopy(self.character),
                'progression':deepcopy(self.managed_growth), 'scene':scene,'carriage':carriage,'ground':ground,
                'automation_settings':deepcopy(self.automation_settings),
                'loot_ledger':deepcopy(self.managed_control.get('loot_ledger', {})),
                'system_log':deepcopy(self.managed_control.get('system_log', {})),
                'gold_ledger':deepcopy(self.managed_control.get('gold_ledger', {})),
                'run_history':deepcopy(control.get('run_history', {})),
                'synthesis':self._managed_synthesis_state(scene),
                'auction':self._managed_auction_state(scene),
                'storage':{'page_size':snap.get('transfer',{}).get('page_size'),
                           'allowed_indices':snap.get('transfer',{}).get('storage_indices',[])}}

    def _managed_available_storage_pages(self):
        return available_storage_pages(self.snapshot, self.client.connected)

    def _validate_managed_storage_pages(self, cleanup, groups=None):
        pages = self._managed_available_storage_pages()
        if self.client.connected and pages is None:
            raise ValidationError('回城整理：仓库开放格位尚未读取完整，请刷新物品清单后再保存或启动。')
        if groups is None:
            validate_available_storage_pages(cleanup, pages)
        else:
            validate_available_storage_pages(cleanup, pages, groups)

    def managed_page(self):
        with self.guard:
            engine = self.managed.state()
            # Refresh labels even for statistics gathered before a mapping
            # was available; quantities and stored/sold receipts stay intact.
            for row in engine['statistics']['others'] + engine['statistics']['minion_equipment']:
                name, _ = self._describe_item(row['name_key'])
                if name and name != row['name_key']:
                    row['name'] = name
                if row.get('bucket'):
                    row['icon'] = self.ui['equipment'].get(row['name_key'], {}).get('icon')
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
            if farming_enabled(engine['settings']) and not control_available:
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
            available_pages = self._managed_available_storage_pages()
            warehouse_ready = available_pages is not None
            if self.client.connected:
                try: self._validate_managed_storage_pages(engine['settings']['cleanup'])
                except ValidationError as exc: blocked.append(str(exc))
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
            settings['farming']['stargazing']['enabled'] = False
            catalog = self._market_catalog()
            settings['auction'] = validate_auction(settings['auction'], catalog.items if catalog else ())
            self._validate_managed_storage_pages(settings['cleanup'])
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
                if (self.managed.settings['reconnect']['enabled']
                        and not (observation.get('character_id') or '').startswith('1:')):
                    raise ValidationError('启用断线重连前，请先进入在线角色并完整读取角色身份。')
                refreshed = self.managed_page()
                if not refreshed['can_start']:
                    raise ValidationError('托管能力发生变化：'+'；'.join(refreshed['blocked_reasons']))
                if self.managed.settings['farming']['enabled']:
                    validate_settings(self.managed.settings)
                    farming = self.managed.settings['farming']
                    if self.managed_can_resume:
                        target, tier = (self.managed.pumpkin_fallback if self.managed.pumpkin_active else
                                        (self.managed.target_dungeon, self.managed.target_difficulty))
                        targets = [{'dungeon_id':target,'difficulty':tier}]
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
                            raise ValidationError((selected or {}).get('entry_reason') or '目标关卡尚未解锁或入场材料不足。')
                if self.managed_can_resume and hasattr(self.managed,'resume'):
                    self.managed_retry = None
                    self.managed_return_recovery = None
                    self.managed.resume(observation, observe_metrics=self.managed_synthesis_wait is None)
                else:
                    self.managed_transition = None
                    self.managed_retry = None
                    self.managed_return_recovery = None
                    # Account the outstanding transformation before observing
                    # its output as loot, including after a user stop/restart.
                    self.managed.start(observation, observe_metrics=self.managed_synthesis_wait is None)
                    self.managed_started_at = datetime.now(timezone.utc).isoformat()
                    self.managed_elapsed = 0
                    self.managed_history = []
                self.managed_started_clock = time.monotonic()
                self.managed_reconnect = ReconnectFlow()
                self._managed_reconnect_read_since = None
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
            if self.operation_kind!='managed':
                self.managed_reconnect = ReconnectFlow()
                self._managed_reconnect_read_since = None
        return self.managed_page()

    def finish_managed(self):
        """User stop is graceful; error/disconnect/shutdown use stop_managed."""
        with self.guard:
            if self.managed_reconnect.active:
                return self.stop_managed(reason='重连期间已停止托管，不再自动登录游戏。')
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
            self.managed_reconnect = ReconnectFlow()
            self._managed_reconnect_read_since = None
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

    def _managed_identity_ok(self, observation, *, require_character=False):
        if self.managed.check_identity(observation, require_character=require_character): return True
        if self.managed.phase == 'paused':
            self.managed_can_resume = True
            self._managed_freeze_clock()
        return False

    def _managed_execute(self, action, observation):
        kind,item = action['kind'],action.get('item')
        def active():
            return (self.managed.running and not self.cancelled.is_set()
                    and not (self.managed.finishing and (kind in ('auction_open', 'auction_upload')
                        or action.get('reason_code') == 'auction_prepare')))
        if not self.managed.running or self.cancelled.is_set(): raise ValidationError('托管已取消。')
        if self.managed.finishing and (kind in ('enter_dungeon','switch_difficulty','merchant_buy','merchant_open','merchant_refresh','allocate_talent','learn_life_skill','set_skill','auction_upload','auction_open',*SYNTHESIS_ACTIONS)
                or action.get('reason_code') in ('synthesis_prepare', 'auction_prepare')):
            return {'verified':False,'retryable':True,'reason':'已请求停止，改为回城整理。'}
        # Inventory writes need fresh rule/equip checks. Pickup backends already
        # resolve the drop, verify receiving capacity/identity and confirm the
        # receipt themselves; do not scan campaign/character/rules twice here.
        if kind not in ('collect_ground', 'collect_carriage'):
            observation = self._managed_observation(inventory_only=kind in ('lock','transfer','sell','salvage')
                and action.get('reason_code') not in ('equip_prepare', 'synthesis_prepare', 'auction_prepare'))
        if observation.get('complete') is not True: raise SnapshotChangedError('物品正在变化，等待重新规划。')
        scene = observation.get('scene',{})
        if scene.get('scene_changing') is True or scene.get('teleporting') is True:
            raise GameBusyError('游戏正在切换场景，等待完成。')
        if scene.get('online_saving') is True:
            if kind in ('return_town','enter_dungeon','switch_difficulty'):
                raise OnlineSyncBusyError('等待游戏完成在线同步后切换场景。')
            raise GameBusyError('等待游戏完成在线同步。')
        if kind in ('sell','salvage') and scene.get('in_town') is not True:
            return {'verified':False,'retryable':True,'reason':'当前场景已变化，回城重新整理后再处置。'}
        if (kind in SYNTHESIS_ACTIONS or action.get('reason_code') == 'synthesis_prepare'):
            if not self._managed_identity_ok(observation, require_character=True):
                return {'verified':False,'retryable':True,'reason':self.managed.status}
            if scene.get('in_town') is not True:
                return {'verified':False,'retryable':True,'reason':'自动合成只在主城进行，等待回城后重新规划。'}
            from .synthesis import next_action as synthesis_action
            planned = synthesis_action(self.managed.settings, observation)
            if planned is None or _synthesis_action_key(action) != _synthesis_action_key(planned):
                return {'verified':False,'retryable':True,'reason':'合成材料、品质上限或排除名单已变化，重新规划。'}
            action = {**action, **planned}
        if kind in ('auction_upload', 'auction_open') or action.get('reason_code') == 'auction_prepare':
            if not self._managed_identity_ok(observation, require_character=True):
                return {'verified': False, 'retryable': True, 'reason': self.managed.status}
            planned = auction_action(self.managed.settings, observation)
            if (not planned or planned['kind'] != kind
                    or planned.get('item', {}).get('item_uid') != (item or {}).get('item_uid')):
                return {'verified': False, 'retryable': True, 'reason': '拍卖白名单、保留数量或物品状态已变化，重新规划。'}
        pending = scene.get('online_pending_requests')
        if kind == 'enter_dungeon' and action.get('dungeon_id') == PUMPKIN:
            state, reason = pumpkin_status(scene, action.get('difficulty'))
            if state != 'ready':
                return {'verified': False, 'retryable': True, 'reason': reason}
        if kind in ('return_town','enter_dungeon','switch_difficulty') and type(pending) is int and pending > 0:
            raise OnlineSyncBusyError(f'等待在线请求处理完成后切换场景（待处理请求 {pending}）。')
        if kind == 'return_town' and scene.get('dead') is not True:
            # A boss can die or another drop can spawn between planning and this
            # fresh read. Re-plan pickup rather than committing a stale return.
            if not self.managed.completion_rewards.ready(observation):
                return {'verified':False, 'retryable':True, 'reason':self.managed.completion_rewards.reason}
            if self.managed.completion_rewards.run_id is not None:
                action['protect_completion_rewards'] = True
        elif kind == 'return_town':
            action.pop('protect_completion_rewards', None)
        pumpkin_replay = kind == 'enter_dungeon' and action.get('reason_code') == 'pumpkin_replay'
        if pumpkin_replay:
            ready, reason = self.managed.pumpkin_replay_ready(observation)
            if (not ready or action.get('dungeon_id') != PUMPKIN
                    or action.get('difficulty') != scene.get('difficulty')
                    or action.get('source_run_id') != run_record(scene.get('run')).get('id')):
                return {'verified':False, 'retryable':True, 'reason':reason or '南瓜王本局已变化，重新规划。'}
            if not self.managed.completion_rewards.ready(observation):
                return {'verified':False, 'retryable':True, 'reason':self.managed.completion_rewards.reason}
            action['protect_completion_rewards'] = True
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
        if kind in ('enter_dungeon','switch_difficulty','merchant_buy','merchant_open','merchant_refresh') and not (stargazing_entry or pumpkin_replay) and not managed_town_rewards_clear(observation):
            return {'verified':False,'retryable':True,'reason':'主城奖励尚未收取或收纳完成，继续整理后再进入地图。'}
        if self.managed.finishing and (kind in ('enter_dungeon','switch_difficulty','merchant_buy','merchant_open','merchant_refresh','allocate_talent','learn_life_skill','set_skill','auction_upload','auction_open',*SYNTHESIS_ACTIONS)
                or action.get('reason_code') in ('synthesis_prepare', 'auction_prepare')):
            return {'verified':False,'retryable':True,'reason':'已请求停止，改为回城整理。'}
        if action.get('reason_code') == 'background_organize':
            if (not self.managed.settings['background']['inventory_automation']
                    or action.get('automation_settings') != observation.get('automation_settings')):
                return {'verified':False,'retryable':True,'reason':'自动整理设置已变化，重新规划。'}
        capability = {'enter_dungeon':'travel','return_town':'travel','open_chest':'chests',
                      'switch_difficulty':'difficulty', 'allocate_talent':'talents', 'learn_life_skill':'life_skills',
                      'set_skill':'skills', 'merchant_buy':'merchant', 'merchant_open':'merchant', 'merchant_close':'merchant', 'merchant_refresh':'merchant',
                      'synthesize_gems':'gem_synthesis', 'synthesize_runes':'rune_synthesis',
                      'auction_open':'auction_upload',
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
        if item and kind in ('transfer','lock','equip','sell','salvage','open_chest','auction_upload'):
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
                            or not self.managed._safe(item)):
                        return {'verified':False,'retryable':True,'reason':'金条或传说缰绳的身份、品质或收纳设置已变化，重新规划。'}
                    pages = self.managed.settings['cleanup'][group+'_pages']
                elif is_minion_equipment(item):
                    pages = self.managed.settings['cleanup']['minion_equipment_pages']
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
            if item_kind(item) != 'equipment' and managed_required_storage(item):
                raise ValidationError('托管必须收纳符文、宝石、金条与传说缰绳，禁止出售或分解。')
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
        if kind in ('transfer','lock','equip','sell','salvage','open_chest','auction_upload'):
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
        if kind in SYNTHESIS_ACTIONS or action.get('reason_code') == 'synthesis_prepare':
            # Optional recipe reads and replanning can span a character change
            # without reconnecting. Check actual control identity once more at
            # the submission boundary, including UID-less gem withdrawals.
            try: control = self.client.control_snapshot()
            except Exception: control = {'available':False}
            current = {**observation, 'character_id':control.get('character_id')
                       if control.get('available') is True else None}
            if not self._managed_identity_ok(current, require_character=True):
                return {'verified':False,'retryable':True,'reason':self.managed.status}
            if self.managed.finishing:
                return {'verified':False,'retryable':True,'reason':'已请求停止，改为回城整理。'}
            if not active(): raise ValidationError('托管已取消。')
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
            try:
                result = self.client.collect_carriage(item['selection_id'],active)
            except CarriageUnavailable as exc:
                # This typed exception is raised only before collection writes.
                # Discard the stale selection and let the normal backoff obtain
                # a fresh drop/capacity snapshot; postwrite errors stay fatal.
                raise GameBusyError(str(exc)) from exc
            result = {**result,'verified':result.get('moved')==1}
        else:
            native_kind = 'enter_map' if kind=='enter_dungeon' else kind
            payload = {**action,'item':source or item}
            if kind in SYNTHESIS_ACTIONS:
                payload['character_id'] = self.managed.character_id
            auction_intent = None
            if kind == 'auction_upload':
                coordinator = self._auction_service()
                config = deepcopy(self.managed.settings['auction'])
                auction_state = observation.get('auction', {})
                steamid = auction_state.get('steamid') if auction_state.get('steamid_verified') is True else None
                payload['expected_steamid'] = steamid
                ticket = None
                if config['sell_enabled']:
                    ticket = coordinator.prepare(item, steamid, config)
                    if not ticket:
                        return {'verified': False, 'retryable': True,
                            'reason': '正在后台核验 Steam 库存基线，核验失败将跳过此物品。'}
                if not active() or self.managed.finishing:
                    return {'verified': False, 'retryable': True, 'reason': '托管已结束，不再上传物品。'}
                auction_intent = coordinator.submitted(item, steamid, ticket)
            if kind=='equip': payload['expected_replaced_uid']=current_score.get('replaced_uid')
            if kind=='enter_dungeon': payload['map_id']=action['dungeon_id']
            if kind=='collect_ground': payload['selection_id']=item['selection_id']
            if kind in ('sell','salvage'):
                payload.update(disposition_authorized=True,rule_matched=item.get('matches') is True,
                               rule_pending=item.get('review') is True or bool(item.get('issues')))
            try:
                result = self.client.execute_game_action(native_kind,payload,self.cancelled,validate=active)
            except Exception as exc:
                if kind == 'auction_open':
                    self.auction_refresh_after = time.monotonic() + 60
                    # Preserve diagnostic codes without logging arbitrary
                    # exception text, HTTP payloads or account credentials.
                    record('managed_auction_open_deferred', error_type=type(exc).__name__,
                           state=getattr(exc, 'state', None),
                           last_call=getattr(exc, 'last_call', None),
                           uncertain=getattr(exc, 'uncertain', False))
                    return {'verified': False, 'retryable': True,
                        'reason': '游戏拍卖行列表暂未读取，跳过本轮上传，其他托管继续。'}
                if auction_intent and (getattr(exc, 'mutation_started', None) is False
                        or isinstance(exc, NativeActionError) and exc.state == GUARD_FAILED and not exc.uncertain):
                    coordinator.rejected(auction_intent)
                    return {'verified': False, 'retryable': True,
                        'reason': '游戏尚未提交该物品上传，稍后重新核验，其他托管继续。'}
                raise
            if auction_intent:
                if result.get('effect_confirmed') is True:
                    coordinator.confirmed(auction_intent, result)
                elif result.get('not_submitted') is True:
                    coordinator.rejected(auction_intent)
            if result.get('transition_requested'):
                if kind == 'enter_dungeon':
                    self.managed.expect_run_entry(action, observation)
                started = time.monotonic()
                self.managed_transition = {'action':action,'deadline':started+45,'hard_deadline':started+120,
                                           'started':started,'session':observation.get('session'),
                                           'requested_scene':self._transition_scene(observation['scene'])}
                if kind == 'return_town' and self.managed_return_recovery is None:
                    # A planned action or a busy preflight has not submitted a
                    # portal. Budget only accepted returns, preserving an older
                    # budget when a confirmed interrupted portal is retried.
                    self.managed_return_recovery = {'started':started,'interruptions':0}
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
                'run_id':run.get('id'),'run_status':run.get('status'),
                **{key:run.get(key) for key in ('finished','final_kill_settled','wave_index','wave_count',
                    'wave_active','enemy_count','completion_confirmed','completion_evidence','retained_terminal')}}

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
            death_wait = (code == 'death_recovery_pending' and action.get('kind') == 'return_town'
                          and self.managed_transition is None and self.managed_return_recovery is None)
            # An unsubmitted travel request is safe to keep waiting for. Never
            # extend the deadline for an accepted or interrupted transition.
            online_wait = (code == 'online_sync_pending'
                           and action.get('kind') in ('return_town','enter_dungeon','switch_difficulty')
                           and self.managed_transition is None and self.managed_return_recovery is None)
            self.managed.pending = None
            self.error = ''
            if elapsed >= 120 and not (death_wait or online_wait):
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
            if online_wait and elapsed >= 120:
                self.managed.status = reason+f' 已等待 {int(elapsed)} 秒，尚未提交切换请求；同步恢复后自动继续。'
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
                if self.managed.stargazing_travel_failed(action, scene):
                    self.managed_transition = None
                    self.managed.pending = None
                    self.managed_retry = None
                    self.managed_return_recovery = None
                    record('managed_stargazing_travel_fallback',scene=self._transition_scene(scene),
                           source_run_id=action.get('source_run_id'))
                    return self.managed.status
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

    def _managed_wait_merchant_refresh(self, observation):
        """Poll a submitted, non-spending stock sync; never invoke it again."""
        wait = self.managed.merchant_refresh_wait
        action = wait['action']
        state = observation.get('progression', {})
        scene = observation.get('scene', {})
        balance = state.get('soul_shards', {}).get('current')
        readable = state.get('available') is True
        if readable and (state.get('class_mask') != action['class_mask']
                         or balance != wait['before_shards']):
            self.managed._pause('补货核验期间角色或灵魂碎片已变化，托管已暂停；未重复提交补货或购买。')
            self.managed_can_resume = True
            self._managed_freeze_clock()
            record('managed_merchant_refresh_changed', before_shards=wait['before_shards'],
                   after_shards=balance, class_mask=state.get('class_mask'))
            return
        stable = (observation.get('complete') is True and readable and state.get('busy') is False
                  and state.get('merchant', {}).get('available') is True
                  and scene.get('available') is True and scene.get('in_town') is True
                  and all(scene.get(key) is False for key in ('scene_changing','teleporting','online_saving'))
                  and type(scene.get('online_pending_requests')) is int and scene['online_pending_requests'] == 0)
        stock = sorted(r['slot'] for r in state.get('merchant', {}).get('stock', []))
        elapsed = time.monotonic() - wait['started']
        if stable and set(action['shop_slots']) <= set(stock):
            self.managed.merchant_refresh_wait = None
            # Pause/resume can clear pending; the original receipt remains the
            # only allowed action until it has been checked against fresh data.
            self.managed.pending = action
            self.managed.complete(action['action_id'], result={'verified':True})
            self.managed.status = '神秘商人库存已补充，继续托管流程。'
            record('managed_merchant_refresh_confirmed', shop_slots=action['shop_slots'],
                   stock_slots=stock, elapsed_seconds=round(elapsed, 2))
        elif elapsed >= 120:
            if stable:
                # Stock remaining empty is not an uncertain purchase. Finish
                # screening gear already bought, and wait for stock recovery
                # before another shopping round. Never fabricate a receipt.
                self.managed.merchant_refresh_wait = None
                self.managed.pending = self.managed.await_effect = None
                self.managed.merchant_refresh_deferred = True
                self.managed.merchant_pending = False
                self.managed.merchant_cleanup_pending = True
                self.managed.merchant_last_balance = balance
                self.managed.merchant_last_capacity = state['soul_shards']['maximum']
                self.managed.status = '商人库存暂未补充，跳过本轮消费；整理已购装备后继续托管。'
                record('managed_merchant_refresh_deferred', shop_slots=action['shop_slots'],
                       stock_slots=stock, elapsed_seconds=round(elapsed, 2))
            else:
                self.managed._pause('神秘商人补货后游戏状态持续未稳定，托管已暂停；未重复提交补货或购买。')
                self.managed_can_resume = True
                self._managed_freeze_clock()
                record('managed_merchant_refresh_paused', stock_slots=stock,
                       elapsed_seconds=round(elapsed, 2), scene=self._transition_scene(scene))
        else:
            self.managed.status = '等待神秘商人同步补充库存，未重复提交补货或购买。'

    def _managed_wait_synthesis(self, observation):
        wait = self.managed_synthesis_wait
        with self.guard:
            if not self.managed.running or self.cancelled.is_set():
                return self.managed.status
            character = self.managed._control_character_id(observation)
            if (observation.get('session') != wait['session']
                    or character != wait['character_id']):
                return self._managed_pause_synthesis('合成核验期间连接或角色已变化，请先核验原角色的合成结果。')
            if time.monotonic() < wait.get('next_at', 0):
                return self.managed.status
        result = wait.get('confirmed')
        if result is None:
            result = self.client.reconcile_synthesis(character, cancel_event=self.cancelled,
                validate=lambda: self.managed.running and not self.cancelled.is_set())
        with self.guard:
            if wait['stage'] == 'receipt' and (not isinstance(result, dict)
                    or not (result.get('synthesis_pending') or result.get('effect_confirmed'))):
                return self._managed_pause_synthesis('原合成核验记录未完整读取，未重复提交合成。')
            if isinstance(result, dict) and result.get('effect_confirmed'):
                wait['confirmed'] = result
            if not self.managed.running or self.cancelled.is_set():
                return self.managed.status
            pending = isinstance(result, dict) and (result.get('synthesis_pending')
                or wait['stage'] == 'cleanup' and result.get('ui_cleanup_pending'))
            if pending:
                wait['next_at'] = time.monotonic() + 2
                if result.get('timed_out') or time.monotonic() - wait['started'] >= 120:
                    reason = ('合成已提交，连续 2 分钟未确认材料与产物；未重复消耗。'
                              if wait['stage'] == 'receipt' else '合成结果已确认，但魔盒清理持续未完成；未再次合成。')
                    return self._managed_pause_synthesis(reason)
                self.managed.status = ('合成已提交，等待材料与产物回执；未重复消耗材料。'
                    if wait['stage'] == 'receipt' else '合成结果已确认，等待魔盒完成清理后继续托管。')
                return self.managed.status
            if wait['stage'] == 'cleanup':
                self.managed_synthesis_wait = None
                self.managed.status = '魔盒清理已完成，继续托管。'
                record('managed_synthesis_cleanup_confirmed', kind=wait['action']['kind'])
                return self.managed.status
            action = wait['action']
            if result.get('ui_cleanup_pending'):
                wait.update(stage='cleanup', confirmed=None, started=time.monotonic(), next_at=0)
            else:
                self.managed_synthesis_wait = None
            self.managed.pending = action
            result = dict(result, verified=True)
            record('managed_synthesis_receipt_recovered', kind=action['kind'], receipt_id=result.get('receipt_id'))
        return self._managed_complete_action(action, result, observation)

    def _managed_pause_synthesis(self, reason):
        # Preserve the pending receipt/cleanup and statistics. Resume polls the
        # same operation even if its feature was switched off in the meantime.
        self.managed._pause(reason + ' 托管已暂停；继续时会先核验原操作。')
        self.managed_can_resume = True
        self._managed_freeze_clock()
        record('managed_synthesis_recovery_paused', stage=self.managed_synthesis_wait['stage'])
        return self.managed.status

    def _managed_once(self):
        self.managed_fast_followup = False
        if self._managed_reconnect_once(): return self.managed.status
        observation = self._managed_observation()
        if self.managed_synthesis_wait:
            return self._managed_wait_synthesis(observation)
        with self.guard:
            # The same bridge connection can remain alive across role changes.
            # Reject them before retry/transition paths update counts or receipts.
            if not self._managed_identity_ok(observation): return self.managed.status
            # Run completion is independent of full inventory and portal waits.
            # In particular, native repeat can replace the winning plan during
            # a backoff or an already submitted stargazing transition.
            self.managed.observe_runs(observation)
            if not self.managed.running: return self.managed.status
            if self.managed.merchant_refresh_wait:
                self._managed_wait_merchant_refresh(observation)
                return self.managed.status
            retry = self.managed_retry
            if retry:
                scene = observation['scene']
                if retry['action_kind']=='return_town' and scene.get('in_town') is True:
                    self.managed_retry = None
                    self.managed_return_recovery = None
                elif (retry['key'][0] == 'online_sync_pending'
                        and self.managed_transition is None and self.managed_return_recovery is None
                        and observation.get('complete') is True and scene.get('available') is True
                        and all(scene.get(key) is False for key in ('scene_changing','teleporting','online_saving'))
                        and type(scene.get('online_pending_requests')) is int
                        and scene['online_pending_requests'] == 0):
                    self.managed_retry = None
                    record('managed_online_sync_resumed',scene=self._transition_scene(scene),
                           elapsed_seconds=round(time.monotonic()-retry['started'],2))
                elif (retry['key'][0] == 'death_recovery_pending'
                        and self.managed_transition is None and self.managed_return_recovery is None
                        and observation.get('complete') is True and scene.get('available') is True
                        and scene.get('dead') is False
                        and all(scene.get(key) is False for key in ('scene_changing','teleporting','online_saving'))
                        and type(scene.get('online_pending_requests')) is int
                        and scene['online_pending_requests'] == 0):
                    # Auto-revive/native repeat may have replaced the dead run.
                    # Discard the unsubmitted intent and let the planner check
                    # merchant, finish, map and reward conditions afresh.
                    self.managed_retry = None
                    if self.managed.phase == 'returning': self.managed.phase = 'farming'
                    record('managed_death_recovery_resumed',scene=self._transition_scene(scene))
                elif time.monotonic() < retry['next_at']:
                    return self.managed.status
            transition = self.managed_transition
            if transition:
                action = transition['action']
                scene = observation['scene']
                if action['kind']=='return_town' and observation.get('session') != transition.get('session'):
                    return self._managed_pause_return('游戏连接或角色已改变，未确认原回城结果。')
                if action['kind']=='return_town' and farming_enabled(self.managed.settings):
                    source = action.get('source_run_id')
                    run = run_record(scene.get('run'))
                    if source and (str(source) in self.managed.handled_runs
                            or scene.get('in_dungeon') is True and run.get('id') and run['id'] != source):
                        transition['skip_cancelled'] = True
                    if not self.managed.running: return self.managed.status
                confirmed = observation.get('complete') is True and scene.get('available') is True and (scene.get('in_town') is True if action['kind']=='return_town'
                     else run_entry_confirmed(action, scene))
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
                if ((not action or action['kind'] != 'return_town')
                        and (action is not None or self.managed.phase != 'returning')
                        and observation.get('complete') is True and observation['scene'].get('available') is True
                        and not any(observation['scene'].get(key) is True
                                    for key in ('scene_changing','teleporting','online_saving'))
                        and not (type(observation['scene'].get('online_pending_requests')) is int
                                 and observation['scene']['online_pending_requests'] > 0)):
                    self.managed_return_recovery = None
                if (self.managed.running and self.managed_return_recovery
                        and time.monotonic()-self.managed_return_recovery['started'] >= 120):
                    return self._managed_pause_return('连续 2 分钟未确认回到主城。')
                retry = self.managed_retry
                if not action and retry and retry['key'][0] == 'death_recovery_pending':
                    scene = self._transition_scene(observation['scene'])
                    wait = (observation.get('complete'), scene, self.managed.status)
                    if retry.get('wait_state') != wait:
                        retry['wait_state'] = wait
                        record('managed_death_recovery_wait',complete=observation.get('complete'),
                               scene=scene,reason=self.managed.status,
                               control_error=self.managed_control.get('reason'))
        if not action: return self.managed.status
        try:
            result = {'verified':True,'scene_confirmed':True} if transition else self._managed_execute(action,observation)
        except DeathRecoveryBusyError as exc:
            return self._managed_defer(action,str(exc),'death_recovery_pending')
        except OnlineSyncBusyError as exc:
            return self._managed_defer(action,str(exc),'online_sync_pending')
        except GameBusyError as exc:
            if action['kind'] == 'return_town' and self._managed_scene().get('dead') is True:
                # Scene/sync gates may precede the DeathUI check. The fresh
                # confirmed death is still a preflight wait, not a submission.
                return self._managed_defer(action,str(exc),'death_recovery_pending')
            return self._managed_defer(action,str(exc),'game_busy')
        except NativeActionError as exc:
            if (action['kind'] == 'equip' and exc.state == REJECTED
                    and getattr(exc, 'mutation_started', None) is False):
                # Equipment's native qualification checks are read-only. A
                # rejection there has not swapped anything; after the first
                # write this exception must keep the normal uncertain fence.
                return self._managed_defer(action,
                    '游戏暂未接受换装资格，重新读取装备条件和角色状态后规划。',
                    'equip_precheck_rejected', native_state=exc.state)
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
        if action['kind'] in SYNTHESIS_ACTIONS and (result.get('synthesis_pending')
                or result.get('effect_confirmed') and result.get('ui_cleanup_pending')):
            with self.guard:
                self.managed_synthesis_wait = {'action':deepcopy(action),
                    'session':observation.get('session'),
                    'character_id':self.managed._control_character_id(observation),
                    'stage':'receipt' if result.get('synthesis_pending') else 'cleanup',
                    'started':time.monotonic(), 'next_at':0}
                if result.get('synthesis_pending'):
                    self.managed_retry = None
                    self.managed.status = '合成已提交，等待材料与产物回执；未重复消耗材料。'
                    record('managed_synthesis_receipt_pending',kind=action['kind'])
                    return self.managed.status
        with self.guard:
            if action['kind'] == 'merchant_refresh' and result.get('merchant_refresh_pending') is True:
                if (self.managed.running and not self.cancelled.is_set() and self.managed.pending
                        and self.managed.pending['action_id'] == action['action_id']):
                    self.managed_retry = None
                    self.managed.merchant_refresh_wait = {'action':deepcopy(action),
                        'before_shards':result['before_shards'], 'started':time.monotonic()}
                    self.managed.status = '等待神秘商人同步补充库存，未重复提交补货或购买。'
                    record('managed_merchant_refresh_pending', shop_slots=action['shop_slots'],
                           before_shards=result['before_shards'])
                return self.managed.status
            if result.get('transition_requested'):
                self.managed_retry = None
                self.managed.status = '已请求场景切换，等待游戏确认。'
                return self.managed.status
            if result.get('retryable'):
                self.managed.pending = None
                self.managed.status = result.get('reason','等待下一轮读取。')
                return self.managed.status
        return self._managed_complete_action(action, result, observation, transition=transition)

    def _managed_complete_action(self, action, result, observation, *, transition=None):
        receipt = result.pop('_receipt_snapshot',None)
        with self.guard:
            self.managed_retry = None
            # A cancelled in-flight action may already have completed. Log its
            # receipt but never resume the planner or submit another action.
            if self.managed.pending:
                self.managed.complete(action['action_id'],result=result)
                if transition and action['kind'] == 'enter_dungeon' and result.get('verified') is True:
                    # Accepted combat travel can arrive after the destination
                    # has already completed. Its quota is selected by complete;
                    # consume this same read now, before native repeat hides it.
                    self.managed.observe_runs(observation)
            if action['kind']=='merchant_buy' and result.get('verified') is True:
                self.managed_merchant_uids.update(uid for uid in (result.get('received_uids') or [result.get('received_uid')])
                                                  if isinstance(uid,str) and uid)
            message = action.get('reason','托管动作')+(' 已确认。' if result.get('verified') else ' 未确认，已停止。')
            self.managed_history.append({'time':datetime.now(timezone.utc).isoformat(),'message':message,
                                         'level':'info' if result.get('verified') else 'error'})
            self.managed_history = self.managed_history[-100:]
            self.journal.append('system','managed_action',message,context={'action':action,'result':result})
        inventory_action = action['kind'] in ('collect_ground','collect_carriage','lock','transfer','sell','salvage',*SYNTHESIS_ACTIONS)
        use_receipt = (isinstance(receipt,dict) and receipt.get('complete') is True
                       and receipt.get('bridge_session') == observation.get('session'))
        self._publish(receipt if use_receipt else self._managed_snapshot(), refresh_character=not inventory_action)
        self.managed_fast_followup = (result.get('verified') is True and action['kind'] in
            ('collect_ground','collect_carriage','lock','transfer','sell','salvage','merchant_open','merchant_close','merchant_buy','merchant_refresh',*SYNTHESIS_ACTIONS))
        return message

    def _managed_pause_reconnect(self, reason):
        # Preserve any acknowledged item/craft receipt fence. Login must never
        # silently turn an uncertain spending operation into a fresh request.
        self.managed._pause(reason + ' 托管已暂停，累计统计已保留。')
        self.managed_can_resume = True
        self._managed_freeze_clock()
        record('managed_reconnect_paused', reason=reason)
        return True

    def _managed_reconnect_once(self):
        """Intercept the menu BEFORE scanning inventory or running travel timers."""
        config = self.managed.settings['reconnect']
        reader = getattr(self.client, 'reconnect_snapshot', None)
        if (not self.managed.running or not config['enabled'] or not callable(reader)
                or self.cancelled.is_set() or not self.client.connected):
            return False
        flow = self.managed_reconnect
        now = time.monotonic()
        try:
            state = reader()
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            with self.guard:
                if not self.managed.running or self.cancelled.is_set(): return True
                since = getattr(self, '_managed_reconnect_read_since', None)
                if since is None:
                    self._managed_reconnect_read_since = now
                    record('managed_reconnect_read_wait', error_type=type(exc).__name__)
                if (flow.active and now - flow.started >= 600
                        or since is not None and now - since >= 120):
                    return self._managed_pause_reconnect('重连状态持续不可读，请检查游戏界面。')
                self.managed.status = '等待重连界面状态稳定，暂不执行物品操作。'
            return True
        self._managed_reconnect_read_since = None
        with self.guard:
            if not self.managed.running or self.cancelled.is_set(): return True
            was_active = flow.active
            decision = flow.decide(state, config, self.managed.character_id, now)
            kind = decision['kind']
            if kind == 'idle': return False
            if self.managed.finishing:
                self.managed.stop('掉线期间已请求结束托管，不再自动登录游戏。')
                return True
            if not was_active:
                record('managed_reconnect_started', character_index=config['character_index'])
            self.managed.phase = 'reconnecting'
            self.managed.status = decision.get('reason', '断线重连中。')
            if kind == 'pause': return self._managed_pause_reconnect(decision['reason'])
            if kind == 'wait': return True
        if kind == 'verify':
            try:
                observation = self._managed_observation()
            except (OSError, RuntimeError, ValueError, KeyError):
                # No planner call, publication from stale data or native writes
                # until all authoritative in-game reads have recovered.
                return True
            with self.guard:
                if not self.managed.running or self.cancelled.is_set(): return True
                if observation.get('complete') is not True or observation.get('scene', {}).get('available') is not True:
                    return True
                if not self._managed_identity_ok(observation, require_character=True): return True
                self.managed_transition = self.managed_retry = self.managed_return_recovery = None
                self.managed.pending = None
                if self.managed.await_effect and self.managed.await_effect['kind'] in (
                        'return_town', 'enter_dungeon', 'merchant_open', 'merchant_close'):
                    self.managed.await_effect = None
                self.managed.await_count = 0
                self.managed.merchant_opened = False
                self.managed._baseline_runs(observation)
                self.managed.phase = 'cleanup' if observation['scene'].get('in_town') is True else 'farming'
                self.managed.reward_barrier = self.managed.phase == 'cleanup'
                self.managed.barrier_reads = 0
                self.managed.status = '断线重连成功，原角色数据已确认，继续本次托管。'
                self.managed_reconnect = ReconnectFlow()
                self.managed_fast_followup = True
                record('managed_reconnect_resumed', character_index=config['character_index'])
            return True
        def active():
            return (self.managed.running and not self.managed.finishing and not self.cancelled.is_set()
                    and not self.closed.is_set() and self.managed_stop_request is None)
        if not active(): return True
        try:
            result = self.client.execute_reconnect_action(kind, config['character_index'], self.managed.character_id,
                validate=active, cancel_event=self.cancelled)
        except NativeActionError as exc:
            with self.guard:
                if exc.uncertain or exc.mutation_started is True:
                    return self._managed_pause_reconnect('重连动作结果未确认，已停止重复提交，请检查游戏界面。')
                flow.next_at = now + 5
                self.managed.status = '重连界面或按钮状态变化，稍后重新读取。'
            return True
        with self.guard:
            if result.get('submitted') is True:
                flow.submitted(kind, time.monotonic())
                record('managed_reconnect_submitted', kind=kind, character_index=config['character_index'])
        return True
