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
from .rules import LockPlanner, rules_from_dict, evaluate, DEFAULT_MIN_CONFIDENCE
from .runtime_adapter import RuntimeAdapter, headline_stats
from .stat_display import preview_value
from .runtime_client import RuntimeClient
from .loot_monitor import LootState
from .action_log import record
from .paths import RESOURCE_ROOT, rules_file

BASE = RESOURCE_ROOT
SLOT_LABELS = {'Weapon':'武器','Helm':'头盔','Chest':'胸甲','Pants':'裤子','Boots':'靴子','Belt':'腰带',
               'Ring':'戒指','Necklace':'项链','Gloves':'手套','Shoulder':'护肩','Back':'披风'}
CLASS_LABELS = {'Barbarian':'战士','Mage':'法师','Hunter':'猎人','Monk':'武僧'}


def rows(snapshot):
    for key in ('inventory','storage'):
        for row in snapshot.get('containers', {}).get(key, {}).get('slots', []):
            if row.get('is_equipment') is True:
                yield row


class AssistantService:
    def __init__(self, client=None, config=None):
        self.catalog = load_catalog(BASE / 'data/game-catalog.json')
        self.ui = json.loads((BASE / 'data/equipment-ui.json').read_text(encoding='utf-8'))
        self.pools = json.loads((BASE / 'data/affix-groups-static.json').read_text(encoding='utf-8'))
        self.adapter = RuntimeAdapter(self.catalog, BASE / 'data/affix-groups-static.json')
        self.client = client or RuntimeClient()
        self.config = Path(config) if config else rules_file()
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
        self.monitoring = False
        self.loot_state = None
        self.message = self.config_error or '连接游戏后，背包和仓库会自动读取。'
        self.error = ''
        self.progress = None
        self.history = []
        self.worker = None
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

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
            'headline_stats':{p['slot']:sorted(headline_stats(p['slot'])) for p in self.pools['slotPools']}}

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
        versions = {r.schema_version for r in rules}
        if len(versions)>1: return [r.to_dict() for r in rules]
        return {'version':next(iter(versions)) if versions else 2,'rules':[r.to_dict() for r in rules]}

    def save_rule(self, raw):
        with self.guard:
            raw = dict(raw)
            raw['id'] = raw.get('id') or uuid.uuid4().hex
            rule = Rule.from_dict(raw, self.catalog)
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
            return rule.to_dict()

    def delete_rule(self, rule_id):
        with self.guard:
            if not any(r.id==rule_id for r in self.rules): raise ValidationError('规则已经不存在。')
            self._persist([r for r in self.rules if r.id!=rule_id])

    def import_rules(self, payload):
        with self.guard:
            imported = rules_from_dict(payload, self.catalog)
            # Imported rules begin disabled; importing never silently starts locks.
            added = [Rule.from_dict({**r.to_dict(),'id':uuid.uuid4().hex,'enabled':False}, self.catalog) for r in imported]
            self._persist(self.rules+added)
            return len(added)

    def _rules(self):
        with self.guard: return list(self.rules)

    def _publish(self, snapshot):
        with self.guard:
            self.snapshot = snapshot
            self.revision += 1

    def state(self):
        with self.guard:
            if self.snapshot and not self.client.connected and not self.busy:
                self.snapshot = None
                self.monitoring = False
                self.loot_state = None
                self.message = '游戏已退出，请重新启动游戏并连接。'
            snap = self.snapshot
            rules = list(self.rules)
            result = {'connected':self.client.connected,'pid':self.client.pid,'busy':self.busy,
                'monitoring':self.monitoring,'message':self.message,'error':self.error,'progress':self.progress,
                'revision':self.revision,'rules':[r.to_dict() for r in rules], 'config_error':self.config_error,
                'history':list(self.history), 'items':[], 'counts':{}, 'complete':bool(snap and snap.get('complete')),
                'captured_at':snap.get('captured_at') if snap else None}
        if not snap: return result
        adapted = self.adapter.adapt_snapshot(snap)
        observations = {(i.container,i.index):i for i in adapted.items}
        planner = LockPlanner(self.catalog)
        for row in rows(snap):
            key = row.get('name_key')
            entry = self.catalog.equipment.get(key)
            meta = self.ui['equipment'].get(key, {})
            adapted_item = observations.get((row.get('container'),row.get('slot_index')))
            observation = adapted_item.observation if adapted_item else None
            groups = {'base':[],'primary':[],'secondary':[],'unknown':[]}
            match = False
            reasons = []
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
                decision = planner.plan(observation,rules)
                if evaluations:
                    reasons = [reason for e in evaluations for reason in e.reasons]
                else: reasons = ['尚未启用这件装备的筛选规则']
                review = decision.action=='review'
            else: review = True
            result['items'].append({'item_uid':row.get('item_uid'),'instance_id':row.get('instance_id'), 'name_key':key,
                'name':entry.label if entry else row.get('internal_name','未识别装备'),
                'container':row.get('container'),'slot_index':row.get('slot_index'),
                'locked':row.get('locked'),'level':row.get('item_level'),'upgrade':row.get('upgrade_level'),
                'icon':meta.get('icon'),'slot':meta.get('slot'),'slot_label':SLOT_LABELS.get(meta.get('slot'), '未知部位'),
                'class_mask':meta.get('class_mask',0),'groups':groups,'matches':match,'review':review,'reasons':reasons})
        for container in ('inventory','storage'):
            values = [i for i in result['items'] if i['container']==container]
            result['counts'][container] = {'equipment':len(values),'locked':sum(i['locked'] is True for i in values),
                'matches':sum(i['matches'] and i['locked'] is False for i in values),
                'occupied':snap['containers'].get(container,{}).get('occupied_count',0),
                'capacity':snap['containers'].get(container,{}).get('slot_count',0)}
        result['issues'] = snap.get('issues', [])
        return result

    def start(self, kind, payload=None):
        payload = payload or {}
        with self.guard:
            if self.closed.is_set(): raise ValidationError('助手已退出，请重新启动。')
            if self.busy: raise ValidationError('当前操作尚未完成，请稍候或停止操作。')
            if kind not in ('connect','disconnect','read','lock_selected','lock_rules','unlock_all'):
                raise ValidationError('不支持的操作。')
            if kind!='connect' and not self.client.connected: raise ValidationError('请先连接游戏。')
            if kind in ('disconnect','unlock_all'):
                self.monitoring = False
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
            self.busy = True
            self.error = ''
            self.progress = None
            self.cancelled.clear()
            self.message = {'connect':'正在连接游戏…','disconnect':'正在断开连接…','read':'正在刷新装备…',
                'lock_selected':'正在锁定所选装备…','lock_rules':'正在按规则锁定装备…','unlock_all':'正在解锁装备，监控已暂停…'}[kind]
            self.worker = threading.Thread(target=self._work, args=(kind,payload), daemon=True)
            self.worker.start()

    def _work(self, kind, payload):
        try:
            record('web_operation_started', kind=kind)
            if kind=='connect':
                self.client.connect()
                self._publish(self.client.snapshot())
                message = '已连接游戏，背包和仓库已读取。'
            elif kind=='disconnect':
                self.client.close()
                self._publish(None)
                message = '已断开游戏连接。'
            elif kind=='read':
                self._publish(self.client.snapshot())
                message = '装备清单已刷新。'
            elif kind=='monitor':
                message = self._monitor_once()
            else:
                snapshot = self.client.snapshot()
                if not snapshot.get('complete'): raise ValidationError('装备正在变化或读取不完整，请稍后重试。')
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
                self.message = message
                if kind not in ('read','monitor') or ('锁定' in message):
                    self.history.insert(0, {'time':time.strftime('%H:%M:%S'),'text':message})
                    self.history = self.history[:12]
            record('web_operation_finished', kind=kind)
        except Exception as exc:
            with self.guard:
                self.error = str(exc)
                self.message = '操作已停止：'+str(exc)
                self.monitoring = False
                self.loot_state = None
            # Show real state after partial batches instead of keeping stale locks.
            if self.client.connected:
                try: self._publish(self.client.snapshot())
                except Exception:
                    self.client.close()
                    self._publish(None)
            record('web_operation_failed', kind=kind, error=str(exc))
        finally:
            with self.guard: self.busy = False

    def _rule_candidates(self, snapshot, candidates):
        allowed = {r.get('instance_id') for r in candidates}
        source = {r.get('instance_id'):r for r in rows(snapshot)}
        planner = LockPlanner(self.catalog)
        return [source[i.instance_id] for i in self.adapter.adapt_snapshot(snapshot).items
                if i.instance_id in allowed and planner.plan(i.observation,self._rules()).should_lock]

    def _apply(self, candidates, unlock=False, by_rules=False):
        count = 0
        for index, row in enumerate(candidates):
            if self.cancelled.is_set(): break
            with self.guard:
                self.progress = {'done':index,'total':len(candidates),'changed':count}
            def validate(actual):
                if self.cancelled.is_set(): return False
                if not by_rules: return True
                observation = self.adapter.adapt_item(actual, snapshot_token='fresh-'+uuid.uuid4().hex,session_id=str(self.client.pid)).observation
                return LockPlanner(self.catalog).plan(observation,self._rules()).should_lock
            action = self.client.unlock_equipment if unlock else self.client.lock_equipment
            result = action(row, validate)
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
                self.cancelled.set()
                self.loot_state = None
                self.message = '监控已关闭。'
            self.monitoring = enabled

    def stop(self):
        self.cancelled.set()
        with self.guard:
            self.monitoring = False
            self.loot_state = None
            self.message = '正在停止后续操作；已经完成的锁状态会保留。' if self.busy else '已停止操作和监控。'

    def _monitor_once(self):
        snapshot = self.client.snapshot()
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
        count = self._apply(candidates, by_rules=True) if candidates else 0
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
                if not self.monitoring or self.busy or not self.client.connected: continue
                self.busy = True
                self.worker = threading.Thread(target=self._work,args=('monitor',{}),daemon=True)
                self.worker.start()

    def close(self):
        self.stop()
        self.closed.set()
        worker = self.worker
        if worker and worker is not threading.current_thread(): worker.join(10)
        self.client.close()
