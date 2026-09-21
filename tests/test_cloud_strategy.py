import copy
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from services.soil3.cloud_strategy.client import CloudStrategyError, OpenAICompatibleClient
from services.soil3.cloud_strategy.service import (
    AUDIT_RAW_RESPONSE_MAX_CHARS,
    MODEL_INPUT_SAFETY_FLAGS,
    MODEL_INPUT_SCALARS,
    MODEL_INPUT_SECTIONS,
    MODEL_INPUT_TIMESTAMPS,
    append_audit,
    bind_model_response,
    parse_model_json,
    project_state_for_model,
    run_chain,
    write_json_atomic,
)
from services.soil3.cloud_strategy.validator import (
    ALLOWED_EXECUTION_FIELDS,
    ALLOWED_EXPECTED_OUTCOME_FIELDS,
    ALLOWED_MODEL_FIELDS,
    CANONICAL_TIMESTAMP_FIELDS,
    PROMPT_VERSION,
    PROTOCOL_MAX_ACTIONS,
    PROTOCOL_MAX_PUMP_SECONDS,
    PROTOCOL_MAX_REASON_SUMMARY_ITEMS,
    PROTOCOL_MAX_TOTAL_PUMP_SECONDS,
    PROTOCOL_MAX_TOTAL_SECONDS,
    PROTOCOL_MAX_WAIT_SECONDS,
    REQUIRED_EXPECTED_OUTCOME_FIELDS,
    REQUIRED_FIELDS,
    REQUIRED_MODEL_FIELDS,
    STATE_BINDINGS,
    STATE_HASH_FIELD,
    StrategyValidator,
    fingerprint,
    is_canonical_timestamp,
    is_state_hash,
    normalize_timestamp,
    parse_timestamp,
)
from services.soil3.state.state_v1 import PHASE3_SAFETY_FLAG_KEYS, StateBuilder


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "services" / "soil3" / "cloud_strategy" / "strategy.v1.schema.json"
PROMPT_PATH = ROOT / "services" / "soil3" / "cloud_strategy" / "prompts" / "strategy_v1.txt"
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


def real_state(soil_humidity=31.4):
    """A state.v1 record from the actual producer, not a hand-made look-alike.

    strategy.v1 binds to fields state.v1 really writes, so every fixture here goes
    through StateBuilder: if the producer changes shape, these tests notice. The
    humidity argument builds a second genuine record for the same device and the
    same observation moment, which is the case a content hash exists to separate.
    """
    return StateBuilder("soil3").build(
        {
            "observed_at": "2026-09-16T10:00:00Z",
            "generated_at": "2026-09-16T10:00:01Z",
            "sensor_readings": [
                {"timestamp": "2026-09-16T09:55:00Z", "humidity": soil_humidity, "temperature": 24.1, "ec_raw": 680, "lux": 1200},
                {"timestamp": "2026-09-16T07:00:00Z", "humidity": 33.0, "temperature": 22.0},
            ],
            "system_state": {
                "pump_active": False,
                "pump_total_cycles": 42,
                "total_water_sec_dispensed": 3100,
                "sensor_fault": 0,
                "hard_safety_low_guard": 1,
            },
            "watering_history": [{"timestamp": "2026-09-15T08:00:00Z", "water_sec": 60}],
            "environment": {
                "air": {
                    "humidity_percent": 58.0,
                    "temperature_c": 26.4,
                    "observed_at": "2026-09-16T09:50:00Z",
                    "source": "environment.air",
                }
            },
            "parameters": {"FC": 38.0, "TARGET_LOW": 40.0, "HARD_SAFETY_LOW": 25.0, "K_P": 1.2},
        }
    )


def sample_state():
    return real_state()


def valid_strategy(state):
    return {
        "schema_version": "strategy.v1",
        "strategy_id": str(uuid.uuid4()),
        "device_code": state["device_code"],
        "state_observed_at": state["observed_at"],
        "state_generated_at": state["generated_at"],
        STATE_HASH_FIELD: fingerprint(state),
        "created_at": "2026-09-16T10:00:00Z",
        "actions": [
            {"action_id": "a1", "type": "observe"},
            {"action_id": "a2", "type": "wait", "seconds": 1200},
            {"action_id": "a3", "type": "stop"},
        ],
        "reason_summary": ["soil_below_target_low", "cloud_strategy_shadow_only"],
        "expected_outcome": {"soil_moisture": "continue_observation", "risk_notes": []},
        "confidence": 0.78,
        "model": {"provider": "fixture", "name": "fixture", "prompt_version": PROMPT_VERSION},
        "execution": {"mode": "proposal_only", "actuator_commands_allowed": False},
    }


