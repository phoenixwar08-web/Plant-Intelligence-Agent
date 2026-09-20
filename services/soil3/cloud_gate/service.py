from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .budget import BudgetLedger
from .gate_v1 import GatePolicy, evaluate_gate


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="soil3 non-executing Cloud Gate V1")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--strategy", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--budget-ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--exploration-requested", action="store_true")
    args = parser.parse_args(argv)

    record = evaluate_gate(
        _load_object(args.state),
        _load_object(args.strategy),
        GatePolicy.from_dict(_load_object(args.config)),
        args.exploration_requested,
        BudgetLedger(args.budget_ledger),
    )
    _write_json_atomic(args.output, record)
    return record


if __name__ == "__main__":
    main()
