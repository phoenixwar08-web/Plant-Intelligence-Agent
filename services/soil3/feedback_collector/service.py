"""Non-executing command-line entry points for soil3 feedback facts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from services.soil3.feedback_collector.action_receipt_v1 import (
    ActionReceiptError,
    ActionReceiptStore,
)
from services.soil3.telemetry.common import load_json


def _manual_record(args: argparse.Namespace) -> dict:
    state = load_json(Path(args.state_file))
    return ActionReceiptStore(args.receipt_dir).record_manual(
        args.action_id,
        state,
        confirmed_by=args.confirmed_by,
        reference_action_at=args.reference_action_at,
        pump_seconds=args.pump_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="soil3 feedback fact recorder")
    commands = parser.add_subparsers(dest="command", required=True)
    manual = commands.add_parser("manual-record", help="record a confirmed manual watering fact")
    manual.add_argument("--receipt-dir", required=True)
    manual.add_argument("--state-file", required=True)
    manual.add_argument("--action-id", required=True)
    manual.add_argument("--confirmed-by", required=True)
    manual.add_argument("--reference-action-at", required=True)
    manual.add_argument("--pump-seconds", required=True, type=float)
    manual.set_defaults(handler=_manual_record)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (ActionReceiptError, OSError, ValueError, json.JSONDecodeError) as error:
        code = error.code if isinstance(error, ActionReceiptError) else "manual_record_failed"
        reasons = error.reasons if isinstance(error, ActionReceiptError) else [type(error).__name__]
        print(json.dumps({"error": code, "reasons": reasons}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
