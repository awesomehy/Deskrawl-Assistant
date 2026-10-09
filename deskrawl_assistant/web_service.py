"""Thread-safe application service for the local browser interface."""
from __future__ import annotations
import json
from pathlib import Path
import re
import threading
import time
import uuid
from .catalog import load_catalog
from .models import Rule, ValidationError
from .rules import LockPlanner, rules_from_dict, evaluate, DEFAULT_MIN_CONFIDENCE, primary_perfect_rules
from .runtime_adapter import RuntimeAdapter, headline_stats
from .stat_display import preview_value
from .runtime_client import RuntimeClient
from .native_memory import SnapshotChangedError
from .loot_monitor import LootState
from .action_log import record
from .paths import RESOURCE_ROOT, rules_file, runtime_dir
from .activity_journal import ActivityJournal, ItemActivityTracker, operation_id
from .automation import DEFAULT_SETTINGS, validate_settings, pressure_candidates, free_slots
from copy import deepcopy
from .version import VERSION

BASE = RESOURCE_ROOT
SLOT_LABELS = {'Weapon':'武器','Helm':'头盔','Chest':'胸甲','Pants':'裤子','Boots':'靴子','Belt':'腰带',
               'Ring':'戒指','Necklace':'项链','Gloves':'手套','Shoulder':'护肩','Back':'披风'}
CLASS_LABELS = {'Barbarian':'战士','Mage':'法师','Hunter':'猎人','Monk':'武僧'}


def rows(snapshot):
    return (row for row in all_rows(snapshot) if row.get('is_equipment') is True)


def all_rows(snapshot):
    for key in ('inventory','storage'):
        for row in snapshot.get('containers', {}).get(key, {}).get('slots', []):
            yield row


def item_kind(row):
    if row.get('is_equipment'): return 'equipment','装备'
    if row.get('item_class')=='GemData' or row.get('internal_name','').startswith('Gem'): return 'gem','宝石'
    if row.get('internal_name','').startswith('TreasureChest'): return 'chest','宝箱'
    if row.get('item_class')=='RuneData': return 'rune','符文'
    return 'material','材料 / 其他'


