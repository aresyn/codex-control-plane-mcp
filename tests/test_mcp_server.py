from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace

from openclaw_codex_mcp.catalog import project_id_for_path
from openclaw_codex_mcp.config import ServerConfig, path_key
from openclaw_codex_mcp.diagnostics import analyze_context, redact_payload, redact_text
from openclaw_codex_mcp.errors import CodexMcpError
from openclaw_codex_mcp.hook_installer import hook_status, install_hooks, uninstall_hooks
from openclaw_codex_mcp.hooks.codex_sqlite_journal import record_payload
from openclaw_codex_mcp.pending_interactions import (
    COMMAND_APPROVAL_METHOD,
    FILE_APPROVAL_METHOD,
    MCP_ELICITATION_METHOD,
    PERMISSIONS_APPROVAL_METHOD,
    TOOL_USER_INPUT_METHOD,
    PendingInteractionManager,
    build_response_for_answer,
    default_response_for_method,
)
from openclaw_codex_mcp.prompt_dedup import normalize_prompt, prompt_hash, prompt_similarity
from openclaw_codex_mcp.protocol import call_tool_result
from openclaw_codex_mcp.search import build_fts_query
from openclaw_codex_mcp.server import StdioMcpServer
from openclaw_codex_mcp.storage import McpStorage
from openclaw_codex_mcp.tools import (
    COMPATIBILITY_TOOLS,
    CONTRACT_VERSION,
    STABLE_OPENCLAW_TOOLS,
    TOOLS,
    ToolService,
    _approval_policy_for_send,
    _collaboration_mode,
    _sandbox_policy_for_send,
    _split_selected_messages,
    _tool_surface_hash,
)
from openclaw_codex_mcp.turn_tracker import TurnTracker


