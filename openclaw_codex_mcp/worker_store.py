from __future__ import annotations

import json
from typing import Any


class WorkerStoreMixin:
    def upsert_worker(
        self,
        *,
        worker_id: str,
        role: str,
        status: str,
        pid: int,
        hostname: str,
        config_fingerprint: str,
        started_at: str,
        last_heartbeat_at: str,
        active_operation_count: int = 0,
        active_turn_count: int = 0,
        app_server_generation: int | None = None,
        last_error: str | None = None,
    ) -> None:
        self.execute_write_with_retry(
            """
            INSERT INTO codex_workers(
              worker_id, role, status, pid, hostname, config_fingerprint,
              started_at, last_heartbeat_at, active_operation_count,
              active_turn_count, app_server_generation, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
              role=excluded.role,
              status=excluded.status,
              pid=excluded.pid,
              hostname=excluded.hostname,
              config_fingerprint=excluded.config_fingerprint,
              last_heartbeat_at=excluded.last_heartbeat_at,
              active_operation_count=excluded.active_operation_count,
              active_turn_count=excluded.active_turn_count,
              app_server_generation=excluded.app_server_generation,
              last_error=excluded.last_error
            """,
            (
                worker_id,
                role,
                status,
                pid,
                hostname,
                config_fingerprint,
                started_at,
                last_heartbeat_at,
                active_operation_count,
                active_turn_count,
                app_server_generation,
                last_error,
            ),
        )

    def update_worker_status(
        self,
        worker_id: str,
        *,
        status: str,
        last_heartbeat_at: str,
        active_operation_count: int = 0,
        active_turn_count: int = 0,
        app_server_generation: int | None = None,
        last_error: str | None = None,
    ) -> None:
        self.execute_write_with_retry(
            """
            UPDATE codex_workers
               SET status = ?,
                   last_heartbeat_at = ?,
                   active_operation_count = ?,
                   active_turn_count = ?,
                   app_server_generation = ?,
                   last_error = ?
             WHERE worker_id = ?
            """,
            (
                status,
                last_heartbeat_at,
                active_operation_count,
                active_turn_count,
                app_server_generation,
                last_error,
                worker_id,
            ),
        )

    def list_workers(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
              FROM codex_workers
             ORDER BY last_heartbeat_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_workers WHERE worker_id = ? LIMIT 1",
            (worker_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_operation_scheduling(
        self,
        *,
        operation_id: str,
        agent_id: str | None,
        priority: str,
        estimated_cost_class: str,
        resource_keys: list[str],
        queue_status: str,
        queued_reason: str | None,
        created_at: str,
        updated_at: str,
    ) -> None:
        self.execute_write_with_retry(
            """
            INSERT INTO codex_operation_scheduling(
              operation_id, agent_id, queue_status, queued_reason, priority,
              estimated_cost_class, resource_keys_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operation_id) DO UPDATE SET
              agent_id=excluded.agent_id,
              priority=excluded.priority,
              estimated_cost_class=excluded.estimated_cost_class,
              resource_keys_json=excluded.resource_keys_json,
              updated_at=excluded.updated_at
            """,
            (
                operation_id,
                agent_id,
                queue_status,
                queued_reason,
                priority,
                estimated_cost_class,
                json.dumps(resource_keys, ensure_ascii=False),
                created_at,
                updated_at,
            ),
        )

    def get_operation_scheduling(self, operation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_operation_scheduling WHERE operation_id = ? LIMIT 1",
            (operation_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def update_operation_scheduling(
        self,
        operation_id: str,
        *,
        queue_status: str,
        queued_reason: str | None,
        updated_at: str,
        worker_id: str | None = None,
        slot_claim: dict[str, Any] | None = None,
        scheduled_at: str | None = None,
    ) -> None:
        self.execute_write_with_retry(
            """
            UPDATE codex_operation_scheduling
               SET queue_status = ?,
                   queued_reason = ?,
                   updated_at = ?,
                   worker_id = COALESCE(?, worker_id),
                   slot_claim_json = ?,
                   scheduled_at = COALESCE(?, scheduled_at)
             WHERE operation_id = ?
            """,
            (
                queue_status,
                queued_reason,
                updated_at,
                worker_id,
                json.dumps(slot_claim, ensure_ascii=False) if slot_claim is not None else None,
                scheduled_at,
                operation_id,
            ),
        )

    def list_operation_scheduling(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT sched.*, ops.status AS operation_status, ops.thread_id, ops.turn_id,
                   ops.project_id, ops.cwd, ops.operation_type, ops.request_json,
                   ops.submitter_config_fingerprint,
                   ops.updated_at AS operation_updated_at
              FROM codex_operation_scheduling AS sched
              LEFT JOIN codex_operations AS ops ON ops.operation_id = sched.operation_id
             ORDER BY sched.updated_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def cleanup_resource_locks(self, *, now: str) -> int:
        cursor = self.execute_write_with_retry(
            "DELETE FROM codex_resource_locks WHERE expires_at <= ?",
            (now,),
        )
        return int(cursor.rowcount or 0)

    def list_resource_locks(self, *, operation_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if operation_id:
            rows = self.connection.execute(
                """
                SELECT *
                  FROM codex_resource_locks
                 WHERE operation_id = ?
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (operation_id, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT *
                  FROM codex_resource_locks
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_resource_locks_for_operation(
        self,
        *,
        operation_id: str,
        locks: list[dict[str, Any]],
    ) -> None:
        def operation() -> None:
            self.connection.execute(
                "DELETE FROM codex_resource_locks WHERE operation_id = ?",
                (operation_id,),
            )
            for lock in locks:
                self.connection.execute(
                    """
                    INSERT INTO codex_resource_locks(
                      lock_key, operation_id, thread_id, project_id,
                      lock_mode, worker_id, expires_at, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lock["lock_key"],
                        operation_id,
                        lock.get("thread_id"),
                        lock.get("project_id"),
                        lock.get("lock_mode") or "exclusive",
                        lock["worker_id"],
                        lock["expires_at"],
                        lock["created_at"],
                    ),
                )
            self.connection.commit()

        self._sqlite_retry(operation, attempts=6)

    def release_resource_locks_for_operation(self, operation_id: str) -> None:
        self.execute_write_with_retry(
            "DELETE FROM codex_resource_locks WHERE operation_id = ?",
            (operation_id,),
        )

    def backfill_resource_lock_thread(self, operation_id: str, *, thread_id: str | None, project_id: str | None = None) -> None:
        if not thread_id:
            return
        self.execute_write_with_retry(
            """
            UPDATE codex_resource_locks
               SET thread_id = COALESCE(thread_id, ?),
                   project_id = COALESCE(project_id, ?)
             WHERE operation_id = ?
            """,
            (thread_id, project_id, operation_id),
        )

    def ensure_thread_active_lock_for_operation(
        self,
        operation_id: str,
        *,
        thread_id: str | None,
        project_id: str | None,
        worker_id: str | None,
        expires_at: str,
        created_at: str,
    ) -> None:
        if not thread_id:
            return
        self.execute_write_with_retry(
            """
            INSERT OR IGNORE INTO codex_resource_locks(
              lock_key, operation_id, thread_id, project_id, lock_mode, worker_id, expires_at, created_at
            )
            VALUES (?, ?, ?, ?, 'exclusive', ?, ?, ?)
            """,
            (
                f"thread:{thread_id}:active-turn",
                operation_id,
                thread_id,
                project_id,
                worker_id or "unknown",
                expires_at,
                created_at,
            ),
        )

    def create_worker_command(
        self,
        *,
        command_id: str,
        command_type: str,
        status: str,
        request: dict[str, Any],
        created_at: str,
        updated_at: str,
    ) -> bool:
        cursor = self.execute_write_with_retry(
            """
            INSERT OR IGNORE INTO codex_worker_commands(
              command_id, command_type, status, request_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                command_type,
                status,
                json.dumps(request, ensure_ascii=False, sort_keys=True),
                created_at,
                updated_at,
            ),
        )
        return bool(cursor.rowcount)

    def get_worker_command(self, command_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_worker_commands WHERE command_id = ? LIMIT 1",
            (command_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_worker_commands(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                """
                SELECT *
                  FROM codex_worker_commands
                 WHERE status = ?
                 ORDER BY updated_at ASC
                 LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT *
                  FROM codex_worker_commands
                 ORDER BY updated_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_worker_command(self, command_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "result_json", "worker_id", "updated_at", "completed_at", "last_error"}
        assignments = []
        params: dict[str, Any] = {"command_id": command_id}
        for key, value in fields.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = :{key}")
            params[key] = value
        if not assignments:
            return
        self.execute_write_with_retry(
            f"UPDATE codex_worker_commands SET {', '.join(assignments)} WHERE command_id = :command_id",
            params,
        )

    def upsert_status_snapshot(
        self,
        *,
        snapshot_key: str,
        snapshot_type: str,
        scope_id: str | None,
        payload: dict[str, Any],
        created_at: str,
        expires_at: str | None = None,
    ) -> None:
        self.execute_write_with_retry(
            """
            INSERT INTO codex_status_snapshots(
              snapshot_key, snapshot_type, scope_id, payload_json, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
              snapshot_type=excluded.snapshot_type,
              scope_id=excluded.scope_id,
              payload_json=excluded.payload_json,
              created_at=excluded.created_at,
              expires_at=excluded.expires_at
            """,
            (
                snapshot_key,
                snapshot_type,
                scope_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                created_at,
                expires_at,
            ),
        )

    def get_latest_status_snapshot(self, snapshot_type: str, *, scope_id: str | None = None) -> dict[str, Any] | None:
        if scope_id is None:
            row = self.connection.execute(
                """
                SELECT *
                  FROM codex_status_snapshots
                 WHERE snapshot_type = ?
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (snapshot_type,),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT *
                  FROM codex_status_snapshots
                 WHERE snapshot_type = ? AND scope_id = ?
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (snapshot_type, scope_id),
            ).fetchone()
        return dict(row) if row is not None else None
