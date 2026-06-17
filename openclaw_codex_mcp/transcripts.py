from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable

from .config import clean_windows_path
from .models import TranscriptMessage, TranscriptSummary, TranscriptTurn


TRUNCATE_CHARS = 4000
TAIL_STATUS_BYTES = 512 * 1024


def iter_transcript_files(sessions_dir: Path, archived_sessions_dir: Path | None = None, include_archived: bool = True) -> Iterable[tuple[Path, bool]]:
    if sessions_dir.exists():
        for path in sessions_dir.rglob("*.jsonl"):
            if path.is_file():
                yield path, False
    if include_archived and archived_sessions_dir and archived_sessions_dir.exists():
        for path in archived_sessions_dir.rglob("*.jsonl"):
            if path.is_file():
                yield path, True


def read_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                obj = json.loads(line)
                if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                    payload = obj["payload"]
                    return {
                        "thread_id": payload.get("id"),
                        "created_at": payload.get("timestamp") or obj.get("timestamp"),
                        "cwd": clean_windows_path(payload.get("cwd")),
                        "originator": payload.get("originator"),
                        "source": payload.get("source"),
                        "thread_source": payload.get("thread_source"),
                        "cli_version": payload.get("cli_version"),
                        "model_provider": payload.get("model_provider"),
                    }
                break
    except (OSError, json.JSONDecodeError):
        return None
    return None


def infer_transcript_tail_status(path: Path, *, max_bytes: int = TAIL_STATUS_BYTES) -> tuple[str, str, list[str]]:
    """Infer recent status from the tail of a JSONL transcript.

    This is intentionally lightweight for active-chat listing. Full transcript
    parsing is reserved for codex_get_chat, where the user asked for content.
    """
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as fh:
            fh.seek(start)
            data = fh.read(max_bytes)
    except OSError:
        return "unknown", "low", ["failed to read transcript tail"]

    if start > 0:
        newline = data.find(b"\n")
        if newline >= 0:
            data = data[newline + 1 :]

    current_turn_id: str | None = None
    turns: dict[str, dict[str, Any]] = {}
    parse_errors = 0

    def ensure_turn(turn_id: str | None, timestamp: str | None = None) -> dict[str, Any] | None:
        if not turn_id:
            return None
        turn = turns.get(turn_id)
        if turn is None:
            turn = {"turn_id": turn_id, "status": "unknown", "started_at": timestamp, "last_seen_at": timestamp}
            turns[turn_id] = turn
        if timestamp:
            turn["last_seen_at"] = timestamp
            if not turn.get("started_at"):
                turn["started_at"] = timestamp
        return turn

    for raw_line in data.splitlines():
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            parse_errors += 1
            continue

        timestamp = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        payload_type = payload.get("type")

        if obj.get("type") == "turn_context":
            current_turn_id = str(payload.get("turn_id") or "")
            ensure_turn(current_turn_id, timestamp)
            continue

        if payload.get("turn_id") or payload.get("turnId"):
            current_turn_id = str(payload.get("turn_id") or payload.get("turnId"))
            ensure_turn(current_turn_id, timestamp)

        if payload_type == "task_started":
            turn = ensure_turn(str(payload.get("turn_id") or payload.get("turnId") or current_turn_id or ""), timestamp)
            if turn:
                turn["status"] = "running"
            continue

        if payload_type == "task_complete":
            turn = ensure_turn(str(payload.get("turn_id") or payload.get("turnId") or current_turn_id or ""), timestamp)
            if turn:
                turn["status"] = "completed"
                turn["completed_at"] = timestamp
            continue

        if payload_type in {"turn_aborted", "error"}:
            turn = ensure_turn(current_turn_id, timestamp)
            if turn:
                turn["status"] = "failed" if payload_type == "error" else "aborted"
            continue

        if payload_type in {"function_call", "custom_tool_call", "tool_search_call", "mcp_tool_call_end"}:
            turn = ensure_turn(current_turn_id, timestamp)
            if turn and turn.get("status") == "unknown":
                turn["status"] = "running"
            continue

        if payload_type in {"message", "user_message", "agent_message", "reasoning"}:
            ensure_turn(current_turn_id, timestamp)

    if not turns:
        evidence = ["transcript tail has no turns"]
        if parse_errors:
            evidence.append(f"tail parse errors: {parse_errors}")
        return "idle", "low", evidence

    latest_turn = sorted(turns.values(), key=lambda item: str(item.get("last_seen_at") or item.get("started_at") or ""), reverse=True)[0]
    turn_id = str(latest_turn.get("turn_id") or "")
    status = str(latest_turn.get("status") or "unknown")
    evidence = [f"tail latest turn {turn_id} status is {status}"]
    if parse_errors:
        evidence.append(f"tail parse errors: {parse_errors}")

    if status == "running":
        return "running", "medium", evidence
    if status in {"failed", "aborted"}:
        return "failed", "medium", evidence
    if status == "completed":
        return "idle", "medium", evidence
    return "unknown", "low", evidence


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _truncate(text: str | None, limit: int = TRUNCATE_CHARS) -> tuple[str | None, dict[str, Any]]:
    if text is None:
        return None, {}
    if len(text) <= limit:
        return text, {}
    return text[:limit] + "\n[truncated]", {"truncated": True, "original_chars": len(text), "returned_chars": limit}


