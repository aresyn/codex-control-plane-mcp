from __future__ import annotations

from . import storage as _storage

globals().update(_storage.__dict__)


class OperationStoreMixin:
    def create_thread_lifecycle_action(self, row: dict[str, Any]) -> bool:
        payload = {
            "project_id": None,
            "completed_at": None,
            "result_json": None,
            "last_error": None,
            "app_server_generation": None,
            "observed_event_id": None,
            "target_turn_id": None,
            **row,
        }
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO codex_thread_lifecycle_actions(
              action_id, action_type, thread_id, project_id, status,
              created_at, updated_at, completed_at, request_json, result_json,
              last_error, app_server_generation, observed_event_id, target_turn_id
            )
            VALUES(
              :action_id, :action_type, :thread_id, :project_id, :status,
              :created_at, :updated_at, :completed_at, :request_json, :result_json,
              :last_error, :app_server_generation, :observed_event_id, :target_turn_id
            )
            """,
            payload,
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def update_thread_lifecycle_action(self, action_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "project_id",
            "updated_at",
            "completed_at",
            "request_json",
            "result_json",
            "last_error",
            "app_server_generation",
            "observed_event_id",
            "target_turn_id",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return
        assignments = ", ".join(f"{key} = :{key}" for key in selected)
        self.connection.execute(
            f"UPDATE codex_thread_lifecycle_actions SET {assignments} WHERE action_id = :action_id",
            {**selected, "action_id": action_id},
        )
        self.connection.commit()

    def get_thread_lifecycle_action(self, action_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_thread_lifecycle_actions WHERE action_id = ? LIMIT 1",
            (action_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_thread_lifecycle_actions(self, *, thread_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM codex_thread_lifecycle_actions
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (*params, limit),
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
            "latest_report_hash": None,
            "final_report_json": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "next_attempt_at": None,
            "max_attempts": 3,
            "last_heartbeat_at": None,
            "submitter_config_fingerprint": None,
            "worker_config_fingerprint": None,
            "worker_config_summary_json": None,
            **row,
        }
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO codex_operations(
              operation_id, client_request_id, operation_type, status, phase,
              project_id, chat_id, thread_id, turn_id, workflow_id, cwd, title,
              request_json, result_json, last_error, attempt_count,
              created_at, updated_at, started_at, completed_at, app_server_generation,
              latest_report_hash, final_report_json,
              lease_owner, lease_expires_at, next_attempt_at, max_attempts, last_heartbeat_at,
              submitter_config_fingerprint, worker_config_fingerprint, worker_config_summary_json
            )
            VALUES(
              :operation_id, :client_request_id, :operation_type, :status, :phase,
              :project_id, :chat_id, :thread_id, :turn_id, :workflow_id, :cwd, :title,
              :request_json, :result_json, :last_error, :attempt_count,
              :created_at, :updated_at, :started_at, :completed_at, :app_server_generation,
              :latest_report_hash, :final_report_json,
              :lease_owner, :lease_expires_at, :next_attempt_at, :max_attempts, :last_heartbeat_at,
              :submitter_config_fingerprint, :worker_config_fingerprint, :worker_config_summary_json
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
            "latest_report_hash",
            "final_report_json",
            "lease_owner",
            "lease_expires_at",
            "next_attempt_at",
            "max_attempts",
            "last_heartbeat_at",
            "submitter_config_fingerprint",
            "worker_config_fingerprint",
            "worker_config_summary_json",
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
        worker_config_fingerprint: str | None = None,
        allow_cross_config_recovery: bool = False,
        config_mismatch_message: str | None = None,
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
            submitter_fingerprint = row["submitter_config_fingerprint"] if "submitter_config_fingerprint" in row.keys() else None
            if (
                not allow_cross_config_recovery
                and submitter_fingerprint
                and worker_config_fingerprint
                and submitter_fingerprint != worker_config_fingerprint
            ):
                self.connection.rollback()
                return None
            self.connection.execute(
                """
                UPDATE codex_operations
                SET lease_owner = ?,
                    lease_expires_at = ?,
                    last_heartbeat_at = ?,
                    updated_at = ?,
                    worker_config_fingerprint = ?
                WHERE operation_id = ?
                """,
                (lease_owner, lease_expires_at, now, now, worker_config_fingerprint, operation_id),
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

    def list_startable_operations(
        self,
        *,
        now: str,
        limit: int = 50,
        worker_config_fingerprint: str | None = None,
        allow_cross_config_recovery: bool = False,
    ) -> list[dict[str, Any]]:
        startable = tuple(sorted(OPERATION_STARTABLE_STATUSES))
        placeholders = ",".join("?" for _ in startable)
        fingerprint_clause = ""
        params: list[Any] = [*startable, now, now]
        if not allow_cross_config_recovery and worker_config_fingerprint:
            fingerprint_clause = """
              AND (
                submitter_config_fingerprint IS NULL
                OR submitter_config_fingerprint = ?
              )
            """
            params.append(worker_config_fingerprint)
        params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM codex_operations
            WHERE status IN ({placeholders})
              AND COALESCE(attempt_count, 0) < COALESCE(max_attempts, 3)
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
              {fingerprint_clause}
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            tuple(params),
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
            SELECT operation_id, operation_type, thread_id, turn_id, request_json
            FROM codex_operations
            WHERE status IN ({placeholders})
              AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
            """,
            (*startable, now),
        ).fetchall()
        reset_ids: list[str] = []
        running_ids: list[str] = []
        completed_ids: list[str] = []
        unknown_ids: list[str] = []
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
                payload: dict[str, Any] = {}
                try:
                    decoded = json.loads(str(row["request_json"] or "{}"))
                    if isinstance(decoded, dict):
                        payload = decoded
                except json.JSONDecodeError:
                    payload = {}
                if (
                    row["operation_type"] == "fork_thread"
                    and row["thread_id"]
                    and not str(payload.get("message") or "").strip()
                ):
                    completed_ids.append(operation_id)
                    self.connection.execute(
                        """
                        UPDATE codex_operations
                        SET status = 'completed',
                            phase = 'completed',
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            last_heartbeat_at = NULL,
                            next_attempt_at = NULL,
                            last_error = NULL,
                            completed_at = ?,
                            updated_at = ?
                        WHERE operation_id = ?
                        """,
                        (now, now, operation_id),
                    )
                    self.connection.execute(
                        """
                        UPDATE codex_prompt_submissions
                        SET status = 'completed',
                            updated_at = ?
                        WHERE operation_id = ?
                        """,
                        (now, operation_id),
                    )
                    continue
                if row["operation_type"] == "review_start" and payload.get("_review_start_attempted"):
                    unknown_ids.append(operation_id)
                    self.connection.execute(
                        """
                        UPDATE codex_operations
                        SET status = 'unknown_after_app_server_exit',
                            phase = 'unknown_after_app_server_exit',
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            last_heartbeat_at = NULL,
                            next_attempt_at = NULL,
                            last_error = COALESCE(last_error, 'MCP server restarted after review/start attempt before review turn id was persisted.'),
                            completed_at = ?,
                            updated_at = ?
                        WHERE operation_id = ?
                        """,
                        (now, now, operation_id),
                    )
                    continue
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
        return {
            "resetOperationIds": reset_ids,
            "runningOperationIds": running_ids,
            "completedOperationIds": completed_ids,
            "unknownOperationIds": unknown_ids,
        }

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
