from pydantic import BaseModel
from typing import Dict, Any

from schemas.sensor import SensorStatus
from schemas.visual import VisualStatus
from schemas.control import ControlStatus
from schemas.safety import SafetyStatus
from schemas.assessment import AssessmentStatus


class PlantStatusSummary(BaseModel):

    device_code: str

    generated_at: str


    sensor: SensorStatus

    visual: VisualStatus

    control: ControlStatus

    safety: SafetyStatus

    assessment: AssessmentStatus


    learning: Dict[str, Any] = {}

    history: Dict[str, Any] = {}

    summary: Dict[str, Any] = {}

    human_events: Dict[str, Any] = {}
