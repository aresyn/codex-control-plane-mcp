from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .agent_guidance import (
    attempt_scope_from_args,
    build_guidance_for_error,
    build_guidance_for_payload,
    build_post_repair_guidance,
    cooldown_after_attempt,
    guard_key_for,
    guidance_text,
    loop_guard_state,
)
from .catalog import ProjectChatCatalog, project_id_for_path
from .chat_summarizer import ChatHistorySplit, filter_meaningful_messages_for_summary, summarize_chat_history, split_before_latest_user
from .codex_app_server import CodexAppServerClient
from .config import ServerConfig, canonical_existing_path, is_allowed_path, path_key
from .deepseek_client import load_deepseek_settings
from .diagnostics import (
    analyze_context,
    check as diagnostic_check,
    event_to_tool,
    overall_status,
    read_log_files,
    redact_payload,
    redact_text,
)
from .errors import (
    CodexMcpError,
    busy,
    duplicate_prompt_active,
    invalid_argument,
    pending_interaction_unavailable,
    project_not_found,
    send_failed,
    thread_not_found,
    transcript_not_found,
    turn_not_found,
)
from .hook_history import HOOK_HISTORY_PREFIX
from .hook_installer import hook_status as installed_hook_status
from .logging_utils import get_logger
from .models import Chat, Project, TranscriptMessage, TranscriptSummary, TranscriptTurn
from .pending_interactions import PendingInteractionManager, interaction_row_to_tool
from .plan_quality import classify_plan_artifact, classify_plan_text, plan_candidate_payload, plan_hash_for_text, plan_quality_payload
from .prompt_dedup import DEFAULT_PROMPT_SIMILARITY_THRESHOLD, normalize_prompt, prompt_hash, prompt_similarity
from .protocol import with_output_schema
from .runtime_capabilities import (
    RUNTIME_CAPABILITIES_CACHE_TTL_SECONDS,
    compact_account_status,
    compact_account_usage,
    compact_hooks,
    compact_initialize_result,
    compact_models,
    compact_permission_profiles,
    compact_provider_capabilities,
    compact_rate_limits,
    compact_sandbox_readiness,
    compact_skills,
    now_iso as runtime_now_iso,
    runtime_health_subset,
    schema_methods_block,
)
from .search import SearchIndex
from .statuses import (
    OPERATION_ACTIVE_STATUSES,
    OPERATION_STARTABLE_STATUSES,
    OPERATION_TERMINAL_STATUSES,
    TURN_ACTIVE_STATUSES,
    TURN_COMPLETION_OBSERVED_STATUSES,
    TURN_TERMINAL_STATUSES,
)
from .storage import McpStorage
from .transcript_importer import import_transcript_to_tracking
from .transcripts import parse_transcript
from .turn_tracker import WAITING_FOR_OPENCLAW_ERROR, progress_event_to_tool, turn_progress_status_fields


UI_RELOAD_NOTE = "Desktop UI may not visually update until restart/reload for UI-started chats."
DEFAULT_TOOL_START_TIMEOUT_SECONDS = 300
DEFAULT_FIRST_MESSAGE_TIMEOUT_SECONDS = 0
OPERATION_LEASE_TTL_SECONDS = 120
OPERATION_HEARTBEAT_SECONDS = 30
OUTPUT_SCHEMA_MAX_CHARS = 50_000
IMAGE_INPUT_URL_MAX_CHARS = 8192
SUPPORTED_IMAGE_INPUT_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
SUPPORTED_IMAGE_INPUT_DETAILS = {"auto", "low", "high", "original"}
GOAL_COMPLETION_ACTIONS = {"clear", "set_complete", "leave"}
TRACKED_TURN_HISTORY_PREFIX = "tracked_turn:"
LOG = get_logger("tools")
PROMPT_OPERATION_ACTIVE_STATUSES = OPERATION_ACTIVE_STATUSES
CONTRACT_VERSION = "1"
SERVER_NAME = "codex-control-plane-mcp"
WORKER_COMMAND_TOOLS = {
    "codex_answer_pending_interaction",
    "codex_interrupt_turn",
    "codex_archive_thread",
    "codex_unarchive_thread",
    "codex_start_thread_compaction",
    "codex_restart_app_server",
    "codex_get_runtime_capabilities",
}

STABLE_OPENCLAW_TOOLS = {
    "codex_submit_task",
    "codex_get_operation_status",
    "codex_start_plan_workflow",
    "codex_start_review_workflow",
    "codex_get_workflow_status",
    "codex_adopt_workflow_plan",
    "codex_approve_plan",
    "codex_preflight_project_run",
    "codex_list_pending_interactions",
    "codex_answer_pending_interaction",
    "codex_interrupt_turn",
    "codex_archive_thread",
    "codex_unarchive_thread",
    "codex_start_thread_compaction",
    "codex_get_thread_compaction_status",
    "codex_get_worker_status",
    "codex_get_queue_status",
    "codex_get_concurrency_status",
    "codex_get_worker_command_status",
    "codex_get_runtime_capabilities",
    "codex_health_summary",
    "codex_collect_diagnostics",
    "codex_repair_issue",
}

COMPATIBILITY_TOOLS = {
    "codex_start_chat",
    "codex_send_message",
    "codex_execute_plan",
    "codex_list_projects",
    "codex_list_project_chats",
    "codex_list_active_chats",
    "codex_search_chats",
    "codex_get_chat_status",
    "codex_get_chat",
    "codex_get_turn_status",
    "codex_restart_app_server",
    "codex_get_app_server_status",
    "codex_get_diagnostic_logs",
    "codex_analyze_issue",
}

