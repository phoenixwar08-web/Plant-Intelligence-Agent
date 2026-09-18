from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .client import CloudResponse, CloudStrategyError, OpenAICompatibleClient
from .validator import STATE_HASH_FIELD, StrategyValidator, fingerprint, parse_timestamp


MODULE_ROOT = Path(__file__).resolve().parent
DEFAULT_PROMPT = MODULE_ROOT / "prompts" / "strategy_v1.txt"
AUDIT_RAW_RESPONSE_MAX_CHARS = 4000

# Only what a watering proposal can reason about. state.v1 additionally carries
# provenance maps, an extension slot, and raw source labels; forwarding those
# would widen what leaves the machine for no decision value.
MODEL_INPUT_SCALARS = ("schema_version", "device_code", "observed_at", "generated_at")
MODEL_INPUT_SECTIONS = {
    "soil": ("humidity_percent", "temperature_c", "ec_raw", "light_lux"),
    "air": ("humidity_percent", "temperature_c"),
    "trends": ("humidity_1h", "humidity_3h", "humidity_6h"),
    "irrigation": ("pump_active", "last_water_at", "last_water_sec", "total_cycles", "total_water_sec"),
    "safety": ("field_capacity", "target_low", "hard_safety_low"),
    "data_quality": ("soil_age_sec", "air_age_sec", "phase3_state_age_sec", "watering_history_age_sec"),
}
# Mirrors the Phase3 flag names state.v1 copies through; a test asserts the two
# lists stay equal so a new flag is a deliberate decision, not a silent leak.
MODEL_INPUT_SAFETY_FLAGS = (
    "pump_active",
    "pending_soak",
    "water_delivery_suspect",
    "reservoir_empty_suspect",
    "low_wet_recovery_suspect",
    "sensor_fault",
    "dynamic_cooldown",
    "predictor_circuit",
    "watering_trigger_guard",
    "recent_response_guard",
    "hard_safety_low_guard",
    "cloud_protection",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_state_for_model(state: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a state.v1 record to the facts a proposal may legitimately use."""
    projected: Dict[str, Any] = {
        key: state[key] for key in MODEL_INPUT_SCALARS if key in state
    }
    for section, keys in MODEL_INPUT_SECTIONS.items():
        block = state.get(section)
        if not isinstance(block, dict):
            continue
        picked = {key: block[key] for key in keys if key in block}
        if section == "safety" and isinstance(block.get("flags"), dict):
            flags = block["flags"]
            picked["flags"] = {key: flags[key] for key in MODEL_INPUT_SAFETY_FLAGS if key in flags}
        if picked:
            projected[section] = picked
    return projected


def bind_model_response(content: Optional[str]) -> Dict[str, Any]:
    """Store a bounded copy of the model text plus a fingerprint of the whole thing.

    An unvalidated response is exactly the artifact that may carry smuggled keys, so
    it is not kept verbatim without limit; the hash still proves what was received.
    """
    text = content if isinstance(content, str) else ""
    return {
        "raw_model_response": text[:AUDIT_RAW_RESPONSE_MAX_CHARS],
        "raw_model_response_chars": len(text),
        "raw_model_response_truncated": len(text) > AUDIT_RAW_RESPONSE_MAX_CHARS,
        "raw_model_response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def parse_model_json(content: str) -> Dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        raise ValueError("markdown_fence_not_allowed")
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("model_output_not_object")
    return value


def build_validator(config: Dict[str, Any]) -> StrategyValidator:
    limits = config["validator"]
    return StrategyValidator(
        max_actions=int(limits["max_actions"]),
        max_pump_seconds=float(limits["max_pump_seconds"]),
        max_wait_seconds=float(limits["max_wait_seconds"]),
        max_total_pump_seconds=float(limits["max_total_pump_seconds"]),
        max_total_seconds=float(limits["max_total_seconds"]),
    )


def append_audit(audit_dir: Path, record: Dict[str, Any]) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    return path


def run_chain(
    *,
    state: Dict[str, Any],
    config: Dict[str, Any],
    prompt: str,
    fixture_content: Optional[str] = None,
    session: Any = None,
) -> Dict[str, Any]:
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    model_input = project_state_for_model(state)
    raw_content = fixture_content
    cloud_meta: Dict[str, Any] = {
        "provider": "fixture" if fixture_content is not None else config.get("provider"),
        "model": "fixture" if fixture_content is not None else config.get("model"),
        "request_id": None,
        "usage": {},
    }
    failure = None

    if fixture_content is None:
        api_key = os.environ.get(str(config.get("api_key_env") or "CLOUD_STRATEGY_API_KEY"), "")
        client = (
            OpenAICompatibleClient(config, api_key, session=session)
            if session is not None
            else OpenAICompatibleClient(config, api_key)
        )
        try:
            response: CloudResponse = client.complete(prompt, model_input)
            raw_content = response.content
            cloud_meta = {
                "provider": response.provider,
                "model": response.model,
                "request_id": response.request_id,
                "usage": response.usage,
            }
        except CloudStrategyError as error:
            failure = {"code": error.code, "message": str(error), "retryable": error.retryable}

    parsed = None
    parse_error = None
    validation = {"accepted": False, "reason_codes": ["model_unavailable"], "strategy": None}
    if failure is None and raw_content is not None:
        try:
            parsed = parse_model_json(raw_content)
        except (json.JSONDecodeError, ValueError) as error:
            parse_error = {"code": "invalid_model_json", "message": str(error)}
            validation = {"accepted": False, "reason_codes": ["invalid_model_json"], "strategy": None}
        else:
            # Trusted attachment. The prompt forbids emitting state_sha256, and a value
            # the model supplies anyway is overwritten here from the snapshot actually
            # loaded, so the binding can never come from provider output. An attempt
            # stays visible in the audit copy of the raw response and its full-text hash.
            parsed[STATE_HASH_FIELD] = fingerprint(state)
            try:
                validator = build_validator(config)
            except (KeyError, TypeError, ValueError, OverflowError):
                validation = {
                    "accepted": False,
                    "reason_codes": ["invalid_validator_config"],
                    "strategy": None,
                }
            else:
                validation = validator.validate(parsed, state).to_dict()
    elif failure is not None:
        validation = {"accepted": False, "reason_codes": [failure["code"]], "strategy": None}

    return {
        "chain_version": "cloud-strategy-chain.v1",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "device_code": state.get("device_code"),
        "state_observed_at": state.get("observed_at"),
        "state_generated_at": state.get("generated_at"),
        "state_sha256": fingerprint(state),
        "model_input": model_input,
        "mode": "proposal_only",
        "actuator_commands_allowed": False,
        "cloud": cloud_meta,
        "failure": failure,
        "parse_error": parse_error,
        **bind_model_response(raw_content),
        "validation": validation,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="soil3 non-executing Cloud Strategy V1 chain")
    parser.add_argument("--config", type=Path, default=os.environ.get("CLOUD_STRATEGY_CONFIG"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--fixture-response", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.config is None:
        parser.error("--config or CLOUD_STRATEGY_CONFIG is required")
    config = load_json(args.config)
    state_path_value = args.state or config.get("state_path")
    if not state_path_value:
        parser.error("--state or config state_path is required")
    state = load_json(Path(state_path_value))
    if state.get("device_code") != "soil3":
        raise SystemExit("state input must be a soil3 state.v1 record")
    for field in ("observed_at", "generated_at"):
        if parse_timestamp(state.get(field)) is None:
            raise SystemExit(f"state input has no usable {field}")

    prompt = args.prompt.read_text(encoding="utf-8")
    fixture_content = args.fixture_response.read_text(encoding="utf-8") if args.fixture_response else None
    result = run_chain(state=state, config=config, prompt=prompt, fixture_content=fixture_content)
    audit_path = append_audit(Path(config["audit_dir"]), result)
    if args.output:
        write_json_atomic(args.output, result)
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "device_code": result["device_code"],
                "state_observed_at": result["state_observed_at"],
                "accepted": result["validation"]["accepted"],
                "reason_codes": result["validation"]["reason_codes"],
                "audit_path": str(audit_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result["validation"]["accepted"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
