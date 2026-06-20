from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from openclaw_codex_mcp.catalog import ProjectChatCatalog
from openclaw_codex_mcp.config import ServerConfig
from openclaw_codex_mcp.storage import McpStorage


class CatalogTests(unittest.TestCase):
    def test_catalog_merges_state_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            codex_home = root / ".codex"
            sessions = codex_home / "sessions"
            sessions.mkdir(parents=True)
            archived = codex_home / "archived_sessions"
            archived.mkdir()
            transcript = sessions / "rollout-thread-1.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-05-24T10:00:00Z",
                                "type": "session_meta",
                                "payload": {"id": "thread-1", "timestamp": "2026-05-24T10:00:00Z", "cwd": str(project)},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-24T10:00:01Z",
                                "type": "turn_context",
                                "payload": {"turn_id": "turn-1"},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            state_db = codex_home / "state_5.sqlite"
            con = sqlite3.connect(state_db)
            con.execute(
                """
                CREATE TABLE threads(
                  id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT, preview TEXT,
                  created_at_ms INTEGER, updated_at_ms INTEGER, archived INTEGER, source TEXT,
                  thread_source TEXT, model TEXT, reasoning_effort TEXT, sandbox_policy TEXT,
                  approval_mode TEXT
                )
                """
            )
            con.execute(
                "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "thread-1",
                    str(transcript),
                    str(project),
                    "UI title",
                    "preview",
                    1770000000000,
                    1770000100000,
                    0,
                    "vscode",
                    "user",
                    "gpt-5.5",
                    "medium",
                    "{}",
                    "never",
                ),
            )
            con.commit()
            con.close()

            config = ServerConfig(
                codex_home=codex_home,
                sessions_dir=sessions,
                archived_sessions_dir=archived,
                codex_state_db=state_db,
                codex_logs_db=codex_home / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[root],
            )
            storage = McpStorage(config.state_db_path)
            storage.connect()
            try:
                catalog = ProjectChatCatalog(config, storage)
                projects = catalog.list_projects()
                self.assertEqual(len(projects), 1)
                chats = catalog.list_project_chats(projects[0].project_id)
                self.assertEqual(len(chats), 1)
                self.assertEqual(chats[0].title, "UI title")
                self.assertEqual(catalog.locate_transcript(chats[0]), str(transcript))
            finally:
                storage.close()

    def test_cached_projects_are_filtered_by_current_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed_project = allowed / "TestProject"
            outside_project = outside / "OtherProject"
            allowed_project.mkdir(parents=True)
            outside_project.mkdir(parents=True)
            config = ServerConfig(
                codex_home=root / ".codex",
                sessions_dir=root / ".codex" / "sessions",
                archived_sessions_dir=root / ".codex" / "archived_sessions",
                codex_state_db=root / ".codex" / "state_5.sqlite",
                codex_logs_db=root / ".codex" / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[allowed],
            )
            storage = McpStorage(config.state_db_path)
            storage.connect()
            try:
                updated_at = "2026-06-20T00:00:00+00:00"
                storage.upsert_project(
                    {
                        "project_id": "allowed-project",
                        "name": "TestProject",
                        "path": str(allowed_project),
                        "normalized_path_key": str(allowed_project),
                        "created_at": None,
                        "last_activity_at": None,
                        "source": "cache",
                        "updated_at": updated_at,
                    }
                )
                storage.upsert_project(
                    {
                        "project_id": "outside-project",
                        "name": "OtherProject",
                        "path": str(outside_project),
                        "normalized_path_key": str(outside_project),
                        "created_at": None,
                        "last_activity_at": None,
                        "source": "cache",
                        "updated_at": updated_at,
                    }
                )
                catalog = ProjectChatCatalog(config, storage)
                cached = catalog.load_cached_projects()
            finally:
                storage.close()

        self.assertEqual(["allowed-project"], [project.project_id for project in cached])

    def test_list_project_chats_uses_cached_index_before_full_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "TestProject"
            project.mkdir()
            config = ServerConfig(
                codex_home=root / ".codex",
                sessions_dir=root / ".codex" / "sessions",
                archived_sessions_dir=root / ".codex" / "archived_sessions",
                codex_state_db=root / ".codex" / "state_5.sqlite",
                codex_logs_db=root / ".codex" / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[root],
            )
            storage = McpStorage(config.state_db_path)
            storage.connect()
            try:
                updated_at = "2026-06-20T00:00:00+00:00"
                storage.upsert_project(
                    {
                        "project_id": "project-1",
                        "name": "TestProject",
                        "path": str(project),
                        "normalized_path_key": str(project),
                        "created_at": updated_at,
                        "last_activity_at": updated_at,
                        "source": "cache",
                        "updated_at": updated_at,
                    }
                )
                storage.upsert_chat(
                    {
                        "chat_id": "chat-1",
                        "thread_id": "thread-1",
                        "project_id": "project-1",
                        "project_path": str(project),
                        "title": "Cached chat",
                        "transcript_path": "",
                        "created_at": updated_at,
                        "updated_at": updated_at,
                        "archived": 0,
                        "last_message_preview": None,
                        "status": "idle",
                        "status_confidence": "medium",
                        "source": "cache",
                        "updated_at_local": updated_at,
                    }
                )
                catalog = ProjectChatCatalog(config, storage)

                def fail_refresh() -> None:
                    raise AssertionError("full refresh should not run when cache is available")

                catalog.refresh = fail_refresh  # type: ignore[method-assign]
                chats = catalog.list_project_chats("project-1")
            finally:
                storage.close()

        self.assertEqual(["chat-1"], [chat.chat_id for chat in chats])


if __name__ == "__main__":
    unittest.main()
