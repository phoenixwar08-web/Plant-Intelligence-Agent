#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


SUPPORTED_DEVICES = {"soil1", "soil2", "soil3", "soil_test"}
AUDIT_PATH = Path("/root/.openclaw/workspace/logs/openclaw_external_watering_events.jsonl")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def audit(payload: dict[str, Any]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def run_checked(cmd: list[str], *, timeout: int = 30) -> str:
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def gsql(sql: str) -> str:
    command = "gsql -d soil_data -p 7654 -t -A -c " + shlex.quote(sql)
    return run_checked(["su", "-", "opengauss", "-c", command], timeout=30)


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("'", "''")
    return "'" + text + "'"


def parse_amount_ml(text: str) -> float | None:
    patterns = [
        r"(\d+(?:\.\d+)?)\s*(?:ml|ML|毫升)",
        r"(\d+(?:\.\d+)?)\s*(?:mL)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return float(m.group(1))
    if "半杯" in text:
        return 100.0
    if "一杯" in text or "1杯" in text:
        return 200.0
    if "一点" in text or "一小口" in text:
        return 30.0
    return None


def ensure_table() -> None:
    gsql(
        """
        CREATE TABLE IF NOT EXISTS external_watering_events (
            event_id VARCHAR(64) PRIMARY KEY,
            device_code VARCHAR(32) NOT NULL,
            event_time TIMESTAMP NOT NULL,
            source VARCHAR(64) NOT NULL,
            operator_id VARCHAR(128),
            operator_name VARCHAR(128),
            amount_ml NUMERIC,
            confidence VARCHAR(32) NOT NULL,
            note TEXT,
            raw_text TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def insert_event(payload: dict[str, Any]) -> None:
    ensure_table()
    amount = payload.get("amount_ml")
    amount_sql = "NULL" if amount is None else str(float(amount))
    gsql(
        f"""
        INSERT INTO external_watering_events
            (event_id, device_code, event_time, source, operator_id, operator_name,
             amount_ml, confidence, note, raw_text)
        VALUES
            ({sql_literal(payload['event_id'])},
             {sql_literal(payload['device_code'])},
             {sql_literal(payload['event_time'].replace('T', ' ')[:19])},
             {sql_literal(payload['source'])},
             {sql_literal(payload.get('operator_id'))},
             {sql_literal(payload.get('operator_name'))},
             {amount_sql},
             {sql_literal(payload['confidence'])},
             {sql_literal(payload.get('note'))},
             {sql_literal(payload.get('raw_text'))});
        """
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Record user-reported external watering for the current plant.")
    parser.add_argument("--device", default="soil3", choices=sorted(SUPPORTED_DEVICES))
    parser.add_argument("--operator-id", default="openclaw-mainbot")
    parser.add_argument("--operator-name", default="植境智养")
    parser.add_argument("--amount-ml", type=float)
    parser.add_argument("--raw-text", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--source", default="qq_user")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    amount_ml = args.amount_ml
    if amount_ml is None and args.raw_text:
        amount_ml = parse_amount_ml(args.raw_text)

    payload = {
        "ok": True,
        "event_id": f"external-{uuid.uuid4().hex[:12]}",
        "device_code": args.device,
        "event_time": now_iso(),
        "source": args.source,
        "operator_id": args.operator_id,
        "operator_name": args.operator_name,
        "amount_ml": amount_ml,
        "confidence": "user_reported",
        "note": args.note or "用户报告刚手动浇过水",
        "raw_text": args.raw_text,
        "dry_run": bool(args.dry_run),
        "created_at": now_iso(),
    }

    if args.dry_run:
        payload["storage"] = "dry_run"
    else:
        try:
            insert_event(payload)
            payload["storage"] = "opengauss"
        except Exception as exc:
            payload["ok"] = False
            payload["storage"] = "jsonl_fallback"
            payload["db_error"] = str(exc)
        audit(payload)
    emit(payload)
    if not payload["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