def chain_config(**overrides):
    config = {
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
    config.update(overrides)
    return config


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
        value["state_observed_at"] = "2026-01-01T00:00:00Z"
        self.assertIn("state_observed_at_mismatch", self.validator.validate(value, self.state).reason_codes)

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
        return chain_config()

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
        self.assertEqual(project_state_for_model(state), result["model_input"])
        self.assertEqual(fingerprint(state), result["state_sha256"])
        self.assertNotIn("state_snapshot", result)
        self.assertEqual(state["observed_at"], result["state_observed_at"])
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
    def test_protocol_doc_states_binding_runtime_consumer_and_provider_status(self):
        """The lifecycle legend and this module's contract must not drift apart again."""
        doc = (ROOT / "docs" / "PROTOCOLS_AND_BOUNDARIES.md").read_text(encoding="utf-8")
        row = next(line for line in doc.splitlines() if line.startswith("| `strategy.v1`"))
        self.assertIn("implemented", row)
        self.assertIn("runtime consumes", row)
        self.assertIn("provider configuration remains disabled", row)
        for field in ("device_code", "`observed_at`", "state_sha256"):
            self.assertIn(field, row)
        self.assertIn("not a binding condition", row)
        legend = next(line for line in doc.splitlines() if line.startswith("“Implemented”"))
        self.assertIn("automated tests", legend)
        self.assertNotIn("verified against real data", legend)

    def test_the_three_descriptions_of_the_binding_agree(self):
        """Doc row, module README, and schema notes must state one binding, not three."""
        doc = (ROOT / "docs" / "PROTOCOLS_AND_BOUNDARIES.md").read_text(encoding="utf-8")
        readme = (ROOT / "services" / "soil3" / "cloud_strategy" / "README.md").read_text(
            encoding="utf-8"
        )
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        notes = schema["x-state-binding-notes"]
        for text in (doc, readme, notes):
            for field in ("device_code", "observed_at", STATE_HASH_FIELD):
                self.assertIn(field, text)
            # demoted in every description, not just in code
            self.assertIn("not a binding condition", text)
        for field, _ in STATE_BINDINGS:
            self.assertIn(field, notes)

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
    """A proposal must name the snapshot it came from, using fields that exist."""

    def setUp(self):
        self.state = sample_state()
        self.validator = StrategyValidator()

    def test_real_producer_state_is_bindable(self):
        result = self.validator.validate(valid_strategy(self.state), self.state)
        self.assertEqual([], result.reason_codes)
        self.assertTrue(result.accepted)

    def test_wrong_device_code_is_rejected(self):
        value = valid_strategy(self.state)
        value["device_code"] = "soil2"
        self.assertIn("invalid_device_code", self.validator.validate(value, self.state).reason_codes)

    def test_state_from_another_device_is_rejected(self):
        state = copy.deepcopy(self.state)
        state["device_code"] = "soil3-backup"
        value = valid_strategy(self.state)
        result = self.validator.validate(value, state)
        self.assertFalse(result.accepted)
        self.assertIn("state_is_not_soil3", result.reason_codes)

    def test_observed_at_mismatch_is_rejected(self):
        value = valid_strategy(self.state)
        value["state_observed_at"] = "2026-09-16T11:00:00Z"
        self.assertIn("state_observed_at_mismatch", self.validator.validate(value, self.state).reason_codes)

    def test_generated_at_is_traceability_not_a_binding_condition(self):
        """A strategy may name any generated_at and still bind: the hash is what pins it."""
        value = valid_strategy(self.state)
        value["state_generated_at"] = "2026-09-16T13:00:00Z"
        result = self.validator.validate(value, self.state)
        self.assertTrue(result.accepted)
        self.assertNotIn("state_generated_at_mismatch", result.reason_codes)

    def test_a_different_generated_at_is_still_caught_by_the_hash(self):
        """Dropping the comparison left no hole: another snapshot hashes differently."""
        other = copy.deepcopy(self.state)
        other["generated_at"] = "2026-09-16T13:00:00Z"
        self.assertNotEqual(fingerprint(self.state), fingerprint(other))
        result = self.validator.validate(valid_strategy(self.state), other)
        self.assertFalse(result.accepted)
        self.assertIn(f"{STATE_HASH_FIELD}_mismatch", result.reason_codes)
        for field, _ in STATE_BINDINGS:
            self.assertNotIn(f"{field}_mismatch", result.reason_codes)

    def test_null_binding_field_is_rejected(self):
        for field, code in (
            ("state_observed_at", "invalid_state_observed_at"),
            ("state_generated_at", "invalid_state_generated_at"),
        ):
            with self.subTest(field=field):
                value = valid_strategy(self.state)
                value[field] = None
                self.assertIn(code, self.validator.validate(value, self.state).reason_codes)

    def test_state_without_usable_timestamp_is_rejected(self):
        for _, state_key in STATE_BINDINGS:
            with self.subTest(state_key=state_key):
                state = copy.deepcopy(self.state)
                state[state_key] = None
                value = valid_strategy(self.state)
                self.assertIn(
                    f"state_has_no_usable_{state_key}",
                    self.validator.validate(value, state).reason_codes,
                )

    def test_naive_timestamp_is_not_a_binding(self):
        for field in CANONICAL_TIMESTAMP_FIELDS:
            with self.subTest(field=field):
                value = valid_strategy(self.state)
                value[field] = "2026-09-16T10:00:00"
                self.assertIn(
                    f"invalid_{field}", self.validator.validate(value, self.state).reason_codes
                )

    def test_equivalent_instant_in_another_notation_is_malformed(self):
        """strategy.v1 carries one notation, so this is a shape defect, not a mismatch."""
        value = valid_strategy(self.state)
        value["state_observed_at"] = "2026-09-16T18:00:00+08:00"
        result = self.validator.validate(value, self.state)
        self.assertIn("invalid_state_observed_at", result.reason_codes)
        self.assertNotIn("state_observed_at_mismatch", result.reason_codes)

    def test_epoch_observed_at_from_producer_binds(self):
        """state.v1 copies observed_at through untouched, so it can arrive as an epoch."""
        state = copy.deepcopy(self.state)
        state["observed_at"] = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc).timestamp()
        value = valid_strategy(self.state)
        result = self.validator.validate(value, state)
        self.assertNotIn("state_observed_at_mismatch", result.reason_codes)
        self.assertNotIn("state_has_no_usable_observed_at", result.reason_codes)

    def test_unparseable_observed_at_is_not_a_binding(self):
        state = copy.deepcopy(self.state)
        state["observed_at"] = "yesterday"
        value = valid_strategy(self.state)
        self.assertIn(
            "state_has_no_usable_observed_at",
            self.validator.validate(value, state).reason_codes,
        )

    def test_every_declared_binding_pair_targets_a_real_state_field(self):
        self.assertEqual([("state_observed_at", "observed_at")], list(STATE_BINDINGS))
        self.assertNotIn("state_generated_at", dict(STATE_BINDINGS))
        for _, state_key in STATE_BINDINGS:
            self.assertIn(state_key, self.state)


