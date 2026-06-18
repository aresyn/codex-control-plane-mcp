from __future__ import annotations

from tests.helpers import *


class McpDefinitionTests(unittest.TestCase):
    def test_review_workflow_validation_errors_are_structured(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                try:
                    missing_context = await service.call("codex_start_review_workflow", {"target_type": "uncommitted_changes"})
                    missing_branch = await service.call(
                        "codex_start_review_workflow",
                        {"project_id": project_id_for_path(str(project)), "target_type": "base_branch"},
                    )
                    missing_instructions = await service.call(
                        "codex_start_review_workflow",
                        {"project_id": project_id_for_path(str(project)), "target_type": "custom"},
                    )
                    unknown_thread = await service.call(
                        "codex_start_review_workflow",
                        {"thread_id": "thread-missing", "target_type": "uncommitted_changes"},
                    )
                    service.storage.upsert_tracked_turn(
                        {
                            "turn_id": "turn-active-review",
                            "thread_id": "thread-active-review",
                            "chat_id": "thread-active-review",
                            "project_id": project_id_for_path(str(project)),
                            "project_path": str(project),
                            "status": "running",
                            "started_at": "2026-05-25T00:00:00+00:00",
                            "updated_at": "2026-05-25T00:00:01+00:00",
                            "completed_at": None,
                            "first_message_at": None,
                            "final_message": None,
                            "last_error": None,
                            "source": "app_server",
                        }
                    )
                    busy_thread = await service.call(
                        "codex_start_review_workflow",
                        {"thread_id": "thread-active-review", "target_type": "uncommitted_changes"},
                    )
                    return missing_context, missing_branch, missing_instructions, unknown_thread, busy_thread
                finally:
                    await service.close()

        missing_context, missing_branch, missing_instructions, unknown_thread, busy_thread = asyncio.run(scenario())

        self.assertEqual("INVALID_ARGUMENT", missing_context["error"]["code"])
        self.assertEqual("INVALID_ARGUMENT", missing_branch["error"]["code"])
        self.assertEqual("INVALID_ARGUMENT", missing_instructions["error"]["code"])
        self.assertEqual("CODEX_THREAD_NOT_FOUND", unknown_thread["error"]["code"])
        self.assertEqual("CODEX_BUSY", busy_thread["error"]["code"])

    def test_review_workflow_existing_thread_detached_is_idempotent_and_reports(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, list[dict], int, int, object]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                sessions = root / ".codex" / "sessions"
                sessions.mkdir(parents=True)
                transcript = sessions / "rollout-review-source.jsonl"
                _write_transcript(transcript, "thread-source", project, [("user", "source request")])
                state_db = root / ".codex" / "state_5.sqlite"
                _create_threads_db(
                    state_db,
                    [
                        {
                            "id": "thread-source",
                            "rollout_path": str(transcript),
                            "cwd": str(project),
                            "title": "Review source",
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
                    started = await service.call(
                        "codex_start_review_workflow",
                        {
                            "thread_id": "thread-source",
                            "target_type": "base_branch",
                            "base_branch": "main",
                            "client_request_id": "review-client-1",
                        },
                    )
                    active = started
                    for _ in range(50):
                        active = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"]})
                        if active.get("reviewTurnId"):
                            break
                        await asyncio.sleep(0.01)
                    repeated = await service.call(
                        "codex_start_review_workflow",
                        {
                            "thread_id": "thread-source",
                            "target_type": "base_branch",
                            "base_branch": "main",
                            "client_request_id": "review-client-1",
                        },
                    )
                    service.storage.update_tracked_turn_status(
                        active["reviewTurnId"],
                        status="completed",
                        updated_at="2026-05-25T00:00:04+00:00",
                        completed_at="2026-05-25T00:00:04+00:00",
                        final_message="Review report: no issues found.",
                    )
                    completed = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"]})
                    prompt_submission = service.storage.get_prompt_submission_by_operation(str(started["reviewOperationId"]))
                    return (
                        started,
                        active,
                        repeated,
                        completed,
                        fake.review_start_calls,
                        len(fake.thread_start_calls),
                        len(fake.turn_start_calls),
                        prompt_submission,
                    )
                finally:
                    await service.close()

        started, active, repeated, completed, review_calls, thread_starts, turn_starts, prompt_submission = asyncio.run(scenario())

        self.assertEqual("code_review", started["workflowKind"])
        self.assertEqual("queued", started["phase"])
        self.assertEqual("review_start", active["reviewOperation"]["operationType"])
        self.assertEqual("reviewing", active["phase"])
        self.assertEqual("wait_review", active["nextRecommendedAction"])
        self.assertEqual("thread-source", active["reviewSourceThreadId"])
        self.assertEqual("thread-review", active["reviewThreadId"])
        self.assertEqual("turn-review", active["reviewTurnId"])
        self.assertEqual("baseBranch", active["reviewTarget"]["type"])
        self.assertEqual("main", active["reviewTarget"]["branch"])
        self.assertEqual("detached", active["reviewDelivery"])
        self.assertTrue(active["reviewOperation"]["reviewState"]["accepted"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(started["workflowId"], repeated["workflowId"])
        self.assertEqual(1, len(review_calls))
        self.assertEqual("thread-source", review_calls[0]["thread_id"])
        self.assertEqual({"type": "baseBranch", "branch": "main"}, review_calls[0]["target"])
        self.assertEqual("detached", review_calls[0]["delivery"])
        self.assertEqual(0, thread_starts)
        self.assertEqual(0, turn_starts)
        self.assertIsNone(prompt_submission)
        self.assertEqual("completed", completed["phase"])
        self.assertEqual("read_review_report", completed["nextRecommendedAction"])
        self.assertFalse(completed["pollRecommended"])
        self.assertEqual("Review report: no issues found.", completed["finalReport"]["text"])
        self.assertIsNotNone(completed["latestReportHash"])
        self.assertEqual("completed", completed["reviewOperation"]["status"])

    def test_review_workflow_project_path_starts_source_thread_then_inline_review(self) -> None:
        async def scenario() -> tuple[dict, list[dict], list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.call(
                        "codex_start_review_workflow",
                        {
                            "project_id": project_id_for_path(str(project)),
                            "target_type": "uncommitted_changes",
                            "client_request_id": "review-project-1",
                        },
                    )
                    active = started
                    for _ in range(50):
                        active = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"]})
                        if active.get("reviewTurnId"):
                            break
                        await asyncio.sleep(0.01)
                    return active, fake.thread_start_calls, fake.review_start_calls
                finally:
                    await service.close()

        active, thread_start_calls, review_start_calls = asyncio.run(scenario())

        self.assertEqual("reviewing", active["phase"])
        self.assertEqual("thread-new", active["reviewSourceThreadId"])
        self.assertEqual("thread-new", active["reviewThreadId"])
        self.assertEqual("turn-review", active["reviewTurnId"])
        self.assertEqual("inline", active["reviewDelivery"])
        self.assertEqual(1, len(thread_start_calls))
        self.assertEqual(1, len(review_start_calls))
        self.assertEqual("thread-new", review_start_calls[0]["thread_id"])
        self.assertEqual({"type": "uncommittedChanges"}, review_start_calls[0]["target"])
        self.assertEqual("inline", review_start_calls[0]["delivery"])

    def test_review_workflow_loads_final_report_from_thread_history(self) -> None:
        async def scenario() -> dict:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                sessions = root / ".codex" / "sessions"
                sessions.mkdir(parents=True)
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.call(
                        "codex_start_review_workflow",
                        {
                            "project_id": project_id_for_path(str(project)),
                            "target_type": "uncommitted_changes",
                            "client_request_id": "review-history-1",
                        },
                    )
                    active = started
                    for _ in range(50):
                        active = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"]})
                        if active.get("reviewTurnId"):
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual("turn-review", active["reviewTurnId"])
                    _write_transcript(
                        sessions / "rollout-review-history.jsonl",
                        "thread-new",
                        project,
                        [
                            ("user", "Review the current code changes."),
                            ("assistant", "Review report: no blocking issues."),
                        ],
                    )
                    return await service.call(
                        "codex_get_workflow_status",
                        {"workflow_id": started["workflowId"], "last_messages": 5, "message_max_chars": 4000},
                    )
                finally:
                    await service.close()

        completed = asyncio.run(scenario())

        self.assertEqual("completed", completed["phase"])
        self.assertEqual("read_review_report", completed["nextRecommendedAction"])
        self.assertEqual("thread-new-turn", completed["reviewTurnId"])
        self.assertEqual("Review report: no blocking issues.", completed["finalReport"]["text"])
        self.assertEqual("completed", completed["reviewOperation"]["status"])
        self.assertEqual("thread-new-turn", completed["reviewOperation"]["turnId"])
        self.assertEqual("thread-new-turn", completed["reviewOperation"]["reviewState"]["reviewTurnId"])
        self.assertEqual("completed", completed["reviewTurn"]["status"])

    def test_review_workflow_review_start_error_fails_workflow(self) -> None:
        async def scenario() -> tuple[dict, dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                fake.review_start_failure = RuntimeError("review rejected")
                service._app_server = fake  # type: ignore[assignment]
                try:
                    started = await service.call(
                        "codex_start_review_workflow",
                        {
                            "project_id": project_id_for_path(str(project)),
                            "target_type": "uncommitted_changes",
                            "client_request_id": "review-error-1",
                        },
                    )
                    status = started
                    for _ in range(50):
                        status = await service.call("codex_get_workflow_status", {"workflow_id": started["workflowId"]})
                        if status.get("status") == "failed":
                            break
                        await asyncio.sleep(0.01)
                    return started, status, fake.review_start_calls
                finally:
                    await service.close()

        started, failed, review_start_calls = asyncio.run(scenario())

        self.assertEqual("code_review", started["workflowKind"])
        self.assertEqual("failed", failed["phase"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("inspect_diagnostics", failed["nextRecommendedAction"])
        self.assertIn("review rejected", failed["lastError"])
        self.assertTrue(failed["reviewOperation"]["reviewState"]["startAttempted"])
        self.assertEqual(0, len(review_start_calls))

    def test_startup_recovery_marks_attempted_review_start_unknown_without_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                operation_id = "op-review-attempted"
                storage.create_operation(
                    _storage_operation_row(
                        operation_id,
                        status="starting_review",
                        operation_type="review_start",
                        request={
                            "operation_type": "review_start",
                            "workflow_id": "wf-review-attempted",
                            "_review_start_attempted": True,
                            "_review_source_thread_id": "thread-source",
                            "_operation_id": operation_id,
                        },
                    )
                )
                recovered = storage.recover_startup_operations(now="2026-05-25T00:05:00+00:00")
                stored = storage.get_operation(operation_id) or {}
            finally:
                storage.close()

        self.assertEqual([operation_id], recovered["unknownOperationIds"])
        self.assertEqual("unknown_after_app_server_exit", stored["status"])
        self.assertIn("review/start attempt", stored["last_error"])
