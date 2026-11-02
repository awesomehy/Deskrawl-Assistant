"""Decorate a plan for display using the inventory's affix classifier/rules.

Synthetic plan identities are confined here: no lock/transfer planner receives
these observations and no selection token is exported to the browser.
"""
from .runtime_adapter import RuntimeReadEvidence
from .rules import DEFAULT_MIN_CONFIDENCE, evaluate, primary_perfect_rules, selected_group_stats


def decorate_equipment(item, rules, adapter, catalog):
    if item.get('kind') != 'equipment': return
    slot = item.get('slot')
    slot_value = next((n for n,s in adapter.slot_names.items() if s == slot), None)
    records = [{'stat':m.get('stat_id'), 'modifier_type':m.get('type_id'), 'value':m['value']}
               for m in item.get('modifiers',[])]
    # duw/dwa build GeneratedItemData.Modifiers directly from PlanModifier.
    # Already guarded by the plan's exact build and atomic read; base armour
    # and weapon attributes are excluded by RuntimeAdapter.headline_stats.
    raw = {'name_key':item['item_key'], 'internal_name':item['item_key'],
        'instance_id':'preview', 'equip_slot':slot_value, 'locked':False, 'modifiers':records}
    known = all(m.get('stat_id') is not None and m.get('type_id') is not None for m in item.get('modifiers',[]))
    evidence = RuntimeReadEvidence(identity_verified=True, slot_verified=slot_value is not None,
        lock_verified=False, enum_values_verified=True, modifiers_complete=known,
        native_affixes_verified=True, snapshot_consistent=True)
    adapted = adapter.adapt_item(raw, session_id='preview-only', evidence=evidence)
    observation = adapted.observation
    item_rules = [r for r in rules if r.enabled and item['item_key'] in r.equipment_keys]
    item['groups'] = {k:[] for k in ('base','primary','secondary','random','unknown')}
    for modifier in adapted.display_modifiers:
        source = item['modifiers'][modifier.record_index]
        group = modifier.group
        hits = [r.name for r in item_rules if modifier.stat_key in selected_group_stats(r, group)] if (
            group in ('primary','secondary','random') and observation.groups_complete.get(group)
            and modifier.confidence >= DEFAULT_MIN_CONFIDENCE
            and modifier.group_confidence >= DEFAULT_MIN_CONFIDENCE) else []
        item['groups'][group].append({'key':modifier.stat_key,
            'name':source.get('name',source.get('text','未确认词条')),
            'display_value':source.get('display_value','未确认'),
            'matched':bool(hits), 'matched_rules':list(dict.fromkeys(hits))})
    evaluations = [evaluate(r,observation,catalog) for r in item_rules]
    item['matches'] = any(e.status == 'match' for e in evaluations)
    item['matched_combinations'] = [r.name for r,e in zip(item_rules,evaluations) if e.status == 'match']
    item['primary_perfect'] = bool(primary_perfect_rules(observation,item_rules,catalog))
    item['rule_status'] = ('match' if item['matches'] else 'review' if
        any(e.status == 'uncertain' for e in evaluations) or not known else 'miss' if item_rules else 'no_rule')
    item['rule_note'] = ('；'.join(item['matched_combinations']) if item['matches'] else
                         '词条待核验' if any(e.status=='uncertain' for e in evaluations) else
                         '未命中规则' if item_rules else '未启用筛选规则')