class SnapshotContentBindingTests(unittest.TestCase):
    """(device_code, observed_at) is unique in practice, not by contract: the hash pins the record."""

    def setUp(self):
        self.state = sample_state()
        self.validator = StrategyValidator()

    def test_strategy_without_a_content_binding_is_incomplete(self):
        value = valid_strategy(self.state)
        del value[STATE_HASH_FIELD]
        self.assertIn(
            f"missing_field:{STATE_HASH_FIELD}",
            self.validator.validate(value, self.state).reason_codes,
        )

    def test_malformed_content_binding_is_rejected(self):
        # the service emits lowercase hex, so anything else is not a binding either
        for candidate in (None, "", "deadbeef", "0" * 63, "0" * 64 + "0", "F" * 64, 12345, ["a"]):
            with self.subTest(candidate=repr(candidate)[:14]):
                value = valid_strategy(self.state)
                value[STATE_HASH_FIELD] = candidate
                self.assertIn(
                    f"invalid_{STATE_HASH_FIELD}",
                    self.validator.validate(value, self.state).reason_codes,
                )

    def test_two_snapshots_of_the_same_moment_are_told_apart(self):
        """The case state.v1 cannot express: same device, same instant, different facts."""
        other = real_state(soil_humidity=44.0)
        self.assertEqual(self.state["device_code"], other["device_code"])
        self.assertEqual(self.state["observed_at"], other["observed_at"])
        self.assertEqual(self.state["generated_at"], other["generated_at"])
        self.assertNotEqual(fingerprint(self.state), fingerprint(other))

        value = valid_strategy(self.state)
        result = self.validator.validate(value, other)
        self.assertFalse(result.accepted)
        self.assertIn(f"{STATE_HASH_FIELD}_mismatch", result.reason_codes)
        for field, _ in STATE_BINDINGS:
            self.assertNotIn(f"{field}_mismatch", result.reason_codes)

    def test_a_proposal_only_binds_the_record_its_hash_came_from(self):
        other = real_state(soil_humidity=44.0)
        for left, right in ((other, self.state), (self.state, other)):
            with self.subTest(bound_to=str(fingerprint(left))[:8]):
                value = valid_strategy(left)
                self.assertTrue(self.validator.validate(value, left).accepted)
                self.assertIn(
                    f"{STATE_HASH_FIELD}_mismatch",
                    self.validator.validate(value, right).reason_codes,
                )

    def test_binding_hash_ignores_key_order_of_the_snapshot(self):
        shuffled = json.loads(json.dumps(dict(reversed(list(self.state.items())))))
        self.assertEqual(fingerprint(self.state), fingerprint(shuffled))
        self.assertTrue(self.validator.validate(valid_strategy(shuffled), self.state).accepted)

    def test_service_attaches_the_hash_when_the_model_omits_it(self):
        state = real_state()
        value = valid_strategy(state)
        del value[STATE_HASH_FIELD]
        result = run_chain(
            state=state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(value),
        )
        self.assertEqual([], result["validation"]["reason_codes"])
        self.assertEqual(fingerprint(state), result["validation"]["strategy"][STATE_HASH_FIELD])

    def test_a_model_supplied_hash_is_overwritten_not_trusted(self):
        state = real_state()
        value = valid_strategy(state)
        value[STATE_HASH_FIELD] = "0" * 64
        result = run_chain(
            state=state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(value),
        )
        strategy = result["validation"]["strategy"]
        self.assertEqual([], result["validation"]["reason_codes"])
        self.assertEqual(fingerprint(state), strategy[STATE_HASH_FIELD])
        # the attempt itself stays auditable in the stored provider text
        self.assertIn("0" * 64, result["raw_model_response"])

    def test_a_hash_under_another_name_is_not_a_binding(self):
        state = real_state()
        value = valid_strategy(state)
        del value[STATE_HASH_FIELD]
        value["snapshot_sha256"] = fingerprint(state)
        result = run_chain(
            state=state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(value),
        )
        self.assertIn(
            "unknown_top_level_fields:snapshot_sha256",
            result["validation"]["reason_codes"],
        )

    def test_binding_survives_an_epoch_observed_at_record(self):
        """A real record whose observed_at is a numeric epoch still gets a full binding."""
        state = real_state()
        state["observed_at"] = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc).timestamp()
        value = valid_strategy(state)
        del value[STATE_HASH_FIELD]
        result = run_chain(
            state=state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(value),
        )
        self.assertEqual([], result["validation"]["reason_codes"])
        self.assertEqual(fingerprint(state), result["validation"]["strategy"][STATE_HASH_FIELD])


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


