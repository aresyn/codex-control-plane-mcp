from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import ServerConfig, clean_windows_path, is_allowed_path
from .models import TranscriptMessage, TranscriptSummary, TranscriptTurn


@dataclass(slots=True)
class KbThreadRecord:
    thread_id: str
    project_key: str
    project_name: str
    project_path: str
    thread_dir: str
    title: str | None
    created_at: str | None
    updated_at: str | None
    status: str
    last_message_preview: str | None
    turn_count: int
    message_count: int


@dataclass(slots=True)
class KbThreadFingerprint:
    path: str
    file_count: int
    total_size: int
    max_mtime_ns: int
    mtime: str | None


class KbHistoryReader:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.root = config.kb_history_projects_root

    def list_thread_records(self) -> list[KbThreadRecord]:
        records: list[KbThreadRecord] = []
        for thread_dir in self._iter_thread_dirs():
            record = self._thread_record(thread_dir)
            if record is not None:
                records.append(record)
        return records

    def locate_thread_dir(self, thread_id: str) -> Path | None:
        if not self.root.exists():
            return None
        for path in self.root.glob(f"*/threads/{thread_id}"):
            if path.is_dir():
                return path
        return None

    def parse_thread_dir(
        self,
        thread_dir: Path,
        *,
        include_tool_calls: bool = False,
        include_tool_outputs: bool = False,
        include_command_outputs: bool = False,
        include_reasoning: bool = False,
    ) -> TranscriptSummary:
        messages: list[TranscriptMessage] = []
        turns: dict[str, TranscriptTurn] = {}
        parse_errors = 0
        thread_id = thread_dir.name
        project_path: str | None = None
        title: str | None = None
        created_at: str | None = None
        updated_at: str | None = None
        source_index = 0

        for turn_file in self._turn_files(thread_dir):
            payload = self._read_json(turn_file)
            if not isinstance(payload, dict):
                parse_errors += 1
                continue
            ids = _dict(payload.get("ids"))
            project = _dict(payload.get("project"))
            timestamps = _dict(payload.get("timestamps"))
            environment = _dict(payload.get("environment"))
            current_thread_id = str(ids.get("thread_id") or thread_id)
            thread_id = current_thread_id or thread_id
            turn_id = str(ids.get("turn_id") or turn_file.stem)
            project_path = clean_windows_path(project.get("cwd")) or project_path
            turn_created_at = _timestamp(timestamps, "created_at_utc", "created_at_local")
            turn_updated_at = _timestamp(timestamps, "updated_at_utc", "updated_at_local", "completed_at_utc", "completed_at_local")
            if turn_created_at and (created_at is None or turn_created_at < created_at):
                created_at = turn_created_at
            if turn_updated_at and (updated_at is None or turn_updated_at > updated_at):
                updated_at = turn_updated_at

            status = _turn_status(str(payload.get("status") or "unknown"))
            turn = TranscriptTurn(
                turn_id=turn_id,
                thread_id=thread_id,
                started_at=turn_created_at,
                completed_at=turn_updated_at if status == "completed" else None,
                status=status,
                model=str(environment.get("model") or "") or None,
                approval_policy=str(environment.get("permission_mode") or "") or None,
                sandbox_policy=None,
                source_line_start=source_index + 1,
            )
            turns[turn_id] = turn

            raw_messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
            for raw_message in sorted((item for item in raw_messages if isinstance(item, dict)), key=_message_sort_key):
                role = _message_role(raw_message.get("role"))
                if not _include_role(
                    role,
                    include_tool_calls=include_tool_calls,
                    include_tool_outputs=include_tool_outputs,
                    include_command_outputs=include_command_outputs,
                    include_reasoning=include_reasoning,
                ):
                    continue
                text = raw_message.get("text")
                if text is None and raw_message.get("text_missing"):
                    text = ""
                if not isinstance(text, str):
                    text = str(text or "")
                source_index += 1
                if title is None and role == "user" and text.strip():
                    title = text.strip().splitlines()[0][:80]
                created = _message_timestamp(raw_message) or turn_created_at
                messages.append(
                    TranscriptMessage(
                        message_id=str(raw_message.get("message_id") or _message_id(thread_id, turn_id, source_index, role)),
                        thread_id=thread_id,
                        turn_id=turn_id,
                        role=role,
                        created_at=created,
                        text=text,
                        items=[{"type": "text", "text": text, "metadata": {"source": "kb_history"}}],
                        metadata={
                            "source": "kb_history",
                            "turn_file": str(turn_file),
                            "hook_event_name": raw_message.get("hook_event_name"),
                            "sequence": raw_message.get("sequence"),
                            "char_count": raw_message.get("char_count"),
                        },
                        source_line_start=source_index,
                        source_line_end=source_index,
                    )
                )
            turn.source_line_end = source_index or turn.source_line_start

        messages.sort(key=lambda item: (item.created_at or "", item.source_line_start or 0))
        return TranscriptSummary(
            thread_id=thread_id or None,
            title=title,
            project_path=project_path,
            created_at=created_at,
            updated_at=updated_at,
            transcript_path=str(thread_dir),
            messages=messages,
            turns=turns,
            parse_errors=parse_errors,
            archived=False,
        )

    def fingerprint(self, thread_dir: Path) -> KbThreadFingerprint:
        total_size = 0
        max_mtime_ns = 0
        file_count = 0
        for path in self._turn_files(thread_dir):
            try:
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            total_size += stat.st_size
            max_mtime_ns = max(max_mtime_ns, stat.st_mtime_ns)
        mtime = datetime.fromtimestamp(max_mtime_ns / 1_000_000_000, timezone.utc).isoformat() if max_mtime_ns else None
        return KbThreadFingerprint(
            path=str(thread_dir),
            file_count=file_count,
            total_size=total_size,
            max_mtime_ns=max_mtime_ns,
            mtime=mtime,
        )

    def infer_thread_status(self, thread_dir: Path) -> tuple[str, str, list[str]]:
        latest: tuple[str, str, str] | None = None
        for turn_file in self._turn_files(thread_dir):
            payload = self._read_json(turn_file)
            if not isinstance(payload, dict):
                continue
            timestamps = _dict(payload.get("timestamps"))
            updated_at = _timestamp(timestamps, "updated_at_utc", "updated_at_local", "completed_at_utc", "completed_at_local") or ""
            status = str(payload.get("status") or "unknown")
            turn_id = str(_dict(payload.get("ids")).get("turn_id") or turn_file.stem)
            candidate = (updated_at, status, turn_id)
            if latest is None or candidate[0] > latest[0]:
                latest = candidate
        if latest is None:
            return "unknown", "low", ["kb history has no readable turn files"]
        status = _turn_status(latest[1])
        evidence = [f"kb latest turn {latest[2]} status is {latest[1]}"]
        if status == "running":
            return "running", "medium", evidence
        if status in {"failed", "aborted"}:
            return "failed", "medium", evidence
        if status == "completed":
            return "idle", "medium", evidence
        return "unknown", "low", evidence

    def _iter_thread_dirs(self) -> Iterable[Path]:
        if not self.root.exists():
            return
        for project_dir in sorted((item for item in self.root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
            threads_dir = project_dir / "threads"
            if not threads_dir.exists():
                continue
            for thread_dir in sorted((item for item in threads_dir.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
                yield thread_dir

    def _thread_record(self, thread_dir: Path) -> KbThreadRecord | None:
        first: dict[str, Any] | None = None
        latest: dict[str, Any] | None = None
        latest_key = ""
        title: str | None = None
        last_message_preview: str | None = None
        turn_count = 0
        message_count = 0

        for turn_file in self._turn_files(thread_dir):
            payload = self._read_json(turn_file)
            if not isinstance(payload, dict):
                continue
            turn_count += 1
            if first is None:
                first = payload
            raw_messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
            message_count += len([item for item in raw_messages if isinstance(item, dict)])
            for raw_message in sorted((item for item in raw_messages if isinstance(item, dict)), key=_message_sort_key):
                role = _message_role(raw_message.get("role"))
                text = raw_message.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                if title is None and role == "user":
                    title = text.strip().splitlines()[0][:80]
                if role in {"user", "assistant", "system"}:
                    last_message_preview = text.strip().splitlines()[0][:240]
            timestamps = _dict(payload.get("timestamps"))
            updated_at = _timestamp(timestamps, "updated_at_utc", "updated_at_local", "completed_at_utc", "completed_at_local") or ""
            if latest is None or updated_at >= latest_key:
                latest = payload
                latest_key = updated_at

        base = latest or first
        if base is None:
            return None
        ids = _dict(base.get("ids"))
        project = _dict(base.get("project"))
        cwd = clean_windows_path(project.get("cwd"))
        if not cwd or not Path(cwd).exists() or not is_allowed_path(cwd, self.config.allowed_roots):
            return None
        timestamps = _dict(base.get("timestamps"))
        created_at = None
        if first is not None:
            created_at = _timestamp(_dict(first.get("timestamps")), "created_at_utc", "created_at_local")
        updated_at = _timestamp(timestamps, "updated_at_utc", "updated_at_local", "completed_at_utc", "completed_at_local")
        return KbThreadRecord(
            thread_id=str(ids.get("thread_id") or thread_dir.name),
            project_key=str(project.get("key") or thread_dir.parents[1].name),
            project_name=str(project.get("name") or project.get("key") or Path(cwd).name),
            project_path=cwd,
            thread_dir=str(thread_dir),
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            status=_turn_status(str(base.get("status") or "unknown")),
            last_message_preview=last_message_preview,
            turn_count=turn_count,
            message_count=message_count,
        )

    def _turn_files(self, thread_dir: Path) -> list[Path]:
        if not thread_dir.exists():
            return []
        return sorted((item for item in thread_dir.glob("*.json") if item.is_file()), key=lambda item: item.name.casefold())

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8-sig") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _timestamp(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            if value.endswith("Z"):
                return value[:-1] + "+00:00"
            return value
    return None


def _message_timestamp(payload: dict[str, Any]) -> str | None:
    return _timestamp(payload, "captured_at_utc", "captured_at_local")


def _message_sort_key(payload: dict[str, Any]) -> tuple[str, int]:
    sequence = payload.get("sequence")
    try:
        parsed_sequence = int(sequence)
    except (TypeError, ValueError):
        parsed_sequence = 0
    return (_message_timestamp(payload) or "", parsed_sequence)


def _message_role(value: Any) -> str:
    role = str(value or "event").lower()
    if role in {"user", "assistant", "system", "tool"}:
        return role
    if role in {"reasoning", "event"}:
        return "event"
    return "event"


def _include_role(
    role: str,
    *,
    include_tool_calls: bool,
    include_tool_outputs: bool,
    include_command_outputs: bool,
    include_reasoning: bool,
) -> bool:
    if role in {"user", "assistant", "system"}:
        return True
    if role == "tool":
        return include_tool_calls or include_tool_outputs or include_command_outputs
    return include_reasoning


def _turn_status(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"completed", "complete", "done"}:
        return "completed"
    if normalized in {"in_progress", "running", "started"}:
        return "running"
    if normalized in {"failed", "error"}:
        return "failed"
    if normalized in {"aborted", "cancelled", "canceled"}:
        return "aborted"
    return normalized or "unknown"


def _message_id(thread_id: str, turn_id: str, source_index: int, role: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kb-history:{thread_id}:{turn_id}:{source_index}:{role}"))
