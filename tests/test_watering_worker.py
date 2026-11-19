import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.soil3.agent_runtime.watering_request import RequestStore
from services.soil3.agent_runtime.watering_worker import process_request
from services.soil3.cloud_gate.gate_v2 import GatePolicy
from test_cloud_strategy import FakeSession, FakeResponse, chain_config
from test_controlled_phase3_execution import state_snapshot


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RequestStore(self.root/'requests')
        self.config = SimpleNamespace(provider_mode='qwen_dashscope',
            runtime_root=self.root, strategy_config=chain_config,
            runner_dir=self.root/'runner', episode_dir=self.root/'episodes',
            phase3_state_path=self.root/'system_state.json',
            gate_policy=GatePolicy(warning_age_seconds=900, deny_age_seconds=18000,
                                  window_seconds=86400, max_exploration_water_seconds=0))
        self.state = state_snapshot(); self.state['soil']['humidity_percent'] = 34
        self.state['safety'].update(target_low=32, field_capacity=45, hard_safety_low=20)
        self.item = self.store.create(dict(script_reference={'suggested_seconds': 5},
            execution_constraints=dict(min_seconds=3, max_seconds=30,
                                       min_check_seconds=300, max_check_seconds=3600)),
            model_key='cloud', process_id='p')
        self.output = dict(action='observe', watering_seconds=0, reason_summary='等待响应。',
            evidence=[{'field': 'current_observation.sensor.humidity_percent', 'finding': '读数34%'}],
            uncertainties=[], next_check_after_seconds=1800)

    def run_worker(self, response):
        with patch('services.soil3.agent_runtime.watering_worker.write_state_snapshot', return_value=self.state), \
             patch('services.soil3.agent_runtime.watering_worker._collect_vision', return_value=({}, None)), \
             patch('services.soil3.agent_runtime.watering_worker._collect_experience', return_value=({}, None)), \
             patch.dict(os.environ, CLOUD_STRATEGY_API_KEY='test-only'):
            return process_request(self.config, self.store.root, session=FakeSession(response))

    def test_six_field_reply_passes_real_chain_and_preserves_no_execution(self):
        response = FakeResponse(200, {'id': 'test', 'model': 'test-model',
            'choices': [{'message': {'content': json.dumps(self.output)}}]})
        result = self.run_worker(response)
        self.assertEqual('ai_ready', result['status'])
        item = self.store.poll()
        self.assertEqual('observe', item['result']['output']['action'])
        self.assertIsNone(item['result']['bundle']['strategy']['confidence'])
        self.assertFalse(item['result']['physical_actions_performed'])

    def test_provider_disconnect_falls_back_without_waiting(self):
        result = self.run_worker(FakeResponse(503, {}))
        self.assertEqual('model_failed', result['status'])
        self.assertEqual('fallback_ready', self.store.poll()['status'])

    def test_invalid_model_output_never_becomes_executable(self):
        self.output['watering_seconds'] = 999
        response = FakeResponse(200, {'model': 'test-model',
            'choices': [{'message': {'content': json.dumps(self.output)}}]})
        self.assertEqual('invalid_output', self.run_worker(response)['status'])
        self.assertEqual('fallback_ready', self.store.poll()['status'])
        self.assertEqual('invalid_output', self.store.poll()['reason'])

    def test_bad_evidence_has_specific_audit_error_without_legacy_field_noise(self):
        self.output['evidence'][0]['field'] = 'current_observation.sensor.fake_humidity'
        response = FakeResponse(200, {'model':'test-model',
            'choices':[{'message':{'content':json.dumps(self.output)}}]})
        self.assertEqual('invalid_output', self.run_worker(response)['status'])
        chain = json.loads((self.root/'watering_decisions'/self.item['decision_id']/'chain.json').read_text())
        reasons = chain['validation']['reason_codes']
        self.assertEqual(1, len(reasons))
        self.assertIn('current_observation.sensor.fake_humidity', reasons[0])
        self.assertTrue(chain['raw_model_response'])

    def test_watering_loader_explicitly_adapts_legacy_trigger_fields_only(self):
        from services.soil3.agent_runtime.service import load_config
        example = Path(__file__).resolve().parents[1]/'config/soil3_agent_runtime.example.json'
        value = json.loads(example.read_text(encoding='utf-8'))
        value.update(trigger_policy='business_event_v1', trigger_event_policy={})
        path = self.root/'legacy-runtime.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(ValueError):
            load_config(path)  # Legacy pipeline must not silently drop policy.
        loaded = load_config(path, watering_mode=True)
        self.assertEqual('soil3', loaded.device_code)
        value['unknown_policy'] = {}
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(ValueError):
            load_config(path, watering_mode=True)

    def test_safety_deny_does_not_enable_script_bypass(self):
        self.state['safety']['flags']['sensor_fault'] = True
        response = FakeResponse(200, {'model': 'test-model',
            'choices': [{'message': {'content': json.dumps(self.output)}}]})
        result = self.run_worker(response)
        self.assertIn(result['status'], {'gate_denied', 'local_safety_only'})
        self.assertEqual('cancelled', self.store.poll()['status'])

    def test_water_reply_reaches_phase3_exact_seconds_once_with_fake_actuator(self):
        from datetime import datetime, timezone
        from test_ai_phase3_policy import ProposalPolicyTests
        now = datetime.now(timezone.utc)
        self.state.update(observed_at=now.isoformat().replace('+00:00', 'Z'),
                          generated_at=now.isoformat().replace('+00:00', 'Z'))
        self.output.update(action='water', watering_seconds=5)
        response = FakeResponse(200, {'model': 'test-model',
            'choices': [{'message': {'content': json.dumps(self.output)}}]})
        self.assertEqual('ai_ready', self.run_worker(response)['status'])
        bundle = self.store.poll()['result']['bundle']
        helper = ProposalPolicyTests(); helper.setUp(); self.addCleanup(helper.doCleanups)
        helper.now = datetime.now(timezone.utc)
        method, brain, reading = helper.model_cycle()
        result = method(brain, reading, 'ordinary', bundle_override=bundle)
        self.assertEqual(5, getattr(result, 'action_sec', None), vars(result))
        brain.actuator.execute_pump.assert_called_once()
        brain._pending_soak = None
        result = method(brain, reading, 'ordinary', bundle_override=bundle)
        self.assertIn('already_consumed', result.notes)
        brain.actuator.execute_pump.assert_called_once()
