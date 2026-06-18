from __future__ import annotations

from . import storage as _storage

globals().update(_storage.__dict__)


class StorageSchemaMixin:
    def _apply_schema_migrations(self) -> None:
        self._add_column_if_missing("app_server_events", "process_generation", "INTEGER")
        self._add_column_if_missing("tracked_turns", "accepted_at", "TEXT")
        self._add_column_if_missing("tracked_turns", "request_id", "TEXT")
        self._add_column_if_missing("tracked_turns", "process_generation", "INTEGER")
        self._add_column_if_missing("tracked_turns", "last_event_seq", "INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing("tracked_turns", "last_assistant_message", "TEXT")
        self._add_column_if_missing("codex_workflows", "execution_client_request_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "app_server_generation", "INTEGER")
        self._add_column_if_missing("codex_workflows", "workflow_kind", "TEXT NOT NULL DEFAULT 'plan_then_execute'")
        self._add_column_if_missing("codex_workflows", "current_operation_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "plan_operation_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "execution_operation_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "review_operation_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "review_source_thread_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "review_thread_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "review_turn_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "review_target_json", "TEXT")
        self._add_column_if_missing("codex_workflows", "review_delivery", "TEXT")
        self._add_column_if_missing("codex_workflows", "latest_plan_item_id", "TEXT")
        self._add_column_if_missing("codex_workflows", "latest_plan_hash", "TEXT")
        self._add_column_if_missing("codex_workflows", "latest_report_hash", "TEXT")
        self._add_column_if_missing("codex_workflows", "final_report_json", "TEXT")
        self._add_column_if_missing("codex_workflows", "goal_objective", "TEXT")
        self._add_column_if_missing("codex_workflows", "goal_token_budget", "INTEGER")
        self._add_column_if_missing("codex_workflows", "goal_completion_action", "TEXT NOT NULL DEFAULT 'clear'")
        self._add_column_if_missing("codex_workflows", "goal_completion_objective", "TEXT")
        self._add_column_if_missing("codex_workflows", "goal_sync_state", "TEXT NOT NULL DEFAULT 'not_configured'")
        self._add_column_if_missing("codex_workflows", "goal_app_server_json", "TEXT")
        self._add_column_if_missing("codex_workflows", "goal_last_error", "TEXT")
        self._add_column_if_missing("codex_workflows", "goal_last_synced_at", "TEXT")
        self._add_column_if_missing("codex_workflows", "goal_cleared_at", "TEXT")
        self._add_column_if_missing("codex_workflows", "goal_managed_hash", "TEXT")
        self.connection.execute(
            "UPDATE codex_workflows SET workflow_kind = 'plan_then_execute' WHERE workflow_kind IS NULL OR workflow_kind = ''"
        )
        self.connection.execute(
            "UPDATE codex_workflows SET goal_completion_action = 'clear' WHERE goal_completion_action IS NULL OR goal_completion_action = ''"
        )
        self.connection.execute(
            "UPDATE codex_workflows SET goal_sync_state = 'not_configured' WHERE goal_sync_state IS NULL OR goal_sync_state = ''"
        )
        self._add_column_if_missing("codex_operations", "app_server_generation", "INTEGER")
        self._add_column_if_missing("codex_operations", "latest_report_hash", "TEXT")
        self._add_column_if_missing("codex_operations", "final_report_json", "TEXT")
        self._add_column_if_missing("codex_operations", "lease_owner", "TEXT")
        self._add_column_if_missing("codex_operations", "lease_expires_at", "TEXT")
        self._add_column_if_missing("codex_operations", "next_attempt_at", "TEXT")
        self._add_column_if_missing("codex_operations", "max_attempts", "INTEGER NOT NULL DEFAULT 3")
        self._add_column_if_missing("codex_operations", "last_heartbeat_at", "TEXT")
        self._add_column_if_missing("codex_operations", "submitter_config_fingerprint", "TEXT")
        self._add_column_if_missing("codex_operations", "worker_config_fingerprint", "TEXT")
        self._add_column_if_missing("codex_operations", "worker_config_summary_json", "TEXT")
        self._add_column_if_missing("pending_interactions", "recommended_action", "TEXT")
        self._add_column_if_missing("pending_interactions", "risk_summary_json", "TEXT")
        self._add_column_if_missing("pending_interactions", "answer_schema_json", "TEXT")
        self._add_column_if_missing("pending_interactions", "response_redacted", "INTEGER NOT NULL DEFAULT 0")
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_codex_workflows_review_thread
            ON codex_workflows(review_thread_id)
            """
        )
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
        self.connection.execute(
            """
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
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tracked_turn_progress_turn
            ON tracked_turn_progress_events(turn_id, id)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tracked_turn_progress_thread
            ON tracked_turn_progress_events(thread_id, id)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tracked_turn_progress_category
            ON tracked_turn_progress_events(category, severity, created_at)
            """
        )
        self.connection.execute(
            """
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
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_codex_thread_lifecycle_thread
            ON codex_thread_lifecycle_actions(thread_id, created_at)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_codex_thread_lifecycle_status
            ON codex_thread_lifecycle_actions(status, updated_at)
            """
        )

    def _add_column_if_missing(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
