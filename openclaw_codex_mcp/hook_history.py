from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import ServerConfig, canonical_existing_path, clean_windows_path, is_allowed_path, path_key
from .models import TranscriptMessage, TranscriptSummary, TranscriptTurn
from .storage import McpStorage


HOOK_HISTORY_PREFIX = "hook_history:"


@dataclass(slots=True)
class HookThreadRecord:
    thread_id: str
    project_name: str
    project_path: str
    transcript_uri: str
    title: str | None
    created_at: str | None
    updated_at: str | None
    status: str
    last_message_preview: str | None
    turn_count: int
    message_count: int


@dataclass(slots=True)
class HookThreadFingerprint:
    path: str
    message_count: int
    total_size: int
    max_mtime_ns: int
    mtime: str | None


class HookHistoryReader:
    def __init__(self, config: ServerConfig, storage: McpStorage) -> None:
        self.config = config
        self.storage = storage

    def thread_uri(self, thread_id: str) -> str:
        return f"{HOOK_HISTORY_PREFIX}{thread_id}"

    def thread_id_from_uri(self, uri: str) -> str | None:
        if uri.startswith(HOOK_HISTORY_PREFIX):
            return uri[len(HOOK_HISTORY_PREFIX) :]
        return None

    def list_thread_records(self) -> list[HookThreadRecord]:
        if not self.config.hook_history_enabled:
            return []
        records: list[HookThreadRecord] = []
        for row in self.storage.list_hook_threads(limit=10_000):
            record = self._thread_record(row)
            if record is not None:
                records.append(record)
        return records

    def locate_thread(self, thread_id: str) -> str | None:
        if not self.config.hook_history_enabled:
            return None
        row = self.storage.get_hook_thread(thread_id)
        return self.thread_uri(thread_id) if row is not None else None

    def locate_turn_thread(self, turn_id: str) -> str | None:
        if not self.config.hook_history_enabled:
            return None
        row = self.storage.find_hook_thread_by_turn(turn_id)
        if row is None:
            return None
        return self.thread_uri(str(row["thread_id"]))

    def parse_thread(self, thread_id: str) -> TranscriptSummary:
        thread = self.storage.get_hook_thread(thread_id)
        turns = {
            str(row["turn_id"]): TranscriptTurn(
                turn_id=str(row["turn_id"]),
                thread_id=str(row["thread_id"]),
                started_at=row.get("started_at"),
                completed_at=row.get("completed_at"),
                status=str(row.get("status") or "unknown"),
                model=row.get("model"),
                approval_policy=row.get("permission_mode"),
                sandbox_policy=None,
            )
            for row in self.storage.list_hook_turns(thread_id)
        }
        messages: list[TranscriptMessage] = []
        title = str((thread or {}).get("title") or "") or None
        for row in self.storage.list_hook_messages(thread_id=thread_id):
            role = str(row.get("role") or "event")
            text = str(row.get("text") or "")
            if title is None and role == "user" and text.strip():
                title = text.strip().splitlines()[0][:80]
            source_line = int(row.get("id") or 0) or None
            messages.append(
                TranscriptMessage(
                    message_id=str(row.get("message_id") or ""),
                    thread_id=str(row.get("thread_id") or thread_id),
                    turn_id=row.get("turn_id"),
                    role=role,
                    created_at=row.get("created_at"),
                    text=text,
                    items=[{"type": "text", "text": text, "metadata": {"source": "hook_history", "textKind": row.get("text_kind")}}],
                    metadata={
                        "source": "hook_history",
                        "hook_event_name": row.get("hook_event_name"),
                        "text_kind": row.get("text_kind"),
                        "sequence": row.get("sequence"),
                    },
                    source_line_start=source_line,
                    source_line_end=source_line,
                )
            )
        for turn in turns.values():
            source_lines = [msg.source_line_start for msg in messages if msg.turn_id == turn.turn_id and msg.source_line_start]
            if source_lines:
                turn.source_line_start = min(source_lines)
                turn.source_line_end = max(source_lines)
        return TranscriptSummary(
            thread_id=thread_id,
            title=title,
            project_path=clean_windows_path((thread or {}).get("project_path")),
            created_at=(thread or {}).get("created_at"),
            updated_at=(thread or {}).get("updated_at"),
            transcript_path=self.thread_uri(thread_id),
            messages=messages,
            turns=turns,
            parse_errors=0,
            archived=False,
        )

    def fingerprint(self, thread_id: str) -> HookThreadFingerprint:
        messages = self.storage.list_hook_messages(thread_id=thread_id)
        total_size = sum(len(str(row.get("text") or "")) for row in messages)
        latest = max((str(row.get("created_at") or "") for row in messages), default="")
        max_mtime_ns = _iso_to_ns(latest)
        return HookThreadFingerprint(
            path=self.thread_uri(thread_id),
            message_count=len(messages),
            total_size=total_size,
            max_mtime_ns=max_mtime_ns,
            mtime=latest or None,
        )

    def infer_thread_status(self, thread_id: str) -> tuple[str, str, list[str]]:
        turns = self.storage.list_hook_turns(thread_id)
        if not turns:
            return "unknown", "low", ["hook history has no turn rows"]
        latest = sorted(turns, key=lambda row: str(row.get("updated_at") or row.get("completed_at") or row.get("started_at") or ""))[-1]
        status = _chat_status_from_turn_status(str(latest.get("status") or "unknown"))
        evidence = [f"hook latest turn {latest.get('turn_id')} status is {latest.get('status')}"]
        confidence = "medium" if status != "unknown" else "low"
        return status, confidence, evidence

    def _thread_record(self, row: dict[str, Any]) -> HookThreadRecord | None:
        project_path = canonical_existing_path(row.get("project_path"))
        if not project_path or not is_allowed_path(project_path, self.config.allowed_roots):
            return None
        thread_id = str(row.get("thread_id") or "")
        if not thread_id:
            return None
        turns = self.storage.list_hook_turns(thread_id)
        messages = self.storage.list_hook_messages(thread_id=thread_id)
        latest_turn = sorted(turns, key=lambda item: str(item.get("updated_at") or item.get("completed_at") or item.get("started_at") or ""))[-1] if turns else {}
        last_message = next((message for message in reversed(messages) if str(message.get("role") or "") in {"user", "assistant", "system"} and str(message.get("text") or "").strip()), None)
        title = row.get("title") or _first_user_title(messages) or thread_id[:16]
        return HookThreadRecord(
            thread_id=thread_id,
            project_name=project_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
            project_path=project_path,
            transcript_uri=self.thread_uri(thread_id),
            title=str(title) if title else None,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at") or (latest_turn or {}).get("updated_at"),
            status=str((latest_turn or {}).get("status") or "unknown"),
            last_message_preview=_preview(str((last_message or {}).get("text") or "")),
            turn_count=len(turns),
            message_count=len(messages),
        )


def _first_user_title(messages: list[dict[str, Any]]) -> str | None:
    for message in messages:
        if message.get("role") == "user":
            text = str(message.get("text") or "").strip()
            if text:
                return text.splitlines()[0][:80]
    return None


def _preview(text: str) -> str | None:
    stripped = text.strip()
    return stripped.splitlines()[0][:240] if stripped else None


def _chat_status_from_turn_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "running":
        return "running"
    if normalized in {"failed", "aborted", "cancelled", "canceled", "interrupted"}:
        return "failed"
    if normalized == "completed":
        return "idle"
    return normalized or "unknown"


def _iso_to_ns(value: str) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000_000_000)