class DescriptiveFieldTests(unittest.TestCase):
    """expected_outcome and model must not become a side channel for control data."""

    def setUp(self):
        self.state = sample_state()
        self.validator = StrategyValidator()

    def test_execution_shaped_keys_cannot_hide_in_expected_outcome(self):
        for key in ("pump_seconds", "gpio", "cmd", "relay", "mqtt_topic", "actuator_commands_allowed"):
            with self.subTest(key=key):
                value = valid_strategy(self.state)
                value["expected_outcome"] = {
                    "soil_moisture": "recovering",
                    "risk_notes": [],
                    key: "on",
                }
                result = self.validator.validate(value, self.state)
                self.assertFalse(result.accepted)
                self.assertIn(f"expected_outcome_unknown_fields:{key}", result.reason_codes)

    def test_expected_outcome_requires_both_declared_keys(self):
        for missing in ("soil_moisture", "risk_notes"):
            with self.subTest(missing=missing):
                value = valid_strategy(self.state)
                del value["expected_outcome"][missing]
                result = self.validator.validate(value, self.state)
                self.assertIn(f"expected_outcome_missing_fields:{missing}", result.reason_codes)

    def test_expected_outcome_field_types_are_enforced(self):
        for field, bad in (
            ("soil_moisture", 12),
            ("soil_moisture", ""),
            ("risk_notes", "single string"),
            ("risk_notes", [12]),
            ("risk_notes", ["   "]),
        ):
            with self.subTest(field=field, bad=bad):
                value = valid_strategy(self.state)
                value["expected_outcome"][field] = bad
                result = self.validator.validate(value, self.state)
                self.assertIn(f"expected_outcome_invalid_field:{field}", result.reason_codes)

    def test_risk_notes_length_is_capped(self):
        value = valid_strategy(self.state)
        value["expected_outcome"]["risk_notes"] = [f"r{i}" for i in range(9)]
        result = self.validator.validate(value, self.state)
        self.assertIn("expected_outcome_risk_notes_too_long", result.reason_codes)

    def test_empty_risk_notes_and_plain_expectation_are_accepted(self):
        value = valid_strategy(self.state)
        value["expected_outcome"] = {"soil_moisture": "holds above hard safety low", "risk_notes": []}
        self.assertEqual([], self.validator.validate(value, self.state).reason_codes)

    def test_execution_shaped_keys_cannot_hide_in_model_metadata(self):
        for key in ("pump_seconds", "gpio", "cmd", "relay", "mqtt_topic"):
            with self.subTest(key=key):
                value = valid_strategy(self.state)
                value["model"][key] = 60
                result = self.validator.validate(value, self.state)
                self.assertFalse(result.accepted)
                self.assertIn(f"model_unknown_fields:{key}", result.reason_codes)


