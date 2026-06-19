from __future__ import annotations

import json
from typing import Any

from .statuses import OPERATION_ACTIVE_STATUSES, OPERATION_TERMINAL_STATUSES, TURN_ACTIVE_STATUSES


ACTIVE_QUEUE_STATUSES = {"scheduled", "running"}
ACTIVE_TURN_CANDIDATE_STATUSES = set(TURN_ACTIVE_STATUSES) | {"ready", "starting"}


def worker_active_turns_snapshot(storage: Any, *, limit: int = 100) -> dict[str, Any]:
    rows = storage.connection.execute(
        f"""
        SELECT ops.operation_id, ops.operation_type, ops.status AS operation_status,
               ops.thread_id, ops.turn_id, ops.project_id, ops.cwd, ops.request_json,
               ops.updated_at AS operation_updated_at,
               sched.queue_status, sched.agent_id, sched.worker_id,
               turns.status AS turn_status, turns.updated_at AS turn_updated_at,
               turns.process_generation AS process_generation
          FROM codex_operations AS ops
          LEFT JOIN codex_operation_scheduling AS sched ON sched.operation_id = ops.operation_id
          LEFT JOIN tracked_turns AS turns ON turns.turn_id = ops.turn_id
         WHERE sched.queue_status IN ('scheduled', 'running')
            OR ops.status IN ({_placeholders(len(OPERATION_ACTIVE_STATUSES))})
            OR turns.status IN ({_placeholders(len(ACTIVE_TURN_CANDIDATE_STATUSES))})
         ORDER BY COALESCE(turns.updated_at, ops.updated_at) DESC
         LIMIT ?
        """,
        tuple(OPERATION_ACTIVE_STATUSES) + tuple(ACTIVE_TURN_CANDIDATE_STATUSES) + (limit,),
    ).fetchall()
    active: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen_turn_ids: set[str] = set()
    for raw in rows:
        row = dict(raw)
        item, reason = _active_turn_from_candidate(row)
        if item is None:
            if reason:
                excluded.append(
                    {
                        "operationId": row.get("operation_id"),
                        "turnId": row.get("turn_id"),
                        "operationStatus": row.get("operation_status"),
                        "turnStatus": row.get("turn_status"),
                        "queueStatus": row.get("queue_status"),
                        "reason": reason,
                    }
                )
            continue
        turn_id = str(item.get("turnId") or "")
        if turn_id and turn_id in seen_turn_ids:
            continue
        if turn_id:
            seen_turn_ids.add(turn_id)
        active.append(item)
    return {
        "activeTurns": active,
        "activeTurnCount": len(active),
        "staleActiveRecordsExcluded": len(excluded),
        "excludedRecords": excluded[:20],
    }


def _active_turn_from_candidate(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    operation_status = str(row.get("operation_status") or "")
    turn_status = str(row.get("turn_status") or "")
    queue_status = str(row.get("queue_status") or "")
    operation_type = str(row.get("operation_type") or "")
    turn_id = _optional_string(row.get("turn_id"))
    request = _json_dict(row.get("request_json"))
    if operation_status in OPERATION_TERMINAL_STATUSES:
        return None, "terminal_operation"
    if operation_type == "steer_turn":
        return None, "auxiliary_operation"
    if operation_type == "fork_thread" and not _optional_string(request.get("message")):
        return None, "fork_only_operation"
    if not turn_id:
        return None, "no_turn_id"
    if turn_status:
        if turn_status not in TURN_ACTIVE_STATUSES:
            return None, f"non_active_turn_status:{turn_status}"
        status = turn_status
    elif queue_status not in ACTIVE_QUEUE_STATUSES and operation_status not in OPERATION_ACTIVE_STATUSES:
        return None, "not_active"
    else:
        status = operation_status or queue_status
    return (
        {
            "operationId": row.get("operation_id"),
            "operationType": operation_type,
            "status": status,
            "operationStatus": operation_status,
            "queueStatus": queue_status or None,
            "threadId": row.get("thread_id"),
            "turnId": turn_id,
            "projectId": row.get("project_id"),
            "agentId": row.get("agent_id"),
            "workerId": row.get("worker_id"),
            "updatedAt": row.get("turn_updated_at") or row.get("operation_updated_at"),
            "processGeneration": row.get("process_generation"),
            "source": "worker_state",
        },
        None,
    )


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))
