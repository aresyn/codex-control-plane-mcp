from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import ServerConfig, path_key
from .logging_utils import get_logger
from .statuses import OPERATION_STARTABLE_STATUSES, OPERATION_TERMINAL_STATUSES, TURN_ACTIVE_STATUSES
from .tools import ToolService, _future_iso, _now_iso, _operation_request_from_row, _optional_string, redact_payload


LOG = get_logger("worker")
RESOURCE_LOCK_TTL_SECONDS = 6 * 60 * 60
SLOT_STARTING_STATUSES = ("starting_app_server", "starting_thread", "starting_review", "starting_turn")


class CentralWorker:
    def __init__(self, service: ToolService, *, observe: bool = False, interval_seconds: float = 1.0) -> None:
        self.service = service
        self.observe = observe
        self.interval_seconds = interval_seconds
        self.worker_id = service._worker_owner
        self.hostname = socket.gethostname()
        self.started_at = _now_iso()
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        role = "observe" if self.observe else "worker"
        self.service.config.execution_mode = role
        self._heartbeat(status="running", role=role, last_error=None)
        LOG.info("central worker started worker_id=%s role=%s", self.worker_id, role)
        try:
            while not self._stop.is_set():
                last_error: str | None = None
                try:
                    self._heartbeat(status="running", role=role, last_error=None)
                    if not self.observe:
                        await self._process_worker_commands()
                        await self._schedule_startable_operations()
                        self._cleanup_terminal_locks()
                except Exception as exc:  # pragma: no cover - defensive loop guard
                    last_error = str(exc)
                    LOG.exception("central worker loop failed")
                    self._heartbeat(status="degraded", role=role, last_error=last_error)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._heartbeat(status="stopped", role=role, last_error=None)
            await self.service.close()
            LOG.info("central worker stopped worker_id=%s", self.worker_id)

    def stop(self) -> None:
        self._stop.set()

    def _heartbeat(self, *, status: str, role: str, last_error: str | None) -> None:
        app_generation = self.service._app_server.process_generation if self.service._app_server is not None else None
        active_tasks = [task for task in self.service._operation_tasks.values() if not task.done()]
        self.service.storage.upsert_worker(
            worker_id=self.worker_id,
            role=role,
            status=status,
            pid=os.getpid(),
            hostname=self.hostname,
            config_fingerprint=self.service._config_fingerprint,
            started_at=self.started_at,
            last_heartbeat_at=_now_iso(),
            active_operation_count=len(active_tasks),
            active_turn_count=self._active_turn_count(),
            app_server_generation=app_generation,
            last_error=last_error,
        )

    def _active_turn_count(self) -> int:
        rows = self.service.storage.connection.execute(
            f"""
            SELECT COUNT(*) AS count
              FROM codex_operations AS ops
              JOIN tracked_turns AS turns ON turns.turn_id = ops.turn_id
             WHERE turns.status IN ({','.join('?' for _ in TURN_ACTIVE_STATUSES)})
            """,
            tuple(TURN_ACTIVE_STATUSES),
        ).fetchone()
        return int(rows["count"] if rows is not None else 0)

    async def _process_worker_commands(self) -> None:
        for command in self.service.storage.list_worker_commands(status="queued", limit=10):
            command_id = str(command.get("command_id") or "")
            command_type = str(command.get("command_type") or "")
            request = _json_dict(command.get("request_json"))
            args = request.get("arguments") if isinstance(request.get("arguments"), dict) else {}
            self.service.storage.update_worker_command(
                command_id,
                status="running",
                worker_id=self.worker_id,
                updated_at=_now_iso(),
            )
            try:
                result = await self.service.call(command_type, args)
                status = "completed" if result.get("ok", True) and not result.get("error") else "failed"
                self.service.storage.update_worker_command(
                    command_id,
                    status=status,
                    result_json=json.dumps(redact_payload(result), ensure_ascii=False),
                    worker_id=self.worker_id,
                    updated_at=_now_iso(),
                    completed_at=_now_iso(),
                    last_error=None if status == "completed" else str((result.get("error") or {}).get("message") or "Worker command failed."),
                )
            except Exception as exc:
                LOG.exception("worker command failed command_id=%s type=%s", command_id, command_type)
                self.service.storage.update_worker_command(
                    command_id,
                    status="failed",
                    result_json=json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
                    worker_id=self.worker_id,
                    updated_at=_now_iso(),
                    completed_at=_now_iso(),
                    last_error=str(exc),
                )

    async def _schedule_startable_operations(self) -> None:
        if self._app_server_backpressure():
            self._mark_waiting_operations("app_server_backpressure")
            return

        operations = self.service.storage.list_startable_operations(
            now=_now_iso(),
            limit=50,
            worker_config_fingerprint=self.service._config_fingerprint,
            allow_cross_config_recovery=self.service._allow_cross_config_recovery,
        )
        for operation in operations:
            operation_id = str(operation.get("operation_id") or "")
            if not operation_id:
                continue
            task = self.service._operation_tasks.get(operation_id)
            if task is not None and not task.done():
                continue
            self.service._ensure_operation_scheduling(
                operation,
                queued_reason="waiting_for_worker",
            )
            decision = self._can_start_operation(operation)
            if not decision["allowed"]:
                self.service.storage.update_operation_scheduling(
                    operation_id,
                    queue_status="queued",
                    queued_reason=str(decision["reason"]),
                    updated_at=_now_iso(),
                )
                continue
            self.service.storage.replace_resource_locks_for_operation(
                operation_id=operation_id,
                locks=decision["locks"],
            )
            self.service.storage.update_operation_scheduling(
                operation_id,
                queue_status="scheduled",
                queued_reason=None,
                updated_at=_now_iso(),
                worker_id=self.worker_id,
                slot_claim=decision["slotClaim"],
                scheduled_at=_now_iso(),
            )
            self.service._schedule_operation_if_needed(operation)

    def _app_server_backpressure(self) -> bool:
        if self.service._app_server is None:
            return False
        snapshot = self.service._app_server.status_snapshot(include_recent_events=False)
        pending = int(snapshot.get("pendingRequests") or 0)
        return pending >= self.service.config.max_app_server_pending_requests

    def _mark_waiting_operations(self, reason: str) -> None:
        for operation in self.service.storage.list_startable_operations(
            now=_now_iso(),
            limit=50,
            worker_config_fingerprint=self.service._config_fingerprint,
            allow_cross_config_recovery=self.service._allow_cross_config_recovery,
        ):
            operation_id = str(operation.get("operation_id") or "")
            if operation_id:
                self.service._ensure_operation_scheduling(operation, queued_reason=reason)
                self.service.storage.update_operation_scheduling(
                    operation_id,
                    queue_status="queued",
                    queued_reason=reason,
                    updated_at=_now_iso(),
                )

    def _can_start_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        active = self._active_operations()
        request = _operation_request_from_row(operation)
        scheduling = self.service.storage.get_operation_scheduling(str(operation["operation_id"])) or {}
        agent_id = _optional_string(scheduling.get("agent_id")) or "unknown"
        project_key = _project_key(operation)
        thread_id = _operation_thread_id(operation, request)

        if len(active) >= self.service.config.max_active_turns_global:
            return _deny("global_slot_limit")
        if _count_by(active, lambda row: _project_key(row) == project_key) >= self.service.config.max_active_turns_per_project:
            return _deny("project_slot_limit")
        if _count_by(active, lambda row: str(row.get("agent_id") or "unknown") == agent_id) >= self.service.config.max_active_turns_per_agent:
            return _deny("agent_slot_limit")
        if thread_id and _count_by(active, lambda row: _operation_thread_id(row, _json_dict(row.get("request_json"))) == thread_id) >= self.service.config.max_active_turns_per_thread:
            return _deny("thread_slot_limit")

        locks = self._locks_for_operation(operation, request, scheduling)
        existing_locks = self.service.storage.list_resource_locks(limit=500)
        existing_keys = {str(row.get("lock_key") or "") for row in existing_locks if row.get("operation_id") != operation.get("operation_id")}
        if any(lock["lock_key"] in existing_keys for lock in locks):
            return _deny("resource_lock_conflict")

        broad_write_lock = next((lock for lock in locks if lock["lock_key"].startswith("project:") and lock["lock_key"].endswith(":write")), None)
        if broad_write_lock is not None:
            same_project_writes = [
                row
                for row in existing_locks
                if str(row.get("lock_key") or "").startswith(f"project:{project_key}:")
                and str(row.get("lock_key") or "").endswith(":write")
                and row.get("operation_id") != operation.get("operation_id")
            ]
            if len(same_project_writes) >= self.service.config.max_active_write_turns_per_project:
                return _deny("write_project_slot_limit")

        return {
            "allowed": True,
            "reason": None,
            "locks": locks,
            "slotClaim": {
                "claimed": True,
                "workerId": self.worker_id,
                "projectKey": project_key,
                "agentId": agent_id,
                "threadId": thread_id,
                "lockKeys": [lock["lock_key"] for lock in locks],
                "claimedAt": _now_iso(),
            },
        }

    def _active_operations(self) -> list[dict[str, Any]]:
        starting = SLOT_STARTING_STATUSES
        turn_active = tuple(TURN_ACTIVE_STATUSES)
        rows = self.service.storage.connection.execute(
            f"""
            SELECT ops.*, sched.agent_id, sched.resource_keys_json
              FROM codex_operations AS ops
              LEFT JOIN codex_operation_scheduling AS sched ON sched.operation_id = ops.operation_id
              LEFT JOIN tracked_turns AS turns ON turns.turn_id = ops.turn_id
             WHERE ops.status IN ({','.join('?' for _ in starting)})
                OR turns.status IN ({','.join('?' for _ in turn_active)})
                OR sched.queue_status = 'scheduled'
             ORDER BY ops.updated_at DESC
            """,
            starting + turn_active,
        ).fetchall()
        return [dict(row) for row in rows]

    def _locks_for_operation(
        self,
        operation: dict[str, Any],
        request: dict[str, Any],
        scheduling: dict[str, Any],
    ) -> list[dict[str, Any]]:
        created_at = _now_iso()
        expires_at = _future_iso(RESOURCE_LOCK_TTL_SECONDS)
        project_key = _project_key(operation)
        thread_id = _operation_thread_id(operation, request)
        project_id = _optional_string(operation.get("project_id"))
        operation_id = str(operation["operation_id"])
        locks: list[dict[str, Any]] = []
        if thread_id:
            locks.append(
                _lock(
                    f"thread:{thread_id}:active-turn",
                    operation_id=operation_id,
                    thread_id=thread_id,
                    project_id=project_id,
                    worker_id=self.worker_id,
                    created_at=created_at,
                    expires_at=expires_at,
                )
            )
        if _operation_is_write_turn(operation, request):
            resource_keys = _json_list(scheduling.get("resource_keys_json"))
            if resource_keys:
                for key in resource_keys:
                    locks.append(
                        _lock(
                            f"resource:{project_key}:{path_key(str(key))}:write",
                            operation_id=operation_id,
                            thread_id=thread_id,
                            project_id=project_id,
                            worker_id=self.worker_id,
                            created_at=created_at,
                            expires_at=expires_at,
                        )
                    )
            else:
                locks.append(
                    _lock(
                        f"project:{project_key}:write",
                        operation_id=operation_id,
                        thread_id=thread_id,
                        project_id=project_id,
                        worker_id=self.worker_id,
                        created_at=created_at,
                        expires_at=expires_at,
                    )
                )
        return locks

    def _cleanup_terminal_locks(self) -> None:
        now = _now_iso()
        self.service.storage.cleanup_resource_locks(now=now)
        for row in self.service.storage.list_resource_locks(limit=500):
            operation_id = str(row.get("operation_id") or "")
            operation = self.service.storage.get_operation(operation_id) if operation_id else None
            if operation is None or str(operation.get("status") or "") in OPERATION_TERMINAL_STATUSES:
                self.service.storage.release_resource_locks_for_operation(operation_id)