def _write_transcript(path: Path, thread_id: str, project: Path, messages: list[tuple[str, str]], extra_rows: list[dict] | None = None) -> None:
    rows = [
        {"timestamp": "2026-05-25T00:00:00Z", "type": "session_meta", "payload": {"id": thread_id, "cwd": str(project)}},
        {"timestamp": "2026-05-25T00:00:01Z", "type": "turn_context", "payload": {"turn_id": f"{thread_id}-turn"}},
    ]
    for index, (role, text) in enumerate(messages, 2):
        rows.append(
            {
                "timestamp": f"2026-05-25T00:00:{index:02d}Z",
                "type": "response_item",
                "payload": {"type": "message", "role": role, "content": text},
            }
        )
    rows.extend(extra_rows or [])
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _write_kb_turn(
    kb_root: Path,
    project: Path,
    thread_id: str,
    turn_id: str,
    messages: list[tuple[str, str]],
    *,
    status: str = "completed",
    created_at: str = "2026-05-25T00:00:00Z",
    updated_at: str = "2026-05-25T00:00:10Z",
) -> Path:
    thread_dir = kb_root / project.name / "threads" / thread_id
    thread_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "source": "hook",
        "record_type": "turn",
        "status": status,
        "ids": {
            "thread_id": thread_id,
            "session_id": thread_id,
            "turn_id": turn_id,
            "correlation_id": f"{thread_id}__{turn_id}",
        },
        "project": {"name": project.name, "key": project.name, "cwd": str(project)},
        "environment": {"model": "gpt-5.5", "permission_mode": "never", "capture_method": "test"},
        "timestamps": {
            "created_at_utc": created_at,
            "updated_at_utc": updated_at,
            "completed_at_utc": updated_at if status == "completed" else None,
        },
        "messages": [
            {
                "message_id": f"{turn_id}-{index}",
                "role": role,
                "text": text,
                "sequence": index,
                "captured_at_utc": updated_at,
                "char_count": len(text),
                "hook_event_name": f"test_{role}",
                "text_missing": False,
            }
            for index, (role, text) in enumerate(messages, 1)
        ],
        "stats": {
            "user_prompt_count": len([item for item in messages if item[0] == "user"]),
            "assistant_report_count": len([item for item in messages if item[0] == "assistant"]),
            "message_count": len(messages),
        },
    }
    path = thread_dir / f"{turn_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _create_threads_db(path: Path, rows: list[dict[str, object]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE threads(
              id TEXT PRIMARY KEY,
              rollout_path TEXT,
              cwd TEXT,
              title TEXT,
              preview TEXT,
              created_at_ms INTEGER,
              updated_at_ms INTEGER,
              archived INTEGER,
              source TEXT,
              thread_source TEXT,
              model TEXT,
              reasoning_effort TEXT,
              sandbox_policy TEXT,
              approval_mode TEXT
            )
            """
        )
        for row in rows:
            connection.execute(
                """
                INSERT INTO threads(
                  id, rollout_path, cwd, title, preview, created_at_ms, updated_at_ms,
                  archived, source, thread_source, model, reasoning_effort, sandbox_policy, approval_mode
                )
                VALUES(:id, :rollout_path, :cwd, :title, :preview, :created_at_ms, :updated_at_ms,
                  :archived, 'desktop', 'ui', 'gpt-5', 'medium', '{}', 'untrusted')
                """,
                row,
            )
        connection.commit()
    finally:
        connection.close()


def _search_service_config(root: Path, state_db: Path) -> ServerConfig:
    codex_home = root / ".codex"
    sessions = codex_home / "sessions"
    archived = codex_home / "archived_sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    archived.mkdir(parents=True, exist_ok=True)
    return ServerConfig(
        codex_home=codex_home,
        sessions_dir=sessions,
        archived_sessions_dir=archived,
        codex_state_db=state_db,
        codex_logs_db=codex_home / "logs_2.sqlite",
        projects_root=root,
        projects_registry_path=root / "projects.json",
        kb_history_projects_root=root / "kb_history" / "projects",
        codex_binary_path=root / "codex.exe",
        state_db_path=root / "mcp.sqlite",
        allowed_roots=[root],
        deepseek_env_path=root / "missing.env",
    )


class FakeAppServer:
    def __init__(self, storage: McpStorage, *, first_message: str | None = "fake first") -> None:
        self.tracker = TurnTracker(storage)
        self.interactions = PendingInteractionManager(storage)
        self.first_message = first_message
        self.process_generation = 1
        self.turn_start_calls: list[dict] = []
        self.thread_start_calls: list[dict] = []
        self._turn_counter = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def thread_resume(self, thread_id: str, cwd: str, timeout_seconds: float | None = 60) -> dict:
        return {"threadId": thread_id}

    async def thread_start(self, **kwargs: object) -> dict:
        self.thread_start_calls.append(dict(kwargs))
        return {"threadId": "thread-new"}

    async def thread_name_set(self, thread_id: str, name: str) -> dict:
        return {}

    async def turn_start(self, **kwargs: object) -> dict:
        self._turn_counter += 1
        turn_id = "turn-fake" if self._turn_counter == 1 else f"turn-fake-{self._turn_counter}"
        thread_id = str(kwargs["thread_id"])
        self.turn_start_calls.append(dict(kwargs))
        self.tracker.register_turn(
            turn_id=turn_id,
            thread_id=thread_id,
            chat_id=kwargs.get("chat_id") if isinstance(kwargs.get("chat_id"), str) else thread_id,
            project_id=kwargs.get("project_id") if isinstance(kwargs.get("project_id"), str) else None,
            project_path=kwargs.get("project_path") if isinstance(kwargs.get("project_path"), str) else str(kwargs.get("cwd") or ""),
        )
        if self.first_message is not None:
            self.tracker.record_event(
                {
                    "method": "item/created",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {"type": "agentMessage", "text": self.first_message},
                    },
                },
                received_at="2026-05-25T00:00:01+00:00",
            )
        return {"turnId": turn_id}

    async def turn_interrupt(self, *, thread_id: str, turn_id: str, timeout_seconds: float | None = 30) -> dict:
        self.tracker.mark_turn_interrupted(turn_id, reason="test interrupt")
        return {"interrupted": True, "threadId": thread_id, "turnId": turn_id}

    def status_snapshot(self, *, include_recent_events: bool = False) -> dict:
        return {
            "ok": True,
            "running": True,
            "processGeneration": self.process_generation,
            "pendingRequests": 0,
            "activeTurns": self.tracker.running_turns(),
            "pendingInteractions": self.interactions.list_interactions(status="pending", limit=50),
        }

    async def restart(self, *, start_after_restart: bool, timeout_seconds: int, force: bool = False) -> dict:
        if force:
            self.tracker.mark_active_turns_unknown(process_generation=self.process_generation, reason="test forced restart")
        return {
            "ok": True,
            "restarted": True,
            "started": start_after_restart,
            "processGeneration": self.process_generation + int(start_after_restart),
            "activeWork": {
                "pendingRequests": 0,
                "activeTurns": self.tracker.running_turns(),
                "pendingInteractions": self.interactions.pending_count(),
            },
        }


class FakeToolService:
    def __init__(self, result: dict) -> None:
        self.result = result

    async def call(self, name: str, arguments: dict | None) -> dict:
        return self.result


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


def _storage_operation_row(
    operation_id: str,
    *,
    status: str = "queued",
    operation_type: str = "send_message",
    thread_id: str | None = None,
    turn_id: str | None = None,
    cwd: str = "D:\\fake",
    request: dict | None = None,
    updated_at: str = "2026-05-25T00:00:00+00:00",
) -> dict:
    payload = request or {
        "operation_type": operation_type,
        "chat_id": thread_id or "thread-test",
        "message": "durable operation test",
        "_skip_prompt_dedup": True,
    }
    return {
        "operation_id": operation_id,
        "client_request_id": f"client-{operation_id}",
        "operation_type": operation_type,
        "status": status,
        "phase": status,
        "project_id": "project-test",
        "chat_id": thread_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "workflow_id": None,
        "cwd": cwd,
        "title": None,
        "request_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "result_json": None,
        "last_error": None,
        "attempt_count": 0,
        "created_at": updated_at,
        "updated_at": updated_at,
        "started_at": None,
        "completed_at": None,
        "app_server_generation": None,
    }


class OperationLeaseStorageTests(unittest.TestCase):
    def test_workflow_state_defaults_and_operation_links_update(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                now = "2026-05-25T00:00:00+00:00"
                storage.create_workflow(
                    {
                        "workflow_id": "wf-storage",
                        "client_request_id": None,
                        "project_id": "project",
                        "thread_id": "",
                        "plan_turn_id": "",
                        "phase": "planning",
                        "status": "planning",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                created = storage.get_workflow("wf-storage") or {}
                storage.update_workflow(
                    "wf-storage",
                    current_operation_id="op-plan",
                    plan_operation_id="op-plan",
                    latest_plan_item_id="plan-1",
                    latest_plan_hash="hash-plan",
                    final_report_json='{"text":"done"}',
                    latest_report_hash="hash-report",
                    updated_at="2026-05-25T00:00:01+00:00",
                )
                updated = storage.get_workflow("wf-storage") or {}
            finally:
                storage.close()

        self.assertEqual("plan_then_execute", created["workflow_kind"])
        self.assertEqual("", created["thread_id"])
        self.assertIsNone(created["current_operation_id"])
        self.assertEqual("op-plan", updated["current_operation_id"])
        self.assertEqual("op-plan", updated["plan_operation_id"])
        self.assertEqual("plan-1", updated["latest_plan_item_id"])
        self.assertEqual("hash-plan", updated["latest_plan_hash"])
        self.assertEqual("hash-report", updated["latest_report_hash"])
        self.assertEqual('{"text":"done"}', updated["final_report_json"])

    def test_operation_lease_acquire_release_and_expired_pickup(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                storage.create_operation(_storage_operation_row("op-lease"))
                now = "2026-05-25T00:00:00+00:00"
                first = storage.acquire_operation_lease(
                    "op-lease",
                    lease_owner="worker-1",
                    now=now,
                    lease_expires_at="2026-05-25T00:02:00+00:00",
                )
                blocked = storage.acquire_operation_lease(
                    "op-lease",
                    lease_owner="worker-2",
                    now="2026-05-25T00:00:30+00:00",
                    lease_expires_at="2026-05-25T00:02:30+00:00",
                )
                heartbeat = storage.heartbeat_operation_lease(
                    "op-lease",
                    lease_owner="worker-1",
                    now="2026-05-25T00:01:00+00:00",
                    lease_expires_at="2026-05-25T00:03:00+00:00",
                )
                storage.release_operation_lease("op-lease", lease_owner="worker-1", updated_at="2026-05-25T00:01:01+00:00")
                second = storage.acquire_operation_lease(
                    "op-lease",
                    lease_owner="worker-2",
                    now="2026-05-25T00:01:02+00:00",
                    lease_expires_at="2026-05-25T00:03:02+00:00",
                )
            finally:
                storage.close()

        self.assertIsNotNone(first)
        self.assertEqual("worker-1", first["lease_owner"])
        self.assertIsNone(blocked)
        self.assertTrue(heartbeat)
        self.assertIsNotNone(second)
        self.assertEqual("worker-2", second["lease_owner"])

    def test_startup_recovery_resets_starting_without_turn_and_preserves_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                storage.create_operation(_storage_operation_row("op-reset", status="starting_thread"))
                storage.create_operation(
                    _storage_operation_row("op-running", status="starting_turn", thread_id="thread-r", turn_id="turn-r")
                )
                storage.update_operation(
                    "op-reset",
                    lease_owner="old-worker",
                    lease_expires_at="2026-05-25T00:00:00+00:00",
                    updated_at="2026-05-25T00:00:00+00:00",
                )
                recovered = storage.recover_startup_operations(now="2026-05-25T00:05:00+00:00")
                reset = storage.get_operation("op-reset") or {}
                running = storage.get_operation("op-running") or {}
            finally:
                storage.close()

        self.assertEqual(["op-reset"], recovered["resetOperationIds"])
        self.assertEqual(["op-running"], recovered["runningOperationIds"])
        self.assertEqual("queued", reset["status"])
        self.assertIsNone(reset["lease_owner"])
        self.assertEqual("running", running["status"])
        self.assertEqual("turn-r", running["turn_id"])

    def test_cleanup_prompt_submissions_deletes_only_old_terminal_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                old = "2026-05-01T00:00:00+00:00"
                fresh = "2026-05-25T00:00:00+00:00"
                for prompt_id, status, updated_at in [
                    ("ps-old-done", "completed", old),
                    ("ps-old-active", "queued", old),
                    ("ps-fresh-done", "completed", fresh),
                ]:
                    storage.create_prompt_submission(
                        {
                            "prompt_submission_id": prompt_id,
                            "project_id": "project-test",
                            "project_path_key": "project-test",
                            "operation_type": "start_chat",
                            "prompt_hash": prompt_id,
                            "prompt_normalized": f"normalized {prompt_id}",
                            "prompt_preview": "preview",
                            "operation_id": None,
                            "chat_id": None,
                            "thread_id": None,
                            "turn_id": None,
                            "workflow_id": None,
                            "status": status,
                            "duplicate_of_submission_id": None,
                            "similarity": None,
                            "created_at": updated_at,
                            "updated_at": updated_at,
                        }
                    )
                dry = storage.cleanup_prompt_submissions(older_than="2026-05-10T00:00:00+00:00", dry_run=True)
                real = storage.cleanup_prompt_submissions(older_than="2026-05-10T00:00:00+00:00", dry_run=False)
                remaining = storage.list_prompt_submissions_for_project("project-test", limit=10)
            finally:
                storage.close()

        self.assertEqual(1, dry["matchedPromptSubmissions"])
        self.assertEqual(0, dry["deletedPromptSubmissions"])
        self.assertEqual(1, real["deletedPromptSubmissions"])
        self.assertEqual({"ps-old-active", "ps-fresh-done"}, {row["prompt_submission_id"] for row in remaining})


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
                "last_assistant_message": "Final hook answer with Bearer abcdefghijklmnop",
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
                projects = service.codex_list_projects()["projects"]
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
                "codex_get_workflow_status",
                "codex_approve_plan",
                "codex_get_turn_status",
                "codex_execute_plan",
                "codex_list_pending_interactions",
                "codex_answer_pending_interaction",
                "codex_interrupt_turn",
                "codex_restart_app_server",
                "codex_get_app_server_status",
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
        self.assertEqual("never", start_schema["approval_policy"]["default"])
        self.assertIn("ask_openclaw", start_schema["approval_policy"]["enum"])
        self.assertIn("plan", start_schema["collaboration_mode"]["enum"])
        self.assertEqual("danger-full-access", start_schema["sandbox"]["default"])

        self.assertEqual("never", send_schema["approval_policy"]["default"])
        self.assertIn("ask_openclaw", send_schema["approval_policy"]["enum"])
        self.assertIn("plan", send_schema["collaboration_mode"]["enum"])
        self.assertEqual("danger-full-access", send_schema["sandbox"]["default"])

        submit_schema = by_name["codex_submit_task"]["inputSchema"]
        self.assertEqual(["operation_type", "message"], submit_schema["required"])
        self.assertIn("start_chat", submit_schema["properties"]["operation_type"]["enum"])
        self.assertIn("send_message", submit_schema["properties"]["operation_type"]["enum"])
        self.assertIn("execute_plan", submit_schema["properties"]["operation_type"]["enum"])
        self.assertEqual("danger-full-access", submit_schema["properties"]["sandbox"]["default"])
        self.assertEqual("never", submit_schema["properties"]["approval_policy"]["default"])

        operation_status_schema = by_name["codex_get_operation_status"]["inputSchema"]
        self.assertEqual(["operation_id"], operation_status_schema["required"])
        self.assertEqual(10, operation_status_schema["properties"]["last_messages"]["default"])

        turn_status_schema = by_name["codex_get_turn_status"]["inputSchema"]["properties"]
        self.assertEqual(10, turn_status_schema["last_messages"]["default"])

        workflow_schema = by_name["codex_start_plan_workflow"]["inputSchema"]["properties"]
        self.assertEqual(0, workflow_schema["first_message_timeout_seconds"]["default"])
        self.assertEqual("danger-full-access", workflow_schema["sandbox"]["default"])
        self.assertEqual("never", workflow_schema["approval_policy"]["default"])

        workflow_status_schema = by_name["codex_get_workflow_status"]["inputSchema"]
        self.assertEqual(["workflow_id"], workflow_status_schema["required"])

        approve_plan_schema = by_name["codex_approve_plan"]["inputSchema"]
        self.assertEqual(["workflow_id"], approve_plan_schema["required"])
        self.assertEqual("Implement the plan.", approve_plan_schema["properties"]["message"]["default"])

        execute_plan_schema = by_name["codex_execute_plan"]["inputSchema"]
        self.assertNotIn("required", execute_plan_schema)
        self.assertIn("workflow_id", execute_plan_schema["properties"])
        self.assertIn("chat_id", execute_plan_schema["properties"])
        self.assertEqual("Implement the plan.", execute_plan_schema["properties"]["message"]["default"])

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

        restart_schema = by_name["codex_restart_app_server"]["inputSchema"]["properties"]
        self.assertIn("force", restart_schema)

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
        self.assertTrue(repair_schema["properties"]["dry_run"]["default"])
        self.assertEqual(30, repair_schema["properties"]["stale_after_minutes"]["default"])
        self.assertEqual(30, repair_schema["properties"]["older_than_days"]["default"])

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

    def test_diagnostic_redaction_and_classifier(self) -> None:
        text = "DEEPSEEK_API_KEY=secret-value Authorization: Bearer abcdefghijklmnop sk-1234567890abcdef 123456789:abcdefghijklmnopqrstuvwxyz"
        redacted = redact_text(text)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)
        self.assertNotIn("sk-1234567890abcdef", redacted)
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
                        "status": "running",
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

    def test_search_query_parser_handles_modes_and_escaping(self) -> None:
        phrase = build_fts_query('"точная фраза"', "auto")
        all_terms = build_fts_query("alpha beta!", "all_terms")
        any_term = build_fts_query("alpha beta", "any_term")
        punctuation = build_fts_query("!!!", "auto")

        self.assertEqual("phrase", phrase.match_mode)
        self.assertEqual('"точная фраза"', phrase.fts_query)
        self.assertEqual("all_terms", all_terms.match_mode)
        self.assertEqual('"alpha" "beta"', all_terms.fts_query)
        self.assertEqual('"alpha" OR "beta"', any_term.fts_query)
        self.assertEqual('"__openclaw_no_terms__"', punctuation.fts_query)

    def test_project_cache_upsert_deduplicates_by_path(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "state.sqlite3")
            storage.connect()
            try:
                base = {
                    "project_id": "registry-id",
                    "name": "OpenClaw",
                    "path": "D:\\CodexProjects\\OpenClaw",
                    "normalized_path_key": "d:/codexprojects/openclaw",
                    "created_at": None,
                    "last_activity_at": None,
                    "source": "mixed",
                    "updated_at": "2026-05-25T00:00:00+00:00",
                }
                storage.upsert_project(base)
                storage.upsert_project(
                    {
                        **base,
                        "project_id": "computed-id",
                        "name": "OpenClaw updated",
                        "source": "sqlite",
                        "updated_at": "2026-05-25T00:01:00+00:00",
                    }
                )
                storage.commit()

                rows = storage.connection.execute("SELECT project_id, name, source FROM projects").fetchall()
                self.assertEqual(1, len(rows))
                self.assertEqual("registry-id", rows[0]["project_id"])
                self.assertEqual("OpenClaw updated", rows[0]["name"])
                self.assertEqual("sqlite", rows[0]["source"])
            finally:
                storage.close()

    def test_catalog_prefers_canonical_existing_path_over_registry_casing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Vibecoding1c"
            project.mkdir()
            wrong_case = project.parent / "vibecoding1c"
            if os.name != "nt" and not wrong_case.exists():
                try:
                    wrong_case.symlink_to(project, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"case-alias symlink is not available: {exc}")
            wrong_case_path = str(wrong_case)
            registry = root / "projects.json"
            registry.write_text(
                json.dumps(
                    [
                        {
                            "project_id": "registry-vibe-id",
                            "name": "vibecoding1c",
                            "root_path": wrong_case_path,
                            "created_at": "2026-05-25T00:00:00+00:00",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            state_db = root / ".codex" / "state_5.sqlite"
            state_db.parent.mkdir(parents=True)
            _create_threads_db(
                state_db,
                [
                    {
                        "id": "thread-vibe",
                        "rollout_path": None,
                        "cwd": str(project),
                        "title": "Vibe",
                        "preview": "",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667200000,
                        "archived": 0,
                    }
                ],
            )
            config = _search_service_config(root, state_db)
            config.projects_registry_path = registry
            config.allowed_roots = [project]
            service = ToolService(config)
            try:
                projects = service.codex_list_projects()["projects"]
                chats = service.codex_list_project_chats({"project_id": "registry-vibe-id", "include_archived": True})["chats"]
                vibe_project = next(item for item in projects if item["project_id"] == "registry-vibe-id")
                self.assertEqual("Vibecoding1c", vibe_project["name"])
                self.assertTrue(os.path.samefile(project, vibe_project["path"]))
                self.assertTrue(os.path.samefile(project, chats[0]["project_path"]))
            finally:
                asyncio.run(service.close())

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
                                    "status": "completed",
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
                        "status": "running",
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
        self.assertEqual("running", result["status"])
        self.assertEqual(["live assistant"], [item["text"] for item in result["last_messages"]])

    def test_pending_interaction_tools_list_and_answer_live_request(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict]:
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
                    return interaction, listed, turn_status, {"answered": answered, "row": row}
                finally:
                    await service.close()

        interaction, listed, turn_status, result = asyncio.run(scenario())

        self.assertEqual("pending", interaction["status"])
        self.assertEqual(1, listed["returned_count"])
        self.assertEqual("waiting_for_approval", turn_status["status"])
        self.assertEqual("item-1", turn_status["pendingInteractions"][0]["itemId"])
        self.assertTrue(result["answered"]["answered"])
        self.assertEqual("answered", result["row"]["status"])

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

    def test_submit_task_start_chat_is_durable_idempotent_and_reconciles_completion(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, list[dict], dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                sessions = root / ".codex" / "sessions"
                sessions.mkdir(parents=True)
                transcript = sessions / "rollout-thread-existing.jsonl"
                _write_transcript(transcript, "thread-existing", project, [("user", "hello")])
                state_db = root / ".codex" / "state_5.sqlite"
                _create_threads_db(
                    state_db,
                    [
                        {
                            "id": "thread-existing",
                            "rollout_path": str(transcript),
                            "cwd": str(project),
                            "title": "Existing",
                            "preview": "",
                            "created_at_ms": 1779667200000,
                            "updated_at_ms": 1779667200000,
                            "archived": 0,
                        }
                    ],
                )
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message="operation first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "do durable work",
                            "client_request_id": "operation-start-1",
                        },
                    )
                    accepted = submitted
                    for _ in range(50):
                        accepted = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if accepted.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    repeated = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "do durable work",
                            "client_request_id": "operation-start-1",
                        },
                    )
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
                    stored = service.storage.get_operation(str(submitted["operationId"])) or {}
                    return submitted, accepted, repeated, completed, fake.turn_start_calls, stored
                finally:
                    await service.close()

        submitted, accepted, repeated, completed, calls, stored = asyncio.run(scenario())

        self.assertEqual("operation-start-1", submitted["clientRequestId"])
        self.assertIn(submitted["status"], {"queued", "starting_app_server", "starting_thread", "running"})
        self.assertEqual("running", accepted["status"])
        self.assertEqual("thread-new", accepted["threadId"])
        self.assertEqual("turn-fake", accepted["turnId"])
        self.assertEqual("durable_queue", accepted["operationSource"])
        self.assertIn(accepted["leaseState"]["state"], {"none", "owned_by_this_worker"})
        self.assertEqual("new", accepted["dedupState"]["state"])
        self.assertEqual(1, accepted["attemptCount"])
        self.assertEqual(["operation first"], [item["text"] for item in accepted["latestMessages"]])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(submitted["operationId"], repeated["operationId"])
        self.assertEqual(1, len(calls))
        self.assertEqual("completed", completed["status"])
        self.assertEqual("completed", completed["phase"])
        self.assertFalse(completed["pollRecommended"])
        self.assertEqual("completed", stored["status"])

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

    def test_submit_task_inactive_duplicate_continues_existing_thread(self) -> None:
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

    def test_search_chats_ranks_groups_filters_and_excludes_operational_records(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_a = root / "ProjectA"
            project_b = root / "ProjectB"
            project_a.mkdir()
            project_b.mkdir()
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            transcript_title = sessions / "rollout-title.jsonl"
            transcript_message = sessions / "rollout-message.jsonl"
            transcript_archived = sessions / "rollout-archived.jsonl"
            _write_transcript(transcript_title, "thread-title", project_a, [("user", "ordinary request")])
            _write_transcript(
                transcript_message,
                "thread-message",
                project_a,
                [("user", "please investigate alpha beta in logs"), ("assistant", "alpha beta was found")],
                extra_rows=[
                    {
                        "timestamp": "2026-05-25T00:00:08Z",
                        "type": "response_item",
                        "payload": {"type": "function_call", "name": "shell", "arguments": "secret tool phrase"},
                    }
                ],
            )
            _write_transcript(transcript_archived, "thread-archived", project_b, [("user", "alpha beta archived note")])
            state_db = root / ".codex" / "state_5.sqlite"
            _create_threads_db(
                state_db,
                [
                    {
                        "id": "thread-title",
                        "rollout_path": str(transcript_title),
                        "cwd": str(project_a),
                        "title": "Alpha Beta title match",
                        "preview": "metadata preview",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667300000,
                        "archived": 0,
                    },
                    {
                        "id": "thread-message",
                        "rollout_path": str(transcript_message),
                        "cwd": str(project_a),
                        "title": "Message only",
                        "preview": "other preview",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667250000,
                        "archived": 0,
                    },
                    {
                        "id": "thread-archived",
                        "rollout_path": str(transcript_archived),
                        "cwd": str(project_b),
                        "title": "Archived",
                        "preview": "alpha beta archived",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667350000,
                        "archived": 1,
                    },
                ],
            )
            service = ToolService(_search_service_config(root, state_db))
            try:
                result = service.codex_search_chats({"query": "alpha beta", "match_mode": "all_terms", "limit": 10})
                archived = service.codex_search_chats({"query": "alpha beta", "include_archived": True, "limit": 10})
                project_b_result = service.codex_search_chats(
                    {
                        "query": "alpha beta",
                        "include_archived": True,
                        "project_id": project_id_for_path(str(project_b)),
                        "limit": 10,
                    }
                )
                tool_only = service.codex_search_chats({"query": "secret tool phrase", "limit": 10})
            finally:
                asyncio.run(service.close())

            self.assertEqual(2, result["total_results"])
            self.assertEqual("thread-title", result["results"][0]["thread_id"])
            self.assertEqual("thread-message", result["results"][1]["thread_id"])
            self.assertEqual(["message"], result["results"][1]["matched_fields"])
            self.assertEqual(3, archived["total_results"])
            self.assertEqual(["thread-archived"], [item["thread_id"] for item in project_b_result["results"]])
            self.assertEqual(0, tool_only["total_results"])
            self.assertEqual(0, result["budget"]["deepseek_calls"])

    def test_search_chats_limit_total_and_transcript_invalidation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            state_rows = []
            for index in range(12):
                thread_id = f"thread-{index:02d}"
                transcript = sessions / f"rollout-{thread_id}.jsonl"
                _write_transcript(transcript, thread_id, project, [("user", f"needle before message {index}")])
                state_rows.append(
                    {
                        "id": thread_id,
                        "rollout_path": str(transcript),
                        "cwd": str(project),
                        "title": f"Chat {index}",
                        "preview": "",
                        "created_at_ms": 1779667200000 + index,
                        "updated_at_ms": 1779667200000 + index,
                        "archived": 0,
                    }
                )
            state_db = root / ".codex" / "state_5.sqlite"
            _create_threads_db(state_db, state_rows)
            config = _search_service_config(root, state_db)
            service = ToolService(config)
            try:
                first = service.codex_search_chats({"query": "needle before", "limit": 10})
                changed_transcript = sessions / "rollout-thread-00.jsonl"
                _write_transcript(changed_transcript, "thread-00", project, [("user", "needle after replacement")])
                second = service.codex_search_chats({"query": "needle after", "limit": 10})
            finally:
                asyncio.run(service.close())

            self.assertEqual(12, first["total_results"])
            self.assertEqual(10, first["returned_count"])
            self.assertEqual("10", first["next_cursor"])
            self.assertGreaterEqual(second["total_results"], 1)
            self.assertEqual("thread-00", second["results"][0]["thread_id"])
            self.assertGreaterEqual(second["index_status"]["indexed_files"], 1)

    def test_kb_history_drives_project_search_status_and_get_chat(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            kb_root = root / "kb_history" / "projects"
            _write_kb_turn(
                kb_root,
                project,
                "kb-thread",
                "turn-old",
                [("user", "old kb user alpha"), ("assistant", "old kb assistant")],
                created_at="2026-05-25T00:00:00Z",
                updated_at="2026-05-25T00:00:10Z",
            )
            _write_kb_turn(
                kb_root,
                project,
                "kb-thread",
                "turn-new",
                [("user", "latest kb user needle"), ("assistant", "latest kb assistant needle")],
                created_at="2026-05-25T00:01:00Z",
                updated_at="2026-05-25T00:01:10Z",
            )
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                projects = service.codex_list_projects()["projects"]
                search = service.codex_search_chats({"query": "needle", "limit": 10})
                status = service.codex_get_chat_status({"chat_id": "kb-thread"})
                chat = service.codex_get_chat({"chat_id": "kb-thread", "range": {"mode": "all"}, "format": "structured"})
                turn_status = service.codex_get_turn_status({"turn_id": "turn-new", "thread_id": "kb-thread"})
            finally:
                asyncio.run(service.close())

            self.assertIn(path_key(project), [path_key(item["path"]) for item in projects])
            self.assertEqual("chat_search_fts_index", search["source"])
            self.assertEqual(1, search["total_results"])
            self.assertEqual("kb-thread", search["results"][0]["thread_id"])
            self.assertEqual("kb_history", status["transcript"]["source"])
            self.assertEqual("latest kb user needle", status["last_user_preview"])
            self.assertEqual("latest kb assistant needle", status["last_assistant_preview"])
            self.assertTrue(chat["source"].startswith("kb_history+"))
            self.assertEqual(["latest kb user needle", "latest kb assistant needle"], [item["text"] for item in chat["messages"]])
            self.assertEqual("kb_history", turn_status["source"])
            self.assertEqual("completed", turn_status["status"])
            self.assertEqual(["latest kb assistant needle"], [item["text"] for item in turn_status["last_messages"]])

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

    def test_summary_cache_and_rolling_summary_storage(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "state.sqlite3")
            storage.connect()
            try:
                summary = {
                    "mode": "before_latest_user",
                    "status": "ok",
                    "text": "cached summary",
                    "deepseek_calls": 1,
                    "estimated_chars_sent_to_deepseek": 500,
                }
                storage.upsert_summary_cache(
                    {
                        "cache_key": "cache-1",
                        "thread_id": "thread-1",
                        "transcript_path": "rollout-thread-1.jsonl",
                        "transcript_size": 100,
                        "transcript_mtime_ns": 200,
                        "boundary_line": 10,
                        "model": "deepseek-v4-flash",
                        "filter_version": "test",
                        "summary_json": json.dumps(summary),
                        "created_at": "2026-05-25T00:00:00+00:00",
                        "last_used_at": "2026-05-25T00:00:00+00:00",
                    }
                )
                storage.upsert_rolling_summary(
                    {
                        "thread_id": "thread-1",
                        "transcript_path": "rollout-thread-1.jsonl",
                        "source_line_end": 10,
                        "summary_text": "rolling summary",
                        "model": "deepseek-v4-flash",
                        "updated_at": "2026-05-25T00:01:00+00:00",
                    }
                )

                cached = storage.get_summary_cache("cache-1", "2026-05-25T00:02:00+00:00")
                rolling = storage.get_rolling_summary("thread-1", "rollout-thread-1.jsonl", 20)

                self.assertIsNotNone(cached)
                self.assertEqual("cached summary", cached["text"])
                self.assertTrue(storage.has_summary_cache_for_thread("thread-1"))
                self.assertIsNotNone(rolling)
                self.assertEqual("rolling summary", rolling["summary_text"])
            finally:
                storage.close()

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

    def test_get_chat_returns_summary_and_latest_raw_tail(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            codex_home = root / ".codex"
            sessions = codex_home / "sessions"
            archived = codex_home / "archived_sessions"
            sessions.mkdir(parents=True)
            archived.mkdir(parents=True)
            transcript = sessions / "rollout-thread-1.jsonl"
            rows = [
                {"timestamp": "2026-05-25T00:00:00Z", "type": "session_meta", "payload": {"id": "thread-1", "cwd": str(project)}},
                {"timestamp": "2026-05-25T00:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1"}},
                {"timestamp": "2026-05-25T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "old user"}},
                {
                    "timestamp": "2026-05-25T00:00:03Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": "old assistant"},
                },
                {"timestamp": "2026-05-25T00:00:04Z", "type": "turn_context", "payload": {"turn_id": "turn-2"}},
                {"timestamp": "2026-05-25T00:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "latest user"}},
                {
                    "timestamp": "2026-05-25T00:00:06Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": "latest assistant"},
                },
                {"timestamp": "2026-05-25T00:00:06Z", "type": "event_msg", "payload": {"type": "context_compacted"}},
                {"timestamp": "2026-05-25T00:00:07Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-2"}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            config = ServerConfig(
                codex_home=codex_home,
                sessions_dir=sessions,
                archived_sessions_dir=archived,
                codex_state_db=codex_home / "state_5.sqlite",
                codex_logs_db=codex_home / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[root],
                deepseek_env_path=root / "missing.env",
            )
            service = ToolService(config)
            try:
                service.storage.upsert_tracked_plan_item(
                    {
                        "item_id": "plan-chat",
                        "turn_id": "turn-2",
                        "thread_id": "thread-1",
                        "status": "completed",
                        "text": "Chat plan",
                        "created_at": "2026-05-25T00:00:05+00:00",
                        "updated_at": "2026-05-25T00:00:06+00:00",
                        "completed_at": "2026-05-25T00:00:06+00:00",
                        "sequence": 1,
                        "payload_json": "{}",
                    }
                )
                result = service.codex_get_chat({"chat_id": "thread-1", "range": {"mode": "all"}, "format": "structured"})
            finally:
                asyncio.run(service.close())

            self.assertEqual("skipped_small_history", result["history_summary"]["status"])
            self.assertEqual(2, result["history_summary"]["messages_summarized"])
            self.assertEqual(0, result["history_summary"]["deepseek_calls"])
            self.assertEqual(["latest user", "latest assistant"], [item["text"] for item in result["messages"]])
            self.assertEqual(["user", "assistant"], [item["role"] for item in result["messages"]])
            self.assertNotIn("old user", [item["text"] for item in result["messages"]])
            self.assertEqual("Chat plan", result["latestPlan"]["markdown"])
            self.assertIn("budget", result)

    def test_get_chat_tail_cap_and_items_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            codex_home = root / ".codex"
            sessions = codex_home / "sessions"
            archived = codex_home / "archived_sessions"
            sessions.mkdir(parents=True)
            archived.mkdir(parents=True)
            transcript = sessions / "rollout-thread-tail.jsonl"
            rows = [
                {"timestamp": "2026-05-25T00:00:00Z", "type": "session_meta", "payload": {"id": "thread-tail", "cwd": str(project)}},
                {"timestamp": "2026-05-25T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "old user"}},
                {"timestamp": "2026-05-25T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "old assistant"}},
                {"timestamp": "2026-05-25T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "latest user"}},
                {"timestamp": "2026-05-25T00:00:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "tail 1"}},
                {"timestamp": "2026-05-25T00:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "tail 2"}},
                {"timestamp": "2026-05-25T00:00:06Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "tail 3"}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            config = ServerConfig(
                codex_home=codex_home,
                sessions_dir=sessions,
                archived_sessions_dir=archived,
                codex_state_db=codex_home / "state_5.sqlite",
                codex_logs_db=codex_home / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[root],
                deepseek_env_path=root / "missing.env",
            )
            service = ToolService(config)
            try:
                result = service.codex_get_chat(
                    {
                        "chat_id": "thread-tail",
                        "range": {"mode": "all"},
                        "format": "structured",
                        "tail_max_messages": 2,
                    }
                )
            finally:
                asyncio.run(service.close())

            self.assertTrue(result["tail_truncated"])
            self.assertEqual(2, len(result["messages"]))
            self.assertEqual(["tail 2", "tail 3"], [item["text"] for item in result["messages"]])
            self.assertTrue(all(item["items"] == [] for item in result["messages"]))
            self.assertTrue(all(item["items_available"] for item in result["messages"]))

    def test_get_chat_status_is_lightweight(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            codex_home = root / ".codex"
            sessions = codex_home / "sessions"
            archived = codex_home / "archived_sessions"
            sessions.mkdir(parents=True)
            archived.mkdir(parents=True)
            transcript = sessions / "rollout-thread-status.jsonl"
            rows = [
                {"timestamp": "2026-05-25T00:00:00Z", "type": "session_meta", "payload": {"id": "thread-status", "cwd": str(project)}},
                {"timestamp": "2026-05-25T00:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1"}},
                {"timestamp": "2026-05-25T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "status user"}},
                {"timestamp": "2026-05-25T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "status assistant"}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            config = ServerConfig(
                codex_home=codex_home,
                sessions_dir=sessions,
                archived_sessions_dir=archived,
                codex_state_db=codex_home / "state_5.sqlite",
                codex_logs_db=codex_home / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[root],
                deepseek_env_path=root / "missing.env",
            )
            service = ToolService(config)
            try:
                result = service.codex_get_chat_status({"chat_id": "thread-status"})
            finally:
                asyncio.run(service.close())

            self.assertEqual("thread-status", result["thread_id"])
            self.assertEqual("status user", result["last_user_preview"])
            self.assertEqual("status assistant", result["last_assistant_preview"])
            self.assertNotIn("messages", result)
            self.assertNotIn("history_summary", result)
            self.assertEqual(0, result["budget"]["deepseek_calls"])

    def test_split_selected_messages_expands_tail_to_previous_user(self) -> None:
        from openclaw_codex_mcp.models import TranscriptMessage

        def message(role: str, text: str, line: int) -> TranscriptMessage:
            return TranscriptMessage(
                message_id=str(line),
                thread_id="thread-1",
                turn_id="turn-1",
                role=role,
                created_at=None,
                text=text,
                items=[],
                metadata={},
                source_line_start=line,
                source_line_end=line,
            )

        messages = [
            message("user", "old", 1),
            message("assistant", "old answer", 2),
            message("user", "latest", 3),
            message("assistant", "tail 1", 4),
            message("tool", "tail 2", 5),
        ]

        split, expanded = _split_selected_messages(messages, messages[-2:])

        self.assertTrue(expanded)
        self.assertEqual(["old", "old answer"], [item.text for item in split.upper])
        self.assertEqual(["latest", "tail 1", "tail 2"], [item.text for item in split.lower])


if __name__ == "__main__":
    unittest.main()
