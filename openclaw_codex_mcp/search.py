from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import ProjectChatCatalog
from .config import ServerConfig
from .hook_history import HOOK_HISTORY_PREFIX
from .logging_utils import get_logger
from .models import Chat, TranscriptMessage
from .storage import McpStorage
from .transcripts import parse_transcript


LOG = get_logger("search")
SEARCHABLE_ROLES = {"user", "assistant", "system"}
MAX_SEARCH_CHUNK_CHARS = 12_000
FTS_MATCH_SCAN_LIMIT = 5_000


@dataclass(slots=True)
class SearchIndexStatus:
    refreshed: bool = False
    indexed_files: int = 0
    skipped_unchanged_files: int = 0
    pending_files: int = 0
    time_budget_exhausted: bool = False

    def to_tool(self) -> dict[str, Any]:
        return {
            "refreshed": self.refreshed,
            "indexed_files": self.indexed_files,
            "skipped_unchanged_files": self.skipped_unchanged_files,
            "pending_files": self.pending_files,
            "time_budget_exhausted": self.time_budget_exhausted,
        }


@dataclass(slots=True)
class ParsedQuery:
    original: str
    normalized: str
    match_mode: str
    fts_query: str
    terms: list[str]
    phrase: str | None = None


@dataclass(slots=True)
class ChatSearchHit:
    chat: Chat
    score: float = 0.0
    match_count: int = 0
    matched_fields: set[str] = field(default_factory=set)
    snippets: list[dict[str, Any]] = field(default_factory=list)


