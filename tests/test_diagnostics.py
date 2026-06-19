from __future__ import annotations

from tests.helpers import *
from openclaw_codex_mcp.agent_guidance import build_guidance_for_payload, guard_key_for


class McpDefinitionTests(unittest.TestCase):
    def test_agent_guidance_builder_blocks_after_loop_guard_limit(self) -> None:
        guard_key = guard_key_for(
            category="operation_failed",
            scope_type="operation",
            scope_id="op-guidance",
            action="collect_diagnostics",
            target={},
        )

        guidance = build_guidance_for_payload(
            {
                "ok": True,
                "operationId": "op-guidance",
                "status": "failed",
                "lastError": "token=secret-value failed",
                "request": {"message": "raw prompt must not leak"},
            },
            surface="operation_status",
            attempt_lookup=lambda key: {
                "guard_key": guard_key,
                "scope_type": "operation",
                "scope_id": "op-guidance",
                "action": "collect_diagnostics",
                "attempt_count": 2,
                "cooldown_until": "2099-01-01T00:00:00+00:00",
            }
            if key == guard_key
            else None,
            now="2026-06-19T00:00:00+00:00",
        )

        self.assertIsNotNone(guidance)
        assert guidance is not None
        self.assertEqual("agent-guidance/v1", guidance["schemaVersion"])
        self.assertEqual("blocked", guidance["problemState"])
        self.assertFalse(guidance["loopGuard"]["allowed"])
        self.assertEqual("cooldown_active", guidance["loopGuard"]["blockedReason"])
        rendered = json.dumps(guidance, ensure_ascii=False)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("raw prompt must not leak", rendered)

    def test_health_summary_contains_version_contract_block(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                health = service.codex_health_summary({})
            finally:
                asyncio.run(service.close())

        version = health["version"]
        self.assertEqual("codex-control-plane-mcp", version["serverName"])
        self.assertEqual(CONTRACT_VERSION, version["contractVersion"])
        self.assertEqual(_tool_surface_hash(), version["toolSurfaceHash"])
        self.assertEqual(len(STABLE_OPENCLAW_TOOLS), version["stableToolCount"])
        self.assertEqual(len(COMPATIBILITY_TOOLS), version["compatibilityToolCount"])
        self.assertEqual(sorted(STABLE_OPENCLAW_TOOLS), version["stableTools"])
        self.assertEqual(sorted(COMPATIBILITY_TOOLS), version["compatibilityTools"])
        self.assertEqual(health["generatedAt"], version["generatedAt"])
        self.assertEqual("not_collected", health["runtimeCapabilities"]["status"])

    def test_health_summary_reports_stalled_turns_without_auto_interrupt(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-stalled-health",
                        "thread_id": "thread-stalled-health",
                        "chat_id": "thread-stalled-health",
                        "project_id": "project",
                        "project_path": str(root),
                        "status": "running",
                        "started_at": "2026-05-25T00:00:00+00:00",
                        "updated_at": "2026-05-25T00:00:00+00:00",
                        "completed_at": None,
                        "first_message_at": None,
                        "final_message": None,
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                health = service.codex_health_summary({})
            finally:
                asyncio.run(service.close())

        supervisor = health["stallSupervisor"]
        self.assertEqual("diagnose_only", supervisor["mode"])
        self.assertEqual(900, supervisor["timeoutSeconds"])
        self.assertFalse(supervisor["automaticInterruptEnabled"])
        self.assertEqual(1, supervisor["stalledTurnCount"])
        self.assertEqual("mark_stale_turns_orphaned", supervisor["nextRecommendedAction"])

    def test_pending_interaction_tool_payload_redacts_secret_answers_and_audits(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, list[dict]]:
            with TemporaryDirectory() as tmp:
                storage = McpStorage(Path(tmp) / "mcp.sqlite")
                storage.connect()
                manager = PendingInteractionManager(storage)
                try:
                    interaction = manager.create(
                        app_server_request_id=1,
                        method=TOOL_USER_INPUT_METHOD,
                        params={
                            "threadId": "thread-secret",
                            "turnId": "turn-secret",
                            "questions": [{"id": "api_token", "question": "Token?", "isSecret": True}],
                        },
                        process_generation=4,
                        timeout_seconds=900,
                    )
                    listed = manager.list_interactions(turn_id="turn-secret", status="pending", limit=10)[0]
                    answered = manager.answer(
                        str(interaction["interactionId"]),
                        {"answers": {"api_token": "super-secret-token"}},
                        current_process_generation=4,
                    )
                    row = storage.get_pending_interaction(str(interaction["interactionId"])) or {}
                    events = storage.list_pending_interaction_events(str(interaction["interactionId"]))
                    return listed, answered, row, events
                finally:
                    storage.close()

        listed, answered, row, events = asyncio.run(scenario())

        self.assertEqual("answer_pending_interaction", listed["recommendedAction"])
        self.assertEqual("question_answers", listed["answerSchema"]["type"])
        self.assertEqual(1, listed["riskSummary"]["secretQuestionCount"])
        self.assertTrue(answered["responseRedacted"])
        self.assertNotIn("super-secret-token", json.dumps(answered, ensure_ascii=False))
        self.assertNotIn("super-secret-token", row["response_json"])
        self.assertEqual("answered", row["status"])
        self.assertEqual(["answered", "created"], [event["event_type"] for event in events])

    def test_diagnostic_redaction_and_classifier(self) -> None:
        fake_bearer = "Bearer " + "abcdefghijklmnop"
        fake_openai_key = "sk-" + "1234567890abcdef"
        fake_telegram_token = "123456789:" + "abcdefghijklmnopqrstuvwxyz"
        text = f"DEEPSEEK_API_KEY=secret-value Authorization: {fake_bearer} {fake_openai_key} {fake_telegram_token}"
        redacted = redact_text(text)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)
        self.assertNotIn(fake_openai_key, redacted)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", redacted)

        payload = redact_payload({"token": "visible-secret", "nested": {"password": "pw", "safe": "ok"}})
        self.assertEqual("[redacted]", payload["token"])
        self.assertEqual("[redacted]", payload["nested"]["password"])
        self.assertEqual("ok", payload["nested"]["safe"])

        analysis = analyze_context(
            "Transport closed after timeout",
            {"checks": [], "activeWork": {"activeTurns": [], "pendingInteractions": 0}},
            {"logs": [{"message": "Codex app-server stdout closed"}], "events": []},
        )
        self.assertEqual("app_server_stdout_closed", analysis["likelyRootCause"]["category"])
        self.assertIn("restart_app_server_idle", [item["action"] for item in analysis["recommendedRepairActions"]])

        auth_analysis = analyze_context(
            "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header",
            {"checks": [], "activeWork": {"activeTurns": [], "pendingInteractions": 0}},
            {
                "logs": [],
                "events": [
                    {
                        "method": "turn/completed",
                        "payload": {
                            "error": {
                                "message": "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
                            }
                        },
                    }
                ],
            },
        )
        self.assertEqual("codex_auth_required", auth_analysis["likelyRootCause"]["category"])
        self.assertIn("reauthenticate_codex_home", [item["action"] for item in auth_analysis["recommendedRepairActions"]])

        sandbox_analysis = analyze_context(
            "windows sandbox: runner error: CreateProcessAsUserW failed: 5",
            {"checks": [], "activeWork": {"activeTurns": [], "pendingInteractions": 0}},
            {"logs": [{"message": "CreateProcessAsUserW failed: 5"}], "events": []},
        )
        self.assertEqual("windows_sandbox_spawn_denied", sandbox_analysis["likelyRootCause"]["category"])
        self.assertIn("Run codex_preflight_project_run", " ".join(sandbox_analysis["nextDiagnosticSteps"]))

        operation_analysis = analyze_context(
            "CODEX_DUPLICATE_PROMPT_ACTIVE and stale operation",
            {
                "checks": [],
                "activeWork": {"activeTurns": [], "pendingInteractions": 0},
                "staleOperations": [{"operationId": "op-stale", "status": "starting_turn"}],
                "promptSubmissions": [{"promptSubmissionId": "ps-1", "duplicateOfSubmissionId": "ps-0"}],
            },
            {"logs": [], "events": []},
        )
        categories = {item["category"] for item in operation_analysis["findings"]}
        actions = {item["action"] for item in operation_analysis["recommendedRepairActions"]}
        self.assertIn("stale_operation", categories)
        self.assertIn("duplicate_prompt", categories)
        self.assertIn("recover_stale_operations", actions)
        self.assertIn("cleanup_prompt_submissions", actions)

    def test_collect_diagnostics_and_logs_redact_events(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            log_path = root / "logs" / "server.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "2026-05-25 00:00:00,000 ERROR pid=1 openclaw_codex_mcp.test: token=secret-value stdout closed\n",
                encoding="utf-8",
            )
            old_env = os.environ.get("OPENCLAW_CODEX_MCP_LOG")
            os.environ["OPENCLAW_CODEX_MCP_LOG"] = str(log_path)
            service = ToolService(config)
            try:
                tracker = TurnTracker(service.storage)
                tracker.register_turn(
                    turn_id="turn-d",
                    thread_id="thread-d",
                    chat_id="thread-d",
                    project_id="project-d",
                    project_path=str(root),
                )
                tracker.record_event(
                    {
                        "method": "warning",
                        "params": {"threadId": "thread-d", "message": "warning token=secret-value"},
                    },
                    received_at="2026-05-25T00:00:01+00:00",
                )
                service.storage.record_app_server_event(
                    "inbound",
                    {
                        "method": "turn/error",
                        "params": {"threadId": "thread-d", "turnId": "turn-d", "message": "api_key=secret-value timeout"},
                    },
                    datetime.now(timezone.utc).isoformat(),
                    process_generation=2,
                )
                diagnostics = service.codex_collect_diagnostics({"thread_id": "thread-d", "turn_id": "turn-d", "include_logs": True})
                logs = service.codex_get_diagnostic_logs(
                    {"thread_id": "thread-d", "turn_id": "turn-d", "include_payload": True, "limit": 10}
                )
            finally:
                asyncio.run(service.close())
                if old_env is None:
                    os.environ.pop("OPENCLAW_CODEX_MCP_LOG", None)
                else:
                    os.environ["OPENCLAW_CODEX_MCP_LOG"] = old_env

        self.assertIn(diagnostics["overallStatus"], {"degraded", "broken"})
        self.assertTrue(any(item["name"] == "codex_binary" for item in diagnostics["checks"]))
        serialized = json.dumps(logs, ensure_ascii=False)
        self.assertNotIn("secret-value", serialized)
        diagnostics_serialized = json.dumps(diagnostics, ensure_ascii=False)
        self.assertNotIn("secret-value", diagnostics_serialized)
        self.assertTrue(any(item["source"] == "turn_progress" for item in diagnostics["timeline"]))
        self.assertEqual(1, diagnostics["progressJournal"]["eventCount"])
        self.assertEqual(1, len(diagnostics["progressJournal"]["warnings"]))
        self.assertEqual(1, logs["returnedEventCount"])

    def test_health_summary_and_operation_diagnostics_correlation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            old = "2026-05-25T00:00:00+00:00"
            try:
                service.storage.create_operation(
                    {
                        "operation_id": "op-health",
                        "client_request_id": "client-health",
                        "operation_type": "start_chat",
                        "status": "starting_turn",
                        "phase": "starting_turn",
                        "project_id": "project",
                        "chat_id": "thread-health",
                        "thread_id": "thread-health",
                        "turn_id": "turn-health",
                        "workflow_id": None,
                        "cwd": str(root),
                        "title": "health",
                        "request_json": json.dumps({"message": "secret normalized prompt should not leak"}),
                        "created_at": old,
                        "updated_at": old,
                    }
                )
                service.storage.create_prompt_submission(
                    {
                        "prompt_submission_id": "ps-health",
                        "project_id": "project",
                        "project_path_key": "project-key",
                        "operation_type": "start_chat",
                        "prompt_hash": "hash-health",
                        "prompt_normalized": "secret normalized prompt should not leak",
                        "prompt_preview": "safe preview",
                        "operation_id": "op-health",
                        "chat_id": "thread-health",
                        "thread_id": "thread-health",
                        "turn_id": "turn-health",
                        "workflow_id": None,
                        "status": "starting_turn",
                        "duplicate_of_submission_id": None,
                        "similarity": None,
                        "created_at": old,
                        "updated_at": old,
                    }
                )
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-health",
                        "thread_id": "thread-health",
                        "chat_id": "thread-health",
                        "project_id": "project",
                        "project_path": str(root),
                        "status": "ready",
                        "started_at": old,
                        "updated_at": old,
                        "completed_at": None,
                        "first_message_at": None,
                        "final_message": None,
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                health = service.codex_health_summary({"operation_id": "op-health", "stale_after_minutes": 1})
                diagnostics = service.codex_collect_diagnostics({"operation_id": "op-health", "include_timeline": True})
            finally:
                asyncio.run(service.close())

        self.assertEqual("recover_stale_operations", health["nextRecommendedAction"])
        self.assertTrue(any(item["operationId"] == "op-health" for item in health["staleOperations"]))
        self.assertEqual("op-health", diagnostics["filters"]["operationId"])
        self.assertEqual("op-health", diagnostics["operationSummary"]["operationId"])
        self.assertEqual("op-health", diagnostics["correlation"]["operation"]["operationId"])
        self.assertTrue(any(item["source"] == "operation" for item in diagnostics["timeline"]))
        self.assertIn(diagnostics["diagnosisConfidence"], {"medium", "high"})
        serialized = json.dumps(diagnostics, ensure_ascii=False)
        self.assertNotIn("secret normalized prompt should not leak", serialized)
        self.assertIn("hash-health", serialized)

    def test_failed_operation_status_includes_agent_guidance(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                service.storage.create_operation(
                    {
                        "operation_id": "op-guided",
                        "client_request_id": "client-guided",
                        "operation_type": "start_chat",
                        "status": "failed",
                        "phase": "failed",
                        "project_id": "project",
                        "chat_id": "thread-guided",
                        "thread_id": "thread-guided",
                        "turn_id": "turn-guided",
                        "workflow_id": None,
                        "cwd": str(root),
                        "title": "guided",
                        "request_json": json.dumps({"message": "secret prompt text"}),
                        "last_error": "token=secret-value failed",
                        "created_at": "2026-05-25T00:00:00+00:00",
                        "updated_at": "2026-05-25T00:00:00+00:00",
                        "completed_at": "2026-05-25T00:00:01+00:00",
                    }
                )
                status = service.codex_get_operation_status({"operation_id": "op-guided"})
            finally:
                asyncio.run(service.close())

        self.assertIn("agentGuidance", status)
        self.assertIn("agentGuidanceText", status)
        self.assertEqual("agent-guidance/v1", status["agentGuidance"]["schemaVersion"])
        self.assertEqual("codex_collect_diagnostics", status["agentGuidance"]["instructions"][0]["toolName"])
        self.assertEqual("op-guided", status["agentGuidance"]["loopGuard"]["scopeId"])
        rendered = json.dumps(status["agentGuidance"], ensure_ascii=False)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("secret prompt text", rendered)

    def test_redact_payload_redacts_image_url_and_local_path(self) -> None:
        payload = {
            "input": [
                {"type": "image", "url": "https://example.com/private.png?token=secret", "detail": "high"},
                {"type": "localImage", "path": r"C:\Secret\screenshot.png", "detail": "low"},
            ]
        }

        redacted = redact_payload(payload)
        rendered = json.dumps(redacted, ensure_ascii=False)

        self.assertNotIn("https://example.com/private.png?token=secret", rendered)
        self.assertNotIn(r"C:\Secret\screenshot.png", rendered)
        self.assertIn("[redacted-image-url]", rendered)
        self.assertIn("[redacted-local-image-path]", rendered)

    def test_repair_issue_force_guardrail_and_stale_turn_repair(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                force_dry = asyncio.run(service.codex_repair_issue({"action": "force_restart_app_server"}))
                with self.assertRaises(Exception):
                    asyncio.run(service.codex_repair_issue({"action": "force_restart_app_server", "dry_run": False}))
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-stale",
                        "thread_id": "thread-stale",
                        "chat_id": "thread-stale",
                        "project_id": "project",
                        "project_path": str(root),
                        "status": "running",
                        "started_at": "2026-05-25T00:00:00+00:00",
                        "updated_at": "2026-05-25T00:00:00+00:00",
                        "completed_at": None,
                        "first_message_at": None,
                        "final_message": None,
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                dry = asyncio.run(
                    service.codex_repair_issue(
                        {"action": "mark_stale_turns_orphaned", "turn_id": "turn-stale", "dry_run": True}
                    )
                )
                after_dry = service.storage.get_tracked_turn("turn-stale") or {}
                repaired = asyncio.run(
                    service.codex_repair_issue(
                        {
                            "action": "mark_stale_turns_orphaned",
                            "turn_id": "turn-stale",
                            "stale_after_minutes": 1,
                            "dry_run": False,
                        }
                    )
                )
                after = service.storage.get_tracked_turn("turn-stale") or {}
            finally:
                asyncio.run(service.close())

        self.assertTrue(force_dry["dryRun"])
        self.assertTrue(force_dry["result"]["wouldRun"])
        self.assertTrue(dry["result"]["wouldMarkOrphaned"])
        self.assertEqual("running", after_dry["status"])
        self.assertTrue(repaired["changed"])
        self.assertEqual("mark_orphaned_after_exit", repaired["action"])
        self.assertEqual("mark_stale_turns_orphaned", repaired["requestedAction"])
        self.assertEqual("unknown_after_app_server_exit", after["status"])

    def test_recover_stale_operations_repair(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            old = "2026-05-25T00:00:00+00:00"
            try:
                service.storage.create_operation(
                    {
                        "operation_id": "op-recover",
                        "client_request_id": "client-recover",
                        "operation_type": "start_chat",
                        "status": "starting_thread",
                        "phase": "starting_thread",
                        "project_id": "project",
                        "chat_id": None,
                        "thread_id": None,
                        "turn_id": None,
                        "workflow_id": None,
                        "cwd": str(root),
                        "title": "recover",
                        "request_json": "{}",
                        "created_at": old,
                        "updated_at": old,
                        "lease_owner": None,
                        "lease_expires_at": None,
                    }
                )
                dry = asyncio.run(
                    service.codex_repair_issue(
                        {"action": "recover_stale_operations", "operation_id": "op-recover", "stale_after_minutes": 1}
                    )
                )
                after_dry = service.storage.get_operation("op-recover") or {}
                repaired = asyncio.run(
                    service.codex_repair_issue(
                        {
                            "action": "recover_stale_operations",
                            "operation_id": "op-recover",
                            "stale_after_minutes": 1,
                            "dry_run": False,
                        }
                    )
                )
                after = service.storage.get_operation("op-recover") or {}
            finally:
                asyncio.run(service.close())

        self.assertTrue(dry["dryRun"])
        self.assertEqual(["op-recover"], dry["result"]["matchedOperationIds"])
        self.assertEqual("starting_thread", after_dry["status"])
        self.assertTrue(repaired["changed"])
        self.assertEqual("queued", after["status"])
        self.assertIn("postRepairGuidance", repaired)
        self.assertEqual(1, repaired["loopGuard"]["attemptCount"])
