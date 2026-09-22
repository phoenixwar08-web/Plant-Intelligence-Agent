from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.soil3.phase3_bridge import Phase3Bridge
from services.soil3.telemetry.common import load_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a non-executing Phase3 Bridge V1 handoff")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--strategy", required=True, type=Path)
    parser.add_argument("--gate", required=True, type=Path)
    parser.add_argument("--runner", required=True, type=Path)
    args = parser.parse_args()
    response = Phase3Bridge().verify(
        load_json(args.state), load_json(args.strategy), load_json(args.gate), load_json(args.runner)
    )
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
