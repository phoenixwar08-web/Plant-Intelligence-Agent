import collections
import csv
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import requests


# temperature, EC, humidity, light, watered, watering seconds,
# temperature delta, humidity delta, seconds since last watering
FEATURE_RANGES = (
    (0, 50), (0, 2000), (0, 100), (0, 100000), (0, 1), (0, 120),
    (-10, 10), (-20, 20), (0, 604800),
)


def normalize(values):
    return [
        float(np.clip((value - low) / (high - low), 0.0, 1.0))
        for value, (low, high) in zip(values, FEATURE_RANGES)
    ]


@dataclass
class SensorSnapshot:
    timestamp: str
    soil_temperature: float
    ec: float
    humidity: float
    air_humidity: float
    forecast_temperature: float
    forecast_humidity: float
    humidity_history: list
    normalized_history: list


class WeatherProvider:
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.cached = None
        self.cached_at = 0.0
        self.session = requests.Session()

    def get(self, config):
        weather = config["weather"]
        service = config["service"]
        if self.cached and time.time() - self.cached_at < service["weather_cache_seconds"]:
            return self.cached
        fallback = (weather["default_temperature"], weather["default_humidity"])
        try:
            response = self.session.get(
                weather["url"],
                params={
                    "latitude": weather["latitude"],
                    "longitude": weather["longitude"],
                    "hourly": "temperature_2m,relative_humidity_2m",
                    "forecast_days": 2
                },
                timeout=service["weather_timeout_seconds"]
            )
            response.raise_for_status()
            hourly = response.json()["hourly"]
            index = datetime.now().hour
            temperatures = hourly["temperature_2m"][index + 1:index + 13]
            humidities = hourly["relative_humidity_2m"][index + 1:index + 13]
            if not temperatures or not humidities:
                raise ValueError("weather response has no forecast values")
            self.cached = (float(np.mean(temperatures)), float(np.mean(humidities)))
            self.cached_at = time.time()
            return self.cached
        except Exception as exc:
            self.logger.warning("Weather lookup failed; using cached/default values: %s", exc)
            return self.cached or fallback


def _read_air_humidity(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as handle:
        lines = collections.deque(handle, maxlen=10)
    for line in reversed(lines):
        parts = line.strip().split(",")
        if len(parts) >= 3:
            try:
                return float(parts[2])
            except ValueError:
                continue
    return default


def read_sensor_snapshot(config, weather_provider):
    path = config["paths"]["soil_csv"]
    sequence_length = config["model"]["sequence_length"]
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as handle:
        rows = list(csv.reader(collections.deque(handle, maxlen=max(60, sequence_length * 3))))

    parsed = []
    for row in rows:
        if len(row) < 5:
            continue
        try:
            parsed.append({
                "time": row[0].strip(),
                "temperature": float(row[2]),
                "humidity": float(row[3]),
                "ec": float(row[4]),
                "watered": float(row[5]) if len(row) > 5 and row[5] else 0.0,
                "light": float(row[6]) if len(row) > 6 and row[6] else 0.0,
                "water_seconds": float(row[7]) if len(row) > 7 and row[7] else 0.0,
            })
        except (ValueError, IndexError):
            continue
    if not parsed:
        raise ValueError("soil CSV has no valid rows")

    valid = []
    previous_temperature = None
    previous_humidity = None
    last_watering_index = None
    for index, item in enumerate(parsed):
        temperature_delta = (
            0.0 if previous_temperature is None else item["temperature"] - previous_temperature
        )
        humidity_delta = 0.0 if previous_humidity is None else item["humidity"] - previous_humidity
        if item["watered"] > 0 or item["water_seconds"] > 0:
            last_watering_index = index
        seconds_since_watering = (
            604800.0 if last_watering_index is None else (index - last_watering_index) * 300.0
        )
        features = normalize([
            item["temperature"], item["ec"], item["humidity"], item["light"],
            item["watered"], item["water_seconds"], temperature_delta, humidity_delta,
            min(604800.0, max(0.0, seconds_since_watering)),
        ])
        valid.append((item, features))
        previous_temperature = item["temperature"]
        previous_humidity = item["humidity"]

    air_humidity = _read_air_humidity(
        config["paths"]["air_humidity_csv"], config["weather"]["default_humidity"]
    )
    forecast_temperature, forecast_humidity = weather_provider.get(config)
    recent = valid[-sequence_length:]
    normalized = [features for _, features in recent]
    last = valid[-1][0]
    return SensorSnapshot(
        timestamp=last["time"],
        soil_temperature=last["temperature"],
        ec=last["ec"],
        humidity=last["humidity"],
        air_humidity=air_humidity,
        forecast_temperature=forecast_temperature,
        forecast_humidity=forecast_humidity,
        humidity_history=[item[0]["humidity"] for item in valid],
        normalized_history=normalized
    )


def estimate_vpd(temperature, relative_humidity):
    saturation = 0.6108 * math.exp((17.27 * temperature) / (temperature + 237.3))
    return max(0.0, saturation * (1.0 - relative_humidity / 100.0))
