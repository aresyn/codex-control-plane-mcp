from __future__ import annotations

from . import tools as _tools

globals().update(_tools.__dict__)


class WorkerServiceMixin:
    def codex_get_worker_status(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = _bounded_int(args.get("limit", 20), 1, 100)
        workers = [_worker_row_to_tool(row) for row in self.storage.list_workers(limit=limit)]
        commands: list[dict[str, Any]] = []
        if bool(args.get("include_recent_commands", True)):
            commands = [_worker_command_row_to_tool(row) for row in self.storage.list_worker_commands(limit=limit)]
        return {
            "ok": True,
            "executionMode": self.config.execution_mode,
            "workerOwner": self._worker_owner,
            "configFingerprint": self._config_fingerprint,
            "startupRecovery": self._startup_recovery,
            "workers": workers,
            "recentCommands": commands,
            "nextRecommendedAction": "inspect_worker_health" if not _has_live_worker(workers) else "none",
            "recommendedPollAfterSeconds": 10,
            "pollRecommended": not _has_live_worker(workers),
        }

    def codex_get_queue_status(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = _bounded_int(args.get("limit", 100), 1, 500)
        status_filter = _optional_string(args.get("status"))
        rows = self.storage.list_operation_scheduling(limit=limit)
        entries = [_operation_scheduling_row_to_tool(row) for row in rows]
        if status_filter:
            entries = [row for row in entries if row.get("queueStatus") == status_filter]
        reasons: dict[str, int] = {}
        for entry in entries:
            reason = str(entry.get("queuedReason") or "none")
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "ok": True,
            "executionMode": self.config.execution_mode,
            "count": len(entries),
            "queueReasons": reasons,
            "operations": entries,
            "nextRecommendedAction": "wait_for_worker_slot" if entries else "none",
            "recommendedPollAfterSeconds": 10 if entries else 0,
            "pollRecommended": bool(entries),
        }

    def codex_get_concurrency_status(self, args: dict[str, Any]) -> dict[str, Any]:
        include_locks = bool(args.get("include_locks", True))
        limit = _bounded_int(args.get("limit", 200), 1, 500)
        active_operations = self.storage.connection.execute(
            """
            SELECT ops.operation_id, ops.operation_type, ops.status, ops.project_id, ops.cwd,
                   ops.thread_id, ops.turn_id, sched.agent_id, sched.resource_keys_json
              FROM codex_operations AS ops
              LEFT JOIN codex_operation_scheduling AS sched ON sched.operation_id = ops.operation_id
             WHERE ops.status IN ('starting_app_server', 'starting_thread', 'starting_turn',
                                  'starting_review', 'running', 'first_message_received',
                                  'waiting_for_approval', 'waiting_for_user_input')
             ORDER BY ops.updated_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        active = [dict(row) for row in active_operations]
        per_project: dict[str, int] = {}
        per_agent: dict[str, int] = {}
        per_thread: dict[str, int] = {}
        for row in active:
            project_key = _project_concurrency_key(row)
            agent_key = str(row.get("agent_id") or "unknown")
            thread_key = str(row.get("thread_id") or "none")
            per_project[project_key] = per_project.get(project_key, 0) + 1
            per_agent[agent_key] = per_agent.get(agent_key, 0) + 1
            if thread_key != "none":
                per_thread[thread_key] = per_thread.get(thread_key, 0) + 1
        locks = [_resource_lock_row_to_tool(row) for row in self.storage.list_resource_locks(limit=limit)] if include_locks else []
        return {
            "ok": True,
            "executionMode": self.config.execution_mode,
            "limits": _concurrency_limits_to_tool(self.config),
            "activeTurnCount": len(active),
            "activeOperations": [_active_operation_to_tool(row) for row in active],
            "counts": {
                "global": len(active),
                "perProject": per_project,
                "perAgent": per_agent,
                "perThread": per_thread,
            },
            "resourceLocks": locks,
            "resourceLockCount": len(locks),
            "nextRecommendedAction": "none",
            "recommendedPollAfterSeconds": 0,
            "pollRecommended": False,
        }

    def codex_get_worker_command_status(self, args: dict[str, Any]) -> dict[str, Any]:
        command_id = _required_string(args, "command_id")
        row = self.storage.get_worker_command(command_id)
        if row is None:
            raise invalid_argument("Worker command was not found.", command_id=command_id)
        payload = _worker_command_row_to_tool(row)
        payload["ok"] = True
        payload["pollRecommended"] = payload.get("status") not in {"completed", "failed", "cancelled", "canceled"}
        payload["recommendedPollAfterSeconds"] = 2 if payload["pollRecommended"] else 0
        payload["nextRecommendedAction"] = "poll_worker_command" if payload["pollRecommended"] else "none"
        return payload


def _worker_row_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "workerId": row.get("worker_id"),
        "role": row.get("role"),
        "status": row.get("status"),
        "pid": row.get("pid"),
        "hostname": row.get("hostname"),
        "configFingerprint": row.get("config_fingerprint"),
        "startedAt": row.get("started_at"),
        "lastHeartbeatAt": row.get("last_heartbeat_at"),
        "activeOperationCount": row.get("active_operation_count") or 0,
        "activeTurnCount": row.get("active_turn_count") or 0,
        "appServerGeneration": row.get("app_server_generation"),
        "lastError": row.get("last_error"),
        "stalenessSeconds": _staleness_seconds(str(row.get("last_heartbeat_at") or "")),
    }


def _worker_command_row_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    request = _json_dict(row.get("request_json"))
    result = _json_dict(row.get("result_json"))
    return {
        "commandId": row.get("command_id"),
        "commandType": row.get("command_type"),
        "status": row.get("status"),
        "request": redact_payload(request),
        "result": redact_payload(result) if result else None,
        "workerId": row.get("worker_id"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "completedAt": row.get("completed_at"),
        "lastError": row.get("last_error"),
    }


def _operation_scheduling_row_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "operationId": row.get("operation_id"),
        "operationType": row.get("operation_type"),
        "operationStatus": row.get("operation_status"),
        "queueStatus": row.get("queue_status"),
        "queuedReason": row.get("queued_reason"),
        "priority": row.get("priority"),
        "estimatedCostClass": row.get("estimated_cost_class"),
        "agentId": row.get("agent_id"),
        "resourceKeys": _json_list(row.get("resource_keys_json")),
        "slotClaim": _json_dict(row.get("slot_claim_json")),
        "workerId": row.get("worker_id"),
        "scheduledAt": row.get("scheduled_at"),
        "threadId": row.get("thread_id"),
        "turnId": row.get("turn_id"),
        "projectId": row.get("project_id"),
        "cwdKey": path_key(row.get("cwd")) if row.get("cwd") else None,
        "updatedAt": row.get("updated_at"),
    }


def _resource_lock_row_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "lockKey": row.get("lock_key"),
        "operationId": row.get("operation_id"),
        "threadId": row.get("thread_id"),
        "projectId": row.get("project_id"),
        "lockMode": row.get("lock_mode"),
        "workerId": row.get("worker_id"),
        "expiresAt": row.get("expires_at"),
        "createdAt": row.get("created_at"),
    }


def _active_operation_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "operationId": row.get("operation_id"),
        "operationType": row.get("operation_type"),
        "status": row.get("status"),
        "projectId": row.get("project_id"),
        "cwdKey": path_key(row.get("cwd")) if row.get("cwd") else None,
        "threadId": row.get("thread_id"),
        "turnId": row.get("turn_id"),
        "agentId": row.get("agent_id"),
        "resourceKeys": _json_list(row.get("resource_keys_json")),
    }


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _has_live_worker(workers: list[dict[str, Any]]) -> bool:
    for worker in workers:
        if worker.get("role") == "worker" and worker.get("status") == "running":
            staleness = worker.get("stalenessSeconds")
            if staleness is None or int(staleness) < 120:
                return True
    return False


def _project_concurrency_key(row: dict[str, Any]) -> str:
    if row.get("project_id"):
        return str(row["project_id"])
    if row.get("cwd"):
        return "cwd:" + path_key(row.get("cwd"))
    return "unknown"


def _concurrency_limits_to_tool(config: Any) -> dict[str, Any]:
    return {
        "maxActiveTurnsGlobal": config.max_active_turns_global,
        "maxActiveTurnsPerProject": config.max_active_turns_per_project,
        "maxActiveTurnsPerAgent": config.max_active_turns_per_agent,
        "maxActiveTurnsPerThread": config.max_active_turns_per_thread,
        "maxActiveWriteTurnsPerProject": config.max_active_write_turns_per_project,
        "maxAppServerPendingRequests": config.max_app_server_pending_requests,
    }