def _message_id(thread_id: str | None, line_no: int, role: str) -> str:
    base = f"{thread_id or 'unknown'}:{line_no}:{role}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, base))


def _payload_text(payload: dict[str, Any]) -> str | None:
    for key in ("message", "text", "output", "result", "summary"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    if isinstance(payload.get("content"), (str, list)):
        return _content_text(payload.get("content"))
    return None


def _tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("name") or payload.get("tool") or payload.get("type") or "tool")


def parse_transcript(
    path: Path,
    *,
    archived: bool = False,
    include_tool_calls: bool = True,
    include_tool_outputs: bool = False,
    include_command_outputs: bool = False,
    include_reasoning: bool = False,
) -> TranscriptSummary:
    thread_id: str | None = None
    project_path: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    current_turn_id: str | None = None
    messages: list[TranscriptMessage] = []
    turns: dict[str, TranscriptTurn] = {}
    parse_errors = 0

    def ensure_turn(turn_id: str | None, timestamp: str | None = None) -> TranscriptTurn | None:
        if not turn_id:
            return None
        turn = turns.get(turn_id)
        if turn is None:
            turn = TranscriptTurn(
                turn_id=turn_id,
                thread_id=thread_id or "",
                started_at=timestamp,
                status="running" if timestamp else "unknown",
            )
            turns[turn_id] = turn
        if timestamp and turn.started_at is None:
            turn.started_at = timestamp
        return turn

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue

                timestamp = obj.get("timestamp")
                if isinstance(timestamp, str):
                    updated_at = timestamp
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                record_type = obj.get("type")
                payload_type = payload.get("type")

                if record_type == "session_meta":
                    thread_id = str(payload.get("id") or thread_id or "")
                    created_at = str(payload.get("timestamp") or timestamp or "") or created_at
                    project_path = clean_windows_path(payload.get("cwd")) or project_path
                    continue

                if record_type == "turn_context":
                    current_turn_id = str(payload.get("turn_id") or "")
                    turn = ensure_turn(current_turn_id, timestamp)
                    if turn:
                        turn.model = payload.get("model")
                        turn.approval_policy = payload.get("approval_policy")
                        sandbox = payload.get("sandbox_policy")
                        turn.sandbox_policy = sandbox if isinstance(sandbox, dict) else None
                        turn.source_line_start = turn.source_line_start or line_no
                    continue

                if payload.get("turn_id") or payload.get("turnId"):
                    current_turn_id = str(payload.get("turn_id") or payload.get("turnId"))
                    ensure_turn(current_turn_id, timestamp)

                if payload_type == "task_started":
                    turn = ensure_turn(str(payload.get("turn_id") or payload.get("turnId") or current_turn_id or ""), timestamp)
                    if turn:
                        turn.status = "running"
                    continue

                if payload_type == "task_complete":
                    turn = ensure_turn(str(payload.get("turn_id") or payload.get("turnId") or current_turn_id or ""), timestamp)
                    if turn:
                        turn.status = "completed"
                        turn.completed_at = timestamp if isinstance(timestamp, str) else None
                        turn.source_line_end = line_no
                    continue

                if payload_type in {"turn_aborted", "error"}:
                    turn = ensure_turn(current_turn_id, timestamp)
                    if turn:
                        turn.status = "failed" if payload_type == "error" else "aborted"

                if payload_type == "message":
                    role = str(payload.get("role") or "event")
                    if role not in {"user", "assistant", "system", "tool"}:
                        role = "event"
                    text = _content_text(payload.get("content"))
                    if not title and role == "user" and text.strip():
                        title = text.strip().splitlines()[0][:80]
                    messages.append(
                        TranscriptMessage(
                            message_id=_message_id(thread_id, line_no, role),
                            thread_id=thread_id or "",
                            turn_id=current_turn_id,
                            role=role,
                            created_at=timestamp if isinstance(timestamp, str) else None,
                            text=text,
                            items=[{"type": "text", "text": text, "metadata": {}}],
                            metadata={"payload_type": payload_type, "source_record_type": record_type},
                            source_line_start=line_no,
                            source_line_end=line_no,
                        )
                    )
                    continue

                if payload_type in {"user_message", "agent_message"}:
                    role = "user" if payload_type == "user_message" else "assistant"
                    text = _payload_text(payload) or ""
                    if messages and messages[-1].role == role and messages[-1].text == text:
                        continue
                    messages.append(
                        TranscriptMessage(
                            message_id=_message_id(thread_id, line_no, role),
                            thread_id=thread_id or "",
                            turn_id=current_turn_id,
                            role=role,
                            created_at=timestamp if isinstance(timestamp, str) else None,
                            text=text,
                            items=[{"type": "text", "text": text, "metadata": {}}],
                            metadata={"payload_type": payload_type, "source_record_type": record_type},
                            source_line_start=line_no,
                            source_line_end=line_no,
                        )
                    )
                    continue

                if payload_type == "reasoning" and include_reasoning:
                    text = _payload_text(payload) or ""
                    messages.append(
                        TranscriptMessage(
                            message_id=_message_id(thread_id, line_no, "event"),
                            thread_id=thread_id or "",
                            turn_id=current_turn_id,
                            role="event",
                            created_at=timestamp if isinstance(timestamp, str) else None,
                            text=text,
                            items=[{"type": "event", "text": text, "metadata": {"kind": "reasoning"}}],
                            metadata={"payload_type": payload_type},
                            source_line_start=line_no,
                            source_line_end=line_no,
                        )
                    )
                    continue

                if payload_type in {"function_call", "custom_tool_call", "tool_search_call", "mcp_tool_call_end"} and include_tool_calls:
                    name = _tool_name(payload)
                    text = str(payload.get("arguments") or payload.get("input") or name)
                    text, meta = _truncate(text)
                    messages.append(
                        TranscriptMessage(
                            message_id=_message_id(thread_id, line_no, "tool"),
                            thread_id=thread_id or "",
                            turn_id=current_turn_id,
                            role="tool",
                            created_at=timestamp if isinstance(timestamp, str) else None,
                            text=text,
                            items=[{"type": "tool_call", "text": text, "metadata": {"tool_name": name, **meta}}],
                            metadata={"payload_type": payload_type},
                            source_line_start=line_no,
                            source_line_end=line_no,
                        )
                    )
                    continue

                if payload_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
                    include_output = include_tool_outputs or (include_command_outputs and "command" in payload_type)
                    if not include_output:
                        continue
                    text = _payload_text(payload) or json.dumps(payload, ensure_ascii=False)
                    text, meta = _truncate(text)
                    messages.append(
                        TranscriptMessage(
                            message_id=_message_id(thread_id, line_no, "tool"),
                            thread_id=thread_id or "",
                            turn_id=current_turn_id,
                            role="tool",
                            created_at=timestamp if isinstance(timestamp, str) else None,
                            text=text,
                            items=[{"type": "tool_result", "text": text, "metadata": meta}],
                            metadata={"payload_type": payload_type},
                            source_line_start=line_no,
                            source_line_end=line_no,
                        )
                    )
                    continue

                if payload_type in {"context_compacted", "patch_apply_end"}:
                    messages.append(
                        TranscriptMessage(
                            message_id=_message_id(thread_id, line_no, "event"),
                            thread_id=thread_id or "",
                            turn_id=current_turn_id,
                            role="event",
                            created_at=timestamp if isinstance(timestamp, str) else None,
                            text=str(payload_type),
                            items=[{"type": "event", "text": str(payload_type), "metadata": {"payload_type": payload_type}}],
                            metadata={"payload_type": payload_type},
                            source_line_start=line_no,
                            source_line_end=line_no,
                        )
                    )
    except OSError:
        parse_errors += 1

    for turn in turns.values():
        if not turn.thread_id and thread_id:
            turn.thread_id = thread_id
    return TranscriptSummary(
        thread_id=thread_id or None,
        title=title,
        project_path=project_path,
        created_at=created_at,
        updated_at=updated_at,
        transcript_path=str(path),
        messages=messages,
        turns=turns,
        parse_errors=parse_errors,
        archived=archived,
    )


def latest_transcript_mtime(path: str | Path | None) -> float:
    if not path:
        return 0.0
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0
