from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openclaw_codex_mcp.config import path_key
from openclaw_codex_mcp.storage import McpStorage


DEFAULT_MAX_TEXT_CHARS = 20_000
CAPTURE_EVENTS = {"UserPromptSubmit", "Stop", "SessionStart", "PreCompact", "PostCompact"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record Codex hook events into the Codex Control Plane MCP SQLite state DB.")
    parser.add_argument("--config", type=Path, default=None, help="JSON config written by codex-control-plane-mcp-hooks install.")
    parser.add_argument("--state-db", type=Path, default=None, help="Override MCP state DB path.")
    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
        state_db = args.state_db or _state_db_from_config(config)
        if state_db is None:
            _emit_system_message("Codex Control Plane hook history skipped: state DB is not configured.")
            return 0
        max_text_chars = _int_value(config.get("maxTextChars"), DEFAULT_MAX_TEXT_CHARS)
        payload = _load_payload()
        result = record_payload(payload, state_db=state_db, max_text_chars=max_text_chars)
        if not result.get("recorded"):
            _emit_system_message(str(result.get("message") or "Codex Control Plane hook history skipped this event."))
    except Exception as exc:  # noqa: BLE001 - Codex hooks must never break the turn.
        _emit_system_message(f"Codex Control Plane hook history failed: {type(exc).__name__}")
    return 0


def record_payload(payload: dict[str, Any], *, state_db: Path, max_text_chars: int = DEFAULT_MAX_TEXT_CHARS) -> dict[str, Any]:
    event = _string(payload.get("hook_event_name")) or "unknown"
    if event not in CAPTURE_EVENTS:
        return {"recorded": False, "message": f"Codex Control Plane hook history ignored event {event}."}
    context = _context(payload)
    thread_id = context["thread_id"]
    if not thread_id:
        return {"recorded": False, "message": "Codex Control Plane hook history skipped event without thread id."}
    timestamp = _timestamp(payload)
    text_items = _text_items(payload, event=event, max_text_chars=max_text_chars)
    status = _turn_status(event)
    turn_id = context["turn_id"]
    storage = McpStorage(state_db)
    storage.connect()
    try:
        storage.upsert_hook_thread(
            {
                "thread_id": thread_id,
                "session_id": context["session_id"],
                "project_path": context["project_path"],
                "project_path_key": path_key(context["project_path"]) if context["project_path"] else None,
                "title": _title_from_payload(payload, text_items),
                "created_at": timestamp,
                "updated_at": timestamp,
                "transcript_path": context["transcript_path"],
                "source": "hook_history",
            }
        )
        if turn_id:
            last_assistant = next((item["text"] for item in reversed(text_items) if item["role"] == "assistant"), None)
            storage.upsert_hook_turn(
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "status": status,
                    "started_at": timestamp,
                    "updated_at": timestamp,
                    "completed_at": timestamp if status in {"completed", "failed"} else None,
                    "model": context["model"],
                    "permission_mode": context["permission_mode"],
                    "last_assistant_message": last_assistant,
                    "last_error": _string(payload.get("error")),
                }
            )
        inserted = 0
        for index, item in enumerate(text_items, start=1):
            message_id = _message_id(thread_id, turn_id, event, item["role"], item["text_kind"])
            event_hash = _event_hash(thread_id, turn_id, event, item["role"], item["text_kind"], item["text"])
            if storage.record_hook_message(
                {
                    "message_id": message_id,
                    "event_hash": event_hash,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "role": item["role"],
                    "text": item["text"],
                    "created_at": timestamp,
                    "sequence": index,
                    "hook_event_name": event,
                    "text_kind": item["text_kind"],
                }
            ):
                inserted += 1
        storage.commit()
        return {
            "recorded": True,
            "threadId": thread_id,
            "turnId": turn_id,
            "event": event,
            "insertedMessages": inserted,
        }
    finally:
        storage.close()