class StateProjectionTests(unittest.TestCase):
    """Only the facts a proposal can use may leave the machine."""

    def setUp(self):
        self.state = real_state()

    def test_projection_carries_only_declared_sections_and_scalars(self):
        projected = project_state_for_model(self.state)
        self.assertEqual(
            set(MODEL_INPUT_SCALARS) & set(self.state), set(projected) - set(MODEL_INPUT_SECTIONS)
        )
        for section, keys in MODEL_INPUT_SECTIONS.items():
            self.assertIn(section, projected)
            self.assertTrue(
                set(projected[section]) <= set(keys) | {"flags"},
                f"{section} carried an undeclared key",
            )

    def test_provenance_and_extension_blocks_are_never_forwarded(self):
        projected = project_state_for_model(self.state)
        for section in ("extensions", "fact_sources", "source_timestamps", "vision"):
            self.assertNotIn(section, projected)

    def test_runtime_paths_in_state_never_reach_the_model(self):
        state = copy.deepcopy(self.state)
        state["extensions"]["water_log"] = "/root/water/phase3.sqlite"
        state["fact_sources"]["soil"] = "/root/water/state.json"
        state["source_timestamps"]["phase3_state"] = "/root/water/state_file.json"
        blob = json.dumps(project_state_for_model(state))
        for secret in ("root", "sqlite", "water_log", "state_file"):
            self.assertNotIn(secret, blob)

    def test_unknown_safety_flags_are_dropped(self):
        state = copy.deepcopy(self.state)
        state["safety"]["flags"]["custom_shell"] = "rm -rf /"
        state["safety"]["flags"]["note"] = "/root/water/relay.json"
        projected = project_state_for_model(state)
        self.assertNotIn("custom_shell", projected["safety"]["flags"])
        self.assertNotIn("note", projected["safety"]["flags"])
        self.assertNotIn("rm -rf", json.dumps(projected))

    def test_safety_flag_allow_list_matches_the_producer(self):
        self.assertEqual(sorted(PHASE3_SAFETY_FLAG_KEYS), sorted(MODEL_INPUT_SAFETY_FLAGS))

    def test_decision_relevant_flags_survive_projection(self):
        flags = project_state_for_model(self.state)["safety"]["flags"]
        self.assertIn("pump_active", flags)
        self.assertIn("hard_safety_low_guard", flags)

    def test_projection_is_not_a_control_channel(self):
        blob = json.dumps(project_state_for_model(self.state))
        for token in ("pump_seconds", "gpio", "relay", "mqtt", "cmd", "manual_water", "action"):
            self.assertNotIn(token, blob)

    def test_projection_ignores_sections_the_state_does_not_have(self):
        state = {"schema_version": "state.v1", "device_code": "soil3", "soil": "not-a-dict"}
        self.assertEqual(
            {"schema_version": "state.v1", "device_code": "soil3"},
            project_state_for_model(state),
        )

    def test_projection_hands_the_model_the_canonical_timestamp_form(self):
        """state.v1 may hold any notation; the one a proposal copies back is fixed."""
        state = copy.deepcopy(self.state)
        state["observed_at"] = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc).timestamp()
        state["generated_at"] = "2026-09-16T18:00:01+08:00"
        projected = project_state_for_model(state)
        for key in MODEL_INPUT_TIMESTAMPS:
            with self.subTest(key=key):
                self.assertTrue(is_canonical_timestamp(projected[key]))
                # rewriting the notation must not move the instant it names
                self.assertEqual(parse_timestamp(state[key]), parse_timestamp(projected[key]))

    def test_projection_does_not_invent_an_unparseable_timestamp(self):
        """Leaving a bad value alone lets the Validator report it as the defect it is."""
        state = copy.deepcopy(self.state)
        state["observed_at"] = "yesterday"
        self.assertEqual("yesterday", project_state_for_model(state)["observed_at"])


class TimestampCanonicalizationTests(unittest.TestCase):
    """strategy.v1 carries one timestamp notation; the service produces it at both ends."""

    def setUp(self):
        self.state = real_state()
        self.validator = StrategyValidator()

    def test_every_notation_of_one_instant_normalizes_to_the_producer_form(self):
        expected = "2026-09-16T10:00:00Z"
        for candidate in (
            expected,
            "2026-09-16T10:00:00+00:00",
            "2026-09-16T18:00:00+08:00",
            "2026-09-16T05:00:00-05:00",
            "2026-09-16T10:00:00.000Z",
            datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc).timestamp(),
            int(datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc).timestamp()),
        ):
            with self.subTest(candidate=str(candidate)):
                self.assertEqual(expected, normalize_timestamp(candidate))
                self.assertTrue(is_canonical_timestamp(normalize_timestamp(candidate)))

    def test_no_instant_is_made_up_from_an_unusable_value(self):
        for candidate in (None, True, "", "   ", "yesterday", "2026-09-16T10:00:00", 10 ** 400):
            with self.subTest(candidate=repr(candidate)[:14]):
                self.assertIsNone(normalize_timestamp(candidate))
                self.assertFalse(is_canonical_timestamp(candidate))

    def test_the_validator_accepts_only_the_canonical_notation(self):
        for field in CANONICAL_TIMESTAMP_FIELDS:
            for candidate in (
                "2026-09-16T10:00:00+00:00",
                "2026-09-16T18:00:00+08:00",
                "2026-09-16T10:00:00",
                1789000000,
            ):
                with self.subTest(field=field, candidate=str(candidate)):
                    value = valid_strategy(self.state)
                    value[field] = candidate
                    self.assertIn(
                        f"invalid_{field}",
                        self.validator.validate(value, self.state).reason_codes,
                    )

    def test_a_provider_written_in_another_offset_is_bound_after_normalization(self):
        """The instant was right and only the notation was not: the chain fixes it."""
        value = valid_strategy(self.state)
        value["state_observed_at"] = "2026-09-16T18:00:00+08:00"
        value["created_at"] = "2026-09-16T18:00:05+08:00"
        result = run_chain(
            state=self.state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(value),
        )
        self.assertEqual([], result["validation"]["reason_codes"])
        strategy = result["validation"]["strategy"]
        self.assertEqual("2026-09-16T10:00:00Z", strategy["state_observed_at"])
        self.assertEqual("2026-09-16T10:00:05Z", strategy["created_at"])

    def test_an_unusable_provider_timestamp_survives_to_be_rejected(self):
        value = valid_strategy(self.state)
        value["state_observed_at"] = "yesterday"
        result = run_chain(
            state=self.state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(value),
        )
        # reported as the defect it is, rather than papered over with the snapshot's own
        self.assertEqual(["invalid_state_observed_at"], result["validation"]["reason_codes"])
        self.assertIn("yesterday", result["raw_model_response"])

    def test_the_chain_record_still_mirrors_the_snapshot_it_loaded(self):
        """Canonicalization belongs to strategy.v1; the audit record reports the source."""
        state = copy.deepcopy(self.state)
        state["observed_at"] = "2026-09-16T18:00:00+08:00"
        result = run_chain(
            state=state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(valid_strategy(self.state)),
        )
        self.assertEqual([], result["validation"]["reason_codes"])
        self.assertEqual("2026-09-16T18:00:00+08:00", result["state_observed_at"])
        self.assertEqual("2026-09-16T10:00:00Z", result["validation"]["strategy"]["state_observed_at"])


