from __future__ import annotations

from tests.helpers import *


class PromptDedupTests(unittest.TestCase):
    def test_prompt_normalization_hash_and_similarity(self) -> None:
        left = normalize_prompt("  Нужно\r\nпроверить   ПРОЕКТ и подготовить план внедрения идемпотентности. ")
        right = normalize_prompt("нужно проверить проект и подготовить план внедрения идемпотентности.")

        self.assertEqual(left, right)
        self.assertEqual(prompt_hash(left), prompt_hash(right))

        similar_a = normalize_prompt("Проанализируй MCP сервер Codex и подготовь подробный план исправления таймаутов и дублей")
        similar_b = normalize_prompt("Проанализируй MCP-сервер Codex и подготовь подробный план исправления таймаутов и дублей")
        different = normalize_prompt("Составь короткую справку по настройке Telegram канала")

        self.assertGreaterEqual(prompt_similarity(similar_a, similar_b), 0.90)
        self.assertLess(prompt_similarity(similar_a, different), 0.90)
        self.assertEqual(0.0, prompt_similarity(normalize_prompt("short prompt"), normalize_prompt("short promzz")))


class McpDefinitionTests(unittest.TestCase):
    def test_submit_task_plan_mode_runtime_policy_floor_only_for_plan(self) -> None:
        async def scenario() -> tuple[dict, dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    plan = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Prepare a plan through submit task",
                            "collaboration_mode": "plan",
                            "sandbox": "read-only",
                            "client_request_id": "submit-plan-runtime-floor",
                        },
                    )
                    normal = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Run a normal read-only task",
                            "sandbox": "read-only",
                            "client_request_id": "submit-normal-read-only",
                        },
                    )
                    for _ in range(50):
                        plan_status = service.codex_get_operation_status({"operation_id": plan["operationId"]})
                        normal_status = service.codex_get_operation_status({"operation_id": normal["operationId"]})
                        if plan_status.get("turnId") and normal_status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    return plan, normal, fake.turn_start_calls
                finally:
                    await service.close()

        plan, normal, turn_start_calls = asyncio.run(scenario())

        self.assertEqual("workspace-write", plan["effectiveSandbox"])
        self.assertEqual("read-only", plan["requestedSandbox"])
        self.assertTrue(plan["runtimePolicyAdjusted"])
        self.assertNotIn("effectiveSandbox", normal)
        self.assertEqual({"type": "workspaceWrite"}, turn_start_calls[0]["sandbox_policy"])
        self.assertEqual({"type": "readOnly"}, turn_start_calls[1]["sandbox_policy"])

    def test_submit_task_plan_mode_uses_configured_danger_full_access_floor(self) -> None:
        async def scenario() -> tuple[dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                config.default_sandbox_policy = {"type": "dangerFullAccess"}
                config.default_approval_policy = "never"
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    plan = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Prepare a plan through submit task",
                            "collaboration_mode": "plan",
                            "sandbox": "read-only",
                            "approval_policy": "on-request",
                            "client_request_id": "submit-plan-runtime-local-danger-overrides-call",
                        },
                    )
                    for _ in range(50):
                        plan_status = service.codex_get_operation_status({"operation_id": plan["operationId"]})
                        if plan_status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    return plan, fake.turn_start_calls
                finally:
                    await service.close()

        plan, turn_start_calls = asyncio.run(scenario())

        self.assertEqual("danger-full-access", plan["effectiveSandbox"])
        self.assertEqual("read-only", plan["requestedSandbox"])
        self.assertEqual("never", plan["effectiveApprovalPolicy"])
        self.assertEqual("on-request", plan["requestedApprovalPolicy"])
        self.assertTrue(plan["runtimePolicyAdjusted"])
        self.assertEqual("danger-full-access", plan["runtimePolicy"]["sandboxFloor"])
        self.assertEqual({"type": "dangerFullAccess"}, turn_start_calls[0]["sandbox_policy"])
        self.assertEqual("never", turn_start_calls[0]["approval_policy"])

    def test_submit_task_returns_retryable_state_busy_when_operation_create_is_locked(self) -> None:
        async def scenario() -> dict:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                project_id = project_id_for_path(str(project))
                original_create_operation = service.storage.create_operation

                def locked_create_operation(row: dict) -> bool:
                    raise sqlite3.OperationalError("database is locked")

                service.storage.create_operation = locked_create_operation  # type: ignore[method-assign]
                try:
                    return await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "locked submit",
                            "client_request_id": "locked-submit-same-id",
                        },
                    )
                finally:
                    service.storage.create_operation = original_create_operation  # type: ignore[method-assign]
                    await service.close()

        result = asyncio.run(scenario())

        self.assertEqual("CODEX_STATE_BUSY", result["error"]["code"])
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual("locked-submit-same-id", result["error"]["details"]["client_request_id"])
        self.assertEqual("retry_write_same_id", result["agentGuidance"]["instructions"][0]["kind"])

    def test_operation_status_corrects_premature_completed_when_turn_is_active(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                operation = _storage_operation_row(
                    "op-premature",
                    status="completed",
                    thread_id="thread-premature",
                    turn_id="turn-premature",
                    cwd=str(root),
                    updated_at="2026-05-25T00:00:05+00:00",
                )
                operation["completed_at"] = "2026-05-25T00:00:05+00:00"
                operation["final_report_json"] = json.dumps({"text": "stale final report"})
                operation["latest_report_hash"] = "stale-hash"
                service.storage.create_operation(operation)
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-premature",
                        "thread_id": "thread-premature",
                        "chat_id": "thread-premature",
                        "project_id": "project",
                        "project_path": str(root),
                        "status": "running",
                        "started_at": "2026-05-25T00:00:00+00:00",
                        "updated_at": "2026-05-25T00:00:06+00:00",
                        "completed_at": None,
                        "first_message_at": "2026-05-25T00:00:01+00:00",
                        "final_message": None,
                        "last_assistant_message": "working",
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                status = service.codex_get_operation_status({"operation_id": "op-premature"})
                repaired = service.storage.get_operation("op-premature") or {}
                diagnostics = service.codex_collect_diagnostics({"operation_id": "op-premature"})
            finally:
                asyncio.run(service.close())

        self.assertEqual("running", status["status"])
        self.assertTrue(status["reconciliationState"]["statusCorrected"])
        self.assertNotIn("finalReport", status)
        self.assertEqual("running", repaired["status"])
        self.assertIsNone(repaired["completed_at"])
        self.assertIsNone(repaired["final_report_json"])
        self.assertTrue(any(item["name"] == "premature_terminal_operation" for item in diagnostics["checks"]))

    def test_operation_status_recovers_terminal_from_transcript_and_releases_slot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                _write_kb_turn(
                    config.kb_history_projects_root,
                    project,
                    "thread-recovered",
                    "turn-recovered",
                    [("assistant", "Recovered final report")],
                    status="completed",
                    updated_at="2026-05-25T00:05:00Z",
                )
                operation = _storage_operation_row(
                    "op-recovered",
                    status="running",
                    thread_id="thread-recovered",
                    turn_id="turn-recovered",
                    cwd=str(project),
                    updated_at="2026-05-25T00:00:05+00:00",
                )
                operation["request_json"] = json.dumps({"message": "long test", "sandbox": "danger-full-access"})
                service.storage.create_operation(operation)
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-recovered",
                        "thread_id": "thread-recovered",
                        "chat_id": "thread-recovered",
                        "project_id": "project",
                        "project_path": str(project),
                        "status": "running",
                        "started_at": "2026-05-25T00:00:00+00:00",
                        "updated_at": "2026-05-25T00:00:06+00:00",
                        "completed_at": None,
                        "first_message_at": "2026-05-25T00:00:01+00:00",
                        "final_message": None,
                        "last_assistant_message": "working",
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                service.storage.upsert_operation_scheduling(
                    operation_id="op-recovered",
                    agent_id="agent",
                    priority="normal",
                    estimated_cost_class="normal",
                    resource_keys=[],
                    queue_status="running",
                    queued_reason=None,
                    created_at="2026-05-25T00:00:00+00:00",
                    updated_at="2026-05-25T00:00:00+00:00",
                )
                service.storage.replace_resource_locks_for_operation(
                    operation_id="op-recovered",
                    locks=[
                        {
                            "lock_key": "thread:thread-recovered:active-turn",
                            "operation_id": "op-recovered",
                            "thread_id": "thread-recovered",
                            "project_id": "project",
                            "lock_mode": "exclusive",
                            "worker_id": "worker",
                            "created_at": "2026-05-25T00:00:00+00:00",
                            "expires_at": "2026-05-25T06:00:00+00:00",
                        }
                    ],
                )

                status = service.codex_get_operation_status({"operation_id": "op-recovered"})
                stored = service.storage.get_operation("op-recovered") or {}
                scheduling = service.storage.get_operation_scheduling("op-recovered") or {}
                locks = service.storage.list_resource_locks(operation_id="op-recovered")
            finally:
                asyncio.run(service.close())

        self.assertEqual("completed", status["status"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual("transcript_terminal", status["turnStatus"]["terminalEvidence"]["source"])
        self.assertTrue(status["turnStatus"]["terminalEvidence"]["recovered"])
        self.assertEqual("completed", scheduling["queue_status"])
        self.assertEqual([], locks)

    def test_config_mismatch_status_does_not_mutate_foreign_operation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
            try:
                operation = _storage_operation_row(
                    "op-foreign-config",
                    status="queued",
                    thread_id="thread-foreign-config",
                    cwd=str(root),
                )
                operation["submitter_config_fingerprint"] = "another-config"
                service.storage.create_operation(operation)

                status = service.codex_get_operation_status({"operation_id": "op-foreign-config"})
                stored = service.storage.get_operation("op-foreign-config") or {}
            finally:
                asyncio.run(service.close())

        self.assertEqual("queued", status["status"])
        self.assertEqual("mismatch", status["configRecoveryState"]["state"])
        self.assertEqual("queued", stored["status"])
        self.assertIsNone(stored["last_error"])
        self.assertIsNone(stored["worker_config_fingerprint"])

    def test_config_fingerprint_ignores_codex_binary_revision_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            right = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            left.codex_binary_path = root / "Codex" / "bin" / "old-revision" / "codex.exe"
            right.codex_binary_path = root / "Codex" / "bin" / "new-revision" / "codex.exe"

            left_fingerprint = _config_fingerprint(left)
            right_fingerprint = _config_fingerprint(right)

        self.assertEqual(left_fingerprint, right_fingerprint)

    def test_turn_tracker_waits_first_message_and_records_completion(self) -> None:
        async def scenario() -> tuple[dict | None, dict | None]:
            with TemporaryDirectory() as tmp:
                storage = McpStorage(Path(tmp) / "state.sqlite3")
                storage.connect()
                try:
                    tracker = TurnTracker(storage)
                    tracker.register_turn(
                        turn_id="turn-1",
                        thread_id="thread-1",
                        chat_id="thread-1",
                        project_id="project-1",
                        project_path=str(Path(tmp)),
                        user_message="first prompt password=SECRETSECRET",
                    )
                    waiter = asyncio.create_task(tracker.wait_first_message("turn-1", 2))
                    await asyncio.sleep(0)
                    tracker.record_event(
                        {
                            "method": "item/created",
                            "params": {
                                "threadId": "thread-1",
                                "turnId": "turn-1",
                                "item": {"type": "agentMessage", "text": "first assistant"},
                            },
                        },
                        received_at="2026-05-25T00:00:01+00:00",
                    )
                    first, timed_out = await waiter
                    tracker.record_event(
                        {
                            "method": "turn/completed",
                            "params": {"threadId": "thread-1", "turnId": "turn-1", "status": "completed"},
                        },
                        received_at="2026-05-25T00:00:02+00:00",
                    )
                    status = tracker.get_turn_status("turn-1", last_messages=10, message_max_chars=8000)
                    hook_turn = storage.get_hook_turn("turn-1")
                    hook_messages = storage.list_hook_messages(thread_id="thread-1")
                    self.assertFalse(timed_out)
                    return first, status, hook_turn, hook_messages
                finally:
                    storage.close()

        first, status, hook_turn, hook_messages = asyncio.run(scenario())

        self.assertIsNotNone(first)
        self.assertEqual("first assistant", first["text"])
        self.assertIsNotNone(status)
        self.assertEqual("completed", status["status"])
        self.assertTrue(status["completion_observed"])
        self.assertEqual(["first assistant"], [item["text"] for item in status["last_messages"]])
        self.assertIsNotNone(hook_turn)
        self.assertEqual("completed", hook_turn["status"])
        self.assertEqual(["user", "assistant"], [item["role"] for item in hook_messages])
        self.assertIn("password=[redacted]", hook_messages[0]["text"])
        self.assertEqual("first assistant", hook_messages[1]["text"])

    def test_turn_tracker_clears_waiting_error_after_interaction_and_completion(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "state.sqlite3")
            storage.connect()
            try:
                tracker = TurnTracker(storage)
                tracker.register_turn(
                    turn_id="turn-waiting",
                    thread_id="thread-waiting",
                    chat_id="thread-waiting",
                    project_id="project-1",
                    project_path=str(Path(tmp)),
                )
                params = {"threadId": "thread-waiting", "turnId": "turn-waiting"}
                tracker.mark_pending_interaction(COMMAND_APPROVAL_METHOD, params)
                waiting = tracker.get_turn_status("turn-waiting", last_messages=10, message_max_chars=8000)
                tracker.mark_interaction_resolved(COMMAND_APPROVAL_METHOD, params)
                resumed = tracker.get_turn_status("turn-waiting", last_messages=10, message_max_chars=8000)
                tracker.record_event(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "thread-waiting", "turnId": "turn-waiting", "status": "completed"},
                    },
                    received_at="2026-05-25T00:00:02+00:00",
                )
                completed = tracker.get_turn_status("turn-waiting", last_messages=10, message_max_chars=8000)
                storage.update_tracked_turn_status(
                    "turn-waiting",
                    status="completed",
                    updated_at="2026-05-25T00:00:03+00:00",
                    last_error="Waiting for OpenClaw response.",
                )
                legacy_completed = tracker.get_turn_status("turn-waiting", last_messages=10, message_max_chars=8000)
                hook_turn = storage.get_hook_turn("turn-waiting")
            finally:
                storage.close()

        self.assertIsNotNone(waiting)
        self.assertEqual("waiting_for_approval", waiting["status"])
        self.assertEqual("Waiting for OpenClaw response.", waiting["lastError"])
        self.assertIsNotNone(resumed)
        self.assertEqual("running", resumed["status"])
        self.assertIsNone(resumed["lastError"])
        self.assertIsNotNone(completed)
        self.assertEqual("completed", completed["status"])
        self.assertIsNone(completed["lastError"])
        self.assertIsNotNone(legacy_completed)
        self.assertEqual("completed", legacy_completed["status"])
        self.assertIsNone(legacy_completed["lastError"])
        self.assertIsNotNone(hook_turn)
        self.assertIsNone(hook_turn["last_error"])

    def test_turn_tracker_records_nested_terminal_error_from_turn_completed(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "state.sqlite3")
            storage.connect()
            try:
                tracker = TurnTracker(storage)
                tracker.register_turn(
                    turn_id="turn-auth",
                    thread_id="thread-auth",
                    chat_id="thread-auth",
                    project_id="project-1",
                    project_path=str(Path(tmp)),
                )
                tracker.record_event(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-auth",
                            "turn": {
                                "id": "turn-auth",
                                "status": "failed",
                                "error": {
                                    "message": "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
                                },
                            },
                        },
                    },
                    received_at="2026-05-25T00:00:02+00:00",
                )
                status = tracker.get_turn_status("turn-auth", last_messages=10, message_max_chars=8000)
                hook_turn = storage.get_hook_turn("turn-auth")
            finally:
                storage.close()

        self.assertIsNotNone(status)
        self.assertEqual("failed", status["status"])
        self.assertTrue(status["terminalEvidence"]["trusted"])
        self.assertIn("401 Unauthorized", status["lastError"])
        self.assertIsNotNone(hook_turn)
        self.assertIn("401 Unauthorized", hook_turn["last_error"])

    def test_turn_tracker_records_plan_deltas_completed_item_and_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "state.sqlite3")
            storage.connect()
            try:
                tracker = TurnTracker(storage)
                tracker.register_turn(
                    turn_id="turn-plan",
                    thread_id="thread-plan",
                    chat_id="thread-plan",
                    project_id="project-1",
                    project_path=str(Path(tmp)),
                )
                tracker.record_event(
                    {
                        "method": "item/plan/delta",
                        "params": {
                            "threadId": "thread-plan",
                            "turnId": "turn-plan",
                            "itemId": "plan-1",
                            "delta": "Part ",
                        },
                    },
                    received_at="2026-05-25T00:00:01+00:00",
                )
                tracker.record_event(
                    {
                        "method": "item/plan/delta",
                        "params": {
                            "threadId": "thread-plan",
                            "turnId": "turn-plan",
                            "itemId": "plan-1",
                            "delta": "one",
                        },
                    },
                    received_at="2026-05-25T00:00:02+00:00",
                )
                tracker.record_event(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-plan",
                            "turnId": "turn-plan",
                            "item": {"type": "plan", "id": "plan-1", "text": "Completed plan"},
                        },
                    },
                    received_at="2026-05-25T00:00:03+00:00",
                )
                tracker.record_thread_snapshot(
                    {
                        "thread": {
                            "id": "thread-plan",
                            "turns": [
                                {
                                    "id": "turn-snapshot",
                                    "status": {"type": "idle"},
                                    "startedAt": 1779667200000,
                                    "completedAt": 1779667205000,
                                    "items": [{"type": "plan", "id": "plan-snapshot", "text": "Snapshot plan"}],
                                }
                            ],
                        }
                    },
                    received_at="2026-05-25T00:00:04+00:00",
                )
                live = tracker.get_turn_status("turn-plan", last_messages=10, message_max_chars=8000)
                snapshot = tracker.get_turn_status("turn-snapshot", last_messages=10, message_max_chars=8000)
            finally:
                storage.close()

        self.assertEqual("Completed plan", live["latestPlan"]["markdown"])
        self.assertEqual("completed", live["latestPlan"]["status"])
        self.assertEqual("Snapshot plan", snapshot["latestPlan"]["markdown"])
        self.assertEqual("completed", snapshot["status"])

    def test_turn_tracker_records_redacted_progress_events_without_raw_diff(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "state.sqlite3")
            storage.connect()
            try:
                tracker = TurnTracker(storage)
                tracker.register_turn(
                    turn_id="turn-progress",
                    thread_id="thread-progress",
                    chat_id="thread-progress",
                    project_id="project-1",
                    project_path=str(Path(tmp)),
                )
                delta_payload = {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-progress",
                        "turnId": "turn-progress",
                        "itemId": "agent-1",
                        "delta": "Working with token=secret-value",
                    },
                }
                tracker.record_event(delta_payload, received_at="2026-05-25T00:00:01+00:00")
                tracker.record_event(delta_payload, received_at="2026-05-25T00:00:01+00:00")
                tracker.record_event(
                    {
                        "method": "item/reasoning/summaryTextDelta",
                        "params": {
                            "threadId": "thread-progress",
                            "turnId": "turn-progress",
                            "itemId": "reasoning-1",
                            "summaryIndex": 0,
                            "delta": "Checking visible summary",
                        },
                    },
                    received_at="2026-05-25T00:00:02+00:00",
                )
                tracker.record_event(
                    {
                        "method": "turn/diff/updated",
                        "params": {
                            "threadId": "thread-progress",
                            "turnId": "turn-progress",
                            "diff": "diff --git a/file b/file\n+++ b/file\n+very-sensitive-diff-line\n-removed-secret-line",
                        },
                    },
                    received_at="2026-05-25T00:00:03+00:00",
                )
                tracker.record_event(
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": "thread-progress",
                            "turnId": "turn-progress",
                            "tokenUsage": {
                                "last": {
                                    "cachedInputTokens": 1,
                                    "inputTokens": 2,
                                    "outputTokens": 3,
                                    "reasoningOutputTokens": 4,
                                    "totalTokens": 10,
                                },
                                "total": {
                                    "cachedInputTokens": 1,
                                    "inputTokens": 2,
                                    "outputTokens": 3,
                                    "reasoningOutputTokens": 4,
                                    "totalTokens": 10,
                                },
                                "modelContextWindow": 128000,
                            },
                        },
                    },
                    received_at="2026-05-25T00:00:04+00:00",
                )
                tracker.record_event(
                    {
                        "method": "model/rerouted",
                        "params": {
                            "threadId": "thread-progress",
                            "turnId": "turn-progress",
                            "fromModel": "gpt-5",
                            "toModel": "gpt-5-safe",
                            "reason": "highRiskCyberActivity",
                        },
                    },
                    received_at="2026-05-25T00:00:05+00:00",
                )
                tracker.record_event(
                    {
                        "method": "guardianWarning",
                        "params": {
                            "threadId": "thread-progress",
                            "message": "Guardian warning api_key=SECRETSECRET",
                        },
                    },
                    received_at="2026-05-25T00:00:06+00:00",
                )
                status = tracker.get_turn_status(
                    "turn-progress",
                    last_messages=10,
                    message_max_chars=8000,
                    progress_events=10,
                    progress_max_chars=2000,
                )
                disabled = tracker.get_turn_status(
                    "turn-progress",
                    last_messages=10,
                    message_max_chars=8000,
                    progress_events=0,
                )
            finally:
                storage.close()

        self.assertIsNotNone(status)
        self.assertEqual(6, status["progressEventCount"])
        categories = [item["category"] for item in status["progressEvents"]]
        self.assertIn("assistant_delta_summary", categories)
        self.assertIn("reasoning_summary", categories)
        self.assertIn("diff_updated", categories)
        self.assertEqual("<1k", status["tokenUsage"]["totalTokensBand"])
        self.assertEqual("gpt-5-safe", status["modelReroutes"][0]["toModel"])
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("token=secret-value", serialized)
        self.assertNotIn("token=[redacted]", serialized)
        self.assertIn("api_key=[redacted]", serialized)
        self.assertNotIn("very-sensitive-diff-line", serialized)
        self.assertNotIn("removed-secret-line", serialized)
        self.assertNotIn("progressEvents", disabled)

    def test_get_turn_status_reads_live_storage(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-live",
                        "thread_id": "thread-live",
                        "chat_id": "thread-live",
                        "project_id": "project-live",
                        "project_path": str(root),
                        "status": "ready",
                        "started_at": "2026-05-25T00:00:00+00:00",
                        "updated_at": "2026-05-25T00:00:01+00:00",
                        "completed_at": None,
                        "first_message_at": "2026-05-25T00:00:01+00:00",
                        "final_message": "live assistant",
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                service.storage.record_tracked_turn_message(
                    {
                        "event_hash": "hash-live",
                        "turn_id": "turn-live",
                        "thread_id": "thread-live",
                        "role": "assistant",
                        "text": "live assistant",
                        "created_at": "2026-05-25T00:00:01+00:00",
                        "sequence": 1,
                        "event_type": "agentMessage",
                        "payload_json": "{}",
                    }
                )
                result = service.codex_get_turn_status({"turn_id": "turn-live"})
            finally:
                asyncio.run(service.close())

        self.assertEqual("turn-live", result["turn_id"])
        self.assertEqual("ready", result["status"])
        self.assertFalse(result["completionObserved"])
        self.assertFalse(result["terminalEvidence"]["trusted"])
        self.assertIsNone(result["finalMessage"])
        self.assertEqual(["live assistant"], [item["text"] for item in result["last_messages"]])

    def test_app_server_request_waits_for_openclaw_answer(self) -> None:
        async def scenario() -> tuple[list[tuple[object, dict]], dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                service = ToolService(config)
                client = service._app_server = FakeAppServer(service.storage, first_message=None)  # type: ignore[assignment]
                sent: list[tuple[object, dict]] = []

                async def respond_success(request_id: object, result: dict) -> None:
                    sent.append((request_id, result))

                client.respond_success = respond_success  # type: ignore[attr-defined]
                client.tracker.register_turn(
                    turn_id="turn-rpc",
                    thread_id="thread-rpc",
                    chat_id="thread-rpc",
                    project_id="project",
                    project_path=str(root),
                    process_generation=1,
                )
                from openclaw_codex_mcp.codex_app_server import CodexAppServerClient

                real_client = CodexAppServerClient(config, service.storage)
                real_client.process_generation = 1
                real_client.tracker = client.tracker
                real_client.interactions = client.interactions
                real_client.respond_success = respond_success  # type: ignore[method-assign]
                try:
                    await real_client._handle_server_request(
                        {
                            "jsonrpc": "2.0",
                            "id": 7,
                            "method": COMMAND_APPROVAL_METHOD,
                            "params": {
                                "threadId": "thread-rpc",
                                "turnId": "turn-rpc",
                                "itemId": "cmd-1",
                                "command": "echo ok",
                                "availableDecisions": ["accept", "decline"],
                            },
                        }
                    )
                    pending = client.interactions.list_interactions(status="pending", limit=1)[0]
                    client.interactions.answer(str(pending["interactionId"]), {"decision": "decline"}, current_process_generation=1)
                    for _ in range(20):
                        if sent:
                            break
                        await asyncio.sleep(0.01)
                    row = service.storage.get_pending_interaction(str(pending["interactionId"])) or {}
                    return sent, row
                finally:
                    await service.close()

        sent, row = asyncio.run(scenario())

        self.assertEqual([(7, {"decision": "decline"})], sent)
        self.assertEqual("answered", row["status"])

    def test_send_message_returns_fast_ack_and_poll_status_has_messages(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            transcript = sessions / "rollout-thread-send.jsonl"
            _write_transcript(transcript, "thread-send", project, [("user", "hello")])
            state_db = root / ".codex" / "state_5.sqlite"
            _create_threads_db(
                state_db,
                [
                    {
                        "id": "thread-send",
                        "rollout_path": str(transcript),
                        "cwd": str(project),
                        "title": "Send",
                        "preview": "",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667200000,
                        "archived": 0,
                    }
                ],
            )
            service = ToolService(_search_service_config(root, state_db))
            service._app_server = FakeAppServer(service.storage, first_message="fake first")  # type: ignore[assignment]
            try:
                result = asyncio.run(
                    service.codex_send_message(
                        {
                            "chat_id": "thread-send",
                            "message": "work",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                )
                status = service.codex_get_turn_status({"turn_id": "turn-fake"})
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["accepted"])
        self.assertTrue(result["pollRecommended"])
        self.assertFalse(result["first_message_observed"])
        self.assertFalse(result["first_message_timed_out"])
        self.assertIsNone(result["first_message"])
        self.assertEqual(["fake first"], [item["text"] for item in result["latestMessages"]])
        self.assertEqual(["fake first"], [item["text"] for item in status["last_messages"]])

    def test_send_message_first_message_timeout_keeps_turn_running(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            transcript = sessions / "rollout-thread-timeout.jsonl"
            _write_transcript(transcript, "thread-timeout", project, [("user", "hello")])
            state_db = root / ".codex" / "state_5.sqlite"
            _create_threads_db(
                state_db,
                [
                    {
                        "id": "thread-timeout",
                        "rollout_path": str(transcript),
                        "cwd": str(project),
                        "title": "Timeout",
                        "preview": "",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667200000,
                        "archived": 0,
                    }
                ],
            )
            service = ToolService(_search_service_config(root, state_db))
            service._app_server = FakeAppServer(service.storage, first_message=None)  # type: ignore[assignment]
            try:
                result = asyncio.run(
                    service.codex_send_message(
                        {
                            "chat_id": "thread-timeout",
                            "message": "work",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                )
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["accepted"])
        self.assertFalse(result["first_message_observed"])
        self.assertFalse(result["first_message_timed_out"])
        self.assertTrue(result["pollRecommended"])
        self.assertEqual("running", result["status"])

    def test_send_message_resolves_fresh_thread_from_durable_operation_without_catalog(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
            fake = FakeAppServer(service.storage, first_message="fresh thread follow-up")
            service._app_server = fake  # type: ignore[assignment]
            project_id = project_id_for_path(str(project))
            service.storage.create_operation(
                _storage_operation_row(
                    "op-known-thread",
                    status="completed",
                    operation_type="start_chat",
                    thread_id="thread-known-only",
                    cwd=str(project),
                    request={"operation_type": "start_chat", "message": "already completed"},
                )
            )
            service.storage.update_operation("op-known-thread", project_id=project_id, chat_id="thread-known-only")
            try:
                result = asyncio.run(
                    service.codex_send_message(
                        {
                            "chat_id": "thread-known-only",
                            "message": "continue fresh thread",
                        }
                    )
                )
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["accepted"])
        self.assertEqual("thread-known-only", result["threadId"])
        self.assertEqual(1, len(fake.turn_start_calls))
        self.assertEqual("thread-known-only", fake.turn_start_calls[0]["thread_id"])

    def test_submit_task_output_schema_passes_to_turn_start_and_persists_structured_report(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, list[dict], dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                schema = {
                    "type": "object",
                    "required": ["summary", "status"],
                    "additionalProperties": False,
                    "properties": {
                        "summary": {"type": "string"},
                        "status": {"type": "string"},
                    },
                }
                try:
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Return a structured final report.",
                            "client_request_id": "operation-output-schema-1",
                            "output_schema": schema,
                        },
                    )
                    running = submitted
                    for _ in range(50):
                        running = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if running.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    final_json = json.dumps({"summary": "Done", "status": "ok"})
                    service.storage.update_tracked_turn_status(
                        running["turnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:04+00:00",
                        completed_at="2026-05-25T00:00:04+00:00",
                        final_message=final_json,
                    )
                    completed = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                    stored = service.storage.get_operation(str(submitted["operationId"])) or {}
                    return submitted, running, completed, fake.turn_start_calls, stored
                finally:
                    await service.close()

        submitted, running, completed, calls, stored = asyncio.run(scenario())

        self.assertEqual("operation-output-schema-1", submitted["clientRequestId"])
        self.assertEqual("running", running["status"])
        self.assertEqual(1, len(calls))
        self.assertEqual("object", calls[0]["output_schema"]["type"])
        self.assertIn("outputSchemaState", running)
        self.assertEqual(running["outputSchemaState"]["schemaHash"], running["requestSummary"]["outputSchemaHash"])
        self.assertNotIn("output_schema", running["requestSummary"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("Done", completed["finalReport"]["structured"]["summary"])
        self.assertEqual("ok", completed["finalReport"]["structured"]["status"])
        self.assertEqual("parsed", completed["finalReport"]["structuredStatus"])
        self.assertEqual("valid_json", completed["finalReport"]["structuredParseStatus"])
        self.assertEqual("valid_json", completed["outputSchemaState"]["parseStatus"])
        self.assertEqual(completed["latestReportHash"], stored["latest_report_hash"])
        self.assertIsNotNone(stored["final_report_json"])

    def test_submit_task_output_schema_rejects_invalid_schema_before_app_server_call(self) -> None:
        async def scenario() -> tuple[dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    result = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id_for_path(str(project)),
                            "message": "invalid schema",
                            "output_schema": {"type": "unsupported"},
                        },
                    )
                    return result, len(fake.turn_start_calls)
                finally:
                    await service.close()

        result, call_count = asyncio.run(scenario())

        self.assertEqual("INVALID_ARGUMENT", result["error"]["code"])
        self.assertEqual(0, call_count)

    def test_submit_task_image_inputs_pass_to_turn_start_and_status_is_safe(self) -> None:
        async def scenario() -> tuple[dict, dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                image = project / "screenshot.png"
                image.write_bytes(b"\x89PNG\r\n\x1a\n")
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message="image first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "cwd": str(project),
                            "message": "Analyze the attached screenshot.",
                            "input_items": [
                                {"type": "localImage", "path": "screenshot.png", "detail": "low"},
                                {
                                    "type": "image",
                                    "url": "https://example.com/private/screenshot.png?token=secret",
                                    "detail": "high",
                                },
                            ],
                        },
                    )
                    running = submitted
                    for _ in range(50):
                        running = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if running.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    return submitted, running, fake.turn_start_calls
                finally:
                    await service.close()

        submitted, running, calls = asyncio.run(scenario())

        self.assertIn("operationId", submitted)
        self.assertEqual("running", running["status"])
        self.assertEqual(1, len(calls))
        sent_items = calls[0]["input_items"]
        self.assertEqual("text", sent_items[0]["type"])
        self.assertEqual("localImage", sent_items[1]["type"])
        self.assertTrue(Path(sent_items[1]["path"]).is_absolute())
        self.assertEqual("image", sent_items[2]["type"])
        self.assertEqual("https://example.com/private/screenshot.png?token=secret", sent_items[2]["url"])
        state = running["inputItemState"]
        self.assertEqual(2, state["count"])
        self.assertEqual(1, state["localImageCount"])
        self.assertEqual(1, state["imageUrlCount"])
        self.assertIn("dedupHash", state)
        rendered = json.dumps(running, ensure_ascii=False)
        self.assertNotIn("https://example.com/private/screenshot.png?token=secret", rendered)
        self.assertNotIn("screenshot.png", rendered)
        self.assertNotIn("input_items", running["requestSummary"])
        self.assertNotIn("_input_items", running["requestSummary"])

    def test_submit_task_image_inputs_validate_paths_urls_and_operation_types(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, dict, dict, dict, int]:
            with TemporaryDirectory() as tmp, TemporaryDirectory() as outside_tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                outside = Path(outside_tmp) / "outside.png"
                outside.write_bytes(b"\x89PNG\r\n\x1a\n")
                bad_extension = project / "note.txt"
                bad_extension.write_text("not an image", encoding="utf-8")
                tiny = project / "tiny.png"
                tiny.write_bytes(b"\x89PNG\r\n\x1a\n")
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                config.max_image_input_bytes = 4
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    base = {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "cwd": str(project),
                        "message": "Analyze image.",
                    }
                    missing = await service.call(
                        "codex_submit_task",
                        {**base, "input_items": [{"type": "localImage", "path": "missing.png"}]},
                    )
                    outside_result = await service.call(
                        "codex_submit_task",
                        {**base, "input_items": [{"type": "localImage", "path": str(outside)}]},
                    )
                    bad_ext = await service.call(
                        "codex_submit_task",
                        {**base, "input_items": [{"type": "localImage", "path": str(bad_extension)}]},
                    )
                    too_large = await service.call(
                        "codex_submit_task",
                        {**base, "input_items": [{"type": "localImage", "path": str(tiny)}]},
                    )
                    bad_url = await service.call(
                        "codex_submit_task",
                        {**base, "input_items": [{"type": "image", "url": "data:image/png;base64,AAAA"}]},
                    )
                    steer = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "steer_turn",
                            "thread_id": "thread-active",
                            "expected_turn_id": "turn-active",
                            "message": "look at this",
                            "input_items": [{"type": "image", "url": "https://example.com/a.png"}],
                        },
                    )
                    fork_only = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "fork_thread",
                            "source_thread_id": "thread-source",
                            "input_items": [{"type": "image", "url": "https://example.com/a.png"}],
                        },
                    )
                    return missing, outside_result, bad_ext, too_large, bad_url, steer, fork_only, len(fake.turn_start_calls)
                finally:
                    await service.close()

        missing, outside_result, bad_ext, too_large, bad_url, steer, fork_only, call_count = asyncio.run(scenario())

        for item in (missing, outside_result, bad_ext, too_large, bad_url, steer, fork_only):
            self.assertEqual("INVALID_ARGUMENT", item["error"]["code"])
        self.assertEqual(0, call_count)

    def test_submit_task_image_dedup_uses_image_descriptor(self) -> None:
        async def scenario() -> tuple[dict, dict, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message="image dedup first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    base = {
                        "operation_type": "start_chat",
                        "project_id": project_id,
                        "message": "Investigate duplicate prompt protection with image evidence.",
                    }
                    first = await service.call(
                        "codex_submit_task",
                        {**base, "input_items": [{"type": "image", "url": "https://example.com/a.png"}]},
                    )
                    duplicate = await service.call(
                        "codex_submit_task",
                        {**base, "input_items": [{"type": "image", "url": "https://example.com/a.png"}]},
                    )
                    different = await service.call(
                        "codex_submit_task",
                        {**base, "input_items": [{"type": "image", "url": "https://example.com/b.png"}]},
                    )
                    return first, duplicate, different
                finally:
                    await service.close()

        first, duplicate, different = asyncio.run(scenario())

        self.assertIn("operationId", first)
        self.assertEqual("CODEX_DUPLICATE_PROMPT_ACTIVE", duplicate["error"]["code"])
        self.assertIn("operationId", different)
        self.assertNotEqual(first["operationId"], different["operationId"])

    def test_submit_task_output_schema_requires_strict_object_schema(self) -> None:
        async def scenario() -> tuple[dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    result = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id_for_path(str(project)),
                            "message": "schema missing additionalProperties false",
                            "output_schema": {
                                "type": "object",
                                "required": ["summary"],
                                "properties": {"summary": {"type": "string"}},
                            },
                        },
                    )
                    return result, len(fake.turn_start_calls)
                finally:
                    await service.close()

        result, call_count = asyncio.run(scenario())

        self.assertEqual("INVALID_ARGUMENT", result["error"]["code"])
        self.assertEqual("$", result["error"]["details"]["path"])
        self.assertEqual(0, call_count)

    def test_submit_task_plain_final_report_remains_readable_without_structured_payload(self) -> None:
        async def scenario() -> dict:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                service._app_server = FakeAppServer(service.storage, first_message=None)  # type: ignore[assignment]
                try:
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id_for_path(str(project)),
                            "message": "Return a plain final report.",
                            "client_request_id": "operation-plain-report-1",
                        },
                    )
                    running = submitted
                    for _ in range(50):
                        running = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if running.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    service.storage.update_tracked_turn_status(
                        running["turnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:04+00:00",
                        completed_at="2026-05-25T00:00:04+00:00",
                        final_message="Plain final report",
                    )
                    return service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                finally:
                    await service.close()

        completed = asyncio.run(scenario())

        self.assertEqual("completed", completed["status"])
        self.assertEqual("Plain final report", completed["finalReport"]["text"])
        self.assertIsNone(completed["finalReport"]["structured"])
        self.assertEqual("not_available", completed["finalReport"]["structuredStatus"])
        self.assertEqual("plain_text", completed["finalReport"]["structuredParseStatus"])

    def test_submit_task_rejects_active_duplicate_prompt_in_project(self) -> None:
        async def scenario() -> tuple[dict, dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message="active duplicate first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    first = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Investigate duplicate prompt protection for active long running work.",
                        },
                    )
                    duplicate = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Investigate duplicate prompt protection for active long running work.",
                        },
                    )
                    return first, duplicate, len(fake.turn_start_calls)
                finally:
                    await service.close()

        first, duplicate, turn_start_count = asyncio.run(scenario())

        self.assertIn("operationId", first)
        self.assertIn("error", duplicate)
        self.assertEqual("CODEX_DUPLICATE_PROMPT_ACTIVE", duplicate["error"]["code"])
        self.assertEqual(first["operationId"], duplicate["error"]["details"]["existingOperationId"])
        self.assertLessEqual(turn_start_count, 1)

    def test_submit_task_allows_active_similar_prompts_with_disjoint_resource_keys(self) -> None:
        async def scenario() -> tuple[dict, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                config = _search_service_config(root, state_db)
                config.execution_mode = "client"
                service = ToolService(config)
                project_id = project_id_for_path(str(project))
                prompt = (
                    "MCP LIVE TEST / SANDBOX PROJECT / OK TO MODIFY FILES. "
                    "Create checkpoints, wait, and write final_report.json for scenario parallel template."
                )
                try:
                    first = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": prompt,
                            "resource_keys": ["area-a"],
                            "client_request_id": "resource-dedup-a",
                        },
                    )
                    second = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": prompt,
                            "resource_keys": ["area-b"],
                            "client_request_id": "resource-dedup-b",
                        },
                    )
                    return first, second
                finally:
                    await service.close()

        first, second = asyncio.run(scenario())

        self.assertNotIn("error", second)
        self.assertNotEqual(first["operationId"], second["operationId"])
        self.assertEqual("queued", first["status"])
        self.assertEqual("queued", second["status"])

    def test_submit_task_inactive_duplicate_continues_existing_thread_when_explicitly_allowed(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, list[dict], list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message="continuation first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    first = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Investigate duplicate prompt continuation after completed work.",
                        },
                    )
                    first_status = first
                    for _ in range(50):
                        first_status = service.codex_get_operation_status({"operation_id": first["operationId"]})
                        if first_status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    fake.tracker.record_event(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": first_status["threadId"],
                                "turnId": first_status["turnId"],
                                "status": "completed",
                            },
                        },
                        received_at="2026-05-25T00:00:02+00:00",
                    )
                    service.codex_get_operation_status({"operation_id": first["operationId"]})
                    repeated = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Investigate duplicate prompt continuation after completed work.",
                            "thread_mode": "auto",
                            "allow_historical_continuation": True,
                        },
                    )
                    repeated_status = repeated
                    for _ in range(50):
                        repeated_status = service.codex_get_operation_status({"operation_id": repeated["operationId"]})
                        if repeated_status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    return first_status, repeated, repeated_status, fake.thread_start_calls, fake.turn_start_calls
                finally:
                    await service.close()

        first_status, repeated, repeated_status, thread_start_calls, turn_start_calls = asyncio.run(scenario())

        self.assertEqual("thread-new", first_status["threadId"])
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual("continued_existing_chat", repeated["dedupAction"])
        self.assertEqual("start_chat", repeated["originalOperationType"])
        self.assertEqual("send_message", repeated["operationType"])
        self.assertEqual("thread-new", repeated_status["threadId"])
        self.assertEqual("turn-fake-2", repeated_status["turnId"])
        self.assertEqual(1, len(thread_start_calls))
        self.assertEqual(2, len(turn_start_calls))

    def test_submit_task_inactive_duplicate_starts_new_thread_by_default(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, list[dict], list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message="new thread first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    first = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Investigate duplicate prompt fresh thread default.",
                        },
                    )
                    first_status = first
                    for _ in range(50):
                        first_status = service.codex_get_operation_status({"operation_id": first["operationId"]})
                        if first_status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    fake.tracker.record_event(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": first_status["threadId"],
                                "turnId": first_status["turnId"],
                                "status": "completed",
                            },
                        },
                        received_at="2026-05-25T00:00:02+00:00",
                    )
                    service.codex_get_operation_status({"operation_id": first["operationId"]})
                    repeated = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Investigate duplicate prompt fresh thread default.",
                        },
                    )
                    repeated_status = repeated
                    for _ in range(50):
                        repeated_status = service.codex_get_operation_status({"operation_id": repeated["operationId"]})
                        if repeated_status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    return first_status, repeated, repeated_status, fake.thread_start_calls, fake.turn_start_calls
                finally:
                    await service.close()

        first_status, repeated, repeated_status, thread_start_calls, turn_start_calls = asyncio.run(scenario())

        self.assertEqual("thread-new", first_status["threadId"])
        self.assertFalse(repeated.get("deduplicated", False))
        self.assertEqual("start_chat", repeated["operationType"])
        self.assertNotEqual(first_status["operationId"], repeated["operationId"])
        self.assertEqual("thread-new", repeated_status["threadId"])
        self.assertEqual("turn-fake-2", repeated_status["turnId"])
        self.assertEqual(2, len(thread_start_calls))
        self.assertEqual(2, len(turn_start_calls))

    def test_submit_task_failed_duplicate_starts_new_thread(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, list[dict], list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message="retry after failed duplicate")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    first = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Investigate duplicate prompt continuation after failed archived smoke.",
                        },
                    )
                    first_status = first
                    for _ in range(50):
                        first_status = service.codex_get_operation_status({"operation_id": first["operationId"]})
                        if first_status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    fake.tracker.record_event(
                        {
                            "method": "turn/error",
                            "params": {
                                "threadId": first_status["threadId"],
                                "turnId": first_status["turnId"],
                                "error": "test failed turn",
                            },
                        },
                        received_at="2026-05-25T00:00:02+00:00",
                    )
                    failed = service.codex_get_operation_status({"operation_id": first["operationId"]})
                    repeated = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "Investigate duplicate prompt continuation after failed archived smoke.",
                        },
                    )
                    repeated_status = repeated
                    for _ in range(50):
                        repeated_status = service.codex_get_operation_status({"operation_id": repeated["operationId"]})
                        if repeated_status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    return failed, repeated, repeated_status, fake.thread_start_calls, fake.turn_start_calls
                finally:
                    await service.close()

        failed, repeated, repeated_status, thread_start_calls, turn_start_calls = asyncio.run(scenario())

        self.assertEqual("failed", failed["status"])
        self.assertNotIn("deduplicated", repeated)
        self.assertEqual("start_chat", repeated["operationType"])
        self.assertEqual("thread-new", repeated_status["threadId"])
        self.assertEqual("turn-fake-2", repeated_status["turnId"])
        self.assertEqual(2, len(thread_start_calls))
        self.assertEqual(2, len(turn_start_calls))

    def test_operation_recovery_after_thread_start_does_not_create_second_thread(self) -> None:
        async def scenario() -> tuple[dict, int, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message="recovered first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                operation_id = "op-recover-thread"
                request = {
                    "operation_type": "start_chat",
                    "project_id": project_id,
                    "message": "recover after thread start",
                    "_skip_prompt_dedup": True,
                    "_operation_id": operation_id,
                }
                try:
                    service.storage.create_operation(
                        _storage_operation_row(
                            operation_id,
                            status="starting_turn",
                            operation_type="start_chat",
                            thread_id="thread-started",
                            cwd=str(project),
                            request=request,
                        )
                    )
                    status = {}
                    for _ in range(50):
                        status = service.codex_get_operation_status({"operation_id": operation_id})
                        if status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    return status, len(fake.thread_start_calls), len(fake.turn_start_calls)
                finally:
                    await service.close()

        status, thread_starts, turn_starts = asyncio.run(scenario())

        self.assertEqual("running", status["status"])
        self.assertEqual("thread-started", status["threadId"])
        self.assertEqual("turn-fake", status["turnId"])
        self.assertEqual(0, thread_starts)
        self.assertEqual(1, turn_starts)

    def test_operation_with_existing_turn_id_is_reconciled_without_new_turn_start(self) -> None:
        async def scenario() -> tuple[dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    fake.tracker.register_turn(
                        turn_id="turn-existing",
                        thread_id="thread-existing",
                        chat_id="thread-existing",
                        project_id=project_id_for_path(str(project)),
                        project_path=str(project),
                    )
                    service.storage.create_operation(
                        _storage_operation_row(
                            "op-existing-turn",
                            status="starting_turn",
                            operation_type="send_message",
                            thread_id="thread-existing",
                            turn_id="turn-existing",
                            cwd=str(project),
                        )
                    )
                    status = {}
                    for _ in range(20):
                        status = service.codex_get_operation_status({"operation_id": "op-existing-turn"})
                        if status.get("status") == "running":
                            break
                        await asyncio.sleep(0.01)
                    return status, len(fake.turn_start_calls)
                finally:
                    await service.close()

        status, turn_starts = asyncio.run(scenario())

        self.assertEqual("running", status["status"])
        self.assertEqual("turn-existing", status["turnId"])
        self.assertEqual(0, turn_starts)

    def test_submit_task_fork_thread_validates_runtime_arguments(self) -> None:
        async def scenario() -> tuple[dict, dict, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                try:
                    missing_message = await service.call(
                        "codex_submit_task",
                        {"operation_type": "start_chat", "project_id": project_id_for_path(str(project))},
                    )
                    missing_source = await service.call("codex_submit_task", {"operation_type": "fork_thread"})
                    unknown_source = await service.call(
                        "codex_submit_task",
                        {"operation_type": "fork_thread", "source_thread_id": "thread-missing"},
                    )
                    return missing_message, missing_source, unknown_source
                finally:
                    await service.close()

        missing_message, missing_source, unknown_source = asyncio.run(scenario())

        self.assertEqual("INVALID_ARGUMENT", missing_message["error"]["code"])
        self.assertEqual("INVALID_ARGUMENT", missing_source["error"]["code"])
        self.assertEqual("CODEX_THREAD_NOT_FOUND", unknown_source["error"]["code"])

    def test_submit_task_fork_thread_only_is_durable_idempotent(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, list[dict], int, object]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                sessions = root / ".codex" / "sessions"
                sessions.mkdir(parents=True)
                transcript = sessions / "rollout-source.jsonl"
                _write_transcript(transcript, "thread-source", project, [("user", "source request")])
                state_db = root / ".codex" / "state_5.sqlite"
                _create_threads_db(
                    state_db,
                    [
                        {
                            "id": "thread-source",
                            "rollout_path": str(transcript),
                            "cwd": str(project),
                            "title": "Source",
                            "preview": "",
                            "created_at_ms": 1779667200000,
                            "updated_at_ms": 1779667200000,
                            "archived": 0,
                        }
                    ],
                )
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "fork_thread",
                            "source_thread_id": "thread-source",
                            "client_request_id": "fork-only-client-1",
                        },
                    )
                    completed = submitted
                    for _ in range(50):
                        completed = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if completed.get("status") == "completed":
                            break
                        await asyncio.sleep(0.01)
                    repeated = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "fork_thread",
                            "source_thread_id": "thread-source",
                            "client_request_id": "fork-only-client-1",
                        },
                    )
                    prompt_submission = service.storage.get_prompt_submission_by_operation(str(submitted["operationId"]))
                    return submitted, completed, repeated, fake.thread_fork_calls, len(fake.turn_start_calls), prompt_submission
                finally:
                    await service.close()

        submitted, completed, repeated, fork_calls, turn_start_count, prompt_submission = asyncio.run(scenario())

        self.assertEqual("fork_thread", submitted["operationType"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("read_forked_thread", completed["nextRecommendedAction"])
        self.assertEqual("thread-fork", completed["threadId"])
        self.assertIsNone(completed["turnId"])
        self.assertTrue(completed["forkState"]["accepted"])
        self.assertEqual("thread-source", completed["forkState"]["sourceThreadId"])
        self.assertEqual("thread-fork", completed["forkState"]["forkedThreadId"])
        self.assertFalse(completed["forkState"]["hasInitialMessage"])
        self.assertEqual("not_recorded", completed["dedupState"]["state"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(submitted["operationId"], repeated["operationId"])
        self.assertEqual(1, len(fork_calls))
        self.assertEqual(0, turn_start_count)
        self.assertIsNone(prompt_submission)

    def test_submit_task_fork_thread_without_client_request_id_creates_new_forks(self) -> None:
        async def scenario() -> tuple[dict, dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    fake.tracker.register_turn(
                        turn_id="turn-source",
                        thread_id="thread-source",
                        chat_id="thread-source",
                        project_id=project_id_for_path(str(project)),
                        project_path=str(project),
                    )
                    first = await service.call(
                        "codex_submit_task",
                        {"operation_type": "fork_thread", "source_thread_id": "thread-source"},
                    )
                    second = await service.call(
                        "codex_submit_task",
                        {"operation_type": "fork_thread", "source_thread_id": "thread-source"},
                    )
                    for operation_id in (first["operationId"], second["operationId"]):
                        for _ in range(50):
                            status = service.codex_get_operation_status({"operation_id": operation_id})
                            if status.get("status") == "completed":
                                break
                            await asyncio.sleep(0.01)
                    return first, second, fake.thread_fork_calls
                finally:
                    await service.close()

        first, second, fork_calls = asyncio.run(scenario())

        self.assertNotEqual(first["operationId"], second["operationId"])
        self.assertEqual(2, len(fork_calls))

    def test_submit_task_fork_thread_timeout_after_attempt_is_ambiguous_without_retry(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                fake.thread_fork_failure = TimeoutError("thread/fork timed out after send")
                service._app_server = fake  # type: ignore[assignment]
                try:
                    fake.tracker.register_turn(
                        turn_id="turn-source",
                        thread_id="thread-source",
                        chat_id="thread-source",
                        project_id=project_id_for_path(str(project)),
                        project_path=str(project),
                    )
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "fork_thread",
                            "source_thread_id": "thread-source",
                            "client_request_id": "fork-timeout-client-1",
                        },
                    )
                    ambiguous = submitted
                    for _ in range(50):
                        ambiguous = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if ambiguous.get("status") == "unknown_after_app_server_exit":
                            break
                        await asyncio.sleep(0.01)
                    repeated = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                    return submitted, ambiguous, repeated, len(fake.thread_fork_calls)
                finally:
                    await service.close()

        submitted, ambiguous, repeated, fork_call_count = asyncio.run(scenario())

        self.assertEqual("fork_thread", submitted["operationType"])
        self.assertEqual("unknown_after_app_server_exit", ambiguous["status"])
        self.assertTrue(ambiguous["forkState"]["startAttempted"])
        self.assertTrue(ambiguous["forkState"]["ambiguous"])
        self.assertEqual("inspect_diagnostics", ambiguous["nextRecommendedAction"])
        self.assertEqual("unknown_after_app_server_exit", repeated["status"])
        self.assertEqual(1, fork_call_count)

    def test_submit_task_fork_thread_with_message_starts_turn_in_fork(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, list[dict], list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message="fork first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    fake.tracker.register_turn(
                        turn_id="turn-source",
                        thread_id="thread-source",
                        chat_id="thread-source",
                        project_id=project_id,
                        project_path=str(project),
                    )
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "fork_thread",
                            "source_thread_id": "thread-source",
                            "message": "continue in fork",
                            "client_request_id": "fork-message-client-1",
                            "fork_config": {"safe": True},
                            "ephemeral": False,
                        },
                    )
                    accepted = submitted
                    for _ in range(50):
                        accepted = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if accepted.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    fake.tracker.record_event(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": accepted["threadId"],
                                "turnId": accepted["turnId"],
                                "status": "completed",
                            },
                        },
                        received_at="2026-05-25T00:00:02+00:00",
                    )
                    completed = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                    return submitted, accepted, completed, fake.thread_fork_calls, fake.turn_start_calls
                finally:
                    await service.close()

        submitted, accepted, completed, fork_calls, turn_start_calls = asyncio.run(scenario())

        self.assertEqual("fork_thread", submitted["operationType"])
        self.assertEqual("running", accepted["status"])
        self.assertEqual("thread-fork", accepted["threadId"])
        self.assertEqual("turn-fake", accepted["turnId"])
        self.assertTrue(accepted["forkState"]["accepted"])
        self.assertTrue(accepted["forkState"]["hasInitialMessage"])
        self.assertEqual("turn-fake", accepted["forkState"]["turnId"])
        self.assertEqual("poll_turn_status", accepted["nextRecommendedAction"])
        self.assertEqual(1, len(fork_calls))
        self.assertEqual(1, len(turn_start_calls))
        self.assertEqual("thread-fork", turn_start_calls[0]["thread_id"])
        self.assertEqual("completed", completed["status"])

    def test_operation_recovery_after_fork_creation_does_not_create_second_fork(self) -> None:
        async def scenario() -> tuple[dict, int, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message="fork recovered first")
                service._app_server = fake  # type: ignore[assignment]
                operation_id = "op-recover-fork"
                request = {
                    "operation_type": "fork_thread",
                    "source_thread_id": "thread-source",
                    "message": "recover fork turn",
                    "cwd": str(project),
                    "_skip_prompt_dedup": True,
                    "_operation_id": operation_id,
                }
                try:
                    service.storage.create_operation(
                        _storage_operation_row(
                            operation_id,
                            status="starting_turn",
                            operation_type="fork_thread",
                            thread_id="thread-fork-existing",
                            cwd=str(project),
                            request=request,
                        )
                    )
                    status = {}
                    for _ in range(50):
                        status = service.codex_get_operation_status({"operation_id": operation_id})
                        if status.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    return status, len(fake.thread_fork_calls), len(fake.turn_start_calls)
                finally:
                    await service.close()

        status, fork_count, turn_start_count = asyncio.run(scenario())

        self.assertEqual("running", status["status"])
        self.assertEqual("thread-fork-existing", status["threadId"])
        self.assertEqual("turn-fake", status["turnId"])
        self.assertEqual(0, fork_count)
        self.assertEqual(1, turn_start_count)

    def test_startup_recovery_completes_fork_only_after_fork_creation(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                operation_id = "op-fork-only-recover"
                storage.create_operation(
                    _storage_operation_row(
                        operation_id,
                        status="starting_thread",
                        operation_type="fork_thread",
                        thread_id="thread-fork-recovered",
                        request={
                            "operation_type": "fork_thread",
                            "source_thread_id": "thread-source",
                            "cwd": str(Path(tmp)),
                            "_operation_id": operation_id,
                        },
                    )
                )
                recovered = storage.recover_startup_operations(now="2026-05-25T00:05:00+00:00")
                stored = storage.get_operation(operation_id) or {}
            finally:
                storage.close()

        self.assertEqual([operation_id], recovered["completedOperationIds"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual("thread-fork-recovered", stored["thread_id"])

    def test_submit_task_steer_turn_is_durable_idempotent_and_follows_target_turn(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, list[dict], int, object]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    fake.tracker.register_turn(
                        turn_id="turn-steer",
                        thread_id="thread-steer",
                        chat_id="thread-steer",
                        project_id=project_id_for_path(str(project)),
                        project_path=str(project),
                    )
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "steer_turn",
                            "thread_id": "thread-steer",
                            "expected_turn_id": "turn-steer",
                            "message": "add this clarification",
                            "client_request_id": "steer-client-1",
                        },
                    )
                    accepted = submitted
                    for _ in range(50):
                        accepted = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if accepted.get("steerState", {}).get("accepted"):
                            break
                        await asyncio.sleep(0.01)
                    repeated = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "steer_turn",
                            "thread_id": "thread-steer",
                            "expected_turn_id": "turn-steer",
                            "message": "add this clarification",
                            "client_request_id": "steer-client-1",
                        },
                    )
                    fake.tracker.record_event(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": "thread-steer",
                                "turnId": "turn-steer",
                                "status": "completed",
                            },
                        },
                        received_at="2026-05-25T00:00:02+00:00",
                    )
                    completed = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                    prompt_submission = service.storage.get_prompt_submission_by_operation(str(submitted["operationId"]))
                    return submitted, accepted, repeated, completed, fake.turn_steer_calls, len(fake.turn_start_calls), prompt_submission
                finally:
                    await service.close()

        submitted, accepted, repeated, completed, steer_calls, turn_start_count, prompt_submission = asyncio.run(scenario())

        self.assertEqual("steer_turn", submitted["operationType"])
        self.assertEqual("thread-steer", accepted["threadId"])
        self.assertEqual("turn-steer", accepted["turnId"])
        self.assertEqual("running", accepted["status"])
        self.assertTrue(accepted["steerState"]["accepted"])
        self.assertEqual("mcp-steer:" + submitted["operationId"], accepted["steerState"]["clientUserMessageId"])
        self.assertEqual("not_recorded", accepted["dedupState"]["state"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(submitted["operationId"], repeated["operationId"])
        self.assertEqual(1, len(steer_calls))
        self.assertEqual(0, turn_start_count)
        self.assertIsNone(prompt_submission)
        self.assertEqual("completed", completed["status"])
        self.assertFalse(completed["pollRecommended"])

    def test_submit_task_steer_turn_rejects_terminal_or_unknown_target(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
            fake = FakeAppServer(service.storage, first_message=None)
            service._app_server = fake  # type: ignore[assignment]
            try:
                fake.tracker.register_turn(
                    turn_id="turn-terminal",
                    thread_id="thread-terminal",
                    chat_id="thread-terminal",
                    project_id=project_id_for_path(str(project)),
                    project_path=str(project),
                )
                fake.tracker.record_event(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "thread-terminal", "turnId": "turn-terminal", "status": "completed"},
                    },
                    received_at="2026-05-25T00:00:02+00:00",
                )
                terminal = asyncio.run(
                    service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "steer_turn",
                            "thread_id": "thread-terminal",
                            "expected_turn_id": "turn-terminal",
                            "message": "too late",
                        },
                    )
                )
                unknown = asyncio.run(
                    service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "steer_turn",
                            "thread_id": "thread-missing",
                            "expected_turn_id": "turn-missing",
                            "message": "missing",
                        },
                    )
                )
            finally:
                asyncio.run(service.close())

        self.assertEqual("INVALID_ARGUMENT", terminal["error"]["code"])
        self.assertEqual("CODEX_TURN_NOT_FOUND", unknown["error"]["code"])

    def test_two_workers_compete_for_one_operation_and_only_one_starts_turn(self) -> None:
        async def scenario() -> tuple[int, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service_a = ToolService(_search_service_config(root, state_db))
                service_b = ToolService(_search_service_config(root, state_db))
                fake_a = FakeAppServer(service_a.storage, first_message=None)
                fake_b = FakeAppServer(service_b.storage, first_message=None)
                service_a._app_server = fake_a  # type: ignore[assignment]
                service_b._app_server = fake_b  # type: ignore[assignment]
                operation_id = "op-compete"
                request = {
                    "operation_type": "send_message",
                    "chat_id": "thread-compete",
                    "message": "competing workers must not duplicate turn",
                    "_resolved_thread_id": "thread-compete",
                    "_resolved_project_path": str(project),
                    "_skip_prompt_dedup": True,
                    "_operation_id": operation_id,
                }
                try:
                    service_a.storage.create_operation(
                        _storage_operation_row(
                            operation_id,
                            status="queued",
                            operation_type="send_message",
                            thread_id="thread-compete",
                            cwd=str(project),
                            request=request,
                        )
                    )
                    await asyncio.gather(service_a._run_operation(operation_id), service_b._run_operation(operation_id))
                    stored = service_a.storage.get_operation(operation_id) or {}
                    return len(fake_a.turn_start_calls) + len(fake_b.turn_start_calls), stored
                finally:
                    await service_a.close()
                    await service_b.close()

        turn_starts, stored = asyncio.run(scenario())

        self.assertEqual(1, turn_starts)
        self.assertEqual("running", stored["status"])
        self.assertEqual("turn-fake", stored["turn_id"])

    def test_two_workers_compete_for_one_steer_operation_and_only_one_steers(self) -> None:
        async def scenario() -> tuple[int, int, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service_a = ToolService(_search_service_config(root, state_db))
                service_b = ToolService(_search_service_config(root, state_db))
                fake_a = FakeAppServer(service_a.storage, first_message=None)
                fake_b = FakeAppServer(service_b.storage, first_message=None)
                service_a._app_server = fake_a  # type: ignore[assignment]
                service_b._app_server = fake_b  # type: ignore[assignment]
                operation_id = "op-steer-compete"
                project_id = project_id_for_path(str(project))
                for fake in (fake_a, fake_b):
                    fake.tracker.register_turn(
                        turn_id="turn-steer-compete",
                        thread_id="thread-steer-compete",
                        chat_id="thread-steer-compete",
                        project_id=project_id,
                        project_path=str(project),
                    )
                request = {
                    "operation_type": "steer_turn",
                    "thread_id": "thread-steer-compete",
                    "expected_turn_id": "turn-steer-compete",
                    "message": "single steering input",
                    "_operation_id": operation_id,
                    "_client_user_message_id": f"mcp-steer:{operation_id}",
                }
                try:
                    service_a.storage.create_operation(
                        _storage_operation_row(
                            operation_id,
                            status="queued",
                            operation_type="steer_turn",
                            thread_id="thread-steer-compete",
                            turn_id="turn-steer-compete",
                            cwd=str(project),
                            request=request,
                        )
                    )
                    await asyncio.gather(service_a._run_operation(operation_id), service_b._run_operation(operation_id))
                    stored = service_a.storage.get_operation(operation_id) or {}
                    steer_count = len(fake_a.turn_steer_calls) + len(fake_b.turn_steer_calls)
                    start_count = len(fake_a.turn_start_calls) + len(fake_b.turn_start_calls)
                    return steer_count, start_count, stored
                finally:
                    await service_a.close()
                    await service_b.close()

        steer_count, start_count, stored = asyncio.run(scenario())

        self.assertEqual(1, steer_count)
        self.assertEqual(0, start_count)
        self.assertEqual("running", stored["status"])
        self.assertEqual("turn-steer-compete", stored["turn_id"])

    def test_two_workers_compete_for_one_fork_operation_and_only_one_forks(self) -> None:
        async def scenario() -> tuple[int, int, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                state_db = root / ".codex" / "state_5.sqlite"
                service_a = ToolService(_search_service_config(root, state_db))
                service_b = ToolService(_search_service_config(root, state_db))
                fake_a = FakeAppServer(service_a.storage, first_message=None)
                fake_b = FakeAppServer(service_b.storage, first_message=None)
                service_a._app_server = fake_a  # type: ignore[assignment]
                service_b._app_server = fake_b  # type: ignore[assignment]
                operation_id = "op-fork-compete"
                request = {
                    "operation_type": "fork_thread",
                    "source_thread_id": "thread-source",
                    "cwd": str(project),
                    "_operation_id": operation_id,
                }
                try:
                    service_a.storage.create_operation(
                        _storage_operation_row(
                            operation_id,
                            status="queued",
                            operation_type="fork_thread",
                            cwd=str(project),
                            request=request,
                        )
                    )
                    await asyncio.gather(service_a._run_operation(operation_id), service_b._run_operation(operation_id))
                    stored = service_a.storage.get_operation(operation_id) or {}
                    fork_count = len(fake_a.thread_fork_calls) + len(fake_b.thread_fork_calls)
                    start_count = len(fake_a.turn_start_calls) + len(fake_b.turn_start_calls)
                    return fork_count, start_count, stored
                finally:
                    await service_a.close()
                    await service_b.close()

        fork_count, start_count, stored = asyncio.run(scenario())

        self.assertEqual(1, fork_count)
        self.assertEqual(0, start_count)
        self.assertEqual("completed", stored["status"])
        self.assertEqual("thread-fork", stored["thread_id"])

    def test_app_server_status_does_not_start_missing_client(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                result = service.codex_get_app_server_status({})
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["ok"])
        self.assertFalse(result["running"])
        self.assertEqual(0, result["processGeneration"])

    def test_force_restart_marks_active_turn_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage, first_message=None)
            service._app_server = fake  # type: ignore[assignment]
            fake.tracker.register_turn(
                turn_id="turn-force",
                thread_id="thread-force",
                chat_id="thread-force",
                project_id="project",
                project_path=str(root),
                process_generation=1,
            )
            try:
                result = asyncio.run(service.codex_restart_app_server({"force": True, "start_after_restart": False}))
                status = service.codex_get_turn_status({"turn_id": "turn-force"})
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["ok"])
        self.assertEqual("unknown_after_app_server_exit", status["status"])
        self.assertFalse(status["completionObserved"])

    def test_restart_app_server_without_existing_client_can_noop(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                result = asyncio.run(service.codex_restart_app_server({"start_after_restart": False}))
            finally:
                asyncio.run(service.close())

            self.assertTrue(result["ok"])
            self.assertFalse(result["restarted"])
            self.assertFalse(result["started"])

    def test_send_respect_existing_uses_thread_policy(self) -> None:
        row = SimpleNamespace(
            approval_mode="never",
            sandbox_policy={"type": "danger-full-access"},
        )

        self.assertEqual("never", _approval_policy_for_send("respect_existing", row, "untrusted"))
        self.assertEqual({"type": "danger-full-access"}, _sandbox_policy_for_send("respect_existing", row, {"type": "readOnly"}))

    def test_send_non_respect_existing_uses_default_open_policy(self) -> None:
        row = SimpleNamespace(
            approval_mode="never",
            sandbox_policy={"type": "danger-full-access"},
        )

        self.assertEqual("never", _approval_policy_for_send("never_auto_approve", row, "never"))
        self.assertEqual({"type": "dangerFullAccess"}, _sandbox_policy_for_send("danger-full-access", row, {"type": "readOnly"}))
