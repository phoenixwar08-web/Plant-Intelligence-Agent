"""
MQTT -> openGauss ingest.

Storage model:
  1. mqtt_raw_events: append-only raw MQTT payloads.
  2. soil_sensor_readings: unified sensor time-series keyed by device_code.
  3. irrigation_events: physical watering events with water_sec > 0.
  4. pump_command_events: pump on/off command audit rows.
  5. legacy soil*_data tables: compatibility writes for older readers.

The writer must never stop because one table or one gsql call is slow. Buffers
are split by destination and failed batches are re-queued independently.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import paho.mqtt.client as mqtt


GAUSS_DB = "soil_data"
GAUSS_PORT = "7654"
GSQL_TIMEOUT_SEC = 60
GSQL_RETRIES = 2

MQTT_BROKER = "localhost"
MQTT_PORT = 1883

SPOOL_DIR = Path("/root/agent/pi_agnet/spool")
SPOOL_DIR.mkdir(parents=True, exist_ok=True)

SOIL_TOPICS = {
    "esp32/soil1": {"device_code": "soil1", "legacy_table": "soil1_data"},
    "esp32/soil2": {"device_code": "soil2", "legacy_table": "soil2_data"},
    "esp32/soil3": {"device_code": "soil3", "legacy_table": "soil3_data"},
    "esp32/soil_test": {"device_code": "soil_test", "legacy_table": "soil_test_data"},
}

WATER_TOPICS = {
    "esp32/watering1": {"device_code": "soil1", "pump_topic": "esp32/pump1/cmd"},
    "esp32/watering2": {"device_code": "soil2", "pump_topic": "esp32/pump2/cmd"},
    "esp32/watering3": {"device_code": "soil3", "pump_topic": "esp32/pump3/cmd"},
}

# soil3 direct pump command monitor: records bypass/manual on/off commands.
PUMP_CMD_TOPICS = {
    "esp32/pump3/cmd": {"device_code": "soil3", "pump_topic": "esp32/pump3/cmd"},
}

PUMP_META_TOPICS = {
    "esp32/pump3/cmd_meta": {"device_code": "soil3", "pump_topic": "esp32/pump3/cmd"},
}

TOPICS = {**SOIL_TOPICS, **WATER_TOPICS, **PUMP_CMD_TOPICS, **PUMP_META_TOPICS}

BATCH_SIZE = 50
FLUSH_INTERVAL = 2.0

raw_buffer: list[dict[str, Any]] = []
sensor_buffer: list[dict[str, Any]] = []
irrigation_buffer: list[dict[str, Any]] = []
pump_command_buffer: list[dict[str, Any]] = []
legacy_buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)

water_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
pump_cmd_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
pump_cmd_meta: dict[str, list[dict[str, Any]]] = defaultdict(list)
last_soil_time: dict[str, Optional[datetime]] = defaultdict(lambda: None)
buffer_lock = threading.Lock()

stats = defaultdict(int)

PUMP_CMD_RECONCILE_WINDOW_SEC = 8.0
PUMP_CMD_RECONCILE_TOLERANCE_SEC = 1.25
PUMP_META_TTL_SEC = 15 * 60

# soil2 的 ESP32 在 RTC/NTP 尚未同步时会发布 2000-01-01。该值不能作为
# 传感器时间写入数据库，否则按 recv_time 查询会误认为数据已停止更新。
SOIL2_MIN_VALID_REPORTED_TIME = datetime(2024, 1, 1)


def parse_dt(tstr: Optional[str]) -> datetime:
    if not tstr:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(str(tstr), fmt)
        except ValueError:
            pass
    return datetime.now()


def correct_soil_time(device_code: str, reported_time: datetime, received_at: datetime) -> datetime:
    """Use broker receive time only for soil2's clearly invalid RTC timestamp."""
    if device_code == "soil2" and reported_time < SOIL2_MIN_VALID_REPORTED_TIME:
        print(
            "  [时间修正] soil2 上报时间 "
            f"{reported_time} 无效，改用接收时间 {received_at}"
        )
        return received_at
    return reported_time


def normalize_pump_command(command: Any) -> str:
    text = str(command or "").strip().lower()
    if text in {"1", "true", "start"}:
        return "on"
    if text in {"0", "false", "stop"}:
        return "off"
    return text


