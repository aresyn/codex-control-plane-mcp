from __future__ import annotations

from tests.helpers import *


class McpDefinitionTests(unittest.TestCase):
    def test_runtime_capabilities_success_cache_refresh_and_health_subset(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_runtime.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage)
            service._app_server = fake  # type: ignore[assignment]
            try:
                first = asyncio.run(service.codex_get_runtime_capabilities({}))
                first_call_count = len(fake.inventory_calls)
                second = asyncio.run(service.codex_get_runtime_capabilities({}))
                after_cache_count = len(fake.inventory_calls)
                refreshed = asyncio.run(service.codex_get_runtime_capabilities({"refresh": True}))
                after_refresh_count = len(fake.inventory_calls)
                health = service.codex_health_summary({})
                after_health_count = len(fake.inventory_calls)
            finally:
                asyncio.run(service.close())

        self.assertTrue(first["ok"])
        self.assertEqual("ok", first["runtimeCapabilities"]["status"])
        self.assertFalse(first["cacheState"]["hit"])
        self.assertEqual(9, first_call_count)
        self.assertTrue(second["cacheState"]["hit"])
        self.assertEqual(first_call_count, after_cache_count)
        self.assertFalse(refreshed["cacheState"]["hit"])
        self.assertEqual(first_call_count * 2, after_refresh_count)
        self.assertEqual(after_refresh_count, after_health_count)

        capabilities = first["runtimeCapabilities"]
        self.assertEqual(2, capabilities["models"]["count"])
        self.assertEqual("gpt-5", capabilities["models"]["defaultModel"])
        self.assertEqual(2, capabilities["permissionProfiles"]["count"])
        self.assertEqual("ready", capabilities["sandboxReadiness"]["status"])
        self.assertEqual({"webSearch": True, "imageGeneration": False, "namespaceTools": True}, capabilities["modelProviderCapabilities"])
        self.assertEqual(2, capabilities["hooks"]["hookCount"])
        self.assertEqual(2, capabilities["skills"]["skillCount"])
        self.assertEqual(82, capabilities["schemaMethods"]["methodCount"])
        self.assertIn("turn/steer", capabilities["schemaMethods"]["methods"])
        self.assertTrue(capabilities["accountStatus"]["authenticated"])
        self.assertEqual("chatgpt", capabilities["accountStatus"]["accountType"])
        self.assertEqual("pro", capabilities["accountStatus"]["planType"])
        self.assertTrue(capabilities["accountStatus"]["emailPresent"])
        self.assertTrue(capabilities["accountStatus"]["identityRedacted"])
        self.assertTrue(capabilities["accountUsage"]["available"])
        self.assertEqual(1, capabilities["accountUsage"]["dailyBucketCount"])
        self.assertEqual("100m_plus", capabilities["accountUsage"]["summary"]["lifetimeUsageBand"])
        self.assertTrue(capabilities["rateLimits"]["available"])
        self.assertTrue(capabilities["rateLimits"]["credits"]["hasCredits"])
        self.assertTrue(capabilities["rateLimits"]["credits"]["balanceRedacted"])
        self.assertTrue(capabilities["rateLimits"]["rateLimitReached"])
        self.assertIn("bucketHash", capabilities["rateLimits"]["primary"])

        rendered = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("run-hook.ps1", rendered)
        self.assertNotIn("stop.ps1", rendered)
        self.assertNotIn("SKILL.md", rendered)
        self.assertNotIn("C:\\Secret", rendered)
        self.assertNotIn("D:\\private", rendered)
        self.assertNotIn("sk-secret", rendered)
        self.assertNotIn("user@example.com", rendered)
        self.assertNotIn("acct_secret", rendered)
        self.assertNotIn("99.95", rendered)
        self.assertNotIn("Private Team Bucket", rendered)
        self.assertNotIn("private-limit-id", rendered)
        self.assertNotIn("123456789", rendered)
        self.assertNotIn("dailyUsageBuckets", rendered)
        self.assertNotIn("startDate", rendered)
        self.assertNotIn('"tokens"', rendered)

        runtime_health = health["runtimeCapabilities"]
        self.assertEqual("ok", runtime_health["status"])
        self.assertEqual(2, runtime_health["modelCount"])
        self.assertEqual("gpt-5", runtime_health["defaultModel"])
        self.assertEqual("ready", runtime_health["sandboxReadiness"])
        self.assertTrue(runtime_health["accountAuthenticated"])
        self.assertEqual("chatgpt", runtime_health["accountType"])
        self.assertEqual("pro", runtime_health["planType"])
        self.assertTrue(runtime_health["rateLimitReached"])
        self.assertTrue(runtime_health["creditsAvailable"])
        self.assertTrue(runtime_health["usageAvailable"])
        self.assertEqual(0, runtime_health["warningsCount"])

    def test_runtime_capabilities_method_timeout_is_partial_ok(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_runtime_timeout.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage)
            fake.inventory_failures["model/list"] = CodexMcpError("CODEX_TIMEOUT", "inventory timed out", retryable=True)
            service._app_server = fake  # type: ignore[assignment]
            try:
                result = asyncio.run(service.codex_get_runtime_capabilities({}))
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["ok"])
        self.assertEqual("partial", result["runtimeCapabilities"]["status"])
        self.assertIsNone(result["runtimeCapabilities"]["models"])
        self.assertEqual("timeout", result["methodResults"]["model/list"]["status"])
        self.assertEqual("CODEX_TIMEOUT", result["methodResults"]["model/list"]["errorCode"])
        self.assertEqual(1, len(result["warnings"]))
        self.assertEqual("model/list", result["warnings"][0]["method"])
        self.assertEqual("ok", result["methodResults"]["permissionProfile/list"]["status"])

    def test_runtime_capabilities_include_flags_skip_optional_methods(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_runtime_skip.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage)
            service._app_server = fake  # type: ignore[assignment]
            try:
                result = asyncio.run(
                    service.codex_get_runtime_capabilities(
                        {
                            "include_models": False,
                            "include_hooks": False,
                            "include_skills": False,
                            "include_account": False,
                        }
                    )
                )
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["ok"])
        self.assertEqual("skipped", result["methodResults"]["model/list"]["status"])
        self.assertEqual("skipped", result["methodResults"]["hooks/list"]["status"])
        self.assertEqual("skipped", result["methodResults"]["skills/list"]["status"])
        self.assertEqual("skipped", result["methodResults"]["account/read"]["status"])
        self.assertEqual("skipped", result["methodResults"]["account/usage/read"]["status"])
        self.assertEqual("skipped", result["methodResults"]["account/rateLimits/read"]["status"])
        self.assertNotIn("model/list", fake.inventory_calls)
        self.assertNotIn("hooks/list", fake.inventory_calls)
        self.assertNotIn("skills/list", fake.inventory_calls)
        self.assertNotIn("account/read", fake.inventory_calls)
        self.assertNotIn("account/usage/read", fake.inventory_calls)
        self.assertNotIn("account/rateLimits/read", fake.inventory_calls)

    def test_client_mode_runtime_capabilities_are_passive_without_starting_app_server(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_runtime_client.sqlite")
            config.execution_mode = "client"
            service = ToolService(config)
            try:
                result = asyncio.run(service.codex_get_runtime_capabilities({}))
                preflight = asyncio.run(
                    service.codex_preflight_project_run(
                        {
                            "cwd": str(root),
                            "live_probe": False,
                        }
                    )
                )
                app_server = service._app_server
            finally:
                asyncio.run(service.close())

        self.assertIsNone(app_server)
        self.assertTrue(result["ok"])
        self.assertEqual("passive", result["runtimeCapabilities"]["status"])
        self.assertEqual("skipped", result["methodResults"]["model/list"]["status"])
        self.assertEqual("passive", preflight["runtimeCapabilities"]["status"])

    def test_listed_allowed_project_preflights_and_submits_in_client_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "TestProject1"
            project.mkdir()
            config = _search_service_config(root, root / ".codex" / "state_runtime_project.sqlite")
            config.execution_mode = "client"
            config.codex_binary_path.write_text("", encoding="utf-8")
            (config.codex_home / "auth.json").write_text("{}", encoding="utf-8")
            service = ToolService(config)
            try:
                projects = service.codex_list_projects(
                    {"compact": True, "refresh": True, "roots": [str(project)], "limit": 10}
                )
                project_id = (projects["projects"][0]["projectId"] if projects["projects"] else None)
                project_name = (projects["projects"][0]["name"] if projects["projects"] else None)
                preflight = asyncio.run(
                    service.codex_preflight_project_run(
                        {
                            "project_id": project_name,
                            "cwd": str(project),
                            "sandbox": "danger-full-access",
                            "approval_policy": "never",
                            "live_probe": False,
                        }
                    )
                )
                chats = service.codex_list_project_chats({"project_id": project_name, "limit": 5})
                submitted = service.codex_submit_task(
                    {
                        "operation_type": "start_chat",
                        "project_id": project_name,
                        "cwd": str(project),
                        "message": "MCP regression test. Do not modify files.",
                        "sandbox": "danger-full-access",
                        "approval_policy": "never",
                        "client_request_id": "regression:list-preflight-submit",
                    }
                )
            finally:
                asyncio.run(service.close())

        self.assertEqual(1, projects["returnedCount"])
        self.assertTrue(project_id)
        self.assertNotEqual("error", preflight["status"])
        self.assertTrue(preflight["ok"])
        self.assertEqual(project_id, preflight["projectId"])
        self.assertEqual(project_name, preflight["requestedProjectId"])
        self.assertEqual(project_id, chats["projectId"])
        self.assertEqual(project_name, chats["requestedProjectId"])
        self.assertTrue(submitted["ok"])
        self.assertEqual("queued", submitted["status"])
        self.assertEqual(project_id, submitted["projectId"])

    def test_runtime_capabilities_unauthenticated_skips_account_usage_and_limits(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_runtime_unauth.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage)
            fake.account_response = {"requiresOpenaiAuth": True, "account": None}
            service._app_server = fake  # type: ignore[assignment]
            try:
                result = asyncio.run(service.codex_get_runtime_capabilities({}))
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["ok"])
        self.assertEqual("ok", result["runtimeCapabilities"]["status"])
        self.assertFalse(result["runtimeCapabilities"]["accountStatus"]["authenticated"])
        self.assertTrue(result["runtimeCapabilities"]["accountStatus"]["requiresOpenaiAuth"])
        self.assertIsNone(result["runtimeCapabilities"]["accountUsage"])
        self.assertIsNone(result["runtimeCapabilities"]["rateLimits"])
        self.assertEqual("skipped", result["methodResults"]["account/usage/read"]["status"])
        self.assertEqual("unauthenticated", result["methodResults"]["account/usage/read"]["reason"])
        self.assertEqual("skipped", result["methodResults"]["account/rateLimits/read"]["status"])
        self.assertNotIn("account/usage/read", fake.inventory_calls)
        self.assertNotIn("account/rateLimits/read", fake.inventory_calls)

    def test_runtime_capabilities_account_timeout_is_partial_and_skips_children(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_runtime_account_timeout.sqlite")
            service = ToolService(config)
            fake = FakeAppServer(service.storage)
            fake.inventory_failures["account/read"] = CodexMcpError("CODEX_TIMEOUT", "account timed out", retryable=True)
            service._app_server = fake  # type: ignore[assignment]
            try:
                result = asyncio.run(service.codex_get_runtime_capabilities({}))
            finally:
                asyncio.run(service.close())

        self.assertTrue(result["ok"])
        self.assertEqual("partial", result["runtimeCapabilities"]["status"])
        self.assertIsNone(result["runtimeCapabilities"]["accountStatus"])
        self.assertIsNone(result["runtimeCapabilities"]["accountUsage"])
        self.assertIsNone(result["runtimeCapabilities"]["rateLimits"])
        self.assertEqual("timeout", result["methodResults"]["account/read"]["status"])
        self.assertEqual("account_status_unavailable", result["methodResults"]["account/usage/read"]["reason"])
        self.assertEqual("account_status_unavailable", result["methodResults"]["account/rateLimits/read"]["reason"])

    def test_diagnostic_event_payload_redacts_account_and_rate_limit_fields(self) -> None:
        row = {
            "id": 1,
            "direction": "inbound",
            "jsonrpc_id": 10,
            "method": "account/rateLimits/read",
            "thread_id": None,
            "turn_id": None,
            "process_generation": 1,
            "received_at": "2026-06-18T00:00:00+00:00",
            "payload_json": json.dumps(
                {
                    "result": {
                        "account": {"email": "user@example.com", "accountId": "acct_secret"},
                        "rateLimits": {
                            "limitId": "private-limit-id",
                            "limitName": "Private Team Bucket",
                            "credits": {"balance": 99.95, "hasCredits": True},
                            "individualLimit": {"limit": 100, "used": 12, "remainingPercent": 88, "resetsAt": "2026-06-19T00:00:00Z"},
                        },
                    }
                }
            ),
        }

        payload = event_to_tool(row, include_payload=True)["payload"]
        rendered = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("user@example.com", rendered)
        self.assertNotIn("acct_secret", rendered)
        self.assertNotIn("private-limit-id", rendered)
        self.assertNotIn("Private Team Bucket", rendered)
        self.assertNotIn("99.95", rendered)
        self.assertNotIn('"limit": 100', rendered)
        self.assertNotIn('"used": 12', rendered)
        self.assertIn("[redacted]", rendered)
        self.assertIn("[redacted-email]", redact_text("contact user@example.com"))
