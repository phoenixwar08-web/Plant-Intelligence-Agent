"""CLI for the analysis-only soil3 trace.v1 store."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.soil3.telemetry.common import load_json
from services.soil3.trace.trace_v1 import TraceError, TraceStore


def _load_json(path: Path, *, expected: type, label: str) -> Any:
    try:
        value = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise TraceError("validation_failed", [f"{label}_file_invalid"]) from error
    if not isinstance(value, expected):
        raise TraceError("validation_failed", [f"{label}_file_not_{expected.__name__}"])
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="soil3 trace.v1 analysis store (no decision, execution, or device control)"
    )
    parser.add_argument("--store-dir", type=Path, required=True,
                        help="Directory holding trace.v1 JSON records")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("create", help="Create one empty analysis trace")

    read = subparsers.add_parser("read", help="Print one trace record")
    read.add_argument("--trace-id", required=True)

    decision = subparsers.add_parser("set-decision", help="Set named decision associations once")
    decision.add_argument("--trace-id", required=True)
    decision.add_argument("--updates", type=Path, required=True,
                          help="JSON object containing named decision fields")

    feedback = subparsers.add_parser("append-feedback", help="Append new feedback references")
    feedback.add_argument("--trace-id", required=True)
    feedback.add_argument("--refs", type=Path, required=True,
                          help="JSON array of feedback record references")

    outcome = subparsers.add_parser("set-outcome", help="Set the Outcome reference once")
    outcome.add_argument("--trace-id", required=True)
    outcome.add_argument("--ref", type=Path, required=True,
                         help="JSON object containing one Outcome record reference")
    return parser


def run(args: argparse.Namespace) -> int:
    store = TraceStore(args.store_dir)
    if args.command == "create":
        output = store.create()
    elif args.command == "read":
        output = store.read(args.trace_id)
    elif args.command == "set-decision":
        output = store.set_decision(
            args.trace_id,
            **_load_json(args.updates, expected=dict, label="updates"),
        )
    elif args.command == "append-feedback":
        output = store.append_feedback_refs(
            args.trace_id,
            _load_json(args.refs, expected=list, label="refs"),
        )
    else:
        output = store.set_outcome_ref(
            args.trace_id,
            _load_json(args.ref, expected=dict, label="ref"),
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    args = build_parser().parse_args()
    try:
        raise SystemExit(run(args))
    except TraceError as error:
        print(json.dumps({"error": error.code, "reasons": error.reasons}, ensure_ascii=False),
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