CLIENT_MODE_COMPATIBILITY_WRITE_TOOLS = {
    "codex_start_chat",
    "codex_send_message",
    "codex_execute_plan",
}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "codex_list_projects",
        "description": "List known Codex projects from the project registry, MCP hook history, transcript index, and read-only Codex state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "compact": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                "refresh": {"type": "boolean", "default": False},
                "include_private_details": {"type": "boolean", "default": False},
                "roots": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_list_project_chats",
        "description": "List Codex chats for a project with UI-like titles where available.",
        "inputSchema": {
            "type": "object",
            "required": ["project_id"],
            "properties": {
                "project_id": {"type": "string", "minLength": 1},
                "include_archived": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "cursor": {"type": ["string", "null"], "default": None},
                "include_preview": {"type": "boolean", "default": False},
                "title_max_chars": {"type": "integer", "minimum": 20, "maximum": 2000, "default": 160},
                "preview_max_chars": {"type": "integer", "minimum": 20, "maximum": 4000, "default": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_list_active_chats",
        "description": "List chats that appear active from live/cache/transcript evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": ["string", "null"], "default": None},
                "include_waiting_for_user": {"type": "boolean", "default": True},
                "include_waiting_for_approval": {"type": "boolean", "default": True},
                "include_running": {"type": "boolean", "default": True},
                "active_window_minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 120},
                "include_evidence": {"type": "boolean", "default": False},
                "title_max_chars": {"type": "integer", "minimum": 20, "maximum": 2000, "default": 160},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_search_chats",
        "description": "Search Codex chats across projects by keywords, multiple terms, or exact phrases using the MCP-owned FTS index over hook history, transcripts, and legacy KB history. Returns ranked chat matches, not full transcripts, and never calls DeepSeek.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "match_mode": {"type": "string", "enum": ["auto", "all_terms", "any_term", "phrase"], "default": "auto"},
                "project_id": {"type": ["string", "null"], "default": None},
                "include_archived": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "cursor": {"type": ["string", "null"], "default": None},
                "include_snippets": {"type": "boolean", "default": True},
                "snippets_per_chat": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
                "snippet_max_chars": {"type": "integer", "minimum": 80, "maximum": 1000, "default": 240},
                "refresh_index": {"type": "boolean", "default": True},
                "index_time_budget_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 8},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_chat_status",
        "description": "Get lightweight Codex chat status and short previews without DeepSeek and without full transcript content.",
        "inputSchema": {
            "type": "object",
            "required": ["chat_id"],
            "properties": {
                "chat_id": {"type": "string", "minLength": 1},
                "project_id": {"type": ["string", "null"], "default": None},
                "preview_max_chars": {"type": "integer", "minimum": 20, "maximum": 4000, "default": 300},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_chat",
        "description": "Get Codex chat content from MCP hook history, Codex transcripts, or legacy KB history fallback. The history before the latest user/orchestrator message is summarized with DeepSeek, and only the latest user/orchestrator message plus following raw Codex/tool/event messages are returned.",
        "inputSchema": {
            "type": "object",
            "required": ["chat_id"],
            "properties": {
                "chat_id": {"type": "string", "minLength": 1},
                "project_id": {"type": ["string", "null"], "default": None},
                "range": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["all", "last_messages", "last_turns", "time_range", "line_range", "token_budget"],
                            "default": "last_messages",
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50},
                        "from": {"type": ["string", "null"], "default": None},
                        "to": {"type": ["string", "null"], "default": None},
                        "token_budget": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 12000},
                    },
                    "additionalProperties": False,
                },
                "include_tool_calls": {"type": "boolean", "default": False},
                "include_tool_outputs": {"type": "boolean", "default": False},
                "include_command_outputs": {"type": "boolean", "default": False},
                "include_reasoning": {"type": "boolean", "default": False},
                "include_metadata": {"type": "boolean", "default": True},
                "include_items": {"type": "boolean", "default": False},
                "tail_max_messages": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 80},
                "tail_max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 30000},
                "force_refresh_summary": {"type": "boolean", "default": False},
                "response_budget_chars": {"type": "integer", "minimum": 2000, "maximum": 300000, "default": 50000},
                "format": {"type": "string", "enum": ["structured", "markdown", "compact"], "default": "structured"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_send_message",
        "description": "Compatibility layer low-level write tool: send a new user message into an existing Codex persisted chat via app-server thread/resume + turn/start. Returns after turn/start is accepted; poll codex_get_turn_status for messages and completion. For durable long-running tasks and client-timeout resilience, prefer codex_submit_task(operation_type='send_message').",
        "inputSchema": {
            "type": "object",
            "required": ["chat_id", "message"],
            "properties": {
                "chat_id": {"type": "string", "minLength": 1},
                "project_id": {"type": ["string", "null"], "default": None},
                "message": {"type": "string", "minLength": 1, "maxLength": 200000},
                "mode": {"type": "string", "enum": ["normal", "command", "append_context"], "default": "normal"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200, "default": DEFAULT_TOOL_START_TIMEOUT_SECONDS},
                "first_message_timeout_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 600,
                    "default": DEFAULT_FIRST_MESSAGE_TIMEOUT_SECONDS,
                    "description": "Deprecated and ignored. This tool now returns immediately after turn/start; use codex_get_turn_status for messages.",
                },
                "first_message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
                "approval_policy": {
                    "type": "string",
                    "enum": ["never", "on-request", "on-failure", "untrusted", "respect_existing", "never_auto_approve", "ask_openclaw"],
                    "default": "on-request",
                },
                "collaboration_mode": {"type": ["string", "null"], "enum": ["default", "plan", None], "default": None},
                "sandbox": {
                    "type": "string",
                    "enum": ["danger-full-access", "workspace-write", "read-only", "respect_existing"],
                    "default": "read-only",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_start_chat",
        "description": "Compatibility layer low-level write tool: start a new Codex chat in a known project via app-server thread/start + turn/start. Returns after turn/start is accepted; poll codex_get_turn_status for messages and completion. For durable long-running tasks and client-timeout resilience, prefer codex_submit_task(operation_type='start_chat').",
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "message"],
            "properties": {
                "project_id": {"type": "string", "minLength": 1},
                "message": {"type": "string", "minLength": 1, "maxLength": 200000},
                "title": {"type": ["string", "null"], "default": None},
                "cwd": {"type": ["string", "null"], "default": None},
                "model": {"type": ["string", "null"], "default": None},
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", None], "default": "read-only"},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", None], "default": "on-request"},
                "collaboration_mode": {"type": ["string", "null"], "enum": ["default", "plan", None], "default": None},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200, "default": DEFAULT_TOOL_START_TIMEOUT_SECONDS},
                "first_message_timeout_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 600,
                    "default": DEFAULT_FIRST_MESSAGE_TIMEOUT_SECONDS,
                    "description": "Deprecated and ignored. This tool now returns immediately after turn/start; use codex_get_turn_status for messages.",
                },
                "first_message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_start_plan_workflow",
        "description": "Start a durable long-running orchestrator -> Codex workflow by launching a new Codex chat in real Plan Mode. Returns workflowId, threadId, and planTurnId for polling.",
        "inputSchema": {
            "type": "object",
            "required": ["project_id", "message"],
            "properties": {
                "project_id": {"type": "string", "minLength": 1},
                "message": {"type": "string", "minLength": 1, "maxLength": 200000},
                "title": {"type": ["string", "null"], "default": None},
                "cwd": {"type": ["string", "null"], "default": None},
                "model": {"type": ["string", "null"], "default": None},
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", None], "default": "read-only"},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", None], "default": "on-request"},
                "client_request_id": {"type": ["string", "null"], "default": None},
                "goal": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Optional explicit Codex thread goal objective mirrored through app-server thread/goal/set after the workflow thread exists.",
                },
                "goal_token_budget": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 10000000,
                    "default": None,
                },
                "goal_completion_action": {
                    "type": ["string", "null"],
                    "enum": ["clear", "set_complete", "leave", None],
                    "default": "clear",
                    "description": "What MCP should do with its managed Codex thread goal after workflow completion.",
                },
                "goal_completion_objective": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Optional objective used when goal_completion_action is set_complete.",
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200, "default": DEFAULT_TOOL_START_TIMEOUT_SECONDS},
                "first_message_timeout_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 600,
                    "default": DEFAULT_FIRST_MESSAGE_TIMEOUT_SECONDS,
                    "description": "Deprecated and ignored. Workflow start returns after turn/start; poll codex_get_workflow_status for plan readiness.",
                },
                "first_message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_start_review_workflow",
        "description": "Start a durable Codex code review workflow through app-server review/start. Returns a fast workflow ack; poll codex_get_workflow_status for review progress and final report.",
        "inputSchema": {
            "type": "object",
            "required": ["target_type"],
            "properties": {
                "thread_id": {"type": ["string", "null"], "default": None},
                "project_id": {"type": ["string", "null"], "default": None},
                "cwd": {"type": ["string", "null"], "default": None},
                "target_type": {
                    "type": "string",
                    "enum": ["uncommitted_changes", "base_branch", "commit", "custom"],
                },
                "base_branch": {"type": ["string", "null"], "default": None},
                "commit_sha": {"type": ["string", "null"], "default": None},
                "commit_title": {"type": ["string", "null"], "default": None},
                "instructions": {"type": ["string", "null"], "default": None, "maxLength": 200000},
                "delivery": {"type": ["string", "null"], "enum": ["inline", "detached", None], "default": None},
                "client_request_id": {"type": ["string", "null"], "default": None},
                "model": {"type": ["string", "null"], "default": None},
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", None], "default": "read-only"},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", None], "default": "on-request"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200, "default": DEFAULT_TOOL_START_TIMEOUT_SECONDS},
                "first_message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_workflow_status",
        "description": "Poll a durable long-running orchestrator -> Codex workflow. Aggregates plan turn, execution turn, latest plan, pending interactions, and recommended next action.",
        "inputSchema": {
            "type": "object",
            "required": ["workflow_id"],
            "properties": {
                "workflow_id": {"type": "string", "minLength": 1},
                "last_messages": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
                "include_events": {"type": "boolean", "default": True},
                "refresh_live": {
                    "type": "boolean",
                    "default": False,
                    "description": "Reserved explicit live refresh flag. Default polling is passive.",
                },
                "refresh_live_goal": {
                    "type": "boolean",
                    "default": False,
                    "description": "Best-effort live thread/goal sync. Defaults to false so frequent workflow polling stays passive and cannot create app-server requests.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_adopt_workflow_plan",
        "description": "Adopt a newer valid Plan Mode candidate from the same Codex thread into an existing durable workflow. This is idempotent by workflow and plan hash and never starts execution by itself.",
        "inputSchema": {
            "type": "object",
            "required": ["workflow_id", "candidate_turn_id", "candidate_plan_hash"],
            "properties": {
                "workflow_id": {"type": "string", "minLength": 1},
                "candidate_turn_id": {"type": "string", "minLength": 1},
                "candidate_plan_hash": {"type": "string", "minLength": 1},
                "client_request_id": {"type": ["string", "null"], "default": None},
                "adoption_note": {"type": ["string", "null"], "default": None, "maxLength": 4000},
                "message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_approve_plan",
        "description": "Approve the latest completed Plan Mode plan for a durable workflow and queue the execution turn. Repeated calls are idempotent and do not create duplicate execution turns.",
        "inputSchema": {
            "type": "object",
            "required": ["workflow_id"],
            "properties": {
                "workflow_id": {"type": "string", "minLength": 1},
                "client_request_id": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Stable retry idempotency key for workflow approval/execution.",
                },
                "message": {"type": ["string", "null"], "default": "Implement the plan."},
                "output_schema": {
                    "type": ["object", "null"],
                    "default": None,
                    "additionalProperties": True,
                    "description": "Optional JSON Schema passed to app-server outputSchema for the execution turn final assistant message.",
                },
                "approval_policy": {
                    "type": "string",
                    "enum": ["never", "on-request", "on-failure", "untrusted", "respect_existing", "never_auto_approve", "ask_openclaw"],
                    "default": "on-request",
                },
                "sandbox": {
                    "type": "string",
                    "enum": ["danger-full-access", "workspace-write", "read-only", "respect_existing"],
                    "default": "read-only",
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200, "default": DEFAULT_TOOL_START_TIMEOUT_SECONDS},
                "first_message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_preflight_project_run",
        "description": "Run read-only preflight checks before a long Codex workflow. Optionally starts a tiny live probe when live_probe=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": ["string", "null"], "default": None},
                "cwd": {"type": ["string", "null"], "default": None},
                "model": {"type": ["string", "null"], "default": None},
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", None], "default": None},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", None], "default": None},
                "workflow_kind": {"type": ["string", "null"], "enum": ["plan", "write", "review", None], "default": "plan"},
                "live_probe": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_turn_status",
        "description": "Get tracked Codex turn status and the latest assistant messages. Uses live app-server tracking with MCP hook history and legacy KB history fallback.",
        "inputSchema": {
            "type": "object",
            "required": ["turn_id"],
            "properties": {
                "turn_id": {"type": "string", "minLength": 1},
                "thread_id": {"type": ["string", "null"], "default": None},
                "last_messages": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
                "progress_events": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10},
                "progress_max_chars": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 2000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_execute_plan",
        "description": "Compatibility layer low-level workflow write tool: submit the latest approved Plan Mode plan for implementation by sending 'Implement the plan.' in default collaboration mode. For durable long-running tasks and client-timeout resilience, prefer codex_submit_task(operation_type='execute_plan').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": ["string", "null"], "default": None},
                "chat_id": {"type": ["string", "null"], "default": None},
                "project_id": {"type": ["string", "null"], "default": None},
                "client_request_id": {"type": ["string", "null"], "default": None},
                "message": {"type": ["string", "null"], "default": "Implement the plan."},
                "output_schema": {
                    "type": ["object", "null"],
                    "default": None,
                    "additionalProperties": True,
                    "description": "Optional JSON Schema passed to app-server outputSchema for the execution turn final assistant message.",
                },
                "force": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200, "default": DEFAULT_TOOL_START_TIMEOUT_SECONDS},
                "first_message_timeout_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 600,
                    "default": DEFAULT_FIRST_MESSAGE_TIMEOUT_SECONDS,
                    "description": "Deprecated and ignored. Plan execution returns after turn/start; poll codex_get_workflow_status or codex_get_turn_status.",
                },
                "first_message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
                "approval_policy": {
                    "type": "string",
                    "enum": ["never", "on-request", "on-failure", "untrusted", "respect_existing", "never_auto_approve", "ask_openclaw"],
                    "default": "on-request",
                },
                "sandbox": {
                    "type": "string",
                    "enum": ["danger-full-access", "workspace-write", "read-only", "respect_existing"],
                    "default": "read-only",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_submit_task",
        "description": "Durably queue a Codex write operation and return immediately with operationId. Use codex_get_operation_status to poll threadId, turnId, messages, pending interactions, and completion.",
        "inputSchema": {
            "type": "object",
            "required": ["operation_type"],
            "properties": {
                "operation_type": {"type": "string", "enum": ["start_chat", "send_message", "execute_plan", "steer_turn", "fork_thread"]},
                "client_request_id": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Stable retry idempotency key. If omitted, MCP creates a new operation and relies on prompt deduplication to prevent active duplicate turns.",
                },
                "agent_id": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Optional orchestrator/agent id used by the central worker scheduler for per-agent limits.",
                },
                "resource_keys": {
                    "type": ["array", "null"],
                    "default": None,
                    "description": "Optional write-scope keys. Disjoint keys allow parallel workspace-write/danger-full-access turns in the same project.",
                    "items": {"type": "string", "minLength": 1, "maxLength": 300},
                    "maxItems": 50,
                },
                "priority": {"type": "string", "enum": ["low", "normal", "high"], "default": "normal"},
                "estimated_cost_class": {"type": "string", "enum": ["light", "normal", "heavy"], "default": "normal"},
                "project_id": {"type": ["string", "null"], "default": None},
                "chat_id": {"type": ["string", "null"], "default": None},
                "thread_id": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Required for operation_type='steer_turn'. Target thread that owns the active turn.",
                },
                "source_thread_id": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Required for operation_type='fork_thread'. Source thread to fork from.",
                },
                "expected_turn_id": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Required for operation_type='steer_turn'. Active turn id precondition passed to Codex app-server.",
                },
                "workflow_id": {"type": ["string", "null"], "default": None},
                "message": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "maxLength": 200000,
                    "default": None,
                    "description": "Required for all operation types except fork_thread. For fork_thread, omit it for fork-only or provide it to start the first turn in the forked thread.",
                },
                "input_items": {
                    "type": ["array", "null"],
                    "default": None,
                    "maxItems": 10,
                    "description": "Optional image inputs appended to the text message for operation types that start a new turn. Supports image URL and localImage file path items only.",
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "required": ["type", "url"],
                                "properties": {
                                    "type": {"type": "string", "enum": ["image"]},
                                    "url": {"type": "string", "minLength": 1, "maxLength": IMAGE_INPUT_URL_MAX_CHARS},
                                    "detail": {"type": ["string", "null"], "enum": ["auto", "low", "high", "original", None], "default": "auto"},
                                },
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "required": ["type", "path"],
                                "properties": {
                                    "type": {"type": "string", "enum": ["localImage"]},
                                    "path": {"type": "string", "minLength": 1},
                                    "detail": {"type": ["string", "null"], "enum": ["auto", "low", "high", "original", None], "default": "auto"},
                                },
                                "additionalProperties": False,
                            },
                        ]
                    },
                },
                "title": {"type": ["string", "null"], "default": None},
                "cwd": {"type": ["string", "null"], "default": None},
                "model": {"type": ["string", "null"], "default": None},
                "fork_config": {"type": ["object", "null"], "default": None, "additionalProperties": True},
                "ephemeral": {"type": "boolean", "default": False},
                "output_schema": {
                    "type": ["object", "null"],
                    "default": None,
                    "additionalProperties": True,
                    "description": "Optional JSON Schema passed to app-server outputSchema for this turn final assistant message.",
                },
                "collaboration_mode": {"type": ["string", "null"], "enum": ["default", "plan", None], "default": None},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", "respect_existing", "never_auto_approve", None], "default": "on-request"},
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", "respect_existing", None], "default": "read-only"},
                "force": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200, "default": DEFAULT_TOOL_START_TIMEOUT_SECONDS},
                "first_message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_operation_status",
        "description": "Poll a durable Codex operation created by codex_submit_task. Returns current operation phase, thread/turn ids, latest messages, pending interactions, and recommended next action.",
        "inputSchema": {
            "type": "object",
            "required": ["operation_id"],
            "properties": {
                "operation_id": {"type": "string", "minLength": 1},
                "last_messages": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
                "progress_events": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10},
                "progress_max_chars": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 2000},
                "include_events": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_list_pending_interactions",
        "description": "List pending Codex app-server approval/input/elicitation requests waiting for the orchestrator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": ["string", "null"], "default": None},
                "turn_id": {"type": ["string", "null"], "default": None},
                "operation_id": {"type": ["string", "null"], "default": None},
                "workflow_id": {"type": ["string", "null"], "default": None},
                "status": {
                    "type": ["string", "null"],
                    "enum": ["pending", "answered", "auto_declined", "expired", "failed", "orphaned_after_app_server_exit", None],
                    "default": "pending",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_answer_pending_interaction",
        "description": "Answer a pending Codex app-server approval/input/elicitation request so the active turn can continue.",
        "inputSchema": {
            "type": "object",
            "required": ["interaction_id"],
            "properties": {
                "interaction_id": {"type": "string", "minLength": 1},
                "decision": {"type": ["string", "null"], "enum": ["accept", "acceptForSession", "decline", "cancel", None], "default": None},
                "decision_payload": {"type": ["object", "null"], "default": None},
                "answers": {"type": ["object", "null"], "default": None},
                "action": {"type": ["string", "null"], "enum": ["accept", "decline", "cancel", None], "default": None},
                "content": {"type": ["object", "null"], "default": None},
                "permissions": {"type": ["object", "null"], "default": None},
                "scope": {"type": ["string", "null"], "enum": ["turn", "session", None], "default": "turn"},
                "strict_auto_review": {"type": ["boolean", "null"], "default": None},
                "raw_response": {"type": ["object", "null"], "default": None},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_interrupt_turn",
        "description": "Interrupt a running Codex turn through the MCP-owned Codex app-server subprocess. Accepts direct thread/turn ids or durable operation/workflow context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": ["string", "null"], "default": None},
                "turn_id": {"type": ["string", "null"], "default": None},
                "operation_id": {"type": ["string", "null"], "default": None},
                "workflow_id": {"type": ["string", "null"], "default": None},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_archive_thread",
        "description": "Archive a known Codex thread through codex-app-server thread/archive. Refuses to run while the thread has active work.",
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string", "minLength": 1},
                "project_id": {"type": ["string", "null"], "default": None},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
                "refresh_catalog": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_unarchive_thread",
        "description": "Unarchive a known Codex thread through codex-app-server thread/unarchive. Refuses to run while the thread has active work.",
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string", "minLength": 1},
                "project_id": {"type": ["string", "null"], "default": None},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
                "refresh_catalog": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_start_thread_compaction",
        "description": "Start Codex context compaction for a known thread through codex-app-server thread/compact/start. Poll codex_get_thread_compaction_status with the returned actionId.",
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string", "minLength": 1},
                "project_id": {"type": ["string", "null"], "default": None},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_thread_compaction_status",
        "description": "Poll a thread compaction action created by codex_start_thread_compaction.",
        "inputSchema": {
            "type": "object",
            "required": ["action_id"],
            "properties": {
                "action_id": {"type": "string", "minLength": 1},
                "include_events": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_worker_status",
        "description": "Read central MCP worker heartbeats and execution mode state without starting codex-app-server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_recent_commands": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_queue_status",
        "description": "Read durable operation queue state, queued reasons, and assigned worker ids.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": ["string", "null"],
                    "enum": ["queued", "scheduled", "running", "completed", "failed", "blocked", None],
                    "default": None,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_concurrency_status",
        "description": "Read active turn counts and resource locks used by the central MCP worker scheduler.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_locks": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_worker_command_status",
        "description": "Poll a control command delegated to the central MCP worker from client mode.",
        "inputSchema": {
            "type": "object",
            "required": ["command_id"],
            "properties": {
                "command_id": {"type": "string", "minLength": 1},
                "include_result": {"type": "boolean", "default": True},
                "max_result_chars": {"type": "integer", "minimum": 0, "maximum": 200000, "default": 12000},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_restart_app_server",
        "description": "Restart only the MCP-owned Codex app-server subprocess. This does not restart Codex Desktop and refuses to run while the MCP app-server has active turns, captures, or pending requests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_after_restart": {"type": "boolean", "default": True},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
                "force": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_app_server_status",
        "description": "Get status of the MCP-owned Codex app-server subprocess without starting it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_recent_events": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_runtime_capabilities",
        "description": "Read a compact cached inventory of local codex-app-server runtime capabilities: models, permission profiles, sandbox readiness, hooks, skills, provider features, redacted account status, and supported schema methods.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "default": False},
                "cwd": {"type": ["string", "null"], "default": None},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30, "default": 2},
                "include_models": {"type": "boolean", "default": True},
                "include_hooks": {"type": "boolean", "default": True},
                "include_skills": {"type": "boolean", "default": True},
                "include_account": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_health_summary",
        "description": "Get a compact read-only MCP/Codex health summary for orchestrators without starting app-server or returning large logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation_id": {"type": ["string", "null"], "default": None},
                "workflow_id": {"type": ["string", "null"], "default": None},
                "thread_id": {"type": ["string", "null"], "default": None},
                "turn_id": {"type": ["string", "null"], "default": None},
                "since_minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "default": 120},
                "stale_after_minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "default": 30},
                "include_recent_errors": {"type": "boolean", "default": True},
                "max_recent_errors": {"type": "integer", "minimum": 0, "maximum": 50, "default": 5},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_collect_diagnostics",
        "description": "Collect a read-only MCP/Codex diagnostic snapshot: paths, app-server state, active work, pending interactions, workflows, logs/events pointers, and health checks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation_id": {"type": ["string", "null"], "default": None},
                "workflow_id": {"type": ["string", "null"], "default": None},
                "thread_id": {"type": ["string", "null"], "default": None},
                "turn_id": {"type": ["string", "null"], "default": None},
                "since_minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "default": 120},
                "include_logs": {"type": "boolean", "default": False},
                "log_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "event_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "include_timeline": {"type": "boolean", "default": True},
                "timeline_limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "refresh_catalog": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_get_diagnostic_logs",
        "description": "Read redacted MCP diagnostic log lines and MCP-owned app-server event audit entries with thread/turn/workflow filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["all", "mcp_log", "app_server_events"], "default": "all"},
                "workflow_id": {"type": ["string", "null"], "default": None},
                "thread_id": {"type": ["string", "null"], "default": None},
                "turn_id": {"type": ["string", "null"], "default": None},
                "process_generation": {"type": ["integer", "null"], "default": None},
                "since_minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "default": 120},
                "severity": {"type": ["string", "null"], "enum": ["debug", "info", "warning", "error", "critical", None], "default": None},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                "max_line_chars": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 4000},
                "include_payload": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_analyze_issue",
        "description": "Analyze MCP/Codex diagnostics and logs to classify likely root cause and recommend safe repair actions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem_text": {"type": ["string", "null"], "default": None},
                "operation_id": {"type": ["string", "null"], "default": None},
                "workflow_id": {"type": ["string", "null"], "default": None},
                "thread_id": {"type": ["string", "null"], "default": None},
                "turn_id": {"type": ["string", "null"], "default": None},
                "since_minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "default": 120},
                "include_evidence": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_repair_issue",
        "description": "Run an allowlisted MCP/Codex repair action with before/after audit. Unsafe actions require force=true.",
        "inputSchema": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "recover_stale_operations",
                        "refresh_catalog_and_history",
                        "reconcile_workflow_from_thread",
                        "retry_workflow_with_runtime_policy",
                        "refresh_catalog_and_kb",
                        "mark_orphaned_after_exit",
                        "restart_app_server_idle",
                        "force_restart_app_server",
                        "mark_stale_turns_orphaned",
                        "expire_stale_pending_interactions",
                        "refresh_catalog",
                        "rebuild_search_index",
                        "validate_paths_and_config",
                        "interrupt_turn",
                        "cleanup_prompt_submissions"
                    ],
                },
                "diagnosis_id": {"type": ["string", "null"], "default": None},
                "thread_id": {"type": ["string", "null"], "default": None},
                "turn_id": {"type": ["string", "null"], "default": None},
                "operation_id": {"type": ["string", "null"], "default": None},
                "workflow_id": {"type": ["string", "null"], "default": None},
                "client_request_id": {"type": ["string", "null"], "default": None},
                "reason": {"type": ["string", "null"], "default": None, "maxLength": 4000},
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", None], "default": None},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", None], "default": None},
                "dry_run": {"type": "boolean", "default": True},
                "force": {"type": "boolean", "default": False},
                "stale_after_minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "default": 30},
                "older_than_days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 30},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
            },
            "additionalProperties": False,
        },
    },
]


