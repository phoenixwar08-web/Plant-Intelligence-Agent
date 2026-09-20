"""CLI for the soil3 episode.v1 record store: create, update, read, close.

The store directory is always an explicit caller-supplied path; this CLI has no
default that could point at production runtime/data/log locations. Every
payload input is a JSON file supplied by the caller — the CLI reads facts, it
never produces them.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.soil3.episode.episode_v1 import EpisodeError, EpisodeStore
from services.soil3.telemetry.common import load_json


def _load_object(path: Optional[Path], what: str) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    value = load_json(path)
    if not isinstance(value, dict):
        raise EpisodeError("validation_failed", [f"{what}_file_not_object"])
    return value


def _load_list(path: Optional[Path], what: str) -> Optional[List[Dict[str, Any]]]:
    if path is None:
        return None
    value = load_json(path)
    if not isinstance(value, list):
        raise EpisodeError("validation_failed", [f"{what}_file_not_list"])
    return value


def _summary(record: Dict[str, Any], path: Path) -> Dict[str, Any]:
    return {
        "episode_id": record["episode_id"],
        "status": record["status"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "closed_at": record["closed_at"],
        "missing_facts": record["missing_facts"],
        "path": str(path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="soil3 episode.v1 record store (no execution, no device control)"
    )
    parser.add_argument("--store-dir", type=Path, required=True,
                        help="Directory holding episode.v1 JSON records")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Open an episode from a state.v1 record")
    create.add_argument("--state", type=Path, required=True, help="state.v1 JSON file")

    read = subparsers.add_parser("read", help="Print one stored episode record")
    read.add_argument("--episode-id", required=True)

    update = subparsers.add_parser("update", help="Attach caller-supplied facts to an open episode")
    update.add_argument("--episode-id", required=True)
    update.add_argument("--strategy", type=Path, help="strategy.v1 JSON file (set-once)")
    update.add_argument("--gate-result", type=Path, help="gate result JSON file (set-once)")
    update.add_argument("--executed-actions", type=Path, help="JSON array of executed action facts (append)")
    update.add_argument("--feedback", type=Path, help="JSON array of feedback observations (append)")
    update.add_argument("--outcome", type=Path, help="outcome evaluation JSON file (set-once)")

    close = subparsers.add_parser("close", help="Finalize an episode and state missing facts")
    close.add_argument("--episode-id", required=True)
    close.add_argument("--outcome", type=Path, help="outcome evaluation JSON file")

    return parser


def run(args: argparse.Namespace) -> int:
    store = EpisodeStore(args.store_dir)
    if args.command == "create":
        state = _load_object(args.state, "state")
        record = store.create(state)
        output = _summary(record, store.episode_path(record["episode_id"]))
    elif args.command == "read":
        record = store.read(args.episode_id)
        output = record
    elif args.command == "update":
        record = store.update(
            args.episode_id,
            strategy=_load_object(args.strategy, "strategy"),
            gate_result=_load_object(args.gate_result, "gate_result"),
            executed_actions=_load_list(args.executed_actions, "executed_actions"),
            feedback=_load_list(args.feedback, "feedback"),
            outcome=_load_object(args.outcome, "outcome"),
        )
        output = _summary(record, store.episode_path(args.episode_id))
    else:  # close
        record = store.close(args.episode_id, outcome=_load_object(args.outcome, "outcome"))
        output = _summary(record, store.episode_path(args.episode_id))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except EpisodeError as error:
        print(json.dumps({"error": error.code, "reasons": error.reasons}, ensure_ascii=False),
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
