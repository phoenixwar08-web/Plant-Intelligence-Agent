from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.soil3.experience_retrieval import ExperienceRetriever
from services.soil3.telemetry.common import load_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieve similar successful and failed soil3 episodes")
    parser.add_argument("--episode-dir", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--limit-per-class", type=int, default=3)
    args = parser.parse_args()
    result = ExperienceRetriever(args.episode_dir).retrieve(
        load_json(args.state), limit_per_class=args.limit_per_class
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
