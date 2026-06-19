from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from openclaw_codex_mcp.catalog import project_id_for_path
from openclaw_codex_mcp.tools import ToolService
from openclaw_codex_mcp.worker import CentralWorker

from .helpers import FakeAppServer, _search_service_config


class CentralWorkerArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_mode_submit_and_status_do_not_execute_operation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            config.execution_mode = "client"
            service = ToolService(config)
            fake = FakeAppServer(service.storage, first_message=None)
            service._app_server = fake  # type: ignore[assignment]
            try:
                result = await service.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id_for_path(str(project)),
                        "message": "client mode should queue only",
                        "client_request_id": "client-mode-queue-only",
                        "agent_id": "codex-dev",
                    },
                )
                status = await service.call("codex_get_operation_status", {"operation_id": result["operationId"]})
            finally:
                await service.close()

            self.assertEqual("queued", result["status"])
            self.assertEqual("waiting_for_worker", result["queueState"]["queuedReason"])
            self.assertEqual("queued", status["status"])
            self.assertEqual([], fake.turn_start_calls)

    async def test_client_mode_compatibility_start_chat_delegates_to_durable_queue(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            config.execution_mode = "client"
            service = ToolService(config)
            fake = FakeAppServer(service.storage, first_message=None)
            service._app_server = fake  # type: ignore[assignment]
            try:
                result = await service.call(
                    "codex_start_chat",
                    {
                        "project_id": project_id_for_path(str(project)),
                        "message": "compatibility client mode should not start app-server",
                        "client_request_id": "compat-client-start-chat",
                    },
                )
                app_status = service.codex_get_app_server_status({})
            finally:
                await service.close()

            self.assertTrue(result["compatibilityDelegated"])
            self.assertEqual("compatibility_delegated_to_durable_queue", result["operationSource"])
            self.assertEqual("queued", result["status"])
            self.assertEqual("waiting_for_worker", result["queueState"]["queuedReason"])
            self.assertEqual([], fake.thread_start_calls)
            self.assertEqual([], fake.turn_start_calls)
            self.assertEqual("worker_managed", app_status["scope"])
            self.assertFalse(app_status["running"])
            self.assertIn("ignoredLocalProcess", app_status)

    async def test_worker_respects_global_active_turn_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            state_db = root / ".codex" / "state_5.sqlite"
            project_id = project_id_for_path(str(project))
            client_config = _search_service_config(root, state_db)
            client_config.execution_mode = "client"
            client = ToolService(client_config)
            worker_config = _search_service_config(root, state_db)
            worker_config.execution_mode = "worker"
            worker_config.max_active_turns_global = 2
            worker_config.max_active_turns_per_project = 10
            worker_service = ToolService(worker_config)
            fake = FakeAppServer(worker_service.storage, first_message=None)
            worker_service._app_server = fake  # type: ignore[assignment]
            worker = CentralWorker(worker_service)
            try:
                operation_ids: list[str] = []
                for index in range(5):
                    result = await client.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": f"global limit task {index}",
                            "client_request_id": f"global-limit-{index}",
                            "agent_id": "codex-dev",
                            "sandbox": "read-only",
                        },
                    )
                    operation_ids.append(result["operationId"])

                await worker._schedule_startable_operations()
                await _wait_for_turn_starts(fake, expected=2)
                queue = await worker_service.call("codex_get_queue_status", {"limit": 10})
            finally:
                await client.close()
                await worker_service.close()

            self.assertEqual(2, len(fake.turn_start_calls))
            queued_reasons = {
                item["operationId"]: item.get("queuedReason")
                for item in queue["operations"]
                if item["operationId"] in operation_ids
            }
            self.assertEqual(5, len(queued_reasons))
            self.assertEqual(3, list(queued_reasons.values()).count("global_slot_limit"))

    async def test_stale_running_operation_without_active_tracked_turn_does_not_consume_slot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            state_db = root / ".codex" / "state_5.sqlite"
            project_id = project_id_for_path(str(project))
            client_config = _search_service_config(root, state_db)
            client_config.execution_mode = "client"
            client = ToolService(client_config)
            worker_config = _search_service_config(root, state_db)
            worker_config.execution_mode = "worker"
            worker_config.max_active_turns_global = 1
            worker_config.max_active_turns_per_project = 10
            worker_service = ToolService(worker_config)
            fake = FakeAppServer(worker_service.storage, first_message=None)
            worker_service._app_server = fake  # type: ignore[assignment]
            worker = CentralWorker(worker_service)
            try:
                stale = await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "historical stale operation",
                        "client_request_id": "stale-running-slot",
                        "agent_id": "codex-dev",
                        "sandbox": "read-only",
                    },
                )
                client.storage.update_operation(
                    stale["operationId"],
                    status="running",
                    phase="running",
                    thread_id="thread-stale",
                    turn_id="turn-stale",
                    updated_at="2026-05-25T00:00:00+00:00",
                )
                fresh = await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "fresh queued operation",
                        "client_request_id": "fresh-after-stale",
                        "agent_id": "codex-dev",
                        "sandbox": "read-only",
                    },
                )

                await worker._schedule_startable_operations()
                await _wait_for_turn_starts(fake, expected=1)
                status = await worker_service.call("codex_get_operation_status", {"operation_id": fresh["operationId"]})
                concurrency = await worker_service.call("codex_get_concurrency_status", {"include_locks": False})
            finally:
                await client.close()
                await worker_service.close()

            self.assertEqual(1, len(fake.turn_start_calls))
            self.assertEqual("running", status["status"])
            self.assertEqual(1, concurrency["activeTurnCount"])
            self.assertEqual(fresh["operationId"], concurrency["activeOperations"][0]["operationId"])

    async def test_steer_turn_bypasses_turn_slot_limits_and_does_not_double_count_active_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            state_db = root / ".codex" / "state_5.sqlite"
            project_id = project_id_for_path(str(project))
            client_config = _search_service_config(root, state_db)
            client_config.execution_mode = "client"
            client = ToolService(client_config)
            worker_config = _search_service_config(root, state_db)
            worker_config.execution_mode = "worker"
            worker_config.max_active_turns_global = 1
            worker_config.max_active_turns_per_project = 1
            worker_config.max_active_turns_per_agent = 1
            worker_service = ToolService(worker_config)
            fake = FakeAppServer(worker_service.storage, first_message=None)
            worker_service._app_server = fake  # type: ignore[assignment]
            worker = CentralWorker(worker_service)
            try:
                started = await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "long running task",
                        "client_request_id": "slot-limit-running-task",
                        "agent_id": "codex-dev",
                        "sandbox": "read-only",
                    },
                )
                await worker._schedule_startable_operations()
                await _wait_for_turn_starts(fake, expected=1)
                running = await worker_service.call("codex_get_operation_status", {"operation_id": started["operationId"]})

                steer = await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "steer_turn",
                        "thread_id": running["threadId"],
                        "expected_turn_id": running["turnId"],
                        "message": "steer the already running task",
                        "client_request_id": "slot-limit-steer-task",
                        "agent_id": "codex-dev",
                    },
                )
                await worker._schedule_startable_operations()
                await _wait_for_steer_calls(fake, expected=1)
                steer_status = await worker_service.call("codex_get_operation_status", {"operation_id": steer["operationId"]})
                concurrency = await worker_service.call("codex_get_concurrency_status", {"include_locks": False})
            finally:
                await client.close()
                await worker_service.close()

            self.assertEqual(1, len(fake.turn_start_calls))
            self.assertEqual(1, len(fake.turn_steer_calls))
            self.assertEqual("running", steer_status["status"])
            self.assertTrue(steer_status["steerState"]["accepted"])
            self.assertEqual("turn-fake", fake.turn_steer_calls[0]["expected_turn_id"])
            self.assertEqual(1, concurrency["activeTurnCount"])
            self.assertEqual(1, concurrency["counts"]["global"])

    async def test_terminal_scheduling_rows_do_not_appear_in_active_queue(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            state_db = root / ".codex" / "state_5.sqlite"
            project_id = project_id_for_path(str(project))
            client_config = _search_service_config(root, state_db)
            client_config.execution_mode = "client"
            client = ToolService(client_config)
            worker_config = _search_service_config(root, state_db)
            worker_config.execution_mode = "worker"
            worker_service = ToolService(worker_config)
            worker = CentralWorker(worker_service)
            try:
                result = await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "terminal scheduling audit",
                        "client_request_id": "terminal-scheduling-audit",
                        "agent_id": "codex-dev",
                    },
                )
                operation_id = result["operationId"]
                client.storage.update_operation(
                    operation_id,
                    status="unknown_after_app_server_exit",
                    phase="unknown_after_app_server_exit",
                    completed_at="2026-05-25T00:00:00+00:00",
                    updated_at="2026-05-25T00:00:00+00:00",
                )
                client.storage.update_operation_scheduling(
                    operation_id,
                    queue_status="running",
                    queued_reason=None,
                    updated_at="2026-05-25T00:00:00+00:00",
                )

                active_queue = await worker_service.call("codex_get_queue_status", {"limit": 10})
                terminal_queue = await worker_service.call("codex_get_queue_status", {"limit": 10, "include_terminal": True})
                worker._cleanup_terminal_scheduling()
                cleaned = worker_service.storage.get_operation_scheduling(operation_id)
            finally:
                await client.close()
                await worker_service.close()

            self.assertEqual(0, active_queue["count"])
            self.assertEqual(1, terminal_queue["count"])
            self.assertEqual("unknown_after_app_server_exit", cleaned["queue_status"])

    async def test_worker_managed_app_status_excludes_stale_active_records(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / ".codex" / "state_5.sqlite"
            config = _search_service_config(root, state_db)
            config.execution_mode = "client"
            service = ToolService(config)
            try:
                now = datetime.now(timezone.utc).isoformat()
                service.storage.upsert_worker(
                    worker_id="worker-live",
                    role="worker",
                    status="running",
                    pid=123,
                    hostname="host",
                    config_fingerprint=service._config_fingerprint,
                    started_at=now,
                    last_heartbeat_at=now,
                    app_server_generation=1,
                )
                _insert_operation_with_scheduling(
                    service,
                    operation_id="op-ready",
                    status="running",
                    turn_id="turn-ready",
                    queue_status="running",
                    now=now,
                )
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-ready",
                        "thread_id": "thread-ready",
                        "chat_id": "thread-ready",
                        "project_id": "project-id",
                        "project_path": str(root),
                        "status": "ready",
                        "started_at": now,
                        "updated_at": now,
                        "completed_at": None,
                        "first_message_at": None,
                        "final_message": None,
                        "last_error": None,
                        "source": "test",
                    }
                )
                _insert_operation_with_scheduling(
                    service,
                    operation_id="op-terminal",
                    status="completed",
                    turn_id="turn-terminal-op",
                    queue_status="running",
                    now=now,
                )
                _insert_operation_with_scheduling(
                    service,
                    operation_id="op-terminal-turn",
                    status="running",
                    turn_id="turn-done",
                    queue_status="running",
                    now=now,
                )
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-done",
                        "thread_id": "thread-done",
                        "chat_id": "thread-done",
                        "project_id": "project-id",
                        "project_path": str(root),
                        "status": "completed",
                        "started_at": now,
                        "updated_at": now,
                        "completed_at": now,
                        "first_message_at": None,
                        "final_message": "done",
                        "last_error": None,
                        "source": "test",
                    }
                )
                _insert_operation_with_scheduling(
                    service,
                    operation_id="op-active",
                    status="running",
                    turn_id="turn-active",
                    queue_status="running",
                    now=now,
                )
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-active",
                        "thread_id": "thread-active",
                        "chat_id": "thread-active",
                        "project_id": "project-id",
                        "project_path": str(root),
                        "status": "running",
                        "started_at": now,
                        "updated_at": now,
                        "completed_at": None,
                        "first_message_at": None,
                        "final_message": None,
                        "last_error": None,
                        "source": "test",
                    }
                )

                app_status = service.codex_get_app_server_status({})
                health = service.codex_health_summary({})
            finally:
                await service.close()

            self.assertEqual(1, app_status["activeTurnCount"])
            self.assertEqual("turn-active", app_status["activeTurns"][0]["turnId"])
            self.assertGreaterEqual(app_status["staleActiveRecordsExcluded"], 3)
            self.assertEqual(1, health["activeWork"]["activeTurnCount"])
            self.assertEqual("turn-active", health["activeWork"]["activeTurns"][0]["turnId"])

    async def test_health_summary_historical_debt_does_not_create_active_work(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / ".codex" / "state_5.sqlite"
            config = _search_service_config(root, state_db)
            config.execution_mode = "client"
            service = ToolService(config)
            try:
                heartbeat_now = datetime.now(timezone.utc).isoformat()
                stale_time = "2026-05-25T00:00:00+00:00"
                service.storage.upsert_worker(
                    worker_id="worker-live",
                    role="worker",
                    status="running",
                    pid=123,
                    hostname="host",
                    config_fingerprint=service._config_fingerprint,
                    started_at=heartbeat_now,
                    last_heartbeat_at=heartbeat_now,
                    app_server_generation=1,
                )
                _insert_operation_with_scheduling(
                    service,
                    operation_id="op-historical-ready",
                    status="running",
                    turn_id="turn-historical-ready",
                    queue_status="running",
                    now=stale_time,
                )
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-historical-ready",
                        "thread_id": "thread-historical-ready",
                        "chat_id": "thread-historical-ready",
                        "project_id": "project-id",
                        "project_path": str(root),
                        "status": "ready",
                        "started_at": stale_time,
                        "updated_at": stale_time,
                        "completed_at": None,
                        "first_message_at": None,
                        "final_message": None,
                        "last_error": None,
                        "source": "test",
                    }
                )
                health = service.codex_health_summary({"stale_after_minutes": 1})
            finally:
                await service.close()

            self.assertEqual(0, health["activeWork"]["activeTurnCount"])
            self.assertFalse(health["pollRecommended"])
            self.assertFalse(health["historicalDebt"]["blocksReadiness"])

    async def test_worker_status_marks_stale_workers_and_redacts_command_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / ".codex" / "state_5.sqlite"
            service = ToolService(_search_service_config(root, state_db))
            try:
                service.storage.upsert_worker(
                    worker_id="worker-stale",
                    role="worker",
                    status="running",
                    pid=123,
                    hostname="host",
                    config_fingerprint="fp",
                    started_at="2026-05-25T00:00:00+00:00",
                    last_heartbeat_at="2026-05-25T00:00:00+00:00",
                )
                service.storage.create_worker_command(
                    command_id="cmd-path",
                    command_type="archive_thread",
                    status="completed",
                    request={"thread_id": "thread"},
                    created_at="2026-05-25T00:00:00+00:00",
                    updated_at="2026-05-25T00:00:00+00:00",
                )
                service.storage.update_worker_command(
                    "cmd-path",
                    result_json=json.dumps(
                        {
                            "appServerResult": {
                                "thread": {"path": "C:\\Users\\shan\\.codex\\sessions\\secret.jsonl"},
                            }
                        },
                        ensure_ascii=False,
                    ),
                    updated_at="2026-05-25T00:00:01+00:00",
                    completed_at="2026-05-25T00:00:01+00:00",
                )
                status = await service.call("codex_get_worker_status", {"include_recent_commands": True})
            finally:
                await service.close()

        rendered = json.dumps(status, ensure_ascii=False)
        self.assertEqual("stale", status["workers"][0]["effectiveStatus"])
        self.assertNotIn("secret.jsonl", rendered)
        self.assertTrue(status["recentCommands"][0]["resultAvailable"])
        self.assertFalse(status["recentCommands"][0]["resultIncluded"])
        self.assertIsNone(status["recentCommands"][0]["result"])

    async def test_client_app_server_status_keeps_worker_derived_active_turns_when_worker_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / ".codex" / "state_5.sqlite"
            config = _search_service_config(root, state_db)
            config.execution_mode = "client"
            service = ToolService(config)
            try:
                now = "2026-05-25T00:00:00+00:00"
                service.storage.upsert_worker(
                    worker_id="worker-stale",
                    role="worker",
                    status="running",
                    pid=123,
                    hostname="host",
                    config_fingerprint="fp",
                    started_at=now,
                    last_heartbeat_at=now,
                    active_operation_count=1,
                    active_turn_count=1,
                )
                service.storage.create_operation(
                    {
                        "operation_id": "op-active-stale-worker",
                        "client_request_id": "op-active-stale-worker",
                        "operation_type": "start_chat",
                        "status": "running",
                        "phase": "running",
                        "project_id": "project",
                        "chat_id": "thread-active",
                        "thread_id": "thread-active",
                        "turn_id": "turn-active",
                        "workflow_id": None,
                        "cwd": str(root),
                        "title": None,
                        "request_json": '{"sandbox":"danger-full-access"}',
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-active",
                        "thread_id": "thread-active",
                        "chat_id": "thread-active",
                        "project_id": "project",
                        "project_path": str(root),
                        "status": "running",
                        "started_at": now,
                        "updated_at": now,
                        "completed_at": None,
                        "first_message_at": None,
                        "final_message": None,
                        "last_assistant_message": None,
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                service.storage.upsert_operation_scheduling(
                    operation_id="op-active-stale-worker",
                    agent_id="agent",
                    priority="normal",
                    estimated_cost_class="normal",
                    resource_keys=[],
                    queue_status="running",
                    queued_reason=None,
                    created_at=now,
                    updated_at=now,
                )

                status = await service.call("codex_get_app_server_status", {})
            finally:
                await service.close()

        self.assertEqual(1, status["activeTurnCount"])
        self.assertEqual("turn-active", status["workerDerivedActiveTurns"][0]["turnId"])
        self.assertTrue(status["workerHeartbeatStale"])
        self.assertEqual("unknown_stale_worker", status["appServerLiveState"])

    async def test_worker_command_status_is_bounded_and_can_omit_result(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / ".codex" / "state_5.sqlite"
            service = ToolService(_search_service_config(root, state_db))
            try:
                now = "2026-06-19T00:00:00+00:00"
                service.storage.create_worker_command(
                    command_id="cmd-large",
                    command_type="codex_get_runtime_capabilities",
                    status="completed",
                    request={"toolName": "codex_get_runtime_capabilities", "arguments": {}},
                    created_at=now,
                    updated_at=now,
                )
                service.storage.update_worker_command(
                    "cmd-large",
                    result_json=json.dumps({"ok": True, "payload": "x" * 50000}, ensure_ascii=False),
                    updated_at=now,
                    completed_at=now,
                )
                omitted = await service.call(
                    "codex_get_worker_command_status",
                    {"command_id": "cmd-large", "include_result": False},
                )
                truncated = await service.call(
                    "codex_get_worker_command_status",
                    {"command_id": "cmd-large", "max_result_chars": 1000},
                )
            finally:
                await service.close()

            self.assertEqual("completed", omitted["status"])
            self.assertTrue(omitted["resultAvailable"])
            self.assertFalse(omitted["resultIncluded"])
            self.assertFalse(omitted["pollRecommended"])
            self.assertEqual("result_omitted_by_request", omitted["resultWarning"])
            self.assertTrue(truncated["resultTruncated"])
            self.assertEqual("stored_result_exceeds_max_result_chars", truncated["resultWarning"])
            self.assertFalse(truncated["pollRecommended"])

    async def test_worker_command_status_handles_invalid_result_json(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / ".codex" / "state_5.sqlite"
            service = ToolService(_search_service_config(root, state_db))
            try:
                now = "2026-06-19T00:00:00+00:00"
                service.storage.create_worker_command(
                    command_id="cmd-invalid-json",
                    command_type="codex_get_runtime_capabilities",
                    status="completed",
                    request={"toolName": "codex_get_runtime_capabilities", "arguments": {}},
                    created_at=now,
                    updated_at=now,
                )
                service.storage.update_worker_command(
                    "cmd-invalid-json",
                    result_json="{not-json",
                    updated_at=now,
                    completed_at=now,
                )
                status = await service.call("codex_get_worker_command_status", {"command_id": "cmd-invalid-json"})
            finally:
                await service.close()

            self.assertEqual("completed", status["status"])
            self.assertTrue(status["resultAvailable"])
            self.assertTrue(status["resultIncluded"])
            self.assertEqual("invalid_result_json", status["resultWarning"])
            self.assertEqual("invalid_result_json", status["result"]["parseError"])

    async def test_cleanup_releases_locks_for_non_slot_operations(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / ".codex" / "state_5.sqlite"
            worker_config = _search_service_config(root, state_db)
            worker_config.execution_mode = "worker"
            worker_service = ToolService(worker_config)
            worker = CentralWorker(worker_service)
            try:
                now = "2026-05-25T00:00:00+00:00"
                operation_id = "steer-lock-cleanup"
                worker_service.storage.create_operation(
                    {
                        "operation_id": operation_id,
                        "client_request_id": "steer-lock-cleanup",
                        "operation_type": "steer_turn",
                        "status": "running",
                        "phase": "running",
                        "project_id": "project-id",
                        "chat_id": "thread-id",
                        "thread_id": "thread-id",
                        "turn_id": "turn-id",
                        "workflow_id": None,
                        "cwd": str(root),
                        "title": None,
                        "request_json": '{"thread_id":"thread-id","expected_turn_id":"turn-id","message":"steer"}',
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                worker_service.storage.replace_resource_locks_for_operation(
                    operation_id=operation_id,
                    locks=[
                        {
                            "lock_key": "thread:thread-id:active-turn",
                            "operation_id": operation_id,
                            "thread_id": "thread-id",
                            "project_id": "project-id",
                            "lock_mode": "exclusive",
                            "worker_id": "stale-worker",
                            "created_at": now,
                            "expires_at": "2026-05-25T06:00:00+00:00",
                        }
                    ],
                )

                worker._cleanup_terminal_locks()
                locks = worker_service.storage.list_resource_locks(operation_id=operation_id)
            finally:
                await worker_service.close()

            self.assertEqual([], locks)

    async def test_cleanup_releases_locks_when_tracked_turn_is_terminal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_db = root / ".codex" / "state_5.sqlite"
            worker_config = _search_service_config(root, state_db)
            worker_config.execution_mode = "worker"
            worker_service = ToolService(worker_config)
            worker = CentralWorker(worker_service)
            try:
                now = "2026-05-25T00:00:00+00:00"
                operation_id = "terminal-turn-lock-cleanup"
                worker_service.storage.create_operation(
                    {
                        "operation_id": operation_id,
                        "client_request_id": "terminal-turn-lock-cleanup",
                        "operation_type": "start_chat",
                        "status": "running",
                        "phase": "running",
                        "project_id": "project-id",
                        "chat_id": "thread-id",
                        "thread_id": "thread-id",
                        "turn_id": "turn-done",
                        "workflow_id": None,
                        "cwd": str(root),
                        "title": None,
                        "request_json": '{"sandbox":"workspace-write"}',
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                worker_service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-done",
                        "thread_id": "thread-id",
                        "chat_id": "thread-id",
                        "project_id": "project-id",
                        "project_path": str(root),
                        "status": "completed",
                        "started_at": now,
                        "updated_at": now,
                        "completed_at": now,
                        "first_message_at": None,
                        "final_message": "done",
                        "last_error": None,
                        "source": "test",
                    }
                )
                worker_service.storage.replace_resource_locks_for_operation(
                    operation_id=operation_id,
                    locks=[
                        {
                            "lock_key": "project:project-id:write",
                            "operation_id": operation_id,
                            "thread_id": "thread-id",
                            "project_id": "project-id",
                            "lock_mode": "exclusive",
                            "worker_id": "stale-worker",
                            "created_at": now,
                            "expires_at": "2026-05-25T06:00:00+00:00",
                        }
                    ],
                )

                worker._cleanup_terminal_locks()
                locks = worker_service.storage.list_resource_locks(operation_id=operation_id)
            finally:
                await worker_service.close()

            self.assertEqual([], locks)

    async def test_write_turns_without_resource_keys_serialize_in_same_project(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            state_db = root / ".codex" / "state_5.sqlite"
            project_id = project_id_for_path(str(project))
            client_config = _search_service_config(root, state_db)
            client_config.execution_mode = "client"
            client = ToolService(client_config)
            worker_config = _search_service_config(root, state_db)
            worker_config.execution_mode = "worker"
            worker_config.max_active_turns_global = 4
            worker_config.max_active_turns_per_project = 4
            worker_config.max_active_write_turns_per_project = 1
            worker_service = ToolService(worker_config)
            fake = FakeAppServer(worker_service.storage, first_message=None)
            worker_service._app_server = fake  # type: ignore[assignment]
            worker = CentralWorker(worker_service)
            try:
                first = await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "write one",
                        "client_request_id": "write-lock-one",
                        "sandbox": "workspace-write",
                    },
                )
                second = await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "write two",
                        "client_request_id": "write-lock-two",
                        "sandbox": "workspace-write",
                    },
                )
                await worker._schedule_startable_operations()
                await _wait_for_turn_starts(fake, expected=1)
                first_status = await worker_service.call("codex_get_operation_status", {"operation_id": first["operationId"]})
                second_status = await worker_service.call("codex_get_operation_status", {"operation_id": second["operationId"]})
            finally:
                await client.close()
                await worker_service.close()

            self.assertEqual(1, len(fake.turn_start_calls))
            self.assertEqual("running", first_status["status"])
            self.assertEqual("resource_lock_conflict", second_status["queueState"]["queuedReason"])

    async def test_disjoint_resource_keys_allow_parallel_write_turns(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            state_db = root / ".codex" / "state_5.sqlite"
            project_id = project_id_for_path(str(project))
            client_config = _search_service_config(root, state_db)
            client_config.execution_mode = "client"
            client = ToolService(client_config)
            worker_config = _search_service_config(root, state_db)
            worker_config.execution_mode = "worker"
            worker_config.max_active_turns_global = 4
            worker_config.max_active_turns_per_project = 4
            worker_config.max_active_write_turns_per_project = 1
            worker_service = ToolService(worker_config)
            fake = FakeAppServer(worker_service.storage, first_message=None)
            worker_service._app_server = fake  # type: ignore[assignment]
            worker = CentralWorker(worker_service)
            try:
                await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "write file a",
                        "client_request_id": "resource-key-a",
                        "sandbox": "workspace-write",
                        "resource_keys": ["chapters/chapter-01.md"],
                    },
                )
                await client.call(
                    "codex_submit_task",
                    {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "write file b",
                        "client_request_id": "resource-key-b",
                        "sandbox": "workspace-write",
                        "resource_keys": ["chapters/chapter-02.md"],
                    },
                )
                await worker._schedule_startable_operations()
                await _wait_for_turn_starts(fake, expected=2)
            finally:
                await client.close()
                await worker_service.close()

            self.assertEqual(2, len(fake.turn_start_calls))


async def _wait_for_turn_starts(fake: FakeAppServer, *, expected: int) -> None:
    for _ in range(100):
        if len(fake.turn_start_calls) >= expected:
            return
        await asyncio.sleep(0.01)


async def _wait_for_steer_calls(fake: FakeAppServer, *, expected: int) -> None:
    for _ in range(100):
        if len(fake.turn_steer_calls) >= expected:
            return
        await asyncio.sleep(0.01)


def _insert_operation_with_scheduling(
    service: ToolService,
    *,
    operation_id: str,
    status: str,
    turn_id: str,
    queue_status: str,
    now: str,
) -> None:
    service.storage.create_operation(
        {
            "operation_id": operation_id,
            "client_request_id": operation_id,
            "operation_type": "start_chat",
            "status": status,
            "phase": status,
            "project_id": "project-id",
            "chat_id": "thread-" + turn_id,
            "thread_id": "thread-" + turn_id,
            "turn_id": turn_id,
            "workflow_id": None,
            "cwd": None,
            "title": None,
            "request_json": json.dumps({"message": operation_id}, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        }
    )
    service.storage.upsert_operation_scheduling(
        operation_id=operation_id,
        agent_id="agent",
        priority="normal",
        estimated_cost_class="normal",
        resource_keys=[],
        queue_status=queue_status,
        queued_reason=None,
        created_at=now,
        updated_at=now,
    )
