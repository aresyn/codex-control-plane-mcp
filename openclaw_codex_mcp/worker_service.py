from __future__ import annotations

from . import tools as _tools
from .statuses import OPERATION_TERMINAL_STATUSES, TURN_ACTIVE_STATUSES

globals().update(_tools.__dict__)


SLOT_STARTING_STATUSES = ("starting_app_server", "starting_thread", "starting_review", "starting_turn")


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
        include_terminal = bool(args.get("include_terminal", False))
        rows = self.storage.list_operation_scheduling(limit=limit)
        entries = [_operation_scheduling_row_to_tool(row) for row in rows]
        if not include_terminal:
            entries = [
                row
                for row in entries
                if _queue_entry_should_be_visible(row, self.storage)
            ]
        if status_filter:
            entries = [row for row in entries if row.get("queueStatus") == status_filter]
        reasons: dict[str, int] = {}
        for entry in entries:
            reason = str(entry.get("queuedReason") or "none")
            reasons[reason] = reasons.get(reason, 0) + 1
        queued = [entry for entry in entries if entry.get("queueStatus") == "queued"]
        running = [entry for entry in entries if entry.get("queueStatus") in {"scheduled", "running"} and _queue_entry_consumes_turn_slot(entry)]
        auxiliary = [entry for entry in entries if entry.get("queueStatus") in {"scheduled", "running"} and not _queue_entry_consumes_turn_slot(entry)]
        blocked_by_locks = [entry for entry in queued if entry.get("queuedReason") in {"resource_lock_conflict", "write_project_slot_limit"}]
        return {
            "ok": True,
            "executionMode": self.config.execution_mode,
            "count": len(entries),
            "queueSummary": {
                "queued": len(queued),
                "runningTurnOperations": len(running),
                "auxiliaryOperations": len(auxiliary),
                "activeTurnSlots": len(running),
                "blockedByLocks": len(blocked_by_locks),
            },
            "queuedOperations": queued,
            "runningOperations": running,
            "auxiliaryOperations": auxiliary,
            "blockedByLocks": blocked_by_locks,
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
                   ops.request_json,
                   ops.thread_id, ops.turn_id, sched.agent_id, sched.resource_keys_json
              FROM codex_operations AS ops
              LEFT JOIN codex_operation_scheduling AS sched ON sched.operation_id = ops.operation_id
              LEFT JOIN tracked_turns AS turns ON turns.turn_id = ops.turn_id
             WHERE ops.status IN (?, ?, ?, ?)
                OR turns.status IN (?, ?, ?, ?, ?, ?)
                OR sched.queue_status = 'scheduled'
             ORDER BY ops.updated_at DESC
             LIMIT ?
            """,
            SLOT_STARTING_STATUSES + tuple(TURN_ACTIVE_STATUSES) + (limit,),
        ).fetchall()
        active = [
            row
            for row in (dict(item) for item in active_operations)
            if _operation_consumes_turn_slot(row, _json_dict(row.get("request_json")))
        ]
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
        started = time.monotonic()
        command_id = _required_string(args, "command_id")
        include_result = bool(args.get("include_result", True))
        max_result_chars = _bounded_int(args.get("max_result_chars", 12000), 0, 200000)
        row = self.storage.get_worker_command(command_id)
        if row is None:
            raise invalid_argument("Worker command was not found.", command_id=command_id)
        payload = _worker_command_row_to_tool(
            row,
            include_result=include_result,
            max_result_chars=max_result_chars,
        )
        payload["ok"] = True
        payload["pollRecommended"] = payload.get("status") not in {"completed", "failed", "cancelled", "canceled"}
        payload["recommendedPollAfterSeconds"] = 2 if payload["pollRecommended"] else 0
        payload["nextRecommendedAction"] = "poll_worker_command" if payload["pollRecommended"] else "none"
        elapsed_ms = int((time.monotonic() - started) * 1000)
        payload["elapsedMs"] = elapsed_ms
        if elapsed_ms > 2000:
            payload["slowStatusEndpoint"] = {
                "code": "slow_status_endpoint",
                "elapsedMs": elapsed_ms,
                "toolName": "codex_get_worker_command_status",
            }
            LOG.warning(
                "slow status endpoint name=codex_get_worker_command_status elapsed_ms=%d command_id=%s",
                elapsed_ms,
                command_id,
            )
        return payload


def _worker_row_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    staleness = _staleness_seconds(str(row.get("last_heartbeat_at") or ""))
    status = row.get("status")
    effective_status = "stale" if status == "running" and staleness is not None and int(staleness) >= 120 else status
    return {
        "workerId": row.get("worker_id"),
        "role": row.get("role"),
        "status": status,
        "effectiveStatus": effective_status,
        "pid": row.get("pid"),
        "hostname": row.get("hostname"),
        "configFingerprint": row.get("config_fingerprint"),
        "startedAt": row.get("started_at"),
        "lastHeartbeatAt": row.get("last_heartbeat_at"),
        "activeOperationCount": row.get("active_operation_count") or 0,
        "activeTurnCount": row.get("active_turn_count") or 0,
        "appServerGeneration": row.get("app_server_generation"),
        "lastError": row.get("last_error"),
        "stalenessSeconds": staleness,
    }


def _worker_command_row_to_tool(
    row: dict[str, Any],
    *,
    include_result: bool = True,
    max_result_chars: int = 12000,
) -> dict[str, Any]:
    request = _json_dict(row.get("request_json"))
    result_payload = _bounded_worker_command_result(
        row.get("result_json"),
        include_result=include_result,
        max_result_chars=max_result_chars,
    )
    return {
        "commandId": row.get("command_id"),
        "commandType": row.get("command_type"),
        "status": row.get("status"),
        "request": _compact_worker_payload(request),
        "result": result_payload["result"],
        "resultAvailable": result_payload["resultAvailable"],
        "resultIncluded": result_payload["resultIncluded"],
        "resultTruncated": result_payload["resultTruncated"],
        "resultStoredChars": result_payload["resultStoredChars"],
        "resultWarning": result_payload["resultWarning"],
        "workerId": row.get("worker_id"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "completedAt": row.get("completed_at"),
        "lastError": row.get("last_error"),
    }


def _operation_scheduling_row_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    slot_claim = _json_dict(row.get("slot_claim_json"))
    if not slot_claim and row.get("queue_status") in {"scheduled", "running"} and _operation_consumes_turn_slot(row, _json_dict(row.get("request_json"))):
        slot_claim = {
            "claimed": True,
            "workerId": row.get("worker_id"),
            "threadId": row.get("thread_id"),
            "turnId": row.get("turn_id"),
            "claimedAt": row.get("scheduled_at"),
            "source": "derived_from_running_operation",
        }
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
        "slotClaim": slot_claim,
        "workerId": row.get("worker_id"),
        "scheduledAt": row.get("scheduled_at"),
        "threadId": row.get("thread_id"),
        "turnId": row.get("turn_id"),
        "projectId": row.get("project_id"),
        "cwdKey": path_key(row.get("cwd")) if row.get("cwd") else None,
        "updatedAt": row.get("updated_at"),
    }


def _queue_entry_consumes_turn_slot(entry: dict[str, Any]) -> bool:
    operation_type = str(entry.get("operationType") or "")
    if operation_type == "steer_turn":
        return False
    if operation_type == "fork_thread" and not entry.get("turnId") and not entry.get("slotClaim", {}).get("claimed"):
        return False
    return True


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


def _bounded_worker_command_result(
    value: Any,
    *,
    include_result: bool,
    max_result_chars: int,
) -> dict[str, Any]:
    raw = str(value or "")
    available = bool(raw)
    base = {
        "result": None,
        "resultAvailable": available,
        "resultIncluded": False,
        "resultTruncated": False,
        "resultStoredChars": len(raw) if available else 0,
        "resultWarning": None,
    }
    if not available:
        return base
    if not include_result:
        base["resultWarning"] = "result_omitted_by_request"
        return base
    if max_result_chars <= 0:
        base["resultTruncated"] = True
        base["resultWarning"] = "max_result_chars_zero"
        return base
    if len(raw) > max_result_chars:
        base["resultTruncated"] = True
        base["resultWarning"] = "stored_result_exceeds_max_result_chars"
        base["result"] = {
            "truncated": True,
            "storedChars": len(raw),
            "maxResultChars": max_result_chars,
        }
        return base
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        base["resultWarning"] = "invalid_result_json"
        base["result"] = {"available": True, "parseError": "invalid_result_json"}
        base["resultIncluded"] = True
        return base
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}
    compacted = _compact_worker_payload(parsed)
    rendered = json.dumps(compacted, ensure_ascii=False, default=str)
    if len(rendered) > max_result_chars:
        base["resultTruncated"] = True
        base["resultWarning"] = "compacted_result_exceeds_max_result_chars"
        base["result"] = {
            "truncated": True,
            "storedChars": len(raw),
            "compactedChars": len(rendered),
            "maxResultChars": max_result_chars,
        }
        return base
    base["result"] = compacted
    base["resultIncluded"] = True
    return base


def _has_live_worker(workers: list[dict[str, Any]]) -> bool:
    for worker in workers:
        if worker.get("role") == "worker" and worker.get("effectiveStatus") == "running":
            return True
    return False


def _queue_entry_should_be_visible(entry: dict[str, Any], storage: Any) -> bool:
    if entry.get("operationStatus") in OPERATION_TERMINAL_STATUSES:
        return False
    if entry.get("queueStatus") not in {"queued", "scheduled", "running"}:
        return False
    turn_id = _optional_string(entry.get("turnId") or entry.get("turn_id"))
    if not turn_id:
        return True
    tracked = storage.get_tracked_turn(turn_id)
    if tracked is not None and str(tracked.get("status") or "") not in TURN_ACTIVE_STATUSES:
        return False
    return True


def _compact_worker_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(payload)
    return _strip_worker_raw_results(redacted)


def _strip_worker_raw_results(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lower = key_text.lower()
            if key_text == "appServerResult":
                cleaned[key_text] = {"present": True, "redacted": True}
                continue
            if lower in {"path", "transcriptpath", "sessionpath"} and isinstance(item, str):
                cleaned[key_text] = "[redacted-path]"
                continue
            cleaned[key_text] = _strip_worker_raw_results(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_worker_raw_results(item) for item in value]
    return value


def _project_concurrency_key(row: dict[str, Any]) -> str:
    if row.get("project_id"):
        return str(row["project_id"])
    if row.get("cwd"):
        return "cwd:" + path_key(row.get("cwd"))
    return "unknown"


def _operation_consumes_turn_slot(row: dict[str, Any], request: dict[str, Any]) -> bool:
    operation_type = str(row.get("operation_type") or "")
    if operation_type == "steer_turn":
        return False
    if operation_type == "fork_thread" and not _optional_string(request.get("message")):
        return False
    return True


def _concurrency_limits_to_tool(config: Any) -> dict[str, Any]:
    return {
        "maxActiveTurnsGlobal": config.max_active_turns_global,
        "maxActiveTurnsPerProject": config.max_active_turns_per_project,
        "maxActiveTurnsPerAgent": config.max_active_turns_per_agent,
        "maxActiveTurnsPerThread": config.max_active_turns_per_thread,
        "maxActiveWriteTurnsPerProject": config.max_active_write_turns_per_project,
        "maxAppServerPendingRequests": config.max_app_server_pending_requests,
    }
