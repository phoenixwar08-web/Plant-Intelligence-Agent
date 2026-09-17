from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .client import CloudResponse, CloudStrategyError, OpenAICompatibleClient
from .validator import StrategyValidator


MODULE_ROOT = Path(__file__).resolve().parent
DEFAULT_PROMPT = MODULE_ROOT / "prompts" / "strategy_v1.txt"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def append_audit(audit_dir: Path, record: Dict[str, Any]) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
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
            response: CloudResponse = client.complete(prompt, state)
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
            limits = config["validator"]
            validator = StrategyValidator(
                max_actions=int(limits["max_actions"]),
                max_pump_seconds=float(limits["max_pump_seconds"]),
                max_wait_seconds=float(limits["max_wait_seconds"]),
            )
            validation = validator.validate(parsed, state).to_dict()
    elif failure is not None:
        validation = {"accepted": False, "reason_codes": [failure["code"]], "strategy": None}

    return {
        "chain_version": "cloud-strategy-chain.v1",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "state_id": state.get("state_id"),
        "plant_id": state.get("plant_id"),
        "state_snapshot": state,
        "mode": "proposal_only",
        "actuator_commands_allowed": False,
        "cloud": cloud_meta,
        "failure": failure,
        "parse_error": parse_error,
        "raw_model_response": raw_content,
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
    if state.get("plant_id") != "soil3" or not state.get("state_id"):
        raise SystemExit("state input must be a soil3 state with state_id")

    prompt = args.prompt.read_text(encoding="utf-8")
    fixture_content = args.fixture_response.read_text(encoding="utf-8") if args.fixture_response else None
    result = run_chain(state=state, config=config, prompt=prompt, fixture_content=fixture_content)
    audit_path = append_audit(Path(config["audit_dir"]), result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "state_id": result["state_id"],
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
