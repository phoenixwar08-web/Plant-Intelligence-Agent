"""Non-executing soil3 Cloud Gate admission module."""

from .gate_v1 import GatePolicy
from .gate_v2 import evaluate_gate_v2

__all__ = ["GatePolicy", "evaluate_gate_v2"]
