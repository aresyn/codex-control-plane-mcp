from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .catalog import project_id_for_path
from .models import Chat


TRACKED_TURN_HISTORY_PREFIX = "tracked_turn:"
HOOK_HISTORY_PREFIX = "hook_history:"


@dataclass(slots=True)
class ThreadResolution:
    chat: Chat
    source: str
    requested_thread_id: str
    canonical_thread_id: str
    alias_resolved: bool = False

    def to_tool(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "requestedThreadId": self.requested_thread_id,
            "canonicalThreadId": self.canonical_thread_id,
            "aliasResolved": self.alias_resolved,
            "projectId": self.chat.project_id,
            "projectPath": self.chat.project_path,
        }


class ThreadResolver:
    """Unified read/preflight resolver for Codex threads.

    Source priority is catalog, tracked turns, hook history, then legacy KB.
    This keeps fresh MCP-managed threads visible before slow legacy history.
    """

    def __init__(self, *, catalog: Any, storage: Any) -> None:
        self.catalog = catalog
        self.storage = storage

    def resolve(self, thread_id: str, project_id: str | None = None, *, refresh_catalog: bool = True) -> ThreadResolution | None:
        requested = str(thread_id or "").strip()
        if not requested:
            return None
        project = self.catalog.get_project(project_id) if project_id else None
        canonical_project_id = project.project_id if project is not None else project_id
        chat = self.catalog.get_chat(requested, canonical_project_id)
        if chat is not None:
            return ThreadResolution(chat=chat, source="catalog", requested_thread_id=requested, canonical_thread_id=chat.thread_id)
        if refresh_catalog:
            try:
                self.catalog.refresh()
            except Exception:
                pass
            chat = self.catalog.get_chat(requested, canonical_project_id)
            if chat is not None:
                return ThreadResolution(chat=chat, source="catalog_refresh", requested_thread_id=requested, canonical_thread_id=chat.thread_id)

        tracked_turn = self.storage.get_latest_tracked_turn_for_thread(requested)
        if tracked_turn is not None:
            project_path = _optional_string(tracked_turn.get("project_path"))
            return ThreadResolution(
                chat=Chat(
                    chat_id=requested,
                    thread_id=requested,
                    project_id=canonical_project_id or _optional_string(tracked_turn.get("project_id")) or (project_id_for_path(project_path) if project_path else None),
                    project_path=project_path,
                    title=requested[:16],
                    created_at=_optional_string(tracked_turn.get("started_at")),
                    updated_at=_optional_string(tracked_turn.get("updated_at")),
                    transcript_path=f"{TRACKED_TURN_HISTORY_PREFIX}{requested}",
                    status=str(tracked_turn.get("status") or "unknown"),
                    source="tracked_turn",
                ),
                source="tracked_turn",
                requested_thread_id=requested,
                canonical_thread_id=requested,
            )

        hook_uri = self.catalog.hook_history.locate_thread(requested)
        if hook_uri is not None:
            hook_thread = self.storage.get_hook_thread(requested) or {}
            project_path = _optional_string(hook_thread.get("project_path"))
            return ThreadResolution(
                chat=Chat(
                    chat_id=requested,
                    thread_id=requested,
                    project_id=canonical_project_id or (project_id_for_path(project_path) if project_path else None),
                    project_path=project_path,
                    title=_optional_string(hook_thread.get("title")),
                    created_at=_optional_string(hook_thread.get("created_at")),
                    updated_at=_optional_string(hook_thread.get("updated_at")),
                    transcript_path=hook_uri,
                    status="unknown",
                    source="hook_history",
                ),
                source="hook_history",
                requested_thread_id=requested,
                canonical_thread_id=requested,
            )

        thread_dir = self.catalog.kb_history.locate_thread_dir(requested)
        if thread_dir is not None:
            return ThreadResolution(
                chat=Chat(
                    chat_id=requested,
                    thread_id=requested,
                    project_id=canonical_project_id,
                    project_path=None,
                    title=requested[:16],
                    transcript_path=str(thread_dir),
                    status="unknown",
                    source="kb_history",
                ),
                source="kb_history",
                requested_thread_id=requested,
                canonical_thread_id=requested,
            )
        return None


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None
