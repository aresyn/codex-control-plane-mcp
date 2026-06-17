from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .statuses import OPERATION_ACTIVE_STATUSES, OPERATION_STARTABLE_STATUSES, PROMPT_SUBMISSION_CLEANUP_STATUSES


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  normalized_path_key TEXT NOT NULL UNIQUE,
  created_at TEXT,
  last_activity_at TEXT,
  source TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats(
  chat_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL UNIQUE,
  project_id TEXT,
  project_path TEXT,
  title TEXT,
  transcript_path TEXT,
  created_at TEXT,
  updated_at TEXT,
  archived INTEGER NOT NULL DEFAULT 0,
  last_message_preview TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  status_confidence TEXT NOT NULL DEFAULT 'low',
  source TEXT NOT NULL,
  updated_at_local TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_files(
  path TEXT PRIMARY KEY,
  thread_id TEXT,
  project_path TEXT,
  archived INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL,
  mtime TEXT NOT NULL,
  indexed_offset INTEGER NOT NULL DEFAULT 0,
  indexed_line INTEGER NOT NULL DEFAULT 0,
  parse_error_count INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL,
  last_indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS turns(
  turn_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  status TEXT NOT NULL,
  model TEXT,
  approval_policy TEXT,
  sandbox_policy_json TEXT,
  source_line_start INTEGER,
  source_line_end INTEGER
);

CREATE TABLE IF NOT EXISTS messages(
  message_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  turn_id TEXT,
  role TEXT NOT NULL,
  created_at TEXT,
  text_preview TEXT,
  full_text_ref TEXT,
  source_line_start INTEGER,
  source_line_end INTEGER,
  metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS active_state(
  thread_id TEXT PRIMARY KEY,
  turn_id TEXT,
  status TEXT NOT NULL,
  pending_approval_json TEXT,
  pending_question TEXT,
  confidence TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  last_activity_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_server_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  direction TEXT NOT NULL,
  jsonrpc_id TEXT,
  method TEXT,
  thread_id TEXT,
  turn_id TEXT,
  process_generation INTEGER,
  payload_json TEXT NOT NULL,
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracked_turns(
  turn_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  chat_id TEXT,
  project_id TEXT,
  project_path TEXT,
  status TEXT NOT NULL,
  started_at TEXT,
  updated_at TEXT,
  completed_at TEXT,
  first_message_at TEXT,
  final_message TEXT,
  last_error TEXT,
  accepted_at TEXT,
  request_id TEXT,
  process_generation INTEGER,
  last_event_seq INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'app_server'
);

CREATE INDEX IF NOT EXISTS idx_tracked_turns_thread
ON tracked_turns(thread_id);

CREATE INDEX IF NOT EXISTS idx_tracked_turns_status
ON tracked_turns(status);

CREATE TABLE IF NOT EXISTS tracked_turn_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_hash TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT,
  sequence INTEGER NOT NULL DEFAULT 0,
  event_type TEXT,
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracked_turn_messages_turn
ON tracked_turn_messages(turn_id, id);

CREATE INDEX IF NOT EXISTS idx_tracked_turn_messages_thread
ON tracked_turn_messages(thread_id, id);

CREATE TABLE IF NOT EXISTS tracked_plan_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'in_progress',
  text TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  sequence INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT,
  UNIQUE(turn_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_tracked_plan_items_turn
ON tracked_plan_items(turn_id, id);

CREATE INDEX IF NOT EXISTS idx_tracked_plan_items_thread
ON tracked_plan_items(thread_id, id);

CREATE TABLE IF NOT EXISTS tracked_plan_events(
  event_hash TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT
);

CREATE TABLE IF NOT EXISTS codex_workflows(
  workflow_id TEXT PRIMARY KEY,
  workflow_kind TEXT NOT NULL DEFAULT 'plan_then_execute',
  client_request_id TEXT UNIQUE,
  execution_client_request_id TEXT,
  current_operation_id TEXT,
  plan_operation_id TEXT,
  execution_operation_id TEXT,
  project_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  plan_turn_id TEXT NOT NULL,
  execution_turn_id TEXT,
  latest_plan_item_id TEXT,
  latest_plan_hash TEXT,
  latest_report_hash TEXT,
  final_report_json TEXT,
  phase TEXT NOT NULL,
  status TEXT NOT NULL,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  app_server_generation INTEGER,
  metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_codex_workflows_thread
ON codex_workflows(thread_id);

CREATE INDEX IF NOT EXISTS idx_codex_workflows_phase
ON codex_workflows(phase, status);

CREATE TABLE IF NOT EXISTS codex_workflow_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codex_workflow_events_workflow
ON codex_workflow_events(workflow_id, id);

CREATE TABLE IF NOT EXISTS codex_operations(
  operation_id TEXT PRIMARY KEY,
  client_request_id TEXT UNIQUE,
  operation_type TEXT NOT NULL,
  status TEXT NOT NULL,
  phase TEXT NOT NULL,
  project_id TEXT,
  chat_id TEXT,
  thread_id TEXT,
  turn_id TEXT,
  workflow_id TEXT,
  cwd TEXT,
  title TEXT,
  request_json TEXT NOT NULL,
  result_json TEXT,
  last_error TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  app_server_generation INTEGER,
  lease_owner TEXT,
  lease_expires_at TEXT,
  next_attempt_at TEXT,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_heartbeat_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_codex_operations_client_request
ON codex_operations(client_request_id);

CREATE INDEX IF NOT EXISTS idx_codex_operations_status
ON codex_operations(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_codex_operations_turn
ON codex_operations(turn_id);

CREATE TABLE IF NOT EXISTS codex_prompt_submissions(
  prompt_submission_id TEXT PRIMARY KEY,
  project_id TEXT,
  project_path_key TEXT NOT NULL,
  operation_type TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  prompt_normalized TEXT NOT NULL,
  prompt_preview TEXT,
  operation_id TEXT,
  chat_id TEXT,
  thread_id TEXT,
  turn_id TEXT,
  workflow_id TEXT,
  status TEXT NOT NULL,
  duplicate_of_submission_id TEXT,
  similarity REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codex_prompt_submissions_hash
ON codex_prompt_submissions(project_path_key, prompt_hash);

CREATE INDEX IF NOT EXISTS idx_codex_prompt_submissions_project
ON codex_prompt_submissions(project_path_key, updated_at);

CREATE INDEX IF NOT EXISTS idx_codex_prompt_submissions_operation
ON codex_prompt_submissions(operation_id);

CREATE INDEX IF NOT EXISTS idx_codex_prompt_submissions_turn
ON codex_prompt_submissions(turn_id);

CREATE TABLE IF NOT EXISTS diagnostic_runs(
  diagnosis_id TEXT PRIMARY KEY,
  problem_text TEXT,
  context_json TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostic_findings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  diagnosis_id TEXT NOT NULL,
  severity TEXT NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  recommended_actions_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnostic_findings_run
ON diagnostic_findings(diagnosis_id, id);

CREATE TABLE IF NOT EXISTS repair_runs(
  repair_run_id TEXT PRIMARY KEY,
  diagnosis_id TEXT,
  action TEXT NOT NULL,
  dry_run INTEGER NOT NULL,
  force INTEGER NOT NULL,
  changed INTEGER NOT NULL,
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_interactions(
  interaction_id TEXT PRIMARY KEY,
  app_server_request_id TEXT NOT NULL,
  method TEXT NOT NULL,
  thread_id TEXT,
  turn_id TEXT,
  item_id TEXT,
  status TEXT NOT NULL,
  params_json TEXT NOT NULL,
  response_json TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  resolved_at TEXT,
  process_generation INTEGER,
  auto_resolved INTEGER NOT NULL DEFAULT 0,
  recommended_action TEXT,
  risk_summary_json TEXT,
  answer_schema_json TEXT,
  response_redacted INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_interactions_status
ON pending_interactions(status, expires_at);

CREATE INDEX IF NOT EXISTS idx_pending_interactions_turn
ON pending_interactions(turn_id, status);

CREATE INDEX IF NOT EXISTS idx_pending_interactions_thread
ON pending_interactions(thread_id, status);

CREATE TABLE IF NOT EXISTS pending_interaction_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  interaction_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_interaction_events_interaction
ON pending_interaction_events(interaction_id, id);

CREATE TABLE IF NOT EXISTS schema_migrations(
  name TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_cache(
  cache_key TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  transcript_path TEXT NOT NULL,
  transcript_size INTEGER NOT NULL,
  transcript_mtime_ns INTEGER NOT NULL,
  boundary_line INTEGER,
  model TEXT NOT NULL,
  filter_version TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_used_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summary_cache_thread
ON summary_cache(thread_id, transcript_path);

CREATE TABLE IF NOT EXISTS rolling_summaries(
  thread_id TEXT PRIMARY KEY,
  transcript_path TEXT NOT NULL,
  source_line_end INTEGER NOT NULL,
  summary_text TEXT NOT NULL,
  model TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool_name TEXT NOT NULL,
  thread_id TEXT,
  budget_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_search_docs(
  doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  project_id TEXT,
  project_path TEXT,
  title TEXT,
  preview TEXT,
  text TEXT,
  role TEXT,
  created_at TEXT,
  source_line_start INTEGER,
  doc_type TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  transcript_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_search_docs_thread
ON chat_search_docs(thread_id);

CREATE INDEX IF NOT EXISTS idx_chat_search_docs_transcript
ON chat_search_docs(transcript_path);

CREATE VIRTUAL TABLE IF NOT EXISTS chat_search_fts USING fts5(
  title,
  preview,
  text,
  tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS chat_search_transcripts(
  transcript_path TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  indexed_at TEXT NOT NULL,
  message_count INTEGER NOT NULL DEFAULT 0,
  parse_error_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS codex_hook_threads(
  thread_id TEXT PRIMARY KEY,
  session_id TEXT,
  project_path TEXT,
  project_path_key TEXT,
  title TEXT,
  created_at TEXT,
  updated_at TEXT,
  transcript_path TEXT,
  source TEXT NOT NULL DEFAULT 'hook_history'
);

CREATE INDEX IF NOT EXISTS idx_codex_hook_threads_project
ON codex_hook_threads(project_path_key, updated_at);

CREATE TABLE IF NOT EXISTS codex_hook_turns(
  turn_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  updated_at TEXT,
  completed_at TEXT,
  model TEXT,
  permission_mode TEXT,
  last_assistant_message TEXT,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_codex_hook_turns_thread
ON codex_hook_turns(thread_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_codex_hook_turns_status
ON codex_hook_turns(status, updated_at);

CREATE TABLE IF NOT EXISTS codex_hook_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL UNIQUE,
  event_hash TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL,
  turn_id TEXT,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT,
  sequence INTEGER NOT NULL DEFAULT 0,
  hook_event_name TEXT,
  text_kind TEXT
);

CREATE INDEX IF NOT EXISTS idx_codex_hook_messages_thread
ON codex_hook_messages(thread_id, id);

CREATE INDEX IF NOT EXISTS idx_codex_hook_messages_turn
ON codex_hook_messages(turn_id, id);

CREATE VIRTUAL TABLE IF NOT EXISTS codex_hook_messages_fts USING fts5(
  message_id UNINDEXED,
  thread_id UNINDEXED,
  turn_id UNINDEXED,
  role UNINDEXED,
  text,
  tokenize = 'unicode61'
);
"""


class McpStorage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._apply_schema_migrations()
        self._connection.commit()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.connect()
        assert self._connection is not None
        return self._connection

    def upsert_project(self, row: dict[str, Any]) -> None:
        existing = self.connection.execute(
            """
            SELECT project_id
            FROM projects
            WHERE normalized_path_key = ? OR path = ?
            LIMIT 1
            """,
            (row["normalized_path_key"], row["path"]),
        ).fetchone()
        if existing is not None and existing["project_id"] != row["project_id"]:
            self.connection.execute(
                """
                UPDATE projects SET
                  name=:name,
                  path=:path,
                  normalized_path_key=:normalized_path_key,
                  created_at=COALESCE(created_at, :created_at),
                  last_activity_at=:last_activity_at,
                  source=:source,
                  updated_at=:updated_at
                WHERE project_id=:existing_project_id
                """,
                {**row, "existing_project_id": existing["project_id"]},
            )
            return
        self.connection.execute(
            """
            INSERT INTO projects(project_id, name, path, normalized_path_key, created_at, last_activity_at, source, updated_at)
            VALUES(:project_id, :name, :path, :normalized_path_key, :created_at, :last_activity_at, :source, :updated_at)
            ON CONFLICT(project_id) DO UPDATE SET
              name=excluded.name,
              path=excluded.path,
              normalized_path_key=excluded.normalized_path_key,
              created_at=COALESCE(projects.created_at, excluded.created_at),
              last_activity_at=excluded.last_activity_at,
              source=excluded.source,
              updated_at=excluded.updated_at
            """,
            row,
        )

    def upsert_chat(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO chats(chat_id, thread_id, project_id, project_path, title, transcript_path, created_at, updated_at,
              archived, last_message_preview, status, status_confidence, source, updated_at_local)
            VALUES(:chat_id, :thread_id, :project_id, :project_path, :title, :transcript_path, :created_at, :updated_at,
              :archived, :last_message_preview, :status, :status_confidence, :source, :updated_at_local)
            ON CONFLICT(chat_id) DO UPDATE SET
              thread_id=excluded.thread_id,
              project_id=excluded.project_id,
              project_path=excluded.project_path,
              title=excluded.title,
              transcript_path=excluded.transcript_path,
              created_at=COALESCE(chats.created_at, excluded.created_at),
              updated_at=excluded.updated_at,
              archived=excluded.archived,
              last_message_preview=excluded.last_message_preview,
              status=excluded.status,
              status_confidence=excluded.status_confidence,
              source=excluded.source,
              updated_at_local=excluded.updated_at_local
            """,
            row,
        )

    def commit(self) -> None:
        self.connection.commit()

    def upsert_hook_thread(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO codex_hook_threads(
              thread_id, session_id, project_path, project_path_key, title,
              created_at, updated_at, transcript_path, source
            )
            VALUES(
              :thread_id, :session_id, :project_path, :project_path_key, :title,
              :created_at, :updated_at, :transcript_path, :source
            )
            ON CONFLICT(thread_id) DO UPDATE SET
              session_id=COALESCE(excluded.session_id, codex_hook_threads.session_id),
              project_path=COALESCE(excluded.project_path, codex_hook_threads.project_path),
              project_path_key=COALESCE(excluded.project_path_key, codex_hook_threads.project_path_key),
              title=COALESCE(excluded.title, codex_hook_threads.title),
              created_at=COALESCE(codex_hook_threads.created_at, excluded.created_at),
              updated_at=MAX(COALESCE(codex_hook_threads.updated_at, ''), COALESCE(excluded.updated_at, '')),
              transcript_path=COALESCE(excluded.transcript_path, codex_hook_threads.transcript_path),
              source=excluded.source
            """,
            {"source": "hook_history", **row},
        )

    def upsert_hook_turn(self, row: dict[str, Any]) -> None:
        payload = {"clear_last_error": 0, **row}
        self.connection.execute(
            """
            INSERT INTO codex_hook_turns(
              turn_id, thread_id, status, started_at, updated_at, completed_at,
              model, permission_mode, last_assistant_message, last_error
            )
            VALUES(
              :turn_id, :thread_id, :status, :started_at, :updated_at, :completed_at,
              :model, :permission_mode, :last_assistant_message, :last_error
            )
            ON CONFLICT(turn_id) DO UPDATE SET
              thread_id=excluded.thread_id,
              status=excluded.status,
              started_at=COALESCE(codex_hook_turns.started_at, excluded.started_at),
              updated_at=MAX(COALESCE(codex_hook_turns.updated_at, ''), COALESCE(excluded.updated_at, '')),
              completed_at=COALESCE(excluded.completed_at, codex_hook_turns.completed_at),
              model=COALESCE(excluded.model, codex_hook_turns.model),
              permission_mode=COALESCE(excluded.permission_mode, codex_hook_turns.permission_mode),
              last_assistant_message=COALESCE(excluded.last_assistant_message, codex_hook_turns.last_assistant_message),
              last_error=CASE
                WHEN :clear_last_error THEN NULL
                ELSE COALESCE(excluded.last_error, codex_hook_turns.last_error)
              END
            """,
            payload,
        )

    def record_hook_message(self, row: dict[str, Any]) -> bool:
        existing = self.connection.execute(
            """
            SELECT id, message_id
            FROM codex_hook_messages
            WHERE message_id = ? OR event_hash = ?
            LIMIT 1
            """,
            (row["message_id"], row["event_hash"]),
        ).fetchone()
        if existing is not None:
            row_id = int(existing["id"])
            if existing["message_id"] == row["message_id"]:
                self.connection.execute(
                    """
                    UPDATE codex_hook_messages SET
                      event_hash=:event_hash,
                      thread_id=:thread_id,
                      turn_id=:turn_id,
                      role=:role,
                      text=:text,
                      created_at=:created_at,
                      sequence=:sequence,
                      hook_event_name=:hook_event_name,
                      text_kind=:text_kind
                    WHERE id=:row_id
                    """,
                    {**row, "row_id": row_id},
                )
                self.connection.execute("DELETE FROM codex_hook_messages_fts WHERE rowid = ?", (row_id,))
                self.connection.execute(
                    """
                    INSERT INTO codex_hook_messages_fts(rowid, message_id, thread_id, turn_id, role, text)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        row["message_id"],
                        row["thread_id"],
                        row.get("turn_id"),
                        row["role"],
                        row["text"],
                    ),
                )
            return False
        cursor = self.connection.execute(
            """
            INSERT INTO codex_hook_messages(
              message_id, event_hash, thread_id, turn_id, role, text, created_at,
              sequence, hook_event_name, text_kind
            )
            VALUES(
              :message_id, :event_hash, :thread_id, :turn_id, :role, :text,
              :created_at, :sequence, :hook_event_name, :text_kind
            )
            """,
            row,
        )
        inserted = cursor.rowcount > 0
        if inserted:
            self.connection.execute(
                """
                INSERT INTO codex_hook_messages_fts(rowid, message_id, thread_id, turn_id, role, text)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    int(cursor.lastrowid),
                    row["message_id"],
                    row["thread_id"],
                    row.get("turn_id"),
                    row["role"],
                    row["text"],
                ),
            )
        return inserted

    def get_hook_thread(self, thread_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_hook_threads WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_hook_threads(self, *, limit: int = 10_000) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_hook_threads
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_hook_thread_by_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT threads.*
            FROM codex_hook_turns turns
            JOIN codex_hook_threads threads ON threads.thread_id = turns.thread_id
            WHERE turns.turn_id = ?
            LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_hook_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_hook_turns WHERE turn_id = ? LIMIT 1",
            (turn_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_hook_turns(self, thread_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_hook_turns
            WHERE thread_id = ?
            ORDER BY COALESCE(started_at, updated_at, completed_at), turn_id
            """,
            (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_hook_messages(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM codex_hook_messages
            {where}
            ORDER BY id ASC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_hook_messages(self, *, thread_id: str | None = None, turn_id: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        row = self.connection.execute(f"SELECT COUNT(*) AS count FROM codex_hook_messages {where}", params).fetchone()
        return int(row["count"] if row is not None else 0)

    def hook_history_status(self) -> dict[str, Any]:
        threads = self.connection.execute("SELECT COUNT(*) AS count FROM codex_hook_threads").fetchone()
        turns = self.connection.execute("SELECT COUNT(*) AS count FROM codex_hook_turns").fetchone()
        messages = self.connection.execute("SELECT COUNT(*) AS count FROM codex_hook_messages").fetchone()
        latest = self.connection.execute(
            """
            SELECT MAX(updated_at) AS updated_at
            FROM (
              SELECT updated_at FROM codex_hook_threads
              UNION ALL
              SELECT updated_at FROM codex_hook_turns
              UNION ALL
              SELECT created_at AS updated_at FROM codex_hook_messages
            )
            """
        ).fetchone()
        return {
            "threadCount": int(threads["count"] if threads is not None else 0),
            "turnCount": int(turns["count"] if turns is not None else 0),
            "messageCount": int(messages["count"] if messages is not None else 0),
            "lastHookEventAt": latest["updated_at"] if latest is not None else None,
        }

    def _apply_schema_migrations(self) -> None:
        self._add_column_if_missing("app_server_events", "process_generation", "INTEGER")
        self._add_column_if_missing("tracked_turns", "accepted_at", "TEXT")
        self._add_column_if_missing("tracked_turns", "request_id", "TEXT")
        self._add_column_if_missing("tracked_turns", "process_generation", "INTEGER")
        self._add_column_if_missing("tracked_turns", "last_event_seq", "INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing("codex_workflows", "execution_client_request_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "app_server_generation", "INTEGER")
        self._add_column_if_missing("codex_workflows", "workflow_kind", "TEXT NOT NULL DEFAULT 'plan_then_execute'")
        self._add_column_if_missing("codex_workflows", "current_operation_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "plan_operation_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "execution_operation_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "latest_plan_item_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "latest_plan_hash", "TEXT")
        self._add_column_if_missing("codex_workflows", "latest_report_hash", "TEXT")
        self._add_column_if_missing("codex_workflows", "final_report_json", "TEXT")
        self.connection.execute(
            "UPDATE codex_workflows SET workflow_kind = 'plan_then_execute' WHERE workflow_kind IS NULL OR workflow_kind = ''"
        )
        self._add_column_if_missing("codex_operations", "app_server_generation", "INTEGER")
        self._add_column_if_missing("codex_operations", "lease_owner", "TEXT")
        self._add_column_if_missing("codex_operations", "lease_expires_at", "TEXT")
        self._add_column_if_missing("codex_operations", "next_attempt_at", "TEXT")
        self._add_column_if_missing("codex_operations", "max_attempts", "INTEGER NOT NULL DEFAULT 3")
        self._add_column_if_missing("codex_operations", "last_heartbeat_at", "TEXT")
        self._add_column_if_missing("pending_interactions", "recommended_action", "TEXT")
        self._add_column_if_missing("pending_interactions", "risk_summary_json", "TEXT")
        self._add_column_if_missing("pending_interactions", "answer_schema_json", "TEXT")
        self._add_column_if_missing("pending_interactions", "response_redacted", "INTEGER NOT NULL DEFAULT 0")
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_codex_operations_lease
            ON codex_operations(status, next_attempt_at, lease_expires_at)
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_interaction_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              interaction_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              status TEXT NOT NULL,
              details_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pending_interaction_events_interaction
            ON pending_interaction_events(interaction_id, id)
            """
        )

    def _add_column_if_missing(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def record_app_server_event(self, direction: str, payload: dict[str, Any], received_at: str, *, process_generation: int | None = None) -> None:
        params = payload.get("params") or payload.get("result") or {}
        method = payload.get("method")
        thread_id = None
        turn_id = None
        if isinstance(params, dict):
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            thread = params.get("thread")
            turn = params.get("turn")
            if not thread_id and isinstance(thread, dict):
                thread_id = thread.get("id")
            if not turn_id and isinstance(turn, dict):
                turn_id = turn.get("id")
        self.connection.execute(
            """
            INSERT INTO app_server_events(direction, jsonrpc_id, method, thread_id, turn_id, process_generation, payload_json, received_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                direction,
                str(payload.get("id")) if payload.get("id") is not None else None,
                method,
                thread_id,
                turn_id,
                process_generation,
                json.dumps(payload, ensure_ascii=False),
                received_at,
            ),
        )
        self.connection.commit()

    def list_app_server_events(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        process_generation: int | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        if process_generation is not None:
            clauses.append("process_generation = ?")
            params.append(process_generation)
        if since:
            clauses.append("received_at >= ?")
            params.append(since)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM app_server_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def upsert_tracked_turn(self, row: dict[str, Any]) -> None:
        payload = {
            "accepted_at": row.get("accepted_at") or row.get("started_at"),
            "request_id": row.get("request_id"),
            "process_generation": row.get("process_generation"),
            "last_event_seq": int(row.get("last_event_seq") or 0),
            "clear_last_error": int(bool(row.get("clear_last_error"))),
            **row,
        }
        self.connection.execute(
            """
            INSERT INTO tracked_turns(
              turn_id, thread_id, chat_id, project_id, project_path, status,
              started_at, updated_at, completed_at, first_message_at,
              final_message, last_error, accepted_at, request_id,
              process_generation, last_event_seq, source
            )
            VALUES(
              :turn_id, :thread_id, :chat_id, :project_id, :project_path, :status,
              :started_at, :updated_at, :completed_at, :first_message_at,
              :final_message, :last_error, :accepted_at, :request_id,
              :process_generation, :last_event_seq, :source
            )
            ON CONFLICT(turn_id) DO UPDATE SET
              thread_id=excluded.thread_id,
              chat_id=COALESCE(excluded.chat_id, tracked_turns.chat_id),
              project_id=COALESCE(excluded.project_id, tracked_turns.project_id),
              project_path=COALESCE(excluded.project_path, tracked_turns.project_path),
              status=excluded.status,
              started_at=COALESCE(tracked_turns.started_at, excluded.started_at),
              updated_at=excluded.updated_at,
              completed_at=COALESCE(excluded.completed_at, tracked_turns.completed_at),
              first_message_at=COALESCE(tracked_turns.first_message_at, excluded.first_message_at),
              final_message=COALESCE(excluded.final_message, tracked_turns.final_message),
              accepted_at=COALESCE(tracked_turns.accepted_at, excluded.accepted_at),
              request_id=COALESCE(excluded.request_id, tracked_turns.request_id),
              process_generation=COALESCE(excluded.process_generation, tracked_turns.process_generation),
              last_event_seq=MAX(tracked_turns.last_event_seq, excluded.last_event_seq),
              last_error=CASE
                WHEN :clear_last_error THEN NULL
                ELSE COALESCE(excluded.last_error, tracked_turns.last_error)
              END,
              source=excluded.source
            """,
            payload,
        )
        self.connection.commit()

    def update_tracked_turn_status(
        self,
        turn_id: str,
        *,
        status: str,
        updated_at: str,
        completed_at: str | None = None,
        final_message: str | None = None,
        last_error: str | None = None,
        clear_last_error: bool = False,
    ) -> None:
        self.connection.execute(
            """
            UPDATE tracked_turns SET
              status = ?,
              updated_at = ?,
              completed_at = COALESCE(?, completed_at),
              final_message = COALESCE(?, final_message),
              last_error = CASE
                WHEN ? THEN NULL
                ELSE COALESCE(?, last_error)
              END
            WHERE turn_id = ?
            """,
            (status, updated_at, completed_at, final_message, int(clear_last_error), last_error, turn_id),
        )
        self.connection.commit()

    def record_tracked_turn_message(self, row: dict[str, Any]) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO tracked_turn_messages(
              event_hash, turn_id, thread_id, role, text, created_at, sequence,
              event_type, payload_json
            )
            VALUES(
              :event_hash, :turn_id, :thread_id, :role, :text, :created_at,
              :sequence, :event_type, :payload_json
            )
            """,
            row,
        )
        inserted = cursor.rowcount > 0
        if inserted:
            event_id = cursor.lastrowid
            self.connection.execute(
                """
                UPDATE tracked_turns SET
                  updated_at = COALESCE(:created_at, updated_at),
                  first_message_at = COALESCE(first_message_at, :created_at),
                  final_message = :text,
                  last_event_seq = MAX(last_event_seq, :event_id)
                WHERE turn_id = :turn_id
                """,
                {**row, "event_id": event_id},
            )
        self.connection.commit()
        return inserted

    def get_tracked_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tracked_turns WHERE turn_id = ? LIMIT 1",
            (turn_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_running_tracked_turns(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_turns
            WHERE status IN ('starting', 'running')
               OR status IN ('accepted', 'started', 'first_message_received', 'waiting_for_approval', 'waiting_for_user_input')
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_last_tracked_turn_messages(self, turn_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_turn_messages
            WHERE turn_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (turn_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def count_tracked_turn_messages(self, turn_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM tracked_turn_messages WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def append_tracked_plan_delta(self, row: dict[str, Any]) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO tracked_plan_events(
              event_hash, item_id, turn_id, thread_id, event_type, created_at, payload_json
            )
            VALUES(
              :event_hash, :item_id, :turn_id, :thread_id, :event_type, :created_at, :payload_json
            )
            """,
            row,
        )
        inserted = cursor.rowcount > 0
        if inserted:
            self.connection.execute(
                """
                INSERT INTO tracked_plan_items(
                  item_id, turn_id, thread_id, status, text, created_at,
                  updated_at, completed_at, sequence, payload_json
                )
                VALUES(
                  :item_id, :turn_id, :thread_id, 'in_progress', :delta,
                  :created_at, :created_at, NULL, :sequence, :payload_json
                )
                ON CONFLICT(turn_id, item_id) DO UPDATE SET
                  text=tracked_plan_items.text || excluded.text,
                  status=CASE
                    WHEN tracked_plan_items.status = 'completed' THEN tracked_plan_items.status
                    ELSE 'in_progress'
                  END,
                  updated_at=excluded.updated_at,
                  sequence=MAX(tracked_plan_items.sequence, excluded.sequence),
                  payload_json=excluded.payload_json
                """,
                row,
            )
        self.connection.commit()
        return inserted

    def upsert_tracked_plan_item(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO tracked_plan_items(
              item_id, turn_id, thread_id, status, text, created_at,
              updated_at, completed_at, sequence, payload_json
            )
            VALUES(
              :item_id, :turn_id, :thread_id, :status, :text, :created_at,
              :updated_at, :completed_at, :sequence, :payload_json
            )
            ON CONFLICT(turn_id, item_id) DO UPDATE SET
              thread_id=excluded.thread_id,
              status=excluded.status,
              text=excluded.text,
              created_at=COALESCE(tracked_plan_items.created_at, excluded.created_at),
              updated_at=excluded.updated_at,
              completed_at=COALESCE(excluded.completed_at, tracked_plan_items.completed_at),
              sequence=MAX(tracked_plan_items.sequence, excluded.sequence),
              payload_json=excluded.payload_json
            """,
            row,
        )
        self.connection.commit()

    def get_tracked_turn_plans(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_plan_items
            WHERE turn_id = ?
            ORDER BY id ASC
            """,
            (turn_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_thread_plans(self, thread_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_plan_items
            WHERE thread_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (thread_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_latest_plan_for_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM tracked_plan_items
            WHERE turn_id = ?
            ORDER BY
              CASE status WHEN 'completed' THEN 1 ELSE 0 END DESC,
              COALESCE(completed_at, updated_at, created_at) DESC,
              id DESC
            LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def create_workflow(self, row: dict[str, Any]) -> None:
        payload = {
            "workflow_kind": "plan_then_execute",
            "execution_client_request_id": None,
            "current_operation_id": None,
            "plan_operation_id": None,
            "execution_operation_id": None,
            "execution_turn_id": None,
            "latest_plan_item_id": None,
            "latest_plan_hash": None,
            "latest_report_hash": None,
            "final_report_json": None,
            "last_error": None,
            "completed_at": None,
            "app_server_generation": None,
            "metadata_json": "{}",
            **row,
        }
        self.connection.execute(
            """
            INSERT INTO codex_workflows(
              workflow_id, workflow_kind, client_request_id, execution_client_request_id,
              current_operation_id, plan_operation_id, execution_operation_id,
              project_id, thread_id, plan_turn_id, execution_turn_id,
              latest_plan_item_id, latest_plan_hash, latest_report_hash, final_report_json,
              phase, status, last_error, created_at, updated_at, completed_at,
              app_server_generation, metadata_json
            )
            VALUES(
              :workflow_id, :workflow_kind, :client_request_id, :execution_client_request_id,
              :current_operation_id, :plan_operation_id, :execution_operation_id,
              :project_id, :thread_id, :plan_turn_id, :execution_turn_id,
              :latest_plan_item_id, :latest_plan_hash, :latest_report_hash, :final_report_json,
              :phase, :status, :last_error, :created_at, :updated_at, :completed_at,
              :app_server_generation, :metadata_json
            )
            """,
            payload,
        )
        self.connection.commit()

    def update_workflow(self, workflow_id: str, **fields: Any) -> None:
        allowed = {
            "workflow_kind",
            "execution_client_request_id",
            "current_operation_id",
            "plan_operation_id",
            "execution_operation_id",
            "thread_id",
            "plan_turn_id",
            "execution_turn_id",
            "latest_plan_item_id",
            "latest_plan_hash",
            "latest_report_hash",
            "final_report_json",
            "phase",
            "status",
            "last_error",
            "updated_at",
            "completed_at",
            "app_server_generation",
            "metadata_json",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return
        assignments = ", ".join(f"{key} = :{key}" for key in selected)
        self.connection.execute(
            f"UPDATE codex_workflows SET {assignments} WHERE workflow_id = :workflow_id",
            {**selected, "workflow_id": workflow_id},
        )
        self.connection.commit()

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_workflows WHERE workflow_id = ? LIMIT 1",
            (workflow_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_workflow_by_client_request_id(self, client_request_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_workflows WHERE client_request_id = ? LIMIT 1",
            (client_request_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_workflow_event(
        self,
        workflow_id: str,
        *,
        event_type: str,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO codex_workflow_events(workflow_id, event_type, message, details_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                event_type,
                message,
                json.dumps(details or {}, ensure_ascii=False),
                created_at,
            ),
        )
        self.connection.commit()

    def list_workflow_events(self, workflow_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_workflow_events
            WHERE workflow_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (workflow_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def list_workflows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_workflows
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_operation(self, row: dict[str, Any]) -> bool:
        payload = {
            "attempt_count": 0,
            "result_json": None,
            "last_error": None,
            "started_at": None,
            "completed_at": None,
            "app_server_generation": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "next_attempt_at": None,
            "max_attempts": 3,
            "last_heartbeat_at": None,
            **row,
        }
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO codex_operations(
              operation_id, client_request_id, operation_type, status, phase,
              project_id, chat_id, thread_id, turn_id, workflow_id, cwd, title,
              request_json, result_json, last_error, attempt_count,
              created_at, updated_at, started_at, completed_at, app_server_generation,
              lease_owner, lease_expires_at, next_attempt_at, max_attempts, last_heartbeat_at
            )
            VALUES(
              :operation_id, :client_request_id, :operation_type, :status, :phase,
              :project_id, :chat_id, :thread_id, :turn_id, :workflow_id, :cwd, :title,
              :request_json, :result_json, :last_error, :attempt_count,
              :created_at, :updated_at, :started_at, :completed_at, :app_server_generation,
              :lease_owner, :lease_expires_at, :next_attempt_at, :max_attempts, :last_heartbeat_at
            )
            """,
            payload,
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def update_operation(self, operation_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "phase",
            "project_id",
            "chat_id",
            "thread_id",
            "turn_id",
            "workflow_id",
            "cwd",
            "title",
            "request_json",
            "result_json",
            "last_error",
            "attempt_count",
            "updated_at",
            "started_at",
            "completed_at",
            "app_server_generation",
            "lease_owner",
            "lease_expires_at",
            "next_attempt_at",
            "max_attempts",
            "last_heartbeat_at",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return
        assignments = ", ".join(f"{key} = :{key}" for key in selected)
        self.connection.execute(
            f"UPDATE codex_operations SET {assignments} WHERE operation_id = :operation_id",
            {**selected, "operation_id": operation_id},
        )
        self.connection.commit()

    def increment_operation_attempt(self, operation_id: str, *, started_at: str, updated_at: str) -> None:
        self.connection.execute(
            """
            UPDATE codex_operations SET
              attempt_count = attempt_count + 1,
              started_at = COALESCE(started_at, ?),
              next_attempt_at = NULL,
              updated_at = ?
            WHERE operation_id = ?
            """,
            (started_at, updated_at, operation_id),
        )
        self.connection.commit()

    def acquire_operation_lease(
        self,
        operation_id: str,
        *,
        lease_owner: str,
        now: str,
        lease_expires_at: str,
    ) -> dict[str, Any] | None:
        startable = tuple(sorted(OPERATION_STARTABLE_STATUSES))
        placeholders = ",".join("?" for _ in startable)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                f"""
                SELECT *
                FROM codex_operations
                WHERE operation_id = ?
                  AND status IN ({placeholders})
                  AND COALESCE(attempt_count, 0) < COALESCE(max_attempts, 3)
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (
                    lease_owner IS NULL
                    OR lease_owner = ?
                    OR lease_expires_at IS NULL
                    OR lease_expires_at <= ?
                  )
                LIMIT 1
                """,
                (operation_id, *startable, now, lease_owner, now),
            ).fetchone()
            if row is None:
                self.connection.rollback()
                return None
            self.connection.execute(
                """
                UPDATE codex_operations
                SET lease_owner = ?,
                    lease_expires_at = ?,
                    last_heartbeat_at = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (lease_owner, lease_expires_at, now, now, operation_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_operation(operation_id)

    def heartbeat_operation_lease(self, operation_id: str, *, lease_owner: str, now: str, lease_expires_at: str) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE codex_operations
            SET lease_expires_at = ?,
                last_heartbeat_at = ?,
                updated_at = ?
            WHERE operation_id = ?
              AND lease_owner = ?
            """,
            (lease_expires_at, now, now, operation_id, lease_owner),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def release_operation_lease(self, operation_id: str, *, lease_owner: str, updated_at: str) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE codex_operations
            SET lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                updated_at = ?
            WHERE operation_id = ?
              AND lease_owner = ?
            """,
            (updated_at, operation_id, lease_owner),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def list_startable_operations(self, *, now: str, limit: int = 50) -> list[dict[str, Any]]:
        startable = tuple(sorted(OPERATION_STARTABLE_STATUSES))
        placeholders = ",".join("?" for _ in startable)
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM codex_operations
            WHERE status IN ({placeholders})
              AND COALESCE(attempt_count, 0) < COALESCE(max_attempts, 3)
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (*startable, now, now, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_operation_failed_if_attempts_exhausted(self, operation_id: str, *, updated_at: str, message: str) -> bool:
        startable = tuple(sorted(OPERATION_STARTABLE_STATUSES))
        placeholders = ",".join("?" for _ in startable)
        cursor = self.connection.execute(
            f"""
            UPDATE codex_operations
            SET status = 'failed',
                phase = 'failed',
                last_error = ?,
                completed_at = ?,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                updated_at = ?
            WHERE operation_id = ?
              AND status IN ({placeholders})
              AND turn_id IS NULL
              AND COALESCE(attempt_count, 0) >= COALESCE(max_attempts, 3)
            """,
            (message, updated_at, updated_at, operation_id, *startable),
        )
        if cursor.rowcount:
            self.connection.execute(
                """
                UPDATE codex_prompt_submissions
                SET status = 'failed',
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (updated_at, operation_id),
            )
        self.connection.commit()
        return cursor.rowcount > 0

    def recover_startup_operations(self, *, now: str) -> dict[str, Any]:
        startable = tuple(sorted(OPERATION_STARTABLE_STATUSES))
        placeholders = ",".join("?" for _ in startable)
        rows = self.connection.execute(
            f"""
            SELECT operation_id, turn_id
            FROM codex_operations
            WHERE status IN ({placeholders})
              AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
            """,
            (*startable, now),
        ).fetchall()
        reset_ids: list[str] = []
        running_ids: list[str] = []
        for row in rows:
            operation_id = str(row["operation_id"])
            if row["turn_id"]:
                running_ids.append(operation_id)
                self.connection.execute(
                    """
                    UPDATE codex_operations
                    SET status = 'running',
                        phase = 'running',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_heartbeat_at = NULL,
                        next_attempt_at = NULL,
                        updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (now, operation_id),
                )
                self.connection.execute(
                    """
                    UPDATE codex_prompt_submissions
                    SET status = 'running',
                        updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (now, operation_id),
                )
            else:
                reset_ids.append(operation_id)
                self.connection.execute(
                    """
                    UPDATE codex_operations
                    SET status = 'queued',
                        phase = 'queued',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_heartbeat_at = NULL,
                        updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (now, operation_id),
                )
                self.connection.execute(
                    """
                    UPDATE codex_prompt_submissions
                    SET status = 'queued',
                        updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (now, operation_id),
                )
        self.connection.commit()
        return {"resetOperationIds": reset_ids, "runningOperationIds": running_ids}

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_operations WHERE operation_id = ? LIMIT 1",
            (operation_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_operation_by_client_request_id(self, client_request_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_operations WHERE client_request_id = ? LIMIT 1",
            (client_request_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_operations(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                """
                SELECT *
                FROM codex_operations
                WHERE status = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM codex_operations
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_operations_for_workflow(self, workflow_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_operations
            WHERE workflow_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (workflow_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_stale_operations(self, *, stale_before: str, limit: int = 50) -> list[dict[str, Any]]:
        active = tuple(sorted(OPERATION_ACTIVE_STATUSES))
        placeholders = ",".join("?" for _ in active)
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM codex_operations
            WHERE status IN ({placeholders})
              AND updated_at < ?
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (*active, stale_before, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_stale_operations(
        self,
        *,
        stale_before: str,
        now: str,
        operation_id: str | None = None,
        dry_run: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        startable = tuple(sorted(OPERATION_STARTABLE_STATUSES))
        placeholders = ",".join("?" for _ in startable)
        clauses = [
            f"status IN ({placeholders})",
            "updated_at < ?",
            "(lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)",
        ]
        params: list[Any] = [*startable, stale_before, now]
        if operation_id:
            clauses.append("operation_id = ?")
            params.append(operation_id)
        rows = self.connection.execute(
            f"""
            SELECT operation_id, turn_id, status, phase, updated_at, lease_owner, lease_expires_at
            FROM codex_operations
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        candidates = [dict(row) for row in rows]
        reset_ids: list[str] = []
        running_ids: list[str] = []
        if not dry_run:
            for row in candidates:
                row_operation_id = str(row["operation_id"])
                if row.get("turn_id"):
                    running_ids.append(row_operation_id)
                    status = "running"
                    phase = "running"
                else:
                    reset_ids.append(row_operation_id)
                    status = "queued"
                    phase = "queued"
                self.connection.execute(
                    """
                    UPDATE codex_operations
                    SET status = ?,
                        phase = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_heartbeat_at = NULL,
                        next_attempt_at = NULL,
                        updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (status, phase, now, row_operation_id),
                )
                self.connection.execute(
                    """
                    UPDATE codex_prompt_submissions
                    SET status = ?,
                        updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (status, now, row_operation_id),
                )
            self.connection.commit()
        return {
            "matchedOperationIds": [str(row["operation_id"]) for row in candidates],
            "resetOperationIds": reset_ids,
            "runningOperationIds": running_ids,
            "matchedOperationCount": len(candidates),
            "dryRun": dry_run,
            "staleBefore": stale_before,
        }

    def create_prompt_submission(self, row: dict[str, Any]) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO codex_prompt_submissions(
              prompt_submission_id, project_id, project_path_key, operation_type,
              prompt_hash, prompt_normalized, prompt_preview, operation_id,
              chat_id, thread_id, turn_id, workflow_id, status,
              duplicate_of_submission_id, similarity, created_at, updated_at
            )
            VALUES(
              :prompt_submission_id, :project_id, :project_path_key, :operation_type,
              :prompt_hash, :prompt_normalized, :prompt_preview, :operation_id,
              :chat_id, :thread_id, :turn_id, :workflow_id, :status,
              :duplicate_of_submission_id, :similarity, :created_at, :updated_at
            )
            """,
            row,
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def update_prompt_submission(self, prompt_submission_id: str, **fields: Any) -> None:
        allowed = {
            "project_id",
            "operation_type",
            "operation_id",
            "chat_id",
            "thread_id",
            "turn_id",
            "workflow_id",
            "status",
            "duplicate_of_submission_id",
            "similarity",
            "updated_at",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return
        assignments = ", ".join(f"{key} = :{key}" for key in selected)
        self.connection.execute(
            f"UPDATE codex_prompt_submissions SET {assignments} WHERE prompt_submission_id = :prompt_submission_id",
            {**selected, "prompt_submission_id": prompt_submission_id},
        )
        self.connection.commit()

    def update_prompt_submission_by_operation(self, operation_id: str, **fields: Any) -> None:
        allowed = {
            "chat_id",
            "thread_id",
            "turn_id",
            "workflow_id",
            "status",
            "updated_at",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return
        assignments = ", ".join(f"{key} = :{key}" for key in selected)
        self.connection.execute(
            f"UPDATE codex_prompt_submissions SET {assignments} WHERE operation_id = :operation_id",
            {**selected, "operation_id": operation_id},
        )
        self.connection.commit()

    def get_prompt_submission(self, prompt_submission_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_prompt_submissions WHERE prompt_submission_id = ? LIMIT 1",
            (prompt_submission_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_prompt_submission_by_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_prompt_submissions WHERE operation_id = ? ORDER BY created_at DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_prompt_submissions_by_hash(self, project_path_key: str, prompt_hash: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_prompt_submissions
            WHERE project_path_key = ? AND prompt_hash = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (project_path_key, prompt_hash, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_prompt_submissions_for_project(self, project_path_key: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_prompt_submissions
            WHERE project_path_key = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (project_path_key, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_prompt_submissions(
        self,
        *,
        operation_id: str | None = None,
        workflow_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if operation_id:
            clauses.append("operation_id = ?")
            params.append(operation_id)
        if workflow_id:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT prompt_submission_id, project_id, project_path_key, operation_type,
                   prompt_hash, prompt_preview, operation_id, chat_id, thread_id, turn_id,
                   workflow_id, status, duplicate_of_submission_id, similarity,
                   created_at, updated_at
            FROM codex_prompt_submissions
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def cleanup_prompt_submissions(self, *, older_than: str, dry_run: bool) -> dict[str, Any]:
        statuses = tuple(sorted(PROMPT_SUBMISSION_CLEANUP_STATUSES))
        placeholders = ",".join("?" for _ in statuses)
        count_row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM codex_prompt_submissions
            WHERE updated_at < ?
              AND status IN ({placeholders})
            """,
            (older_than, *statuses),
        ).fetchone()
        matched = int(count_row["count"] if count_row is not None else 0)
        deleted = 0
        if not dry_run and matched:
            cursor = self.connection.execute(
                f"""
                DELETE FROM codex_prompt_submissions
                WHERE updated_at < ?
                  AND status IN ({placeholders})
                """,
                (older_than, *statuses),
            )
            self.connection.commit()
            deleted = cursor.rowcount
        return {
            "matchedPromptSubmissions": matched,
            "deletedPromptSubmissions": deleted,
            "olderThan": older_than,
            "terminalStatuses": list(statuses),
        }

    def upsert_pending_interaction(self, row: dict[str, Any]) -> None:
        row = {
            "recommended_action": None,
            "risk_summary_json": None,
            "answer_schema_json": None,
            "response_redacted": 0,
            **row,
        }
        self.connection.execute(
            """
            INSERT INTO pending_interactions(
              interaction_id, app_server_request_id, method, thread_id, turn_id,
              item_id, status, params_json, response_json, created_at, expires_at,
              resolved_at, process_generation, auto_resolved, recommended_action,
              risk_summary_json, answer_schema_json, response_redacted, last_error
            )
            VALUES(
              :interaction_id, :app_server_request_id, :method, :thread_id, :turn_id,
              :item_id, :status, :params_json, :response_json, :created_at, :expires_at,
              :resolved_at, :process_generation, :auto_resolved, :recommended_action,
              :risk_summary_json, :answer_schema_json, :response_redacted, :last_error
            )
            ON CONFLICT(interaction_id) DO UPDATE SET
              status=excluded.status,
              response_json=excluded.response_json,
              resolved_at=excluded.resolved_at,
              auto_resolved=excluded.auto_resolved,
              recommended_action=COALESCE(excluded.recommended_action, pending_interactions.recommended_action),
              risk_summary_json=COALESCE(excluded.risk_summary_json, pending_interactions.risk_summary_json),
              answer_schema_json=COALESCE(excluded.answer_schema_json, pending_interactions.answer_schema_json),
              response_redacted=excluded.response_redacted,
              last_error=excluded.last_error
            """,
            row,
        )
        self.connection.commit()

    def update_pending_interaction(
        self,
        interaction_id: str,
        *,
        status: str,
        resolved_at: str | None = None,
        response: dict[str, Any] | None = None,
        auto_resolved: bool | None = None,
        response_redacted: bool | None = None,
        last_error: str | None = None,
        event_type: str | None = None,
        event_details: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE pending_interactions SET
              status = ?,
              resolved_at = COALESCE(?, resolved_at),
              response_json = COALESCE(?, response_json),
              auto_resolved = COALESCE(?, auto_resolved),
              response_redacted = COALESCE(?, response_redacted),
              last_error = COALESCE(?, last_error)
            WHERE interaction_id = ?
            """,
            (
                status,
                resolved_at,
                json.dumps(response, ensure_ascii=False) if response is not None else None,
                int(auto_resolved) if auto_resolved is not None else None,
                int(response_redacted) if response_redacted is not None else None,
                last_error,
                interaction_id,
            ),
        )
        if event_type:
            self._record_pending_interaction_event_uncommitted(
                interaction_id,
                event_type=event_type,
                status=status,
                details=event_details or {},
                created_at=resolved_at or "",
            )
        self.connection.commit()

    def get_pending_interaction(self, interaction_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM pending_interactions WHERE interaction_id = ? LIMIT 1",
            (interaction_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_pending_interactions(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM pending_interactions
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_pending_interactions(self, *, status: str = "pending") -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM pending_interactions WHERE status = ?",
            (status,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def expire_pending_interactions(self, *, expires_before: str, resolved_at: str, reason: str) -> int:
        rows = self.connection.execute(
            """
            SELECT interaction_id
            FROM pending_interactions
            WHERE status = 'pending'
              AND expires_at <= ?
            """,
            (expires_before,),
        ).fetchall()
        cursor = self.connection.execute(
            """
            UPDATE pending_interactions SET
              status = 'expired',
              resolved_at = COALESCE(resolved_at, ?),
              last_error = COALESCE(last_error, ?)
            WHERE status = 'pending'
              AND expires_at <= ?
            """,
            (resolved_at, reason, expires_before),
        )
        for row in rows:
            self._record_pending_interaction_event_uncommitted(
                str(row["interaction_id"]),
                event_type="expired",
                status="expired",
                details={"reason": reason},
                created_at=resolved_at,
            )
        self.connection.commit()
        return int(cursor.rowcount)

    def mark_pending_interactions_orphaned(self, *, process_generation: int | None, reason: str, resolved_at: str) -> int:
        if process_generation is None:
            rows = self.connection.execute(
                """
                SELECT interaction_id
                FROM pending_interactions
                WHERE status = 'pending'
                """
            ).fetchall()
            cursor = self.connection.execute(
                """
                UPDATE pending_interactions SET
                  status = 'orphaned_after_app_server_exit',
                  resolved_at = COALESCE(resolved_at, ?),
                  last_error = COALESCE(last_error, ?)
                WHERE status = 'pending'
                """,
                (resolved_at, reason),
            )
        else:
            rows = self.connection.execute(
                """
                SELECT interaction_id
                FROM pending_interactions
                WHERE status = 'pending'
                  AND (process_generation = ? OR process_generation IS NULL)
                """,
                (process_generation,),
            ).fetchall()
            cursor = self.connection.execute(
                """
                UPDATE pending_interactions SET
                  status = 'orphaned_after_app_server_exit',
                  resolved_at = COALESCE(resolved_at, ?),
                  last_error = COALESCE(last_error, ?)
                WHERE status = 'pending'
                  AND (process_generation = ? OR process_generation IS NULL)
                """,
                (resolved_at, reason, process_generation),
            )
        for row in rows:
            self._record_pending_interaction_event_uncommitted(
                str(row["interaction_id"]),
                event_type="orphaned",
                status="orphaned_after_app_server_exit",
                details={"reason": reason, "processGeneration": process_generation},
                created_at=resolved_at,
            )
        self.connection.commit()
        return int(cursor.rowcount)

    def record_pending_interaction_event(
        self,
        interaction_id: str,
        *,
        event_type: str,
        status: str,
        details: dict[str, Any] | None,
        created_at: str,
    ) -> None:
        self._record_pending_interaction_event_uncommitted(
            interaction_id,
            event_type=event_type,
            status=status,
            details=details or {},
            created_at=created_at,
        )
        self.connection.commit()

    def list_pending_interaction_events(self, interaction_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM pending_interaction_events
            WHERE interaction_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (interaction_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def _record_pending_interaction_event_uncommitted(
        self,
        interaction_id: str,
        *,
        event_type: str,
        status: str,
        details: dict[str, Any],
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO pending_interaction_events(interaction_id, event_type, status, details_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                interaction_id,
                event_type,
                status,
                json.dumps(details, ensure_ascii=False),
                created_at,
            ),
        )

    def get_summary_cache(self, cache_key: str, used_at: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT summary_json, created_at
            FROM summary_cache
            WHERE cache_key = ?
            LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        self.connection.execute(
            "UPDATE summary_cache SET last_used_at = ? WHERE cache_key = ?",
            (used_at, cache_key),
        )
        self.connection.commit()
        payload = json.loads(row["summary_json"])
        payload["created_at"] = payload.get("created_at") or row["created_at"]
        return payload

    def upsert_summary_cache(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO summary_cache(
              cache_key, thread_id, transcript_path, transcript_size, transcript_mtime_ns,
              boundary_line, model, filter_version, summary_json, created_at, last_used_at
            )
            VALUES(
              :cache_key, :thread_id, :transcript_path, :transcript_size, :transcript_mtime_ns,
              :boundary_line, :model, :filter_version, :summary_json, :created_at, :last_used_at
            )
            ON CONFLICT(cache_key) DO UPDATE SET
              summary_json=excluded.summary_json,
              last_used_at=excluded.last_used_at
            """,
            row,
        )
        self.connection.commit()

    def has_summary_cache_for_thread(self, thread_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM summary_cache WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row is not None

    def get_rolling_summary(self, thread_id: str, transcript_path: str, before_line: int | None) -> dict[str, Any] | None:
        if before_line is None:
            return None
        row = self.connection.execute(
            """
            SELECT thread_id, transcript_path, source_line_end, summary_text, model, updated_at
            FROM rolling_summaries
            WHERE thread_id = ? AND transcript_path = ? AND source_line_end < ?
            ORDER BY source_line_end DESC
            LIMIT 1
            """,
            (thread_id, transcript_path, before_line),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_rolling_summary(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO rolling_summaries(thread_id, transcript_path, source_line_end, summary_text, model, updated_at)
            VALUES(:thread_id, :transcript_path, :source_line_end, :summary_text, :model, :updated_at)
            ON CONFLICT(thread_id) DO UPDATE SET
              transcript_path=excluded.transcript_path,
              source_line_end=excluded.source_line_end,
              summary_text=excluded.summary_text,
              model=excluded.model,
              updated_at=excluded.updated_at
            """,
            row,
        )
        self.connection.commit()

    def record_budget_audit(self, tool_name: str, thread_id: str | None, budget: dict[str, Any], created_at: str) -> None:
        self.connection.execute(
            """
            INSERT INTO budget_audit(tool_name, thread_id, budget_json, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (tool_name, thread_id, json.dumps(budget, ensure_ascii=False), created_at),
        )
        self.connection.commit()

    def record_diagnostic_run(
        self,
        *,
        diagnosis_id: str,
        problem_text: str | None,
        context: dict[str, Any],
        summary: dict[str, Any],
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO diagnostic_runs(diagnosis_id, problem_text, context_json, summary_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                diagnosis_id,
                problem_text,
                json.dumps(context, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                created_at,
            ),
        )
        self.connection.commit()

    def record_diagnostic_finding(
        self,
        *,
        diagnosis_id: str,
        severity: str,
        category: str,
        title: str,
        evidence: list[Any],
        recommended_actions: list[dict[str, Any]],
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO diagnostic_findings(
              diagnosis_id, severity, category, title, evidence_json,
              recommended_actions_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                diagnosis_id,
                severity,
                category,
                title,
                json.dumps(evidence, ensure_ascii=False),
                json.dumps(recommended_actions, ensure_ascii=False),
                created_at,
            ),
        )
        self.connection.commit()

    def record_repair_run(
        self,
        *,
        repair_run_id: str,
        diagnosis_id: str | None,
        action: str,
        dry_run: bool,
        force: bool,
        changed: bool,
        before: dict[str, Any],
        after: dict[str, Any],
        result: dict[str, Any],
        created_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO repair_runs(
              repair_run_id, diagnosis_id, action, dry_run, force, changed,
              before_json, after_json, result_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repair_run_id,
                diagnosis_id,
                action,
                int(dry_run),
                int(force),
                int(changed),
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                created_at,
            ),
        )
        self.connection.commit()