def remember_pump_command(device_code: str, command: Any, when: datetime) -> None:
    cmd = normalize_pump_command(command)
    if cmd not in {"on", "off"}:
        return
    events = pump_cmd_events[device_code]
    events.append({"time": when, "cmd": cmd})
    cutoff = when.timestamp() - 120.0
    pump_cmd_events[device_code] = [
        ev for ev in events
        if isinstance(ev.get("time"), datetime) and ev["time"].timestamp() >= cutoff
    ]


def reconciled_watering_sec(
    device_code: str,
    reported_sec: float,
    event_time: datetime,
) -> tuple[float, Optional[str]]:
    """Prefer the observed pump on/off interval when ESP watering_sec is off by ~1s."""
    events = pump_cmd_events.get(device_code) or []
    if not events:
        return reported_sec, None

    nearby_off = [
        ev for ev in events
        if ev.get("cmd") == "off"
        and abs((event_time - ev["time"]).total_seconds()) <= PUMP_CMD_RECONCILE_WINDOW_SEC
    ]
    if not nearby_off:
        return reported_sec, None
    off_ev = max(nearby_off, key=lambda ev: ev["time"])
    on_candidates = [
        ev for ev in events
        if ev.get("cmd") == "on"
        and ev["time"] <= off_ev["time"]
        and (off_ev["time"] - ev["time"]).total_seconds() <= max(
            PUMP_CMD_RECONCILE_WINDOW_SEC,
            reported_sec + PUMP_CMD_RECONCILE_WINDOW_SEC,
        )
    ]
    if not on_candidates:
        return reported_sec, None
    on_ev = max(on_candidates, key=lambda ev: ev["time"])
    interval_sec = max(0.0, (off_ev["time"] - on_ev["time"]).total_seconds())
    if interval_sec <= 0:
        return reported_sec, None
    if abs(interval_sec - reported_sec) > PUMP_CMD_RECONCILE_TOLERANCE_SEC:
        return reported_sec, None
    interval_sec = round(interval_sec, 3)
    if abs(interval_sec - reported_sec) < 0.001:
        return reported_sec, None
    return interval_sec, f"pump_on_off_interval:{reported_sec:.3f}->{interval_sec:.3f}"


def sql_quote(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def sql_num(value: Any, default: float = 0.0) -> str:
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return str(float(default))


def sql_optional_num(value: Any) -> str:
    if value is None:
        return "NULL"
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return "NULL"


def sql_bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def valid_required_soil_num(value: Any) -> bool:
    """Soil temp/humidity/ec must be present and non-zero; lux=0 can be real at night."""
    if value is None or value == "":
        return False
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return False


def gsql_exec(sql: str, label: str) -> None:
    """Execute SQL through gsql using a readable temp file for robust quoting."""
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sql", delete=False) as f:
            f.write(sql)
            if not sql.endswith("\n"):
                f.write("\n")
            tmp_name = f.name
        os.chmod(tmp_name, 0o644)

        cmd = [
            "su",
            "-",
            "opengauss",
            "-c",
            f"gsql -d {GAUSS_DB} -p {GAUSS_PORT} -v ON_ERROR_STOP=1 -f {tmp_name}",
        ]
        last_error = None
        for attempt in range(1, GSQL_RETRIES + 2):
            try:
                r = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=GSQL_TIMEOUT_SEC,
                )
                if r.returncode == 0:
                    return
                last_error = r.stderr.strip() or r.stdout.strip()
                print(f"  [gsql ERROR] {label} attempt={attempt}: {last_error}")
            except subprocess.TimeoutExpired as e:
                last_error = f"timeout>{GSQL_TIMEOUT_SEC}s"
                print(f"  [gsql TIMEOUT] {label} attempt={attempt}: {last_error}")
                if e.stderr:
                    print(str(e.stderr)[-500:])
            time.sleep(min(2 * attempt, 10))
        raise RuntimeError(f"gsql failed for {label}: {last_error}")
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def append_spool(name: str, rows: list[dict[str, Any]], error: Exception) -> None:
    path = SPOOL_DIR / f"{name}.failed.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({"row": row, "error": str(error), "ts": time.time()}, ensure_ascii=False) + "\n")