class SearchIndex:
    def __init__(self, config: ServerConfig, storage: McpStorage, catalog: ProjectChatCatalog) -> None:
        self.config = config
        self.storage = storage
        self.catalog = catalog

    def refresh(self, *, include_archived: bool, time_budget_seconds: int) -> SearchIndexStatus:
        started = time.monotonic()
        status = SearchIndexStatus(refreshed=True)
        if time_budget_seconds > 3 or getattr(self.catalog, "_last_refresh_at", None) is None:
            self.catalog.refresh()
        chats = list(self.catalog.chats.values())
        for chat in chats:
            if chat.archived and not include_archived:
                continue
            self._index_chat_metadata(chat)

        deadline = time.monotonic() + max(1, time_budget_seconds)
        candidates = [chat for chat in chats if include_archived or not chat.archived]
        for chat in candidates:
            transcript = self.catalog.locate_transcript(chat)
            if not transcript:
                continue
            path = Path(transcript) if not transcript.startswith(HOOK_HISTORY_PREFIX) else None
            try:
                if transcript.startswith(HOOK_HISTORY_PREFIX):
                    thread_id = self.catalog.hook_history.thread_id_from_uri(transcript) or chat.thread_id
                    fingerprint = self.catalog.hook_history.fingerprint(thread_id)
                    size = fingerprint.total_size
                    mtime_ns = fingerprint.max_mtime_ns
                elif path is not None and path.is_dir():
                    fingerprint = self.catalog.kb_history.fingerprint(path)
                    size = fingerprint.total_size
                    mtime_ns = fingerprint.max_mtime_ns
                elif path is not None:
                    stat = path.stat()
                    size = stat.st_size
                    mtime_ns = stat.st_mtime_ns
                else:
                    continue
            except OSError:
                continue
            if self._transcript_checkpoint_is_current(transcript, chat.thread_id, size, mtime_ns):
                status.skipped_unchanged_files += 1
                continue
            if time.monotonic() >= deadline:
                status.time_budget_exhausted = True
                continue
            self._index_transcript(chat, transcript, size, mtime_ns)
            status.indexed_files += 1

        if status.time_budget_exhausted:
            for chat in candidates:
                transcript = self.catalog.locate_transcript(chat)
                if not transcript:
                    continue
                path = Path(transcript) if not transcript.startswith(HOOK_HISTORY_PREFIX) else None
                try:
                    if transcript.startswith(HOOK_HISTORY_PREFIX):
                        thread_id = self.catalog.hook_history.thread_id_from_uri(transcript) or chat.thread_id
                        fingerprint = self.catalog.hook_history.fingerprint(thread_id)
                        size = fingerprint.total_size
                        mtime_ns = fingerprint.max_mtime_ns
                    elif path is not None and path.is_dir():
                        fingerprint = self.catalog.kb_history.fingerprint(path)
                        size = fingerprint.total_size
                        mtime_ns = fingerprint.max_mtime_ns
                    elif path is not None:
                        stat = path.stat()
                        size = stat.st_size
                        mtime_ns = stat.st_mtime_ns
                    else:
                        continue
                except OSError:
                    continue
                if not self._transcript_checkpoint_is_current(transcript, chat.thread_id, size, mtime_ns):
                    status.pending_files += 1
        self.storage.commit()
        LOG.info(
            "search refresh done indexed=%d skipped=%d pending=%d exhausted=%s elapsed_ms=%d",
            status.indexed_files,
            status.skipped_unchanged_files,
            status.pending_files,
            status.time_budget_exhausted,
            int((time.monotonic() - started) * 1000),
        )
        return status

    def search(
        self,
        query: str,
        *,
        match_mode: str,
        project_id: str | None,
        include_archived: bool,
        limit: int,
        offset: int,
        include_snippets: bool,
        snippets_per_chat: int,
        snippet_max_chars: int,
    ) -> tuple[ParsedQuery, int, list[dict[str, Any]]]:
        parsed = build_fts_query(query, match_mode)
        chat_by_thread = {
            chat.thread_id: chat
            for chat in self.catalog.chats.values()
            if (include_archived or not chat.archived) and (project_id is None or chat.project_id == project_id)
        }
        if not chat_by_thread:
            return parsed, 0, []

        rows = self._matching_rows(parsed.fts_query)
        grouped: dict[str, ChatSearchHit] = {}
        for row in rows:
            thread_id = str(row["thread_id"])
            chat = chat_by_thread.get(thread_id)
            if chat is None:
                continue
            hit = grouped.get(thread_id)
            if hit is None:
                hit = ChatSearchHit(chat=chat)
                grouped[thread_id] = hit
            fields = _matched_fields(row, parsed)
            hit.matched_fields.update(fields)
            hit.match_count += 1
            hit.score += _row_score(row, parsed, fields)
            if include_snippets and len(hit.snippets) < snippets_per_chat and row["doc_type"] == "message":
                hit.snippets.append(_snippet_from_row(row, snippet_max_chars))

        hits = sorted(grouped.values(), key=lambda item: (item.score, item.chat.updated_at or ""), reverse=True)
        total = len(hits)
        page = hits[offset : offset + limit]
        return parsed, total, [_hit_to_tool(rank=offset + idx + 1, hit=hit) for idx, hit in enumerate(page)]

    def _matching_rows(self, fts_query: str) -> list[sqlite3.Row]:
        return self.storage.connection.execute(
            """
            SELECT docs.*, bm25(chat_search_fts, 3.0, 1.6, 1.0) AS fts_rank
            FROM chat_search_fts
            JOIN chat_search_docs docs ON docs.doc_id = chat_search_fts.rowid
            WHERE chat_search_fts MATCH ?
            LIMIT ?
            """,
            (fts_query, FTS_MATCH_SCAN_LIMIT),
        ).fetchall()

    def _index_chat_metadata(self, chat: Chat) -> None:
        self._delete_docs(chat.thread_id, doc_type="metadata")
        title = chat.title or chat.thread_id[:16]
        preview = chat.last_message_preview or ""
        if not title.strip() and not preview.strip():
            return
        self._insert_doc(
            chat=chat,
            title=title,
            preview=preview,
            text="",
            role=None,
            created_at=chat.updated_at,
            source_line_start=None,
            doc_type="metadata",
            transcript_path=chat.transcript_path,
        )

    def _index_transcript(self, chat: Chat, transcript: str, size: int, mtime_ns: int) -> None:
        self._delete_docs(chat.thread_id, transcript_path=transcript, doc_type="message")
        if transcript.startswith(HOOK_HISTORY_PREFIX):
            thread_id = self.catalog.hook_history.thread_id_from_uri(transcript) or chat.thread_id
            parsed = self.catalog.hook_history.parse_thread(thread_id)
        else:
            path = Path(transcript)
            if path.is_dir():
                parsed = self.catalog.kb_history.parse_thread_dir(
                    path,
                    include_tool_calls=False,
                    include_tool_outputs=False,
                    include_command_outputs=False,
                    include_reasoning=False,
                )
            else:
                parsed = parse_transcript(
                    path,
                    archived=chat.archived,
                    include_tool_calls=False,
                    include_tool_outputs=False,
                    include_command_outputs=False,
                    include_reasoning=False,
                )
        count = 0
        for message in parsed.messages:
            if not _is_searchable_message(message):
                continue
            for chunk in _message_chunks(message):
                self._insert_doc(
                    chat=chat,
                    title=chat.title or parsed.title or chat.thread_id[:16],
                    preview=chat.last_message_preview or "",
                    text=chunk,
                    role=message.role,
                    created_at=message.created_at,
                    source_line_start=message.source_line_start,
                    doc_type="message",
                    transcript_path=transcript,
                )
                count += 1
        self.storage.connection.execute(
            """
            INSERT INTO chat_search_transcripts(
              transcript_path, thread_id, size, mtime_ns, indexed_at, message_count, parse_error_count
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transcript_path) DO UPDATE SET
              thread_id=excluded.thread_id,
              size=excluded.size,
              mtime_ns=excluded.mtime_ns,
              indexed_at=excluded.indexed_at,
              message_count=excluded.message_count,
              parse_error_count=excluded.parse_error_count
            """,
            (transcript, chat.thread_id, size, mtime_ns, _now_iso(), count, parsed.parse_errors),
        )

    def _transcript_checkpoint_is_current(self, transcript: str, thread_id: str, size: int, mtime_ns: int) -> bool:
        row = self.storage.connection.execute(
            """
            SELECT size, mtime_ns, thread_id
            FROM chat_search_transcripts
            WHERE transcript_path = ?
            LIMIT 1
            """,
            (transcript,),
        ).fetchone()
        return row is not None and int(row["size"]) == size and int(row["mtime_ns"]) == mtime_ns and row["thread_id"] == thread_id

    def _delete_docs(self, thread_id: str, *, transcript_path: str | None = None, doc_type: str | None = None) -> None:
        conditions = ["thread_id = ?"]
        params: list[Any] = [thread_id]
        if transcript_path is not None:
            conditions.append("transcript_path = ?")
            params.append(transcript_path)
        if doc_type is not None:
            conditions.append("doc_type = ?")
            params.append(doc_type)
        where = " AND ".join(conditions)
        rowids = [
            int(row["doc_id"])
            for row in self.storage.connection.execute(f"SELECT doc_id FROM chat_search_docs WHERE {where}", params).fetchall()
        ]
        if rowids:
            self.storage.connection.executemany("DELETE FROM chat_search_fts WHERE rowid = ?", [(rowid,) for rowid in rowids])
        self.storage.connection.execute(f"DELETE FROM chat_search_docs WHERE {where}", params)

    def _insert_doc(
        self,
        *,
        chat: Chat,
        title: str,
        preview: str,
        text: str,
        role: str | None,
        created_at: str | None,
        source_line_start: int | None,
        doc_type: str,
        transcript_path: str | None,
    ) -> None:
        cursor = self.storage.connection.execute(
            """
            INSERT INTO chat_search_docs(
              thread_id, chat_id, project_id, project_path, title, preview, text, role,
              created_at, source_line_start, doc_type, archived, updated_at, transcript_path
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat.thread_id,
                chat.chat_id,
                chat.project_id,
                chat.project_path,
                title,
                preview,
                text,
                role,
                created_at,
                source_line_start,
                doc_type,
                1 if chat.archived else 0,
                chat.updated_at,
                transcript_path,
            ),
        )
        doc_id = int(cursor.lastrowid)
        self.storage.connection.execute(
            "INSERT INTO chat_search_fts(rowid, title, preview, text) VALUES(?, ?, ?, ?)",
            (doc_id, title, preview, text),
        )


def build_fts_query(query: str, match_mode: str) -> ParsedQuery:
    normalized = _normalize_query(query)
    if not normalized:
        raise ValueError("query is empty")
    mode = match_mode if match_mode in {"auto", "all_terms", "any_term", "phrase"} else "auto"
    phrase = _quoted_phrase(normalized)
    if mode == "auto":
        mode = "phrase" if phrase is not None else "all_terms"
    if phrase is None and mode == "phrase":
        phrase = normalized
    terms = _terms(phrase if mode == "phrase" and phrase else normalized)
    if not terms:
        fts_query = _fts_quote("__openclaw_no_terms__")
    elif mode == "phrase":
        fts_query = _fts_quote(phrase or normalized)
    elif mode == "any_term":
        fts_query = " OR ".join(_fts_quote(term) for term in terms)
    else:
        fts_query = " ".join(_fts_quote(term) for term in terms)
    return ParsedQuery(
        original=query,
        normalized=normalized,
        match_mode=mode,
        fts_query=fts_query,
        terms=terms,
        phrase=phrase or (normalized if len(terms) > 1 else None),
    )


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().split())


def _quoted_phrase(query: str) -> str | None:
    if len(query) >= 2 and query[0] == query[-1] and query[0] in {"'", '"'}:
        phrase = query[1:-1].strip()
        return phrase or None
    return None


def _terms(query: str) -> list[str]:
    return re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE)


def _fts_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _is_searchable_message(message: TranscriptMessage) -> bool:
    return message.role in SEARCHABLE_ROLES and bool((message.text or "").strip())


def _message_chunks(message: TranscriptMessage) -> list[str]:
    text = (message.text or "").strip()
    if len(text) <= MAX_SEARCH_CHUNK_CHARS:
        return [text]
    return [text[start : start + MAX_SEARCH_CHUNK_CHARS] for start in range(0, len(text), MAX_SEARCH_CHUNK_CHARS)]


def _matched_fields(row: sqlite3.Row, query: ParsedQuery) -> set[str]:
    fields: set[str] = set()
    for field_name, output_name in (("title", "title"), ("preview", "preview"), ("text", "message")):
        value = str(row[field_name] or "").casefold()
        if query.phrase and query.phrase.casefold() in value:
            fields.add(output_name)
            continue
        if query.terms and any(term in value for term in query.terms):
            fields.add(output_name)
    return fields or {"message"}


def _row_score(row: sqlite3.Row, query: ParsedQuery, fields: set[str]) -> float:
    score = max(0.0, -float(row["fts_rank"] or 0.0))
    if "title" in fields:
        score += 80.0
    if "preview" in fields:
        score += 35.0
    if "message" in fields:
        score += 10.0
    combined = " ".join(str(row[name] or "") for name in ("title", "preview", "text")).casefold()
    if query.phrase and query.phrase.casefold() in combined:
        score += 45.0
    score += _recency_boost(row["updated_at"])
    return round(score, 6)


def _recency_boost(value: Any) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    age_days = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 86_400)
    return max(0.0, 8.0 - math.log1p(age_days))


def _snippet_from_row(row: sqlite3.Row, max_chars: int) -> dict[str, Any]:
    text = _truncate(str(row["text"] or ""), max_chars)
    return {
        "role": row["role"],
        "created_at": row["created_at"],
        "source_line_start": row["source_line_start"],
        "text": text,
    }


def _hit_to_tool(*, rank: int, hit: ChatSearchHit) -> dict[str, Any]:
    chat = hit.chat
    return {
        "rank": rank,
        "score": round(hit.score, 6),
        "chat_id": chat.chat_id,
        "thread_id": chat.thread_id,
        "project_id": chat.project_id,
        "project_path": chat.project_path,
        "title": _truncate(chat.title or chat.thread_id[:16], 240),
        "updated_at": chat.updated_at,
        "archived": chat.archived,
        "status": chat.status,
        "last_message_preview": _truncate(chat.last_message_preview, 240) if chat.last_message_preview else None,
        "matched_fields": sorted(hit.matched_fields),
        "match_count": hit.match_count,
        "snippets": hit.snippets,
    }


def _truncate(value: str | None, max_chars: int) -> str | None:
    if value is None:
        return None
    if len(value) <= max_chars:
        return value
    return value[:max(0, max_chars - 3)].rstrip() + "..."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
