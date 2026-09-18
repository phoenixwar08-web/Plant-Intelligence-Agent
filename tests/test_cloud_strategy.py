import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from services.soil3.cloud_strategy.client import CloudStrategyError, OpenAICompatibleClient
from services.soil3.cloud_strategy.service import append_audit, parse_model_json, run_chain, write_json_atomic
from services.soil3.cloud_strategy.validator import (
    PROTOCOL_MAX_ACTIONS,
    PROTOCOL_MAX_PUMP_SECONDS,
    PROTOCOL_MAX_TOTAL_PUMP_SECONDS,
    PROTOCOL_MAX_TOTAL_SECONDS,
    PROTOCOL_MAX_WAIT_SECONDS,
    REQUIRED_FIELDS,
    StrategyValidator,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "cloud_strategy" / "strategy.v1.schema.json"
EXAMPLE_CONFIG_PATH = ROOT / "config" / "cloud_strategy.example.json"


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
            "validator": {
                "max_actions": 12,
                "max_pump_seconds": 120,
                "max_wait_seconds": 86400,
                "max_total_pump_seconds": 240,
                "max_total_seconds": 86400,
            },
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


class CumulativeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.state = sample_state()
        self.validator = StrategyValidator()

    def _with_actions(self, actions):
        value = valid_strategy(self.state)
        value["actions"] = actions
        return self.validator.validate(value, self.state)

    def test_repeated_longest_water_is_rejected_by_cumulative_pump_cap(self):
        result = self._with_actions(
            [{"action_id": f"w{i}", "type": "water", "pump_seconds": 120} for i in range(12)]
        )
        self.assertFalse(result.accepted)
        self.assertIn("total_pump_seconds_extreme", result.reason_codes)

    def test_repeated_longest_wait_is_rejected_by_total_duration_cap(self):
        result = self._with_actions(
            [{"action_id": f"t{i}", "type": "wait", "seconds": 86400} for i in range(12)]
        )
        self.assertFalse(result.accepted)
        self.assertIn("total_seconds_extreme", result.reason_codes)

    def test_water_and_wait_seconds_combine_into_total_duration(self):
        result = self._with_actions(
            [
                {"action_id": "w", "type": "water", "pump_seconds": 120},
                {"action_id": "t", "type": "wait", "seconds": 86400},
            ]
        )
        self.assertFalse(result.accepted)
        self.assertIn("total_seconds_extreme", result.reason_codes)

    def test_strategy_inside_cumulative_budget_is_still_accepted(self):
        result = self._with_actions(
            [
                {"action_id": "w1", "type": "water", "pump_seconds": 100},
                {"action_id": "w2", "type": "water", "pump_seconds": 100},
                {"action_id": "t", "type": "wait", "seconds": 3600},
            ]
        )
        self.assertEqual([], result.reason_codes)
        self.assertTrue(result.accepted)

    def test_cumulative_limits_cannot_be_widened_by_config(self):
        with self.assertRaises(ValueError):
            StrategyValidator(max_total_pump_seconds=PROTOCOL_MAX_TOTAL_PUMP_SECONDS + 1)
        with self.assertRaises(ValueError):
            StrategyValidator(max_total_seconds=PROTOCOL_MAX_TOTAL_SECONDS + 1)
        with self.assertRaises(ValueError):
            StrategyValidator(max_pump_seconds=120, max_total_pump_seconds=60)

    def test_cumulative_limits_may_be_tightened_by_config(self):
        validator = StrategyValidator(max_pump_seconds=30, max_wait_seconds=60, max_total_seconds=120)
        value = valid_strategy(self.state)
        value["actions"] = [{"action_id": "a1", "type": "observe"}]
        self.assertTrue(validator.validate(value, self.state).accepted)

    def test_tightened_total_cap_rejects_within_single_action_cap(self):
        validator = StrategyValidator(max_pump_seconds=120, max_total_pump_seconds=150)
        value = valid_strategy(self.state)
        value["actions"] = [
            {"action_id": "w1", "type": "water", "pump_seconds": 100},
            {"action_id": "w2", "type": "water", "pump_seconds": 100},
        ]
        result = validator.validate(value, self.state)
        self.assertFalse(result.accepted)
        self.assertIn("total_pump_seconds_extreme", result.reason_codes)


class NumericSafetyTests(unittest.TestCase):
    def setUp(self):
        self.state = sample_state()
        self.validator = StrategyValidator()

    def _pump(self, seconds):
        value = valid_strategy(self.state)
        value["actions"] = [{"action_id": "w", "type": "water", "pump_seconds": seconds}]
        return self.validator.validate(value, self.state)

    def test_huge_integer_pump_seconds_is_rejected_not_crashing(self):
        result = self._pump(10 ** 400)
        self.assertFalse(result.accepted)
        self.assertIn("action[0]:invalid_pump_seconds_type", result.reason_codes)

    def test_huge_integer_confidence_is_rejected_not_crashing(self):
        value = valid_strategy(self.state)
        value["confidence"] = 10 ** 400
        result = self.validator.validate(value, self.state)
        self.assertFalse(result.accepted)
        self.assertIn("invalid_confidence", result.reason_codes)

    def test_non_finite_numbers_are_rejected(self):
        for seconds in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(seconds=seconds):
                self.assertIn("action[0]:invalid_pump_seconds_type", self._pump(seconds).reason_codes)

    def test_boolean_is_not_a_number(self):
        self.assertIn("action[0]:invalid_pump_seconds_type", self._pump(True).reason_codes)


class IdentityBindingTests(unittest.TestCase):
    def setUp(self):
        self.state = sample_state()
        self.validator = StrategyValidator()

    def test_null_state_id_is_not_a_match(self):
        value = valid_strategy(self.state)
        state = copy.deepcopy(self.state)
        value["state_id"] = None
        state["state_id"] = None
        result = self.validator.validate(value, state)
        self.assertFalse(result.accepted)
        self.assertIn("invalid_state_id", result.reason_codes)

    def test_state_without_usable_state_id_is_rejected(self):
        state = copy.deepcopy(self.state)
        claimed = state["state_id"]
        del state["state_id"]
        value = valid_strategy(self.state)
        value["state_id"] = claimed
        result = self.validator.validate(value, state)
        self.assertFalse(result.accepted)
        self.assertIn("state_has_no_usable_state_id", result.reason_codes)

    def test_blank_state_id_on_either_side_is_rejected(self):
        state = copy.deepcopy(self.state)
        state["state_id"] = "   "
        value = valid_strategy(self.state)
        value["state_id"] = "   "
        self.assertIn("invalid_state_id", self.validator.validate(value, state).reason_codes)

    def test_non_string_state_id_is_rejected(self):
        state = copy.deepcopy(self.state)
        state["state_id"] = 12345
        value = valid_strategy(self.state)
        value["state_id"] = 12345
        self.assertIn("invalid_state_id", self.validator.validate(value, state).reason_codes)


class ExecutionFieldTests(unittest.TestCase):
    def setUp(self):
        self.state = sample_state()
        self.validator = StrategyValidator()

    def test_execution_rejects_smuggled_control_fields(self):
        value = valid_strategy(self.state)
        value["execution"] = {
            "mode": "proposal_only",
            "actuator_commands_allowed": False,
            "mqtt_publish": "soil3/water",
            "relay": "on",
        }
        result = self.validator.validate(value, self.state)
        self.assertFalse(result.accepted)
        self.assertIn("execution_unknown_fields:mqtt_publish,relay", result.reason_codes)

    def test_clean_execution_metadata_is_accepted(self):
        value = valid_strategy(self.state)
        self.assertTrue(self.validator.validate(value, self.state).accepted)


class ProtocolConsistencyTests(unittest.TestCase):
    """The schema and the Validator are two sources of truth; keep them from drifting."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_declared_protocol_limits_match_validator_constants(self):
        declared = self.schema["x-protocol-limits"]
        self.assertEqual(
            {
                "max_actions": PROTOCOL_MAX_ACTIONS,
                "max_pump_seconds": PROTOCOL_MAX_PUMP_SECONDS,
                "max_wait_seconds": PROTOCOL_MAX_WAIT_SECONDS,
                "max_total_pump_seconds": PROTOCOL_MAX_TOTAL_PUMP_SECONDS,
                "max_total_seconds": PROTOCOL_MAX_TOTAL_SECONDS,
            },
            declared,
        )

    def test_field_level_bounds_match_the_declared_limits(self):
        actions = self.schema["properties"]["actions"]
        self.assertEqual(PROTOCOL_MAX_ACTIONS, actions["maxItems"])
        variants = {
            item["properties"]["type"].get("const"): item for item in actions["items"]["oneOf"]
        }
        self.assertEqual(PROTOCOL_MAX_PUMP_SECONDS, variants["water"]["properties"]["pump_seconds"]["maximum"])
        self.assertEqual(PROTOCOL_MAX_WAIT_SECONDS, variants["wait"]["properties"]["seconds"]["maximum"])

    def test_strictness_flags_match_the_validator(self):
        self.assertIs(False, self.schema["additionalProperties"])
        self.assertIs(False, self.schema["properties"]["execution"]["additionalProperties"])
        for variant in self.schema["properties"]["actions"]["items"]["oneOf"]:
            self.assertIs(False, variant["additionalProperties"])

    def test_required_fields_are_the_same_set_in_both_places(self):
        self.assertEqual(sorted(REQUIRED_FIELDS), sorted(self.schema["required"]))
        self.assertEqual(set(self.schema["properties"]), set(REQUIRED_FIELDS))

    def test_example_config_limits_stay_inside_protocol_bounds(self):
        config = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
        limits = config["validator"]
        self.assertFalse(config["enabled"])
        self.assertEqual(
            {
                "max_actions",
                "max_pump_seconds",
                "max_wait_seconds",
                "max_total_pump_seconds",
                "max_total_seconds",
            },
            set(limits),
        )
        self.assertLessEqual(limits["max_actions"], PROTOCOL_MAX_ACTIONS)
        self.assertLessEqual(limits["max_pump_seconds"], PROTOCOL_MAX_PUMP_SECONDS)
        self.assertLessEqual(limits["max_wait_seconds"], PROTOCOL_MAX_WAIT_SECONDS)
        self.assertLessEqual(limits["max_total_pump_seconds"], PROTOCOL_MAX_TOTAL_PUMP_SECONDS)
        self.assertLessEqual(limits["max_total_seconds"], PROTOCOL_MAX_TOTAL_SECONDS)
        StrategyValidator(
            max_actions=limits["max_actions"],
            max_pump_seconds=limits["max_pump_seconds"],
            max_wait_seconds=limits["max_wait_seconds"],
            max_total_pump_seconds=limits["max_total_pump_seconds"],
            max_total_seconds=limits["max_total_seconds"],
        )


class ChainFailureTests(unittest.TestCase):
    def config(self):
        return {
            "enabled": True,
            "provider": "test",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "validator": {
                "max_actions": 12,
                "max_pump_seconds": 120,
                "max_wait_seconds": 86400,
                "max_total_pump_seconds": 240,
                "max_total_seconds": 86400,
            },
        }

    def _run(self, config):
        state = sample_state()
        return run_chain(
            state=state,
            config=config,
            prompt="prompt",
            fixture_content=json.dumps(valid_strategy(state)),
        )

    def test_broken_validator_config_fails_closed_without_traceback(self):
        for mutate, label in (
            (lambda limits: limits.pop("max_total_seconds"), "missing key"),
            (lambda limits: limits.update(max_pump_seconds=121), "widened beyond protocol"),
            (lambda limits: limits.update(max_wait_seconds=10 ** 400), "huge integer"),
            (lambda limits: limits.update(max_actions="many"), "non-numeric"),
        ):
            with self.subTest(case=label):
                config = copy.deepcopy(self.config())
                mutate(config["validator"])
                result = self._run(config)
                self.assertFalse(result["validation"]["accepted"])
                self.assertEqual(["invalid_validator_config"], result["validation"]["reason_codes"])

    def test_accepted_chain_still_reports_no_reason_codes(self):
        result = self._run(self.config())
        self.assertTrue(result["validation"]["accepted"])
        self.assertEqual([], result["validation"]["reason_codes"])

    def test_output_write_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "result.json"
            path = write_json_atomic(target, {"accepted": True})
            self.assertEqual(target, path)
            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["accepted"])
            self.assertEqual([target.name], sorted(p.name for p in path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
