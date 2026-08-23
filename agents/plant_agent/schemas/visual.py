from pydantic import BaseModel, Field
from typing import List, Optional


class VisualStatus(BaseModel):

    observed_at: Optional[str] = None


    # OpenClaw视觉判断结果
    plant_health: Optional[str] = Field(
        default=None,
        description="overall plant health"
    )

    disease_suspected: bool = Field(
        default=False,
        description="whether disease is suspected"
    )


    visual_stress_level: Optional[str] = Field(
        default=None,
        description="visual health level"
    )


    yellow_leaf_ratio: Optional[float] = Field(
        default=None,
        ge=0,
        le=1,
        description="yellow leaf ratio"
    )


    green_leaf_area_px: Optional[int] = Field(
        default=None,
        description="green leaf pixel area"
    )


    flower_count: Optional[int] = Field(
        default=None,
        ge=0
    )


    fruit_count: Optional[int] = Field(
        default=None,
        ge=0
    )


    suspected_issues: List[str] = Field(
        default_factory=list
    )
