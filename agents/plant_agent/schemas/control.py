from pydantic import BaseModel, Field
from typing import Optional


class TriggerGuard(BaseModel):

    active: bool = False

    blocked: bool = False

    reason: Optional[str] = None

    candidate_plan: Optional[str] = None

    candidate_water_sec: Optional[float] = None



class CooldownStatus(BaseModel):

    cooldown_sec: Optional[float] = None

    base_sec: Optional[float] = None

    reason: Optional[str] = None

    last_delta_m: Optional[float] = None



class WateringWindow(BaseModel):

    level: Optional[str] = None

    reason: Optional[str] = None

    max_sec: Optional[float] = None

    allow_large_pulse: Optional[bool] = None

    allow_explore: Optional[bool] = None



class ControlStatus(BaseModel):

    pump_active: bool = False

    pump_last_command_sec: Optional[float] = None

    pump_total_cycles: Optional[int] = None

    total_water_sec_dispensed: Optional[float] = None


    decision: str = "unknown"

    decision_reason: str = "unknown"


    trigger_guard: TriggerGuard = Field(
        default_factory=TriggerGuard
    )


    cooldown: CooldownStatus = Field(
        default_factory=CooldownStatus
    )


    watering_window: WateringWindow = Field(
        default_factory=WateringWindow
    )


    pending_soak: Optional[dict] = None
