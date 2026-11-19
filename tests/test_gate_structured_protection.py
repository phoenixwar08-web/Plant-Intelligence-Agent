import copy
import unittest
from datetime import datetime
from dataclasses import replace

from test_cloud_gate import fresh_state, valid_strategy, policy
from services.soil3.cloud_gate.gate_v1 import evaluate_gate


class StructuredProtectionTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_state()
        self.now = datetime.fromisoformat(self.state['observed_at'].replace('Z', '+00:00')).timestamp()
        self.state['safety']['flags'].update(
            dynamic_cooldown={'cooldown_sec':3600},
            hard_safety_low_guard={'effective_low':25, 'updated_at':self.now},
            recent_response_guard={'active':True, 'expires_at':self.now-1, 'delta_per_sec':1.2})
        self.runtime = {'last_normal_irrigation_timestamp':self.now-7200}

    def evaluate(self, config=None):
        return evaluate_gate(self.state, valid_strategy(self.state), config or policy(), False,
                             phase3_system_state=self.runtime)

    def test_default_policy_parses_records_and_expiry_without_changing_state(self):
        before = copy.deepcopy(self.state)
        result = self.evaluate()
        self.assertEqual('allow', result['decision'], result['reason_codes'])
        self.assertEqual(before, self.state)

    def test_real_recent_response_still_blocks_in_advisory_mode(self):
        self.state['safety']['flags']['recent_response_guard']['expires_at'] = self.now+3600
        strategy = valid_strategy(self.state)
        strategy['actions'] = [{'action_id': 'a1', 'type': 'water', 'pump_seconds': 3}]
        result = evaluate_gate(self.state, strategy,
                               replace(policy(), native_preferences_advisory=True), False,
                               phase3_system_state=self.runtime)
        self.assertFalse(any(code.startswith('strategy_invalid:') for code in result['reason_codes']))
        self.assertIn('safety_flag_active:recent_response_guard', result['reason_codes'])

    def test_recent_response_allows_only_observation(self):
        self.state['safety']['flags']['recent_response_guard']['expires_at'] = self.now + 3600
        strategy = valid_strategy(self.state)
        strategy['actions'] = [{'action_id': 'a1', 'type': 'observe'}]
        result = evaluate_gate(self.state, strategy, policy(), False,
                               phase3_system_state=self.runtime)
        self.assertEqual('allow_with_warning', result['decision'], result['reason_codes'])

    def test_active_cooldown_remains_policy_dependent_not_parse_dependent(self):
        self.runtime['last_normal_irrigation_timestamp'] = self.now-10
        self.assertIn('safety_flag_active:dynamic_cooldown', self.evaluate()['reason_codes'])
        advisory = self.evaluate(replace(policy(), native_preferences_advisory=True))
        self.assertEqual('allow_with_warning', advisory['decision'])

    def test_malformed_threshold_record_is_not_turned_into_safe(self):
        self.state['safety']['flags']['hard_safety_low_guard'] = {'updated_at':self.now}
        self.assertIn('safety_flag_invalid:hard_safety_low_guard', self.evaluate()['reason_codes'])

    def test_gate_v2_forwards_time_context_under_default_policy(self):
        from services.soil3.cloud_gate.gate_v2 import evaluate_gate_v2
        result = evaluate_gate_v2(self.state, valid_strategy(self.state), policy(), False,
                                  phase3_system_state=self.runtime)
        self.assertEqual('allow', result['decision'], result['reason_codes'])

    def test_runtime_supplies_phase3_context_even_without_advisory_policy(self):
        import tempfile, json
        from pathlib import Path
        from unittest.mock import patch
        from test_soil3_agent_runtime import PipelineTests
        from services.soil3.agent_runtime import runtime_v1
        helper = PipelineTests(); helper.setUp(); self.addCleanup(helper.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            config = runtime_v1.RuntimeConfig.from_dict(helper.runtime_config_value(Path(directory)))
            config.phase3_state_path.parent.mkdir(parents=True)
            config.phase3_state_path.write_text(json.dumps(self.runtime))
            with patch.object(runtime_v1, 'build_health_snapshot', return_value=helper.health_snapshot()), \
                 patch.object(runtime_v1, 'evaluate_gate_v2', wraps=runtime_v1.evaluate_gate_v2) as gate:
                runtime_v1.run_pipeline(config)
            self.assertEqual(self.runtime, gate.call_args.kwargs.get('phase3_system_state'))