class AuditRecordTests(unittest.TestCase):
    """Traceable without persisting unbounded, unvalidated model output."""

    def test_short_response_is_stored_verbatim_with_a_fingerprint(self):
        text = '{"a":1}'
        bound = bind_model_response(text)
        self.assertEqual(text, bound["raw_model_response"])
        self.assertFalse(bound["raw_model_response_truncated"])
        self.assertEqual(len(text), bound["raw_model_response_chars"])
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), bound["raw_model_response_sha256"])

    def test_oversized_response_is_bounded_but_still_identifiable(self):
        text = "x" * (AUDIT_RAW_RESPONSE_MAX_CHARS + 5000)
        bound = bind_model_response(text)
        self.assertEqual(AUDIT_RAW_RESPONSE_MAX_CHARS, len(bound["raw_model_response"]))
        self.assertTrue(bound["raw_model_response_truncated"])
        self.assertEqual(len(text), bound["raw_model_response_chars"])
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), bound["raw_model_response_sha256"])

    def test_absent_response_hashes_the_empty_string(self):
        bound = bind_model_response(None)
        self.assertEqual("", bound["raw_model_response"])
        self.assertEqual(0, bound["raw_model_response_chars"])
        self.assertEqual(hashlib.sha256(b"").hexdigest(), bound["raw_model_response_sha256"])

    def test_chain_record_stores_projection_not_full_snapshot(self):
        state = real_state()
        state["extensions"]["water_log"] = "/root/water/phase3.sqlite"
        result = run_chain(
            state=state,
            config=chain_config(),
            prompt="prompt",
            fixture_content="x" * (AUDIT_RAW_RESPONSE_MAX_CHARS + 10),
        )
        self.assertNotIn("state_snapshot", result)
        self.assertNotIn("water_log", json.dumps(result["model_input"]))
        self.assertTrue(result["raw_model_response_truncated"])
        self.assertEqual(64, len(result["state_sha256"]))

    def test_fingerprint_is_stable_and_content_sensitive(self):
        first = fingerprint({"b": 1, "a": [1, 2]})
        self.assertEqual(first, fingerprint({"a": [1, 2], "b": 1}))
        self.assertNotEqual(first, fingerprint({"a": [1, 2], "b": 2}))


class NoRequestSession:
    def post(self, *args, **kwargs):
        raise AssertionError("a malformed config must fail before any request is sent")


