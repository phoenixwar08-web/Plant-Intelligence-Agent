from pydantic import BaseModel, Field
from typing import List


class AssessmentStatus(BaseModel):

    level: str = Field(
        default="normal",
        description="risk level"
    )

    risk_score: int = Field(
        default=0,
        ge=0,
        le=100
    )

    risk_sources: List[str] = Field(
        default_factory=list
    )

    confidence: float = Field(
        default=1.0,
        ge=0,
        le=1
    )
