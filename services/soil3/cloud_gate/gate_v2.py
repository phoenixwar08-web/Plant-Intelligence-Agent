"""Cloud Gate V2: a content-bound admission record for strategy.v1."""
from __future__ import annotations

from typing import Any

from services.soil3.cloud_strategy.validator import StrategyValidator, fingerprint

from .gate_v1 import GatePolicy, evaluate_gate


def evaluate_gate_v2(
    state: Any,
    strategy: Any,
    policy: GatePolicy,
    exploration_requested: Any,
    ledger: Any = None,
) -> dict[str, Any]:
    """Return a gate.v2 record with a formal Strategy content binding.

    The underlying admission decision remains gate.v1 semantics.  A content
    hash is exposed only when the formal validator accepted the exact complete
    Strategy; this includes Strategies Gate later denies for safety or budget.
    """
    normalized_state = state if isinstance(state, dict) else {}
    normalized_strategy = strategy if isinstance(strategy, dict) else {}
    validation = StrategyValidator().validate(normalized_strategy, normalized_state)
    record = evaluate_gate(
        normalized_state,
        normalized_strategy,
        policy,
        exploration_requested,
        ledger,
    )
    record["schema_version"] = "gate.v2"
    record["strategy_sha256"] = (
        fingerprint(normalized_strategy) if validation.accepted else None
    )
    return record
