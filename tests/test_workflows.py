from __future__ import annotations

from tests.helpers import *


class McpDefinitionTests(unittest.TestCase):
    def test_workflow_status_finds_and_adopts_later_transcript_plan_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Book"
            project.mkdir()
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            thread_id = "thread-book"
            official_turn_id = "turn-blocker"
            candidate_turn_id = "turn-candidate"
            transcript = sessions / "rollout-thread-book.jsonl"
            rows = [
                {"timestamp": "2026-06-18T21:15:00Z", "type": "session_meta", "payload": {"id": thread_id, "cwd": str(project)}},
                {"timestamp": "2026-06-18T21:15:01Z", "type": "turn_context", "payload": {"turn_id": official_turn_id}},
                {
                    "timestamp": "2026-06-18T21:16:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "Не могу подготовить план: CreateProcessAsUserW failed: 5",
                    },
                },
                {"timestamp": "2026-06-18T21:16:10Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": official_turn_id}},
                {"timestamp": "2026-06-18T21:40:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": candidate_turn_id}},
                {"timestamp": "2026-06-18T21:40:01Z", "type": "turn_context", "payload": {"turn_id": candidate_turn_id}},
                {
                    "timestamp": "2026-06-18T21:44:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "<proposed_plan>\n# Plan\n\nWrite chapter one safely.\n</proposed_plan>",
                    },
                },
                {"timestamp": "2026-06-18T21:44:10Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": candidate_turn_id}},
            ]
            transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            state_db = root / ".codex" / "state_5.sqlite"
            _create_threads_db(
                state_db,
                [
                    {
                        "id": thread_id,
                        "rollout_path": str(transcript),
                        "cwd": str(project),
                        "title": "Book workflow",
                        "preview": "",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779669000000,
                        "archived": 0,
                    }
                ],
            )
            service = ToolService(_search_service_config(root, state_db))
            try:
                now = "2026-06-18T21:16:10+00:00"
                workflow_id = "wf-book"
                service.storage.create_workflow(
                    {
                        "workflow_id": workflow_id,
                        "client_request_id": "book:plan",
                        "project_id": project_id_for_path(str(project)),
                        "thread_id": thread_id,
                        "plan_turn_id": official_turn_id,
                        "execution_turn_id": None,
                        "phase": "plan_ready",
                        "status": "plan_ready",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": official_turn_id,
                        "thread_id": thread_id,
                        "chat_id": thread_id,
                        "project_id": project_id_for_path(str(project)),
                        "project_path": str(project),
                        "status": "completed",
                        "started_at": "2026-06-18T21:15:01+00:00",
                        "updated_at": now,
                        "completed_at": now,
                        "first_message_at": now,
                        "final_message": "Не могу подготовить план: CreateProcessAsUserW failed: 5",
                        "last_assistant_message": "Не могу подготовить план: CreateProcessAsUserW failed: 5",
                        "last_error": None,
                        "source": "storage",
                    }
                )
                service.storage.upsert_tracked_plan_item(
                    {
                        "item_id": f"{official_turn_id}:assistant-final-plan",
                        "turn_id": official_turn_id,
                        "thread_id": thread_id,
                        "status": "completed",
                        "text": "Не могу подготовить план: CreateProcessAsUserW failed: 5",
                        "created_at": now,
                        "updated_at": now,
                        "completed_at": now,
                        "sequence": 1,
                        "payload_json": json.dumps({"source": "assistant_final_message_fallback"}),
                    }
                )

                status = service.codex_get_workflow_status({"workflow_id": workflow_id})
                observation = status["workflowObservation"]
                self.assertEqual("plan_candidate_found", status["phase"])
                self.assertEqual("adopt_candidate_plan", status["nextRecommendedAction"])
                self.assertEqual("blocker", observation["officialPlanQuality"])
                self.assertEqual(candidate_turn_id, observation["candidatePlans"][0]["turnId"])

                adopted = service.codex_adopt_workflow_plan(
                    {
                        "workflow_id": workflow_id,
                        "candidate_turn_id": candidate_turn_id,
                        "candidate_plan_hash": observation["candidatePlans"][0]["planHash"],
                    }
                )
            finally:
                asyncio.run(service.close())

        self.assertEqual("plan_ready", adopted["phase"])
        self.assertEqual(candidate_turn_id, adopted["planTurnId"])
        self.assertEqual("valid_plan", adopted["latestPlan"]["planQuality"])

    def test_plan_workflow_start_idempotency_ready_and_execute(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, dict, dict, dict, dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                sessions = root / ".codex" / "sessions"
                sessions.mkdir(parents=True)
                transcript = sessions / "rollout-thread-new.jsonl"
                _write_transcript(transcript, "thread-new", project, [("user", "hello")])
                state_db = root / ".codex" / "state_5.sqlite"
                _create_threads_db(
                    state_db,
                    [
                        {
                            "id": "thread-new",
                            "rollout_path": str(transcript),
                            "cwd": str(project),
                            "title": "Workflow",
                            "preview": "",
                            "created_at_ms": 1779667200000,
                            "updated_at_ms": 1779667200000,
                            "archived": 0,
                        }
                    ],
                )
                config = _search_service_config(root, state_db)
                project_id = project_id_for_path(str(project))
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id,
                            "message": "prepare a plan",
                            "client_request_id": "workflow-start-1",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    repeated = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id,
                            "message": "prepare a plan",
                            "client_request_id": "workflow-start-1",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    for _ in range(20):
                        running = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if running["planTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Plan operation did not produce planTurnId")
                    service.storage.upsert_tracked_plan_item(
                        {
                            "item_id": "plan-1",
                            "turn_id": running["planTurnId"],
                            "thread_id": running["threadId"],
                            "status": "completed",
                            "text": "Workflow plan",
                            "created_at": "2026-05-25T00:00:00+00:00",
                            "updated_at": "2026-05-25T00:00:01+00:00",
                            "completed_at": "2026-05-25T00:00:01+00:00",
                            "sequence": 1,
                            "payload_json": "{}",
                        }
                    )
                    service.storage.update_tracked_turn_status(
                        running["planTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:02+00:00",
                        completed_at="2026-05-25T00:00:02+00:00",
                    )
                    ready = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                    approved = service.codex_approve_plan(
                        {
                            "workflow_id": started["workflowId"],
                            "client_request_id": "workflow-execute-1",
                            "first_message_max_chars": 8000,
                        }
                    )
                    for _ in range(20):
                        executing = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if executing["executionTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Execution operation did not produce executionTurnId")
                    repeated_execute = service.codex_approve_plan({"workflow_id": started["workflowId"]})
                    compat_execute = await service.codex_execute_plan({"workflow_id": started["workflowId"]})
                    service.storage.update_tracked_turn_status(
                        executing["executionTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:04+00:00",
                        completed_at="2026-05-25T00:00:04+00:00",
                        final_message="Final workflow report",
                    )
                    completed = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                    return started, repeated, ready, approved, executing, repeated_execute, compat_execute, completed, fake.turn_start_calls
                finally:
                    await service.close()

        started, repeated, ready, approved, executing, repeated_execute, compat_execute, completed, turn_start_calls = asyncio.run(scenario())

        self.assertEqual(started["workflowId"], repeated["workflowId"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual("planning", started["phase"])
        self.assertIsNone(started["threadId"])
        self.assertIsNone(started["planTurnId"])
        self.assertIsNotNone(started["planOperationId"])
        self.assertEqual(started["planOperationId"], started["currentOperationId"])
        self.assertEqual("plan_ready", ready["phase"])
        self.assertEqual("execute_plan", ready["nextRecommendedAction"])
        self.assertEqual("Workflow plan", ready["latestPlan"]["markdown"])
        self.assertIsNotNone(ready["latestPlanHash"])
        self.assertIsNotNone(approved["executionOperationId"])
        self.assertEqual(approved["executionOperationId"], approved["currentOperationId"])
        self.assertEqual("executing", executing["phase"])
        self.assertEqual("wait_execution", executing["nextRecommendedAction"])
        self.assertTrue(repeated_execute["idempotent"])
        self.assertTrue(compat_execute["idempotent"])
        self.assertEqual("completed", completed["phase"])
        self.assertEqual("read_final_report", completed["nextRecommendedAction"])
        self.assertFalse(completed["pollRecommended"])
        self.assertEqual("Final workflow report", completed["finalReport"]["text"])
        self.assertIsNotNone(completed["latestReportHash"])
        self.assertEqual("plan", turn_start_calls[0]["collaboration_mode"]["mode"])
        self.assertEqual("default", turn_start_calls[1]["collaboration_mode"]["mode"])
        self.assertEqual(2, len(turn_start_calls))

    def test_plan_workflow_runtime_policy_floor_raises_read_only_to_workspace_write(self) -> None:
        async def scenario() -> tuple[dict, dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a plan",
                            "sandbox": "read-only",
                            "client_request_id": "workflow-plan-runtime-floor",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    status = started
                    for _ in range(50):
                        status = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if status["planTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    return started, status, fake.turn_start_calls
                finally:
                    await service.close()

        started, status, turn_start_calls = asyncio.run(scenario())

        self.assertEqual("read-only", started["requestedSandbox"])
        self.assertEqual("workspace-write", started["effectiveSandbox"])
        self.assertTrue(started["runtimePolicyAdjusted"])
        self.assertEqual("workspace-write", status["effectiveSandbox"])
        self.assertEqual({"type": "workspaceWrite"}, turn_start_calls[0]["sandbox_policy"])
        self.assertEqual("on-request", turn_start_calls[0]["approval_policy"])

    def test_plan_workflow_respects_configured_danger_full_access_default(self) -> None:
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
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a plan",
                            "client_request_id": "workflow-plan-runtime-local-danger",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    for _ in range(50):
                        status = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if status["planTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    return started, fake.turn_start_calls
                finally:
                    await service.close()

        started, turn_start_calls = asyncio.run(scenario())

        self.assertFalse(started["runtimePolicyAdjusted"])
        self.assertEqual("danger-full-access", started["effectiveSandbox"])
        self.assertEqual("never", started["effectiveApprovalPolicy"])
        self.assertEqual({"type": "dangerFullAccess"}, turn_start_calls[0]["sandbox_policy"])
        self.assertEqual("never", turn_start_calls[0]["approval_policy"])

    def test_plan_workflow_configured_danger_full_access_overrides_weaker_call_policy(self) -> None:
        async def scenario() -> tuple[dict, dict, list[dict]]:
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
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a plan",
                            "sandbox": "read-only",
                            "approval_policy": "on-request",
                            "client_request_id": "workflow-plan-runtime-local-danger-overrides-call",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    status = started
                    for _ in range(50):
                        status = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if status["planTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    return started, status, fake.turn_start_calls
                finally:
                    await service.close()

        started, status, turn_start_calls = asyncio.run(scenario())

        self.assertEqual("read-only", started["requestedSandbox"])
        self.assertEqual("danger-full-access", started["effectiveSandbox"])
        self.assertEqual("on-request", started["requestedApprovalPolicy"])
        self.assertEqual("never", started["effectiveApprovalPolicy"])
        self.assertEqual("danger-full-access", started["runtimePolicy"]["sandboxFloor"])
        self.assertTrue(started["runtimePolicyAdjusted"])
        self.assertTrue(started["runtimePolicy"]["sandboxPolicyAdjusted"])
        self.assertTrue(started["runtimePolicy"]["approvalPolicyAdjusted"])
        self.assertEqual("danger-full-access", status["effectiveSandbox"])
        self.assertEqual("never", status["effectiveApprovalPolicy"])
        self.assertEqual({"type": "dangerFullAccess"}, turn_start_calls[0]["sandbox_policy"])
        self.assertEqual("never", turn_start_calls[0]["approval_policy"])

    def test_retry_workflow_with_runtime_policy_creates_linked_workflow(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, dict, list[dict], dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a plan that needs a retry",
                            "client_request_id": "workflow-retry-source",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    source_status = started
                    for _ in range(50):
                        source_status = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if source_status["planTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    now = "2026-05-25T00:10:00+00:00"
                    service.storage.update_operation(
                        started["planOperationId"],
                        status="failed",
                        phase="failed",
                        last_error="CreateProcessAsUserW failed: 5",
                        completed_at=now,
                        updated_at=now,
                    )
                    service.storage.update_tracked_turn_status(
                        source_status["planTurnId"],
                        status="failed",
                        updated_at=now,
                        completed_at=now,
                        last_error="CreateProcessAsUserW failed: 5",
                    )
                    service.storage.update_workflow(
                        started["workflowId"],
                        phase="failed",
                        status="failed",
                        last_error="CreateProcessAsUserW failed: 5",
                        completed_at=now,
                        updated_at=now,
                    )
                    dry_run = await service.codex_repair_issue(
                        {
                            "action": "retry_workflow_with_runtime_policy",
                            "workflow_id": started["workflowId"],
                            "sandbox": "read-only",
                            "approval_policy": "on-request",
                        }
                    )
                    repair = await service.codex_repair_issue(
                        {
                            "action": "retry_workflow_with_runtime_policy",
                            "workflow_id": started["workflowId"],
                            "sandbox": "danger-full-access",
                            "approval_policy": "never",
                            "client_request_id": "workflow-retry-runtime-policy",
                            "reason": "Retry after Windows sandbox runner failure.",
                            "dry_run": False,
                        }
                    )
                    new_status = repair["result"]["newWorkflow"]
                    for _ in range(50):
                        new_status = service.codex_get_workflow_status({"workflow_id": repair["result"]["newWorkflowId"]})
                        if new_status["planTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    repeated = await service.codex_repair_issue(
                        {
                            "action": "retry_workflow_with_runtime_policy",
                            "workflow_id": started["workflowId"],
                            "sandbox": "danger-full-access",
                            "approval_policy": "never",
                            "client_request_id": "workflow-retry-runtime-policy",
                            "dry_run": False,
                        }
                    )
                    source_after = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                    source_operation = service.storage.get_operation(started["planOperationId"]) or {}
                    return dry_run, repair, repeated, source_after, new_status, fake.turn_start_calls, source_operation
                finally:
                    await service.close()

        dry_run, repair, repeated, source_after, new_status, turn_start_calls, source_operation = asyncio.run(scenario())

        self.assertTrue(dry_run["dryRun"])
        self.assertEqual("workspace-write", dry_run["result"]["runtimePolicy"]["effectiveSandbox"])
        self.assertEqual("danger-full-access", repair["result"]["runtimePolicy"]["effectiveSandbox"])
        self.assertEqual("never", repair["result"]["runtimePolicy"]["effectiveApprovalPolicy"])
        self.assertEqual(source_after["workflowId"], repair["result"]["replacesWorkflowId"])
        self.assertEqual(repair["result"]["newWorkflowId"], repeated["result"]["newWorkflowId"])
        self.assertTrue(repeated["result"]["idempotent"])
        self.assertEqual(repair["result"]["newWorkflowId"], source_after["workflowRetryState"]["replacedByWorkflowId"])
        self.assertEqual(source_after["workflowId"], new_status["workflowRetryState"]["replacesWorkflowId"])
        self.assertEqual({"type": "dangerFullAccess"}, turn_start_calls[1]["sandbox_policy"])
        self.assertEqual("never", turn_start_calls[1]["approval_policy"])
        self.assertEqual(2, len(turn_start_calls))
        self.assertEqual("failed", source_operation["status"])

    def test_plan_workflow_uses_completed_assistant_message_as_plan_fallback(self) -> None:
        async def scenario() -> dict:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a plain text plan",
                            "client_request_id": "workflow-plan-fallback",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    for _ in range(20):
                        running = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if running["planTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Plan operation did not produce planTurnId")
                    service.storage.update_tracked_turn_status(
                        running["planTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:02+00:00",
                        completed_at="2026-05-25T00:00:02+00:00",
                        final_message="Plain assistant plan fallback",
                    )
                    return service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                finally:
                    await service.close()

        ready = asyncio.run(scenario())

        self.assertEqual("plan_needs_review", ready["phase"])
        self.assertEqual("review_plan", ready["nextRecommendedAction"])
        self.assertEqual("Plain assistant plan fallback", ready["latestPlan"]["markdown"])
        self.assertEqual("completed", ready["latestPlan"]["status"])
        self.assertIn(ready["latestPlan"]["planQuality"], {"partial", "needs_review"})

    def test_plan_workflow_goal_sets_once_and_clears_on_completion(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, int, int, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id,
                            "message": "prepare a plan",
                            "client_request_id": "workflow-goal-start",
                            "goal": "Ship the workflow goal sync smoke",
                            "goal_token_budget": 1234,
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    active = started
                    for _ in range(50):
                        active = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"], "refresh_live_goal": True})
                        if active["threadId"] and active["threadGoal"]["syncState"] == "active":
                            break
                        await asyncio.sleep(0.01)
                    repeated_active = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"], "refresh_live_goal": True})
                    service.storage.upsert_tracked_plan_item(
                        {
                            "item_id": "plan-goal-1",
                            "turn_id": active["planTurnId"],
                            "thread_id": active["threadId"],
                            "status": "completed",
                            "text": "Workflow plan",
                            "created_at": "2026-05-25T00:00:00+00:00",
                            "updated_at": "2026-05-25T00:00:01+00:00",
                            "completed_at": "2026-05-25T00:00:01+00:00",
                            "sequence": 1,
                            "payload_json": "{}",
                        }
                    )
                    service.storage.update_tracked_turn_status(
                        active["planTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:02+00:00",
                        completed_at="2026-05-25T00:00:02+00:00",
                    )
                    service.codex_approve_plan(
                        {
                            "workflow_id": started["workflowId"],
                            "client_request_id": "workflow-goal-execute",
                        }
                    )
                    executing = active
                    for _ in range(50):
                        executing = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"], "refresh_live_goal": True})
                        if executing["executionTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    service.storage.update_tracked_turn_status(
                        executing["executionTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:04+00:00",
                        completed_at="2026-05-25T00:00:04+00:00",
                        final_message="Final workflow report",
                    )
                    completed = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"], "refresh_live_goal": True})
                    return (
                        started,
                        active,
                        repeated_active,
                        completed,
                        len(fake.thread_goal_set_calls),
                        len(fake.thread_goal_get_calls),
                        len(fake.thread_goal_clear_calls),
                    )
                finally:
                    await service.close()

        started, active, repeated_active, completed, set_count, get_count, clear_count = asyncio.run(scenario())

        self.assertEqual("pending_thread", started["threadGoal"]["syncState"])
        self.assertEqual("active", active["threadGoal"]["syncState"])
        self.assertEqual("Ship the workflow goal sync smoke", active["threadGoal"]["currentGoal"]["objective"])
        self.assertEqual(1234, active["threadGoal"]["currentGoal"]["tokenBudget"])
        self.assertEqual("active", repeated_active["threadGoal"]["syncState"])
        self.assertEqual(1, set_count)
        self.assertGreaterEqual(get_count, 1)
        self.assertEqual(1, clear_count)
        self.assertEqual("completed", completed["phase"])
        self.assertEqual("cleared", completed["threadGoal"]["syncState"])
        self.assertIsNone(completed["threadGoal"]["currentGoal"])

    def test_plan_workflow_without_goal_does_not_set_app_server_goal(self) -> None:
        async def scenario() -> tuple[dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a plan without goal",
                            "client_request_id": "workflow-no-goal-start",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    status = started
                    for _ in range(50):
                        status = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"]})
                        if status["threadId"]:
                            break
                        await asyncio.sleep(0.01)
                    return status, len(fake.thread_goal_set_calls)
                finally:
                    await service.close()

        status, set_count = asyncio.run(scenario())

        self.assertFalse(status["threadGoal"]["configured"])
        self.assertEqual("not_configured", status["threadGoal"]["syncState"])
        self.assertEqual(0, set_count)

    def test_workflow_status_default_does_not_refresh_live_goal(self) -> None:
        async def scenario() -> tuple[dict, int, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a passive polling plan",
                            "client_request_id": "workflow-passive-status-goal",
                            "goal": "This goal should not sync during passive polling",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    status = started
                    for _ in range(50):
                        status = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"]})
                        if status["threadId"]:
                            break
                        await asyncio.sleep(0.01)
                    return status, len(fake.thread_goal_set_calls), len(fake.thread_goal_get_calls)
                finally:
                    await service.close()

        status, set_count, get_count = asyncio.run(scenario())

        self.assertEqual("pending_thread", status["threadGoal"]["syncState"])
        self.assertFalse(status["threadGoal"]["liveRefreshPerformed"])
        self.assertEqual(0, set_count)
        self.assertEqual(0, get_count)

    def test_plan_workflow_goal_unsupported_does_not_fail_status(self) -> None:
        async def scenario() -> tuple[dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                fake.thread_goal_failures["set"] = RuntimeError("Method not found: thread/goal/set")
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a plan",
                            "client_request_id": "workflow-goal-unsupported",
                            "goal": "Unsupported goal should not fail workflow",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    status = started
                    for _ in range(50):
                        status = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"], "refresh_live_goal": True})
                        if status["threadGoal"]["syncState"] == "unsupported":
                            break
                        await asyncio.sleep(0.01)
                    return status, len(fake.thread_goal_set_calls)
                finally:
                    await service.close()

        status, set_count = asyncio.run(scenario())

        self.assertTrue(status["ok"])
        self.assertEqual("unsupported", status["threadGoal"]["syncState"])
        self.assertFalse(status["threadGoal"]["available"])
        self.assertEqual(1, set_count)

    def test_plan_workflow_goal_error_observes_existing_goal_without_second_set(self) -> None:
        async def scenario() -> tuple[dict, int, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                fake.thread_goal_failures["set"] = RuntimeError("temporary goal set timeout")
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id_for_path(str(project)),
                            "message": "prepare a plan",
                            "client_request_id": "workflow-goal-transient-error",
                            "goal": "Recover goal after timeout",
                            "goal_token_budget": 4321,
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    errored = started
                    for _ in range(50):
                        errored = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"], "refresh_live_goal": True})
                        if errored["threadGoal"]["syncState"] == "error":
                            break
                        await asyncio.sleep(0.01)
                    thread_id = errored["threadId"]
                    fake.thread_goal_failures.pop("set", None)
                    fake.thread_goals[thread_id] = {
                        "threadId": thread_id,
                        "objective": "Recover goal after timeout",
                        "status": "active",
                        "tokenBudget": 4321,
                    }
                    recovered = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"], "refresh_live_goal": True})
                    return recovered, len(fake.thread_goal_set_calls), len(fake.thread_goal_get_calls)
                finally:
                    await service.close()

        status, set_count, get_count = asyncio.run(scenario())

        self.assertEqual("active", status["threadGoal"]["syncState"])
        self.assertIsNone(status["threadGoal"]["lastError"])
        self.assertEqual(1, set_count)
        self.assertGreaterEqual(get_count, 1)

    def test_completed_workflow_goal_set_complete_and_external_override(self) -> None:
        async def scenario() -> tuple[dict, list[dict], dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    complete_started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id,
                            "message": "prepare a complete-goal plan",
                            "client_request_id": "workflow-goal-complete-start",
                            "goal": "Run a goal to completion",
                            "goal_completion_action": "set_complete",
                            "goal_completion_objective": "Goal completed by MCP",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    complete_active = complete_started
                    for _ in range(50):
                        complete_active = await service.call("codex_get_workflow_status", {"workflow_id": complete_started["workflowId"], "refresh_live_goal": True})
                        if complete_active["threadGoal"]["syncState"] == "active":
                            break
                        await asyncio.sleep(0.01)
                    service.storage.upsert_tracked_plan_item(
                        {
                            "item_id": "plan-goal-complete",
                            "turn_id": complete_active["planTurnId"],
                            "thread_id": complete_active["threadId"],
                            "status": "completed",
                            "text": "Workflow plan",
                            "created_at": "2026-05-25T00:00:00+00:00",
                            "updated_at": "2026-05-25T00:00:01+00:00",
                            "completed_at": "2026-05-25T00:00:01+00:00",
                            "sequence": 1,
                            "payload_json": "{}",
                        }
                    )
                    service.storage.update_tracked_turn_status(
                        complete_active["planTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:02+00:00",
                        completed_at="2026-05-25T00:00:02+00:00",
                    )
                    service.codex_approve_plan({"workflow_id": complete_started["workflowId"], "client_request_id": "workflow-goal-complete-exec"})
                    complete_executing = complete_active
                    for _ in range(50):
                        complete_executing = await service.call("codex_get_workflow_status", {"workflow_id": complete_started["workflowId"], "refresh_live_goal": True})
                        if complete_executing["executionTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    service.storage.update_tracked_turn_status(
                        complete_executing["executionTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:04+00:00",
                        completed_at="2026-05-25T00:00:04+00:00",
                        final_message="Final workflow report",
                    )
                    complete_done = await service.call("codex_get_workflow_status", {"workflow_id": complete_started["workflowId"], "refresh_live_goal": True})

                    override_started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id,
                            "message": "prepare an override plan",
                            "client_request_id": "workflow-goal-override-start",
                            "goal": "Managed goal before override",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    override_active = override_started
                    for _ in range(50):
                        override_active = await service.call("codex_get_workflow_status", {"workflow_id": override_started["workflowId"], "refresh_live_goal": True})
                        if override_active["threadGoal"]["syncState"] == "active":
                            break
                        await asyncio.sleep(0.01)
                    fake.thread_goals[override_active["threadId"]]["objective"] = "External goal override"
                    override_status = await service.call("codex_get_workflow_status", {"workflow_id": override_started["workflowId"], "refresh_live_goal": True})
                    return complete_done, fake.thread_goal_set_calls, override_status, len(fake.thread_goal_clear_calls)
                finally:
                    await service.close()

        complete_done, set_calls, override_status, clear_count = asyncio.run(scenario())

        self.assertEqual("complete", complete_done["threadGoal"]["syncState"])
        self.assertEqual("complete", complete_done["threadGoal"]["currentGoal"]["status"])
        self.assertEqual("Goal completed by MCP", complete_done["threadGoal"]["currentGoal"]["objective"])
        self.assertTrue(any(call["status"] == "complete" for call in set_calls))
        self.assertEqual("external_override", override_status["threadGoal"]["syncState"])
        self.assertEqual("External goal override", override_status["threadGoal"]["currentGoal"]["objective"])
        self.assertEqual(0, clear_count)

    def test_approve_plan_output_schema_persists_structured_workflow_report(self) -> None:
        async def scenario() -> tuple[dict, list[dict], dict]:
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
                    "required": ["summary", "risk"],
                    "additionalProperties": False,
                    "properties": {
                        "summary": {"type": "string"},
                        "risk": {"type": "string"},
                    },
                }
                try:
                    started = await service.codex_start_plan_workflow(
                        {
                            "project_id": project_id,
                            "message": "prepare a plan",
                            "client_request_id": "workflow-output-schema-start",
                            "first_message_timeout_seconds": 1,
                        }
                    )
                    for _ in range(50):
                        planning = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if planning["planTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Plan operation did not produce planTurnId")
                    service.storage.upsert_tracked_plan_item(
                        {
                            "item_id": "plan-schema-1",
                            "turn_id": planning["planTurnId"],
                            "thread_id": planning["threadId"],
                            "status": "completed",
                            "text": "Workflow plan",
                            "created_at": "2026-05-25T00:00:00+00:00",
                            "updated_at": "2026-05-25T00:00:01+00:00",
                            "completed_at": "2026-05-25T00:00:01+00:00",
                            "sequence": 1,
                            "payload_json": "{}",
                        }
                    )
                    service.storage.update_tracked_turn_status(
                        planning["planTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:02+00:00",
                        completed_at="2026-05-25T00:00:02+00:00",
                    )
                    approved = service.codex_approve_plan(
                        {
                            "workflow_id": started["workflowId"],
                            "client_request_id": "workflow-output-schema-execute",
                            "output_schema": schema,
                        }
                    )
                    for _ in range(50):
                        executing = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                        if executing["executionTurnId"]:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Execution operation did not produce executionTurnId")
                    final_json = json.dumps({"summary": "Implemented", "risk": "low"})
                    service.storage.update_tracked_turn_status(
                        executing["executionTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:04+00:00",
                        completed_at="2026-05-25T00:00:04+00:00",
                        final_message=final_json,
                    )
                    completed = service.codex_get_workflow_status({"workflow_id": started["workflowId"]})
                    return approved, fake.turn_start_calls, completed
                finally:
                    await service.close()

        approved, turn_start_calls, completed = asyncio.run(scenario())

        self.assertIsNotNone(approved["executionOperationId"])
        self.assertIsNone(turn_start_calls[0].get("output_schema"))
        self.assertEqual("object", turn_start_calls[1]["output_schema"]["type"])
        self.assertEqual("completed", completed["phase"])
        self.assertEqual("Implemented", completed["finalReport"]["structured"]["summary"])
        self.assertEqual("low", completed["finalReport"]["structured"]["risk"])
        self.assertEqual("parsed", completed["finalReport"]["structuredStatus"])
        self.assertEqual("valid_json", completed["executionOperation"]["outputSchemaState"]["parseStatus"])
        self.assertEqual(completed["latestReportHash"], completed["executionOperation"]["latestReportHash"])

    def test_workflow_status_surfaces_pending_interactions_and_orphaned_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage, first_message=None)
            service._app_server = fake  # type: ignore[assignment]
            try:
                fake.tracker.register_turn(
                    turn_id="turn-plan",
                    thread_id="thread-workflow",
                    chat_id="thread-workflow",
                    project_id="project",
                    project_path=str(root),
                    status="completed",
                    process_generation=1,
                )
                fake.tracker.register_turn(
                    turn_id="turn-exec",
                    thread_id="thread-workflow",
                    chat_id="thread-workflow",
                    project_id="project",
                    project_path=str(root),
                    status="running",
                    process_generation=1,
                )
                now = "2026-05-25T00:00:00+00:00"
                service.storage.create_workflow(
                    {
                        "workflow_id": "wf-test",
                        "client_request_id": None,
                        "execution_client_request_id": None,
                        "project_id": "project",
                        "thread_id": "thread-workflow",
                        "plan_turn_id": "turn-plan",
                        "execution_turn_id": "turn-exec",
                        "phase": "executing",
                        "status": "executing",
                        "last_error": None,
                        "created_at": now,
                        "updated_at": now,
                        "completed_at": None,
                        "app_server_generation": 1,
                        "metadata_json": "{}",
                    }
                )
                service.storage.upsert_pending_interaction(
                    {
                        "interaction_id": "int-workflow",
                        "app_server_request_id": "9",
                        "method": COMMAND_APPROVAL_METHOD,
                        "thread_id": "thread-workflow",
                        "turn_id": "turn-exec",
                        "item_id": "cmd-1",
                        "status": "pending",
                        "params_json": json.dumps(
                            {
                                "threadId": "thread-workflow",
                                "turnId": "turn-exec",
                                "itemId": "cmd-1",
                                "command": "echo ok",
                            }
                        ),
                        "response_json": None,
                        "created_at": now,
                        "expires_at": "2026-05-25T00:15:00+00:00",
                        "resolved_at": None,
                        "process_generation": 1,
                        "auto_resolved": 0,
                        "last_error": None,
                    }
                )
                pending = service.codex_get_workflow_status({"workflow_id": "wf-test"})
                service.storage.mark_pending_interactions_orphaned(
                    process_generation=1,
                    reason="test app-server exit",
                    resolved_at="2026-05-25T00:00:10+00:00",
                )
                fake.tracker.mark_active_turns_unknown(process_generation=1, reason="test app-server exit")
                orphaned = service.codex_get_workflow_status({"workflow_id": "wf-test"})
            finally:
                asyncio.run(service.close())

        self.assertEqual("waiting_for_approval", pending["phase"])
        self.assertEqual("cmd-1", pending["pendingInteractions"][0]["itemId"])
        self.assertEqual("answer_pending_interaction", pending["pendingInteractions"][0]["recommendedAction"])
        self.assertEqual("approval_decision", pending["pendingInteractions"][0]["answerSchema"]["type"])
        self.assertEqual("orphaned_after_app_server_exit", orphaned["phase"])

    def test_pending_interactions_filter_by_operation_and_workflow_context(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                now = "2026-05-25T00:00:00+00:00"
                service.storage.create_operation(
                    _storage_operation_row(
                        "op-pending",
                        status="running",
                        thread_id="thread-pending-context",
                        turn_id="turn-pending-context",
                    )
                )
                service.storage.create_workflow(
                    {
                        "workflow_id": "wf-pending",
                        "client_request_id": None,
                        "project_id": "project",
                        "thread_id": "thread-pending-context",
                        "plan_turn_id": "turn-plan",
                        "execution_turn_id": "turn-pending-context",
                        "phase": "executing",
                        "status": "executing",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                service.storage.upsert_pending_interaction(
                    {
                        "interaction_id": "int-context",
                        "app_server_request_id": "7",
                        "method": COMMAND_APPROVAL_METHOD,
                        "thread_id": "thread-pending-context",
                        "turn_id": "turn-pending-context",
                        "item_id": "cmd-context",
                        "status": "pending",
                        "params_json": json.dumps(
                            {
                                "threadId": "thread-pending-context",
                                "turnId": "turn-pending-context",
                                "itemId": "cmd-context",
                                "command": "echo ok",
                                "availableDecisions": ["accept", "decline"],
                            }
                        ),
                        "response_json": None,
                        "created_at": now,
                        "expires_at": "2026-05-25T00:15:00+00:00",
                        "resolved_at": None,
                        "process_generation": 1,
                        "auto_resolved": 0,
                        "last_error": None,
                    }
                )
                by_operation = service.codex_list_pending_interactions({"operation_id": "op-pending"})
                by_workflow = service.codex_list_pending_interactions({"workflow_id": "wf-pending"})
            finally:
                asyncio.run(service.close())

        self.assertEqual(1, by_operation["returnedCount"])
        self.assertEqual("int-context", by_operation["interactions"][0]["interactionId"])
        self.assertEqual("answer_pending_interaction", by_operation["interactions"][0]["recommendedAction"])
        self.assertEqual(1, by_workflow["returnedCount"])
        self.assertEqual("thread-pending-context", by_workflow["threadId"])
