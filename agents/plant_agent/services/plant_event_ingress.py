#!/usr/bin/env python3
"""Trusted host-only ingress for QQ human-event confirmations.

This module intentionally has no command-line request interface.  It is run by
systemd and accepts one bounded JSON request over a root-only Unix socket.
The QQ adapter bridge is the sole caller; agent/tool processes run in isolated
agent sandboxes and cannot reach the socket or this host path.
"""

from __future__ import annotations

import json
import os
import socketserver
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services import human_record_gateway as watering
from services import manual_check_gateway as manual_check
from services.human_event_service import event_from_payload as watering_event_from_payload
from services.human_event_service import parse_manual_watering_command
from services.manual_check_service import event_from_payload as check_event_from_payload


SOCKET_PATH = Path(os.environ.get("PLANT_EVENT_INGRESS_SOCKET", "/run/user/0/plant-event-ingress/ingress.sock"))
MAX_REQUEST_BYTES = 8192
SCHEMA_VERSION = 1
FAILURE_TEXT = "人工事件服务暂不可用，未写入任何记录"
INVALID_TEXT = "人工事件请求无效"


def _response(ok: bool, text: str) -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "ok": ok, "text": text}


def _context(value: Any, factory: Callable[..., Any]) -> Any:
    if not isinstance(value, dict) or set(value) != {
        "channel", "agent_id", "conversation_id", "sender_id", "sender_name", "source_message_id",
    }:
        raise ValueError("context invalid")
    return factory(**value)


class PlantEventIngress:
    """Fixed request dispatcher; it never accepts arbitrary commands or SQL."""

    def __init__(
        self,
        watering_gateway: Optional[Any] = None,
        manual_check_gateway: Optional[Any] = None,
    ) -> None:
        self.watering_gateway = watering_gateway or watering.HumanRecordGateway(watering.GsqlPendingStore())
        self.manual_check_gateway = manual_check_gateway or manual_check.ManualCheckGateway(manual_check.GsqlManualCheckStore())

    def handle(self, request: Any) -> Dict[str, Any]:
        try:
            if not isinstance(request, dict) or request.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("request invalid")
            kind = request.get("kind")
            operation = request.get("operation")
            if kind not in {"watering", "manual_check"} or operation not in {"preview", "confirm", "cancel"}:
                raise ValueError("operation invalid")
            allowed = {"schema_version", "kind", "operation", "context"}
            if operation == "preview":
                allowed.add("text")
            if operation == "confirm":
                allowed.add("confirmation_message_id")
            if set(request) != allowed:
                raise ValueError("request shape invalid")
            return self._watering(operation, request) if kind == "watering" else self._manual_check(operation, request)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return _response(False, INVALID_TEXT)
        except Exception:
            return _response(False, FAILURE_TEXT)

    def _watering(self, operation: str, request: Dict[str, Any]) -> Dict[str, Any]:
        context = _context(request["context"], watering.RecordContext)
        if operation == "preview":
            text = request["text"]
            event = parse_manual_watering_command(text)
            self.watering_gateway.preview(context, text)
            duration = "未填写" if event.duration_sec is None else f"{event.duration_sec} 秒"
            volume = "未填写" if event.volume_ml is None else f"{event.volume_ml} ml"
            note = "无" if event.note is None else event.note
            return _response(True, "\n".join([
                "soil2 人工浇水记录预览", f"时间：{event.occurred_at}", f"时长：{duration}",
                f"水量：{volume}", f"备注：{note}", "请在 10 分钟内回复“确认记录”，或回复“取消记录”。",
            ]))
        if operation == "confirm":
            result = self.watering_gateway.confirm(context, request["confirmation_message_id"])
            if result is None:
                return _response(False, "没有待确认的人工浇水记录")
            return _response(True, f"soil2 人工浇水记录已保存 ✅\n事件编号：{result.human_event_id}")
        result = self.watering_gateway.cancel(context)
        return _response(result is not None, "人工浇水记录已取消" if result is not None else "没有待确认的人工浇水记录")

    def _manual_check(self, operation: str, request: Dict[str, Any]) -> Dict[str, Any]:
        context = _context(request["context"], manual_check.RecordContext)
        if operation == "preview":
            result = self.manual_check_gateway.preview(context, request["text"])
            record = self.manual_check_gateway.store._find("id = " + str(result.pending_id))
            if not record:
                raise RuntimeError("pending missing")
            event = check_event_from_payload(json.loads(record["event_payload_json"]))
            labels = {"no_issue": "未发现异常", "issue_found": "发现异常", "not_completed": "无法完成检查"}
            return _response(True, "\n".join([
                "soil2 人工检查记录预览", f"检查项：{event.check_code}", f"依据：{event.check_evidence}",
                f"结果：{labels[event.result]}", f"备注：{event.note or '未提供'}",
                "请在 10 分钟内回复“确认检查”，或回复“取消检查”。",
            ]))
        if operation == "confirm":
            result = self.manual_check_gateway.confirm(context, request["confirmation_message_id"])
            if result is None:
                return _response(False, "没有待确认的人工检查记录")
            return _response(True, f"soil2 人工检查记录已保存 ✅\n事件编号：{result.manual_check_event_id}")
        result = self.manual_check_gateway.cancel(context)
        return _response(result is not None, "人工检查记录已取消" if result is not None else "没有待确认的人工检查记录")


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        # Do not read an extra byte here: a valid stream client may keep its
        # write side open while waiting for the reply, which would otherwise
        # block this single-threaded service.  The newline delimiter and strict
        # object-shape validation already bound the accepted request.
        if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            payload = _response(False, INVALID_TEXT)
        else:
            try:
                payload = self.server.ingress.handle(json.loads(raw.decode("utf-8")))  # type: ignore[attr-defined]
            except Exception:
                payload = _response(False, FAILURE_TEXT)
        self.wfile.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")


class _UnixServer(socketserver.UnixStreamServer):
    allow_reuse_address = False


def serve(socket_path: Path = SOCKET_PATH) -> None:
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    with _UnixServer(str(socket_path), _RequestHandler) as server:
        os.chmod(socket_path, 0o600)
        server.ingress = PlantEventIngress()  # type: ignore[attr-defined]
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    serve()
