#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/root/.openclaw/workspace"))
DEFAULT_MEMORY = ROOT / "memory" / "plant_event_memory.jsonl"
VALID_IMPORTANCE = {"low", "normal", "high"}
VALID_STATUS = {"open", "resolved", "expired", "recorded"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in events[-2000:]
    )
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def normalized_event(raw: dict[str, Any]) -> dict[str, Any]:
    event_type = str(raw.get("event_type") or "").strip()
    device_code = str(raw.get("device_code") or "").strip()
    if not event_type or not device_code:
        raise ValueError("event_type and device_code are required")
    status = str(raw.get("status") or "recorded")
    importance = str(raw.get("importance") or "normal")
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status: {status}")
    if importance not in VALID_IMPORTANCE:
        raise ValueError(f"invalid importance: {importance}")
    event = {
        "event_id": str(raw.get("event_id") or f"plant-event-{uuid.uuid4().hex[:14]}"),
        "device_code": device_code,
        "event_type": event_type,
        "occurred_at": str(raw.get("occurred_at") or now_iso()),
        "source": str(raw.get("source") or "openclaw"),
        "actor_type": str(raw.get("actor_type") or "system"),
        "actor_name": raw.get("actor_name"),
        "status": status,
        "importance": importance,
        "summary": str(raw.get("summary") or event_type),
        "user_text": raw.get("user_text"),
        "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {},
        "related_event_id": raw.get("related_event_id"),
        "follow_up_due_at": raw.get("follow_up_due_at"),
        "resolved_at": raw.get("resolved_at"),
        "resolution": raw.get("resolution"),
        "created_at": str(raw.get("created_at") or now_iso()),
        "updated_at": now_iso(),
    }
    return event


def upsert_event(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    event = normalized_event(raw)
    events = read_events(path)
    replaced = False
    output: list[dict[str, Any]] = []
    for old in events:
        if old.get("event_id") == event["event_id"]:
            merged = {**old, **event, "created_at": old.get("created_at") or event["created_at"]}
            output.append(merged)
            event = merged
            replaced = True
        else:
            output.append(old)
    if not replaced:
        output.append(event)
    write_events(path, output)
    return event


def query_events(
    events: list[dict[str, Any]],
    *,
    device_code: str,
    limit: int,
    event_type: str | None = None,
    unresolved_only: bool = False,
) -> list[dict[str, Any]]:
    selected = []
    for item in events:
        if item.get("device_code") != device_code:
            continue
        if event_type and item.get("event_type") != event_type:
            continue
        if unresolved_only and item.get("status") != "open":
            continue
        selected.append(item)
    selected.sort(key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("event_id") or "")))
    return selected[-max(1, limit):]


def memory_points(events: list[dict[str, Any]]) -> list[str]:
    points: list[str] = []
    for item in reversed(events):
        summary = str(item.get("summary") or "").strip()
        if summary and summary not in points:
            points.append(summary)
        if len(points) >= 4:
            break
    return points


def load_input(value: str) -> dict[str, Any]:
    if value == "-":
        return json.load(__import__("sys").stdin)
    return json.loads(Path(value).read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Store and query meaningful plant-care events.")
    parser.add_argument("--memory", default=str(DEFAULT_MEMORY))
    parser.add_argument("--append-json", help="JSON path or '-' for stdin")
    parser.add_argument("--device", default="soil3")
    parser.add_argument("--event-type")
    parser.add_argument("--unresolved-only", action="store_true")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    path = Path(args.memory)
    appended = None
    if args.append_json:
        appended = upsert_event(path, load_input(args.append_json))
    recent = query_events(
        read_events(path),
        device_code=args.device,
        limit=args.limit,
        event_type=args.event_type,
        unresolved_only=args.unresolved_only,
    )
    payload = {
        "ok": True,
        "source": "openclaw_plant_event_memory",
        "device_code": args.device,
        "memory_path": str(path),
        "appended": appended,
        "recent_events": recent,
        "unresolved_events": [item for item in recent if item.get("status") == "open"],
        "memory_points": memory_points(recent),
        "control_boundary": "Event memory records and explains events only; it cannot open a pump.",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