def build_raw_insert(rows: list[dict[str, Any]]) -> str:
    values = []
    for r in rows:
        values.append(
            "("
            f"{sql_quote(r['received_at'])},"
            f"{sql_quote(r['topic'])},"
            f"{sql_quote(r['payload'])},"
            f"{sql_quote(r.get('device_code'))},"
            f"{sql_quote(r.get('device_id'))},"
            f"{sql_bool(bool(r.get('parsed_ok')))},"
            f"{sql_quote(r.get('error'))}"
            ")"
        )
    return (
        "INSERT INTO mqtt_raw_events "
        "(received_at, topic, payload, device_code, device_id, parsed_ok, error) VALUES "
        + ",".join(values)
        + ";"
    )


def build_sensor_insert(rows: list[dict[str, Any]]) -> str:
    values = []
    for r in rows:
        values.append(
            "("
            f"{sql_quote(r['device_code'])},"
            f"{sql_quote(r.get('device_id'))},"
            f"{sql_quote(r['recv_time'])},"
            f"{sql_num(r.get('temp'))},"
            f"{sql_num(r.get('humidity'))},"
            f"{sql_num(r.get('ec'))},"
            f"{sql_num(r.get('lux'))},"
            f"{sql_optional_num(r.get('air_humidity'))},"
            f"{int(r.get('watering_flag') or 0)},"
            f"{sql_num(r.get('watering_sec'))},"
            f"{sql_quote(r.get('source', 'mqtt'))},"
            f"{sql_quote(r.get('topic'))},"
            f"{sql_quote(r.get('payload'))}"
            ")"
        )
    return (
        "INSERT INTO soil_sensor_readings "
        "(device_code, device_id, recv_time, temp, humidity, ec, lux, air_humidity, watering_flag, watering_sec, source, topic, raw_payload) VALUES "
        + ",".join(values)
        + ";"
    )


def build_legacy_insert(table: str, rows: list[dict[str, Any]]) -> str:
    values = []
    for r in rows:
        values.append(
            "("
            f"{sql_quote(r['recv_time'])},"
            f"{sql_quote(r.get('device_id'))},"
            f"{sql_num(r.get('temp'))},"
            f"{sql_num(r.get('humidity'))},"
            f"{sql_num(r.get('ec'))},"
            f"{int(r.get('watering_flag') or 0)},"
            f"{sql_num(r.get('lux'))},"
            f"{sql_num(r.get('watering_sec'))},"
            f"{sql_optional_num(r.get('air_humidity'))}"
            ")"
        )
    return (
        f"INSERT INTO {table} "
        "(recv_time, device_id, temp, humidity, ec, watering_flag, lux, watering_sec, air_humidity) VALUES "
        + ",".join(values)
        + ";"
    )


def build_irrigation_insert(rows: list[dict[str, Any]]) -> str:
    values = []
    for r in rows:
        values.append(
            "("
            f"{sql_quote(r['device_code'])},"
            f"{sql_quote(r['command_time'])},"
            f"{sql_quote(r.get('pump_topic'))},"
            f"{sql_num(r.get('water_sec'))},"
            f"{sql_quote(r.get('reason', 'mqtt_watering_event'))},"
            f"{sql_quote(r.get('source', 'mqtt'))},"
            f"{sql_quote(r.get('status', 'issued'))},"
            f"{sql_quote(r.get('payload'))},"
            f"{sql_quote(r.get('operator_id'))},"
            f"{sql_quote(r.get('operator_name'))},"
            f"{sql_quote(r.get('request_id'))},"
            f"{sql_quote(r.get('command_payload'))}"
            ")"
        )
    return (
        "INSERT INTO irrigation_events "
        "(device_code, command_time, pump_topic, water_sec, reason, source, status, raw_payload, operator_id, operator_name, request_id, command_payload) VALUES "
        + ",".join(values)
        + ";"
    )


def build_pump_command_insert(rows: list[dict[str, Any]]) -> str:
    values = []
    for r in rows:
        values.append(
            "("
            f"{sql_quote(r['device_code'])},"
            f"{sql_quote(r['command_time'])},"
            f"{sql_quote(r.get('pump_topic'))},"
            f"{sql_num(r.get('water_sec'))},"
            f"{sql_quote(r.get('reason', 'direct_pump_cmd_unattributed'))},"
            f"{sql_quote(r.get('source', 'unknown_direct_mqtt'))},"
            f"{sql_quote(r.get('status', 'unattributed_direct_cmd'))},"
            f"{sql_quote(r.get('payload'))},"
            f"{sql_quote(r.get('operator_id'))},"
            f"{sql_quote(r.get('operator_name'))},"
            f"{sql_quote(r.get('request_id'))},"
            f"{sql_quote(r.get('command_payload'))}"
            ")"
        )
    return (
        "INSERT INTO pump_command_events "
        "(device_code, command_time, pump_topic, water_sec, reason, source, status, raw_payload, operator_id, operator_name, request_id, command_payload) VALUES "
        + ",".join(values)
        + ";"
    )