def _deny(reason: str) -> dict[str, Any]:
    return {"allowed": False, "reason": reason, "locks": [], "slotClaim": {"claimed": False, "reason": reason}}


def _lock(
    lock_key: str,
    *,
    operation_id: str,
    thread_id: str | None,
    project_id: str | None,
    worker_id: str,
    created_at: str,
    expires_at: str,
) -> dict[str, Any]:
    return {
        "lock_key": lock_key,
        "operation_id": operation_id,
        "thread_id": thread_id,
        "project_id": project_id,
        "lock_mode": "exclusive",
        "worker_id": worker_id,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def _project_key(operation: dict[str, Any]) -> str:
    if operation.get("project_id"):
        return str(operation["project_id"])
    if operation.get("cwd"):
        return path_key(operation.get("cwd"))
    return "unknown"


def _operation_thread_id(operation: dict[str, Any], request: dict[str, Any]) -> str | None:
    return (
        _optional_string(operation.get("thread_id"))
        or _optional_string(request.get("_resolved_thread_id"))
        or _optional_string(request.get("thread_id"))
        or _optional_string(request.get("chat_id"))
    )


def _operation_is_write_turn(operation: dict[str, Any], request: dict[str, Any]) -> bool:
    operation_type = str(operation.get("operation_type") or "")
    if operation_type == "fork_thread" and not _optional_string(request.get("message")):
        return False
    sandbox = str(request.get("sandbox") or "").strip()
    return sandbox in {"workspace-write", "danger-full-access"}


def _count_by(rows: list[dict[str, Any]], predicate: Any) -> int:
    return sum(1 for row in rows if predicate(row))


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


async def async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Codex Control Plane MCP central worker.")
    parser.add_argument("--observe", action="store_true", help="Heartbeat only. Do not acquire leases or start app-server work.")
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)

    config = ServerConfig.load(Path(__file__).resolve().parents[1])
    config.execution_mode = "observe" if args.observe else "worker"
    service = ToolService(config)
    worker = CentralWorker(service, observe=args.observe, interval_seconds=max(0.25, args.interval_seconds))
    try:
        await worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))
