from __future__ import annotations

import json
from typing import Any

from .config import path_key


def operation_consumes_turn_slot(operation: dict[str, Any], request: dict[str, Any]) -> bool:
    operation_type = str(operation.get("operation_type") or operation.get("operationType") or "")
    if operation_type == "steer_turn":
        return False
    if operation_type == "fork_thread" and not _optional_string(request.get("message")):
        return False
    return True


def operation_is_write_turn(operation: dict[str, Any], request: dict[str, Any]) -> bool:
    if not operation_consumes_turn_slot(operation, request):
        return False
    sandbox = str(request.get("sandbox") or "").strip()
    return sandbox in {"workspace-write", "danger-full-access"}


def thread_active_lock(
    *,
    operation_id: str,
    thread_id: str,
    project_id: str | None,
    worker_id: str | None,
    created_at: str,
    expires_at: str,
) -> dict[str, Any]:
    return {
        "lock_key": f"thread:{thread_id}:active-turn",
        "operation_id": operation_id,
        "thread_id": thread_id,
        "project_id": project_id,
        "lock_mode": "exclusive",
        "worker_id": worker_id or "unknown",
        "created_at": created_at,
        "expires_at": expires_at,
    }


def operation_request_from_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def project_key(operation: dict[str, Any]) -> str:
    if operation.get("project_id"):
        return str(operation["project_id"])
    if operation.get("cwd"):
        return path_key(operation.get("cwd"))
    return "unknown"


def operation_thread_id(operation: dict[str, Any], request: dict[str, Any]) -> str | None:
    return (
        _optional_string(operation.get("thread_id"))
        or _optional_string(request.get("_resolved_thread_id"))
        or _optional_string(request.get("thread_id"))
        or _optional_string(request.get("chat_id"))
    )


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None
