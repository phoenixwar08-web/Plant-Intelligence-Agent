from pydantic import BaseModel, Field
from typing import List, Optional


class SafetyFlag(BaseModel):

    active: bool = False

    reason: Optional[str] = None

    clear_reason: Optional[str] = None


class SafetyStatus(BaseModel):

    water_delivery_suspect: SafetyFlag = Field(
        default_factory=SafetyFlag
    )

    low_wet_recovery_suspect: SafetyFlag = Field(
        default_factory=SafetyFlag
    )

    reservoir_empty_suspect: SafetyFlag = Field(
        default_factory=SafetyFlag
    )


    predictor_state: Optional[str] = None

    predictor_fail_count: int = 0


    automatic_watering_allowed: bool = True


    blocking_reasons: List[str] = Field(
        default_factory=list
    )