class AssistantService:
    def __init__(self, client=None, config=None):
        self.catalog = load_catalog(BASE / 'data/game-catalog.json')
        tables = json.loads((BASE / 'data/game-catalog.json').read_text(encoding='utf-8'))['tables']
        self.item_names = {key:entry for table in ('Equipments','Items') for key,entry in tables[table]['entries'].items()}
        self.ui = json.loads((BASE / 'data/equipment-ui.json').read_text(encoding='utf-8'))
        self.item_ui = json.loads((BASE / 'data/item-ui.json').read_text(encoding='utf-8'))
        self.pools = json.loads((BASE / 'data/affix-groups-static.json').read_text(encoding='utf-8'))
        self.adapter = RuntimeAdapter(self.catalog, BASE / 'data/affix-groups-static.json')
        self.client = client or RuntimeClient()
        self.config = Path(config) if config else rules_file()
        self.journal = ActivityJournal((self.config.parent if config else runtime_dir()) / 'activity.sqlite3')
        self.activity = ItemActivityTracker(self.journal, self._describe_item)
        self.client.item_events = self._record_item_commit
        self.operation_kind = None
        self.automation_config = self.config.with_name('automation.json')
        self.automation_settings = deepcopy(DEFAULT_SETTINGS)
        self.automation_running = False
        self.automation_status = '自动整理已暂停。'
        self.pressure_active = False
        self.carriage = {'available':False,'count':0,'items':[]}
        try:
            if self.automation_config.exists():
                self.automation_settings = validate_settings(json.loads(self.automation_config.read_text(encoding='utf-8')),self.item_ui['collectibles'])
        except Exception as exc:
            self.automation_status = '自动整理配置未载入，原文件已保留：'+str(exc)
        self.guard = threading.RLock()
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.rules = []
        self.config_error = ''
        try:
            if self.config.exists():
                self.rules = rules_from_dict(json.loads(self.config.read_text(encoding='utf-8-sig')), self.catalog)
        except Exception as exc:
            self.config_error = '规则文件未能载入，已保留原文件：'+str(exc)
        self.snapshot = None
        self.revision = 0
        self.busy = False
        self.updating = False
        self.monitoring = False
        self.loot_state = None
        self.message = self.config_error or '连接游戏后，背包和仓库会自动读取。'
        self.error = ''
        self.progress = None
        self.history = []
        self.worker = None
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def _describe_item(self, key):
        entry = self.item_names.get(key, {})
        name = entry.get('Chinese (Simplified)') or entry.get('English') or key
        meta = self.item_ui['items'].get(key,{})
        kind = 'equipment' if key in self.ui['equipment'] else meta.get('kind','material')
        return name, kind

    def log_page(self, **filters):
        return self.journal.page(**filters)

    def _record_item_commit(self, event, result):
        if result.get('journal_recorded'): return
        movements = result.get('movements', [])
        if not movements: return
        op = result.setdefault('operation_id', operation_id())
        entries = []
        for index, movement in enumerate(movements):
            name, kind = self._describe_item(movement['name'])
            label = '从马车收取' if event == 'carriage_collected' else '转移物品'
            if movement.get('merged'): label += '（合并堆叠）'
            entries.append(dict(category='transfer',event=event,message=label+'：'+name,
                item_key=movement['name'],item_name=name,item_kind=kind,quantity=movement['count'],
                source=movement['source'],destination=movement['destination'],event_key=op+':'+str(index),
                context={'operation_id':op,'automatic':self.operation_kind=='monitor',
                         'merged':movement.get('merged',False),'identity':movement.get('identity','')}))
        self.journal.append_many(entries)
        self.activity.apply_movements(movements)
        result['journal_recorded'] = True

    def _record_transfer_failure(self, row, target, error, *, carriage=False, automatic=False):
        key = row.get('internal_name') or row.get('name_key') or '未知物品'
        name, kind = self._describe_item(key)
        self.journal.append('error','item_transfer_failed','物品转移未完成核验：'+name+' · '+str(error),
            item_key=key,item_name=name,item_kind=kind,quantity=row.get('count',1),
            source={'container':'carriage' if carriage else row.get('container'),'slot_index':row.get('slot_index'),
                    'count_before':row.get('count',1)},destination={'container':target},
            context={'automatic':automatic,'error':str(error),'quantity_requested':True})

    def catalog_payload(self):
        localized = json.loads((BASE / 'data/game-catalog.json').read_text(encoding='utf-8'))['tables']['Equipments']['entries']
        equipment = []
        for key, item in self.ui['equipment'].items():
            entry = self.catalog.equipment[key]
            desc = localized.get(key+'.Desc', {})
            equipment.append({'key':key, 'name':entry.label,'en':entry.en,'legendary':key.startswith('Legendary'),
                'description':re.sub(r'<[^>]+>', '', desc.get('Chinese (Simplified)') or desc.get('English') or ''),
                **{k:item[k] for k in ('slot','slot_value','class_mask','icon')},
                'slot_label':SLOT_LABELS.get(item['slot'], item['slot'])})
        return {'equipment':equipment,'stats':[{'key':k,'name':e.label,'en':e.en} for k,e in self.catalog.stats.items()],
            'classes':[{'key':k,'name':v,'value':self.ui['enums']['HeroClass'][k]} for k,v in CLASS_LABELS.items()],
            'slots':[{'key':k,'name':v} for k,v in SLOT_LABELS.items()],
            'pools':{p['slot']:{g:[key for key in p[g] if key not in headline_stats(p['slot'])]
                for g in ('primary','secondary')} for p in self.pools['slotPools']},
            'headline_stats':{p['slot']:sorted(headline_stats(p['slot'])) for p in self.pools['slotPools']},
            'collectibles':list(self.item_ui['collectibles'].values())}

    def _persist(self, rules):
        if self.config_error:
            raise ValidationError(self.config_error)
        self.config.parent.mkdir(parents=True, exist_ok=True)
        versions = {r.schema_version for r in rules}
        payload = {'version':next(iter(versions)) if len(versions)==1 else 2, 'rules':[r.to_dict() for r in rules]}
        # A mixed legacy/count file is represented as the supported bare list.
        if len(versions)>1: payload = payload['rules']
        temp = self.config.with_suffix('.json.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temp.replace(self.config)
        self.rules = rules
        self.revision += 1

    def export_rules(self):
        rules = self._rules()
        self.journal.append('settings','rules_exported',f'导出 {len(rules)} 个词条组合。')
        versions = {r.schema_version for r in rules}
        if len(versions)>1: return [r.to_dict() for r in rules]
        return {'version':next(iter(versions)) if versions else 2,'rules':[r.to_dict() for r in rules]}

    def save_rule(self, raw):
        with self.guard:
            raw = dict(raw)
            raw['id'] = raw.get('id') or uuid.uuid4().hex
            rule = Rule.from_dict(raw, self.catalog)
            if len(rule.name)>80: raise ValidationError('组合名称最多 80 个字符。')
            if len(rule.equipment_keys)!=1 or not rule.is_count_rule:
                raise ValidationError('每条配置需对应一件装备，并设置主词条数量条件。')
            if any(g.operator!='>=' for g in rule.groups.values()):
                raise ValidationError('界面使用“至少 n 类（≥n）”。')
            item = self.ui['equipment'][rule.equipment_keys[0]]
            pool = next(p for p in self.pools['slotPools'] if p['slot']==item['slot'])
            for name, group in rule.groups.items():
                if set(group.selected_stats) & headline_stats(item['slot']):
                    raise ValidationError('基础护甲、武器伤害和武器速度不属于随机主副词条，不能参与筛选。')
                if set(group.selected_stats)-set(pool[name]):
                    raise ValidationError('所选词条不属于这件装备的'+('主词条' if name=='primary' else '副词条')+'池。')
            updated = [rule if r.id==rule.id else r for r in self.rules]
            if not any(r.id==rule.id for r in self.rules): updated.append(rule)
            self._persist(updated)
            self.journal.append('settings','rule_saved','保存词条组合：'+rule.name,
                item_key=rule.equipment_keys[0],item_name=self._describe_item(rule.equipment_keys[0])[0],
                context={'rule':rule.to_dict()})
            return rule.to_dict()

    def delete_rule(self, rule_id):
        return self.delete_rules([rule_id])

    def delete_rules(self, rule_ids, equipment_key=None):
        with self.guard:
            if (not isinstance(rule_ids,list) or not rule_ids or
                    any(not isinstance(i,str) or not i for i in rule_ids) or len(set(rule_ids))!=len(rule_ids)):
                raise ValidationError('请选择要删除的组合，每个组合只能选择一次。')
            deleted = [r for r in self.rules if r.id in rule_ids]
            if len(deleted)!=len(rule_ids): raise ValidationError('部分组合已经不存在，请刷新后重试。')
            if equipment_key is not None and any(equipment_key not in r.equipment_keys for r in deleted):
                raise ValidationError('所选组合不属于当前装备。')
            # An imported legacy rule may be shared by several pieces of gear.
            # A scoped deletion removes only this equipment's association.
            updated=[]
            for rule in self.rules:
                if rule.id not in rule_ids: updated.append(rule)
                elif equipment_key is not None and len(rule.equipment_keys)>1:
                    updated.append(Rule.from_dict({**rule.to_dict(),
                        'equipment_keys':[k for k in rule.equipment_keys if k!=equipment_key]},self.catalog))
            self._persist(updated)
            key=equipment_key or deleted[0].equipment_keys[0]
            self.journal.append('settings','rule_deleted' if len(deleted)==1 else 'rules_deleted',
                '删除词条组合：'+deleted[0].name if len(deleted)==1 else f'删除 {len(deleted)} 个词条组合。',
                item_key=key,item_name=self._describe_item(key)[0],
                context={'rule':deleted[0].to_dict()} if len(deleted)==1 else {'rules':[r.to_dict() for r in deleted]})
            return len(deleted)

    def import_rules(self, payload, enabled=False):
        with self.guard:
            if not isinstance(enabled,bool): raise ValidationError('导入后启用选项必须为开启或关闭。')
            imported = rules_from_dict(payload, self.catalog)
            added = [Rule.from_dict({**r.to_dict(),'id':uuid.uuid4().hex,'enabled':enabled}, self.catalog) for r in imported]
            self._persist(self.rules+added)
            self.journal.append('settings','rules_imported',f'导入 {len(added)} 个词条组合，'+('已启用。' if enabled else '已停用。'),
                context={'enabled':enabled,'rules':[r.to_dict() for r in added]})
            return len(added)

    @staticmethod
    def _combination_signature(rule):
        data=rule.to_dict()
        for key in ('id','equipment_keys'): data.pop(key)
        for group in data.get('groups',{}).values(): group['selected_stats'].sort()
        if 'conditions' in data:
            data['conditions'].sort(key=lambda c:json.dumps(c,sort_keys=True))
        return json.dumps(data,sort_keys=True,ensure_ascii=False)

    def copy_rules(self, source_key, target_key, rule_ids=None, replace_existing=False, expected_target_ids=None):
        with self.guard:
            if not isinstance(replace_existing,bool): raise ValidationError('复制方式无效。')
            if not isinstance(source_key,str) or not isinstance(target_key,str): raise ValidationError('请选择来源和目标装备。')
            source=self.ui['equipment'].get(source_key);target=self.ui['equipment'].get(target_key)
            if not source or not target: raise ValidationError('来源或目标装备不存在。')
            if source_key==target_key: raise ValidationError('请选择另一件同类装备。')
            if source['slot']!=target['slot']: raise ValidationError('只能复制到相同部位的装备。')
            candidates=[r for r in self.rules if source_key in r.equipment_keys]
            if rule_ids is not None:
                if (not isinstance(rule_ids,list) or not rule_ids or any(not isinstance(i,str) for i in rule_ids)
                        or len(set(rule_ids))!=len(rule_ids)):
                    raise ValidationError('请选择有效的来源组合。')
                candidates=[r for r in candidates if r.id in rule_ids]
                if len(candidates)!=len(rule_ids): raise ValidationError('来源组合已变化，请刷新后重试。')
            if not candidates: raise ValidationError('来源装备还没有保存的组合。')
            target_rules=[r for r in self.rules if target_key in r.equipment_keys]
            if replace_existing and expected_target_ids is not None:
                if (not isinstance(expected_target_ids,list) or any(not isinstance(i,str) for i in expected_target_ids)
                        or set(expected_target_ids)!={r.id for r in target_rules}
                        or len(set(expected_target_ids))!=len(expected_target_ids)):
                    raise ValidationError('目标装备的组合已变化，请重新打开复制窗口。')
            kept=[]
            for rule in self.rules:
                if not replace_existing or target_key not in rule.equipment_keys: kept.append(rule)
                elif len(rule.equipment_keys)>1:
                    kept.append(Rule.from_dict({**rule.to_dict(),
                        'equipment_keys':[k for k in rule.equipment_keys if k!=target_key]},self.catalog))
            signatures={self._combination_signature(r) for r in target_rules} if not replace_existing else set()
            added=[];skipped=0
            for rule in candidates:
                copied=Rule.from_dict({**rule.to_dict(),'id':uuid.uuid4().hex,'equipment_keys':[target_key]},self.catalog)
                signature=self._combination_signature(copied)
                if signature in signatures: skipped+=1;continue
                signatures.add(signature);added.append(copied)
            if added or replace_existing: self._persist(kept+added)
            self.journal.append('settings','rules_copied',f'复制 {len(added)} 个组合到 '+self._describe_item(target_key)[0]+'。',
                item_key=target_key,item_name=self._describe_item(target_key)[0],
                context={'source_key':source_key,'target_key':target_key,'replace_existing':replace_existing,
                    'replaced':len(target_rules) if replace_existing else 0,'skipped':skipped,'rules':[r.to_dict() for r in added]})
            return {'copied':len(added),'skipped':skipped,'replaced':len(target_rules) if replace_existing else 0,
                'target_key':target_key}

    def _rules(self):
        with self.guard: return list(self.rules)

    def _publish(self, snapshot):
        carriage = None
        if snapshot and hasattr(self.client,'carriage_snapshot'):
            try: carriage = self.client.carriage_snapshot()
            except Exception as exc: carriage = {'available':False,'complete':False,'count':0,'items':[],'reason':str(exc)}
        if snapshot: self.activity.observe(snapshot,carriage)
        with self.guard:
            self.snapshot = snapshot
            if carriage is not None: self.carriage = carriage
            self.revision += 1

    def state(self):
        with self.guard:
            if self.snapshot and not self.client.connected and not self.busy:
                self.snapshot = None
                self.monitoring = False
                self.automation_running = False
                self.carriage = {'available':False,'count':0,'items':[]}
                self.loot_state = None
                self.message = '游戏已退出，请重新启动游戏并连接。'
            snap = self.snapshot
            rules = list(self.rules)
            result = {'connected':self.client.connected,'pid':self.client.pid,'busy':self.busy or self.updating,
                'monitoring':self.monitoring,'message':self.message,'error':self.error,'progress':self.progress,
                'revision':self.revision,'rules':[r.to_dict() for r in rules], 'config_error':self.config_error,
                'history':list(self.history), 'items':[], 'counts':{}, 'complete':bool(snap and snap.get('complete')),
                'captured_at':snap.get('captured_at') if snap else None,
                'transfer':snap.get('transfer',{'available':False}) if snap else {'available':False}}
            result['automation'] = {'settings':deepcopy(self.automation_settings),'running':self.automation_running,
                'status':self.automation_status,'carriage':self._carriage_payload()}
            result['app_version'] = VERSION
        if not snap: return result
        adapted = self.adapter.adapt_snapshot(snap)
        observations = {(i.container,i.index):i for i in adapted.items}
        planner = LockPlanner(self.catalog)
        for row in all_rows(snap):
            key = row.get('name_key')
            kind, kind_label = item_kind(row)
            localized = self.item_names.get(row.get('internal_name'),{})
            entry = self.catalog.equipment.get(key)
            meta = self.ui['equipment'].get(key, {}) if kind=='equipment' else self.item_ui['items'].get(row.get('internal_name'),{})
            adapted_item = observations.get((row.get('container'),row.get('slot_index')))
            observation = adapted_item.observation if adapted_item else None
            groups = {'base':[],'primary':[],'secondary':[],'unknown':[]}
            match = False
            reasons = []
            matched_combinations = []
            primary_perfect = []
            if observation:
                item_rules = [r for r in rules if r.enabled and key in r.equipment_keys]
                evaluations = [evaluate(r,observation,self.catalog) for r in item_rules]
                for modifier in adapted_item.display_modifiers:
                    stat, group = modifier.stat_key, modifier.group
                    hit_rules = []
                    if (group in ('primary','secondary') and observation.name_confidence >= DEFAULT_MIN_CONFIDENCE
                            and observation.groups_complete.get(group) and modifier.confidence >= DEFAULT_MIN_CONFIDENCE
                            and modifier.group_confidence >= DEFAULT_MIN_CONFIDENCE):
                        hit_rules = [r.name for r in item_rules if r.is_count_rule and group in r.groups
                                     and stat in r.groups[group].selected_stats]
                    groups[group].append({'key':stat,'name':self.catalog.stats[stat].label if stat in self.catalog.stats else stat,
                        **preview_value(modifier, row), 'matched':bool(hit_rules), 'matched_rules':list(dict.fromkeys(hit_rules))})
                match = any(e.status=='match' for e in evaluations)
                matched_combinations = [r.name for r,e in zip(item_rules,evaluations) if e.status=='match']
                primary_perfect = [r.name for r in primary_perfect_rules(observation,item_rules,self.catalog)]
                decision = planner.plan(observation,rules)
                if evaluations:
                    reasons = [r.name+' · '+reason for r,e in zip(item_rules,evaluations) for reason in e.reasons]
                else: reasons = ['尚未启用这件装备的筛选规则']
                review = decision.action=='review'
            else: review = row.get('is_equipment') is True
            selection_id = row.get('transfer_key') or row.get('instance_id')
            movable = bool(result['complete'] and result['transfer'].get('available') and row.get('transfer_key') and not row.get('issues'))
            if row.get('container')=='storage' and row.get('slot_index') not in result['transfer'].get('storage_indices',[]):
                movable = False
            result['items'].append({'item_uid':row.get('item_uid'),'instance_id':row.get('instance_id'), 'name_key':key,
                'selection_id':selection_id,'movable':movable,'is_equipment':row.get('is_equipment') is True,
                'kind':kind,'kind_label':kind_label,'count':row.get('count',1),
                'max_stack':row.get('max_stack'),
                'internal_name':row.get('internal_name'),'effect_groups':meta.get('effect_groups',[]),'effect_note':meta.get('effect_note',''),
                'name':entry.label if entry else localized.get('Chinese (Simplified)') or localized.get('English') or row.get('internal_name','未识别物品'),
                'container':row.get('container'),'slot_index':row.get('slot_index'),
                'locked':row.get('locked'),'level':row.get('item_level'),'upgrade':row.get('upgrade_level'),
                'icon':meta.get('icon'),'slot':meta.get('slot'),'slot_label':SLOT_LABELS.get(meta.get('slot'), '未知部位') if kind=='equipment' else kind_label,
                'class_mask':meta.get('class_mask',0),'groups':groups,'matches':match,'matched_combinations':matched_combinations,
                'review':review,'reasons':reasons,'primary_perfect':bool(primary_perfect),
                'primary_perfect_combinations':primary_perfect})
        for container in ('inventory','storage'):
            values = [i for i in result['items'] if i['container']==container]
            result['counts'][container] = {'equipment':sum(i['is_equipment'] for i in values),'locked':sum(i['is_equipment'] and i['locked'] is True for i in values),
                'matches':sum(i['matches'] and i['locked'] is False for i in values),
                'occupied':snap['containers'].get(container,{}).get('occupied_count',0),
                'capacity':snap['containers'].get(container,{}).get('slot_count',0)}
        result['issues'] = snap.get('issues', [])
        return result

    def start(self, kind, payload=None):
        payload = payload or {}
        with self.guard:
            if self.closed.is_set(): raise ValidationError('助手已退出，请重新启动。')
            if self.updating: raise ValidationError('正在更新程序，请稍候。')
            if self.busy: raise ValidationError('当前操作尚未完成，请稍候或停止操作。')
            if kind not in ('connect','disconnect','read','lock_selected','lock_rules','unlock_all','move_items'):
                raise ValidationError('不支持的操作。')
            if kind!='connect' and not self.client.connected: raise ValidationError('请先连接游戏。')
            if kind in ('disconnect','unlock_all'):
                self.monitoring = False
                self.automation_running = False
                self.loot_state = None
            if kind=='lock_rules' and not any(r.enabled for r in self.rules):
                raise ValidationError('请先保存并启用至少一条筛选规则。')
            scope = payload.get('scope','all')
            if scope not in ('all','inventory','storage'): raise ValidationError('操作范围无效。')
            if kind=='lock_selected':
                selection = payload.get('items', [])
                if not isinstance(selection,list) or not 1<=len(selection)<=2048:
                    raise ValidationError('请先勾选要锁定的装备。')
                if any(not isinstance(i,dict) or not all(isinstance(i.get(k),str) and i[k] for k in ('item_uid','instance_id','name_key')) for i in selection):
                    raise ValidationError('所选装备身份不完整，请刷新清单。')
            if kind=='move_items':
                target = payload.get('target')
                selection = payload.get('items')
                if target not in ('inventory','storage'):
                    raise ValidationError('物品移动方向无效。')
                if not isinstance(selection,list) or not 1<=len(selection)<=250 or any(not isinstance(k,str) or not k for k in selection) or len(set(selection))!=len(selection):
                    raise ValidationError('请勾选要移动的物品；每次最多 250 组。')
                if not self.snapshot or not self.snapshot.get('complete') or not self.snapshot.get('transfer',{}).get('available'):
                    raise ValidationError('移动条件尚未核验，请刷新物品清单。')
                source = 'storage' if target=='inventory' else 'inventory'
                current = {r.get('transfer_key'):r for r in all_rows(self.snapshot) if r.get('container')==source}
                if any(k not in current for k in selection):
                    raise ValidationError('所选物品已变化或不属于来源容器，请刷新后重新选择。')
                # Resolve browser tokens only against the trusted server snapshot.
                payload = {'target':target,'expected':[dict(current[k]) for k in selection]}
            self.busy = True
            self.operation_kind = kind
            self.error = ''
            self.progress = None
            self.cancelled.clear()
            self.message = {'connect':'正在连接游戏…','disconnect':'正在断开连接…','read':'正在刷新装备…',
                'lock_selected':'正在锁定所选装备…','lock_rules':'正在按规则锁定装备…','unlock_all':'正在解锁装备，监控已暂停…','move_items':'正在核验物品和目标空格…'}[kind]
            self.worker = threading.Thread(target=self._work, args=(kind,payload), daemon=True)
            self.worker.start()

    def _work(self, kind, payload):
        with self.guard: self.operation_kind = kind
        try:
            if kind!='observe': record('web_operation_started', kind=kind)
            if kind not in ('observe','monitor'):
                self.journal.append('system','operation_started',{
                    'connect':'连接游戏','disconnect':'断开游戏','read':'刷新物品清单',
                    'lock_selected':'锁定所选装备','lock_rules':'按规则锁定装备','unlock_all':'全部解锁装备',
                    'move_items':'移动所选物品'}[kind]+'：开始',context={'operation':kind,'scope':payload.get('scope','all')})
            if kind=='connect':
                self.client.connect()
                self.activity.reset()
                self._publish(self.client.snapshot())
                message = '已连接游戏，背包和仓库已读取。'
            elif kind=='disconnect':
                self.client.close()
                self._publish(None)
                message = '已断开游戏连接。'
            elif kind in ('read','observe'):
                self._publish(self.client.snapshot())
                message = '装备清单已刷新。'
            elif kind=='monitor':
                message = self._monitor_once()
            elif kind=='move_items':
                self._publish(self.client.snapshot())
                with self.guard: self.progress = {'done':0,'total':len(payload['expected'])}
                result = self.client.move_items(payload['expected'],payload['target'],lambda:not self.cancelled.is_set())
                self._record_item_commit('items_transferred',result)
                self._publish(self.client.snapshot())
                with self.guard: self.progress = {'done':result['moved'],'total':len(payload['expected'])}
                label = '移入仓库' if payload['target']=='storage' else '取回背包'
                stacked = f"，其中 {result['stacked_count']} 颗宝石已合并堆叠" if result.get('stacked_count') else ''
                message = f"已{label} {result['moved']} 组物品{stacked}；数量、词条及锁定状态保留，已回读并请求游戏保存。"
            else:
                snapshot = self.client.snapshot()
                if not snapshot.get('complete'): raise ValidationError('装备正在变化或读取不完整，请稍后重试。')
                self._publish(snapshot)
                scope = payload.get('scope','all')
                candidates = [r for r in rows(snapshot) if scope=='all' or r.get('container')==scope]
                if kind=='lock_selected':
                    identities = {(i['item_uid'],i['instance_id'],i['name_key']) for i in payload['items']}
                    # Resolve selected instances afresh, including moves between containers.
                    candidates = [r for r in rows(snapshot) if (r.get('item_uid'),r.get('instance_id'),r.get('name_key')) in identities]
                    if len(candidates)!=len(identities): raise ValidationError('部分所选装备已经移出背包或仓库，请刷新后重新选择。')
                    candidates = [r for r in candidates if r.get('locked') is False]
                elif kind=='unlock_all': candidates = [r for r in candidates if r.get('locked') is True]
                else: candidates = self._rule_candidates(snapshot, candidates)
                count = self._apply(candidates, kind=='unlock_all', kind=='lock_rules')
                self._publish(self.client.snapshot())
                label = '解锁' if kind=='unlock_all' else '锁定'
                message = f'已后台{label} {count} 件装备'+('；已停止后续操作。' if self.cancelled.is_set() else '；已回读锁状态并请求游戏保存。')
            with self.guard:
                if kind!='observe': self.message = message
                if kind not in ('read','monitor','observe') or ('锁定' in message) or ('自动整理：' in message):
                    self.history.insert(0, {'time':time.strftime('%H:%M:%S'),'text':message})
                    self.history = self.history[:12]
            if kind not in ('observe','monitor'):
                self.journal.append('system','operation_finished',message,context={'operation':kind,'cancelled':self.cancelled.is_set()})
            if kind!='observe': record('web_operation_finished', kind=kind)
        except Exception as exc:
            if kind in ('monitor','observe') and isinstance(exc, SnapshotChangedError):
                # Keep both switches and the pending loot identities. Nothing
                # from an inconsistent read is published or used for a write.
                with self.guard:
                    self.error = ''
                    if kind=='monitor': self.message = '监控运行中：物品正在变化，等待下一轮完整读取。'
                    if self.automation_running:
                        self.automation_status = '自动整理运行中：物品正在变化，等待下一轮检查。'
                record('web_operation_retry', kind=kind, error=str(exc))
                return
            with self.guard:
                self.error = str(exc)
                self.message = '操作已停止：'+str(exc)
                self.monitoring = False
                self.automation_running = False
                self.automation_status = '自动整理已停止：'+str(exc)
                self.loot_state = None
            # Show real state after partial batches instead of keeping stale locks.
            if self.client.connected:
                try: self._publish(self.client.snapshot())
                except Exception:
                    self.client.close()
                    self._publish(None)
            record('web_operation_failed', kind=kind, error=str(exc))
            if kind=='move_items':
                for row in payload['expected']: self._record_transfer_failure(row,payload['target'],exc)
            self.journal.append('error','operation_failed','操作失败：'+str(exc),context={'operation':kind})
        finally:
            with self.guard:
                self.busy = False
                self.operation_kind = None

    def _rule_candidates(self, snapshot, candidates):
        allowed = {r.get('instance_id') for r in candidates}
        source = {r.get('instance_id'):r for r in rows(snapshot)}
        planner = LockPlanner(self.catalog)
        return [source[i.instance_id] for i in self.adapter.adapt_snapshot(snapshot).items
                if i.instance_id in allowed and planner.plan(i.observation,self._rules()).should_lock]

    def _apply(self, candidates, unlock=False, by_rules=False, monitor_only=False):
        count = 0
        for index, row in enumerate(candidates):
            if self.cancelled.is_set() or (monitor_only and not self.monitoring): break
            with self.guard:
                self.progress = {'done':index,'total':len(candidates),'changed':count}
            matched_combinations = []
            def validate(actual):
                if self.cancelled.is_set() or (monitor_only and not self.monitoring): return False
                if not by_rules: return True
                observation = self.adapter.adapt_item(actual, snapshot_token='fresh-'+uuid.uuid4().hex,session_id=str(self.client.pid)).observation
                matched_combinations[:] = [r.name for r in self._rules() if r.enabled
                    and evaluate(r,observation,self.catalog).status=='match']
                return LockPlanner(self.catalog).plan(observation,self._rules()).should_lock
            action = self.client.unlock_equipment if unlock else self.client.lock_equipment
            name, item_type = self._describe_item(row.get('internal_name') or row['name_key'])
            try: result = action(row, validate)
            except Exception as exc:
                self.journal.append('error','unlock_failed' if unlock else 'lock_failed',
                    ('解锁失败：' if unlock else '锁定失败：')+name+' · '+str(exc),item_key=row['name_key'],
                    item_name=name,item_kind=item_type,quantity=row.get('count',1),
                    source={'container':row.get('container'),'slot_index':row.get('slot_index')},
                    context={'error':str(exc),'automatic':monitor_only})
                raise
            position={'container':result.get('container',row.get('container')),
                      'slot_index':result.get('slot_index',row.get('slot_index'))}
            self.journal.append('lock','equipment_unlocked' if unlock else 'equipment_locked',
                ('解锁' if unlock else '锁定')+('（状态已满足）' if result['status'].startswith('already_') else '')+'：'+name,
                item_key=row['name_key'],item_name=name,item_kind=item_type,quantity=row.get('count',1),destination=position,
                context={'automatic':monitor_only,'by_rules':by_rules,'status':result['status'],
                         'identity':row.get('item_uid'),'locked_before':row.get('locked'),'locked_after':not unlock,
                         'matched_combinations':matched_combinations})
            count += result['status']==('unlocked' if unlock else 'locked')
            with self.guard: self.progress = {'done':index+1,'total':len(candidates),'changed':count}
        return count

    def set_monitoring(self, enabled):
        if not isinstance(enabled,bool): raise ValidationError('监控开关必须为布尔值。')
        with self.guard:
            if enabled:
                if not self.client.connected: raise ValidationError('请先连接游戏。')
                if self.busy: raise ValidationError('请等待当前操作完成后开启监控。')
                if not any(r.enabled for r in self.rules): raise ValidationError('请先保存并启用至少一条规则。')
                if not self.snapshot or not self.snapshot.get('complete'): raise ValidationError('请先刷新完整的装备清单。')
                if self.monitoring: return
                self.loot_state = None
                self.cancelled.clear()
                self.message = '监控已开启：将记录现有装备，仅自动锁定之后新增且符合规则的装备。'
            else:
                if not self.automation_running: self.cancelled.set()
                self.loot_state = None
                self.message = '监控已关闭。'
            self.monitoring = enabled
            self.journal.append('settings','monitor_changed','开启自动锁定监控' if enabled else '关闭自动锁定监控')

    def stop(self):
        self.cancelled.set()
        with self.guard:
            self.monitoring = False
            self.automation_running = False
            self.automation_status = '自动整理已暂停。'
            self.loot_state = None
            self.message = '正在停止后续操作；已经完成的操作会保留。' if self.busy else '已停止操作和监控。'
            self.journal.append('system','operations_stopped',self.message)

    def _carriage_payload(self):
        value=deepcopy(self.carriage)
        for row in value.get('items',[]):
            key=row.get('internal_name');entry=self.item_names.get(key,{})
            row['name']=entry.get('Chinese (Simplified)') or entry.get('English') or key
            row['icon']=self.item_ui['items'].get(key,{}).get('icon') or self.ui['equipment'].get(key,{}).get('icon')
        return value

    def save_automation(self,raw):
        settings=validate_settings(raw,self.item_ui['collectibles'])
        with self.guard:
            if self.closed.is_set(): raise ValidationError('助手已退出。')
            self.automation_config.parent.mkdir(parents=True,exist_ok=True)
            temp=self.automation_config.with_suffix('.json.tmp')
            temp.write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            temp.replace(self.automation_config)
            self.automation_settings=settings
            self.pressure_active=False
            self.revision+=1
            self.journal.append('settings','automation_saved','保存自动整理设置',context={'settings':settings})
            return deepcopy(settings)

    def set_automation_running(self,enabled):
        if not isinstance(enabled,bool): raise ValidationError('自动整理开关无效。')
        with self.guard:
            if enabled:
                if not self.client.connected: raise ValidationError('请先连接游戏。')
                if self.busy: raise ValidationError('请等待当前操作完成。')
                if not self.snapshot or not self.snapshot.get('complete'): raise ValidationError('请先刷新完整的物品清单。')
                if not any(v['enabled'] for v in self.automation_settings.values()): raise ValidationError('请先启用并保存至少一项自动整理功能。')
                self.cancelled.clear()
            self.automation_running=enabled
            self.pressure_active=False
            self.automation_status='自动整理运行中，每 2 秒检查一次。' if enabled else '自动整理已暂停。'
            self.journal.append('settings','automation_changed','启动自动整理' if enabled else '暂停自动整理')

    def _monitor_once(self):
        from .carriage_transfer import CarriageUnavailable
        with self.guard: lock_active=self.monitoring;auto_active=self.automation_running
        lock_message=self._lock_monitor_once() if lock_active else ''
        try: auto_message=self._automation_once() if auto_active else ''
        except CarriageUnavailable as exc:
            auto_message=str(exc)
            with self.guard: self.automation_status=auto_message
            self._publish(self.client.snapshot())
        return ' '.join(v for v in (lock_message,auto_message) if v) or '监控已关闭。'

    def _automation_once(self):
        from .native_memory import MemoryReadError
        from .carriage_transfer import CarriageUnavailable
        with self.guard: settings=deepcopy(self.automation_settings)
        def active():
            with self.guard:
                return self.automation_running and not self.cancelled.is_set() and settings==self.automation_settings
        snapshot=self.client.snapshot()
        self._publish(snapshot)
        if not snapshot.get('complete'):
            self._publish(snapshot)
            with self.guard: self.automation_status='物品正在变化，等待下一次完整读取。'
            return self.automation_status
        changes=[];waiting=[];budget=8
        def move_one(row):
            nonlocal snapshot,budget
            if not active(): return False
            if not snapshot.get('transfer',{}).get('available'):
                waiting.append('搬运条件尚未核验，等待刷新。');return False
            try:
                result=self.client.move_items([row],'storage',active)
                self._record_item_commit('items_transferred',result)
            except MemoryReadError as exc:
                if any(v in str(exc) for v in ('空格不足','发生变化','正在变化','已取消')):
                    waiting.append(str(exc));return False
                self._record_transfer_failure(row,'storage',exc,automatic=True)
                raise
            budget-=1
            name=self.item_names.get(row['internal_name'],{}).get('Chinese (Simplified)') or row['internal_name']
            suffix='（合并堆叠）' if result.get('stacked_count') else ''
            changes.append('移入仓库 '+name+suffix)
            snapshot=self.client.snapshot()
            self._publish(snapshot)
            if not snapshot.get('complete'): raise CarriageUnavailable('搬运后物品正在变化，等待下一次读取。')
            return True
        if settings['gems']['enabled']:
            blocked=set()
            while budget and active():
                gems=[r for r in snapshot['containers']['inventory']['slots'] if item_kind(r)[0]=='gem' and not r.get('issues') and r.get('transfer_key') and r['transfer_key'] not in blocked]
                if not gems: break
                if not move_one(gems[0]):
                    blocked.add(gems[0]['transfer_key'])
                    budget-=1
        if settings['pressure']['enabled'] and budget and active():
            candidates,self.pressure_active=pressure_candidates(snapshot,settings['pressure'],self.pressure_active)
            if self.pressure_active and not candidates: waiting.append('背包空间紧张，当前没有符合入库范围的装备。')
            while candidates and budget and active():
                if not move_one(candidates[0]): break
                candidates,self.pressure_active=pressure_candidates(snapshot,settings['pressure'],self.pressure_active)
        if settings['carriage']['enabled'] and budget and active():
            carriage=self.client.carriage_snapshot()
            self.activity.observe(snapshot,carriage)
            with self.guard: self.carriage=carriage
            if not carriage.get('available'): waiting.append(carriage.get('reason','马车尚未出现。'))
            else:
                selected=set(settings['carriage']['items'])
                for row in carriage.get('items',[]):
                    if not budget or not active(): break
                    if row['internal_name'] not in selected or not row.get('collectable'): continue
                    if free_slots(snapshot)<=0:
                        waiting.append('背包已满，马车物品保留，等待腾出空格。');break
                    try:
                        result=self.client.collect_carriage(row['selection_id'],active)
                        self._record_item_commit('carriage_collected',result)
                    except CarriageUnavailable as exc: waiting.append(str(exc));break
                    except MemoryReadError as exc:
                        if '发生变化' in str(exc): waiting.append('马车正在收集物品，等待下一次检查。');break
                        self._record_transfer_failure(row,'inventory',exc,carriage=True,automatic=True)
                        raise
                    changes.append('从马车收取 '+(self.item_names.get(result['name'],{}).get('Chinese (Simplified)') or result['name']))
                    budget-=1;snapshot=self.client.snapshot()
                    self._publish(snapshot)
                    if not snapshot.get('complete'): break
        self._publish(snapshot)
        message='自动整理：'+'；'.join(changes) if changes else '自动整理运行中，等待符合设置的物品。'
        with self.guard:
            if self.automation_running: self.automation_status='；'.join(waiting) if waiting else message
        if changes: record('automation_transfers',changes=changes)
        return message if changes else self.automation_status

    def _lock_monitor_once(self):
        snapshot = self.client.snapshot()
        self._publish(snapshot)
        with self.guard:
            previous = self.loot_state
            active = self.monitoring
        if not active: return '监控已关闭。'
        state = LootState.observe(snapshot, previous)
        if not snapshot.get('complete'):
            self._publish(snapshot)
            return '本次装备正在变化，等待下次完整读取。'
        baseline = previous is None or (state and state.session!=previous.session)
        candidates = [] if baseline or state is None else self._rule_candidates(snapshot,
            [r for r in rows(snapshot) if r.get('instance_id') in state.pending])
        count = self._apply(candidates, by_rules=True, monitor_only=True) if candidates else 0
        if count:
            snapshot = self.client.snapshot()
            state = LootState.observe(snapshot,state)
        with self.guard:
            if self.monitoring: self.loot_state = state
        self._publish(snapshot)
        if count: return f'监控中：已后台锁定 {count} 件新增装备。'
        return '监控中：现有装备已记录，等待符合规则的新装备。'

    def _monitor_loop(self):
        while not self.closed.wait(2):
            with self.guard:
                if self.busy or self.updating or not self.client.connected: continue
                self.busy = True
                self.operation_kind = 'monitor' if self.monitoring or self.automation_running else 'observe'
                self.worker = threading.Thread(target=self._work,args=(self.operation_kind,{}),daemon=True)
                self.worker.start()

    def close(self):
        self.stop()
        self.closed.set()
        worker = self.worker
        if worker and worker is not threading.current_thread(): worker.join(10)
        self.client.close()
