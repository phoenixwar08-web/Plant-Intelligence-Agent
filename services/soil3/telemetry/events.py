import csv
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .common import load_json, utc_timestamp


ABNORMAL_KEYS = {
    "water_delivery_suspect": "critical",
    "reservoir_empty_suspect": "critical",
    "low_wet_recovery_suspect": "warning",
    "sensor_fault": "warning"
}

SNAPSHOT_STATE_KEYS = (
    "pump_active",
    "pump_active_since",
    "pending_soak",
    "water_delivery_suspect",
    "reservoir_empty_suspect",
    "low_wet_recovery_suspect",
    "sensor_fault",
    "dynamic_cooldown",
    "predictor_circuit",
    "watering_trigger_guard",
    "recent_response_guard",
    "hard_safety_low_guard",
    "cloud_protection",
)


def _service_status(unit):
    if not unit:
        return {"unit": None, "status": "unknown"}
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        status = (result.stdout or result.stderr).strip().lower() or "unknown"
        return {"unit": unit, "status": status}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"unit": unit, "status": "unknown", "error": str(exc)}


def _database_timestamp_seconds(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _database_age_seconds(value, now):
    timestamp = _database_timestamp_seconds(value)
    if timestamp is None:
        return None
    return max(0.0, float(now) - timestamp)


def _classify_opengauss_error(message):
    text = str(message or "").strip().lower()
    if "password authentication failed" in text or "authentication failed" in text:
        return "authentication_failed"
    if "permission denied" in text or "must be owner" in text:
        return "permission_denied"
    if "database" in text and "does not exist" in text:
        return "database_missing"
    if ("relation" in text or "table" in text) and "does not exist" in text:
        return "table_missing"
    if any(marker in text for marker in (
        "connection refused",
        "could not connect",
        "failed to connect",
        "no such file or directory",
        "server closed the connection",
    )):
        return "port_unreachable"
    return "query_failed"


def _opengauss_command(sql):
    gauss_user = os.getenv("OPENGAUSS_OS_USER", "opengauss")
    gsql_path = os.getenv("OPENGAUSS_GSQL_PATH", "/usr/local/opengauss/bin/gsql")
    library_path = os.getenv("OPENGAUSS_LIBRARY_PATH", "/usr/local/opengauss/lib")
    database = os.getenv("OPENGAUSS_DATABASE", "soil_data")
    port = os.getenv("OPENGAUSS_PORT", "7654")
    return [
        "runuser", "-u", gauss_user, "--",
        "env", "LD_LIBRARY_PATH=%s" % library_path,
        gsql_path,
        "-d", database,
        "-p", port,
        "-t", "-A", "-F", "|",
        "-c", sql,
    ]


def _parse_canonical_soil_row(stdout):
    parts = str(stdout or "").strip().split("|", 3)
    if len(parts) != 4:
        return None
    timestamp, humidity, temperature, ec_raw = (part.strip() for part in parts)
    values = tuple(_safe_float(value) for value in (humidity, temperature, ec_raw))
    if (
        not timestamp
        or any(value is None or not math.isfinite(value) for value in values)
        or not (5.0 < values[0] <= 100.0)
        or not (0.0 <= values[1] <= 45.0)
        or not (0.0 <= values[2] <= 5000.0)
    ):
        return None
    recv_time = _database_timestamp_seconds(timestamp)
    if recv_time is None or recv_time > time.time():
        return None
    return {
        "timestamp": timestamp,
        "humidity": values[0],
        "temperature": values[1],
        "ec_raw": values[2],
    }


def _opengauss_latest_soil_reading(device_name, *, timeout=12, runner=subprocess.run):
    safe_device = str(device_name).replace("'", "''")
    sql = (
        "SELECT recv_time::text,humidity,temp,ec FROM soil_sensor_readings "
        "WHERE device_code='%s' AND humidity IS NOT NULL AND temp IS NOT NULL "
        "AND ec IS NOT NULL ORDER BY recv_time DESC,id DESC LIMIT 1;"
    ) % safe_device
    try:
        result = runner(
            _opengauss_command(sql),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_canonical_soil_row(result.stdout)


def _opengauss_health(device_name, now=None, timeout=12):
    current = float(now if now is not None else time.time())
    service = _service_status("opengauss.service")
    writer = _service_status("mqtt_direct_gauss.service")
    base = {
        "connected": False,
        "status": "unknown",
        "cause": None,
        "error": None,
        "service_status": service.get("status"),
        "writer_service_status": writer.get("status"),
        "latest_row_age_seconds": None,
        "latest_air_age_seconds": None,
    }
    if service.get("status") != "active":
        base.update(status="service_stopped", cause="service_stopped")
        return base

    safe_device = str(device_name).replace("'", "''")
    sql = (
        "SELECT "
        "COALESCE((SELECT recv_time::text FROM soil_sensor_readings "
        "WHERE device_code='%s' ORDER BY recv_time DESC,id DESC LIMIT 1),''),"
        "COALESCE((SELECT recv_time::text FROM soil_sensor_readings "
        "WHERE device_code='%s' AND air_humidity IS NOT NULL "
        "ORDER BY recv_time DESC,id DESC LIMIT 1),'');"
    ) % (safe_device, safe_device)
    try:
        result = subprocess.run(
            _opengauss_command(sql),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        base.update(status="query_timeout", cause="query_timeout")
        return base
    except OSError as exc:
        base.update(status="client_error", cause="client_error", error=str(exc)[:300])
        return base

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "openGauss query failed").strip()
        cause = _classify_opengauss_error(error)
        base.update(status=cause, cause=cause, error=" ".join(error.split())[:300])
        return base

    parts = result.stdout.strip().split("|", 1)
    latest_row_at = parts[0].strip() if parts else ""
    latest_air_at = parts[1].strip() if len(parts) > 1 else ""
    row_age = _database_age_seconds(latest_row_at, current)
    air_age = _database_age_seconds(latest_air_at, current)
    base.update(
        connected=True,
        status="ok",
        latest_row_age_seconds=round(row_age, 1) if row_age is not None else None,
        latest_air_age_seconds=round(air_age, 1) if air_age is not None else None,
    )
    if not latest_row_at:
        base.update(status="no_device_data", cause="no_device_data")
    elif writer.get("status") != "active":
        base.update(status="writer_service_stopped", cause="writer_service_stopped")
    elif not latest_air_at:
        base.update(status="air_humidity_missing", cause="air_humidity_missing")
    return base


def _recent_sensor_readings(path, limit):
    readings = deque(maxlen=max(int(limit), 2))
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            for row in csv.DictReader((line.replace("\x00", "") for line in handle)):
                try:
                    humidity = float(row.get("humidity"))
                except (TypeError, ValueError):
                    continue
                readings.append({
                    "timestamp": row.get("timestamp"),
                    "humidity": humidity,
                    "temperature": _safe_float(row.get("temperature")),
                    "ec_raw": _safe_float(row.get("ec_raw")),
                    "ec_norm": _safe_float(row.get("ec_norm")),
                    "vpd": _safe_float(row.get("vpd")),
                    "action_sec": _safe_float(row.get("action_sec")),
                })
    except OSError:
        return []
    return list(readings)


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_required_sensor_value(value):
    number = _safe_float(value)
    return number is not None and math.isfinite(number) and number != 0.0


def _local_timestamp(value):
    text = str(value or "").strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).astimezone()
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return None


def _latest_local_sensor_fault(path):
    """Return the latest raw row that openGauss would reject."""
    if not path:
        return None
    latest = None
    indexes = (("temperature", 2), ("humidity", 3), ("ec", 4))
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.reader((line.replace("\x00", "") for line in handle))
            next(reader, None)
            for row in reader:
                if len(row) < 5:
                    continue
                invalid_fields = []
                for name, index in indexes:
                    raw_value = str(row[index] or "").strip()
                    if not _valid_required_sensor_value(raw_value):
                        invalid_fields.append({
                            "field": name,
                            "display_value": 0,
                            "raw_value": raw_value or None,
                        })
                if invalid_fields:
                    latest = {
                        "observed_at": _local_timestamp(row[0]),
                        "invalid_fields": invalid_fields,
                        "source": "local_mqtt_sensor_log",
                    }
    except OSError:
        return None
    return latest


def _latest_local_sensor_observation(path, now=None):
    if not path:
        return None
    current = float(now if now is not None else time.time())
    latest = None
    indexes = (("temperature", 2), ("humidity", 3), ("ec", 4))
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.reader((line.replace("\x00", "") for line in handle))
            next(reader, None)
            for row in reader:
                if len(row) < 5:
                    continue
                observed_at = _local_timestamp(row[0])
                observed_ts = None
                if observed_at:
                    try:
                        observed_ts = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
                invalid_fields = [
                    name for name, index in indexes
                    if not _valid_required_sensor_value(str(row[index] or "").strip())
                ]
                latest = {
                    "observed_at": observed_at,
                    "age_seconds": round(max(0.0, current - observed_ts), 1) if observed_ts is not None else None,
                    "required_values_valid": not invalid_fields,
                    "invalid_fields": invalid_fields,
                    "source": "local_mqtt_sensor_log",
                }
    except OSError:
        return None
    return latest


def _recent_watering_history(path, limit):
    try:
        value = load_json(path)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    selected = []
    for item in value:
        if not isinstance(item, dict):
            continue
        selected.append({
            "timestamp": item.get("timestamp"),
            "prediction_timestamp": item.get("prediction_timestamp"),
            "request_id": item.get("request_id"),
            "plan_label": item.get("plan_label"),
            "status": item.get("status"),
            "water_sec": _safe_float(item.get("water_sec")),
            "humidity_before": _safe_float(item.get("humidity_before")),
            "humidity_after": _safe_float(item.get("humidity_after")),
            "delta_m": _safe_float(item.get("delta_m")),
            "reason": item.get("reason"),
        })
    return selected[-max(int(limit), 1):]


def build_health_snapshot(
    device_name,
    state_path,
    sensor_log_path=None,
    local_sensor_log_path=None,
    irrigation_trials_path=None,
    service_unit=None,
    mqtt_connected=True,
    sensor_limit=24,
    watering_limit=20,
    now=None,
):
    """Build a compact raw-facts message for cloud-side anomaly detection."""
    current = float(now if now is not None else time.time())
    state_error = None
    try:
        state = load_json(state_path)
        if not isinstance(state, dict):
            raise ValueError("state root must be an object")
        state_age = max(0.0, current - os.path.getmtime(state_path))
        state_valid = True
    except (FileNotFoundError, OSError, ValueError, TypeError) as exc:
        state = {}
        state_age = None
        state_valid = False
        state_error = str(exc)

    parent = Path(state_path).parent
    sensor_path = str(sensor_log_path or parent / "sensor_log.csv")
    trials_path = str(irrigation_trials_path or parent / "irrigation_trials.json")
    compact_state = {key: state.get(key) for key in SNAPSHOT_STATE_KEYS if key in state}
    gauss = state.get("air_humidity_fallback") or {}
    gauss_source = str(gauss.get("source") or "")
    gauss_health = _opengauss_health(device_name, now=current)
    gauss_age = gauss_health.get("latest_row_age_seconds")
    canonical_soil = _opengauss_latest_soil_reading(device_name)
    sensor_readings = [canonical_soil] if canonical_soil is not None else []
    latest_sensor = sensor_readings[-1] if sensor_readings else {}
    local_observation = _latest_local_sensor_observation(local_sensor_log_path, now=current)
    local_observation_age = (
        local_observation.get("age_seconds")
        if isinstance(local_observation, dict)
        else None
    )
    sensor_data_stale_seconds = 600.0
    sensor_data_current = (
        local_observation_age is not None
        and local_observation_age <= sensor_data_stale_seconds
    )
    observed_at = datetime.fromtimestamp(current, timezone.utc).isoformat().replace("+00:00", "Z")
    raw_id = "%s:%0.3f" % (device_name, current)
    snapshot_id = "snap-" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
    return {
        "schema_version": 2,
        "message_type": "phase3_health_snapshot",
        "event_type": "health_snapshot",
        "event_id": snapshot_id,
        "snapshot_id": snapshot_id,
        "device_code": device_name,
        "observed_at": observed_at,
        "state_file": {
            "valid": state_valid,
            "age_seconds": round(state_age, 1) if state_age is not None else None,
            "error": state_error,
        },
        "phase3_service": _service_status(service_unit or "phase3_%s.service" % device_name),
        "sensor_readings": sensor_readings,
        "local_sensor_diagnostics": {
            "latest_invalid_required_values": _latest_local_sensor_fault(local_sensor_log_path),
            "latest_observation": local_observation,
        },
        "watering_history": _recent_watering_history(trials_path, watering_limit),
        "system_state": compact_state,
        "environment": {
            "soil": {
                "observed_at": latest_sensor.get("timestamp"),
                "humidity_percent": latest_sensor.get("humidity"),
                "temperature_c": latest_sensor.get("temperature"),
                "ec_raw": latest_sensor.get("ec_raw"),
                "ec_normalized": latest_sensor.get("ec_norm"),
                "vpd_kpa": latest_sensor.get("vpd"),
            },
            "air": {
                "temperature_c": _safe_float(gauss.get("air_temp")),
                "humidity_percent": _safe_float(gauss.get("rh")),
                "source": gauss_source or None,
                "age_seconds": gauss_age,
            },
            "pump": {
                "active": bool(state.get("pump_active")),
                "last_command_seconds": _safe_float(state.get("pump_last_command_sec")),
                "total_cycles": state.get("pump_total_cycles"),
                "total_water_seconds": _safe_float(state.get("total_water_sec_dispensed")),
            },
        },
        "links": {
            "mqtt": {
                "connected": bool(mqtt_connected),
                "source": "iotda_mqtt_client",
            },
            "iotda": {
                "connected": bool(mqtt_connected),
                "source": "iotda_mqtt_client",
            },
            "sensor_data": {
                "current": sensor_data_current,
                "last_observed_at": (
                    local_observation.get("observed_at")
                    if isinstance(local_observation, dict)
                    else None
                ),
                "age_seconds": local_observation_age,
                "stale_after_seconds": sensor_data_stale_seconds,
                "source": "local_sensor_observation",
            },
            "opengauss": {
                **gauss_health,
                "age_seconds": gauss_age,
                "source": gauss_source or None,
            },
        },
    }


def _active(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        if "active" in value:
            active = value["active"]
            if isinstance(active, bool):
                return active
            if isinstance(active, (int, float)):
                return active == 1
            if isinstance(active, str):
                return active.strip().lower() in ("1", "true", "yes", "on", "active")
            return False
        if "status" in value:
            return str(value["status"]).lower() in ("active", "open", "fault")
        return bool(value)
    return False


def detect_events(device_name, state_path, stale_after_seconds):
    events = []
    try:
        state = load_json(state_path)
        age = max(0, time.time() - os.path.getmtime(state_path))
    except (FileNotFoundError, ValueError) as exc:
        state, age = {}, stale_after_seconds + 1
        events.append(_event(device_name, "state_file_error", "critical", {"error": str(exc)}))

    if age > stale_after_seconds:
        events.append(_event(device_name, "sensor_stale", "warning", {"state_age_seconds": round(age)}))
    if isinstance(state, dict):
        for key, severity in ABNORMAL_KEYS.items():
            value = state.get(key)
            if _active(value):
                events.append(_event(device_name, key, severity, {"state": value}))
    return events


def _event(device_name, event_type, severity, details):
    bucket = int(time.time() // 60)
    raw = "%s:%s:%s" % (device_name, event_type, bucket)
    return {
        "event_id": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24],
        "event_type": event_type,
        "device_code": device_name,
        "severity": severity,
        "occurred_at": utc_timestamp(),
        "details": details
    }


class EventSpool:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                device_code TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                sent_at REAL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS alert_cooldown (
                device_code TEXT PRIMARY KEY,
                last_reserved_at REAL NOT NULL,
                last_event_id TEXT NOT NULL
            )
        """)
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def enqueue(self, event, cooldown_seconds):
        with self.lock:
            try:
                cutoff = time.time() - cooldown_seconds
                duplicate = self.db.execute(
                    "SELECT 1 FROM events WHERE device_code=? AND event_type=? AND created_at>=? LIMIT 1",
                    (event["device_code"], event["event_type"], cutoff)
                ).fetchone()
                if duplicate:
                    return False
                current = time.time()
                self.db.execute(
                    "INSERT OR IGNORE INTO events(event_id,device_code,event_type,payload,created_at,next_attempt_at) VALUES(?,?,?,?,?,?)",
                    (event["event_id"], event["device_code"], event["event_type"], json.dumps(event, ensure_ascii=False), current, current)
                )
                self.db.commit()
                return True
            except sqlite3.Error:
                self.db.rollback()
                raise

    def reserve_alert(self, device_code, event_id, cooldown_seconds, now=None):
        """Atomically reserve the device's one allowed alert in a cooldown window."""
        current = float(now if now is not None else time.time())
        cooldown = max(0.0, float(cooldown_seconds))
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute(
                    "SELECT last_reserved_at FROM alert_cooldown WHERE device_code=?",
                    (device_code,),
                ).fetchone()
                if row and current - float(row[0]) < cooldown:
                    self.db.commit()
                    return False
                self.db.execute(
                    "INSERT INTO alert_cooldown(device_code,last_reserved_at,last_event_id) VALUES(?,?,?) "
                    "ON CONFLICT(device_code) DO UPDATE SET "
                    "last_reserved_at=excluded.last_reserved_at,last_event_id=excluded.last_event_id",
                    (device_code, current, event_id),
                )
                self.db.commit()
                return True
            except sqlite3.Error:
                self.db.rollback()
                raise

    def pending(self, limit=50):
        with self.lock:
            rows = self.db.execute(
                "SELECT event_id,device_code,payload,attempts FROM events WHERE sent_at IS NULL AND next_attempt_at<=? ORDER BY created_at LIMIT ?",
                (time.time(), limit)
            ).fetchall()
        return [{"event_id": r[0], "device_code": r[1], "payload": json.loads(r[2]), "attempts": r[3]} for r in rows]

    def mark_sent(self, event_id):
        with self.lock:
            self.db.execute("UPDATE events SET sent_at=? WHERE event_id=?", (time.time(), event_id))
            self.db.commit()

    def mark_failed(self, event_id, attempts):
        delay = min(3600, 5 * (2 ** min(attempts, 8)))
        with self.lock:
            self.db.execute("UPDATE events SET attempts=attempts+1,next_attempt_at=? WHERE event_id=?", (time.time() + delay, event_id))
            self.db.commit()
