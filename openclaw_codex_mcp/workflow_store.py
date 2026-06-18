from __future__ import annotations

from . import storage as _storage

globals().update(_storage.__dict__)


class WorkflowStoreMixin:
    def create_workflow(self, row: dict[str, Any]) -> None:
        payload = {
            "workflow_kind": "plan_then_execute",
            "execution_client_request_id": None,
            "current_operation_id": None,
            "plan_operation_id": None,
            "execution_operation_id": None,
            "review_operation_id": None,
            "review_source_thread_id": None,
            "review_thread_id": None,
            "review_turn_id": None,
            "review_target_json": None,
            "review_delivery": None,
            "execution_turn_id": None,
            "latest_plan_item_id": None,
            "latest_plan_hash": None,
            "latest_report_hash": None,
            "final_report_json": None,
            "goal_objective": None,
            "goal_token_budget": None,
            "goal_completion_action": "clear",
            "goal_completion_objective": None,
            "goal_sync_state": "not_configured",
            "goal_app_server_json": None,
            "goal_last_error": None,
            "goal_last_synced_at": None,
            "goal_cleared_at": None,
            "goal_managed_hash": None,
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
              review_operation_id, review_source_thread_id, review_thread_id, review_turn_id,
              review_target_json, review_delivery,
              project_id, thread_id, plan_turn_id, execution_turn_id,
              latest_plan_item_id, latest_plan_hash, latest_report_hash, final_report_json,
              goal_objective, goal_token_budget, goal_completion_action, goal_completion_objective,
              goal_sync_state, goal_app_server_json, goal_last_error, goal_last_synced_at,
              goal_cleared_at, goal_managed_hash,
              phase, status, last_error, created_at, updated_at, completed_at,
              app_server_generation, metadata_json
            )
            VALUES(
              :workflow_id, :workflow_kind, :client_request_id, :execution_client_request_id,
              :current_operation_id, :plan_operation_id, :execution_operation_id,
              :review_operation_id, :review_source_thread_id, :review_thread_id, :review_turn_id,
              :review_target_json, :review_delivery,
              :project_id, :thread_id, :plan_turn_id, :execution_turn_id,
              :latest_plan_item_id, :latest_plan_hash, :latest_report_hash, :final_report_json,
              :goal_objective, :goal_token_budget, :goal_completion_action, :goal_completion_objective,
              :goal_sync_state, :goal_app_server_json, :goal_last_error, :goal_last_synced_at,
              :goal_cleared_at, :goal_managed_hash,
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
            "review_operation_id",
            "review_source_thread_id",
            "review_thread_id",
            "review_turn_id",
            "review_target_json",
            "review_delivery",
            "thread_id",
            "plan_turn_id",
            "execution_turn_id",
            "latest_plan_item_id",
            "latest_plan_hash",
            "latest_report_hash",
            "final_report_json",
            "goal_objective",
            "goal_token_budget",
            "goal_completion_action",
            "goal_completion_objective",
            "goal_sync_state",
            "goal_app_server_json",
            "goal_last_error",
            "goal_last_synced_at",
            "goal_cleared_at",
            "goal_managed_hash",
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
