"""Opt-in, local-only evidence capture for high-value planned/observed loot.

This is not the activity journal: server promises do not count as settled loot.
No game action or upload is performed. Polling can miss transient drops.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import threading
import uuid

from .item_protection import mandatory_storage
from .loot_plan_reader import describe_item, SLOT_LABELS, _labels
from .paths import RESOURCE_ROOT
from .steam_market import MarketCatalog


def now():
    return datetime.now(timezone.utc).isoformat()


def plan_hash(raw):
    return hashlib.sha256(raw['runId'].encode()).hexdigest()


@lru_cache(maxsize=1)
def _market_catalog():
    # Shipped Steam item definitions, not a quote lookup or network request.
    return MarketCatalog.load(RESOURCE_ROOT / 'data/steam-market-catalog.json')


def high_value_reason(row):
    reason = mandatory_storage(row)
    if reason:
        return reason
    key = row.get('internal_name') or row.get('name_key')
    entry = _market_catalog().items.get(key)
    if not entry:
        return ''
    variant = 'black_mist' if row.get('is_black_mist') is True else 'normal'
    if entry.get('kind') == 'equipment':
        # A base legendary template's black-mist definition must not turn its
        # ordinary legendary variant into a marketable planned drop.
        rarity = row.get('rarity', row.get('base_rarity'))
        expected = 3 if variant == 'black_mist' else entry.get('rarity')
        if type(rarity) is not int or rarity != expected:
            return ''
    if not entry.get(variant):
        return ''
    # Monitoring discovers valuable templates even before instance binding is
    # known. It is not a claim that this particular item can already be listed.
    if key == 'GoldBar':
        return '金条'
    if key == 'UpgradeCommodity1':
        return '开光石'
    if key.startswith('Rein_'):
        return '传说缰绳'
    return '可交易物品'


def _observation_key(row):
    uid = row.get('item_uid')
    if isinstance(uid, str) and uid:
        return ('uid', uid)
    # Ground and carriage share the same LootDrop identity. Inventory slot
    # keys/quantity deltas cannot prove a new drop: sorting, transfers and
    # synthesis also change them. Never pretend this ID is a PlanItem dropId.
    source = row.get('capture_source', row.get('source'))
    identity = row.get('observation_id')
    if source in ('ground', 'carriage') and isinstance(identity, str) and identity:
        return ('drop', identity)
    return None


def _saved_observation_key(row):
    identity = _observation_key(row)
    if identity is None or identity[0] == 'uid':
        return identity
    event = row.get('observation_event_id')
    return ('drop_event', event) if isinstance(event, str) and event else identity


def _item_name(key):
    labels = _labels()
    return (labels['names'].get(key, {}).get('Chinese (Simplified)')
            or labels['items'].get(key, {}).get('name')
            or labels['collectibles'].get(key, {}).get('name') or key)


def planned_hits(raw):
    hits = []
    for wave in raw.get('waves',[]):
        for item in (wave.get('loot') or {}).get('items',[]):
            pretty = describe_item({'base_item_id':item['baseItemId'], 'slot':item['slot'],
                'rarity':item['rarity'], 'level':item['itemLevel'], 'ancient':item['ancient'],
                'black_mist':item['blackMist'], 'modifiers':item['modifiers'], 'sockets':item['sockets'],
                'qty':item['qty'], 'chest_gold':item['chestGold']})
            reason = high_value_reason({**pretty, 'internal_name':item['baseItemId'],
                'is_black_mist':pretty['kind']=='equipment' and item['blackMist'],
                'is_equipment':pretty['kind']=='equipment' or pretty['kind']=='unknown' and item['slot'] in SLOT_LABELS})
            if not reason and pretty['kind']=='unknown' and item['slot'] in SLOT_LABELS and item['blackMist']:
                reason = '黑雾装备'
            if reason:
                hits.append({'wave':wave['wave'], 'drop_id':item['dropId'],
                    'item_key':item['baseItemId'], 'name':pretty['name'], 'category':reason})
    return hits


class LootCaptureMonitor:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.guard = threading.RLock()
        self.enabled = False
        self.message = '监控未开启。'
        self.error = ''
        self.last_saved = None
        self.runs = OrderedDict()
        self.session = None
        self.character = None
        self.seen = set()
        self.new_ids = {}
        self.captured_ids = set()
        self.drop_generations = {}
        self.active_drop_ids = set()
        self.baseline_ready = False
        self.pending = set()
        self.polls = 0

    def status(self):
        with self.guard:
            # Files can be moved or removed while the assistant is running.
            # Count evidence files on each refresh, excluding temporary writes.
            try:
                saved_runs = sum(path.is_file() for path in self.directory.glob('*.json'))
                error = self.error
            except OSError:
                saved_runs = None
                error = self.error or '留档目录暂时无法读取，文件数量未确认。'
            return {'enabled':self.enabled, 'message':self.message, 'error':error,
                'saved_runs':saved_runs, 'last_saved':self.last_saved,
                'directory':str(self.directory.resolve()), 'polls':self.polls}

    def set_enabled(self, enabled):
        if type(enabled) is not bool: raise ValueError('高价值掉落监控开关必须为布尔值。')
        with self.guard:
            if enabled and not self.enabled:
                self.runs.clear()
                self.session = None
                self.character = None
                self.seen.clear()
                self.new_ids.clear()
                self.captured_ids.clear()
                self.drop_generations.clear()
                self.active_drop_ids.clear()
                self.pending.clear()
                self.baseline_ready = False
            self.enabled = enabled
            self.error = ''
            self.message = '监控已开启，等待稳定的在线关卡与物品清单。' if enabled else '监控已关闭，已保存文件仍保留。'
        return self.status()

    def waiting(self, message):
        with self.guard:
            if self.enabled: self.message = message

    def failed(self):
        with self.guard:
            if self.enabled:
                self.error = '本次监控读取或保存失败，将自动重试；其他功能继续运行。'

    def _restore(self, key, entry):
        """Re-enabling/revisiting a run must not overwrite earlier evidence."""
        target = self.directory/(key+'.json')
        if not target.exists(): return entry
        if target.stat().st_size > 32*1024*1024:
            raise ValueError('已有掉落留档超出读取上限。')
        saved = json.loads(target.read_text(encoding='utf-8'))
        if (not isinstance(saved,dict) or saved.get('schema_version') != 1
                or saved.get('association') != entry['association']
                or not isinstance(saved.get('first_read_at'),str)
                or not isinstance(saved.get('planned_high_value'),list)
                or not isinstance(saved.get('observed_high_value'),list)):
            raise ValueError('已有掉落留档无法核验，保留原文件。')
        first = saved.get('raw_plan')
        if entry['first_plan'] is not None and (not isinstance(first,dict)
                or first.get('runId') != entry['first_plan']['runId']):
            raise ValueError('已有掉落留档场次不一致，保留原文件。')
        observed = saved['observed_high_value']
        if any(not isinstance(row,dict) or not isinstance(row.get('item_uid'),str)
               or _observation_key(row) is None for row in observed):
            raise ValueError('已有掉落观测无法核验，保留原文件。')
        entry.update(first_read_at=saved['first_read_at'], first_read_wave=saved.get('first_read_wave'),
            first_plan=first, latest_plan=saved.get('latest_raw_plan') or first,
            planned_hits=saved['planned_high_value'], observed_hits=observed,
            observed_ids={_saved_observation_key(row) for row in observed}, saved=True)
        if isinstance(saved.get('saved_at'),str):
            self.last_saved = max(self.last_saved or '',saved['saved_at'])
        return entry

    def _save(self, key, entry):
        document = {'schema_version':1, 'source':'external_read_only_RunPlan',
            'capture_scope':'RunPlan context and complete RunWave.loot/PlanItem/PlanModifier fields; not a network packet.',
            'first_read_at':entry['first_read_at'], 'first_read_wave':entry['first_read_wave'],
            'saved_at':now(), 'plan_changed_since_first_read':entry['first_plan'] != entry['latest_plan'],
            'raw_plan':entry['first_plan'], 'latest_raw_plan':entry['latest_plan'] if entry['first_plan'] != entry['latest_plan'] else None,
            'planned_high_value':entry['planned_hits'], 'observed_high_value':entry['observed_hits'],
            'association':entry['association'],
            'note':'计划不等于实际结算；实际观测仅在完整清单基线后记录新增UID或地面／马车掉落观测标识。观测标识不是dropId；无精确UID和dropId匹配时不能证明物品在开局计划中。商人购买单独留档，不关联刷图计划。'}
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory/(key+'.json')
        temporary = self.directory/(key+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('w',encoding='utf-8') as stream:
                json.dump(document,stream,ensure_ascii=False,indent=2,allow_nan=False)
                stream.write('\n')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        entry['saved'] = True
        self.last_saved = document['saved_at']
        self.pending.discard(key)

    def _observation_identities(self, observations, *, combat):
        identities = {identity for row in observations if (identity := _observation_key(row)) is not None}
        drop_ids = {identity[1] for identity in identities if identity[0] == 'drop'}
        # Native LootDrop objects can be recycled for later same-kind drops.
        # Only a complete combat snapshot confirming absence starts a new
        # lifetime. Town unloading can briefly remove a carried object from
        # both lists; this gap does not prove a new drop.
        for drop_id in drop_ids:
            if combat and drop_id not in self.active_drop_ids:
                self.drop_generations[drop_id] = self.drop_generations.get(drop_id, -1) + 1
            else:
                self.drop_generations.setdefault(drop_id, 0)
        if combat:
            self.active_drop_ids = drop_ids
        else:
            self.active_drop_ids.update(drop_ids)
        return {identity:identity if identity[0] == 'uid' else
                (*identity, self.drop_generations[identity[1]]) for identity in identities}

    def feed(self, bundle):
        with self.guard:
            if not self.enabled: return
            session = bundle['session']
            if session != self.session:
                self.runs.clear()
                self.pending.clear()
                self.seen.clear()
                self.new_ids.clear()
                self.captured_ids.clear()
                self.drop_generations.clear()
                self.active_drop_ids.clear()
                self.baseline_ready = False
                self.session = session
                self.character = None
            character = bundle.get('character_key')
            if character and character != self.character:
                # Switching save characters in the same process must establish
                # a new baseline, not call their owned valuables fresh loot.
                self.seen.clear()
                self.new_ids.clear()
                self.captured_ids.clear()
                self.drop_generations.clear()
                self.active_drop_ids.clear()
                self.baseline_ready = False
                self.character = character
            self.polls += 1
            self.error = ''
            raw = bundle.get('raw_plan')
            preview = bundle.get('preview') or {}
            if raw and preview.get('available'):
                digest = plan_hash(raw)
                key = hashlib.sha256((session+'\0'+raw['runId']).encode()).hexdigest()
                if key not in self.runs:
                    self.runs[key] = self._restore(key, {'first_read_at':now(), 'first_read_wave':preview['run']['current_wave'],
                        'first_plan':deepcopy(raw), 'latest_plan':deepcopy(raw), 'plan_hash':digest,
                        'association':'same_run', 'planned_hits':[], 'observed_hits':[], 'observed_ids':set()})
                entry = self.runs[key]
                changed = entry['latest_plan'] != raw
                entry['latest_plan'] = deepcopy(raw)
                hits = planned_hits(raw)
                if hits != entry['planned_hits'] or changed and entry.get('saved'):
                    entry['planned_hits'] = hits
                    if hits or entry.get('saved'): self.pending.add(key)
                self.runs.move_to_end(key)
            if bundle.get('observations_complete') is True:
                observations = bundle.get('observations',[])
                merchant_uids = set(bundle.get('merchant_uids',[]))
                combat = bundle.get('observation_scene') == 'combat'
                identities = self._observation_identities(observations, combat=combat)
                ids = set(identities.values())
                if not self.baseline_ready:
                    self.seen.update(ids)
                    self.baseline_ready = True
                else:
                    for row in observations:
                        identity = identities.get(_observation_key(row))
                        if identity is None: continue
                        uid = row.get('item_uid') or ''
                        if identity not in self.seen:
                            self.seen.add(identity)
                            if identity[0] == 'drop' and not combat:
                                # UID-less town drops are offloaded cargo or
                                # another unknown source, not fresh run loot.
                                # Still baseline them to prevent later moves
                                # from manufacturing an acquisition event.
                                continue
                            self.new_ids[identity] = {'run':bundle.get('observation_run') if combat else None,
                                'source':row.get('capture_source'), 'time':now(),
                                'event':uuid.uuid4().hex if identity[0] == 'drop' else None}
                        # A newly seen ground drop can become classifiable
                        # only later in the bag. Existing baseline items stay
                        # excluded; a transfer cannot create a second event.
                        if identity not in self.new_ids or identity in self.captured_ids: continue
                        reason = high_value_reason(row)
                        if not reason: continue
                        event = self.new_ids[identity]['event']
                        record_key = ('drop_event', event) if event else identity
                        merchant_purchase = bool(uid) and uid in merchant_uids
                        entry_pair = None if merchant_purchase else next(((k,e) for k,e in reversed(self.runs.items())
                            if e['plan_hash'] and e['plan_hash']==self.new_ids[identity]['run']), None)
                        if entry_pair is None:
                            # Keep the evidence without attaching it to a
                            # different/reused map's cached plan. A confirmed
                            # merchant receipt overrides a retained run ID.
                            association = 'merchant_purchase' if merchant_purchase else 'unattributed'
                            token = uid if identity[0] == 'uid' else 'drop_event\0'+event
                            key = hashlib.sha256((session+'\0'+association+'\0'+token).encode()).hexdigest()
                            entry = {'first_read_at':now(), 'first_read_wave':None, 'first_plan':None,
                                'latest_plan':None, 'plan_hash':None, 'association':association,
                                'planned_hits':[], 'observed_hits':[], 'observed_ids':set()}
                            entry = self._restore(key,entry)
                            self.runs[key] = entry
                        else: key, entry = entry_pair
                        if record_key in entry['observed_ids']: continue
                        entry['observed_ids'].add(record_key)
                        self.captured_ids.add(identity)
                        plan_items = [(w['wave'],i) for w in (entry['latest_plan'] or {}).get('waves',[])
                            for i in (w.get('loot') or {}).get('items',[])]
                        exact_items = [(w,i) for w,i in plan_items if uid and i['dropId']==uid and i['baseItemId']==row.get('internal_name')]
                        exact = [w for w,_ in exact_items]
                        template = [w for w,i in plan_items if i['baseItemId']==row.get('internal_name')]
                        entry['observed_hits'].append({'observed_at':now(), 'item_uid':uid,
                            'observation_id':row.get('observation_id'),
                            'observation_event_id':event,
                            'observation_generation':identity[2] if identity[0] == 'drop' else None,
                            'identity_kind':'item_uid' if identity[0] == 'uid' else 'loot_drop',
                            'item_key':row.get('internal_name'), 'category':reason, 'source':row.get('capture_source'),
                            'acquisition_source':'merchant' if merchant_purchase else 'unconfirmed',
                            'name':_item_name(row.get('internal_name')),
                            'quantity':row.get('count') if type(row.get('count')) is int and row['count'] > 0 else None,
                            'first_observed_at':self.new_ids[identity]['time'], 'first_source':self.new_ids[identity]['source'],
                            'rarity':row.get('rarity',row.get('base_rarity')), 'black_mist':row.get('is_black_mist') is True,
                            'exact_drop_id_waves':exact, 'same_template_waves':template,
                            'exact_plan_items':[i for _,i in exact_items],
                            'plan_membership':'exact_id' if exact else 'unconfirmed'})
                        self.pending.add(key)
                    # Fail closed for actual detection rather than silently
                    # forgetting old UIDs and treating transfers as new drops.
                    if len(self.seen)>100_000:
                        self.baseline_ready = False
                        self.seen = ids
                        self.new_ids.clear()
                        self.captured_ids.clear()
                        self.drop_generations = {drop_id:self.drop_generations[drop_id] for drop_id in self.active_drop_ids}
            # Retry pending writes even if the next poll has no new events.
            for key in list(self.pending): self._save(key,self.runs[key])
            while len(self.runs)>4:
                old, _ = self.runs.popitem(last=False)
                self.pending.discard(old)
            self.message = ('正在监控计划与实际新增物品。' if preview.get('available') else
                            '正在监控实际新增物品，等待下一局在线掉落计划。')
            if bundle.get('observations_complete') is not True:
                self.message += '实际物品清单暂未完整，只检查已确认计划。'
