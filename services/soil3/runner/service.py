from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.soil3.runner.runner_v1 import DryRunRunner, RunnerStore
from services.soil3.telemetry.common import load_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist and advance strategy.v1 in dry-run mode")
    parser.add_argument("--store-dir", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--strategy", required=True, type=Path)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--strategy-id", required=True)
    read = subparsers.add_parser("read")
    read.add_argument("--strategy-id", required=True)
    args = parser.parse_args()

    runner = DryRunRunner(RunnerStore(args.store_dir))
    if args.command == "start":
        record = runner.run(load_json(args.strategy))
    elif args.command == "resume":
        record = runner.resume(args.strategy_id)
    else:
        record = runner.read(args.strategy_id)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
