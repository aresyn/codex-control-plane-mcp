from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ChatStatus = str


@dataclass(slots=True)
class Project:
    project_id: str
    name: str
    path: str
    created_at: str | None = None
    last_activity_at: str | None = None
    source: str = "mixed"
    normalized_path_key: str = ""

    def to_tool(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "path": self.path,
            "last_activity_at": self.last_activity_at,
            "source": self.source
            if self.source in {"app_server", "sqlite", "transcript_index", "registry", "disk_scan", "hook_history", "kb_history", "mixed"}
            else "mixed",
        }


@dataclass(slots=True)
class Chat:
    chat_id: str
    thread_id: str
    project_id: str | None
    project_path: str | None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    transcript_path: str | None = None
    archived: bool = False
    last_message_preview: str | None = None
    status: ChatStatus = "unknown"
    status_confidence: str = "low"
    source: str = "mixed"

    def to_tool(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "title": self.title or self.thread_id[:16],
            "project_id": self.project_id,
            "project_path": self.project_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_message_preview": self.last_message_preview,
            "status": self.status,
            "source": self.source if self.source in {"app_server", "transcript_index", "hook_history", "kb_history", "mixed"} else "mixed",
        }


@dataclass(slots=True)
class TranscriptMessage:
    message_id: str | None
    thread_id: str
    turn_id: str | None
    role: str
    created_at: str | None
    text: str | None
    items: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_line_start: int | None = None
    source_line_end: int | None = None

    def to_tool(self, include_metadata: bool) -> dict[str, Any]:
        payload = {
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "role": self.role,
            "created_at": self.created_at,
            "text": self.text,
            "items": self.items,
            "metadata": self.metadata if include_metadata else {},
        }
        return payload


@dataclass(slots=True)
class TranscriptTurn:
    turn_id: str
    thread_id: str
    started_at: str | None = None
    completed_at: str | None = None
    status: str = "unknown"
    model: str | None = None
    approval_policy: str | None = None
    sandbox_policy: dict[str, Any] | None = None
    source_line_start: int | None = None
    source_line_end: int | None = None


@dataclass(slots=True)
class TranscriptSummary:
    thread_id: str | None
    title: str | None
    project_path: str | None
    created_at: str | None
    updated_at: str | None
    transcript_path: str
    messages: list[TranscriptMessage]
    turns: dict[str, TranscriptTurn]
    parse_errors: int = 0
    archived: bool = False
