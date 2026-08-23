#!/usr/bin/env python3
"""Read-only, deterministic soil2 inspection checklist."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo


BASE = Path(__file__).resolve().parent.parent
DEVICE_CODE = "soil2"
TIMEZONE = ZoneInfo("Asia/Shanghai")


OUTPUT_DIR = BASE / "outputs"
STATUS_PATH = OUTPUT_DIR / "soil2_status.json"
DECISION_PATH = OUTPUT_DIR / "soil2_decision.json"
QUALITY_PATH = OUTPUT_DIR / "health" / "soil2_data_quality.json"

QUALITY_SOURCES = ("sensor", "status", "decision", "visual")
QUALITY_CHECKS = {
    "sensor": ["检查传感器供电与上报链路", "检查探针连接和位置"],
    "status": ["检查状态融合服务是否正常刷新"],
    "decision": ["检查状态融合与决策文件是否正常刷新"],
    "visual": ["补拍当前照片", "检查摄像头链路和画面"],
}
SEVERITY_ORDER = {"clear": 0, "warning": 1, "critical": 2}


def _load_required_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("required checklist input is unavailable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("required checklist input is invalid")
    return payload


def _now(now=None) -> datetime:
    if isinstance(now, datetime):
        return now.astimezone(TIMEZONE) if now.tzinfo else now.replace(tzinfo=TIMEZONE)
    return datetime.now(TIMEZONE)


def _check(code: str, severity: str, source: str, evidence: str, recommended_checks: List[str]) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "source": source,
        "evidence": evidence,
        "recommended_checks": recommended_checks,
    }


def _active_flag(safety: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = safety.get(name, {})
    return value if isinstance(value, dict) else {}


def build_checklist(device_code: str = DEVICE_CODE, now=None) -> Dict[str, Any]:
    """Return only facts already present in status, decision, and quality files."""
    if device_code != DEVICE_CODE:
        raise ValueError("only soil2 is supported")

    status = _load_required_json(STATUS_PATH)
    decision_data = _load_required_json(DECISION_PATH)
    quality = _load_required_json(QUALITY_PATH)
    safety = status.get("safety") if isinstance(status.get("safety"), dict) else {}
    decision = decision_data.get("decision") if isinstance(decision_data.get("decision"), dict) else {}
    checks: List[Dict[str, Any]] = []

    safety_rules = (
        (
            "water_delivery_suspect",
            ["检查水箱余量", "检查水路是否通畅", "检查泵出水状态"],
        ),
        (
            "reservoir_empty_suspect",
            ["检查水箱余量", "检查供水是否正常"],
        ),
        (
            "low_wet_recovery_suspect",
            ["检查盆土湿润分布", "检查探针位置"],
        ),
    )
    for code, recommended_checks in safety_rules:
        flag = _active_flag(safety, code)
        if flag.get("active") is True:
            checks.append(
                _check(
                    code,
                    "warning",
                    "safety",
                    str(flag.get("reason") or f"safety.{code}.active=true"),
                    recommended_checks,
                )
            )

    if decision.get("action") == "manual_check":
        reasoning = decision.get("reasoning")
        facts = [str(item) for item in reasoning if isinstance(item, str) and item] if isinstance(reasoning, list) else []
        checks.append(
            _check(
                "manual_check",
                "warning",
                "decision",
                "；".join(facts) or "decision.action=manual_check",
                ["人工观察植株与盆土", "补拍当前照片"],
            )
        )

    freshness = quality.get("freshness") if isinstance(quality.get("freshness"), dict) else {}
    data_quality: Dict[str, str] = {}
    for source in QUALITY_SOURCES:
        item = freshness.get(source)
        state = item.get("state") if isinstance(item, dict) else "unavailable"
        if state not in ("fresh", "stale", "unavailable"):
            state = "unavailable"
        data_quality[source] = state
        if state == "fresh":
            continue
        severity = "critical" if state == "unavailable" and source in ("sensor", "status", "decision") else "warning"
        observed_at = item.get("observed_at") if isinstance(item, dict) else None
        evidence = f"数据质量审计标记为 {state}"
        if observed_at:
            evidence = f"{evidence}；最近时间：{observed_at}"
        checks.append(_check(f"{source}_{state}", severity, source, evidence, QUALITY_CHECKS[source]))

    overall = "clear"
    for item in checks:
        if SEVERITY_ORDER[item["severity"]] > SEVERITY_ORDER[overall]:
            overall = item["severity"]
    return {
        "schema_version": 1,
        "device_code": device_code,
        "generated_at": _now(now).isoformat(),
        "overall": overall,
        "checks": checks,
        "data_quality": data_quality,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="soil2 read-only inspection checklist")
    parser.add_argument("device_code", nargs="?", default=DEVICE_CODE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_checklist(args.device_code)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")) if args.json else json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
