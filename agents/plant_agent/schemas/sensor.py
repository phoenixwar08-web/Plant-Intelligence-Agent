from pydantic import BaseModel, Field
from typing import Optional


class SensorStatus(BaseModel):

    device_code: str

    recv_time: Optional[str] = None

    soil_temperature: Optional[float] = None

    soil_humidity: Optional[float] = Field(
        default=None,
        description="soil moisture percentage"
    )

    air_humidity: Optional[float] = None

    ec: Optional[float] = None

    lux: Optional[float] = None

    watering_flag: int = 0

    watering_sec: float = 0.0

    source: str = "mqtt"