def requeue(target: str, rows: list[dict[str, Any]]) -> None:
    with buffer_lock:
        if target == "raw":
            raw_buffer[0:0] = rows
        elif target == "sensor":
            sensor_buffer[0:0] = rows
        elif target == "irrigation":
            irrigation_buffer[0:0] = rows
        elif target == "pump_command":
            pump_command_buffer[0:0] = rows
        else:
            legacy_buffers[target][0:0] = rows


def flush_target(name: str, rows: list[dict[str, Any]], sql_builder) -> None:
    if not rows:
        return
    try:
        gsql_exec(sql_builder(rows), name)
        stats[f"{name}_written"] += len(rows)
        print(f"  -> openGauss 写入 {len(rows)} 条到 {name}")
    except Exception as e:
        stats[f"{name}_failed"] += len(rows)
        append_spool(name, rows, e)
        requeue(name, rows)
        print(f"  [flush ERROR] {name}: {e}")


def flush_legacy(table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        gsql_exec(build_legacy_insert(table, rows), table)
        stats[f"{table}_written"] += len(rows)
        print(f"  -> openGauss 兼容写入 {len(rows)} 条到 {table}")
    except Exception as e:
        stats[f"{table}_failed"] += len(rows)
        append_spool(table, rows, e)
        requeue(table, rows)
        print(f"  [legacy flush ERROR] {table}: {e}")


def flush_all() -> None:
    with buffer_lock:
        raw_rows = raw_buffer[:]
        sensor_rows = sensor_buffer[:]
        irrigation_rows = irrigation_buffer[:]
        pump_command_rows = pump_command_buffer[:]
        legacy_snapshot = {table: rows[:] for table, rows in legacy_buffers.items() if rows}

        raw_buffer.clear()
        sensor_buffer.clear()
        irrigation_buffer.clear()
        pump_command_buffer.clear()
        for table in legacy_snapshot:
            legacy_buffers[table].clear()

    flush_target("raw", raw_rows, build_raw_insert)
    flush_target("sensor", sensor_rows, build_sensor_insert)
    flush_target("irrigation", irrigation_rows, build_irrigation_insert)
    flush_target("pump_command", pump_command_rows, build_pump_command_insert)
    for table, rows in legacy_snapshot.items():
        flush_legacy(table, rows)


def timed_flush() -> None:
    while True:
        time.sleep(FLUSH_INTERVAL)
        try:
            flush_all()
        except Exception:
            print("[timed_flush ERROR] unexpected failure, keeping thread alive")
            traceback.print_exc()


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        for topic in TOPICS:
            client.subscribe(topic)
            print(f"  订阅: {topic}")
        print("MQTT 已连接，开始监听...\n")
    else:
        print(f"MQTT 连接失败: {rc}")


def add_raw_event(
    topic: str,
    payload: str,
    parsed_ok: bool,
    data: Optional[dict[str, Any]],
    error: Optional[str] = None,
) -> None:
    cfg = TOPICS.get(topic) or {}
    device_code = cfg.get("device_code")
    with buffer_lock:
        raw_buffer.append(
            {
                "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "topic": topic,
                "payload": payload,
                "device_code": device_code,
                "device_id": (data or {}).get("device_id"),
                "parsed_ok": parsed_ok,
                "error": error,
            }
        )


def remember_pump_meta(topic: str, data: Optional[dict[str, Any]]) -> bool:
    if topic not in PUMP_META_TOPICS:
        return False
    if not isinstance(data, dict):
        return True
    cfg = PUMP_META_TOPICS[topic]
    now = datetime.now()
    meta = dict(data)
    meta["_received_at"] = now
    meta.setdefault("device_code", cfg["device_code"])
    meta.setdefault("pump_topic", cfg.get("pump_topic"))
    events = pump_cmd_meta[cfg["device_code"]]
    events.append(meta)
    cutoff = now.timestamp() - PUMP_META_TTL_SEC
    pump_cmd_meta[cfg["device_code"]] = [
        ev for ev in events
        if isinstance(ev.get("_received_at"), datetime) and ev["_received_at"].timestamp() >= cutoff
    ]
    print(
        "  [pump元数据] "
        f"{cfg['device_code']} source={meta.get('source') or 'unknown'} "
        f"operator={meta.get('operator_id') or 'unknown'} "
        f"request={meta.get('request_id') or '-'}"
    )
    return True


def latest_pump_meta(device_code: str, when: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    events = pump_cmd_meta.get(device_code) or []
    if not events:
        return None
    when = when or datetime.now()
    valid = [
        ev for ev in events
        if isinstance(ev.get("_received_at"), datetime)
        and 0 <= (when - ev["_received_at"]).total_seconds() <= PUMP_META_TTL_SEC
    ]
    if not valid:
        return None
    return max(valid, key=lambda ev: ev["_received_at"])


def apply_pump_meta(row: dict[str, Any], meta: Optional[dict[str, Any]], *, apply_status: bool = True) -> dict[str, Any]:
    if not meta:
        return row
    row["source"] = meta.get("source") or row.get("source")
    row["reason"] = meta.get("reason") or row.get("reason")
    row["operator_id"] = meta.get("operator_id") or row.get("operator_id")
    row["operator_name"] = meta.get("operator_name") or row.get("operator_name")
    row["request_id"] = meta.get("request_id") or row.get("request_id")
    if apply_status:
        row["status"] = meta.get("status") or row.get("status")
    return row


def handle_direct_pump_command(topic: str, payload: str, data: Optional[dict[str, Any]]) -> bool:
    if topic not in PUMP_CMD_TOPICS:
        return False
    cfg = PUMP_CMD_TOPICS[topic]
    now = datetime.now()
    operator_id = None
    operator_name = None
    request_id = None
    source = "direct_mqtt"
    reason = "direct_pump_cmd_unattributed"
    status = "unattributed_direct_cmd"
    water_sec = 0.0

    command_payload = payload.strip()
    if isinstance(data, dict):
        operator_id = data.get("operator_id")
        operator_name = data.get("operator_name")
        request_id = data.get("request_id")
        source = data.get("source") or source
        reason = data.get("reason") or reason
        status = data.get("status") or ("manual_direct_cmd" if operator_id else status)
        water_sec = float(data.get("water_sec") or data.get("watering_sec") or data.get("sec") or 0)
        command_payload = data.get("cmd") or data.get("command") or payload.strip()

    remember_pump_command(cfg["device_code"], command_payload, now)
    meta = latest_pump_meta(cfg["device_code"], now)

    with buffer_lock:
        row = {
            "device_code": cfg["device_code"],
            "command_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "pump_topic": cfg.get("pump_topic"),
            "water_sec": water_sec,
            "reason": reason,
            "source": source if operator_id else "unknown_direct_mqtt",
            "status": status,
            "payload": payload,
            "operator_id": operator_id or "unknown",
            "operator_name": operator_name or "unknown",
            "request_id": request_id,
            "command_payload": command_payload,
        }
        if not operator_id:
            apply_pump_meta(row, meta)
        pump_command_buffer.append(
            {
                **row,
            }
        )
    print(
        f"  [pump3直接命令] {cfg['device_code']} {command_payload} "
        f"operator={row.get('operator_id') or 'unknown'} source={row.get('source') or 'unknown'}"
    )
    return True


def on_message(client, userdata, msg: mqtt.MQTTMessage):
    topic = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace")
    retained_control_message = bool(getattr(msg, "retain", False)) and (
        topic in PUMP_CMD_TOPICS or topic in PUMP_META_TOPICS
    )
    data = None
    parsed_ok = True
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        parsed_ok = False
        if retained_control_message:
            add_raw_event(topic, payload, False, None, "retained_pump_control_ignored")
            print(f"  [pump控制] 忽略 retained 历史消息 {topic}: {payload}")
            return
        if topic in PUMP_CMD_TOPICS:
            add_raw_event(topic, payload, False, None, str(e))
            handle_direct_pump_command(topic, payload, None)
            return
        add_raw_event(topic, payload, False, None, str(e))
        return

    add_raw_event(topic, payload, parsed_ok, data)
    if retained_control_message:
        print(f"  [pump控制] 忽略 retained 历史消息 {topic}: {payload}")
        return
    if remember_pump_meta(topic, data):
        return
    if handle_direct_pump_command(topic, payload, data):
        return

    if topic in WATER_TOPICS:
        cfg = WATER_TOPICS[topic]
        device_code = cfg["device_code"]
        w_time = parse_dt(data.get("time"))
        reported_sec = float(data.get("watering_sec") or data.get("sec") or 0)
        sec, reconcile_reason = reconciled_watering_sec(device_code, reported_sec, w_time)
        meta = latest_pump_meta(device_code, w_time)
        water_events[device_code].append({"time": w_time, "sec": sec})
        status = data.get("status", "issued")
        if reconcile_reason:
            status = "issued_duration_reconciled"
        with buffer_lock:
            row = {
                "device_code": device_code,
                "command_time": w_time.strftime("%Y-%m-%d %H:%M:%S"),
                "pump_topic": cfg.get("pump_topic"),
                "water_sec": sec,
                "payload": payload,
                "operator_id": data.get("operator_id"),
                "operator_name": data.get("operator_name"),
                "request_id": data.get("request_id"),
                "command_payload": data.get("command") or data.get("cmd"),
                "reason": data.get("reason", "mqtt_watering_event"),
                "source": data.get("source", "mqtt"),
                "status": status,
            }
            if not data.get("operator_id"):
                apply_pump_meta(row, meta, apply_status=False)
            irrigation_buffer.append(row)
        if reconcile_reason:
            print(f"  [浇水事件] {device_code} {w_time} +{sec}s ({reconcile_reason}) source={row.get('source')}")
        else:
            print(f"  [浇水事件] {device_code} {w_time} +{sec}s source={row.get('source')}")
        return

    if topic not in SOIL_TOPICS:
        return

    cfg = SOIL_TOPICS[topic]
    device_code = cfg["device_code"]
    received_at = datetime.now()
    reported_time = parse_dt(data.get("time"))
    soil_time = correct_soil_time(device_code, reported_time, received_at)
    print(f"  [土壤数据] {topic} | {soil_time} | 设备:{data.get('device_id','')}")

    if last_soil_time[device_code] is not None and soil_time == last_soil_time[device_code]:
        print("    重复数据，跳过")
        return

    total_sec = 0.0
    remain = []
    window_start = last_soil_time[device_code]
    for ev in water_events[device_code]:
        if window_start is None:
            if ev["time"] <= soil_time:
                total_sec += float(ev["sec"])
            else:
                remain.append(ev)
        else:
            if window_start < ev["time"] <= soil_time:
                total_sec += float(ev["sec"])
            else:
                remain.append(ev)
    water_events[device_code] = remain

    row = {
        "device_code": device_code,
        "recv_time": soil_time.strftime("%Y-%m-%d %H:%M:%S"),
        "device_id": data.get("device_id", ""),
        "temp": data.get("temp"),
        "humidity": data.get("humidity"),
        "ec": data.get("ec"),
        "lux": data.get("lux", 0),
        "air_humidity": data.get("air_humidity"),
        "watering_flag": 1 if total_sec > 0 else 0,
        "watering_sec": total_sec,
        "topic": topic,
        "payload": payload,
    }

    # 只把温度/湿度作为必填硬字段；EC 为 0 或缺失（部分 ESP32 的 EC 探头读数
    # 不可用时报 0/"--"）不能丢弃整条土壤数据，否则会误判传感器断联。
    invalid_fields = [
        name for name in ("temp", "humidity")
        if not valid_required_soil_num(row.get(name))
    ]
    if invalid_fields:
        stats[f"{device_code}_dirty_sensor_skipped"] += 1
        print(
            "    [dirty sensor skip] "
            f"{device_code} {soil_time} invalid={','.join(invalid_fields)} "
            f"temp={row.get('temp')} humidity={row.get('humidity')} ec={row.get('ec')} "
            f"lux={row.get('lux')} raw preserved"
        )
        return

    with buffer_lock:
        sensor_buffer.append(row)
        legacy_buffers[cfg["legacy_table"]].append(row)
        pending = len(sensor_buffer) + sum(len(v) for v in legacy_buffers.values())

    last_soil_time[device_code] = soil_time

    if pending >= BATCH_SIZE:
        flush_all()


def main():
    print("=" * 50)
    print("MQTT -> openGauss ingest: raw + unified sensor + legacy compatibility")
    print("=" * 50)

    threading.Thread(target=timed_flush, daemon=True).start()

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            print(f"MQTT 异常: {e}，5秒后重连...")
            time.sleep(5)


if __name__ == "__main__":
    main()