def _load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        configured = os.environ.get("CODEX_CONTROL_PLANE_MCP_HOOK_CONFIG") or os.environ.get("OPENCLAW_CODEX_MCP_HOOK_CONFIG")
        path = Path(configured) if configured else None
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _state_db_from_config(config: dict[str, Any]) -> Path | None:
    value = os.environ.get("CODEX_MCP_STATE_DB") or config.get("stateDb")
    if value:
        return Path(str(value))
    return None


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    if not raw.strip():
        return {"hook_event_name": "unknown"}
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {"hook_event_name": "unknown"}


def _context(payload: dict[str, Any]) -> dict[str, str]:
    session_id = _string(payload.get("session_id"))
    thread_id = _string(payload.get("thread_id")) or session_id
    return {
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": _string(payload.get("turn_id")),
        "project_path": _clean_path(_string(payload.get("cwd"))),
        "transcript_path": _clean_path(_string(payload.get("transcript_path"))),
        "model": _string(payload.get("model")),
        "permission_mode": _string(payload.get("permission_mode")),
    }


def _timestamp(payload: dict[str, Any]) -> str:
    for key in ("timestamp", "created_at", "createdAt"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.replace("Z", "+00:00")
    return datetime.now(timezone.utc).isoformat()


def _text_items(payload: dict[str, Any], *, event: str, max_text_chars: int) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if event == "UserPromptSubmit":
        text = _text_value(payload.get("prompt"))
        if text:
            items.append({"role": "user", "text_kind": "prompt", "text": _limit(_redact(text), max_text_chars)})
        return items
    if event == "Stop":
        text = _text_value(payload.get("last_assistant_message"))
        if text:
            items.append({"role": "assistant", "text_kind": "final_answer", "text": _limit(_redact(text), max_text_chars)})
        return items
    if event in {"PreCompact", "PostCompact"}:
        text = _first_text(payload, "summary", "message", "text")
        if text:
            items.append({"role": "system", "text_kind": "compact_summary", "text": _limit(_redact(text), max_text_chars)})
        return items
    text = _first_text(payload, "assistant_message", "agent_message", "message", "summary", "text")
    if text:
        items.append({"role": "assistant", "text_kind": "visible_agent_text", "text": _limit(_redact(text), max_text_chars)})
    return items


def _turn_status(event: str) -> str:
    if event == "Stop":
        return "completed"
    if event in {"UserPromptSubmit", "PreCompact", "PostCompact"}:
        return "running"
    return "unknown"


def _title_from_payload(payload: dict[str, Any], text_items: list[dict[str, str]]) -> str | None:
    explicit = _string(payload.get("title"))
    if explicit:
        return explicit[:160]
    prompt = next((item["text"] for item in text_items if item["role"] == "user" and item["text"].strip()), "")
    return prompt.strip().splitlines()[0][:160] if prompt.strip() else None


def _message_id(thread_id: str, turn_id: str, event: str, role: str, text_kind: str) -> str:
    base = f"{thread_id}:{turn_id}:{event}:{role}:{text_kind}"
    return "hook_" + hashlib.sha256(base.encode("utf-8", errors="replace")).hexdigest()


def _event_hash(thread_id: str, turn_id: str, event: str, role: str, text_kind: str, text: str) -> str:
    base = json.dumps(
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "event": event,
            "role": role,
            "text_kind": text_kind,
            "text": text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(base.encode("utf-8", errors="replace")).hexdigest()


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = _text_value(payload.get(key))
        if text:
            return text
    return ""


def _text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return ""


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_path(value: str) -> str:
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return value


def _limit(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n[truncated by codex-control-plane-mcp hook history]"
    return text[: max(0, max_chars - len(marker))].rstrip() + marker


def _redact(text: str) -> str:
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-[REDACTED]", text)
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", "Bearer [REDACTED]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(
        r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*['\"]?[^'\"\s]{8,}",
        r"\1=[REDACTED]",
        redacted,
    )
    return redacted


def _int_value(value: Any, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _emit_system_message(message: str) -> None:
    print(json.dumps({"systemMessage": message}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
