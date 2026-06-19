from __future__ import annotations

from tests.helpers import *


class ConfigDefaultsTests(unittest.TestCase):
    def test_public_defaults_are_base_dir_scoped_without_private_path_literals(self) -> None:
        keys = [
            "CODEX_HOME",
            "CODEX_PROJECTS_ROOT",
            "CODEX_PROJECTS_REGISTRY",
            "CODEX_KB_HISTORY_PROJECTS_ROOT",
            "CODEX_ALLOWED_ROOTS",
            "CODEX_MCP_STATE_DB",
            "CODEX_MCP_DEFAULT_APPROVAL_POLICY",
            "CODEX_MCP_DEFAULT_SANDBOX",
            "CODEX_MCP_DEFAULT_SANDBOX_POLICY",
            "CODEX_CONTROL_PLANE_MCP_CONFIG",
            "OPENCLAW_CODEX_MCP_CONFIG",
            "DEEPSEEK_ENV_PATH",
        ]
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = ServerConfig.load(root)
                self.assertEqual(root, config.projects_root)
                self.assertEqual(root / "projects.json", config.projects_registry_path)
                self.assertEqual(root / "_kb_history" / "projects", config.kb_history_projects_root)
                self.assertEqual([root], config.allowed_roots)
                self.assertEqual(root / "state" / "codex-mcp-state.sqlite3", config.state_db_path)
                self.assertEqual("on-request", config.default_approval_policy)
                self.assertEqual({"type": "readOnly"}, config.default_sandbox_policy)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        source = Path("openclaw_codex_mcp/config.py").read_text(encoding="utf-8")
        self.assertNotIn("C:\\Users\\", source)
        self.assertNotIn("D:\\", source)

    def test_write_policy_defaults_are_loaded_from_env_and_are_overridable(self) -> None:
        keys = [
            "CODEX_MCP_DEFAULT_APPROVAL_POLICY",
            "CODEX_MCP_DEFAULT_SANDBOX",
            "CODEX_MCP_DEFAULT_SANDBOX_POLICY",
            "CODEX_CONTROL_PLANE_MCP_CONFIG",
            "OPENCLAW_CODEX_MCP_CONFIG",
        ]
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                os.environ["CODEX_MCP_DEFAULT_APPROVAL_POLICY"] = "on-request"
                os.environ["CODEX_MCP_DEFAULT_SANDBOX"] = "workspace-write"
                config = ServerConfig.load(root)
                self.assertEqual("on-request", config.default_approval_policy)
                self.assertEqual({"type": "workspaceWrite"}, config.default_sandbox_policy)

                row = SimpleNamespace(approval_mode="never", sandbox_policy={"type": "dangerFullAccess"})
                self.assertEqual("never", _approval_policy_for_send("never", row, config.default_approval_policy))
                self.assertEqual({"type": "readOnly"}, _sandbox_policy_for_send("read-only", row, config.default_sandbox_policy))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_public_config_env_alias_wins_over_legacy_alias(self) -> None:
        keys = ["CODEX_CONTROL_PLANE_MCP_CONFIG", "OPENCLAW_CODEX_MCP_CONFIG"]
        previous = {key: os.environ.get(key) for key in keys}
        try:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                legacy = root / "legacy.json"
                public = root / "public.json"
                legacy.write_text(json.dumps({"projects_root": str(root / "legacy")}), encoding="utf-8")
                public.write_text(json.dumps({"projects_root": str(root / "public")}), encoding="utf-8")
                os.environ["OPENCLAW_CODEX_MCP_CONFIG"] = str(legacy)
                os.environ["CODEX_CONTROL_PLANE_MCP_CONFIG"] = str(public)

                config = ServerConfig.load(root)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(root / "public", config.projects_root)


