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
from openclaw_codex_mcp.diagnostics import analyze_context, event_to_tool, redact_payload, redact_text
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
    _config_fingerprint,
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
        self.process = SimpleNamespace(returncode=None)
        self.turn_start_calls: list[dict] = []
        self.turn_steer_calls: list[dict] = []
        self.review_start_calls: list[dict] = []
        self.thread_start_calls: list[dict] = []
        self.thread_resume_calls: list[dict] = []
        self.thread_fork_calls: list[dict] = []
        self.thread_archive_calls: list[dict] = []
        self.thread_unarchive_calls: list[dict] = []
        self.thread_compact_start_calls: list[dict] = []
        self.thread_goal_get_calls: list[dict] = []
        self.thread_goal_set_calls: list[dict] = []
        self.thread_goal_clear_calls: list[dict] = []
        self.thread_goal_failures: dict[str, Exception] = {}
        self.thread_goals: dict[str, dict] = {}
        self.inventory_calls: list[str] = []
        self.inventory_failures: dict[str, Exception] = {}
        self.account_response: dict = {
            "requiresOpenaiAuth": False,
            "account": {"type": "chatgpt", "email": "user@example.com", "planType": "pro", "accountId": "acct_secret"},
        }
        self.account_usage_response: dict = {
            "summary": {
                "lifetimeTokens": 123456789,
                "peakDailyTokens": 987654,
                "currentStreakDays": 9,
                "longestStreakDays": 42,
                "longestRunningTurnSec": 7200,
            },
            "dailyUsageBuckets": [{"startDate": "2026-06-17", "tokens": 12345}],
        }
        self.account_rate_limits_response: dict = {
            "rateLimits": {
                "limitId": "private-limit-id",
                "limitName": "Private Team Bucket",
                "planType": "pro",
                "rateLimitReachedType": "none",
                "credits": {"hasCredits": True, "unlimited": False, "balance": 99.95},
                "primary": {"usedPercent": 12.5, "resetsAt": "2026-06-18T12:00:00+00:00", "windowDurationMins": 300},
                "secondary": {"usedPercent": 91.0, "resetsAt": "2026-06-18T13:00:00+00:00", "windowDurationMins": 10080},
                "individualLimit": {"limit": 100.0, "used": 12.0, "remainingPercent": 88.0, "resetsAt": "2026-06-19T00:00:00+00:00"},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "limitName": "Codex",
                    "planType": "pro",
                    "rateLimitReachedType": "none",
                    "credits": {"hasCredits": True, "unlimited": False, "balance": 99.95},
                    "primary": {"usedPercent": 12.5, "resetsAt": "2026-06-18T12:00:00+00:00", "windowDurationMins": 300},
                },
                "private-limit-id": {
                    "limitId": "private-limit-id",
                    "limitName": "Private Team Bucket",
                    "planType": "pro",
                    "rateLimitReachedType": "hard",
                    "credits": {"hasCredits": False, "unlimited": False, "balance": 0.0},
                    "primary": {"usedPercent": 100.0, "resetsAt": "2026-06-18T12:00:00+00:00", "windowDurationMins": 300},
                },
            },
        }
        self.initialize_result = {
            "protocolVersion": "2025-01-10",
            "serverInfo": {"name": "codex-app-server", "version": "test"},
            "platform": "windows",
            "userAgent": "codex-app-server-test",
        }
        self._turn_counter = 0
        self._fork_counter = 0
        self._review_counter = 0
        self.thread_fork_failure: Exception | None = None
        self.review_start_failure: Exception | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def status_snapshot(self, include_recent_events: bool = False) -> dict:
        return {
            "running": True,
            "started": True,
            "pid": 12345,
            "processGeneration": self.process_generation,
            "pendingRequests": 0,
            "activeTurns": [],
        }

    async def thread_resume(self, thread_id: str, cwd: str, timeout_seconds: float | None = 60) -> dict:
        self.thread_resume_calls.append({"thread_id": thread_id, "cwd": cwd, "timeout_seconds": timeout_seconds})
        return {"threadId": thread_id}

    async def thread_start(self, **kwargs: object) -> dict:
        self.thread_start_calls.append(dict(kwargs))
        return {"threadId": "thread-new"}

    async def thread_fork(self, **kwargs: object) -> dict:
        self._fork_counter += 1
        thread_id = "thread-fork" if self._fork_counter == 1 else f"thread-fork-{self._fork_counter}"
        self.thread_fork_calls.append(dict(kwargs))
        if self.thread_fork_failure is not None:
            raise self.thread_fork_failure
        return {
            "thread": {"id": thread_id},
            "cwd": kwargs.get("cwd"),
            "approvalPolicy": kwargs.get("approval_policy"),
            "sandbox": kwargs.get("sandbox"),
            "model": kwargs.get("model"),
            "_processGeneration": self.process_generation,
        }

    async def thread_name_set(self, thread_id: str, name: str) -> dict:
        return {}

    async def thread_archive(self, thread_id: str, timeout_seconds: float | None = 30) -> dict:
        self.thread_archive_calls.append({"thread_id": thread_id, "timeout_seconds": timeout_seconds})
        return {"archived": True, "threadId": thread_id, "_processGeneration": self.process_generation}

    async def thread_unarchive(self, thread_id: str, timeout_seconds: float | None = 30) -> dict:
        self.thread_unarchive_calls.append({"thread_id": thread_id, "timeout_seconds": timeout_seconds})
        return {"thread": {"id": thread_id}, "_processGeneration": self.process_generation}

    async def thread_compact_start(self, thread_id: str, timeout_seconds: float | None = 30) -> dict:
        self.thread_compact_start_calls.append({"thread_id": thread_id, "timeout_seconds": timeout_seconds})
        return {"started": True, "threadId": thread_id, "_processGeneration": self.process_generation}

    async def thread_goal_get(self, thread_id: str, timeout_seconds: float | None = 2) -> dict:
        self.thread_goal_get_calls.append({"thread_id": thread_id, "timeout_seconds": timeout_seconds})
        failure = self.thread_goal_failures.get("get")
        if failure is not None:
            raise failure
        goal = self.thread_goals.get(thread_id)
        return {"goal": dict(goal) if goal is not None else None, "_processGeneration": self.process_generation}

    async def thread_goal_set(
        self,
        thread_id: str,
        *,
        objective: str | None,
        status: str | None = "active",
        token_budget: int | None = None,
        timeout_seconds: float | None = 5,
    ) -> dict:
        self.thread_goal_set_calls.append(
            {
                "thread_id": thread_id,
                "objective": objective,
                "status": status,
                "token_budget": token_budget,
                "timeout_seconds": timeout_seconds,
            }
        )
        failure = self.thread_goal_failures.get("set")
        if failure is not None:
            raise failure
        existing = self.thread_goals.get(thread_id) or {}
        goal = {
            "threadId": thread_id,
            "objective": objective or "",
            "status": status or "active",
            "tokenBudget": token_budget,
            "tokensUsed": int(existing.get("tokensUsed") or 0),
            "timeUsedSeconds": int(existing.get("timeUsedSeconds") or 0),
            "createdAt": int(existing.get("createdAt") or 1779667200000),
            "updatedAt": int(existing.get("updatedAt") or 1779667201000) + 1,
        }
        self.thread_goals[thread_id] = goal
        return {"goal": dict(goal), "_processGeneration": self.process_generation}

    async def thread_goal_clear(self, thread_id: str, timeout_seconds: float | None = 5) -> dict:
        self.thread_goal_clear_calls.append({"thread_id": thread_id, "timeout_seconds": timeout_seconds})
        failure = self.thread_goal_failures.get("clear")
        if failure is not None:
            raise failure
        self.thread_goals.pop(thread_id, None)
        return {"cleared": True, "_processGeneration": self.process_generation}

    async def review_start(
        self,
        *,
        thread_id: str,
        target: dict,
        delivery: str | None = None,
        timeout_seconds: float | None = 60,
    ) -> dict:
        if self.review_start_failure is not None:
            raise self.review_start_failure
        self._review_counter += 1
        review_thread_id = thread_id if delivery != "detached" else ("thread-review" if self._review_counter == 1 else f"thread-review-{self._review_counter}")
        turn_id = "turn-review" if self._review_counter == 1 else f"turn-review-{self._review_counter}"
        self.review_start_calls.append(
            {
                "thread_id": thread_id,
                "target": target,
                "delivery": delivery,
                "timeout_seconds": timeout_seconds,
            }
        )
        self.tracker.register_turn(
            turn_id=turn_id,
            thread_id=review_thread_id,
            chat_id=review_thread_id,
            project_id=None,
            project_path=None,
            status="running",
            process_generation=self.process_generation,
        )
        if self.first_message is not None:
            self.tracker.record_event(
                {
                    "method": "item/created",
                    "params": {
                        "threadId": review_thread_id,
                        "turnId": turn_id,
                        "item": {"type": "agentMessage", "text": self.first_message},
                    },
                },
                received_at="2026-05-25T00:00:01+00:00",
            )
        return {
            "reviewThreadId": review_thread_id,
            "turn": {"id": turn_id, "status": "inProgress", "items": []},
            "_processGeneration": self.process_generation,
        }

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

    async def turn_steer(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        input_items: list[dict],
        client_user_message_id: str | None = None,
        timeout_seconds: float | None = 60,
    ) -> dict:
        self.turn_steer_calls.append(
            {
                "thread_id": thread_id,
                "expected_turn_id": expected_turn_id,
                "input_items": input_items,
                "client_user_message_id": client_user_message_id,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"turnId": expected_turn_id}

    async def model_list(
        self,
        *,
        limit: int | None = 100,
        include_hidden: bool | None = False,
        cursor: str | None = None,
        timeout_seconds: float | None = 2,
    ) -> dict:
        self._maybe_fail_inventory("model/list")
        self.inventory_calls.append("model/list")
        return {
            "data": [
                {
                    "id": "gpt-5",
                    "model": "gpt-5",
                    "displayName": "GPT-5",
                    "isDefault": True,
                    "hidden": False,
                    "inputModalities": ["text", "image"],
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [{"id": "low"}, {"id": "medium"}, {"id": "high"}],
                    "serviceTiers": [{"id": "default"}, {"id": "priority"}],
                },
                {
                    "id": "hidden-model",
                    "model": "hidden-model",
                    "displayName": "Hidden",
                    "isDefault": False,
                    "hidden": True,
                    "inputModalities": ["text"],
                    "defaultReasoningEffort": "low",
                    "supportedReasoningEfforts": ["low"],
                    "serviceTiers": [],
                },
            ],
            "nextCursor": None,
        }

    async def permission_profile_list(
        self,
        *,
        cwd: str | None = None,
        limit: int | None = 100,
        cursor: str | None = None,
        timeout_seconds: float | None = 2,
    ) -> dict:
        self._maybe_fail_inventory("permissionProfile/list")
        self.inventory_calls.append("permissionProfile/list")
        return {
            "data": [
                {"id": "read-only", "description": "Read-only checks"},
                {"id": "danger-full-access", "description": "Full local access"},
            ],
            "nextCursor": None,
        }

    async def windows_sandbox_readiness(self, *, timeout_seconds: float | None = 2) -> dict:
        self._maybe_fail_inventory("windowsSandbox/readiness")
        self.inventory_calls.append("windowsSandbox/readiness")
        return {"status": "ready"}

    async def hooks_list(self, *, cwds: list[str], timeout_seconds: float | None = 2) -> dict:
        self._maybe_fail_inventory("hooks/list")
        self.inventory_calls.append("hooks/list")
        return {
            "data": [
                {
                    "cwd": cwds[0] if cwds else "",
                    "warnings": ["hook warning"],
                    "errors": [],
                    "hooks": [
                        {
                            "eventName": "UserPromptSubmit",
                            "source": "user",
                            "trustStatus": "trusted",
                            "enabled": True,
                            "handlerType": "command",
                            "isManaged": True,
                            "command": r"C:\Secret\run-hook.ps1 --token sk-secret",
                            "sourcePath": r"C:\Secret\hooks.json",
                        },
                        {
                            "eventName": "Stop",
                            "source": "project",
                            "trustStatus": "untrusted",
                            "enabled": False,
                            "handlerType": "command",
                            "isManaged": False,
                            "command": r"D:\private\stop.ps1",
                            "sourcePath": r"D:\private\hooks.json",
                        },
                    ],
                }
            ]
        }

    async def skills_list(self, *, cwds: list[str], force_reload: bool = False, timeout_seconds: float | None = 2) -> dict:
        self._maybe_fail_inventory("skills/list")
        self.inventory_calls.append("skills/list")
        return {
            "data": [
                {
                    "cwd": cwds[0] if cwds else "",
                    "errors": [],
                    "skills": [
                        {
                            "name": "humanizer",
                            "scope": "user",
                            "enabled": True,
                            "path": r"C:\Users\you\.codex\skills\humanizer\SKILL.md",
                            "description": "Make prose natural",
                        },
                        {
                            "name": "project-skill",
                            "scope": "project",
                            "enabled": False,
                            "path": r"D:\private\project\.codex\skills\project-skill\SKILL.md",
                            "description": "Project skill",
                        },
                    ],
                }
            ]
        }

    async def model_provider_capabilities_read(self, *, timeout_seconds: float | None = 2) -> dict:
        self._maybe_fail_inventory("modelProvider/capabilities/read")
        self.inventory_calls.append("modelProvider/capabilities/read")
        return {"webSearch": True, "imageGeneration": False, "namespaceTools": True}

    async def account_read(self, *, refresh_token: bool = False, timeout_seconds: float | None = 2) -> dict:
        self._maybe_fail_inventory("account/read")
        self.inventory_calls.append("account/read")
        result = dict(self.account_response)
        result["refreshToken"] = refresh_token
        return result

    async def account_usage_read(self, *, timeout_seconds: float | None = 2) -> dict:
        self._maybe_fail_inventory("account/usage/read")
        self.inventory_calls.append("account/usage/read")
        return dict(self.account_usage_response)

    async def account_rate_limits_read(self, *, timeout_seconds: float | None = 2) -> dict:
        self._maybe_fail_inventory("account/rateLimits/read")
        self.inventory_calls.append("account/rateLimits/read")
        return dict(self.account_rate_limits_response)

    def _maybe_fail_inventory(self, method: str) -> None:
        failure = self.inventory_failures.get(method)
        if failure is not None:
            raise failure

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



__all__ = [name for name in globals() if not name.startswith("__")]
