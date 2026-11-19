"""Consume an explicit Phase3 request; produce proposals, never actuate."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .watering_request import RequestStore
from .watering_context import process_context
from .runtime_v1 import (write_state_snapshot, _collect_vision, _collect_experience,
                         atomic_write_json, _gate_contract_valid)
from services.soil3.cloud_strategy.service import run_chain, project_optional_context
from services.soil3.cloud_strategy.watering_decision import build_input, prepare_input, validate_input
from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
from services.soil3.runner.runner_v1 import RunnerStore, DryRunRunner
from services.soil3.phase3_bridge.bridge_v1 import Phase3Bridge
from services.soil3.episode.episode_v1 import EpisodeStore
from services.soil3.cloud_trigger import build_decision_context, evaluate_trigger

PROMPT = Path(__file__).parents[1] / 'cloud_strategy/prompts/watering_decision_v1.txt'


def process_request(config, request_dir, *, session=None):
    store = RequestStore(request_dir)
    request = store.poll()
    if not request or not store.start_worker(request['decision_id']):
        return {'status': 'no_pending_request'}
    decision_id = request['decision_id']
    artifact_dir = config.runtime_root / 'watering_decisions' / decision_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if config.provider_mode != 'qwen_dashscope' or request['model_key'] != 'cloud':
        store.fail(decision_id, 'provider_unavailable')
        return {'status': 'fallback_ready', 'decision_id': decision_id}
    try:
        history = {}
        state = write_state_snapshot(config, history_output=history)
        try:
            system_state = json.loads(config.phase3_state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            system_state = None
        admission_context = build_decision_context(state,
            phase3_system_state=system_state,
            deny_age_seconds=config.gate_policy.deny_age_seconds)
        admission = evaluate_trigger(admission_context, None,
            deny_age_seconds=config.gate_policy.deny_age_seconds,
            semantic_filter_enabled=False)
        if admission.decision == 'LOCAL_SAFETY_ONLY':
            store.cancel('local_safety_only', decision_id)
            return {'status': 'local_safety_only', 'decision_id': decision_id}
        vision_diagnostics = {}
        vision_association, vision_context = _collect_vision(config, diagnostics=vision_diagnostics)
        atomic_write_json(artifact_dir / 'vision_status.json', {
            'availability': vision_association.get('availability', 'unavailable'),
            **vision_diagnostics,
        })
        _, experience_context = _collect_experience(config, state, decision_id)
        vision = project_optional_context(vision_context, kind='vision')
        if vision.get('availability') == 'available':
            # Add validated traceability scalars only; never image paths/payloads.
            for projected, original in zip(vision['facts'], vision_context['facts']):
                for key in ('image_id', 'previous_image_id', 'captured_at', 'analyzed_at'):
                    projected[key] = original.get(key)
        experience = project_optional_context(experience_context, kind='experience')
        context = build_input(state, request,
            vision=vision if vision.get('availability') == 'available' else None,
            experience=experience if experience.get('availability') == 'available' else None,
            recent_history=history or None, system_state=system_state)
        context['execution_constraints']['safety_state'] = dict(
            flags=admission_context['safety_flags'],
            predictor_circuit=admission_context['predictor_circuit'])
        context['current_episode'] = process_context(store, request, context['current_observation'])
        context = prepare_input(context)
        validate_input(context)
        if not store.attach_context(decision_id, context):
            return {'status': 'expired_before_call', 'decision_id': decision_id}
        atomic_write_json(artifact_dir / 'input.json', context)
        remaining = request['deadline_at'] - time.time()
        if remaining <= 0:
            store.poll()
            return {'status': 'deadline_exceeded', 'decision_id': decision_id}
        provider = config.strategy_config()
        provider['timeout_seconds'] = remaining
        provider['max_retries'] = 0  # One shared request deadline, no timeout multiplication.
        started = time.monotonic()
        chain = run_chain(state=state, config=provider, prompt=PROMPT.read_text(encoding='utf-8'),
                          watering_input=context, session=session)
        atomic_write_json(artifact_dir / 'chain.json', chain)
        validation = chain['validation']
        if not validation['accepted']:
            failure = chain.get('failure')
            store.fail(decision_id, failure['code'] if failure else 'invalid_output')
            return {'status': 'model_failed' if failure else 'invalid_output', 'decision_id': decision_id}
        strategy = validation['strategy']
        try:
            system_state = json.loads(config.phase3_state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            system_state = None
        gate = evaluate_gate_v2(state, strategy, config.gate_policy, False,
                               phase3_system_state=system_state)
        atomic_write_json(artifact_dir / 'gate.json', gate)
        if not _gate_contract_valid(gate, state, strategy) or gate['decision'] == 'deny':
            store.cancel('gate_denied', decision_id)  # Never cancel a newer request.
            return {'status': 'gate_denied', 'decision_id': decision_id}
        runner = DryRunRunner(RunnerStore(config.runner_dir)).run(strategy, state)
        bridge = Phase3Bridge().verify(state, strategy, gate, runner)
        atomic_write_json(artifact_dir / 'bridge.json', bridge)
        if bridge.get('accepted') is not True:
            store.cancel('bridge_rejected', decision_id)
            return {'status': 'bridge_rejected', 'decision_id': decision_id}
        episodes = EpisodeStore(config.episode_dir)
        episode = episodes.create(state)
        episodes.update(episode['episode_id'], strategy=strategy, gate_result=gate)
        result = dict(bundle=dict(schema_version='phase3_model_proposal.v1',
            state=state, strategy=strategy, gate=gate, runner=runner),
            output=chain['watering_output'], episode_id=episode['episode_id'],
            latency_ms=(time.monotonic()-started)*1000,
            model=chain['cloud'], physical_actions_performed=False)
        atomic_write_json(artifact_dir / 'result.json', result)
        accepted = store.reply(decision_id, request['input_sha256'], result)
        return {'status': 'ai_ready' if accepted else 'late_ignored', 'decision_id': decision_id}
    except Exception as error:
        # Internal errors are not inferred to be network failures. Keep the same
        # deadline; service logs only a class/code, never credentials or payloads.
        atomic_write_json(artifact_dir / 'error.json', {'code': type(error).__name__})
        raise