class ProviderConfigTests(unittest.TestCase):
    """A broken runtime config fails closed with a code, not a bare traceback."""

    MALFORMED = {
        "missing base_url": {"del": "base_url"},
        "missing model": {"del": "model"},
        "blank base_url": {"base_url": "   "},
        "non-string base_url": {"base_url": 42},
        "null timeout": {"timeout_seconds": None},
        "non-numeric timeout": {"timeout_seconds": "soon"},
        "zero timeout": {"timeout_seconds": 0},
        "infinite timeout": {"timeout_seconds": float("inf")},
        "non-numeric retries": {"max_retries": "lots"},
        "non-numeric temperature": {"temperature": "warm"},
        "zero max_tokens": {"max_tokens": 0},
    }

    def test_every_malformed_config_yields_one_stable_reason_code(self):
        state = real_state()
        for label, override in self.MALFORMED.items():
            with self.subTest(config=label):
                config = chain_config()
                if "del" in override:
                    del config[override["del"]]
                else:
                    config.update(override)
                with mock.patch.dict(os.environ, {"CLOUD_STRATEGY_API_KEY": "secret"}):
                    result = run_chain(
                        state=state, config=config, prompt="prompt", session=NoRequestSession()
                    )
                self.assertFalse(result["validation"]["accepted"])
                self.assertEqual(["invalid_provider_config"], result["validation"]["reason_codes"])
                self.assertEqual("invalid_provider_config", result["failure"]["code"])

    def test_complete_raises_the_wrapped_error_type(self):
        client = OpenAICompatibleClient(chain_config(base_url=None), "secret", session=NoRequestSession())
        with self.assertRaises(CloudStrategyError) as context:
            client.complete("prompt", project_state_for_model(real_state()))
        self.assertEqual("invalid_provider_config", context.exception.code)

    def test_valid_config_still_reaches_the_provider(self):
        state = real_state()
        session = FakeSession(FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}))
        client = OpenAICompatibleClient(chain_config(), "secret", session=session)
        client.complete("prompt", project_state_for_model(state))
        self.assertEqual(1, len(session.calls))

    def test_broken_validator_config_remains_distinct(self):
        config = chain_config(validator={"max_actions": 9999})
        with mock.patch.dict(os.environ, {"CLOUD_STRATEGY_API_KEY": "secret"}):
            result = run_chain(
                state=real_state(),
                config=config,
                prompt="prompt",
                fixture_content=json.dumps(valid_strategy(real_state())),
            )
        self.assertEqual(["invalid_validator_config"], result["validation"]["reason_codes"])


