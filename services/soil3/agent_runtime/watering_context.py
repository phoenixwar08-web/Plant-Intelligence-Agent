"""Read-only associations across decisions in one care process."""
import copy
import json


def process_context(store, request, current_observation):
    previous = []
    unreadable = []
    for path in store.root.glob('*.json'):
        if path.name == 'active.json':
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            unreadable.append(path.name)
            continue
        if (record.get('schema_version') == 'watering_request.v1'
                and record.get('care_process_id') == request['care_process_id']
                and record.get('created_at', float('inf')) <= request['created_at']
                and record['decision_id'] != request['decision_id']):
            previous.append(record)
    previous.sort(key=lambda x: x['created_at'])
    baseline = (previous[0].get('model_input', {}).get('current_observation')
                if previous else None) or current_observation
    actions = []
    for item in previous:
        outcome = item.get('outcome') or {}
        if outcome.get('action_sec', 0) > 0:
            receipt = outcome.get('feedback_action_id')
            actions.append(dict(decision_id=item['decision_id'], source=item['owner'],
                command_completed_at=item.get('finished_at'),
                seconds=outcome['action_sec'], feedback_action_id=receipt,
                feedback_episode_id=('ep-' + receipt[4:] if receipt else None),
                physical_flow_confirmed=False))
    baseline_h = (baseline.get('sensor') or {}).get('humidity_percent')
    current_h = (current_observation.get('sensor') or {}).get('humidity_percent')
    delta = (current_h-baseline_h if isinstance(current_h, (int, float))
             and isinstance(baseline_h, (int, float)) else None)
    before_vision, after_vision = baseline.get('vision'), current_observation.get('vision')
    visual_pairs = []
    before_facts = (before_vision or {}).get('facts', [])
    for after in (after_vision or {}).get('facts', []):
        for before in before_facts:
            if before.get('plant_zone') == after.get('plant_zone'):
                visual_pairs.append(dict(plant_zone=after.get('plant_zone'),
                    before=before, after=after,
                    comparison=after.get('change_vs_previous')
                        if after.get('previous_image_id') == before.get('image_id')
                        and before.get('image_id') else 'unknown',
                    baseline_is_direct_predecessor=bool(before.get('image_id'))
                        and after.get('previous_image_id') == before.get('image_id')))
    return copy.deepcopy(dict(baseline=baseline, executed_actions=actions[-64:],
        response_so_far=dict(humidity_delta_pp=delta, visual_before_after=visual_pairs),
        pending_observation=dict(process_status='open',
            visual_comparison_available=any(p['comparison'] != 'unknown' for p in visual_pairs)),
        command_seconds_last_24h=sum(a['seconds'] for a in actions
            if a['command_completed_at'] and
            0 <= request['created_at']-a['command_completed_at'] <= 86400),
        cumulative_coverage='this_entry_only_not_all_device_water',
        missing_facts=['external_interventions_and_full_device_budget'],
        truncated_actions=len(actions) > 64, unreadable_records=unreadable))
