from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .runtime_v1 import RuntimeConfig, write_state_snapshot


def load_config(path: Path) -> RuntimeConfig:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    return RuntimeConfig.from_dict(value)


def state_summary(config: RuntimeConfig) -> dict[str, Any]:
    state = write_state_snapshot(config)
    flags = state.get("safety", {}).get("flags", {})
    return {
        "path": str(config.state_output),
        "schema_version": state.get("schema_version"),
        "observed_at": state.get("observed_at"),
        "safety_flags": sorted(flags) if isinstance(flags, dict) else [],
    }


def run(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="soil3 proposal-only agent runtime")
    parser.add_argument("command", choices=("state",))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    return state_summary(config)


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