for _tool in TOOLS:
    with_output_schema(_tool)
    _tool.setdefault(
        "annotations",
        {
            "openclawContractGroup": "stable"
            if _tool.get("name") in STABLE_OPENCLAW_TOOLS
            else "compatibility"
        },
    )


class ToolService:
    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or ServerConfig.load(Path(__file__).resolve().parents[1])
        LOG.info(
            "tool service init state_db=%s sessions=%s archived=%s codex_state=%s projects_root=%s",
            self.config.state_db_path,
            self.config.sessions_dir,
            self.config.archived_sessions_dir,
            self.config.codex_state_db,
            self.config.projects_root,
        )
        self.storage = McpStorage(self.config.state_db_path)
        self.storage.connect()
        self._worker_owner = f"mcp:{os.getpid()}:{uuid.uuid4().hex}"
        self._config_fingerprint = _config_fingerprint(self.config)
        self._config_summary_json = json.dumps(_config_fingerprint_summary(self.config), ensure_ascii=False, sort_keys=True)
        self._allow_cross_config_recovery = os.environ.get("CODEX_MCP_ALLOW_CROSS_CONFIG_RECOVERY") == "1"
        if self.config.execution_mode in {"inline", "worker"}:
            self._startup_recovery = self.storage.recover_startup_operations(now=_now_iso())
        else:
            self._startup_recovery = {
                "skipped": True,
                "executionMode": self.config.execution_mode,
                "reason": "This MCP process is not allowed to execute durable operations.",
            }
        if self._startup_recovery.get("resetOperationIds") or self._startup_recovery.get("runningOperationIds"):
            LOG.info("operation startup recovery owner=%s result=%s", self._worker_owner, self._startup_recovery)
        self.catalog = ProjectChatCatalog(self.config, self.storage)
        self._app_server: CodexAppServerClient | None = None
        self._operation_tasks: dict[str, asyncio.Task[None]] = {}
        self._runtime_capabilities_cache: dict[str, Any] | None = None
        self._runtime_capabilities_cache_key: str | None = None
        self._runtime_capabilities_cache_at: float | None = None

    def _can_schedule_inline(self) -> bool:
        return self.config.execution_mode == "inline"

    def _can_execute_operations(self) -> bool:
        return self.config.execution_mode in {"inline", "worker"}

    def _delegates_control_to_worker(self) -> bool:
        return self.config.execution_mode == "client"

    def _should_delegate_compatibility_write(self, name: str, args: dict[str, Any]) -> bool:
        if name not in CLIENT_MODE_COMPATIBILITY_WRITE_TOOLS:
            return False
        if not self._delegates_control_to_worker():
            return False
        if bool(args.get("_worker_internal_call")):
            return False
        if _optional_string(args.get("_operation_id")) and bool(args.get("_skip_prompt_dedup")):
            return False
        return True

    def _delegated_compatibility_write_payload(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        payload = dict(args)
        if name == "codex_start_chat":
            payload["operation_type"] = "start_chat"
        elif name == "codex_send_message":
            payload["operation_type"] = "send_message"
        elif name == "codex_execute_plan":
            if _optional_string(payload.get("workflow_id")) and not bool(payload.get("force", False)):
                result = self.codex_approve_plan(payload)
                result["compatibilityDelegated"] = True
                result["operationSource"] = "compatibility_delegated_to_durable_queue"
                result.setdefault(
                    "compatibilityWarning",
                    "This compatibility workflow write was delegated to the durable approval path because this MCP process runs in client mode.",
                )
                return result
            payload["operation_type"] = "execute_plan"
            payload.setdefault("message", "Implement the plan.")
        else:
            raise invalid_argument(f"Unsupported compatibility write tool: {name}")
        result = self.codex_submit_task(payload)
        result["compatibilityDelegated"] = True
        result["operationSource"] = "compatibility_delegated_to_durable_queue"
        result.setdefault(
            "compatibilityWarning",
            "This compatibility write tool was delegated to the durable worker queue because this MCP process runs in client mode.",
        )
        return result

    def _enqueue_worker_command(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        command_id = "cmd_" + uuid.uuid4().hex
        now = _now_iso()
        request = {"toolName": tool_name, "arguments": args}
        self.storage.create_worker_command(
            command_id=command_id,
            command_type=tool_name,
            status="queued",
            request=request,
            created_at=now,
            updated_at=now,
        )
        payload = {
            "ok": True,
            "commandId": command_id,
            "commandType": tool_name,
            "status": "queued",
            "executionMode": self.config.execution_mode,
            "nextRecommendedAction": "poll_worker_command",
            "recommendedPollAfterSeconds": 2,
            "pollRecommended": True,
            "agentGuidance": {
                "schemaVersion": "agent-guidance/v1",
                "problemState": "wait",
                "summary": "Control action was delegated to the central MCP worker.",
                "instructions": [
                    {
                        "kind": "poll",
                        "toolName": "codex_get_worker_command_status",
                        "arguments": {"command_id": command_id},
                        "dryRunFirst": False,
                        "stopIf": "status is completed or failed",
                        "continueIf": "status is queued or running",
                    }
                ],
                "loopGuard": {
                    "guardKey": f"worker-command:{command_id}",
                    "attemptCount": 0,
                    "maxAttempts": 1,
                    "allowed": True,
                    "blockedReason": None,
                    "escalationAction": None,
                },
                "evidenceRefs": [{"type": "workerCommand", "id": command_id}],
            },
            "agentGuidanceText": (
                "Команда передана центральному MCP worker. "
                "Не выполняй это действие повторно напрямую. "
                "Опроси codex_get_worker_command_status по commandId и продолжай только после completed."
            ),
        }
        if tool_name == "codex_get_runtime_capabilities":
            payload["refreshCommandId"] = command_id
            payload["runtimeCapabilities"] = {
                "status": "refresh_queued",
                "cacheSource": "worker_command",
                "workerRuntimeSnapshot": self._worker_runtime_snapshot_for_client(),
            }
            payload["cacheState"] = {"hit": False, "source": "worker_command", "refreshQueued": True}
        return payload

    async def close(self) -> None:
        for task in list(self._operation_tasks.values()):
            task.cancel()
        for task in list(self._operation_tasks.values()):
            with suppress(BaseException):
                await task
        self._operation_tasks.clear()
        if self._app_server is not None:
            await self._app_server.stop()
        self.storage.close()

    def _attach_agent_guidance(self, payload: dict[str, Any], *, surface: str) -> dict[str, Any]:
        guidance = build_guidance_for_payload(
            payload,
            surface=surface,
            attempt_lookup=self.storage.get_agent_guidance_attempt,
        )
        if guidance is None:
            return payload
        payload["agentGuidance"] = guidance
        payload["agentGuidanceText"] = guidance_text(guidance)
        payload["recoveryAttemptState"] = guidance.get("loopGuard")
        return payload

    def _attach_error_guidance(self, payload: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else None
        if error is None:
            return payload
        guidance = build_guidance_for_error(
            error,
            tool_name=tool_name,
            attempt_lookup=self.storage.get_agent_guidance_attempt,
        )
        if guidance is None:
            return payload
        payload["agentGuidance"] = guidance
        payload["agentGuidanceText"] = guidance_text(guidance)
        payload["recoveryAttemptState"] = guidance.get("loopGuard")
        return payload

    def _record_guidance_attempt(
        self,
        *,
        action: str,
        args: dict[str, Any],
        result: dict[str, Any],
        status: str,
        count_attempt: bool,
        force: bool = False,
    ) -> dict[str, Any]:
        scope_type, scope_id = attempt_scope_from_args(action, args)
        guard_key = guard_key_for(
            category=f"action:{action}",
            scope_type=scope_type,
            scope_id=scope_id,
            action=action,
            target={},
        )
        now = _now_iso()
        current = self.storage.get_agent_guidance_attempt(guard_key)
        next_attempt_count = int((current or {}).get("attempt_count") or 0) + (1 if count_attempt else 0)
        cooldown_until = (
            cooldown_after_attempt(action, next_attempt_count, now=now, failed_forced=force and status == "failed")
            if count_attempt
            else (current or {}).get("cooldown_until")
        )
        row = self.storage.record_agent_guidance_attempt(
            guard_key=guard_key,
            scope_type=scope_type,
            scope_id=scope_id,
            action=action,
            status=status,
            created_at=now,
            cooldown_until=cooldown_until,
            result=redact_payload(result),
            count_attempt=count_attempt,
        )
        return loop_guard_state(
            guard_key=guard_key,
            scope_type=scope_type,
            scope_id=scope_id,
            action=action,
            attempt_row=row,
            now=now,
        )

    async def call(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments or {}
        started = time.monotonic()
        LOG.info("call start name=%s argument_keys=%s", name, sorted(args.keys()))
        if self._can_schedule_inline():
            self._schedule_recoverable_operations()
        try:
            if self._should_delegate_compatibility_write(name, args):
                result = self._delegated_compatibility_write_payload(name, args)
                LOG.info("call delegated compatibility write name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if (
                self._delegates_control_to_worker()
                and name == "codex_get_runtime_capabilities"
                and bool(args.get("refresh", False))
            ):
                result = self._enqueue_worker_command(name, args)
                LOG.info("call delegated name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if (
                self._delegates_control_to_worker()
                and name in WORKER_COMMAND_TOOLS
                and name != "codex_get_runtime_capabilities"
            ):
                result = self._enqueue_worker_command(name, args)
                LOG.info("call delegated name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_list_projects":
                result = self.codex_list_projects(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_list_project_chats":
                result = self.codex_list_project_chats(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_list_active_chats":
                result = self.codex_list_active_chats(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_search_chats":
                result = self.codex_search_chats(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_chat_status":
                result = self.codex_get_chat_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_chat":
                result = self.codex_get_chat(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_send_message":
                result = await self.codex_send_message(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_start_chat":
                result = await self.codex_start_chat(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_start_plan_workflow":
                result = await self.codex_start_plan_workflow(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_start_review_workflow":
                result = self.codex_start_review_workflow(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_workflow_status":
                result = await self.codex_get_workflow_status_async(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_adopt_workflow_plan":
                result = self.codex_adopt_workflow_plan(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_approve_plan":
                result = self.codex_approve_plan(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_preflight_project_run":
                result = await self.codex_preflight_project_run(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_turn_status":
                result = self.codex_get_turn_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_execute_plan":
                result = await self.codex_execute_plan(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_submit_task":
                result = self.codex_submit_task(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_operation_status":
                result = self.codex_get_operation_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_list_pending_interactions":
                result = self.codex_list_pending_interactions(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_answer_pending_interaction":
                result = await self.codex_answer_pending_interaction(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_interrupt_turn":
                result = await self.codex_interrupt_turn(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_archive_thread":
                result = await self.codex_archive_thread(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_unarchive_thread":
                result = await self.codex_unarchive_thread(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_start_thread_compaction":
                result = await self.codex_start_thread_compaction(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_thread_compaction_status":
                result = self.codex_get_thread_compaction_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_worker_status":
                result = self.codex_get_worker_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_queue_status":
                result = self.codex_get_queue_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_concurrency_status":
                result = self.codex_get_concurrency_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_worker_command_status":
                result = self.codex_get_worker_command_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_restart_app_server":
                result = await self.codex_restart_app_server(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_app_server_status":
                result = self.codex_get_app_server_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_runtime_capabilities":
                result = await self.codex_get_runtime_capabilities(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_health_summary":
                result = self.codex_health_summary(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_collect_diagnostics":
                result = self.codex_collect_diagnostics(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_diagnostic_logs":
                result = self.codex_get_diagnostic_logs(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_analyze_issue":
                result = self.codex_analyze_issue(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_repair_issue":
                result = await self.codex_repair_issue(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            raise invalid_argument(f"Unknown tool: {name}")
        except CodexMcpError as exc:
            LOG.warning("call codex error name=%s code=%s retryable=%s", name, exc.code, exc.retryable)
            return self._attach_error_guidance(exc.to_dict(), tool_name=name)
        except RuntimeError as exc:
            LOG.exception("call runtime error name=%s", name)
            return self._attach_error_guidance(send_failed(str(exc)).to_dict(), tool_name=name)
        except Exception as exc:
            LOG.exception("call unexpected error name=%s", name)
            return self._attach_error_guidance(send_failed(str(exc)).to_dict(), tool_name=name)

    async def _app(self) -> CodexAppServerClient:
        if self._app_server is None:
            self._app_server = CodexAppServerClient(self.config, self.storage)
        await self._app_server.start()
        return self._app_server


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _bounded_optional_text(value: Any, *, field_name: str, max_chars: int) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    if len(text) > max_chars:
        raise invalid_argument(f"{field_name} is too long.", field=field_name, maxChars=max_chars)
    return text


def _optional_bounded_int(value: Any, min_value: int, max_value: int, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise invalid_argument(f"{field_name} must be an integer.", field=field_name) from exc
    if parsed < min_value or parsed > max_value:
        raise invalid_argument(f"{field_name} must be between {min_value} and {max_value}.", field=field_name, min=min_value, max=max_value)
    return parsed


def _safe_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:32]


def _image_input_detail(value: Any, *, index: int) -> str:
    detail = _optional_string(value) or "auto"
    if detail not in SUPPORTED_IMAGE_INPUT_DETAILS:
        raise invalid_argument("Unsupported image detail value.", index=index, detail=detail)
    return detail


def _normalize_remote_image_input(
    item: dict[str, Any],
    *,
    detail: str,
    index: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    url = _optional_string(item.get("url"))
    if not url:
        raise invalid_argument("image input item requires url.", index=index)
    if len(url) > IMAGE_INPUT_URL_MAX_CHARS:
        raise invalid_argument("image url is too long.", index=index, maxChars=IMAGE_INPUT_URL_MAX_CHARS)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise invalid_argument("image url must use http or https.", index=index, scheme=parsed.scheme or None)
    normalized = {"type": "image", "url": url, "detail": detail}
    safe = {
        "type": "image",
        "detail": detail,
        "urlScheme": parsed.scheme,
        "urlHash": _safe_digest(url),
    }
    dedup = {
        "type": "image",
        "detail": detail,
        "urlHash": safe["urlHash"],
    }
    return normalized, safe, dedup


def _normalize_local_image_input(
    item: dict[str, Any],
    *,
    detail: str,
    index: int,
    cwd: str,
    allowed_roots: list[Path],
    max_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    raw_path = _optional_string(item.get("path"))
    if not raw_path:
        raise invalid_argument("localImage input item requires path.", index=index)
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        if not cwd:
            raise invalid_argument("Relative localImage path requires a resolved cwd.", index=index)
        candidate = Path(cwd) / candidate
    canonical = canonical_existing_path(candidate)
    path = Path(canonical)
    if not path.exists():
        raise invalid_argument("localImage file was not found.", index=index)
    if not path.is_file():
        raise invalid_argument("localImage path must point to a file.", index=index)
    if not is_allowed_path(path, allowed_roots):
        raise invalid_argument("localImage path is outside the allowlist.", index=index)
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_IMAGE_INPUT_SUFFIXES:
        raise invalid_argument("Unsupported localImage file extension.", index=index, extension=suffix)
    try:
        stat = path.stat()
    except OSError as exc:
        raise invalid_argument("localImage file metadata could not be read.", index=index) from exc
    size = int(stat.st_size)
    if size > max_bytes:
        raise invalid_argument("localImage file is too large.", index=index, maxBytes=max_bytes, actualBytes=size)
    path_hash = _safe_digest(path_key(path))
    normalized = {"type": "localImage", "path": canonical, "detail": detail}
    safe = {
        "type": "localImage",
        "detail": detail,
        "extension": suffix,
        "sizeBytes": size,
        "pathHash": path_hash,
    }
    dedup = {
        "type": "localImage",
        "detail": detail,
        "extension": suffix,
        "sizeBytes": size,
        "mtimeNs": int(stat.st_mtime_ns),
        "pathHash": path_hash,
    }
    return normalized, safe, dedup


def _turn_start_input_items(message: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    input_items = args.get("_input_items")
    if isinstance(input_items, list):
        return copy.deepcopy(input_items)
    return [{"type": "text", "text": message}]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _workflow_goal_hash(objective: str | None, token_budget: int | None) -> str | None:
    if not objective:
        return None
    return hashlib.sha256(
        _canonical_json(
            {
                "objective": objective,
                "tokenBudget": token_budget,
            }
        ).encode("utf-8")
    ).hexdigest()[:32]


def _extract_thread_goal(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    goal = result.get("goal")
    return goal if isinstance(goal, dict) else None


def _thread_goal_hash(goal: dict[str, Any] | None) -> str | None:
    if not goal:
        return None
    objective = _optional_string(goal.get("objective"))
    try:
        token_budget = _optional_bounded_int(goal.get("tokenBudget"), 1, 10000000, field_name="tokenBudget")
    except CodexMcpError:
        token_budget = None
    return _workflow_goal_hash(objective, token_budget)


def _thread_goal_to_tool(goal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not goal:
        return None
    return {
        "threadId": goal.get("threadId"),
        "objective": redact_text(goal.get("objective"), max_chars=1000),
        "status": goal.get("status"),
        "tokenBudget": goal.get("tokenBudget"),
        "tokensUsed": goal.get("tokensUsed"),
        "timeUsedSeconds": goal.get("timeUsedSeconds"),
        "createdAt": goal.get("createdAt"),
        "updatedAt": goal.get("updatedAt"),
    }


def _thread_goal_json(goal: dict[str, Any] | None) -> str | None:
    compact = _thread_goal_to_tool(goal)
    return json.dumps(compact, ensure_ascii=False, sort_keys=True) if compact is not None else None


def _workflow_goal_from_json(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        loaded = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _is_goal_unsupported_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in ("method not found", "unknown method", "unsupported", "not supported", "-32601"))


def _output_schema_digest(schema: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(schema).encode("utf-8")).hexdigest()[:32]


def _schema_type_includes(schema_type: Any, expected: str) -> bool:
    if isinstance(schema_type, str):
        return schema_type == expected
    if isinstance(schema_type, list):
        return expected in schema_type
    return False


def _validate_output_schema_node(schema: dict[str, Any], *, path: str) -> None:
    schema_type = schema.get("type")
    valid_types = {"object", "array", "string", "number", "integer", "boolean", "null"}
    if schema_type is not None:
        if isinstance(schema_type, str):
            if schema_type not in valid_types:
                raise invalid_argument("output_schema has an unsupported type.", schemaType=schema_type, path=path)
        elif isinstance(schema_type, list):
            if not all(isinstance(item, str) and item in valid_types for item in schema_type):
                raise invalid_argument("output_schema has an unsupported type list.", schemaType=schema_type, path=path)
        else:
            raise invalid_argument("output_schema.type must be a string or list of strings.", path=path)
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise invalid_argument("output_schema.properties must be an object.", path=path)
    if "required" in schema and not (
        isinstance(schema["required"], list) and all(isinstance(item, str) and item for item in schema["required"])
    ):
        raise invalid_argument("output_schema.required must be a list of strings.", path=path)
    for key in ("$defs", "definitions"):
        if key in schema and not isinstance(schema[key], dict):
            raise invalid_argument(f"output_schema.{key} must be an object.", path=path)
    if (_schema_type_includes(schema_type, "object") or properties is not None) and schema.get("additionalProperties") is not False:
        raise invalid_argument(
            "output_schema object schemas must set additionalProperties=false.",
            path=path,
        )
    if isinstance(properties, dict):
        for name, child in properties.items():
            if isinstance(child, dict):
                _validate_output_schema_node(child, path=f"{path}.properties.{name}")
    items = schema.get("items")
    if isinstance(items, dict):
        _validate_output_schema_node(items, path=f"{path}.items")
    elif isinstance(items, list):
        for index, child in enumerate(items):
            if isinstance(child, dict):
                _validate_output_schema_node(child, path=f"{path}.items[{index}]")
    for key in ("$defs", "definitions"):
        definitions = schema.get(key)
        if isinstance(definitions, dict):
            for name, child in definitions.items():
                if isinstance(child, dict):
                    _validate_output_schema_node(child, path=f"{path}.{key}.{name}")
    for key in ("allOf", "anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for index, child in enumerate(variants):
                if isinstance(child, dict):
                    _validate_output_schema_node(child, path=f"{path}.{key}[{index}]")


def _validate_output_schema(value: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if value in (None, ""):
        return None, None
    if not isinstance(value, dict):
        raise invalid_argument("output_schema must be a JSON object.")
    try:
        canonical = _canonical_json(value)
        schema = json.loads(canonical)
    except (TypeError, ValueError) as exc:
        raise invalid_argument("output_schema must be JSON serializable.") from exc
    if len(canonical) > OUTPUT_SCHEMA_MAX_CHARS:
        raise invalid_argument("output_schema is too large.", maxChars=OUTPUT_SCHEMA_MAX_CHARS, actualChars=len(canonical))
    if not schema:
        raise invalid_argument("output_schema must not be empty.")
    _validate_output_schema_node(schema, path="$")
    digest = _output_schema_digest(schema)
    return schema, {
        "provided": True,
        "applied": True,
        "source": "request",
        "parseStatus": "pending",
        "schemaHash": digest,
        "schemaChars": len(canonical),
    }


def _extract_structured_report(text: str | None) -> tuple[dict[str, Any] | None, str]:
    raw = _optional_string(text)
    if not raw:
        return None, "empty"
    candidates = [raw]
    for match in re.finditer(r"```(?:json|JSON)?\s*(.*?)```", raw, flags=re.DOTALL):
        candidate = match.group(1).strip()
        if candidate:
            candidates.append(candidate)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, "valid_json"
    return None, "plain_text"


def _stored_final_report_json(
    *,
    final_text: str,
    thread_id: Any,
    turn_id: Any,
    source: Any,
    schema_hash: str | None,
) -> tuple[str, str]:
    structured, parse_status = _extract_structured_report(final_text)
    report_hash_basis = {"text": final_text, "structured": structured, "schemaHash": schema_hash}
    full_report = {
        "text": final_text,
        "summary": final_text,
        "threadId": thread_id,
        "turnId": turn_id,
        "source": source or "storage",
        "readFullVia": "codex_get_chat",
        "structured": structured,
        "structuredStatus": "parsed" if structured is not None else "not_available",
        "structuredParseStatus": parse_status,
        "schemaHash": schema_hash,
    }
    return prompt_hash(_canonical_json(report_hash_basis)), json.dumps(full_report, ensure_ascii=False)


def _report_for_status(stored: dict[str, Any], *, message_max_chars: int) -> dict[str, Any]:
    text = _optional_string(stored.get("text")) or ""
    truncated, budget = _truncate_text(text, message_max_chars)
    report = dict(stored)
    report["text"] = truncated
    report["summary"] = truncated
    report["truncated"] = bool(budget.get("truncated"))
    report["originalChars"] = budget.get("original_chars")
    report["returnedChars"] = budget.get("returned_chars")
    report.setdefault("structuredStatus", "parsed" if isinstance(report.get("structured"), dict) else "not_available")
    report.setdefault("readFullVia", "codex_get_chat")
    return report


def _diagnostic_log_path() -> Path:
    configured = os.environ.get("CODEX_CONTROL_PLANE_MCP_LOG") or os.environ.get("OPENCLAW_CODEX_MCP_LOG")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "logs" / "server.log"


def _rotated_log_paths(log_path: Path) -> list[Path]:
    return [log_path] + [log_path.with_name(log_path.name + f".{index}") for index in range(1, 6)]


def _since_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _workflow_summary_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflowId": row.get("workflow_id"),
        "projectId": row.get("project_id"),
        "threadId": row.get("thread_id"),
        "planTurnId": row.get("plan_turn_id"),
        "executionTurnId": row.get("execution_turn_id"),
        "phase": row.get("phase"),
        "status": row.get("status"),
        "lastError": row.get("last_error"),
        "updatedAt": row.get("updated_at"),
        "appServerGeneration": row.get("app_server_generation"),
    }


def _operation_summary_to_tool(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "operationId": row.get("operation_id"),
        "clientRequestId": row.get("client_request_id"),
        "operationType": row.get("operation_type"),
        "status": row.get("status"),
        "phase": row.get("phase"),
        "projectId": row.get("project_id"),
        "chatId": row.get("chat_id"),
        "threadId": row.get("thread_id"),
        "turnId": row.get("turn_id"),
        "workflowId": row.get("workflow_id"),
        "attemptCount": row.get("attempt_count"),
        "maxAttempts": row.get("max_attempts"),
        "leaseOwner": row.get("lease_owner"),
        "leaseExpiresAt": row.get("lease_expires_at"),
        "nextAttemptAt": row.get("next_attempt_at"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "lastHeartbeatAt": row.get("last_heartbeat_at"),
        "lastError": row.get("last_error"),
        "stalenessSeconds": _staleness_seconds(str(row.get("updated_at") or "")),
    }


def _prompt_submission_summary_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "promptSubmissionId": row.get("prompt_submission_id"),
        "projectId": row.get("project_id"),
        "projectPathKey": row.get("project_path_key"),
        "operationType": row.get("operation_type"),
        "promptHash": row.get("prompt_hash"),
        "operationId": row.get("operation_id"),
        "chatId": row.get("chat_id"),
        "threadId": row.get("thread_id"),
        "turnId": row.get("turn_id"),
        "workflowId": row.get("workflow_id"),
        "status": row.get("status"),
        "duplicateOfSubmissionId": row.get("duplicate_of_submission_id"),
        "similarity": row.get("similarity"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def _tracked_turn_last_error(row: dict[str, Any]) -> Any:
    if row.get("status") == "completed" and row.get("last_error") == WAITING_FOR_OPENCLAW_ERROR:
        return None
    return row.get("last_error")


def _turn_status_with_final_message(status: Any, final_message: str | None) -> str:
    value = str(status or "unknown").strip().lower()
    return value or "unknown"


def _terminal_evidence_from_status(
    status: Any,
    *,
    source: str,
    observed_at: Any,
    method: str,
) -> dict[str, Any]:
    status_value = str(status or "").strip().lower()
    trusted = status_value in TURN_TERMINAL_STATUSES and bool(observed_at)
    return {
        "trusted": trusted,
        "source": source if trusted else None,
        "method": method if trusted else None,
        "observedAt": observed_at if trusted else None,
    }


def _turn_status_has_trusted_terminal_evidence(status: dict[str, Any] | None) -> bool:
    if not isinstance(status, dict):
        return False
    terminal_evidence = status.get("terminalEvidence")
    if isinstance(terminal_evidence, dict):
        return bool(terminal_evidence.get("trusted"))
    status_value = str(status.get("status") or "").strip().lower()
    if status_value not in TURN_TERMINAL_STATUSES:
        return False
    return bool(status.get("completedAt") or status.get("completed_at")) and bool(status.get("completionObserved"))


def _merge_turn_messages(live: dict[str, Any], fallback: dict[str, Any], *, source: str) -> dict[str, Any]:
    merged = dict(live)
    messages = fallback.get("latestMessages") or fallback.get("last_messages") or []
    merged["latestMessages"] = messages
    merged["last_messages"] = messages
    merged["hasMore"] = fallback.get("hasMore", merged.get("hasMore"))
    merged["source"] = source
    return merged


def _operation_reconciliation_state(operation: dict[str, Any], turn_status: dict[str, Any] | None) -> dict[str, Any]:
    evidence = turn_status.get("terminalEvidence") if isinstance(turn_status, dict) else None
    trusted_terminal = _turn_status_has_trusted_terminal_evidence(turn_status)
    operation_status = str(operation.get("status") or "")
    turn_status_value = str((turn_status or {}).get("status") or "")
    return {
        "trustedTerminal": trusted_terminal,
        "terminalEvidenceSource": (evidence or {}).get("source") if isinstance(evidence, dict) else None,
        "terminalEventAt": (evidence or {}).get("observedAt") if isinstance(evidence, dict) else None,
        "statusCorrected": bool(operation.get("_status_corrected")),
    }


def _tracked_turn_summary_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "threadId": row.get("thread_id"),
        "turnId": row.get("turn_id"),
        "chatId": row.get("chat_id"),
        "projectId": row.get("project_id"),
        "status": row.get("status"),
        "startedAt": row.get("started_at"),
        "updatedAt": row.get("updated_at"),
        "completedAt": row.get("completed_at"),
        "processGeneration": row.get("process_generation"),
        "lastError": _tracked_turn_last_error(row),
        "stalenessSeconds": _staleness_seconds(str(row.get("updated_at") or row.get("started_at") or "")),
    }


def _timeline_entry(
    when: Any,
    source: str,
    event: str,
    *,
    operation_id: Any = None,
    workflow_id: Any = None,
    thread_id: Any = None,
    turn_id: Any = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "time": when,
        "source": source,
        "event": event,
        "operationId": operation_id,
        "workflowId": workflow_id,
        "threadId": thread_id,
        "turnId": turn_id,
        "details": details or {},
    }


def _json_loads_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _config_fingerprint_summary(config: ServerConfig) -> dict[str, Any]:
    return {
        "codexHome": str(config.codex_home),
        "sessionsDir": str(config.sessions_dir),
        "codexStateDb": str(config.codex_state_db),
        "stateDbPath": str(config.state_db_path),
        "codexBinaryPath": str(config.codex_binary_path),
        "allowedRoots": [str(root) for root in config.allowed_roots],
    }


def _config_fingerprint(config: ServerConfig) -> str:
    summary = _config_fingerprint_summary(config)
    payload = {
        "codexHome": summary.get("codexHome"),
        "sessionsDir": summary.get("sessionsDir"),
        "codexStateDb": summary.get("codexStateDb"),
        "stateDbPath": summary.get("stateDbPath"),
        "allowedRoots": summary.get("allowedRoots"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _recent_error_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        method = str(event.get("method") or "").casefold()
        if "error" in method or "failed" in method or method in {"turn/error", "error"}:
            result.append(event)
    return result


def _filter_operations_for_context(context: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    operation_id = context.get("operationId")
    workflow_id = context.get("workflowId")
    thread_id = context.get("threadId")
    turn_id = context.get("turnId")
    if not any((operation_id, workflow_id, thread_id, turn_id)):
        return rows
    result: list[dict[str, Any]] = []
    for row in rows:
        if operation_id and row.get("operation_id") == operation_id:
            result.append(row)
            continue
        if workflow_id and row.get("workflow_id") == workflow_id:
            result.append(row)
            continue
        if thread_id and row.get("thread_id") == thread_id:
            result.append(row)
            continue
        if turn_id and row.get("turn_id") == turn_id:
            result.append(row)
            continue
    return result


def _recommended_actions_from_checks(checks: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("suggestedAction"))
        for item in checks
        if item.get("status") in {"warning", "error"} and item.get("suggestedAction")
    ]


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _health_next_action(
    *,
    app_status: dict[str, Any],
    pending_interactions: list[dict[str, Any]],
    stale_operations: list[dict[str, Any]],
    recent_errors: list[dict[str, Any]],
    checks: list[dict[str, Any]],
) -> str:
    if pending_interactions:
        return "answer_pending_interaction"
    if stale_operations:
        return "recover_stale_operations"
    if not bool(app_status.get("running")):
        return "restart_app_server_idle"
    if recent_errors or any(item.get("status") == "error" for item in checks):
        return "inspect_diagnostics"
    if any(item.get("status") == "warning" for item in checks):
        return "inspect_diagnostics"
    return "none"


def _diagnosis_confidence(*, checks: list[dict[str, Any]], correlation: dict[str, Any], timeline: list[dict[str, Any]]) -> str:
    if correlation.get("operation") and (correlation.get("trackedTurn") or timeline):
        return "high"
    if any(item.get("status") in {"warning", "error"} for item in checks):
        return "medium"
    if correlation.get("workflow") or correlation.get("operation"):
        return "medium"
    return "low"


def _canonical_repair_action(action_name: str) -> str:
    aliases = {
        "mark_stale_turns_orphaned": "mark_orphaned_after_exit",
        "refresh_catalog": "refresh_catalog_and_history",
        "rebuild_search_index": "refresh_catalog_and_history",
        "refresh_catalog_and_kb": "refresh_catalog_and_history",
        "workflow_thread_reconcile": "reconcile_workflow_from_thread",
        "retry_workflow": "retry_workflow_with_runtime_policy",
    }
    return aliases.get(action_name, action_name)


def _contract_version_block(*, generated_at: str) -> dict[str, Any]:
    return {
        "serverName": SERVER_NAME,
        "serverVersion": __version__,
        "contractVersion": CONTRACT_VERSION,
        "toolSurfaceHash": _tool_surface_hash(),
        "stableToolCount": len(STABLE_OPENCLAW_TOOLS),
        "compatibilityToolCount": len(COMPATIBILITY_TOOLS),
        "stableTools": sorted(STABLE_OPENCLAW_TOOLS),
        "compatibilityTools": sorted(COMPATIBILITY_TOOLS),
        "generatedAt": generated_at,
    }


def _tool_surface_hash() -> str:
    surface = []
    for tool in sorted(TOOLS, key=lambda item: str(item.get("name") or "")):
        surface.append(
            {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "inputSchema": tool.get("inputSchema"),
                "outputSchema": tool.get("outputSchema"),
                "contractGroup": (tool.get("annotations") or {}).get("openclawContractGroup"),
            }
        )
    canonical = json.dumps(surface, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _operation_request_payload(args: dict[str, Any], *, operation_type: str, message: str | None) -> dict[str, Any]:
    keys = {
        "project_id",
        "chat_id",
        "thread_id",
        "source_thread_id",
        "expected_turn_id",
        "workflow_id",
        "title",
        "cwd",
        "model",
        "fork_config",
        "ephemeral",
        "output_schema_hash",
        "collaboration_mode",
        "approval_policy",
        "sandbox",
        "force",
        "timeout_seconds",
        "first_message_max_chars",
    }
    payload = {"operation_type": operation_type}
    if message not in (None, ""):
        payload["message"] = message
    for key in sorted(keys):
        value = args.get(key)
        if value not in (None, ""):
            payload[key] = value
    return payload


def _operation_client_request_id(payload: dict[str, Any]) -> str:
    public_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
    canonical = json.dumps(public_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"auto:{digest}"


def _operation_request_from_row(row: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(row.get("request_json") or "{}"))
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _operation_tool_args(payload: dict[str, Any]) -> dict[str, Any]:
    args = dict(payload)
    args.pop("operation_type", None)
    args.pop("client_request_id", None)
    args.setdefault("first_message_timeout_seconds", 0)
    return args


def _operation_row_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    request_payload = _operation_request_from_row(row)
    public_request = {key: value for key, value in request_payload.items() if not key.startswith("_")}
    dedup_metadata = request_payload.get("_dedup_metadata") if isinstance(request_payload.get("_dedup_metadata"), dict) else None
    try:
        result_payload = json.loads(str(row.get("result_json") or "{}"))
    except json.JSONDecodeError:
        result_payload = {}
    if not isinstance(result_payload, dict):
        result_payload = {}
    result = {
        "ok": True,
        "operation_id": row.get("operation_id"),
        "operationId": row.get("operation_id"),
        "client_request_id": row.get("client_request_id"),
        "clientRequestId": row.get("client_request_id"),
        "operation_type": row.get("operation_type"),
        "operationType": row.get("operation_type"),
        "status": row.get("status"),
        "phase": row.get("phase"),
        "project_id": row.get("project_id"),
        "projectId": row.get("project_id"),
        "chat_id": row.get("chat_id"),
        "chatId": row.get("chat_id"),
        "thread_id": row.get("thread_id"),
        "threadId": row.get("thread_id"),
        "turn_id": row.get("turn_id"),
        "turnId": row.get("turn_id"),
        "workflow_id": row.get("workflow_id"),
        "workflowId": row.get("workflow_id"),
        "cwd": row.get("cwd"),
        "title": row.get("title"),
        "attempt_count": row.get("attempt_count"),
        "attemptCount": row.get("attempt_count"),
        "max_attempts": row.get("max_attempts"),
        "maxAttempts": row.get("max_attempts"),
        "next_attempt_at": row.get("next_attempt_at"),
        "nextAttemptAt": row.get("next_attempt_at"),
        "created_at": row.get("created_at"),
        "createdAt": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "updatedAt": row.get("updated_at"),
        "started_at": row.get("started_at"),
        "startedAt": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "completedAt": row.get("completed_at"),
        "latestReportHash": row.get("latest_report_hash"),
        "last_error": row.get("last_error"),
        "lastError": row.get("last_error"),
        "appServerGeneration": row.get("app_server_generation"),
        "request": public_request,
    }
    output_schema_state = request_payload.get("_output_schema_state") if isinstance(request_payload.get("_output_schema_state"), dict) else None
    if output_schema_state is None and request_payload.get("output_schema_hash"):
        output_schema_state = {
            "provided": True,
            "applied": True,
            "source": "request",
            "parseStatus": "pending",
            "schemaHash": request_payload.get("output_schema_hash"),
        }
    if output_schema_state is not None:
        result["outputSchemaState"] = dict(output_schema_state)
    input_item_state = request_payload.get("_input_item_state")
    if isinstance(input_item_state, dict):
        result["inputItemState"] = dict(input_item_state)
    runtime_policy = request_payload.get("_runtime_policy")
    if isinstance(runtime_policy, dict):
        result.update(_runtime_policy_public_fields(runtime_policy))
    original_operation_type = request_payload.get("original_operation_type")
    if original_operation_type:
        result["originalOperationType"] = original_operation_type
    if dedup_metadata:
        result.update(
            {
                "deduplicated": True,
                "dedupAction": "continued_existing_chat",
                "duplicateOfSubmissionId": dedup_metadata.get("duplicateOfSubmissionId"),
                "similarity": round(float(dedup_metadata.get("similarity") or 0), 4),
                "existingOperationId": dedup_metadata.get("existingOperationId"),
                "existingChatId": dedup_metadata.get("existingChatId"),
                "existingThreadId": dedup_metadata.get("existingThreadId"),
                "existingTurnId": dedup_metadata.get("existingTurnId"),
                "existingStatus": dedup_metadata.get("existingStatus"),
            }
        )
        result.setdefault("originalOperationType", dedup_metadata.get("originalOperationType"))
    if row.get("operation_type") == "steer_turn":
        result_steer_state = result_payload.get("steerState") if isinstance(result_payload.get("steerState"), dict) else {}
        client_user_message_id = (
            result_steer_state.get("clientUserMessageId")
            or request_payload.get("_client_user_message_id")
            or request_payload.get("client_user_message_id")
        )
        result["steerState"] = {
            "accepted": bool(result_steer_state.get("accepted")) or bool(row.get("result_json")),
            "targetThreadId": result_steer_state.get("targetThreadId") or row.get("thread_id") or request_payload.get("thread_id"),
            "targetTurnId": result_steer_state.get("targetTurnId") or row.get("turn_id") or request_payload.get("expected_turn_id"),
            "clientUserMessageId": client_user_message_id,
        }
    if row.get("operation_type") == "fork_thread":
        result_fork_state = result_payload.get("forkState") if isinstance(result_payload.get("forkState"), dict) else {}
        result["forkState"] = {
            "accepted": bool(result_fork_state.get("accepted")) or bool(row.get("thread_id")),
            "sourceThreadId": result_fork_state.get("sourceThreadId") or request_payload.get("source_thread_id"),
            "forkedThreadId": result_fork_state.get("forkedThreadId") or row.get("thread_id"),
            "hasInitialMessage": bool(result_fork_state.get("hasInitialMessage")) or bool(_optional_string(request_payload.get("message"))),
            "cwd": result_fork_state.get("cwd") or row.get("cwd") or request_payload.get("cwd"),
            "model": result_fork_state.get("model") or request_payload.get("model"),
            "ephemeral": bool(result_fork_state.get("ephemeral")) or bool(request_payload.get("ephemeral", False)),
            "turnId": result_fork_state.get("turnId") or row.get("turn_id"),
            "startAttempted": bool(result_fork_state.get("startAttempted")) or bool(request_payload.get("_fork_start_attempted")),
            "startAttemptedAt": result_fork_state.get("startAttemptedAt") or request_payload.get("_fork_start_attempted_at"),
            "ambiguous": bool(result_fork_state.get("ambiguous"))
            or (bool(request_payload.get("_fork_start_attempted")) and not row.get("thread_id") and row.get("status") == "unknown_after_app_server_exit"),
        }
    if row.get("operation_type") == "review_start":
        result_review_state = result_payload.get("reviewState") if isinstance(result_payload.get("reviewState"), dict) else {}
        target = result_review_state.get("target") if isinstance(result_review_state.get("target"), dict) else request_payload.get("_review_target")
        result["reviewState"] = {
            "accepted": bool(result_review_state.get("accepted")) or bool(row.get("turn_id")),
            "sourceThreadId": result_review_state.get("sourceThreadId") or request_payload.get("_review_source_thread_id") or request_payload.get("thread_id"),
            "reviewThreadId": row.get("thread_id") or result_review_state.get("reviewThreadId"),
            "reviewTurnId": row.get("turn_id") or result_review_state.get("reviewTurnId"),
            "target": target if isinstance(target, dict) else None,
            "delivery": result_review_state.get("delivery") or request_payload.get("_review_delivery") or request_payload.get("delivery"),
            "startAttempted": bool(request_payload.get("_review_start_attempted")),
        }
    return result


def _staleness_seconds(updated_at: str) -> int | None:
    parsed = _parse_iso_datetime(updated_at)
    if parsed is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _stable_mtime_ns(updated_at: str | None) -> int:
    parsed = _parse_iso_datetime(updated_at)
    if parsed is None:
        return 0
    return int(parsed.timestamp() * 1_000_000_000)


def _operation_next_action(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    if status == "queued":
        return "wait_for_background_worker"
    if status in {"starting_app_server", "starting_thread", "starting_review", "starting_turn"}:
        return "poll_operation_status"
    if status in {"waiting_for_approval", "waiting_for_user_input"}:
        return "answer_pending_interaction"
    if status == "running":
        return "poll_turn_status"
    if status == "completed":
        if payload.get("operationType") == "fork_thread" and not payload.get("turnId"):
            return "read_forked_thread"
        return "read_final_report"
    if status == "orphaned" and (payload.get("turnId") or payload.get("threadId")):
        return "poll_history"
    if status in {"failed", "orphaned", "cancelled", "canceled", "interrupted", "unknown_after_app_server_exit"}:
        return "inspect_diagnostics"
    return "poll_operation_status"


def _operation_poll_after(payload: dict[str, Any]) -> int:
    status = str(payload.get("status") or "")
    if status in OPERATION_STARTABLE_STATUSES:
        return 2
    if status in {"waiting_for_approval", "waiting_for_user_input"}:
        return 10
    if status == "running":
        return 15
    return 0


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_string(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise invalid_argument(f"{key} must be a non-empty string")
    return value.strip()


def _bounded_int(value: Any, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise invalid_argument("Expected integer value") from exc
    if parsed < min_value or parsed > max_value:
        raise invalid_argument(f"Integer value must be between {min_value} and {max_value}")
    return parsed


def _extract_turn_id(result: dict[str, Any]) -> str | None:
    return result.get("turnId") or (result.get("turn") or {}).get("id")


def _extract_thread_id(result: dict[str, Any]) -> str | None:
    return result.get("threadId") or result.get("id") or (result.get("thread") or {}).get("id")


def _extract_review_thread_id(result: dict[str, Any]) -> str | None:
    return result.get("reviewThreadId") or result.get("review_thread_id") or _extract_thread_id(result)


def _extract_review_turn_id(result: dict[str, Any]) -> str | None:
    for key in ("reviewTurnId", "review_turn_id"):
        value = result.get(key)
        if value:
            return str(value)
    for key in ("reviewTurn", "review_turn"):
        nested = result.get(key)
        if isinstance(nested, dict) and nested.get("id"):
            return str(nested["id"])
        if isinstance(nested, dict) and nested.get("turnId"):
            return str(nested["turnId"])
    review = result.get("review")
    if isinstance(review, dict):
        turn = review.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        if review.get("turnId"):
            return str(review["turnId"])
    return _extract_turn_id(result)


def _review_target_from_args(args: dict[str, Any]) -> dict[str, Any]:
    target_type = _required_string(args, "target_type")
    if target_type == "uncommitted_changes":
        return {"type": "uncommittedChanges"}
    if target_type == "base_branch":
        return {"type": "baseBranch", "branch": _required_string(args, "base_branch")}
    if target_type == "commit":
        target = {"type": "commit", "sha": _required_string(args, "commit_sha")}
        title = _optional_string(args.get("commit_title"))
        if title:
            target["title"] = title
        return target
    if target_type == "custom":
        return {"type": "custom", "instructions": _required_string(args, "instructions")}
    raise invalid_argument("Unsupported review target_type.", target_type=target_type)


def _review_turn_initial_status(turn: dict[str, Any]) -> str:
    status = str((turn or {}).get("status") or "").strip().lower()
    if status in {"completed", "complete", "done"}:
        return "completed"
    if status in {"failed", "error"}:
        return "failed"
    if status in {"interrupted", "cancelled", "canceled", "aborted"}:
        return "interrupted" if status == "interrupted" else status
    return "running"


def _review_target_label(target: dict[str, Any]) -> str:
    target_type = str(target.get("type") or "review")
    if target_type == "baseBranch":
        return f"Code review against base branch {target.get('branch') or ''}".strip()
    if target_type == "commit":
        title = _optional_string(target.get("title"))
        return f"Code review for commit {target.get('sha') or ''}{': ' + title if title else ''}".strip()
    if target_type == "custom":
        return "Code review with custom instructions"
    return "Code review for uncommitted changes"


def _operation_review_start_attempted(operation: dict[str, Any]) -> bool:
    payload = _operation_request_from_row(operation)
    return bool(payload.get("_review_start_attempted"))


def _review_start_error_is_ambiguous(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ("timeout", "timed out", "stdout closed", "connection", "cancelled", "canceled"))


def _redacted_preview(value: str, limit: int = 120) -> str:
    text = value.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-[redacted]", text)
    text = re.sub(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b", "[telegram-token-redacted]", text)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _approval_policy_for_send(value: Any, thread_row: Any, default_policy: str) -> str:
    selected = str(value or default_policy or "on-request")
    if value == "respect_existing":
        existing = getattr(thread_row, "approval_mode", None)
        if existing in {"never", "on-request", "on-failure", "untrusted"}:
            return str(existing)
        if default_policy in {"never", "on-request", "on-failure", "untrusted"}:
            return default_policy
        return "on-request"
    if selected == "never_auto_approve":
        return "never"
    if selected == "ask_openclaw":
        return "on-request"
    if selected in {"never", "on-request", "on-failure", "untrusted"}:
        return selected
    return "on-request"


def _approval_policy_for_start(value: Any, default_policy: str) -> str:
    selected = str(value or default_policy or "on-request")
    if selected == "ask_openclaw":
        return "on-request"
    if selected in {"never", "on-request", "on-failure", "untrusted"}:
        return selected
    return "on-request"


def _sandbox_policy_for_send(value: Any, thread_row: Any, default_policy: dict[str, Any]) -> dict[str, Any]:
    if value == "respect_existing":
        existing = getattr(thread_row, "sandbox_policy", None)
        if isinstance(existing, dict) and isinstance(existing.get("type"), str) and existing["type"]:
            return existing
    return _sandbox_policy(value) or default_policy


def _sandbox_policy(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if value == "read-only":
        return {"type": "readOnly"}
    if value == "workspace-write":
        return {"type": "workspaceWrite"}
    if value == "danger-full-access":
        return {"type": "dangerFullAccess"}
    raise invalid_argument("Unsupported sandbox value", sandbox=value)


def _sandbox_value_from_policy(policy: dict[str, Any]) -> str:
    policy_type = str((policy or {}).get("type") or "")
    if policy_type == "readOnly":
        return "read-only"
    if policy_type == "workspaceWrite":
        return "workspace-write"
    if policy_type == "dangerFullAccess":
        return "danger-full-access"
    return "read-only"


_PLAN_MODE_SANDBOX_RANK = {
    "read-only": 0,
    "respect_existing": 0,
    "workspace-write": 1,
    "danger-full-access": 2,
}


def _plan_mode_sandbox_floor(default_sandbox_policy: dict[str, Any]) -> str:
    configured = _sandbox_value_from_policy(default_sandbox_policy)
    if _PLAN_MODE_SANDBOX_RANK.get(configured, 0) > _PLAN_MODE_SANDBOX_RANK["workspace-write"]:
        return configured
    return "workspace-write"


def _raise_plan_mode_sandbox_to_floor(requested_sandbox: str, floor: str) -> tuple[str, bool, str | None]:
    requested_rank = _PLAN_MODE_SANDBOX_RANK.get(requested_sandbox, 0)
    floor_rank = _PLAN_MODE_SANDBOX_RANK.get(floor, _PLAN_MODE_SANDBOX_RANK["workspace-write"])
    if requested_rank >= floor_rank:
        return requested_sandbox, False, None
    reason = "plan_mode_requires_workspace_write"
    if floor != "workspace-write":
        reason = "plan_mode_uses_configured_sandbox_floor"
    return floor, True, reason


def _plan_mode_runtime_policy(
    args: dict[str, Any],
    *,
    default_sandbox_policy: dict[str, Any],
    default_approval_policy: str,
) -> dict[str, Any]:
    sandbox_floor = _plan_mode_sandbox_floor(default_sandbox_policy)
    requested_sandbox = _optional_string(args.get("sandbox")) or _sandbox_value_from_policy(default_sandbox_policy)
    requested_approval = _optional_string(args.get("approval_policy")) or default_approval_policy
    effective_sandbox, sandbox_adjusted, sandbox_reason = _raise_plan_mode_sandbox_to_floor(requested_sandbox, sandbox_floor)
    effective_approval = _approval_policy_for_start(requested_approval, default_approval_policy)
    approval_adjusted = False
    approval_reason: str | None = None
    if default_approval_policy == "never" and effective_approval != "never":
        effective_approval = "never"
        approval_adjusted = True
        approval_reason = "plan_mode_uses_configured_approval_policy"
    adjusted = sandbox_adjusted or approval_adjusted
    reason = sandbox_reason or approval_reason
    if sandbox_adjusted and approval_adjusted:
        reason = "plan_mode_uses_configured_runtime_policy"
    return {
        "mode": "plan",
        "requestedSandbox": requested_sandbox,
        "effectiveSandbox": effective_sandbox,
        "requestedApprovalPolicy": requested_approval,
        "effectiveApprovalPolicy": effective_approval,
        "runtimePolicyAdjusted": adjusted,
        "adjustmentReason": reason,
        "sandboxFloor": sandbox_floor,
        "sandboxPolicyAdjusted": sandbox_adjusted,
        "approvalPolicyAdjusted": approval_adjusted,
    }


def _apply_plan_mode_runtime_policy(
    args: dict[str, Any],
    *,
    default_sandbox_policy: dict[str, Any],
    default_approval_policy: str,
) -> dict[str, Any] | None:
    if str(args.get("collaboration_mode") or "").strip() != "plan":
        return None
    state = _plan_mode_runtime_policy(
        args,
        default_sandbox_policy=default_sandbox_policy,
        default_approval_policy=default_approval_policy,
    )
    args["sandbox"] = state["effectiveSandbox"]
    args["approval_policy"] = state["effectiveApprovalPolicy"]
    args["_runtime_policy"] = state
    return state


def _runtime_policy_public_fields(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    public = dict(state)
    return {
        "runtimePolicy": public,
        "runtimePolicyAdjusted": bool(public.get("runtimePolicyAdjusted")),
        "requestedSandbox": public.get("requestedSandbox"),
        "effectiveSandbox": public.get("effectiveSandbox"),
        "requestedApprovalPolicy": public.get("requestedApprovalPolicy"),
        "effectiveApprovalPolicy": public.get("effectiveApprovalPolicy"),
    }


def _collaboration_mode(value: Any, *, model: str | None, config: ServerConfig) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    selected = str(value).strip()
    if selected not in {"default", "plan"}:
        raise invalid_argument("Unsupported collaboration_mode value", collaboration_mode=selected)
    return {
        "mode": selected,
        "settings": {
            "model": model or config.default_model,
            "reasoning_effort": None,
            "developer_instructions": None,
        },
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _safe_codex_timestamp(value: Any) -> tuple[str | None, bool]:
    if value in (None, ""):
        return None, False
    parsed: datetime | None = None
    corrected = False
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None, True
        if numeric > 1e17:
            seconds = numeric / 1_000_000_000
        elif numeric > 1e14:
            seconds = numeric / 1_000_000
        elif numeric > 1e11:
            seconds = numeric / 1_000
        else:
            seconds = numeric
        try:
            parsed = datetime.fromtimestamp(seconds, timezone.utc)
            corrected = True
        except (OverflowError, OSError, ValueError):
            return None, True
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return _safe_codex_timestamp(float(text))
            except ValueError:
                return None, True
    if parsed is None:
        return None, corrected
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
        corrected = True
    parsed = parsed.astimezone(timezone.utc)
    if parsed.year < 2020:
        return None, True
    return parsed.isoformat(), corrected


def _chat_to_tool(chat: Chat, *, include_preview: bool, title_max_chars: int, preview_max_chars: int) -> dict[str, Any]:
    payload = chat.to_tool()
    title, title_meta = _truncate_text(payload.get("title"), title_max_chars)
    payload["title"] = title
    payload["title_truncated"] = bool(title_meta.get("truncated"))
    payload["title_original_chars"] = title_meta.get("original_chars", len(title or ""))
    if include_preview:
        preview, preview_meta = _truncate_text(payload.get("last_message_preview"), preview_max_chars)
        payload["last_message_preview"] = preview
        payload["preview_truncated"] = bool(preview_meta.get("truncated"))
    else:
        payload["last_message_preview"] = None
        payload["preview_available"] = bool(chat.last_message_preview)
    return payload


def _pending_interaction_summary(row: dict[str, Any]) -> dict[str, Any]:
    return interaction_row_to_tool(row)


def _plan_row_to_tool(row: dict[str, Any], max_chars: int) -> dict[str, Any]:
    raw_text = str(row.get("text") or "")
    text, meta = _truncate_text(raw_text, max_chars)
    created_at, created_corrected = _safe_codex_timestamp(row.get("created_at"))
    updated_at, updated_corrected = _safe_codex_timestamp(row.get("updated_at"))
    completed_at, completed_corrected = _safe_codex_timestamp(row.get("completed_at"))
    return {
        "item_id": row.get("item_id"),
        "itemId": row.get("item_id"),
        "thread_id": row.get("thread_id"),
        "threadId": row.get("thread_id"),
        "turn_id": row.get("turn_id"),
        "turnId": row.get("turn_id"),
        "status": row.get("status"),
        "markdown": text,
        "text": text,
        "created_at": created_at,
        "createdAt": created_at,
        "updated_at": updated_at,
        "updatedAt": updated_at,
        "completed_at": completed_at,
        "completedAt": completed_at,
        "timestampCorrected": bool(created_corrected or updated_corrected or completed_corrected),
        "truncated": bool(meta.get("truncated")),
        "originalChars": meta.get("original_chars"),
        "planQuality": classify_plan_artifact(raw_text, row.get("payload_json")),
        **plan_quality_payload(raw_text, row.get("payload_json")),
    }


def _latest_plan(plans: list[dict[str, Any]]) -> dict[str, Any] | None:
    completed = [plan for plan in plans if plan.get("status") == "completed"]
    if completed:
        return completed[-1]
    return plans[-1] if plans else None


def _derive_workflow_phase(
    workflow: dict[str, Any],
    *,
    plan_turn: dict[str, Any] | None,
    execution_turn: dict[str, Any] | None,
    latest_plan: dict[str, Any] | None,
    pending_interactions: list[dict[str, Any]],
    plan_operation: dict[str, Any] | None = None,
    execution_operation: dict[str, Any] | None = None,
) -> tuple[str, str, str | None]:
    if pending_interactions:
        if any(item.get("kind") == "user_input" for item in pending_interactions):
            return "waiting_for_user_input", "waiting_for_user_input", workflow.get("last_error")
        return "waiting_for_approval", "waiting_for_approval", workflow.get("last_error")

    if execution_turn is not None:
        execution_status = str(execution_turn.get("status") or "unknown")
        if execution_status == "completed":
            return "completed", "completed", None
        if execution_status in {"failed", "aborted", "cancelled", "canceled", "interrupted"}:
            return "failed", execution_status, execution_turn.get("lastError") or workflow.get("last_error")
        if execution_status == "unknown_after_app_server_exit":
            return "orphaned_after_app_server_exit", "orphaned_after_app_server_exit", execution_turn.get("lastError") or workflow.get("last_error")
        return "executing", execution_status if execution_status != "unknown" else "executing", None

    if execution_operation is not None:
        operation_status = str(execution_operation.get("status") or "unknown")
        if operation_status in {"failed", "cancelled", "canceled", "interrupted", "unknown_after_app_server_exit"}:
            phase = "orphaned_after_app_server_exit" if operation_status == "unknown_after_app_server_exit" else "failed"
            return phase, operation_status, execution_operation.get("lastError") or workflow.get("last_error")
        if operation_status == "completed":
            return "completed", "completed", None
        return "executing", operation_status if operation_status != "unknown" else "executing", None

    plan_status = str((plan_turn or {}).get("status") or "unknown")
    if latest_plan is not None and latest_plan.get("status") == "completed":
        quality = str(latest_plan.get("planQuality") or latest_plan.get("quality") or classify_plan_text(latest_plan.get("markdown") or latest_plan.get("text")))
        if quality == "valid_plan":
            return "plan_ready", "plan_ready", None
        if quality in {"blocker", "refusal"}:
            return "failed", "failed", "Plan turn completed with a blocker/refusal instead of an executable plan."
        return "plan_needs_review", "plan_needs_review", None
    if plan_status in {"failed", "aborted", "cancelled", "canceled", "interrupted"}:
        return "failed", plan_status, (plan_turn or {}).get("lastError") or workflow.get("last_error")
    if plan_status == "unknown_after_app_server_exit":
        return "orphaned_after_app_server_exit", "orphaned_after_app_server_exit", (plan_turn or {}).get("lastError") or workflow.get("last_error")
    if plan_status == "completed" and latest_plan is None:
        return "failed", "failed", "Plan turn completed but no structured plan item was captured."
    if plan_operation is not None:
        operation_status = str(plan_operation.get("status") or "unknown")
        if operation_status in {"failed", "cancelled", "canceled", "interrupted", "unknown_after_app_server_exit"}:
            phase = "orphaned_after_app_server_exit" if operation_status == "unknown_after_app_server_exit" else "failed"
            return phase, operation_status, plan_operation.get("lastError") or workflow.get("last_error")
    return "planning", plan_status if plan_status not in {"unknown", ""} else "planning", None


def _derive_review_workflow_phase(
    workflow: dict[str, Any],
    *,
    review_turn: dict[str, Any] | None,
    review_operation: dict[str, Any] | None,
    pending_interactions: list[dict[str, Any]],
) -> tuple[str, str, str | None]:
    if pending_interactions:
        if any(item.get("kind") == "user_input" for item in pending_interactions):
            return "waiting_for_user_input", "waiting_for_user_input", workflow.get("last_error")
        return "waiting_for_approval", "waiting_for_approval", workflow.get("last_error")

    if _optional_string(workflow.get("final_report_json")):
        return "completed", "completed", None

    if review_turn is not None:
        turn_status = str(review_turn.get("status") or "unknown")
        if turn_status == "completed":
            return "completed", "completed", None
        if turn_status in {"failed", "aborted", "cancelled", "canceled", "interrupted"}:
            status = "failed" if turn_status == "failed" else turn_status
            return "failed", status, review_turn.get("lastError") or review_turn.get("last_error") or workflow.get("last_error")
        if turn_status == "unknown_after_app_server_exit":
            return "orphaned", "unknown_after_app_server_exit", review_turn.get("lastError") or review_turn.get("last_error") or workflow.get("last_error")
        if turn_status and turn_status != "unknown":
            return "reviewing", turn_status if turn_status not in {"running", "first_message_received"} else "reviewing", None

    if review_operation is not None:
        operation_status = str(review_operation.get("status") or "unknown")
        if operation_status == "completed":
            return "completed", "completed", None
        if operation_status == "unknown_after_app_server_exit":
            return "orphaned", "unknown_after_app_server_exit", review_operation.get("lastError") or workflow.get("last_error")
        if operation_status in {"failed", "aborted", "cancelled", "canceled", "interrupted", "orphaned"}:
            return "failed", operation_status, review_operation.get("lastError") or workflow.get("last_error")
        if operation_status == "starting_thread":
            return "starting_thread", "starting_thread", None
        if operation_status == "starting_review":
            return "starting_review", "starting_review", None
        if operation_status in {"queued", "starting_app_server"}:
            return "queued", operation_status, None
        if operation_status in {"running", "first_message_received"}:
            return "reviewing", "reviewing", None

    phase = str(workflow.get("phase") or "queued")
    status = str(workflow.get("status") or phase)
    return phase, status, workflow.get("last_error")


def _next_workflow_action(phase: str) -> str:
    if phase == "plan_ready":
        return "execute_plan"
    if phase == "plan_needs_review":
        return "review_plan"
    if phase == "plan_candidate_found":
        return "adopt_candidate_plan"
    if phase in {"waiting_for_approval", "waiting_for_user_input"}:
        return "answer_pending_interaction"
    if phase == "planning":
        return "wait_plan"
    if phase == "executing":
        return "wait_execution"
    if phase == "completed":
        return "read_final_report"
    if phase in {"orphaned_after_app_server_exit", "failed"}:
        return "inspect_diagnostics"
    return "wait_plan"


def _next_review_workflow_action(phase: str, status: str) -> str:
    if phase in {"waiting_for_approval", "waiting_for_user_input"}:
        return "answer_pending_interaction"
    if phase == "completed" or status == "completed":
        return "read_review_report"
    if phase in {"failed", "orphaned"} or status in {"failed", "unknown_after_app_server_exit", "orphaned", "interrupted", "cancelled", "canceled"}:
        return "inspect_diagnostics"
    return "wait_review"


def _workflow_poll_seconds(phase: str) -> int:
    if phase in {"waiting_for_approval", "waiting_for_user_input"}:
        return 15
    if phase == "planning":
        return 10
    if phase == "executing":
        return 30
    if phase in {"plan_ready", "plan_needs_review", "plan_candidate_found", "completed", "failed", "orphaned_after_app_server_exit"}:
        return 0
    return 15


def _review_workflow_poll_seconds(phase: str, status: str) -> int:
    if phase in {"waiting_for_approval", "waiting_for_user_input"}:
        return 15
    if phase in {"completed", "failed", "orphaned"} or status in {"completed", "failed", "unknown_after_app_server_exit"}:
        return 0
    if phase in {"queued", "starting_thread", "starting_review"}:
        return 5
    return 15


def _workflow_review_target(workflow: dict[str, Any]) -> dict[str, Any] | None:
    try:
        loaded = json.loads(str(workflow.get("review_target_json") or "null"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _workflow_event_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    try:
        loaded = json.loads(str(row.get("details_json") or "{}"))
        if isinstance(loaded, dict):
            details = loaded
    except json.JSONDecodeError:
        details = {}
    return {
        "id": row.get("id"),
        "workflowId": row.get("workflow_id"),
        "eventType": row.get("event_type"),
        "message": row.get("message"),
        "details": details,
        "createdAt": row.get("created_at"),
    }


def _min_staleness(values: list[Any]) -> int | None:
    parsed = [_parse_iso(value) for value in values if value]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    newest = max(parsed)
    return max(0, int((datetime.now(timezone.utc) - newest).total_seconds()))


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _truncate_text(value: str | None, limit: int) -> tuple[str | None, dict[str, Any]]:
    if value is None:
        return None, {"original_chars": 0, "returned_chars": 0, "truncated": False}
    if len(value) <= limit:
        return value, {"original_chars": len(value), "returned_chars": len(value), "truncated": False}
    return value[:limit].rstrip() + "...", {"original_chars": len(value), "returned_chars": limit, "truncated": True}


def _message_to_tool(item: TranscriptMessage, include_metadata: bool, include_items: bool) -> dict[str, Any]:
    payload = item.to_tool(include_metadata)
    if not include_items:
        payload["items"] = []
        payload["items_available"] = bool(item.items)
    return payload


def _limit_tail(messages: list[TranscriptMessage], *, max_messages: int, max_chars: int) -> tuple[list[TranscriptMessage], dict[str, Any]]:
    original_count = len(messages)
    original_chars = sum(len(item.text or "") for item in messages)
    selected = messages[-max_messages:]
    while selected and sum(len(item.text or "") for item in selected) > max_chars:
        selected = selected[1:]
    if not selected and messages:
        selected = [_truncate_transcript_message(messages[-1], max_chars)]
    returned_chars = sum(len(item.text or "") for item in selected)
    omitted_messages = original_count - len(selected)
    omitted_chars = max(0, original_chars - returned_chars)
    truncated = omitted_messages > 0 or omitted_chars > 0
    return selected, {
        "tail_truncated": truncated,
        "tail_omitted_messages": omitted_messages,
        "tail_omitted_chars": omitted_chars,
        "has_more_tail": truncated,
    }


def _truncate_transcript_message(message: TranscriptMessage, limit: int) -> TranscriptMessage:
    text = message.text or ""
    if len(text) <= limit:
        return message
    marker = "\n[message truncated by tail_max_chars]"
    shortened = text[: max(0, limit - len(marker))].rstrip() + marker
    metadata = dict(message.metadata)
    metadata.update({"truncated": True, "original_chars": len(text), "returned_chars": len(shortened)})
    return TranscriptMessage(
        message_id=message.message_id,
        thread_id=message.thread_id,
        turn_id=message.turn_id,
        role=message.role,
        created_at=message.created_at,
        text=shortened,
        items=[],
        metadata=metadata,
        source_line_start=message.source_line_start,
        source_line_end=message.source_line_end,
    )


def _with_budget(
    result: dict[str, Any],
    *,
    tool_name: str,
    deepseek_calls: int = 0,
    estimated_chars_sent_to_deepseek: int = 0,
    cache_hit: bool = False,
    truncated_fields: list[str] | None = None,
) -> dict[str, Any]:
    result["budget"] = {
        "tool_name": tool_name,
        "estimated_chars_returned": len(json.dumps(result, ensure_ascii=False)),
        "estimated_chars_sent_to_deepseek": estimated_chars_sent_to_deepseek,
        "deepseek_calls": deepseek_calls,
        "cache_hit": cache_hit,
        "truncated_fields": truncated_fields or [],
    }
    return result


def _budget_for_result(
    result: dict[str, Any],
    tool_name: str,
    history_summary: dict[str, Any] | None,
    truncated_fields: list[str],
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "estimated_chars_returned": len(json.dumps(result, ensure_ascii=False)),
        "estimated_chars_sent_to_deepseek": int((history_summary or {}).get("estimated_chars_sent_to_deepseek") or 0),
        "deepseek_calls": int((history_summary or {}).get("deepseek_calls") or 0),
        "cache_hit": bool((history_summary or {}).get("cache_hit")),
        "truncated_fields": truncated_fields,
    }


def _summary_filter_version(config: ServerConfig) -> str:
    return (
        "v4-meaningful-recent-single"
        f":recent={config.deepseek_recent_messages_limit}"
        f":chars={config.deepseek_max_input_chars_per_chunk}"
        f":smalln={config.deepseek_small_history_message_limit}"
        f":smallc={config.deepseek_small_history_chars}"
    )


def _summary_cache_key(
    thread_id: str,
    transcript_path: str,
    transcript_size: int,
    transcript_mtime_ns: int,
    boundary_line: int | None,
    config: ServerConfig,
) -> str:
    settings = load_deepseek_settings(config)
    payload = {
        "thread_id": thread_id,
        "transcript_path": transcript_path,
        "transcript_size": transcript_size,
        "transcript_mtime_ns": transcript_mtime_ns,
        "boundary_line": boundary_line,
        "recent_limit": config.deepseek_recent_messages_limit,
        "char_budget": config.deepseek_max_input_chars_per_chunk,
        "model": settings.model,
        "filter_version": _summary_filter_version(config),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _last_source_line(messages: list[TranscriptMessage]) -> int | None:
    for message in reversed(messages):
        if message.source_line_end is not None:
            return message.source_line_end
        if message.source_line_start is not None:
            return message.source_line_start
    return None


def _latest_turn(turns: dict[str, Any]) -> dict[str, Any] | None:
    if not turns:
        return None
    latest = sorted(turns.values(), key=lambda turn: str(getattr(turn, "completed_at", None) or getattr(turn, "started_at", None) or ""), reverse=True)[0]
    return {
        "turn_id": latest.turn_id,
        "status": latest.status,
        "started_at": latest.started_at,
        "completed_at": latest.completed_at,
    }


def _last_message_by_role(messages: list[TranscriptMessage], role: str) -> TranscriptMessage | None:
    for message in reversed(messages):
        if message.role == role:
            return message
    return None


def turns_last_assistant(messages: list[TranscriptMessage], turn_id: str) -> str | None:
    for message in reversed(messages):
        if message.turn_id == turn_id and message.role == "assistant":
            return message.text
    return None


def _select_messages(messages: list[TranscriptMessage], range_args: dict[str, Any]) -> tuple[list[TranscriptMessage], dict[str, Any]]:
    mode = range_args.get("mode") or "last_messages"
    limit = _bounded_int(range_args.get("limit", 50), 1, 1000)
    selected = messages
    if mode == "last_messages":
        selected = messages[-limit:]
    elif mode == "last_turns":
        seen: list[str] = []
        for item in messages:
            if item.turn_id and item.turn_id not in seen:
                seen.append(item.turn_id)
        allowed = set(seen[-limit:])
        selected = [item for item in messages if item.turn_id in allowed]
    elif mode == "line_range":
        start = int(range_args.get("from") or 1)
        end = int(range_args.get("to") or 2**31)
        selected = [item for item in messages if (item.source_line_start or 0) >= start and (item.source_line_end or 0) <= end]
    elif mode == "time_range":
        start = range_args.get("from")
        end = range_args.get("to")
        selected = [item for item in messages if (not start or (item.created_at or "") >= start) and (not end or (item.created_at or "") <= end)]
    elif mode == "token_budget":
        budget = int(range_args.get("token_budget") or 12000)
        chars = budget * 4
        total = 0
        picked: list[TranscriptMessage] = []
        for item in reversed(messages):
            total += len(item.text or "")
            picked.append(item)
            if total >= chars:
                break
        selected = list(reversed(picked))
    elif mode == "all":
        selected = messages
    else:
        raise invalid_argument("Unsupported range mode", mode=mode)
    first_idx = messages.index(selected[0]) if selected else 0
    last_idx = messages.index(selected[-1]) if selected else -1
    return selected, {
        "range_used": range_args or {"mode": "last_messages", "limit": limit},
        "has_more_before": bool(messages and first_idx > 0),
        "has_more_after": bool(messages and last_idx < len(messages) - 1),
        "next_cursor": None,
        "prev_cursor": None,
    }


def _split_selected_messages(
    all_messages: list[TranscriptMessage],
    selected: list[TranscriptMessage],
) -> tuple[ChatHistorySplit, bool]:
    split = split_before_latest_user(selected)
    if split.latest_user_index is not None or not selected:
        return split, False

    first_idx = _message_index(all_messages, selected[0])
    last_idx = _message_index(all_messages, selected[-1])
    if first_idx is None or last_idx is None:
        return split, False

    previous_user_idx: int | None = None
    for idx in range(first_idx - 1, -1, -1):
        if all_messages[idx].role == "user":
            previous_user_idx = idx
            break
    if previous_user_idx is None:
        return split, False

    return (
        ChatHistorySplit(
            upper=all_messages[:previous_user_idx],
            lower=all_messages[previous_user_idx : last_idx + 1],
            latest_user_index=previous_user_idx,
        ),
        True,
    )


def _message_index(messages: list[TranscriptMessage], target: TranscriptMessage) -> int | None:
    for idx, message in enumerate(messages):
        if message is target:
            return idx
    if target.message_id:
        for idx, message in enumerate(messages):
            if message.message_id == target.message_id:
                return idx
    return None


def _output_title(value: str | None, limit: int = 240) -> tuple[str | None, dict[str, Any]]:
    if value is None:
        return None, {"title_truncated": False, "title_original_chars": 0}
    if len(value) <= limit:
        return value, {"title_truncated": False, "title_original_chars": len(value)}
    return value[:limit].rstrip() + "...", {"title_truncated": True, "title_original_chars": len(value)}


def _filter_output_messages(messages: list[TranscriptMessage], *, include_operational: bool) -> list[TranscriptMessage]:
    if include_operational:
        return messages
    return [message for message in messages if message.role in {"user", "assistant", "system"}]


def _messages_to_markdown(messages: list[TranscriptMessage]) -> str:
    parts: list[str] = []
    for item in messages:
        header = f"### {item.role}"
        if item.turn_id:
            header += f" ({item.turn_id})"
        parts.append(header)
        parts.append(item.text or "")
    return "\n\n".join(parts)


def _summary_to_markdown(history_summary: dict[str, Any]) -> str:
    status = history_summary.get("status") or "unknown"
    model = history_summary.get("model") or "unknown"
    text = str(history_summary.get("text") or "").strip()
    warnings = history_summary.get("warnings")
    parts = [f"## Сжатая предыдущая история ({status}, {model})"]
    if text:
        parts.append(text)
    else:
        parts.append("[summary unavailable]")
    if isinstance(warnings, list) and warnings:
        parts.append("Warnings: " + "; ".join(str(item) for item in warnings))
    parts.append("## Последний участок без обработки")
    return "\n\n".join(parts)


def _compact_message(item: TranscriptMessage) -> dict[str, Any]:
    text = item.text or ""
    if len(text) > 500:
        text = text[:500] + "\n[truncated]"
    return {
        "message_id": item.message_id,
        "turn_id": item.turn_id,
        "role": item.role,
        "created_at": item.created_at,
        "text": text,
    }

def _install_tool_service_mixins() -> None:
    from .chat_service import ChatServiceMixin
    from .diagnostic_service import DiagnosticServiceMixin
    from .operation_service import OperationServiceMixin
    from .review_service import ReviewServiceMixin
    from .runtime_service import RuntimeServiceMixin
    from .thread_lifecycle_service import ThreadLifecycleServiceMixin
    from .worker_service import WorkerServiceMixin
    from .workflow_service import WorkflowServiceMixin

    for mixin in (
        ChatServiceMixin,
        DiagnosticServiceMixin,
        OperationServiceMixin,
        ReviewServiceMixin,
        RuntimeServiceMixin,
        ThreadLifecycleServiceMixin,
        WorkerServiceMixin,
        WorkflowServiceMixin,
    ):
        for name, value in mixin.__dict__.items():
            if name.startswith("__"):
                continue
            if callable(value):
                setattr(ToolService, name, value)


_install_tool_service_mixins()
