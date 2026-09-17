import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from services.soil3.cloud_strategy.client import CloudStrategyError, OpenAICompatibleClient
from services.soil3.cloud_strategy.service import append_audit, parse_model_json, run_chain
from services.soil3.cloud_strategy.validator import StrategyValidator


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def sample_state():
    return {
        "schema_version": "state.v1",
        "state_id": str(uuid.uuid4()),
        "plant_id": "soil3",
        "generated_at": "2026-09-16T10:00:00+00:00",
        "sensors": {"soil_moisture_pct": 38.7},
        "safety_state": {"sensor_stale": False, "actuator_commands_allowed": False},
    }


def valid_strategy(state):
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "state_id": state["state_id"],
        "plant_id": "soil3",
        "created_at": "2026-09-16T10:00:00+00:00",
        "actions": [
            {"action_id": "a1", "type": "observe"},
            {"action_id": "a2", "type": "wait", "seconds": 1200},
            {"action_id": "a3", "type": "stop"},
        ],
        "reason_summary": ["soil_moisture_declining", "cloud_strategy_shadow_only"],
        "expected_outcome": {"soil_moisture": "continue_observation", "risk_notes": []},
        "confidence": 0.78,
        "model": {"provider": "fixture", "name": "fixture", "prompt_version": "strategy-prompt.v1"},
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.state = sample_state()
        self.validator = StrategyValidator(max_actions=12, max_pump_seconds=120, max_wait_seconds=86400)

    def test_valid_strategy_passes(self):
        result = self.validator.validate(valid_strategy(self.state), self.state)
        self.assertTrue(result.accepted)
        self.assertEqual([], result.reason_codes)

    def test_unknown_action_is_rejected(self):
        value = valid_strategy(self.state)
        value["actions"] = [{"action_id": "x", "type": "fertilize"}]
        self.assertIn("action[0]:unknown_action", self.validator.validate(value, self.state).reason_codes)

    def test_negative_and_extreme_water_are_rejected(self):
        for seconds, code in ((-1, "pump_seconds_not_positive"), (121, "pump_seconds_extreme")):
            value = valid_strategy(self.state)
            value["actions"] = [{"action_id": "w", "type": "water", "pump_seconds": seconds}]
            self.assertIn(f"action[0]:{code}", self.validator.validate(value, self.state).reason_codes)

    def test_wrong_type_is_rejected(self):
        value = valid_strategy(self.state)
        value["actions"] = [{"action_id": "w", "type": "water", "pump_seconds": "3"}]
        self.assertIn("action[0]:invalid_pump_seconds_type", self.validator.validate(value, self.state).reason_codes)

    def test_state_mismatch_is_rejected(self):
        value = valid_strategy(self.state)
        value["state_id"] = str(uuid.uuid4())
        self.assertIn("state_id_mismatch", self.validator.validate(value, self.state).reason_codes)

    def test_action_after_stop_is_rejected(self):
        value = valid_strategy(self.state)
        value["actions"] = [
            {"action_id": "s", "type": "stop"},
            {"action_id": "o", "type": "observe"},
        ]
        self.assertIn("action[1]:action_after_stop", self.validator.validate(value, self.state).reason_codes)

    def test_unknown_top_level_field_is_rejected(self):
        value = valid_strategy(self.state)
        value["mqtt_topic"] = "forbidden"
        result = self.validator.validate(value, self.state)
        self.assertIn("unknown_top_level_fields:mqtt_topic", result.reason_codes)

    def test_prompt_version_must_match(self):
        value = valid_strategy(self.state)
        value["model"]["prompt_version"] = "unknown"
        self.assertIn("invalid_model_field:prompt_version", self.validator.validate(value, self.state).reason_codes)

    def test_runtime_config_cannot_widen_protocol_limits(self):
        with self.assertRaises(ValueError):
            StrategyValidator(max_pump_seconds=121)
        with self.assertRaises(ValueError):
            StrategyValidator(max_wait_seconds=86401)
        with self.assertRaises(ValueError):
            StrategyValidator(max_actions=13)


