#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path("/root/.openclaw/workspace")
CONFIG_PATH = Path("/root/.openclaw/openclaw.json")
EXTERNAL_WATERING = ROOT / "tools" / "openclaw_external_watering.py"
EVENT_MEMORY = ROOT / "tools" / "openclaw_plant_event_memory.py"

DEVICE_PROFILES = {
    "soil3": {
        "agent_id": "main",
        "account_id": "main",
        "group_id": "664EDE9402BCA3FE56EA6249185F4BB3",
        # QQ member_openid of the designated operator for control actions.
        "control_user_ids": ["08B455D945227B24B40BF03C97E4D561"],
        "default_seconds": 3.0,
    },
    "soil2": {
        "agent_id": "qqbot4",
        "account_id": "qqbot4",
        "group_id": "B0A2122480841340AAD793955A1CEA03",
        "control_user_ids": ["AAB0D1413A04505DC2B453586A74E770"],
        # soil2 never invents a dose; a pending Phase3 recommendation supplies it.
        "default_seconds": 0.0,
    },
}

DEVICE_CODE = "soil3"
AGENT_ID = "main"
ACCOUNT_ID = "main"
GROUP_ID = DEVICE_PROFILES[DEVICE_CODE]["group_id"]
SESSIONS_DIR = Path("/root/.openclaw/agents/main/sessions")
GATEWAY = ROOT / "tools" / "openclaw_soil3_water.py"
PHASE3_LOG = Path("/root/water/phase3/soil3/irrigation_system.log")
STATE_PATH = ROOT / "state" / "openclaw_irrigation_watcher.json"
EVENT_LOG = ROOT / "logs" / "openclaw_irrigation_watcher.jsonl"
LOCK_PATH = Path("/tmp/openclaw_irrigation_watcher.lock")
POLL_SEC = 5
CONFIRM_WINDOW_SEC = 120
SUPPRESS_MAIN_SEC = 180
PROMPT_MIN_INTERVAL_SEC = 6 * 3600
AUTO_CHECK_MIN_INTERVAL_SEC = 60
DEFAULT_SECONDS = 3.0

USER_RE = re.compile(r"^\[(?P<name>.+?) \((?P<id>[A-Fa-f0-9]+)\)\]\s*(?P<text>.*)$")
SECONDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*秒")
ML_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ml|ML|mL|毫升)")


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def log_event(event: dict[str, Any]) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    event.setdefault("at", now_iso())
    with EVENT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def iso_after(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds).astimezone().isoformat(timespec="seconds")


def control_user_ids() -> set[str]:
    return {
        str(user_id).upper()
        for user_id in DEVICE_PROFILES[DEVICE_CODE].get("control_user_ids", [])
        if str(user_id).strip()
    }


def control_button_permission() -> dict[str, Any]:
    """Keep control buttons clickable only by the configured device operator."""
    user_ids = sorted(control_user_ids())
    if user_ids:
        return {"type": 0, "specify_user_ids": user_ids}
    # A device with no designated operator may only be controlled by group admins.
    return {"type": 1}


def is_control_user(msg: dict[str, Any]) -> bool:
    user_ids = control_user_ids()
    return not user_ids or str(msg.get("id") or "").upper() in user_ids


