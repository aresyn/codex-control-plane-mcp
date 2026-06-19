from __future__ import annotations

from tests.helpers import *


class HookHistoryTests(unittest.TestCase):
    def test_hook_payload_records_redacted_idempotent_sqlite_history(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            state_db = root / "mcp.sqlite"
            user_payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "hook-thread",
                "thread_id": "hook-thread",
                "turn_id": "hook-turn",
                "cwd": str(project),
                "model": "gpt-5.5",
                "permission_mode": "never",
                "prompt": "Find hook needle with api_key=SECRETSECRET",
            }
            stop_payload = {
                **user_payload,
                "hook_event_name": "Stop",
                "last_assistant_message": "Final hook answer with " + "Bearer " + "abcdefghijklmnop",
            }

            self.assertTrue(record_payload(user_payload, state_db=state_db)["recorded"])
            self.assertTrue(record_payload(user_payload, state_db=state_db)["recorded"])
            self.assertTrue(record_payload(stop_payload, state_db=state_db)["recorded"])

            storage = McpStorage(state_db)
            storage.connect()
            try:
                status = storage.hook_history_status()
                messages = storage.list_hook_messages(thread_id="hook-thread")
                turn = storage.get_hook_turn("hook-turn")
            finally:
                storage.close()

            serialized = json.dumps(messages, ensure_ascii=False)
            self.assertEqual(1, status["threadCount"])
            self.assertEqual(1, status["turnCount"])
            self.assertEqual(2, status["messageCount"])
            self.assertEqual(2, len(messages))
            self.assertNotIn("SECRETSECRET", serialized)
            self.assertNotIn("abcdefghijklmnop", serialized)
            self.assertEqual("completed", turn["status"])

    def test_hook_installer_merges_and_uninstalls_without_removing_user_hooks(self) -> None:
        with TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            hooks_json = codex_home / "hooks.json"
            hooks_json.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {"hooks": [{"type": "command", "command": "python user_hook.py", "timeout": 30}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            state_db = Path(tmp) / "mcp.sqlite"

            installed = install_hooks(codex_home=codex_home, state_db=state_db)
            status = hook_status(codex_home=codex_home)
            removed = uninstall_hooks(codex_home=codex_home)
            payload = json.loads(hooks_json.read_text(encoding="utf-8"))

            self.assertTrue(installed["ok"])
            self.assertTrue(status["installed"])
            self.assertGreaterEqual(len(list(codex_home.glob("hooks.json.codex-control-plane-backup-*"))), 1)
            self.assertGreaterEqual(removed["removedHandlers"], 5)
            self.assertEqual("python user_hook.py", payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
            self.assertIn("codex-control-plane-mcp-hooks.json", installed["configPath"])

    def test_hook_installer_stores_absolute_state_db_for_relative_input(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                installed = install_hooks(codex_home=codex_home, state_db=Path("state") / "mcp.sqlite")
                status = hook_status(codex_home=codex_home)
                config = json.loads((codex_home / "codex-control-plane-mcp-hooks.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        expected = str((root / "state" / "mcp.sqlite").resolve(strict=False))
        self.assertEqual(expected, installed["stateDb"])
        self.assertEqual(expected, status["stateDb"])
        self.assertEqual(expected, config["stateDb"])
        self.assertTrue(Path(config["stateDb"]).is_absolute())

    def test_hook_installer_upgrades_legacy_hook_marker(self) -> None:
        with TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            hooks_json = codex_home / "hooks.json"
            legacy_command = f"{sys.executable} -m openclaw_codex_mcp.hooks.codex_sqlite_journal --config legacy.json"
            hooks_json.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {"hooks": [{"type": "command", "command": legacy_command, "timeout": 30}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            installed = install_hooks(codex_home=codex_home, state_db=Path(tmp) / "mcp.sqlite")
            payload = json.loads(hooks_json.read_text(encoding="utf-8"))
            command = payload["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

        self.assertTrue(installed["changed"])
        self.assertIn("codex_control_plane_mcp.hooks.codex_sqlite_journal", command)
        self.assertNotIn("openclaw_codex_mcp.hooks.codex_sqlite_journal", command)

    def test_hook_history_drives_project_search_status_get_chat_and_turn_status(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            record_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "hook-thread",
                    "thread_id": "hook-thread",
                    "turn_id": "hook-turn",
                    "cwd": str(project),
                    "model": "gpt-5.5",
                    "permission_mode": "never",
                    "prompt": "latest hook user needle",
                },
                state_db=config.state_db_path,
            )
            record_payload(
                {
                    "hook_event_name": "Stop",
                    "session_id": "hook-thread",
                    "thread_id": "hook-thread",
                    "turn_id": "hook-turn",
                    "cwd": str(project),
                    "model": "gpt-5.5",
                    "permission_mode": "never",
                    "last_assistant_message": "latest hook assistant needle",
                },
                state_db=config.state_db_path,
            )
            service = ToolService(config)
            try:
                projects = service.codex_list_projects({"include_private_details": True})["projects"]
                search = service.codex_search_chats({"query": "needle", "limit": 10})
                status = service.codex_get_chat_status({"chat_id": "hook-thread"})
                chat = service.codex_get_chat({"chat_id": "hook-thread", "range": {"mode": "all"}, "format": "structured"})
                turn_status = service.codex_get_turn_status({"turn_id": "hook-turn", "thread_id": "hook-thread"})
                health = service.codex_health_summary({})
            finally:
                asyncio.run(service.close())

            self.assertIn(path_key(project), [path_key(item["path"]) for item in projects])
            self.assertEqual("chat_search_fts_index", search["source"])
            self.assertEqual(1, search["total_results"])
            self.assertEqual("hook-thread", search["results"][0]["thread_id"])
            self.assertEqual("hook_history", status["transcript"]["source"])
            self.assertEqual("latest hook user needle", status["last_user_preview"])
            self.assertEqual("latest hook assistant needle", status["last_assistant_preview"])
            self.assertTrue(chat["source"].startswith("hook_history+"))
            self.assertEqual(["latest hook user needle", "latest hook assistant needle"], [item["text"] for item in chat["messages"]])
            self.assertEqual("hook_history", turn_status["source"])
            self.assertEqual("completed", turn_status["status"])
            self.assertEqual("latest hook assistant needle", turn_status["finalMessage"])
            self.assertEqual(1, health["hookHistory"]["threadCount"])
            self.assertIn("hookHistoryStatus", health)