class RealChainIntegrationTests(unittest.TestCase):
    """The chain on the producer's own record shape, not a hand-made look-alike."""

    def test_chain_closes_on_a_real_state_v1_record(self):
        state = real_state()
        result = run_chain(
            state=state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(valid_strategy(state)),
        )
        self.assertEqual([], result["validation"]["reason_codes"])
        self.assertTrue(result["validation"]["accepted"])
        self.assertEqual("soil3", result["device_code"])
        self.assertEqual(state["observed_at"], result["state_observed_at"])
        self.assertEqual(
            fingerprint(state), result["validation"]["strategy"][STATE_HASH_FIELD]
        )

    def test_proposal_built_for_another_snapshot_is_rejected(self):
        state = real_state()
        stale = copy.deepcopy(state)
        stale["observed_at"] = "2026-09-16T02:00:00Z"
        result = run_chain(
            state=stale,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(valid_strategy(state)),
        )
        self.assertIn("state_observed_at_mismatch", result["validation"]["reason_codes"])

    def test_chain_binding_always_names_the_record_it_loaded(self):
        """Two records with identical identity fields cannot be interchanged downstream."""
        wet = real_state(soil_humidity=44.0)
        dry = real_state(soil_humidity=12.0)
        self.assertNotEqual(fingerprint(wet), fingerprint(dry))
        result = run_chain(
            state=dry,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(valid_strategy(wet)),
        )
        strategy = result["validation"]["strategy"]
        self.assertTrue(result["validation"]["accepted"])
        self.assertEqual(fingerprint(dry), strategy[STATE_HASH_FIELD])
        self.assertNotEqual(fingerprint(wet), strategy[STATE_HASH_FIELD])

    def test_audit_round_trip_of_a_real_chain_run(self):
        state = real_state()
        result = run_chain(
            state=state,
            config=chain_config(),
            prompt="prompt",
            fixture_content=json.dumps(valid_strategy(state)),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = append_audit(Path(directory), result)
            stored = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(stored["validation"]["accepted"])
        self.assertEqual(state["generated_at"], stored["state_generated_at"])
        self.assertEqual(fingerprint(state), stored["state_sha256"])

    def test_binding_fields_exist_in_producer_output(self):
        state = real_state()
        for _, state_key in STATE_BINDINGS:
            self.assertIn(state_key, state)
        self.assertEqual("soil3", state["device_code"])


class PromptVersionTests(unittest.TestCase):
    """Prompt text and its version label must not drift apart again."""

    def test_schema_pin_matches_the_validator_constant(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(PROMPT_VERSION, schema["properties"]["model"]["properties"]["prompt_version"]["const"])

    def test_prompt_file_declares_its_own_version(self):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn(f'prompt_version="{PROMPT_VERSION}"', text)
        self.assertNotIn("strategy-prompt.v1", text)

    def test_prompt_file_describes_the_binding_fields_it_requires(self):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        for field, _ in STATE_BINDINGS:
            self.assertIn(field, text)
        self.assertIn("device_code", text)
        self.assertNotIn("state_id", text)
        self.assertNotIn("plant_id", text)

    def test_prompt_file_forbids_the_model_from_writing_the_content_binding(self):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn(STATE_HASH_FIELD, text)
        self.assertIn(f"Never write {STATE_HASH_FIELD}", text)

    def test_prompt_file_states_the_one_timestamp_form(self):
        text = PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("RFC 3339 UTC", text)
        self.assertIn("2026-09-16T10:00:05Z", text)

    def test_previous_prompt_version_is_no_longer_accepted(self):
        state = real_state()
        value = valid_strategy(state)
        value["model"]["prompt_version"] = "strategy-prompt.v1"
        self.assertIn(
            "invalid_model_field:prompt_version",
            StrategyValidator().validate(value, state).reason_codes,
        )


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
                "max_reason_summary_items": PROTOCOL_MAX_REASON_SUMMARY_ITEMS,
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
        self.assertIs(False, self.schema["properties"]["model"]["additionalProperties"])
        self.assertIs(
            False, self.schema["properties"]["expected_outcome"]["additionalProperties"]
        )
        for variant in self.schema["properties"]["actions"]["items"]["oneOf"]:
            self.assertIs(False, variant["additionalProperties"])

    def test_allowed_key_sets_match_the_validator(self):
        self.assertEqual(
            set(self.schema["properties"]["execution"]["properties"]),
            ALLOWED_EXECUTION_FIELDS,
        )
        self.assertEqual(
            set(self.schema["properties"]["model"]["properties"]), ALLOWED_MODEL_FIELDS
        )
        self.assertEqual(
            set(self.schema["properties"]["model"]["required"]), REQUIRED_MODEL_FIELDS
        )
        self.assertEqual(
            set(self.schema["properties"]["expected_outcome"]["properties"]),
            ALLOWED_EXPECTED_OUTCOME_FIELDS,
        )
        self.assertEqual(
            set(self.schema["properties"]["expected_outcome"]["required"]),
            REQUIRED_EXPECTED_OUTCOME_FIELDS,
        )
        self.assertEqual("string", self.schema["properties"]["reason_summary"]["items"]["type"])
        self.assertEqual(
            PROTOCOL_MAX_REASON_SUMMARY_ITEMS, self.schema["properties"]["reason_summary"]["maxItems"]
        )
        self.assertEqual(
            PROTOCOL_MAX_REASON_SUMMARY_ITEMS,
            self.schema["properties"]["expected_outcome"]["properties"]["risk_notes"]["maxItems"],
        )

    def test_required_fields_are_the_same_set_in_both_places(self):
        self.assertEqual(sorted(REQUIRED_FIELDS), sorted(self.schema["required"]))
        self.assertEqual(set(self.schema["properties"]), set(REQUIRED_FIELDS))

    def timestamp_rule(self, field):
        """Resolve the schema node a timestamp field declares, following a $defs ref."""
        node = self.schema["properties"][field]
        ref = node.get("$ref", "")
        if ref.startswith("#/$defs/"):
            return self.schema["$defs"][ref[len("#/$defs/") :]]
        return node

    def test_the_single_timestamp_form_is_declared_once_and_shared(self):
        declared = {
            field: self.schema["properties"][field].get("$ref")
            for field in CANONICAL_TIMESTAMP_FIELDS
        }
        self.assertEqual({"#/$defs/rfc3339_utc"}, set(declared.values()), declared)

    def test_the_timestamp_pattern_is_the_form_the_validator_accepts(self):
        """Schema and Validator must not disagree about which notation is legal again."""
        pattern = self.timestamp_rule("created_at")["pattern"]
        self.assertEqual("string", self.timestamp_rule("state_observed_at")["type"])
        for candidate in ("2026-09-16T10:00:00Z", "2026-09-16T10:00:00.500000Z"):
            with self.subTest(accepted=candidate):
                self.assertTrue(is_canonical_timestamp(candidate))
                self.assertRegex(candidate, pattern)
        for candidate in (
            "2026-09-16T10:00:00+00:00",
            "2026-09-16T18:00:00+08:00",
            "2026-09-16T10:00:00",
            "2026-09-16T10:00:00.5Z",
            "2026-09-16T10:00:00.500Z",
            "2026-09-16T10:00:00z",
            "1789000000",
            1789000000,
        ):
            with self.subTest(rejected=str(candidate)):
                self.assertFalse(is_canonical_timestamp(candidate))
                if isinstance(candidate, str):
                    self.assertNotRegex(candidate, pattern)

    def test_content_binding_is_declared_the_same_way_in_both_places(self):
        self.assertIn(STATE_HASH_FIELD, REQUIRED_FIELDS)
        declared = self.schema["properties"][STATE_HASH_FIELD]
        self.assertEqual("string", declared["type"])
        # the Validator's shape rule and the schema pattern must not disagree
        for candidate in (fingerprint(real_state()), "0" * 64):
            with self.subTest(candidate=candidate[:8]):
                self.assertTrue(is_state_hash(candidate))
                self.assertRegex(candidate, declared["pattern"])
        for candidate in ("", "0" * 63, "0" * 65, "F" * 64):
            with self.subTest(rejected=candidate[:8] or "empty"):
                self.assertFalse(is_state_hash(candidate))
                self.assertNotRegex(candidate, declared["pattern"])

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
