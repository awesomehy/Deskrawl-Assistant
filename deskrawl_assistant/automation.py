"""Validated preferences and capacity planning for automatic physical transfers."""
from copy import deepcopy
from .models import ValidationError

DEFAULT_SETTINGS = {
    'carriage':{'enabled':False,'items':[]},
    'gems':{'enabled':False},
    'pressure':{'enabled':False,'threshold':4,'target_free':12,'equipment_filter':'all'},
}


def validate_settings(raw,available):
    if not isinstance(raw,dict) or set(raw)!=set(DEFAULT_SETTINGS): raise ValidationError('自动整理配置格式无效。')
    for group,fields in DEFAULT_SETTINGS.items():
        if not isinstance(raw[group],dict) or set(raw[group])!=set(fields): raise ValidationError('自动整理选项不完整。')
        if not isinstance(raw[group]['enabled'],bool): raise ValidationError('自动整理开关必须为布尔值。')
    selected=raw['carriage']['items']
    if (not isinstance(selected,list) or len(selected)>1024 or any(not isinstance(k,str) or k not in available for k in selected)
            or len(set(selected))!=len(selected)):
        raise ValidationError('马车物品选择无效，请重新选择。')
    if raw['carriage']['enabled'] and not selected: raise ValidationError('请先勾选至少一种要从马车收取的物品。')
    p=raw['pressure'];threshold=p['threshold'];target=p['target_free']
    if any(isinstance(v,bool) or not isinstance(v,int) for v in (threshold,target)) or not 0<=threshold<target<=2048:
        raise ValidationError('整理后预留空格须大于触发空格数，范围为 0–2048。')
    if p['equipment_filter'] not in ('all','locked','unlocked'): raise ValidationError('装备入库范围无效。')
    return deepcopy(raw)


def free_slots(snapshot):
    bag=snapshot.get('containers',{}).get('inventory',{})
    capacity,occupied=bag.get('slot_count',0),bag.get('occupied_count',0)
    if not 0<=occupied<=capacity: raise ValidationError('背包容量读取异常。')
    return capacity-occupied


def pressure_candidates(snapshot,settings,continuing=False):
    free=free_slots(snapshot)
    goal=min(settings['target_free'],snapshot['containers']['inventory']['slot_count'])
    active=settings['enabled'] and (continuing or free<=settings['threshold']) and free<goal
    if not active: return [],False
    candidates=[r for r in snapshot['containers']['inventory']['slots'] if r.get('is_equipment') is True
                and not r.get('issues') and r.get('transfer_key')
                and (settings['equipment_filter']=='all' or r.get('locked') is (settings['equipment_filter']=='locked'))]
    # Retain a hysteresis goal between cycles; move only enough to reach it.
    return candidates[:goal-free],True
