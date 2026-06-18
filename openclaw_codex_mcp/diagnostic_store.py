from __future__ import annotations

from . import storage as _storage

globals().update(_storage.__dict__)


class DiagnosticStoreMixin:
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
