from __future__ import annotations

from tests.helpers import *


class LiveFindingsRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_f003_broad_search_refresh_respects_small_budget(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
            try:
                result = service.codex_search_chats(
                    {
                        "query": "MCP LIVE TEST",
                        "refresh_index": True,
                        "index_time_budget_seconds": 3,
                        "limit": 10,
                    }
                )
            finally:
                await service.close()

        self.assertTrue(result["index_status"]["time_budget_exhausted"])
        self.assertEqual("retry_without_refresh_or_increase_budget", result["nextRecommendedAction"])
        self.assertFalse(result["index_status"]["refreshed"])

    async def test_f012_collect_diagnostics_agent_safe_redacts_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
            try:
                diagnostics = service.codex_collect_diagnostics({})
            finally:
                await service.close()

        serialized = json.dumps(diagnostics, ensure_ascii=False)
        self.assertNotIn(str(root), serialized)
        self.assertIn("[redacted-path:", serialized)

    async def test_f015_worker_command_timeout_is_unambiguous_terminal_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
            now = "2026-06-20T00:00:00+00:00"
            try:
                service.storage.create_worker_command(
                    command_id="cmd-timeout",
                    command_type="codex_archive_thread",
                    status="running",
                    request={"toolName": "codex_archive_thread", "arguments": {"thread_id": "thread-1"}},
                    created_at=now,
                    updated_at=now,
                )
                service.storage.update_worker_command(
                    "cmd-timeout",
                    result_json=json.dumps({"ok": True, "commandTimedOut": True, "status": "running"}, ensure_ascii=False),
                    updated_at=now,
                    last_error="Worker command timed out before completion.",
                )
                status = await service.call("codex_get_worker_command_status", {"command_id": "cmd-timeout"})
            finally:
                await service.close()

        self.assertEqual("timed_out", status["status"])
        self.assertTrue(status["commandTimedOut"])
        self.assertFalse(status["pollRecommended"])
        self.assertEqual("inspect_worker_command", status["nextRecommendedAction"])

    async def test_f026_hook_installer_windows_command_uses_call_operator(self) -> None:
        with TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            state_db = Path(tmp) / "state.sqlite"
            installed = install_hooks(codex_home=codex_home, state_db=state_db)
            payload = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            handler = payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]

        self.assertTrue(installed["ok"])
        self.assertIn("codex_control_plane_mcp.hooks.codex_sqlite_journal", handler["command"])
        self.assertTrue(handler["commandWindows"].startswith("& \""))
        self.assertIn("\" -m codex_control_plane_mcp.hooks.codex_sqlite_journal --config \"", handler["commandWindows"])

    async def test_f027_passive_workflow_status_does_not_mutate_completed_at_or_final_report(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ToolService(_search_service_config(root, root / ".codex" / "state_5.sqlite"))
            try:
                service.storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-exec",
                        "thread_id": "thread-workflow",
                        "chat_id": "thread-workflow",
                        "project_id": "project",
                        "project_path": str(root),
                        "status": "completed",
                        "started_at": "2026-06-20T00:00:00+00:00",
                        "updated_at": "2026-06-20T00:00:10+00:00",
                        "completed_at": "2026-06-20T00:00:10+00:00",
                        "first_message_at": None,
                        "final_message": "{\"summary\":\"done\"}",
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                service.storage.create_workflow(
                    {
                        "workflow_id": "wf-passive",
                        "client_request_id": "wf-passive-client",
                        "project_id": "project",
                        "thread_id": "thread-workflow",
                        "plan_turn_id": "",
                        "execution_turn_id": "turn-exec",
                        "phase": "executing",
                        "status": "executing",
                        "created_at": "2026-06-20T00:00:00+00:00",
                        "updated_at": "2026-06-20T00:00:01+00:00",
                    }
                )
                before = service.storage.get_workflow("wf-passive") or {}
                status = service.codex_get_workflow_status({"workflow_id": "wf-passive"})
                after = service.storage.get_workflow("wf-passive") or {}
            finally:
                await service.close()

        self.assertEqual(before.get("completed_at"), after.get("completed_at"))
        self.assertIsNone(after.get("final_report_json"))
        self.assertEqual("operation_or_turn_fallback", status["timestampSource"])
        self.assertIsNotNone(status["finalReport"])

    async def test_f013_analyze_no_obvious_issue_returns_no_action_guidance(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            config.codex_binary_path.write_text("", encoding="utf-8")
            (config.codex_home / "auth.json").write_text("{}", encoding="utf-8")
            config.kb_history_projects_root.mkdir(parents=True, exist_ok=True)
            install_hooks(codex_home=config.codex_home, state_db=config.state_db_path)
            service = ToolService(config)
            service._app_server = FakeAppServer(service.storage, first_message=None)  # type: ignore[assignment]
            try:
                analysis = service.codex_analyze_issue({"thread_id": "thread-empty", "include_evidence": True})
            finally:
                await service.close()

        self.assertEqual("no_obvious_issue", analysis["likelyRootCause"]["category"])
        self.assertEqual("no_action", analysis["agentGuidance"]["problemState"])
        self.assertNotIn("validate_paths_and_config", json.dumps(analysis, ensure_ascii=False))
