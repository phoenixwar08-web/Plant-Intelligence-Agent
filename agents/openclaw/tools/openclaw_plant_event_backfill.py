#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from openclaw_plant_event_memory import DEFAULT_MEMORY, upsert_event
from openclaw_plant_status_summary import gsql_rows


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/root/.openclaw/workspace"))
WATCHER_LOG = ROOT / "logs" / "openclaw_irrigation_watcher.jsonl"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def gateway_event(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("type") != "gateway_result":
        return None
    result = row.get("result") or {}
    if not result.get("ok") or result.get("status") != "completed":
        return None
    seconds = result.get("approved_seconds") or result.get("requested_seconds")
    return {
        "event_id": result.get("request_id"),
        "device_code": result.get("device_code") or "soil3",
        "event_type": "user_confirmed_watering",
        "occurred_at": result.get("completed_at") or row.get("at"),
        "source": "openclaw_irrigation_watcher_backfill",
        "actor_type": "user",
        "actor_name": result.get("operator_name"),
        "status": "resolved",
        "importance": "normal",
        "summary": f"你曾通过植境智养让我小口喝了 {float(seconds):g} 秒",
        "resolved_at": result.get("completed_at") or row.get("at"),
        "resolution": "historical_execution_recorded",
        "evidence": {
            "water_sec": seconds,
            "humidity_before": (result.get("context") or {}).get("humidity"),
            "request_id": result.get("request_id"),
        },
    }


def external_event(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("type") != "external_watering_result":
        return None
    result = row.get("result") or {}
    if not result.get("ok"):
        return None
    amount = result.get("amount_ml")
    return {
        "event_id": result.get("event_id"),
        "device_code": result.get("device_code") or "soil3",
        "event_type": "external_watering_confirmed",
        "occurred_at": result.get("event_time") or row.get("at"),
        "source": "openclaw_irrigation_watcher_backfill",
        "actor_type": "user",
        "actor_name": result.get("operator_name"),
        "status": "resolved",
        "importance": "normal",
        "summary": (
            f"你曾报告手动浇了约 {float(amount):g} ml"
            if amount is not None
            else "你曾报告手动浇过水"
        ),
        "resolved_at": row.get("at"),
        "resolution": "historical_user_report_recorded",
        "evidence": {"amount_ml": amount, "external_event_id": result.get("event_id")},
    }


def deferred_event(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("type") != "reply_seen" or row.get("action") != "defer":
        return None
    message = row.get("message") or {}
    source_id = message.get("source_event_id") or str(message.get("timestamp") or row.get("at"))
    return {
        "event_id": f"defer-{source_id}",
        "device_code": "soil3",
        "event_type": "watering_deferred",
        "occurred_at": row.get("at"),
        "source": "openclaw_irrigation_watcher_backfill",
        "actor_type": "user",
        "actor_name": message.get("name"),
        "status": "recorded",
        "importance": "normal",
        "summary": "你曾选择先不浇，继续观察",
        "user_text": message.get("text"),
    }


def historical_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        event = gateway_event(row) or external_event(row) or deferred_event(row)
        if not event or not event.get("event_id"):
            continue
        event_id = str(event["event_id"])
        if event_id in seen:
            continue
        seen.add(event_id)
        output.append(event)
    return output


def database_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    irrigation_rows = gsql_rows(
        "SELECT command_time,water_sec,source,status,operator_name,request_id,reason "
        "FROM irrigation_events "
        "WHERE device_code='soil3' AND water_sec>0 "
        "AND (source LIKE 'openclaw%' OR reason LIKE 'user_requested%' OR request_id LIKE 'openclaw-%') "
        "ORDER BY command_time;"
    )
    for row in irrigation_rows:
        occurred_at = row[0].replace(" ", "T") + "+08:00"
        seconds = float(row[1])
        display_seconds = round(seconds, 1)
        request_id = row[5] if len(row) > 5 and row[5] else f"db-user-water-{row[0]}-{seconds:g}"
        events.append(
            {
                "event_id": request_id,
                "device_code": "soil3",
                "event_type": "user_confirmed_watering",
                "occurred_at": occurred_at,
                "source": "opengauss_irrigation_events_backfill",
                "actor_type": "user",
                "actor_name": row[4] if len(row) > 4 and row[4] else None,
                "status": "resolved",
                "importance": "normal",
                "summary": f"你曾通过植境智养让我小口喝了 {display_seconds:g} 秒",
                "resolved_at": occurred_at,
                "resolution": "historical_execution_recorded",
                "evidence": {
                    "water_sec": seconds,
                    "raw_source": row[2] if len(row) > 2 else None,
                    "execution_status": row[3] if len(row) > 3 else None,
                    "request_id": request_id,
                    "reason": row[6] if len(row) > 6 else None,
                },
            }
        )
    try:
        external_rows = gsql_rows(
            "SELECT event_id,event_time,amount_ml,operator_name "
            "FROM external_watering_events "
            "WHERE device_code='soil3' ORDER BY event_time;"
        )
    except Exception:
        external_rows = []
    for row in external_rows:
        amount = float(row[2]) if len(row) > 2 and row[2] else None
        occurred_at = row[1].replace(" ", "T") + "+08:00"
        events.append(
            {
                "event_id": row[0],
                "device_code": "soil3",
                "event_type": "external_watering_confirmed",
                "occurred_at": occurred_at,
                "source": "opengauss_external_watering_backfill",
                "actor_type": "user",
                "actor_name": row[3] if len(row) > 3 and row[3] else None,
                "status": "resolved",
                "importance": "normal",
                "summary": (
                    f"你曾报告手动浇了约 {amount:g} ml"
                    if amount is not None
                    else "你曾报告手动浇过水"
                ),
                "resolved_at": occurred_at,
                "resolution": "historical_user_report_recorded",
                "evidence": {"amount_ml": amount, "external_event_id": row[0]},
            }
        )
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill meaningful Plant Talk events from watcher audit logs.")
    parser.add_argument("--watcher-log", default=str(WATCHER_LOG))
    parser.add_argument("--memory", default=str(DEFAULT_MEMORY))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    events = historical_events(read_jsonl(Path(args.watcher_log)))
    by_id = {str(event["event_id"]): event for event in events}
    for event in database_events():
        by_id.setdefault(str(event["event_id"]), event)
    events = sorted(by_id.values(), key=lambda item: str(item.get("occurred_at") or ""))
    written = []
    if not args.dry_run:
        for event in events:
            written.append(upsert_event(Path(args.memory), event))
    payload = {
        "ok": True,
        "dry_run": args.dry_run,
        "found": len(events),
        "written": len(written),
        "events": events,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