def record_plant_event(event: dict[str, Any]) -> dict[str, Any]:
    event.setdefault("device_code", "soil3")
    event.setdefault("source", "openclaw_irrigation_watcher")
    try:
        result = subprocess.run(
            [str(EVENT_MEMORY), "--append-json", "-", "--device", event["device_code"]],
            input=json.dumps(event, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=20,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else {
            "ok": False,
            "error": (result.stderr or result.stdout)[:1000],
        }
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
    log_event({"type": "plant_event_memory_result", "event": event, "result": payload})
    return payload


def close_prompt_event(pending: dict[str, Any] | None, resolution: str, summary: str) -> None:
    if not pending:
        return
    record_plant_event(
        {
            "event_id": pending.get("id"),
            "event_type": "watering_confirmation_requested",
            "status": "resolved",
            "importance": "normal",
            "summary": summary,
            "resolution": resolution,
            "resolved_at": now_iso(),
            "evidence": {
                "recommended_action": pending.get("action"),
                "recommended_seconds": pending.get("seconds"),
            },
        }
    )


def initial_state() -> dict[str, Any]:
    return {
        "last_seen_ts_ms": now_ms() - 10_000,
        "pending": None,
        "suppress_main_until": [],
        "last_prompt_at": 0,
        "last_auto_check_at": 0,
    }


def prune_suppressions(state: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    items = state.get("suppress_main_until") or []
    state["suppress_main_until"] = [
        item for item in items
        if float(item.get("expires_at") or 0) > now
    ]
    return state


def add_main_suppression(
    state: dict[str, Any],
    msg: dict[str, Any],
    action: str,
    pending: dict[str, Any] | None,
) -> dict[str, Any]:
    state = prune_suppressions(state)
    item = {
        "message_id": msg.get("source_event_id"),
        "message_ts": msg.get("timestamp"),
        "user_id": msg.get("id"),
        "user_name": msg.get("name"),
        "text": msg.get("text"),
        "action": action,
        "pending_id": (pending or {}).get("id"),
        "created_at": now_iso(),
        "expires_at": time.time() + SUPPRESS_MAIN_SEC,
        "note": "handled_by_openclaw_irrigation_watcher_main_dialogue_must_stay_silent",
    }
    items = state.get("suppress_main_until") or []
    items.append(item)
    state["suppress_main_until"] = items[-20:]
    log_event({"type": "main_suppression_added", "suppression": item})
    return state


def configure_runtime(device_code: str, account_id: str | None, agent_id: str | None, group_id: str | None) -> None:
    global DEVICE_CODE, ACCOUNT_ID, AGENT_ID, GROUP_ID, SESSIONS_DIR, GATEWAY, PHASE3_LOG, STATE_PATH, EVENT_LOG, LOCK_PATH, DEFAULT_SECONDS
    profile = DEVICE_PROFILES[device_code]
    DEVICE_CODE = device_code
    ACCOUNT_ID = account_id or str(profile["account_id"])
    AGENT_ID = agent_id or str(profile["agent_id"])
    GROUP_ID = group_id or str(profile["group_id"])
    SESSIONS_DIR = Path(f"/root/.openclaw/agents/{AGENT_ID}/sessions")
    GATEWAY = ROOT / "tools" / f"openclaw_{DEVICE_CODE}_water.py"
    PHASE3_LOG = Path(f"/root/water/phase3/{DEVICE_CODE}/irrigation_system.log")
    suffix = "" if DEVICE_CODE == "soil3" else f"_{DEVICE_CODE}"
    STATE_PATH = ROOT / "state" / f"openclaw_irrigation_watcher{suffix}.json"
    EVENT_LOG = ROOT / "logs" / f"openclaw_irrigation_watcher{suffix}.jsonl"
    LOCK_PATH = Path(f"/tmp/openclaw_irrigation_watcher_{DEVICE_CODE}.lock")
    DEFAULT_SECONDS = float(profile["default_seconds"])


def get_qqbot_config() -> dict[str, str]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    q = cfg["channels"]["qqbot"]
    source = q if ACCOUNT_ID == "main" else (q.get("accounts") or {}).get(ACCOUNT_ID, {})
    app_id = source.get("appId")
    secret = source.get("clientSecret")
    if not app_id or not secret:
        raise RuntimeError(f"QQBot account is not configured: {ACCOUNT_ID}")
    return {"appId": app_id, "clientSecret": secret}


def get_access_token() -> str:
    cfg = get_qqbot_config()
    data = json.dumps({"appId": cfg["appId"], "clientSecret": cfg["clientSecret"]}).encode()
    req = urllib.request.Request(
        "https://bots.qq.com/app/getAppAccessToken",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"access token missing: {payload}")
    return token


def send_group(content: str) -> dict[str, Any]:
    token = get_access_token()
    req = urllib.request.Request(
        f"https://api.sgroup.qq.com/v2/groups/{GROUP_ID}/messages",
        data=json.dumps({"content": content, "msg_type": 0}, ensure_ascii=False).encode(),
        headers={
            "Authorization": "QQBot " + token,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    log_event({"type": "qq_sent", "content": content, "result": payload})
    return payload


def send_markdown_keyboard(markdown: str, buttons: list[dict[str, Any]], *, test: bool = False) -> dict[str, Any]:
    """Send a QQ group Markdown card; button data stays compatible with A/B/C parsing."""
    token = get_access_token()
    rows = [{"buttons": buttons[index:index + 3]} for index in range(0, len(buttons), 3)]
    body = {
        "msg_type": 2,
        "markdown": {"content": markdown},
        "keyboard": {"content": {"rows": rows}},
    }
    req = urllib.request.Request(
        f"https://api.sgroup.qq.com/v2/groups/{GROUP_ID}/messages",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={
            "Authorization": "QQBot " + token,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    log_event({"type": "qq_keyboard_sent", "test": test, "markdown": markdown, "buttons": buttons, "result": payload})
    return payload


def parse_user_content(content: str) -> dict[str, Any] | None:
    m = USER_RE.match(content.strip())
    if not m:
        return None
    text = m.group("text").replace("(@you)", "").strip()
    return {"name": m.group("name"), "id": m.group("id"), "text": text}


def recent_user_messages(after_ts_ms: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    paths = [
        Path(p)
        for p in glob.glob(str(SESSIONS_DIR / "*.jsonl"))
        if not p.endswith(".trajectory.jsonl")
    ]
    for path in paths:
        try:
            with path.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    msg = obj.get("message") or {}
                    if msg.get("role") != "user":
                        continue
                    ts = int(msg.get("timestamp") or 0)
                    if ts <= after_ts_ms:
                        continue
                    content = msg.get("content")
                    if not isinstance(content, str) or "(@you)" not in content:
                        continue
                    parsed = parse_user_content(content)
                    if not parsed:
                        continue
                    messages.append(
                        {
                            "source_event_id": obj.get("id"),
                            "timestamp": ts,
                            "session": str(path),
                            "content": content,
                            **parsed,
                        }
                    )
        except FileNotFoundError:
            continue
    messages.sort(key=lambda m: (m["timestamp"], m.get("id") or ""))
    return messages


def extract_seconds(text: str, default: float = DEFAULT_SECONDS) -> float:
    m = SECONDS_RE.search(text)
    if not m:
        return default
    return float(m.group(1))


def extract_amount_ml(text: str) -> float | None:
    m = ML_RE.search(text)
    if m:
        return float(m.group(1))
    if "半杯" in text:
        return 100.0
    if "一杯" in text or "1杯" in text:
        return 200.0
    if "一点" in text or "一小口" in text:
        return 30.0
    return None


def classify_reply(text: str) -> tuple[str, float]:
    normalized = re.sub(r"\s+", "", text.strip().upper())
    seconds = extract_seconds(text)
    if normalized in {"A", "A.", "A。"} or normalized.startswith("A"):
        return "approve", seconds
    if normalized in {"B", "B.", "B。"} or normalized.startswith("B"):
        return "defer", seconds
    if normalized in {"C", "C.", "C。"} or normalized.startswith("C"):
        return "manual", seconds
    if any(token in text for token in ("刚浇", "手动浇", "我浇了", "浇过了", "喂过水", "加过水")):
        return "manual", seconds
    if "确认浇水" in text or "确认给" in text:
        return "risk_confirm", seconds
    if "浇水" in text and "秒" in text:
        return "direct_water", seconds
    return "unknown", seconds


def run_gateway(
    seconds: float,
    user: dict[str, Any],
    *,
    risk_confirmed: bool,
    dry_run: bool = False,
    phase3_recommended_seconds: float | None = None,
) -> dict[str, Any]:
    cmd = [
        str(GATEWAY),
        "--seconds",
        f"{seconds:g}",
        "--operator-id",
        user.get("id") or "openclaw-mainbot",
        "--operator-name",
        user.get("name") or "植境智养",
        "--source",
        "openclaw_irrigation_watcher",
        "--reason",
        "openclaw_irrigation_watcher",
    ]
    if risk_confirmed:
        cmd.append("--risk-confirmed")
    if DEVICE_CODE == "soil2" and phase3_recommended_seconds is not None:
        cmd.extend(["--phase3-recommended-seconds", f"{phase3_recommended_seconds:g}"])
    if dry_run:
        cmd.append("--dry-run")
    env = os.environ.copy()
    env["OPENCLAW_IRRIGATION_WATCHER"] = "1"
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=40, env=env)
    stdout = result.stdout.strip()
    payload: dict[str, Any]
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except Exception:
        payload = {
            "ok": False,
            "status": "error",
            "reason": "gateway_parse_error",
            "detail": stdout or result.stderr.strip(),
        }
    payload["exit_code"] = result.returncode
    log_event({"type": "gateway_result", "cmd": cmd, "result": payload})
    return payload


def run_external_watering(user: dict[str, Any], raw_text: str) -> dict[str, Any]:
    cmd = [
        str(EXTERNAL_WATERING),
        "--operator-id",
        user.get("id") or "openclaw-mainbot",
        "--operator-name",
        user.get("name") or "植境智养",
        "--raw-text",
        raw_text,
        "--note",
        "用户通过 QQ 报告刚手动浇过水",
        "--device",
        DEVICE_CODE,
        "--source",
        f"openclaw_{ACCOUNT_ID}",
    ]
    amount_ml = extract_amount_ml(raw_text)
    if amount_ml is not None:
        cmd.extend(["--amount-ml", f"{amount_ml:g}"])
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=40)
    stdout = result.stdout.strip()
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except Exception:
        payload = {
            "ok": False,
            "storage": "unknown",
            "reason": "external_watering_parse_error",
            "detail": stdout or result.stderr.strip(),
        }
    payload["exit_code"] = result.returncode
    log_event({"type": "external_watering_result", "cmd": cmd, "result": payload})
    return payload


def external_watering_reply(payload: dict[str, Any]) -> str:
    if payload.get("ok"):
        amount = payload.get("amount_ml")
        if amount is not None:
            return f"收到，我记下了：你刚手动给我浇了约 {amount:g} ml。接下来我先慢慢吸收，别急着让我连喝。"
        return "收到，我记下了：你刚手动照顾过我。接下来我先慢慢吸收，别急着让我连喝。"
    return "我听懂了你刚手动浇过，但记录落库失败了；我已经写了本地审计，后面需要检查一下数据库连接。"


def gateway_reply_text(payload: dict[str, Any], seconds: float) -> str:
    status = payload.get("status")
    reason = payload.get("reason")
    ctx = payload.get("context") or {}
    humidity = ctx.get("humidity")
    target = ctx.get("target_low")
    if status == "needs_confirmation":
        return (
            "我先把水杯举起来但不喝：现在有一点风险。\n"
            f"{payload.get('detail', '继续浇水需要二次确认')}\n"
            f"当前湿度 {humidity}%，喝水线 {target}%。\n"
            f"如果你坚持小口测试，请再回复：确认浇水 {seconds:g} 秒"
        )
    if payload.get("ok") and status == "completed":
        return f"收到，我已经小口喝了 {payload.get('approved_seconds', seconds)} 秒。现在先别加餐，让水在土里散一散。"
    if payload.get("ok") and status == "dry_run_ok":
        return f"安全检查通过，自动养护这轮建议的小口剂量 {seconds:g} 秒可执行。"
    return f"这次我先不喝：{payload.get('detail') or reason or '安全检查没有通过'}"


def latest_phase3_recommendation() -> dict[str, Any]:
    info: dict[str, Any] = {
        "action": "unknown",
        "seconds": 0.0,
        "summary": "没有读到 Phase3 当前动作。",
        "reason": "unknown",
        "raw": {},
    }
    if not PHASE3_LOG.exists():
        return info
    try:
        lines = PHASE3_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-240:]
    except Exception:
        return info

    last_layer0 = ""
    last_recovery = ""
    last_arm = ""
    last_cycle = ""
    for line in lines:
        if "[Layer0] 土壤:" in line:
            last_layer0 = line
        if "[LowWetRecovery]" in line:
            last_recovery = line
        if "[StyleExperiment] arm=" in line:
            last_arm = line
        if "[Cycle #" in line:
            last_cycle = line

    info["raw"] = {
        "layer0": last_layer0,
        "low_wet_recovery": last_recovery,
        "arm": last_arm,
        "cycle": last_cycle,
    }

    water_sec = 0.0
    m = re.search(r"\bwater=([0-9.]+)s\b", last_arm)
    if m:
        water_sec = float(m.group(1))

    humidity = None
    h = re.search(r"H=([0-9.]+)%", last_layer0 or last_cycle)
    if h:
        humidity = float(h.group(1))

    target_low = None
    t = re.search(r"TARGET_LOW=([0-9.]+)%", "\n".join([last_recovery, last_cycle]))
    if t:
        target_low = float(t.group(1))

    reason = "unknown"
    r = re.search(r"reason=([^ ]+)", last_arm)
    if r:
        reason = r.group(1)

    if water_sec > 0:
        info.update(
            {
                "action": "water",
                "seconds": water_sec,
                "summary": f"Phase3 当前动作是浇水 {water_sec:g} 秒。",
                "humidity": humidity,
                "target_low": target_low,
                "reason": reason,
            }
        )
    else:
        info.update(
            {
                "action": "observe",
                "seconds": 0.0,
                "summary": "Phase3 当前动作是不浇水/继续观察。",
                "humidity": humidity,
                "target_low": target_low,
                "reason": reason,
            }
        )
    return info


def create_pending(source: str, phase3: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"confirm-{uuid.uuid4().hex[:10]}",
        "source": source,
        "device_code": DEVICE_CODE,
        "action": phase3.get("action", "unknown"),
        "seconds": float(phase3.get("seconds") or 0.0),
        "phase3": phase3,
        "created_at": time.time(),
        "expires_at": time.time() + CONFIRM_WINDOW_SEC,
        "mode": "awaiting_choice",
    }


def prompt_text(pending: dict[str, Any], *, source: str = "manual_test") -> str:
    phase3 = pending.get("phase3") or {}
    seconds = float(pending.get("seconds") or 0.0)
    humidity = phase3.get("humidity")
    target_low = phase3.get("target_low")
    if pending.get("action") == "water":
        intro = f"我有点想喝水，自动养护判断这次可以给我小口喝 {seconds:g} 秒。"
        option_a = f"A 好，给我小口喝 {seconds:g} 秒"
    else:
        intro = "我现在先不急着喝水，自动养护判断这轮继续观察更稳。"
        option_a = "A 好，先不浇，继续观察"
    fact = ""
    if humidity is not None:
        fact += f"根部附近湿度约 {humidity}%"
    if target_low is not None:
        fact += f"，参考喝水线 {target_low}%"
    if fact:
        fact += "。\n"
    return (
        "🌿 这盆植物想和你商量一下\n\n"
        f"{intro}\n"
        f"{fact}"
        f"{option_a}\n"
        "B 先别浇，再观察 2 小时\n"
        "C 我刚手动浇过了，别重复喂我\n\n"
        "如果 2 分钟内不回复，我就先不额外开泵，让自动养护继续观察。"
    )


def prompt_markdown(pending: dict[str, Any]) -> str:
    phase3 = pending.get("phase3") or {}
    seconds = float(pending.get("seconds") or 0.0)
    humidity = phase3.get("humidity")
    target_low = phase3.get("target_low")
    if pending.get("action") == "water":
        decision = f"自动养护建议这次小口喝 **{seconds:g} 秒**。"
    else:
        decision = "自动养护建议这轮先观察，不额外喝水。"
    facts: list[str] = []
    if humidity is not None:
        facts.append(f"根部附近水分：**{humidity}%**")
    if target_low is not None:
        facts.append(f"参考喝水线：**{target_low}%**")
    fact_text = "\n".join(f"- {item}" for item in facts)
    return "\n".join(
        [
            "## 🌿 我想和你商量一下",
            "",
            decision,
            fact_text,
            "",
            "请在 **2 分钟** 内选择；不回复时，我不会额外开泵，继续由自动养护观察。",
        ]
    )


def prompt_keyboard(pending: dict[str, Any]) -> list[dict[str, Any]]:
    seconds = float(pending.get("seconds") or 0.0)
    if pending.get("action") == "water":
        approve_label = f"浇水 {seconds:g} 秒"
    else:
        approve_label = "继续观察"
    choices = [
        ("A", approve_label, 1),
        ("B", "延后 2 小时", 0),
        ("C", "我刚手动浇过", 0),
    ]
    return [
        {
            "id": f"watering-{pending['id']}-{choice}",
            "render_data": {"label": label, "style": style},
            "action": {
                "type": 2,
                "permission": control_button_permission(),
                "data": choice,
                "enter": True,
            },
        }
        for choice, label, style in choices
    ]


def risk_confirmation_markdown(payload: dict[str, Any], seconds: float) -> str:
    """Render a second-confirmation card without exposing control internals."""
    ctx = payload.get("context") or {}
    facts: list[str] = []
    if ctx.get("humidity") is not None:
        facts.append(f"根部附近水分：**{ctx['humidity']}%**")
    if ctx.get("target_low") is not None:
        facts.append(f"参考喝水线：**{ctx['target_low']}%**")
    detail = str(payload.get("detail") or "刚浇过水或土壤偏湿，继续浇需要你再确认一次。")
    return "\n".join(
        [
            "## ⚠️ 再确认一下",
            "",
            "我已经把水杯拿起来了，但发现现在继续浇水有一点风险。",
            detail,
            *(f"- {item}" for item in facts),
            "",
            f"如果你仍决定浇水，只会按当前建议小口浇 **{seconds:g} 秒**。",
            "请在 **2 分钟** 内选择；不回复时，我不会额外开泵。",
        ]
    )


def risk_confirmation_keyboard(pending: dict[str, Any]) -> list[dict[str, Any]]:
    seconds = float(pending.get("seconds") or DEFAULT_SECONDS)
    choices = [
        (f"确认浇水 {seconds:g} 秒", f"确认浇水 {seconds:g} 秒", 1),
        ("B", "暂不浇水，继续观察", 0),
        ("C", "我刚手动浇过", 0),
    ]
    return [
        {
            "id": f"risk-watering-{pending['id']}-{index}",
            "render_data": {"label": label, "style": style},
            "action": {
                "type": 2,
                "permission": control_button_permission(),
                "data": data,
                "enter": True,
            },
        }
        for index, (data, label, style) in enumerate(choices, start=1)
    ]


def send_keyboard_preview() -> dict[str, Any]:
    """Visual-only card test. Its buttons only fill input and cannot affect watering."""
    preview = {
        "id": "preview",
        "action": "water",
        "seconds": 3.0,
        "phase3": {"humidity": 35.0, "target_low": 33.0},
    }
    buttons = prompt_keyboard(preview)
    for button in buttons:
        button["id"] = "preview-" + button["id"]
        button["action"]["data"] = "卡片预览，不执行"
        button["action"]["enter"] = False
    return send_markdown_keyboard(
        "## 🌿 浇水确认卡片预览\n\n这是一条显示测试，不会创建浇水请求，也不会开泵。\n\n正式确认时，三个按钮会分别回传 A、B、C。",
        buttons,
        test=True,
    )


def send_prompt(state: dict[str, Any], *, source: str, force: bool = False) -> dict[str, Any]:
    if state.get("pending") and not force:
        return state
    phase3 = latest_phase3_recommendation()
    pending = create_pending(source, phase3)
    state["pending"] = pending
    state["last_prompt_at"] = time.time()
    state["last_seen_ts_ms"] = now_ms()
    send_markdown_keyboard(prompt_markdown(pending), prompt_keyboard(pending))
    log_event({"type": "prompt_created", "pending": pending})
    record_plant_event(
        {
            "event_id": pending["id"],
            "event_type": "watering_confirmation_requested",
            "status": "open",
            "importance": "normal",
            "summary": (
                f"自动养护建议小口浇水 {pending['seconds']:g} 秒，正在等你的选择"
                if pending.get("action") == "water"
                else "自动养护建议继续观察，已经把选择告诉你"
            ),
            "follow_up_due_at": iso_after(CONFIRM_WINDOW_SEC),
            "evidence": {
                "recommended_action": pending.get("action"),
                "recommended_seconds": pending.get("seconds"),
                "trigger": source,
            },
        }
    )
    return state


def maybe_auto_prompt(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("pending"):
        return state
    if time.time() - float(state.get("last_prompt_at") or 0) < PROMPT_MIN_INTERVAL_SEC:
        return state
    if time.time() - float(state.get("last_auto_check_at") or 0) < AUTO_CHECK_MIN_INTERVAL_SEC:
        return state
    state["last_auto_check_at"] = time.time()
    phase3 = latest_phase3_recommendation()
    if phase3.get("action") != "water" or float(phase3.get("seconds") or 0.0) <= 0:
        log_event({"type": "auto_prompt_skipped", "reason": "phase3_not_watering", "phase3": phase3})
        return state
    candidate_seconds = float(phase3.get("seconds") or 0.0)
    dry_user = {"id": "openclaw-watcher", "name": "植境智养"}
    payload = run_gateway(
        candidate_seconds,
        dry_user,
        risk_confirmed=False,
        dry_run=True,
        phase3_recommended_seconds=candidate_seconds,
    )
    if payload.get("ok") and payload.get("status") == "dry_run_ok":
        return send_prompt(state, source="auto_candidate")
    log_event({"type": "auto_prompt_skipped", "reason": payload.get("reason"), "status": payload.get("status")})
    return state


def handle_reply(state: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
    action, seconds = classify_reply(msg["text"])
    pending = state.get("pending")
    control_actions = {"approve", "defer", "manual", "direct_water", "risk_confirm"}
    if action in control_actions and not is_control_user(msg):
        log_event({"type": "unauthorized_control_request", "message": msg, "action": action})
        send_group("这项养护操作仅限指定管理员使用；我会继续按当前自动养护策略观察。")
        return state
    if not pending and action not in {"direct_water", "risk_confirm", "manual"}:
        return state

    if pending:
        seconds = float(pending.get("seconds") or seconds or DEFAULT_SECONDS)
        if action in {"approve", "defer", "manual", "risk_confirm"}:
            state = add_main_suppression(state, msg, action, pending)
    elif action == "manual":
        state = add_main_suppression(state, msg, action, None)

    log_event({"type": "reply_seen", "message": msg, "action": action, "seconds": seconds, "pending": pending})

    if action == "defer":
        send_group("收到，先别浇。我先把杯子放下，继续观察 2 小时，自动养护会盯着我的状态。")
        close_prompt_event(pending, "user_deferred", "你选择先不浇，继续观察 2 小时")
        record_plant_event(
            {
                "event_type": "watering_deferred",
                "status": "recorded",
                "summary": "你选择先不浇，让我继续观察 2 小时",
                "actor_type": "user",
                "actor_name": msg.get("name"),
                "user_text": msg.get("text"),
            }
        )
        state["pending"] = None
    elif action == "manual":
        payload = run_external_watering(msg, msg.get("text") or "用户选择 C：刚手动浇过")
        send_group(external_watering_reply(payload))
        close_prompt_event(pending, "manual_watering_reported", "你告诉我刚刚已经手动浇过水")
        if payload.get("ok"):
            record_plant_event(
                {
                    "event_id": payload.get("event_id"),
                    "event_type": "external_watering_confirmed",
                    "status": "open",
                    "importance": "normal",
                    "summary": (
                        f"你报告刚手动浇了约 {payload['amount_ml']:g} ml"
                        if payload.get("amount_ml") is not None
                        else "你报告刚刚手动浇过水"
                    ),
                    "actor_type": "user",
                    "actor_name": msg.get("name"),
                    "user_text": msg.get("text"),
                    "follow_up_due_at": iso_after(30 * 60),
                    "evidence": {
                        "amount_ml": payload.get("amount_ml"),
                        "external_event_id": payload.get("event_id"),
                    },
                }
            )
        state["pending"] = None
    elif action in {"approve", "direct_water"}:
        if pending and pending.get("action") != "water":
            send_group("收到，这轮我先不额外喝水，继续观察。别急着给我续杯，我还在慢慢感受土里的水分。")
            state["pending"] = None
            return state
        payload = run_gateway(
            seconds,
            msg,
            risk_confirmed=False,
            phase3_recommended_seconds=(
                float((pending or {}).get("seconds") or 0.0) if pending else None
            ),
        )
        if payload.get("status") == "needs_confirmation":
            close_prompt_event(pending, "soft_risk_found", "安全检查发现风险，正在等待你的二次确认")
            event_id = payload.get("request_id") or (pending or {}).get("id")
            record_plant_event(
                {
                    "event_id": event_id,
                    "event_type": "watering_risk_confirmation_requested",
                    "status": "open",
                    "importance": "high",
                    "summary": "这次浇水存在偏湿或刚浇过的风险，正在等你二次确认",
                    "actor_type": "user",
                    "actor_name": msg.get("name"),
                    "user_text": msg.get("text"),
                    "follow_up_due_at": iso_after(CONFIRM_WINDOW_SEC),
                    "evidence": {
                        "requested_seconds": seconds,
                        "risk_reason": payload.get("reason"),
                        "humidity": (payload.get("context") or {}).get("humidity"),
                    },
                }
            )
            state["pending"] = {
                **(pending or create_pending("direct_water", {"action": "water", "seconds": seconds})),
                "id": event_id,
                "seconds": seconds,
                "mode": "awaiting_risk_confirm",
                "expires_at": time.time() + CONFIRM_WINDOW_SEC,
                "risk_reason": payload.get("reason"),
            }
            send_markdown_keyboard(
                risk_confirmation_markdown(payload, seconds),
                risk_confirmation_keyboard(state["pending"]),
            )
        else:
            send_group(gateway_reply_text(payload, seconds))
            close_prompt_event(pending, str(payload.get("status") or "finished"), "你的浇水选择已经完成安全检查")
            if payload.get("ok") and payload.get("status") == "completed":
                record_plant_event(
                    {
                        "event_id": payload.get("request_id"),
                        "event_type": "user_confirmed_watering",
                        "status": "open",
                        "importance": "normal",
                        "summary": f"你通过植境智养让我小口喝了 {payload.get('approved_seconds', seconds):g} 秒",
                        "actor_type": "user",
                        "actor_name": msg.get("name"),
                        "user_text": msg.get("text"),
                        "follow_up_due_at": iso_after(20 * 60),
                        "evidence": {
                            "water_sec": payload.get("approved_seconds", seconds),
                            "humidity_before": (payload.get("context") or {}).get("humidity"),
                            "request_id": payload.get("request_id"),
                        },
                    }
                )
            elif not payload.get("ok"):
                record_plant_event(
                    {
                        "event_type": "watering_request_not_executed",
                        "status": "recorded",
                        "importance": "normal",
                        "summary": "这次浇水请求没有执行，安全检查选择了继续观察",
                        "actor_type": "user",
                        "actor_name": msg.get("name"),
                        "user_text": msg.get("text"),
                        "evidence": {"reason": payload.get("reason"), "requested_seconds": seconds},
                    }
                )
            state["pending"] = None
    elif action == "risk_confirm":
        if not pending:
            send_group("我这边没有正在等待二次确认的浇水请求。你可以明确说“浇水 N 秒”，我会先做安全检查，再决定能不能执行。")
            return state
        if pending.get("action") != "water":
            send_group("这轮自动养护建议先别浇，所以“确认浇水”不能接着执行。若你要人工干预，请重新明确说“浇水 N 秒”。")
            state["pending"] = None
            return state
        payload = run_gateway(
            seconds,
            msg,
            risk_confirmed=True,
            phase3_recommended_seconds=float((pending or {}).get("seconds") or 0.0),
        )
        send_group(gateway_reply_text(payload, seconds))
        close_prompt_event(pending, str(payload.get("status") or "finished"), "你的二次确认已经处理")
        if payload.get("ok") and payload.get("status") == "completed":
            record_plant_event(
                {
                    "event_id": payload.get("request_id"),
                    "event_type": "user_confirmed_watering",
                    "status": "open",
                    "importance": "high",
                    "summary": f"你二次确认后，我小口喝了 {payload.get('approved_seconds', seconds):g} 秒",
                    "actor_type": "user",
                    "actor_name": msg.get("name"),
                    "user_text": msg.get("text"),
                    "follow_up_due_at": iso_after(20 * 60),
                    "evidence": {
                        "water_sec": payload.get("approved_seconds", seconds),
                        "humidity_before": (payload.get("context") or {}).get("humidity"),
                        "request_id": payload.get("request_id"),
                        "risk_confirmed": True,
                    },
                }
            )
        state["pending"] = None
    return state


def expire_pending_if_needed(state: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("pending")
    if not pending:
        return state
    if time.time() <= float(pending.get("expires_at") or 0):
        return state
    send_group("2 分钟没有等到回复。我先不额外浇水，让自动养护继续观察。")
    log_event({"type": "pending_expired", "pending": pending})
    record_plant_event(
        {
            "event_id": pending.get("id"),
            "event_type": (
                "watering_risk_confirmation_requested"
                if pending.get("mode") == "awaiting_risk_confirm"
                else "watering_confirmation_requested"
            ),
            "status": "expired",
            "importance": "normal",
            "summary": "两分钟没有收到回复，我没有额外开泵，自动养护继续观察",
            "resolution": "no_user_reply_default_autonomy",
            "resolved_at": now_iso(),
            "evidence": {
                "recommended_action": pending.get("action"),
                "recommended_seconds": pending.get("seconds"),
            },
        }
    )
    state["pending"] = None
    return state


def run_once(*, auto_prompt: bool, force_prompt: bool = False) -> None:
    with LOCK_PATH.open("w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        state = load_json(STATE_PATH, initial_state())
        state = prune_suppressions(state)
        state = expire_pending_if_needed(state)
        if force_prompt:
            state = send_prompt(state, source="manual_test", force=True)
        elif auto_prompt:
            state = maybe_auto_prompt(state)

        last_seen = int(state.get("last_seen_ts_ms") or 0)
        for msg in recent_user_messages(last_seen):
            state["last_seen_ts_ms"] = max(int(state.get("last_seen_ts_ms") or 0), int(msg["timestamp"]))
            state = handle_reply(state, msg)
        save_json(STATE_PATH, state)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw irrigation confirmation watcher")
    parser.add_argument("--device", choices=sorted(DEVICE_PROFILES), default="soil3")
    parser.add_argument("--account-id")
    parser.add_argument("--agent-id")
    parser.add_argument("--group-id")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--auto-prompt", action="store_true")
    parser.add_argument("--send-prompt", action="store_true")
    parser.add_argument("--send-keyboard-test", action="store_true")
    args = parser.parse_args()
    configure_runtime(args.device, args.account_id, args.agent_id, args.group_id)

    if args.send_keyboard_test:
        if DEVICE_CODE != "soil3":
            raise SystemExit("keyboard preview is currently enabled only for soil3")
        print(json.dumps(send_keyboard_preview(), ensure_ascii=False))
        return

    if args.loop:
        while True:
            try:
                run_once(auto_prompt=args.auto_prompt)
            except Exception as exc:
                log_event({"type": "watcher_error", "error": str(exc)})
            time.sleep(POLL_SEC)
    else:
        run_once(auto_prompt=args.auto_prompt, force_prompt=args.send_prompt)


if __name__ == "__main__":
    main()
