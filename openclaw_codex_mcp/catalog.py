from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_state import CodexStateReader, CodexThreadRow
from .config import ServerConfig, canonical_existing_path, clean_windows_path, is_allowed_path, path_key
from .hook_history import HOOK_HISTORY_PREFIX, HookHistoryReader, HookThreadRecord
from .kb_history import KbHistoryReader, KbThreadRecord
from .logging_utils import get_logger
from .models import Chat, Project
from .storage import McpStorage
from .transcripts import infer_transcript_tail_status, iter_transcript_files, read_session_meta


PROJECT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "openclaw-codex-mcp:projects")
LOG = get_logger("catalog")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_id_for_path(path: str) -> str:
    return str(uuid.uuid5(PROJECT_NAMESPACE, path_key(path)))


class ProjectChatCatalog:
    def __init__(self, config: ServerConfig, storage: McpStorage) -> None:
        self.config = config
        self.storage = storage
        self.state_reader = CodexStateReader(config.codex_state_db)
        self.hook_history = HookHistoryReader(config, storage)
        self.kb_history = KbHistoryReader(config)
        self.projects: dict[str, Project] = {}
        self.project_aliases: dict[str, Project] = {}
        self.projects_by_path_key: dict[str, Project] = {}
        self.chats: dict[str, Chat] = {}
        self._state_rows_by_thread: dict[str, CodexThreadRow] = {}
        self._last_refresh_at: str | None = None

    def refresh(self) -> None:
        started = time.monotonic()
        projects: dict[str, Project] = {}
        chats: dict[str, Chat] = {}
        state_rows = self.state_reader.list_threads()
        self._state_rows_by_thread = {row.thread_id: row for row in state_rows}
        hook_records = self.hook_history.list_thread_records()
        kb_records = self.kb_history.list_thread_records()

        for project in self._projects_from_allowed_roots():
            self._add_project(projects, project)
        for project in self._projects_from_registry():
            self._add_project(projects, project)

        for row in state_rows:
            cwd = canonical_existing_path(row.cwd)
            if cwd and Path(cwd).exists() and is_allowed_path(cwd, self.config.allowed_roots):
                project = self._project_from_path(Path(cwd), "sqlite")
                self._add_project(projects, project)

        for path, archived in iter_transcript_files(self.config.sessions_dir, self.config.archived_sessions_dir, include_archived=True):
            meta = read_session_meta(path)
            if not meta:
                continue
            cwd = canonical_existing_path(meta.get("cwd"))
            if cwd and Path(cwd).exists() and is_allowed_path(cwd, self.config.allowed_roots):
                self._add_project(projects, self._project_from_path(Path(cwd), "transcript_index"))

        for record in hook_records:
            self._add_project(projects, self._project_from_hook_record(record))

        for record in kb_records:
            self._add_project(projects, self._project_from_kb_record(record))

        by_path = {project.normalized_path_key: project for project in projects.values()}
        for row in state_rows:
            cwd = canonical_existing_path(row.cwd)
            if not cwd or not Path(cwd).exists() or not is_allowed_path(cwd, self.config.allowed_roots):
                continue
            project = by_path.get(path_key(cwd))
            transcript_path = row.rollout_path or None
            chat = Chat(
                chat_id=row.thread_id,
                thread_id=row.thread_id,
                project_id=project.project_id if project else None,
                project_path=cwd,
                title=row.title or _preview_title(row.preview) or row.thread_id[:16],
                created_at=row.created_at,
                updated_at=row.updated_at,
                transcript_path=transcript_path,
                archived=row.archived,
                last_message_preview=row.preview or None,
                status="idle",
                status_confidence="medium",
                source="mixed",
            )
            chats[chat.chat_id] = chat

        for path, archived in iter_transcript_files(self.config.sessions_dir, self.config.archived_sessions_dir, include_archived=True):
            meta = read_session_meta(path)
            if not meta or not meta.get("thread_id"):
                continue
            thread_id = str(meta["thread_id"])
            cwd = canonical_existing_path(meta.get("cwd"))
            if cwd and (not Path(cwd).exists() or not is_allowed_path(cwd, self.config.allowed_roots)):
                continue
            project = by_path.get(path_key(cwd))
            existing = chats.get(thread_id)
            if existing:
                existing.transcript_path = existing.transcript_path or str(path)
                existing.archived = existing.archived or archived
                existing.source = "mixed"
                continue
            stat = path.stat()
            chat = Chat(
                chat_id=thread_id,
                thread_id=thread_id,
                project_id=project.project_id if project else None,
                project_path=cwd or None,
                title=thread_id[:16],
                created_at=meta.get("created_at"),
                updated_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                transcript_path=str(path),
                archived=archived,
                status="unknown",
                status_confidence="low",
                source="transcript_index",
            )
            chats[chat.chat_id] = chat

        for record in hook_records:
            project = by_path.get(path_key(record.project_path))
            existing = chats.get(record.thread_id)
            if existing:
                if not existing.transcript_path:
                    existing.transcript_path = record.transcript_uri
                existing.project_id = existing.project_id or (project.project_id if project else None)
                existing.project_path = existing.project_path or record.project_path
                existing.title = existing.title or record.title or record.thread_id[:16]
                existing.created_at = existing.created_at or record.created_at
                if record.updated_at and (not existing.updated_at or record.updated_at > existing.updated_at):
                    existing.updated_at = record.updated_at
                existing.last_message_preview = existing.last_message_preview or record.last_message_preview
                if existing.status in {"unknown", ""}:
                    existing.status = _chat_status_from_turn_status(record.status)
                    existing.status_confidence = "medium"
                existing.source = "mixed"
                continue
            chat = Chat(
                chat_id=record.thread_id,
                thread_id=record.thread_id,
                project_id=project.project_id if project else None,
                project_path=record.project_path,
                title=record.title or record.thread_id[:16],
                created_at=record.created_at,
                updated_at=record.updated_at,
                transcript_path=record.transcript_uri,
                archived=False,
                last_message_preview=record.last_message_preview,
                status=_chat_status_from_turn_status(record.status),
                status_confidence="medium",
                source="hook_history",
            )
            chats[chat.chat_id] = chat

        for record in kb_records:
            project = by_path.get(path_key(record.project_path))
            project_path = canonical_existing_path(record.project_path)
            existing = chats.get(record.thread_id)
            if existing:
                if not _chat_has_preferred_history(existing):
                    existing.transcript_path = record.thread_dir
                existing.project_id = existing.project_id or (project.project_id if project else None)
                existing.project_path = existing.project_path or project_path or record.project_path
                existing.title = existing.title or record.title or record.thread_id[:16]
                existing.created_at = existing.created_at or record.created_at
                incoming_newer = bool(record.updated_at and (not existing.updated_at or record.updated_at >= existing.updated_at))
                if record.updated_at and (not existing.updated_at or record.updated_at > existing.updated_at):
                    existing.updated_at = record.updated_at
                existing.last_message_preview = record.last_message_preview or existing.last_message_preview
                if incoming_newer or existing.status in {"unknown", ""}:
                    existing.status = _chat_status_from_turn_status(record.status)
                    existing.status_confidence = "medium"
                existing.source = "mixed"
                continue
            chat = Chat(
                chat_id=record.thread_id,
                thread_id=record.thread_id,
                project_id=project.project_id if project else None,
                project_path=project_path or record.project_path,
                title=record.title or record.thread_id[:16],
                created_at=record.created_at,
                updated_at=record.updated_at,
                transcript_path=record.thread_dir,
                archived=False,
                last_message_preview=record.last_message_preview,
                status=_chat_status_from_turn_status(record.status),
                status_confidence="medium",
                source="kb_history",
            )
            chats[chat.chat_id] = chat

        self.projects = projects
        self.projects_by_path_key = by_path
        self.chats = chats
        self._last_refresh_at = now_iso()
        self._persist_cache()
        LOG.info(
            "catalog refresh done projects=%d chats=%d state_rows=%d elapsed_ms=%d",
            len(projects),
            len(chats),
            len(state_rows),
            int((time.monotonic() - started) * 1000),
        )

    def list_projects(self) -> list[Project]:
        self._ensure_refreshed()
        return sorted(self.projects.values(), key=lambda item: item.name.casefold())

    def load_cached_projects(self) -> list[Project]:
        rows = self.storage.connection.execute(
            """
            SELECT project_id, name, path, normalized_path_key, created_at,
                   last_activity_at, source
              FROM projects
             ORDER BY lower(name)
            """
        ).fetchall()
        projects: list[Project] = []
        for row in rows:
            project_path = canonical_existing_path(row["path"])
            if not project_path or not is_allowed_path(project_path, self.config.allowed_roots):
                continue
            source = str(row["source"] or "")
            project_id = str(row["project_id"])
            if source != "registry":
                project_id = project_id_for_path(project_path)
            project = Project(
                project_id=project_id,
                name=str(row["name"]),
                path=project_path,
                normalized_path_key=path_key(project_path),
                created_at=row["created_at"],
                last_activity_at=row["last_activity_at"],
                source=source,
            )
            projects.append(project)
            cached_project_id = str(row["project_id"])
            if cached_project_id != project.project_id:
                self.project_aliases[cached_project_id] = project
        return projects

    def load_cached_chats(self) -> None:
        if self.chats:
            return
        allowed_project_ids = set(self.projects.keys()) if self.projects else None
        rows = self.storage.connection.execute(
            """
            SELECT chat_id, thread_id, project_id, project_path, title, transcript_path,
                   created_at, updated_at, archived, last_message_preview, status,
                   status_confidence, source
              FROM chats
             ORDER BY updated_at DESC
            """
        ).fetchall()
        chats: dict[str, Chat] = {}
        for row in rows:
            project_id = str(row["project_id"]) if row["project_id"] is not None else None
            if allowed_project_ids is not None and project_id not in allowed_project_ids:
                continue
            project_path = canonical_existing_path(row["project_path"])
            if project_path and not is_allowed_path(project_path, self.config.allowed_roots):
                continue
            chat = Chat(
                chat_id=str(row["chat_id"]),
                thread_id=str(row["thread_id"]),
                project_id=project_id,
                project_path=project_path or row["project_path"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                transcript_path=row["transcript_path"],
                archived=bool(row["archived"]),
                last_message_preview=row["last_message_preview"],
                status=row["status"],
                status_confidence=row["status_confidence"],
                source=row["source"],
            )
            chats[chat.chat_id] = chat
        self.chats = chats

    def get_project(self, project_id: str) -> Project | None:
        self._ensure_refreshed()
        return self._resolve_project_ref(project_id)

    def resolve_project_id(self, project_ref: str | None) -> str | None:
        project = self.get_project(project_ref or "") if project_ref else None
        return project.project_id if project is not None else None

    def list_project_chats(self, project_id: str, include_archived: bool = False) -> list[Chat]:
        self._ensure_refreshed()
        project = self._resolve_project_ref(project_id)
        canonical_project_id = project.project_id if project is not None else project_id
        rows = [
            chat
            for chat in self.chats.values()
            if chat.project_id == canonical_project_id and (include_archived or not chat.archived)
        ]
        return sorted(rows, key=lambda item: item.updated_at or "", reverse=True)

    def get_chat(self, chat_id: str, project_id: str | None = None) -> Chat | None:
        self._ensure_refreshed()
        canonical_project_id = None
        if project_id:
            project = self._resolve_project_ref(project_id)
            canonical_project_id = project.project_id if project is not None else project_id
        chat = self.chats.get(chat_id)
        if chat is None:
            for candidate in self.chats.values():
                if candidate.thread_id == chat_id:
                    chat = candidate
                    break
        if chat and canonical_project_id and chat.project_id != canonical_project_id:
            return None
        return chat

    def get_thread_row(self, thread_id: str) -> CodexThreadRow | None:
        self._ensure_refreshed()
        return self._state_rows_by_thread.get(thread_id)

    def locate_transcript(self, chat: Chat) -> str | None:
        if chat.transcript_path and Path(chat.transcript_path).exists():
            return chat.transcript_path
        if chat.transcript_path and (
            str(chat.transcript_path).startswith(HOOK_HISTORY_PREFIX)
            or str(chat.transcript_path).startswith("tracked_turn:")
        ):
            return chat.transcript_path
        hook_thread = self.hook_history.locate_thread(chat.thread_id)
        if hook_thread is not None:
            return hook_thread
        kb_thread_dir = self.kb_history.locate_thread_dir(chat.thread_id)
        if kb_thread_dir is not None and kb_thread_dir.exists():
            return str(kb_thread_dir)
        row = self._state_rows_by_thread.get(chat.thread_id)
        if row and row.rollout_path and Path(row.rollout_path).exists():
            return row.rollout_path
        self.refresh()
        refreshed = self.chats.get(chat.chat_id)
        if refreshed and refreshed.transcript_path and Path(refreshed.transcript_path).exists():
            return refreshed.transcript_path
        return None

    def infer_chat_status(self, chat: Chat, active_window_minutes: int = 120) -> tuple[str, str, list[str]]:
        transcript_path = self.locate_transcript(chat)
        if not transcript_path:
            return chat.status, "low", ["no transcript found"]
        if transcript_path.startswith(HOOK_HISTORY_PREFIX):
            thread_id = self.hook_history.thread_id_from_uri(transcript_path) or chat.thread_id
            return self.hook_history.infer_thread_status(thread_id)
        if transcript_path.startswith("tracked_turn:"):
            return chat.status, "medium", ["status inferred from tracked_turn journal"]
        path = Path(transcript_path)
        if path.is_dir():
            fingerprint = self.kb_history.fingerprint(path)
            if fingerprint.max_mtime_ns:
                age_seconds = time.time() - (fingerprint.max_mtime_ns / 1_000_000_000)
                if age_seconds > active_window_minutes * 60:
                    return "idle", "low", [f"kb history older than active window ({active_window_minutes} minutes)"]
            return self.kb_history.infer_thread_status(path)
        try:
            age_seconds = time.time() - path.stat().st_mtime
        except OSError:
            return "unknown", "low", ["transcript stat failed"]
        if age_seconds > active_window_minutes * 60:
            return "idle", "low", [f"transcript older than active window ({active_window_minutes} minutes)"]
        status, confidence, evidence = infer_transcript_tail_status(path)
        return status, confidence, evidence

    def _ensure_refreshed(self) -> None:
        if self._last_refresh_at is None:
            if not self._load_cached_index():
                self.refresh()

    def _load_cached_index(self) -> bool:
        projects = self.load_cached_projects()
        if not projects:
            return False
        self.projects = {project.project_id: project for project in projects}
        self.projects_by_path_key = {
            project.normalized_path_key: project
            for project in projects
            if project.normalized_path_key
        }
        self.load_cached_chats()
        self._last_refresh_at = now_iso()
        return True

    def _resolve_project_ref(self, project_ref: str | None, *, allow_refresh: bool = True) -> Project | None:
        ref = str(project_ref or "").strip()
        if not ref:
            return None
        direct = self.projects.get(ref) or self.project_aliases.get(ref)
        if direct is not None:
            return self._maybe_refresh_stale_project_alias(ref, direct, allow_refresh=allow_refresh)

        ref_key = path_key(ref)
        if ref_key:
            by_path = self.projects_by_path_key.get(ref_key)
            if by_path is not None:
                return self._maybe_refresh_stale_project_alias(ref, by_path, allow_refresh=allow_refresh)
            for project in self.projects.values():
                if project.normalized_path_key == ref_key or path_key(project.path) == ref_key:
                    return self._maybe_refresh_stale_project_alias(ref, project, allow_refresh=allow_refresh)

        exact_name = [project for project in self.projects.values() if project.name == ref]
        if len(exact_name) == 1:
            return self._maybe_refresh_stale_project_alias(ref, exact_name[0], allow_refresh=allow_refresh)
        if len(exact_name) > 1:
            return None

        folded = ref.casefold()
        casefold_name = [project for project in self.projects.values() if project.name.casefold() == folded]
        if len(casefold_name) == 1:
            return self._maybe_refresh_stale_project_alias(ref, casefold_name[0], allow_refresh=allow_refresh)
        if len(casefold_name) > 1:
            return None

        basename_matches = [
            project
            for project in self.projects.values()
            if Path(project.path).name == ref or Path(project.path).name.casefold() == folded
        ]
        if len(basename_matches) == 1:
            return self._maybe_refresh_stale_project_alias(ref, basename_matches[0], allow_refresh=allow_refresh)
        return None

    def _maybe_refresh_stale_project_alias(self, project_ref: str, project: Project, *, allow_refresh: bool) -> Project:
        if not allow_refresh or not self._project_alias_candidate_is_stale(project):
            return project
        project_path_key = path_key(project.path)
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001 - project aliases must remain best-effort.
            LOG.warning("project alias refresh failed project_ref=%s project_id=%s error=%s", project_ref, project.project_id, exc)
            return project
        refreshed = self._resolve_project_ref(project_ref, allow_refresh=False)
        if refreshed is None and project_path_key:
            refreshed = self.projects_by_path_key.get(project_path_key)
        if refreshed is not None:
            self.project_aliases[project.project_id] = refreshed
        return refreshed or project

    def _project_alias_candidate_is_stale(self, project: Project) -> bool:
        if project.source == "registry":
            return False
        cleaned = canonical_existing_path(project.path)
        if not cleaned or not Path(cleaned).exists() or not is_allowed_path(cleaned, self.config.allowed_roots):
            return False
        return project.project_id != project_id_for_path(cleaned)

    def _projects_from_registry(self) -> list[Project]:
        path = self.config.projects_registry_path
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        projects: list[Project] = []
        if not isinstance(payload, list):
            return projects
        for item in payload:
            if not isinstance(item, dict):
                continue
            root_path = canonical_existing_path(item.get("root_path"))
            if not root_path or not is_allowed_path(root_path, self.config.allowed_roots):
                continue
            projects.append(
                Project(
                    project_id=str(item.get("project_id") or project_id_for_path(root_path)),
                    name=str(item.get("name") or Path(root_path).name),
                    path=root_path,
                    created_at=item.get("created_at"),
                    source="registry",
                    normalized_path_key=path_key(root_path),
                )
            )
        return projects

    def _projects_from_allowed_roots(self) -> list[Project]:
        projects: list[Project] = []
        seen: set[str] = set()
        for root in self.config.allowed_roots:
            if not root.exists() or not root.is_dir():
                continue
            for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
                if not child.is_dir() or child.name.startswith(".") or child.name == "_kb_history":
                    continue
                if not is_allowed_path(child, self.config.allowed_roots):
                    continue
                key = path_key(child)
                if key in seen:
                    continue
                seen.add(key)
                self._maybe_append_project(projects, self._project_from_path(child, "disk_scan"))
        return projects

    def _project_from_path(self, path: Path, source: str) -> Project:
        cleaned = canonical_existing_path(path)
        return Project(
            project_id=project_id_for_path(cleaned),
            name=Path(cleaned).name,
            path=cleaned,
            source=source,
            normalized_path_key=path_key(cleaned),
        )

    def _project_from_kb_record(self, record: KbThreadRecord) -> Project:
        cleaned = canonical_existing_path(record.project_path)
        return Project(
            project_id=project_id_for_path(cleaned),
            name=record.project_name or Path(cleaned).name,
            path=cleaned,
            created_at=record.created_at,
            last_activity_at=record.updated_at,
            source="kb_history",
            normalized_path_key=path_key(cleaned),
        )

    def _project_from_hook_record(self, record: HookThreadRecord) -> Project:
        cleaned = canonical_existing_path(record.project_path)
        return Project(
            project_id=project_id_for_path(cleaned),
            name=record.project_name or Path(cleaned).name,
            path=cleaned,
            created_at=record.created_at,
            last_activity_at=record.updated_at,
            source="hook_history",
            normalized_path_key=path_key(cleaned),
        )

    def _maybe_append_project(self, projects: list[Project], project: Project) -> None:
        if not any(item.normalized_path_key == project.normalized_path_key for item in projects):
            projects.append(project)

    def _add_project(self, projects: dict[str, Project], project: Project) -> None:
        existing = None
        for item in projects.values():
            if item.normalized_path_key == project.normalized_path_key:
                existing = item
                break
        if existing:
            existing_priority = _project_source_priority(existing.source)
            incoming_priority = _project_source_priority(project.source)
            if incoming_priority > existing_priority:
                existing.name = project.name
                existing.path = project.path
                existing.normalized_path_key = project.normalized_path_key
                existing.created_at = existing.created_at or project.created_at
            else:
                canonical_path = canonical_existing_path(existing.path)
                if canonical_path and canonical_path != existing.path:
                    existing.path = canonical_path
                    existing.name = Path(canonical_path).name
                    existing.normalized_path_key = path_key(canonical_path)
            if project.last_activity_at and (
                not existing.last_activity_at or project.last_activity_at > existing.last_activity_at
            ):
                existing.last_activity_at = project.last_activity_at
            existing.source = "mixed" if existing.source != project.source else existing.source
            return
        projects[project.project_id] = project

    def _persist_cache(self) -> None:
        updated_at = now_iso()
        for project in self.projects.values():
            self.storage.upsert_project(
                {
                    "project_id": project.project_id,
                    "name": project.name,
                    "path": project.path,
                    "normalized_path_key": project.normalized_path_key,
                    "created_at": project.created_at,
                    "last_activity_at": project.last_activity_at,
                    "source": project.source,
                    "updated_at": updated_at,
                }
            )
        for chat in self.chats.values():
            self.storage.upsert_chat(
                {
                    "chat_id": chat.chat_id,
                    "thread_id": chat.thread_id,
                    "project_id": chat.project_id,
                    "project_path": chat.project_path,
                    "title": chat.title,
                    "transcript_path": chat.transcript_path,
                    "created_at": chat.created_at,
                    "updated_at": chat.updated_at,
                    "archived": 1 if chat.archived else 0,
                    "last_message_preview": chat.last_message_preview,
                    "status": chat.status,
                    "status_confidence": chat.status_confidence,
                    "source": chat.source,
                    "updated_at_local": updated_at,
                }
            )
        self.storage.commit()


def _preview_title(preview: str | None) -> str | None:
    if not preview:
        return None
    line = preview.strip().splitlines()[0].strip()
    return line[:80] if line else None


def _chat_status_from_turn_status(status: str) -> str:
    if status == "running":
        return "running"
    if status in {"failed", "aborted"}:
        return "failed"
    if status == "completed":
        return "idle"
    return status or "unknown"


def _project_source_priority(source: str) -> int:
    return {
        "registry": 10,
        "kb_history": 20,
        "hook_history": 25,
        "transcript_index": 30,
        "sqlite": 40,
        "disk_scan": 50,
        "app_server": 60,
        "mixed": 70,
    }.get(source, 0)


def _chat_has_preferred_history(chat: Chat) -> bool:
    transcript = str(chat.transcript_path or "")
    return transcript.startswith(HOOK_HISTORY_PREFIX) or transcript.startswith("tracked_turn:") or chat.source == "hook_history"
