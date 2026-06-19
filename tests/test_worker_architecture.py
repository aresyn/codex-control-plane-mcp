from __future__ import annotations

import asyncio
import unittest
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