class ClientAndChainTests(unittest.TestCase):
    def config(self):
        return {
            "enabled": True,
            "provider": "test",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "timeout_seconds": 1,
            "max_retries": 0,
            "temperature": 0,
            "max_tokens": 200,
            "json_response_format": True,
            "validator": {"max_actions": 12, "max_pump_seconds": 120, "max_wait_seconds": 86400},
        }

    def test_client_requires_key(self):
        with self.assertRaises(CloudStrategyError) as context:
            OpenAICompatibleClient(self.config(), "").complete("prompt", sample_state())
        self.assertEqual("missing_api_key", context.exception.code)

    def test_client_parses_openai_compatible_response(self):
        state = sample_state()
        content = json.dumps(valid_strategy(state))
        session = FakeSession(
            FakeResponse(
                200,
                {
                    "id": "request-1",
                    "model": "test-model",
                    "choices": [{"message": {"content": content}}],
                    "usage": {"total_tokens": 10},
                },
            )
        )
        response = OpenAICompatibleClient(self.config(), "secret", session=session).complete("prompt", state)
        self.assertEqual(content, response.content)
        self.assertEqual(1, len(session.calls))

    def test_client_maps_quota_error(self):
        session = FakeSession(FakeResponse(403, {"error": {"message": "quota"}}))
        with self.assertRaises(CloudStrategyError) as context:
            OpenAICompatibleClient(self.config(), "secret", session=session).complete("prompt", sample_state())
        self.assertEqual("model_quota_or_permission_denied", context.exception.code)

    def test_invalid_json_is_rejected(self):
        self.assertRaises(json.JSONDecodeError, parse_model_json, "not json")
        with self.assertRaisesRegex(ValueError, "markdown_fence_not_allowed"):
            parse_model_json("```json\n{}\n```")

    def test_fixture_chain_is_accepted_and_audited(self):
        state = sample_state()
        result = run_chain(
            state=state,
            config=self.config(),
            prompt="prompt",
            fixture_content=json.dumps(valid_strategy(state)),
        )
        self.assertTrue(result["validation"]["accepted"])
        self.assertFalse(result["actuator_commands_allowed"])
        self.assertEqual(state, result["state_snapshot"])
        with tempfile.TemporaryDirectory() as directory:
            path = append_audit(Path(directory), result)
            self.assertEqual(1, len(path.read_text(encoding="utf-8").splitlines()))

    def test_disabled_provider_fails_closed(self):
        state = sample_state()
        config = copy.deepcopy(self.config())
        config["enabled"] = False
        result = run_chain(state=state, config=config, prompt="prompt")
        self.assertFalse(result["validation"]["accepted"])
        self.assertEqual(["provider_disabled"], result["validation"]["reason_codes"])


class BoundaryTests(unittest.TestCase):
    def test_schema_and_example_config_are_valid_json(self):
        schema_path = (
            ROOT
            / "services"
            / "soil3"
            / "cloud_strategy"
            / "strategy.v1.schema.json"
        )
        config_path = ROOT / "config" / "cloud_strategy.example.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "strategy.v1",
            schema["properties"]["schema_version"]["const"],
        )
        self.assertFalse(json.loads(config_path.read_text(encoding="utf-8"))["enabled"])

    def test_module_contains_no_actuator_or_mqtt_publish_call(self):
        module = ROOT / "services" / "soil3" / "cloud_strategy"
        source = "\n".join(path.read_text(encoding="utf-8") for path in module.glob("*.py"))
        self.assertNotIn(".publish(", source)
        self.assertNotIn("mosquitto_pub", source)
        self.assertNotIn("manual_water", source)
        self.assertNotIn("ActionPlan", source)


if __name__ == "__main__":
    unittest.main()
