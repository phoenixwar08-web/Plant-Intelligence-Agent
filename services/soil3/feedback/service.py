"""CLI for the soil3 feedback.v1 record store: record, read, list, outcome, attach.

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
from typing import Any, Dict, Optional

from services.soil3.episode.episode_v1 import EpisodeError
from services.soil3.feedback.feedback_v1 import FeedbackError, FeedbackStore
from services.soil3.telemetry.common import load_json


def _load_object(path: Optional[Path], what: str) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    value = load_json(path)
    if not isinstance(value, dict):
        raise FeedbackError("validation_failed", [f"{what}_file_not_object"])
    return value


def _record_summary(record: Dict[str, Any], path: Path) -> Dict[str, Any]:
    return {
        "feedback_id": record["feedback_id"],
        "episode_id": record["episode_id"],
        "window": record["window"],
        "observed_at": record["observed_at"],
        "contradictions": record["contradictions"],
        "missing_observations": record["missing_observations"],
        "path": str(path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="soil3 feedback.v1 multi-window observation store (no execution, no device control)"
    )
    parser.add_argument("--store-dir", type=Path, required=True,
                        help="Directory holding feedback.v1 JSON records")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Store one window observation")
    record.add_argument("--input", type=Path, required=True,
                        help="JSON file with the caller-supplied observation payload")

    read = subparsers.add_parser("read", help="Print one stored feedback record")
    read.add_argument("--feedback-id", required=True)

    list_ = subparsers.add_parser("list", help="Print all records of one episode, oldest first")
    list_.add_argument("--episode-id", required=True)

    outcome = subparsers.add_parser("outcome", help="Print the aggregated outcome payload for one episode")
    outcome.add_argument("--episode-id", required=True)

    attach = subparsers.add_parser(
        "attach", help="Append an episode's feedback records to its episode.v1 record and set its outcome once")
    attach.add_argument("--episode-id", required=True)
    attach.add_argument("--episode-store-dir", type=Path, required=True,
                        help="Directory holding the episode.v1 record store")

    return parser


def run(args: argparse.Namespace) -> int:
    store = FeedbackStore(args.store_dir)
    if args.command == "record":
        payload = _load_object(args.input, "input")
        record = store.record(payload)
        output = _record_summary(record, store.feedback_path(record["feedback_id"]))
    elif args.command == "read":
        output = store.read(args.feedback_id)
    elif args.command == "list":
        records = store.list_for_episode(args.episode_id)
        output = {
            "episode_id": args.episode_id,
            "count": len(records),
            "records": records,
        }
    elif args.command == "outcome":
        output = store.outcome(args.episode_id)
    else:  # attach
        output = store.attach_to_episode(args.episode_id, args.episode_store_dir)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except (FeedbackError, EpisodeError) as error:
        print(json.dumps({"error": error.code, "reasons": error.reasons}, ensure_ascii=False),
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
