"""Application-level watering input/output; no execution authority."""
from __future__ import annotations

import copy
import math
import re

PROMPT_VERSION = 'watering-decision-prompt.v1'
OUTPUT_FIELDS = {'action', 'watering_seconds', 'reason_summary', 'evidence',
                 'uncertainties', 'next_check_after_seconds'}


def build_input(state, request, *, vision=None, experience=None, recent_history=None, system_state=None):
    seed = request['input']
    history = copy.deepcopy(recent_history) if recent_history else dict(
        sensor_series=None, trend_summary=state.get('trends'), watering_windows=None,
        missing_facts=['sensor_series', 'watering_windows'])
    for key in history.get('missing_facts', []):
        if key in {'sensor_series', 'watering_windows'}:
            history[key] = None
    history['controller_command_state'] = None
    if isinstance(system_state, dict):
        last_off, last_seconds = system_state.get('pump_last_off_at'), system_state.get('pump_last_command_sec')
        if _number(last_off) and _number(last_seconds):
            history['controller_command_state'] = dict(last_off_publication_at_epoch_seconds=last_off,
                last_requested_seconds=last_seconds, source='phase3_system_state',
                association='not_verified_same_action',
                evidence_scope='software_control_state_not_completed_action_or_flow')
    limits = copy.deepcopy(seed.get('execution_constraints'))
    if isinstance(limits, dict):
        safety = state.get('safety')
        limits['control_reference'] = ({key: safety.get(key) for key in
            ('field_capacity', 'target_low', 'hard_safety_low', 'kp')}
            if isinstance(safety, dict) else None)
        limits['control_reference_meaning'] = 'existing_controller_settings_not_validated_plant_optimum'
    result = copy.deepcopy(dict(
        schema_version='watering_decision_input.v1',
        decision_context=dict(decision_id=request['decision_id'],
            care_process_id=request['care_process_id'], device_code='soil3',
            observed_at=state.get('observed_at'), deadline_at=request['deadline_at'],
            trigger_reason=seed.get('trigger_reason', 'script_watering_need')),
        current_observation=dict(sensor=state.get('soil'), air=state.get('air'),
            observed_at=state.get('observed_at'),
            vision=vision, data_quality=state.get('data_quality'),
            units=dict(soil_humidity='sensor_percent', temperature='degC',
                air_humidity='percent_RH', ec_raw='raw_uncalibrated', light='lux'),
            measurement_limits=dict(ec_raw='uncalibrated_reading',
                humidity_percent='sensor_reading_not_plant_health',
                air_temperature='independent_sensor_unverified'),
            missing_observations=(['vision'] if vision is None else [])),
        recent_history=history,
        current_episode=seed.get('current_episode', dict(baseline=None,
            executed_actions=None, response_so_far=None,
            irrigation_summary=state.get('irrigation'),
            missing_facts=['baseline', 'executed_actions', 'response_so_far'])),
        script_reference=seed.get('script_reference'),
        historical_reference=experience,
        execution_constraints=limits))
    return prepare_input(result)


def resolve_field(context, path):
    if not isinstance(path, str) or len(path) > 200 or not re.fullmatch(r'\w+(?:\.\w+)*', path, re.ASCII):
        raise ValueError('watering_evidence_path_format')
    value = context
    for key in path.split('.'):
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdigit() and str(int(key)) == key and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ValueError('watering_evidence_unknown_field:' + path)
    if value is None or value == [] or value == {}:
        raise ValueError('watering_evidence_missing_observation:' + path)
    return value


