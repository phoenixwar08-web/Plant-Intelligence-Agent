from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gate_v1 import GatePolicy


LEDGER_SCHEMA_VERSION = "cloud-gate-budget.v1"


class BudgetLedgerError(RuntimeError):
    """A ledger problem that must cause the caller to fail closed."""


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    requested_water_seconds: float
    reserved_water_seconds: float
    remaining_water_seconds: float
    available: bool


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise BudgetLedgerError("invalid reservation timestamp") from error
    if parsed.tzinfo is None:
        raise BudgetLedgerError("invalid reservation timestamp")
    return parsed.astimezone(timezone.utc)


def _finite_nonnegative(value: Any) -> float:
    if isinstance(value, bool):
        raise BudgetLedgerError("budget amount must be a finite non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise BudgetLedgerError("budget amount must be a finite non-negative number") from error
    if not math.isfinite(number) or number < 0:
        raise BudgetLedgerError("budget amount must be a finite non-negative number")
    return number


class BudgetLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def reserve(
        self,
        device_code: str,
        reservation_id: str,
        requested_water_seconds: float,
        policy: "GatePolicy",
        decided_at: str,
    ) -> BudgetReservation:
        if not isinstance(device_code, str) or not device_code:
            raise BudgetLedgerError("invalid device code")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise BudgetLedgerError("invalid reservation id")
        requested = _finite_nonnegative(requested_water_seconds)
        now = _timestamp(decided_at)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            ledger = self._read()
            reservations = ledger["reservations"]
            existing = next(
                (
                    item
                    for item in reservations
                    if item.get("device_code") == device_code
                    and item.get("reservation_id") == reservation_id
                ),
                None,
            )
            if existing is not None:
                return BudgetReservation(
                    reservation_id=reservation_id,
                    requested_water_seconds=float(existing["requested_water_seconds"]),
                    reserved_water_seconds=float(existing["reserved_water_seconds"]),
                    remaining_water_seconds=float(existing["remaining_water_seconds"]),
                    available=bool(existing["available"]),
                )

            active = [
                item
                for item in reservations
                if item.get("device_code") == device_code
                and (now - _timestamp(item["decided_at"])).total_seconds() < policy.window_seconds
            ]
            used = sum(_finite_nonnegative(item["reserved_water_seconds"]) for item in active)
            remaining = max(0.0, policy.max_exploration_water_seconds - used)
            available = requested <= remaining
            reserved = requested if available else 0.0
            result = BudgetReservation(
                reservation_id=reservation_id,
                requested_water_seconds=requested,
                reserved_water_seconds=reserved,
                remaining_water_seconds=remaining - reserved if available else remaining,
                available=available,
            )
            reservations.append(
                {
                    "device_code": device_code,
                    "reservation_id": reservation_id,
                    "decided_at": decided_at,
                    "requested_water_seconds": result.requested_water_seconds,
                    "reserved_water_seconds": result.reserved_water_seconds,
                    "remaining_water_seconds": result.remaining_water_seconds,
                    "available": result.available,
                }
            )
            self._write(ledger)
            return result
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _acquire_lock(self) -> None:
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise BudgetLedgerError("budget ledger is locked") from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(str(os.getpid()))

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {"schema_version": LEDGER_SCHEMA_VERSION, "reservations": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BudgetLedgerError("budget ledger is unreadable") from error
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != LEDGER_SCHEMA_VERSION
            or not isinstance(value.get("reservations"), list)
            or not all(isinstance(item, dict) for item in value["reservations"])
        ):
            raise BudgetLedgerError("budget ledger has invalid shape")
        return value

    def _write(self, ledger: dict[str, list[dict[str, Any]]]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(ledger, output, ensure_ascii=False, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