class PackagingCompatibilityTests(unittest.TestCase):
    def test_pyproject_exposes_public_and_legacy_console_scripts(self) -> None:
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        scripts = pyproject["project"]["scripts"]

        self.assertEqual("codex-control-plane-mcp", pyproject["project"]["name"])
        self.assertEqual("codex_control_plane_mcp.server:main", scripts["codex-control-plane-mcp"])
        self.assertEqual("codex_control_plane_mcp.hook_installer:main", scripts["codex-control-plane-mcp-hooks"])
        self.assertEqual("codex_control_plane_mcp.admin:main", scripts["codex-control-plane-mcp-admin"])
        self.assertEqual("openclaw_codex_mcp.server:main", scripts["openclaw-codex-mcp"])
        self.assertEqual("openclaw_codex_mcp.hook_installer:main", scripts["openclaw-codex-mcp-hooks"])

    def test_public_shim_package_delegates_entrypoints(self) -> None:
        import codex_control_plane_mcp
        import codex_control_plane_mcp.hook_installer as public_hooks
        import codex_control_plane_mcp.server as public_server
        import openclaw_codex_mcp
        import openclaw_codex_mcp.hook_installer as legacy_hooks
        import openclaw_codex_mcp.server as legacy_server

        self.assertEqual(openclaw_codex_mcp.__version__, codex_control_plane_mcp.__version__)
        self.assertIs(public_server.main, legacy_server.main)
        self.assertIs(public_hooks.main, legacy_hooks.main)