def prepare_input(context):
    """Publish a bounded reference vocabulary after all context is assembled."""
    context = copy.deepcopy(context)
    paths = []
    prefixes = [
        'current_observation.sensor', 'current_observation.air', 'current_observation.vision',
        'current_observation.data_quality', 'current_observation.observed_at',
        'execution_constraints.control_reference', 'execution_constraints.safety_state',
        'recent_history.trend_summary', 'recent_history.controller_command_state',
        'current_episode.baseline', 'current_episode.response_so_far', 'current_episode.executed_actions',
        'script_reference', 'historical_reference',
        'recent_history.sensor_series', 'recent_history.watering_windows']
    truncated = False

    def walk(value, path, depth=0):
        nonlocal truncated
        if value is None or value == [] or value == {}:
            return
        if len(paths) >= 128:
            truncated = True
            return
        paths.append(path)
        if depth >= 6:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and re.fullmatch(r'\w+', key, re.ASCII) and not key.startswith('missing_'):
                    walk(child, path + '.' + key, depth+1)
        elif isinstance(value, list):
            truncated |= len(value) > 8
            for index, child in enumerate(value[:8]):
                walk(child, path + '.' + str(index), depth+1)

    for prefix in prefixes:
        try:
            value = resolve_field(context, prefix)
        except ValueError:
            continue
        walk(value, prefix)
    context['decision_context']['evidence_fields'] = paths
    context['decision_context']['evidence_catalog_truncated'] = truncated
    context['decision_context']['input_rules'] = [
        'missing_is_unknown_not_no_watering', 'ec_raw_is_uncalibrated',
        'control_thresholds_are_not_validated_plant_optimum',
        'cite_only_listed_evidence_fields', 'unbound_command_state_is_not_completed_watering']
    return context


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_input(context):
    expected = {'schema_version', 'decision_context', 'current_observation',
                'recent_history', 'current_episode', 'script_reference',
                'historical_reference', 'execution_constraints'}
    if (not isinstance(context, dict) or set(context) != expected or
            context['schema_version'] != 'watering_decision_input.v1'):
        raise ValueError('watering_input_fields')
    for key in expected - {'schema_version'}:
        if context[key] is not None and not isinstance(context[key], dict):
            raise ValueError('watering_input_section:' + key)
    limits = context['execution_constraints']
    for key in ('min_seconds', 'max_seconds', 'min_check_seconds', 'max_check_seconds'):
        if not isinstance(limits, dict) or not _number(limits.get(key)) or limits[key] <= 0:
            raise ValueError('watering_input_limits:' + key)
    if (limits['min_seconds'] > limits['max_seconds'] or
            limits['min_check_seconds'] > limits['max_check_seconds']):
        raise ValueError('watering_input_limits_reversed')
    decision = context['decision_context']
    if not isinstance(decision, dict):
        raise ValueError('watering_input_decision_context')
    fields = decision.get('evidence_fields')
    if not isinstance(fields, list) or not 1 <= len(fields) <= 128:
        raise ValueError('watering_input_evidence_catalog')
    for field in fields:
        resolve_field(context, field)
    return context


def validate_output(value, context):
    validate_input(context)
    if not isinstance(value, dict) or set(value) != OUTPUT_FIELDS:
        raise ValueError('watering_output_fields')
    seconds = value['watering_seconds']
    if value['action'] not in {'water', 'observe'} or not _number(seconds):
        raise ValueError('watering_action_or_seconds')
    limits = context['execution_constraints']
    if value['action'] == 'observe':
        if seconds != 0:
            raise ValueError('observe_seconds_must_be_zero')
    elif not limits['min_seconds'] <= seconds <= limits['max_seconds']:
        raise ValueError('watering_dose_outside_limits')
    reason = value['reason_summary']
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
        raise ValueError('watering_reason_summary')
    uncertainties = value['uncertainties']
    if (not isinstance(uncertainties, list) or len(uncertainties) > 8 or
            any(not isinstance(x, str) or not x.strip() for x in uncertainties)):
        raise ValueError('watering_uncertainties')
    evidence = value['evidence']
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12:
        raise ValueError('watering_evidence')
    for fact in evidence:
        if (not isinstance(fact, dict) or set(fact) != {'field', 'finding'} or
                not isinstance(fact['field'], str) or
                not isinstance(fact['finding'], str) or not fact['finding'].strip()):
            raise ValueError('watering_evidence_item')
        resolve_field(context, fact['field'])
        if fact['field'] not in context['decision_context'].get('evidence_fields', []):
            raise ValueError('watering_evidence_not_listed:' + fact['field'])
    check = value['next_check_after_seconds']
    if (not _number(check) or
            not limits['min_check_seconds'] <= check <= limits['max_check_seconds']):
        raise ValueError('watering_recheck_outside_limits')
    return copy.deepcopy(value)


def adapt_output(value):
    action = dict(action_id='a1', type=value['action'])
    if value['action'] == 'water':
        action['pump_seconds'] = value['watering_seconds']
    return dict(actions=[action], reason_summary=[value['reason_summary']],
        # These are absent from the agreed output, not invented model estimates.
        expected_outcome=dict(soil_moisture=None, risk_notes=value['uncertainties']),
        confidence=None)
