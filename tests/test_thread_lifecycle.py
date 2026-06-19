from __future__ import annotations

from tests.helpers import *


class McpDefinitionTests(unittest.TestCase):
    def test_pending_interaction_response_builders(self) -> None:
        self.assertEqual(
            {"decision": "accept"},
            build_response_for_answer(
                COMMAND_APPROVAL_METHOD,
                {"availableDecisions": ["accept", "decline"]},
                {"decision": "accept"},
            ),
        )
        self.assertEqual(
            {"decision": "acceptForSession"},
            build_response_for_answer(FILE_APPROVAL_METHOD, {}, {"decision": "acceptForSession"}),
        )
        self.assertEqual(
            {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["git status"]}}},
            build_response_for_answer(
                COMMAND_APPROVAL_METHOD,
                {"availableDecisions": [{"acceptWithExecpolicyAmendment": {"execpolicy_amendment": []}}, "decline"]},
                {"decision_payload": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["git status"]}}},
            ),
        )
        self.assertEqual(
            {"answers": {"q1": {"answers": ["single"]}, "q2": {"answers": ["a", "b"]}}},
            build_response_for_answer(
                TOOL_USER_INPUT_METHOD,
                {},
                {"answers": {"q1": "single", "q2": ["a", "b"]}},
            ),
        )
        self.assertEqual(
            {"action": "accept", "content": {"value": 1}, "_meta": None},
            build_response_for_answer(MCP_ELICITATION_METHOD, {}, {"action": "accept", "content": {"value": 1}}),
        )
        self.assertEqual(
            {"permissions": {"filesystem": ["write"]}, "scope": "session", "strictAutoReview": True},
            build_response_for_answer(
                PERMISSIONS_APPROVAL_METHOD,
                {},
                {"permissions": {"filesystem": ["write"]}, "scope": "session", "strict_auto_review": True},
            ),
        )
        self.assertEqual({"permissions": {}, "scope": "turn"}, default_response_for_method(PERMISSIONS_APPROVAL_METHOD, {}))
        with self.assertRaises(CodexMcpError) as raised:
            build_response_for_answer(
                TOOL_USER_INPUT_METHOD,
                {"questions": [{"id": "q1", "question": "Value?"}]},
                {"answers": {"q2": "unexpected"}},
            )
        self.assertEqual("INVALID_ARGUMENT", raised.exception.code)

    def test_pending_interaction_tools_list_and_answer_live_request(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                fake.tracker.register_turn(
                    turn_id="turn-pending",
                    thread_id="thread-pending",
                    chat_id="thread-pending",
                    project_id="project",
                    project_path=str(root),
                    process_generation=1,
                )
                interaction = fake.interactions.create(
                    app_server_request_id=42,
                    method=COMMAND_APPROVAL_METHOD,
                    params={
                        "threadId": "thread-pending",
                        "turnId": "turn-pending",
                        "itemId": "item-1",
                        "command": "echo ok",
                        "availableDecisions": ["accept", "decline"],
                    },
                    process_generation=1,
                    timeout_seconds=900,
                )
                fake.tracker.mark_pending_interaction(COMMAND_APPROVAL_METHOD, interaction["params"])
                try:
                    listed = service.codex_list_pending_interactions({"thread_id": "thread-pending"})
                    turn_status = service.codex_get_turn_status({"turn_id": "turn-pending"})
                    answered = await service.codex_answer_pending_interaction(
                        {"interaction_id": interaction["interactionId"], "decision": "accept"}
                    )
                    row = service.storage.get_pending_interaction(str(interaction["interactionId"])) or {}
                    after_default = service.codex_list_pending_interactions({"thread_id": "thread-pending"})
                    after_answered = service.codex_list_pending_interactions({"thread_id": "thread-pending", "status": "answered"})
                    return interaction, listed, turn_status, {"answered": answered, "row": row}, {
                        "default": after_default,
                        "answered": after_answered,
                    }
                finally:
                    await service.close()

        interaction, listed, turn_status, result, after = asyncio.run(scenario())

        self.assertEqual("pending", interaction["status"])
        self.assertEqual(1, listed["returned_count"])
        self.assertEqual("waiting_for_approval", turn_status["status"])
        self.assertEqual("item-1", turn_status["pendingInteractions"][0]["itemId"])
        self.assertTrue(result["answered"]["answered"])
        self.assertEqual("answered", result["row"]["status"])
        self.assertEqual(0, after["default"]["returnedCount"])
        self.assertEqual(1, after["answered"]["returnedCount"])

    def test_pending_interaction_timeout_auto_declines(self) -> None:
        async def scenario() -> tuple[dict, dict, list[dict], str]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                storage = McpStorage(root / "mcp.sqlite")
                storage.connect()
                manager = PendingInteractionManager(storage)
                try:
                    interaction = manager.create(
                        app_server_request_id=1,
                        method=COMMAND_APPROVAL_METHOD,
                        params={
                            "threadId": "thread-timeout",
                            "turnId": "turn-timeout",
                            "availableDecisions": ["decline"],
                        },
                        process_generation=1,
                        timeout_seconds=1,
                    )
                    response = await manager.wait_for_response(str(interaction["interactionId"]), timeout_seconds=1)
                    row = storage.get_pending_interaction(str(interaction["interactionId"])) or {}
                    events = storage.list_pending_interaction_events(str(interaction["interactionId"]))
                    try:
                        manager.answer(str(interaction["interactionId"]), {"decision": "decline"}, current_process_generation=1)
                    except CodexMcpError as exc:
                        error_code = exc.code
                    else:
                        error_code = ""
                    return response, row, events, error_code
                finally:
                    storage.close()

        response, row, events, error_code = asyncio.run(scenario())

        self.assertEqual({"decision": "decline"}, response)
        self.assertEqual("auto_declined", row["status"])
        self.assertEqual(1, row["auto_resolved"])
        self.assertEqual("CODEX_PENDING_INTERACTION_UNAVAILABLE", error_code)
        self.assertEqual(["auto_declined", "created"], [event["event_type"] for event in events])

    def test_pending_interaction_orphaned_after_app_server_exit(self) -> None:
        async def scenario() -> dict:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                storage = McpStorage(root / "mcp.sqlite")
                storage.connect()
                manager = PendingInteractionManager(storage)
                try:
                    interaction = manager.create(
                        app_server_request_id=1,
                        method=TOOL_USER_INPUT_METHOD,
                        params={"threadId": "thread-exit", "turnId": "turn-exit", "questions": [{"id": "q1", "question": "Value?"}]},
                        process_generation=3,
                        timeout_seconds=900,
                    )
                    manager.orphan_live(process_generation=3, reason="test exit")
                    row = storage.get_pending_interaction(str(interaction["interactionId"])) or {}
                    events = storage.list_pending_interaction_events(str(interaction["interactionId"]))
                    row["event_types"] = [event["event_type"] for event in events]
                    return row
                finally:
                    storage.close()

        row = asyncio.run(scenario())

        self.assertEqual("orphaned_after_app_server_exit", row["status"])
        self.assertEqual("test exit", row["last_error"])
        self.assertEqual(["orphaned", "created"], row["event_types"])

    def test_thread_lifecycle_archive_and_unarchive_are_audited(self) -> None:
        async def scenario() -> tuple[dict, dict, int, int, int, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                service = ToolService(config)
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                fake.tracker.register_turn(
                    turn_id="turn-life",
                    thread_id="thread-life",
                    chat_id="thread-life",
                    project_id=project_id,
                    project_path=str(project),
                    status="completed",
                )
                refresh_count = 0
                original_refresh = service.catalog.refresh

                def counted_refresh() -> None:
                    nonlocal refresh_count
                    refresh_count += 1
                    original_refresh()

                service.catalog.refresh = counted_refresh  # type: ignore[method-assign]
                try:
                    archived = await service.call(
                        "codex_archive_thread",
                        {"thread_id": "thread-life", "project_id": project_id, "timeout_seconds": 7},
                    )
                    unarchived = await service.call(
                        "codex_unarchive_thread",
                        {"thread_id": "thread-life", "project_id": project_id, "timeout_seconds": 8},
                    )
                    actions = service.storage.list_thread_lifecycle_actions(thread_id="thread-life", limit=10)
                    return (
                        archived,
                        unarchived,
                        refresh_count,
                        len(fake.thread_archive_calls),
                        len(fake.thread_unarchive_calls),
                        actions,
                    )
                finally:
                    await service.close()

        archived, unarchived, refresh_count, archive_calls, unarchive_calls, actions = asyncio.run(scenario())

        self.assertEqual("archive", archived["actionType"])
        self.assertEqual("completed", archived["status"])
        self.assertFalse(archived["pollRecommended"])
        self.assertTrue(archived["threadState"]["archived"])
        self.assertEqual("unarchive", unarchived["actionType"])
        self.assertEqual("completed", unarchived["status"])
        self.assertFalse(unarchived["threadState"]["archived"])
        self.assertEqual(1, archive_calls)
        self.assertEqual(1, unarchive_calls)
        self.assertGreaterEqual(refresh_count, 2)
        self.assertEqual(["completed", "completed"], sorted(item["status"] for item in actions))

    def test_thread_lifecycle_resolves_fork_only_thread_from_operation(self) -> None:
        async def scenario() -> tuple[dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                service.storage.create_operation(
                    _storage_operation_row(
                        "op-fork-only",
                        status="completed",
                        operation_type="fork_thread",
                        thread_id="thread-fork-only",
                        cwd=str(project),
                        request={"operation_type": "fork_thread", "source_thread_id": "thread-source"},
                    )
                )
                service.storage.update_operation("op-fork-only", project_id=project_id, chat_id="thread-fork-only")
                try:
                    archived = await service.call(
                        "codex_archive_thread",
                        {"thread_id": "thread-fork-only", "project_id": project_id},
                    )
                    return archived, fake.thread_archive_calls
                finally:
                    await service.close()

        archived, archive_calls = asyncio.run(scenario())

        self.assertEqual("completed", archived["status"])
        self.assertEqual("operation", archived["threadState"]["source"])
        self.assertEqual(1, len(archive_calls))
        self.assertNotIn("AppData", json.dumps(archived, ensure_ascii=False))

    def test_thread_lifecycle_rejects_missing_or_busy_thread(self) -> None:
        async def scenario() -> tuple[dict, dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                fake.tracker.register_turn(
                    turn_id="turn-busy",
                    thread_id="thread-busy",
                    chat_id="thread-busy",
                    project_id=project_id_for_path(str(project)),
                    project_path=str(project),
                    status="running",
                )
                try:
                    missing = await service.call("codex_archive_thread", {"thread_id": "thread-missing"})
                    busy_result = await service.call("codex_archive_thread", {"thread_id": "thread-busy"})
                    return missing, busy_result, len(fake.thread_archive_calls)
                finally:
                    await service.close()

        missing, busy_result, archive_calls = asyncio.run(scenario())

        self.assertEqual("CODEX_THREAD_NOT_FOUND", missing["error"]["code"])
        self.assertEqual("CODEX_BUSY", busy_result["error"]["code"])
        self.assertEqual(0, archive_calls)

    def test_thread_compaction_status_completes_from_app_server_event(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, int]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                fake.tracker.register_turn(
                    turn_id="turn-compact",
                    thread_id="thread-compact",
                    chat_id="thread-compact",
                    project_id=project_id_for_path(str(project)),
                    project_path=str(project),
                    status="completed",
                )
                try:
                    started = await service.call("codex_start_thread_compaction", {"thread_id": "thread-compact"})
                    running = await service.call(
                        "codex_get_thread_compaction_status",
                        {"action_id": started["actionId"], "include_events": True},
                    )
                    service.storage.record_app_server_event(
                        "inbound",
                        {
                            "method": "thread/compacted",
                            "params": {"threadId": "thread-compact", "turnId": "turn-compact-result"},
                        },
                        datetime.now(timezone.utc).isoformat(),
                        process_generation=1,
                    )
                    completed = await service.call(
                        "codex_get_thread_compaction_status",
                        {"action_id": started["actionId"]},
                    )
                    return started, running, completed, len(fake.thread_compact_start_calls)
                finally:
                    await service.close()

        started, running, completed, compact_calls = asyncio.run(scenario())

        self.assertEqual("compact", started["actionType"])
        self.assertEqual("running", started["status"])
        self.assertTrue(started["pollRecommended"])
        self.assertEqual("poll_thread_compaction", started["nextRecommendedAction"])
        self.assertEqual("running", running["status"])
        self.assertIn("events", running)
        self.assertEqual("completed", completed["status"])
        self.assertFalse(completed["pollRecommended"])
        self.assertEqual("turn-compact-result", completed["targetTurnId"])
        self.assertEqual(1, compact_calls)

    def test_thread_compaction_unknown_after_app_server_exit(self) -> None:
        async def scenario() -> dict:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                fake.tracker.register_turn(
                    turn_id="turn-compact-exit",
                    thread_id="thread-compact-exit",
                    chat_id="thread-compact-exit",
                    project_id=project_id_for_path(str(project)),
                    project_path=str(project),
                    status="completed",
                )
                try:
                    started = await service.call("codex_start_thread_compaction", {"thread_id": "thread-compact-exit"})
                    fake.process.returncode = 1
                    return await service.call(
                        "codex_get_thread_compaction_status",
                        {"action_id": started["actionId"]},
                    )
                finally:
                    await service.close()

        status = asyncio.run(scenario())

        self.assertEqual("unknown_after_app_server_exit", status["status"])
        self.assertEqual("inspect_diagnostics", status["nextRecommendedAction"])
        self.assertFalse(status["pollRecommended"])

    def test_thread_lifecycle_app_server_error_creates_failed_action(self) -> None:
        async def scenario() -> tuple[dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
                fake = FakeAppServer(service.storage, first_message=None)
                service._app_server = fake  # type: ignore[assignment]
                fake.tracker.register_turn(
                    turn_id="turn-fail-life",
                    thread_id="thread-fail-life",
                    chat_id="thread-fail-life",
                    project_id=project_id_for_path(str(project)),
                    project_path=str(project),
                    status="completed",
                )

                async def fail_archive(thread_id: str, timeout_seconds: float | None = 30) -> dict:
                    fake_key = "sk-" + "redacted-test-token"
                    fake.thread_archive_calls.append({"thread_id": thread_id, "timeout_seconds": timeout_seconds})
                    raise RuntimeError(f"archive failed with token {fake_key}")

                fake.thread_archive = fail_archive  # type: ignore[method-assign]
                try:
                    result = await service.call("codex_archive_thread", {"thread_id": "thread-fail-life"})
                    actions = service.storage.list_thread_lifecycle_actions(thread_id="thread-fail-life")
                    return result, actions
                finally:
                    await service.close()

        result, actions = asyncio.run(scenario())

        self.assertEqual("CODEX_SEND_FAILED", result["error"]["code"])
        self.assertEqual(1, len(actions))
        self.assertEqual("failed", actions[0]["status"])
        self.assertNotIn("sk-" + "redacted-test-token", actions[0]["last_error"])

    def test_interrupt_turn_marks_tracked_turn_interrupted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage, first_message=None)
            service._app_server = fake  # type: ignore[assignment]
            fake.tracker.register_turn(
                turn_id="turn-interrupt",
                thread_id="thread-interrupt",
                chat_id="thread-interrupt",
                project_id="project",
                project_path=str(root),
                process_generation=1,
            )
            try:
                result = asyncio.run(
                    service.codex_interrupt_turn(
                        {
                            "thread_id": "thread-interrupt",
                            "turn_id": "turn-interrupt",
                        }
                    )
                )
                status = service.codex_get_turn_status({"turn_id": "turn-interrupt"})
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["interrupted"])
        self.assertEqual("interrupted", status["status"])
        self.assertTrue(status["completionObserved"])

    def test_interrupt_turn_resolves_operation_context(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage, first_message=None)
            service._app_server = fake  # type: ignore[assignment]
            fake.tracker.register_turn(
                turn_id="turn-op-interrupt",
                thread_id="thread-op-interrupt",
                chat_id="thread-op-interrupt",
                project_id="project",
                project_path=str(root),
                process_generation=1,
            )
            service.storage.create_operation(
                _storage_operation_row(
                    "op-interrupt",
                    status="running",
                    thread_id="thread-op-interrupt",
                    turn_id="turn-op-interrupt",
                )
            )
            try:
                result = asyncio.run(service.codex_interrupt_turn({"operation_id": "op-interrupt"}))
                operation = service.storage.get_operation("op-interrupt") or {}
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["interrupted"])
        self.assertEqual("operation", result["interruptedTarget"]["source"])
        self.assertEqual("turn-op-interrupt", result["turnId"])
        self.assertEqual("interrupted", operation["status"])