class McpDefinitionTests(unittest.TestCase):
    def test_required_tools_are_registered(self) -> None:
        names = {tool["name"] for tool in TOOLS}
        self.assertEqual(
            {
                "codex_list_projects",
                "codex_list_project_chats",
                "codex_list_active_chats",
                "codex_search_chats",
                "codex_get_chat_status",
                "codex_get_chat",
                "codex_send_message",
                "codex_start_chat",
                "codex_submit_task",
                "codex_get_operation_status",
                "codex_start_plan_workflow",
                "codex_start_review_workflow",
                "codex_get_workflow_status",
                "codex_adopt_workflow_plan",
                "codex_approve_plan",
                "codex_preflight_project_run",
                "codex_get_turn_status",
                "codex_execute_plan",
                "codex_list_pending_interactions",
                "codex_answer_pending_interaction",
                "codex_interrupt_turn",
                "codex_archive_thread",
                "codex_unarchive_thread",
                "codex_start_thread_compaction",
                "codex_get_thread_compaction_status",
                "codex_restart_app_server",
                "codex_get_app_server_status",
                "codex_get_runtime_capabilities",
                "codex_health_summary",
                "codex_collect_diagnostics",
                "codex_get_diagnostic_logs",
                "codex_analyze_issue",
                "codex_repair_issue",
            },
            names,
        )
        for tool in TOOLS:
            self.assertIn("outputSchema", tool)
            self.assertEqual(["ok"], tool["outputSchema"]["required"])
            self.assertIn((tool.get("annotations") or {}).get("openclawContractGroup"), {"stable", "compatibility"})
        self.assertEqual(set(), {tool["name"] for tool in TOOLS} - STABLE_OPENCLAW_TOOLS - COMPATIBILITY_TOOLS)
        self.assertEqual(set(), STABLE_OPENCLAW_TOOLS & COMPATIBILITY_TOOLS)
        self.assertEqual(64, len(_tool_surface_hash()))

    def test_write_tools_return_fast_ack_schema(self) -> None:
        by_name = {tool["name"]: tool for tool in TOOLS}

        send_schema = by_name["codex_send_message"]["inputSchema"]["properties"]
        self.assertNotIn("wait_for_completion", send_schema)
        self.assertNotIn("return_events", send_schema)
        self.assertEqual(0, send_schema["first_message_timeout_seconds"]["default"])
        self.assertEqual(300, send_schema["timeout_seconds"]["default"])

        start_schema = by_name["codex_start_chat"]["inputSchema"]["properties"]
        self.assertNotIn("wait_for_completion", start_schema)
        self.assertNotIn("return_events", start_schema)
        self.assertEqual(0, start_schema["first_message_timeout_seconds"]["default"])
        self.assertEqual(300, start_schema["timeout_seconds"]["default"])
        self.assertEqual("on-request", start_schema["approval_policy"]["default"])
        self.assertIn("ask_openclaw", start_schema["approval_policy"]["enum"])
        self.assertIn("plan", start_schema["collaboration_mode"]["enum"])
        self.assertEqual("read-only", start_schema["sandbox"]["default"])

        self.assertEqual("on-request", send_schema["approval_policy"]["default"])
        self.assertIn("ask_openclaw", send_schema["approval_policy"]["enum"])
        self.assertIn("plan", send_schema["collaboration_mode"]["enum"])
        self.assertEqual("read-only", send_schema["sandbox"]["default"])

        submit_schema = by_name["codex_submit_task"]["inputSchema"]
        self.assertEqual(["operation_type"], submit_schema["required"])
        self.assertIn("start_chat", submit_schema["properties"]["operation_type"]["enum"])
        self.assertIn("send_message", submit_schema["properties"]["operation_type"]["enum"])
        self.assertIn("execute_plan", submit_schema["properties"]["operation_type"]["enum"])
        self.assertIn("steer_turn", submit_schema["properties"]["operation_type"]["enum"])
        self.assertIn("fork_thread", submit_schema["properties"]["operation_type"]["enum"])
        self.assertNotIn("review_start", submit_schema["properties"]["operation_type"]["enum"])
        self.assertIn("thread_id", submit_schema["properties"])
        self.assertIn("source_thread_id", submit_schema["properties"])
        self.assertIn("expected_turn_id", submit_schema["properties"])
        self.assertIn("fork_config", submit_schema["properties"])
        self.assertIn("ephemeral", submit_schema["properties"])
        self.assertIn("output_schema", submit_schema["properties"])
        self.assertIn("input_items", submit_schema["properties"])
        self.assertEqual(["string", "null"], submit_schema["properties"]["message"]["type"])
        self.assertEqual("read-only", submit_schema["properties"]["sandbox"]["default"])
        self.assertEqual("on-request", submit_schema["properties"]["approval_policy"]["default"])

        operation_status_schema = by_name["codex_get_operation_status"]["inputSchema"]
        self.assertEqual(["operation_id"], operation_status_schema["required"])
        self.assertEqual(10, operation_status_schema["properties"]["last_messages"]["default"])
        self.assertEqual(10, operation_status_schema["properties"]["progress_events"]["default"])
        self.assertEqual(2000, operation_status_schema["properties"]["progress_max_chars"]["default"])

        turn_status_schema = by_name["codex_get_turn_status"]["inputSchema"]["properties"]
        self.assertEqual(10, turn_status_schema["last_messages"]["default"])
        self.assertEqual(10, turn_status_schema["progress_events"]["default"])
        self.assertEqual(2000, turn_status_schema["progress_max_chars"]["default"])

        workflow_schema = by_name["codex_start_plan_workflow"]["inputSchema"]["properties"]
        self.assertEqual(0, workflow_schema["first_message_timeout_seconds"]["default"])
        self.assertEqual("read-only", workflow_schema["sandbox"]["default"])
        self.assertEqual("on-request", workflow_schema["approval_policy"]["default"])
        self.assertIn("goal", workflow_schema)
        self.assertIn("goal_token_budget", workflow_schema)
        self.assertEqual("clear", workflow_schema["goal_completion_action"]["default"])
        self.assertIn("set_complete", workflow_schema["goal_completion_action"]["enum"])

        review_schema = by_name["codex_start_review_workflow"]["inputSchema"]
        self.assertEqual(["target_type"], review_schema["required"])
        self.assertIn("thread_id", review_schema["properties"])
        self.assertIn("project_id", review_schema["properties"])
        self.assertIn("cwd", review_schema["properties"])
        self.assertIn("base_branch", review_schema["properties"])
        self.assertIn("commit_sha", review_schema["properties"])
        self.assertIn("instructions", review_schema["properties"])
        self.assertIn("detached", review_schema["properties"]["delivery"]["enum"])
        self.assertEqual("read-only", review_schema["properties"]["sandbox"]["default"])
        self.assertEqual("on-request", review_schema["properties"]["approval_policy"]["default"])

        workflow_status_schema = by_name["codex_get_workflow_status"]["inputSchema"]
        self.assertEqual(["workflow_id"], workflow_status_schema["required"])

        adopt_schema = by_name["codex_adopt_workflow_plan"]["inputSchema"]
        self.assertEqual(["workflow_id", "candidate_turn_id", "candidate_plan_hash"], adopt_schema["required"])
        self.assertIn("adoption_note", adopt_schema["properties"])

        approve_plan_schema = by_name["codex_approve_plan"]["inputSchema"]
        self.assertEqual(["workflow_id"], approve_plan_schema["required"])
        self.assertEqual("Implement the plan.", approve_plan_schema["properties"]["message"]["default"])
        self.assertEqual("read-only", approve_plan_schema["properties"]["sandbox"]["default"])
        self.assertEqual("on-request", approve_plan_schema["properties"]["approval_policy"]["default"])
        self.assertIn("output_schema", approve_plan_schema["properties"])

        execute_plan_schema = by_name["codex_execute_plan"]["inputSchema"]
        self.assertNotIn("required", execute_plan_schema)
        self.assertIn("workflow_id", execute_plan_schema["properties"])
        self.assertIn("chat_id", execute_plan_schema["properties"])
        self.assertEqual("Implement the plan.", execute_plan_schema["properties"]["message"]["default"])
        self.assertEqual("read-only", execute_plan_schema["properties"]["sandbox"]["default"])
        self.assertEqual("on-request", execute_plan_schema["properties"]["approval_policy"]["default"])
        self.assertIn("output_schema", execute_plan_schema["properties"])

        pending_schema = by_name["codex_list_pending_interactions"]["inputSchema"]["properties"]
        self.assertEqual("pending", pending_schema["status"]["default"])
        self.assertIn("operation_id", pending_schema)
        self.assertIn("workflow_id", pending_schema)

        answer_schema = by_name["codex_answer_pending_interaction"]["inputSchema"]
        self.assertEqual(["interaction_id"], answer_schema["required"])
        self.assertIn("decision_payload", answer_schema["properties"])

        interrupt_schema = by_name["codex_interrupt_turn"]["inputSchema"]
        self.assertNotIn("required", interrupt_schema)
        self.assertIn("operation_id", interrupt_schema["properties"])
        self.assertIn("workflow_id", interrupt_schema["properties"])

        archive_schema = by_name["codex_archive_thread"]["inputSchema"]
        unarchive_schema = by_name["codex_unarchive_thread"]["inputSchema"]
        compact_start_schema = by_name["codex_start_thread_compaction"]["inputSchema"]
        compact_status_schema = by_name["codex_get_thread_compaction_status"]["inputSchema"]
        self.assertEqual(["thread_id"], archive_schema["required"])
        self.assertEqual(["thread_id"], unarchive_schema["required"])
        self.assertEqual(["thread_id"], compact_start_schema["required"])
        self.assertEqual(["action_id"], compact_status_schema["required"])
        self.assertNotIn("codex_delete_thread", by_name)

        restart_schema = by_name["codex_restart_app_server"]["inputSchema"]["properties"]
        self.assertIn("force", restart_schema)

        runtime_schema = by_name["codex_get_runtime_capabilities"]["inputSchema"]["properties"]
        self.assertFalse(runtime_schema["refresh"]["default"])
        self.assertIsNone(runtime_schema["cwd"]["default"])
        self.assertEqual(2, runtime_schema["timeout_seconds"]["default"])
        self.assertTrue(runtime_schema["include_models"]["default"])
        self.assertTrue(runtime_schema["include_hooks"]["default"])
        self.assertTrue(runtime_schema["include_skills"]["default"])
        self.assertTrue(runtime_schema["include_account"]["default"])

        preflight_schema = by_name["codex_preflight_project_run"]["inputSchema"]["properties"]
        self.assertFalse(preflight_schema["live_probe"]["default"])
        self.assertIn("workflow_kind", preflight_schema)

        diagnostics_schema = by_name["codex_collect_diagnostics"]["inputSchema"]["properties"]
        self.assertIn("operation_id", diagnostics_schema)
        self.assertIn("include_logs", diagnostics_schema)
        self.assertTrue(diagnostics_schema["include_timeline"]["default"])
        self.assertEqual(120, diagnostics_schema["since_minutes"]["default"])

        health_schema = by_name["codex_health_summary"]["inputSchema"]["properties"]
        self.assertIn("operation_id", health_schema)
        self.assertEqual(30, health_schema["stale_after_minutes"]["default"])

        logs_schema = by_name["codex_get_diagnostic_logs"]["inputSchema"]["properties"]
        self.assertIn("app_server_events", logs_schema["source"]["enum"])
        self.assertFalse(logs_schema["include_payload"]["default"])

        analyze_schema = by_name["codex_analyze_issue"]["inputSchema"]["properties"]
        self.assertIn("problem_text", analyze_schema)
        self.assertIn("operation_id", analyze_schema)

        repair_schema = by_name["codex_repair_issue"]["inputSchema"]
        self.assertEqual(["action"], repair_schema["required"])
        self.assertIn("recover_stale_operations", repair_schema["properties"]["action"]["enum"])
        self.assertIn("refresh_catalog_and_kb", repair_schema["properties"]["action"]["enum"])
        self.assertIn("mark_orphaned_after_exit", repair_schema["properties"]["action"]["enum"])
        self.assertIn("force_restart_app_server", repair_schema["properties"]["action"]["enum"])
        self.assertIn("cleanup_prompt_submissions", repair_schema["properties"]["action"]["enum"])
        self.assertIn("reconcile_workflow_from_thread", repair_schema["properties"]["action"]["enum"])
        self.assertIn("retry_workflow_with_runtime_policy", repair_schema["properties"]["action"]["enum"])
        self.assertIn("client_request_id", repair_schema["properties"])
        self.assertIn("sandbox", repair_schema["properties"])
        self.assertIn("approval_policy", repair_schema["properties"])
        self.assertIn("reason", repair_schema["properties"])
        self.assertTrue(repair_schema["properties"]["dry_run"]["default"])
        self.assertEqual(30, repair_schema["properties"]["stale_after_minutes"]["default"])
        self.assertEqual(30, repair_schema["properties"]["older_than_days"]["default"])

    def test_collaboration_mode_builder(self) -> None:
        config = ServerConfig(default_model="gpt-5.5")
        self.assertIsNone(_collaboration_mode(None, model=None, config=config))
        self.assertEqual(
            {
                "mode": "plan",
                "settings": {
                    "model": "gpt-5.4",
                    "reasoning_effort": None,
                    "developer_instructions": None,
                },
            },
            _collaboration_mode("plan", model="gpt-5.4", config=config),
        )

    def test_call_tool_result_uses_structured_content_and_error_flag(self) -> None:
        success = call_tool_result({"threadId": "thread-1", "content": "done"})
        self.assertFalse(success["isError"])
        self.assertEqual("thread-1", success["structuredContent"]["threadId"])
        self.assertTrue(success["structuredContent"]["ok"])

        failure = call_tool_result(
            {
                "error": {
                    "code": "CODEX_BUSY",
                    "message": "busy",
                    "details": {"thread_id": "thread-1"},
                    "retryable": True,
                }
            }
        )
        self.assertTrue(failure["isError"])
        self.assertFalse(failure["structuredContent"]["ok"])
        self.assertEqual("CODEX_BUSY", failure["structuredContent"]["error"]["code"])

    def test_stdio_tools_call_returns_structured_content(self) -> None:
        server = StdioMcpServer.__new__(StdioMcpServer)
        server.service = FakeToolService({"threadId": "thread-1", "content": "done"})

        result = asyncio.run(
            server._handle_request(
                "tools/call",
                {"name": "fake", "arguments": {"prompt": "hello"}},
            )
        )

        self.assertFalse(result["isError"])
        self.assertTrue(result["structuredContent"]["ok"])
        self.assertEqual("thread-1", result["structuredContent"]["threadId"])

    def test_stdio_tools_call_returns_structured_error(self) -> None:
        server = StdioMcpServer.__new__(StdioMcpServer)
        server.service = FakeToolService(
            {
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "bad",
                    "details": {},
                    "retryable": False,
                }
            }
        )

        result = asyncio.run(
            server._handle_request(
                "tools/call",
                {"name": "fake", "arguments": {}},
            )
        )

        self.assertTrue(result["isError"])
        self.assertFalse(result["structuredContent"]["ok"])
        self.assertEqual("INVALID_ARGUMENT", result["structuredContent"]["error"]["code"])

    def test_stdio_initialize_and_tools_list(self) -> None:
        server = StdioMcpServer.__new__(StdioMcpServer)
        server.service = FakeToolService({"ok": True})

        initialized = asyncio.run(server._handle_request("initialize", {"protocolVersion": "2025-01-10"}))
        listed = asyncio.run(server._handle_request("tools/list", {}))

        self.assertEqual("2025-01-10", initialized["protocolVersion"])
        self.assertIn("tools", initialized["capabilities"])
        self.assertEqual("codex-control-plane-mcp", initialized["serverInfo"]["name"])
        names = {tool["name"] for tool in listed["tools"]}
        self.assertIn("codex_start_plan_workflow", names)
        self.assertIn("codex_get_workflow_status", names)

    def test_stdio_subprocess_outputs_only_jsonrpc_frames(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["CODEX_MCP_STATE_DB"] = str(root / "state.sqlite3")
            env["CODEX_CONTROL_PLANE_MCP_LOG"] = str(root / "server.log")
            env["CODEX_PROJECTS_ROOT"] = str(root)
            env["CODEX_ALLOWED_ROOTS"] = str(root)
            process = subprocess.Popen(
                [sys.executable, "-m", "codex_control_plane_mcp.server"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            try:
                assert process.stdin is not None
                assert process.stdout is not None

                def request(request_id: int, method: str, params: dict) -> dict:
                    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
                    process.stdin.flush()
                    line = process.stdout.readline()
                    self.assertTrue(line.strip(), "MCP subprocess did not return a stdout frame")
                    return json.loads(line)

                initialized = request(1, "initialize", {"protocolVersion": "2025-01-10"})
                listed = request(2, "tools/list", {})
                tool_error = request(3, "tools/call", {"name": "missing_tool", "arguments": {}})
                rpc_error = request(4, "missing/method", {})

                self.assertEqual("2.0", initialized["jsonrpc"])
                self.assertEqual("2025-01-10", initialized["result"]["protocolVersion"])
                self.assertIn("tools", listed["result"])
                self.assertTrue(tool_error["result"]["isError"])
                self.assertFalse(tool_error["result"]["structuredContent"]["ok"])
                self.assertEqual("INVALID_ARGUMENT", tool_error["result"]["structuredContent"]["error"]["code"])
                self.assertEqual(-32601, rpc_error["error"]["code"])
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                stderr = process.stderr.read() if process.stderr is not None else ""
                self.assertEqual("", stderr)
