from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, TypeVar

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
  last_assistant_message TEXT,
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

CREATE TABLE IF NOT EXISTS tracked_turn_progress_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_hash TEXT NOT NULL UNIQUE,
  turn_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  category TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  item_id TEXT,
  sequence INTEGER NOT NULL DEFAULT 0,
  text TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  truncated INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tracked_turn_progress_turn
ON tracked_turn_progress_events(turn_id, id);

CREATE INDEX IF NOT EXISTS idx_tracked_turn_progress_thread
ON tracked_turn_progress_events(thread_id, id);

CREATE INDEX IF NOT EXISTS idx_tracked_turn_progress_category
ON tracked_turn_progress_events(category, severity, created_at);

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
  review_operation_id TEXT,
  review_source_thread_id TEXT,
  review_thread_id TEXT,
  review_turn_id TEXT,
  review_target_json TEXT,
  review_delivery TEXT,
  project_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  plan_turn_id TEXT NOT NULL,
  execution_turn_id TEXT,
  latest_plan_item_id TEXT,
  latest_plan_hash TEXT,
  latest_report_hash TEXT,
  final_report_json TEXT,
  goal_objective TEXT,
  goal_token_budget INTEGER,
  goal_completion_action TEXT NOT NULL DEFAULT 'clear',
  goal_completion_objective TEXT,
  goal_sync_state TEXT NOT NULL DEFAULT 'not_configured',
  goal_app_server_json TEXT,
  goal_last_error TEXT,
  goal_last_synced_at TEXT,
  goal_cleared_at TEXT,
  goal_managed_hash TEXT,
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
  latest_report_hash TEXT,
  final_report_json TEXT,
  lease_owner TEXT,
  lease_expires_at TEXT,
  next_attempt_at TEXT,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_heartbeat_at TEXT,
  submitter_config_fingerprint TEXT,
  worker_config_fingerprint TEXT,
  worker_config_summary_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_codex_operations_client_request
ON codex_operations(client_request_id);

CREATE INDEX IF NOT EXISTS idx_codex_operations_status
ON codex_operations(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_codex_operations_turn
ON codex_operations(turn_id);

CREATE TABLE IF NOT EXISTS codex_thread_lifecycle_actions(
  action_id TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  project_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  request_json TEXT NOT NULL,
  result_json TEXT,
  last_error TEXT,
  app_server_generation INTEGER,
  observed_event_id INTEGER,
  target_turn_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_codex_thread_lifecycle_thread
ON codex_thread_lifecycle_actions(thread_id, created_at);

CREATE INDEX IF NOT EXISTS idx_codex_thread_lifecycle_status
ON codex_thread_lifecycle_actions(status, updated_at);

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

CREATE TABLE IF NOT EXISTS agent_guidance_attempts(
  guard_key TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL,
  last_attempt_at TEXT,
  cooldown_until TEXT,
  last_result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_guidance_attempts_scope
ON agent_guidance_attempts(scope_type, scope_id, action);

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

_T = TypeVar("_T")


class McpStorage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._configure_connection()
        self._initialize_schema_with_retry()

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

    def execute_write_with_retry(
        self,
        sql: str,
        parameters: Any = (),
        *,
        commit: bool = True,
        attempts: int = 6,
    ) -> sqlite3.Cursor:
        cursor: sqlite3.Cursor | None = None

        def operation() -> sqlite3.Cursor:
            nonlocal cursor
            cursor = self.connection.execute(sql, parameters)
            if commit:
                self.connection.commit()
            assert cursor is not None
            return cursor

        return self._sqlite_retry(operation, attempts=attempts)

    def _configure_connection(self) -> None:
        assert self._connection is not None
        pragmas = [
            ("PRAGMA busy_timeout=30000", True),
            ("PRAGMA journal_mode=WAL", False),
            ("PRAGMA synchronous=NORMAL", False),
            ("PRAGMA foreign_keys=ON", False),
        ]
        for statement, required in pragmas:
            try:
                self._connection.execute(statement)
            except sqlite3.DatabaseError:
                if required:
                    raise

    def _initialize_schema_with_retry(self) -> None:
        def operation() -> None:
            assert self._connection is not None
            self._connection.executescript(SCHEMA)
            self._apply_schema_migrations()
            self._connection.commit()

        self._sqlite_retry(operation, attempts=10, base_delay_seconds=0.05, max_delay_seconds=1.0)

    def _sqlite_retry(
        self,
        operation: Callable[[], _T],
        *,
        attempts: int = 6,
        base_delay_seconds: float = 0.025,
        max_delay_seconds: float = 0.5,
    ) -> _T:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(max(1, attempts)):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_busy(exc) or attempt >= attempts - 1:
                    raise
                last_error = exc
                with suppress(sqlite3.Error):
                    self.connection.rollback()
                delay = min(max_delay_seconds, base_delay_seconds * (2**attempt))
                time.sleep(delay + random.uniform(0, delay / 3))
        assert last_error is not None
        raise last_error


def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text or "database is busy" in text

def _install_storage_mixins() -> None:
    from .diagnostic_store import DiagnosticStoreMixin
    from .hook_store import HookStoreMixin
    from .operation_store import OperationStoreMixin
    from .search_store import SearchStoreMixin
    from .storage_schema import StorageSchemaMixin
    from .turn_store import TurnStoreMixin
    from .workflow_store import WorkflowStoreMixin

    for mixin in (
        StorageSchemaMixin,
        HookStoreMixin,
        TurnStoreMixin,
        WorkflowStoreMixin,
        OperationStoreMixin,
        DiagnosticStoreMixin,
        SearchStoreMixin,
    ):
        for name, value in mixin.__dict__.items():
            if name.startswith("__"):
                continue
            if callable(value):
                setattr(McpStorage, name, value)


_install_storage_mixins()
