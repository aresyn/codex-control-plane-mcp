from __future__ import annotations

import asyncio
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

from . import __version__
from .catalog import ProjectChatCatalog
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
from .models import Chat, TranscriptMessage, TranscriptSummary
from .pending_interactions import PendingInteractionManager, interaction_row_to_tool
from .prompt_dedup import DEFAULT_PROMPT_SIMILARITY_THRESHOLD, normalize_prompt, prompt_hash, prompt_similarity
from .protocol import with_output_schema
from .search import SearchIndex
from .statuses import (
    OPERATION_ACTIVE_STATUSES,
    OPERATION_STARTABLE_STATUSES,
    OPERATION_TERMINAL_STATUSES,
)
from .storage import McpStorage
from .transcripts import parse_transcript
from .turn_tracker import WAITING_FOR_OPENCLAW_ERROR


UI_RELOAD_NOTE = "Desktop UI may not visually update until restart/reload for UI-started chats."
DEFAULT_TOOL_START_TIMEOUT_SECONDS = 300
DEFAULT_FIRST_MESSAGE_TIMEOUT_SECONDS = 0
OPERATION_LEASE_TTL_SECONDS = 120
OPERATION_HEARTBEAT_SECONDS = 30
LOG = get_logger("tools")
PROMPT_OPERATION_ACTIVE_STATUSES = OPERATION_ACTIVE_STATUSES
CONTRACT_VERSION = "1"
SERVER_NAME = "codex-control-plane-mcp"

STABLE_OPENCLAW_TOOLS = {
    "codex_submit_task",
    "codex_get_operation_status",
    "codex_start_plan_workflow",
    "codex_get_workflow_status",
    "codex_approve_plan",
    "codex_list_pending_interactions",
    "codex_answer_pending_interaction",
    "codex_interrupt_turn",
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


TOOLS: list[dict[str, Any]] = [
    {
        "name": "codex_list_projects",
        "description": "List known Codex projects from the project registry, MCP hook history, transcript index, and read-only Codex state.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
                    "default": "never",
                },
                "collaboration_mode": {"type": ["string", "null"], "enum": ["default", "plan", None], "default": None},
                "sandbox": {
                    "type": "string",
                    "enum": ["danger-full-access", "workspace-write", "read-only", "respect_existing"],
                    "default": "danger-full-access",
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
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", None], "default": "danger-full-access"},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", None], "default": "never"},
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
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", None], "default": "danger-full-access"},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", None], "default": "never"},
                "client_request_id": {"type": ["string", "null"], "default": None},
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
                "approval_policy": {
                    "type": "string",
                    "enum": ["never", "on-request", "on-failure", "untrusted", "respect_existing", "never_auto_approve", "ask_openclaw"],
                    "default": "never",
                },
                "sandbox": {
                    "type": "string",
                    "enum": ["danger-full-access", "workspace-write", "read-only", "respect_existing"],
                    "default": "danger-full-access",
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200, "default": DEFAULT_TOOL_START_TIMEOUT_SECONDS},
                "first_message_max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 8000},
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
                    "default": "never",
                },
                "sandbox": {
                    "type": "string",
                    "enum": ["danger-full-access", "workspace-write", "read-only", "respect_existing"],
                    "default": "danger-full-access",
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
            "required": ["operation_type", "message"],
            "properties": {
                "operation_type": {"type": "string", "enum": ["start_chat", "send_message", "execute_plan"]},
                "client_request_id": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Stable retry idempotency key. If omitted, MCP creates a new operation and relies on prompt deduplication to prevent active duplicate turns.",
                },
                "project_id": {"type": ["string", "null"], "default": None},
                "chat_id": {"type": ["string", "null"], "default": None},
                "workflow_id": {"type": ["string", "null"], "default": None},
                "message": {"type": "string", "minLength": 1, "maxLength": 200000},
                "title": {"type": ["string", "null"], "default": None},
                "cwd": {"type": ["string", "null"], "default": None},
                "model": {"type": ["string", "null"], "default": None},
                "collaboration_mode": {"type": ["string", "null"], "enum": ["default", "plan", None], "default": None},
                "approval_policy": {"type": ["string", "null"], "enum": ["never", "on-request", "on-failure", "untrusted", "ask_openclaw", "respect_existing", "never_auto_approve", None], "default": "never"},
                "sandbox": {"type": ["string", "null"], "enum": ["read-only", "workspace-write", "danger-full-access", "respect_existing", None], "default": "danger-full-access"},
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
        self._startup_recovery = self.storage.recover_startup_operations(now=_now_iso())
        if self._startup_recovery.get("resetOperationIds") or self._startup_recovery.get("runningOperationIds"):
            LOG.info("operation startup recovery owner=%s result=%s", self._worker_owner, self._startup_recovery)
        self.catalog = ProjectChatCatalog(self.config, self.storage)
        self._app_server: CodexAppServerClient | None = None
        self._operation_tasks: dict[str, asyncio.Task[None]] = {}

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

    def _prompt_dedup_basis(self, operation_type: str, message: str, *, workflow: dict[str, Any] | None = None) -> str:
        if operation_type != "execute_plan" or workflow is None:
            return message
        latest_plan = self.storage.get_latest_plan_for_turn(str(workflow.get("plan_turn_id") or ""))
        plan_text = str((latest_plan or {}).get("text") or "").strip()
        if not plan_text:
            return message
        return f"{message}\n\n[latest completed plan]\n{plan_text}"

    def _find_prompt_duplicate(
        self,
        *,
        project_path_key: str,
        normalized_prompt: str,
        normalized_hash: str,
        ignore_submission_id: str | None = None,
        ignore_operation_id: str | None = None,
    ) -> dict[str, Any] | None:
        candidates_by_id: dict[str, tuple[dict[str, Any], float]] = {}
        for row in self.storage.find_prompt_submissions_by_hash(project_path_key, normalized_hash, limit=50):
            prompt_submission_id = str(row.get("prompt_submission_id") or "")
            if prompt_submission_id:
                candidates_by_id[prompt_submission_id] = (row, 1.0)
        for row in self.storage.list_prompt_submissions_for_project(project_path_key, limit=200):
            prompt_submission_id = str(row.get("prompt_submission_id") or "")
            if not prompt_submission_id or prompt_submission_id in candidates_by_id:
                continue
            similarity = prompt_similarity(normalized_prompt, str(row.get("prompt_normalized") or ""))
            if similarity >= DEFAULT_PROMPT_SIMILARITY_THRESHOLD:
                candidates_by_id[prompt_submission_id] = (row, similarity)

        matches: list[dict[str, Any]] = []
        for row, similarity in candidates_by_id.values():
            if ignore_submission_id and row.get("prompt_submission_id") == ignore_submission_id:
                continue
            if ignore_operation_id and row.get("operation_id") == ignore_operation_id:
                continue
            effective_status = self._prompt_submission_effective_status(row)
            enriched = dict(row)
            enriched["similarity"] = similarity
            enriched["effective_status"] = effective_status
            enriched["active"] = effective_status in PROMPT_OPERATION_ACTIVE_STATUSES
            matches.append(enriched)

        if not matches:
            return None
        active = [row for row in matches if row.get("active")]
        if active:
            return sorted(active, key=lambda row: (float(row.get("similarity") or 0), str(row.get("updated_at") or "")), reverse=True)[0]
        resumable = [row for row in matches if self._prompt_duplicate_can_continue(row)]
        if not resumable:
            return None
        return sorted(resumable, key=lambda row: (float(row.get("similarity") or 0), str(row.get("updated_at") or "")), reverse=True)[0]

    def _prompt_duplicate_can_continue(self, row: dict[str, Any]) -> bool:
        if str(row.get("effective_status") or row.get("status") or "") != "completed":
            return False
        thread_id = _optional_string(row.get("thread_id")) or _optional_string(row.get("chat_id"))
        if not thread_id:
            return False
        chat = self.catalog.get_chat(thread_id, _optional_string(row.get("project_id")))
        if chat is not None and chat.archived:
            return False
        return True

    def _prompt_submission_effective_status(self, row: dict[str, Any]) -> str:
        turn_id = _optional_string(row.get("turn_id"))
        if turn_id:
            turn = self.storage.get_tracked_turn(turn_id)
            if turn is not None:
                return str(turn.get("status") or row.get("status") or "unknown")
        operation_id = _optional_string(row.get("operation_id"))
        if operation_id:
            operation = self.storage.get_operation(operation_id)
            if operation is not None:
                return str(operation.get("status") or row.get("status") or "unknown")
        return str(row.get("status") or "unknown")

    def _duplicate_prompt_error(self, match: dict[str, Any]) -> CodexMcpError:
        return duplicate_prompt_active(
            existingOperationId=match.get("operation_id"),
            existingChatId=match.get("chat_id") or match.get("thread_id"),
            existingThreadId=match.get("thread_id") or match.get("chat_id"),
            existingTurnId=match.get("turn_id"),
            existingStatus=match.get("effective_status") or match.get("status"),
            duplicateOfSubmissionId=match.get("prompt_submission_id"),
            similarity=round(float(match.get("similarity") or 0), 4),
            nextRecommendedAction="poll_existing_turn",
        )

    def _create_prompt_submission(
        self,
        *,
        project_id: str | None,
        project_path_key: str,
        operation_type: str,
        message: str,
        normalized_prompt: str,
        normalized_hash: str,
        operation_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        workflow_id: str | None = None,
        status: str = "queued",
        duplicate_of_submission_id: str | None = None,
        similarity: float | None = None,
    ) -> str:
        prompt_submission_id = "ps_" + uuid.uuid4().hex
        now = _now_iso()
        self.storage.create_prompt_submission(
            {
                "prompt_submission_id": prompt_submission_id,
                "project_id": project_id,
                "project_path_key": project_path_key,
                "operation_type": operation_type,
                "prompt_hash": normalized_hash,
                "prompt_normalized": normalized_prompt,
                "prompt_preview": _redacted_preview(message),
                "operation_id": operation_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "workflow_id": workflow_id,
                "status": status,
                "duplicate_of_submission_id": duplicate_of_submission_id,
                "similarity": similarity,
                "created_at": now,
                "updated_at": now,
            }
        )
        return prompt_submission_id

    def _prepare_prompt_submission(
        self,
        *,
        project_id: str | None,
        project_path_key: str,
        operation_type: str,
        message: str,
        dedup_basis: str,
        operation_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        workflow_id: str | None = None,
        ignore_submission_id: str | None = None,
        ignore_operation_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_prompt = normalize_prompt(dedup_basis)
        normalized_hash = prompt_hash(normalized_prompt)
        match = self._find_prompt_duplicate(
            project_path_key=project_path_key,
            normalized_prompt=normalized_prompt,
            normalized_hash=normalized_hash,
            ignore_submission_id=ignore_submission_id,
            ignore_operation_id=ignore_operation_id,
        )
        if match is not None and match.get("active"):
            raise self._duplicate_prompt_error(match)

        duplicate_of = _optional_string((match or {}).get("prompt_submission_id"))
        similarity = float((match or {}).get("similarity") or 0) if match is not None else None
        prompt_submission_id = self._create_prompt_submission(
            project_id=project_id,
            project_path_key=project_path_key,
            operation_type=operation_type,
            message=message,
            normalized_prompt=normalized_prompt,
            normalized_hash=normalized_hash,
            operation_id=operation_id,
            chat_id=chat_id,
            thread_id=thread_id,
            workflow_id=workflow_id,
            status="queued",
            duplicate_of_submission_id=duplicate_of,
            similarity=similarity,
        )
        if match is None:
            return {"action": "new", "promptSubmissionId": prompt_submission_id}
        return {
            "action": "continue_existing_chat",
            "promptSubmissionId": prompt_submission_id,
            "duplicateOfSubmissionId": duplicate_of,
            "similarity": similarity,
            "existingChatId": match.get("chat_id") or match.get("thread_id"),
            "existingThreadId": match.get("thread_id") or match.get("chat_id"),
            "existingTurnId": match.get("turn_id"),
            "existingOperationId": match.get("operation_id"),
            "existingStatus": match.get("effective_status") or match.get("status"),
            "originalOperationType": operation_type,
        }

    def _active_turn_for_thread(self, thread_id: str) -> dict[str, Any] | None:
        for turn in self.storage.get_running_tracked_turns():
            if turn.get("thread_id") == thread_id:
                return turn
        return None

    def _dedup_metadata_for_result(self, dedup: dict[str, Any] | None) -> dict[str, Any]:
        if not dedup or dedup.get("action") == "new":
            return {}
        return {
            "deduplicated": True,
            "dedupAction": "continued_existing_chat",
            "duplicateOfSubmissionId": dedup.get("duplicateOfSubmissionId"),
            "similarity": round(float(dedup.get("similarity") or 0), 4),
            "originalOperationType": dedup.get("originalOperationType"),
            "existingOperationId": dedup.get("existingOperationId"),
            "existingChatId": dedup.get("existingChatId"),
            "existingThreadId": dedup.get("existingThreadId"),
            "existingTurnId": dedup.get("existingTurnId"),
            "existingStatus": dedup.get("existingStatus"),
        }

    async def _send_message_resolved(
        self,
        *,
        chat_id: str,
        thread_id: str,
        project_id: str | None,
        project_path: str,
        message: str,
        args: dict[str, Any],
        prompt_submission_id: str | None = None,
        dedup_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation_id = _optional_string(args.get("_operation_id"))
        active_turn = self._active_turn_for_thread(thread_id)
        if active_turn is not None:
            raise busy(thread_id, str(active_turn.get("status") or "running"))

        timeout_seconds = _bounded_int(args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS), 1, 7200)
        first_message_max_chars = _bounded_int(args.get("first_message_max_chars", 8000), 500, 200000)
        thread_row = self.catalog.get_thread_row(thread_id)
        approval_policy = _approval_policy_for_send(args.get("approval_policy"), thread_row, self.config.default_approval_policy)
        sandbox_policy = _sandbox_policy_for_send(args.get("sandbox"), thread_row, self.config.default_sandbox_policy)
        collaboration_mode = _collaboration_mode(args.get("collaboration_mode"), model=None, config=self.config)
        client = await self._app()
        try:
            if prompt_submission_id:
                self.storage.update_prompt_submission(
                    prompt_submission_id,
                    status="starting_turn",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    project_id=project_id,
                    updated_at=_now_iso(),
                )
            await client.thread_resume(thread_id, project_path, timeout_seconds=timeout_seconds)
            if operation_id:
                self.storage.update_operation(
                    operation_id,
                    status="starting_turn",
                    phase="starting_turn",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    project_id=project_id,
                    cwd=project_path,
                    updated_at=_now_iso(),
                    app_server_generation=client.process_generation,
                )
            result = await client.turn_start(
                thread_id=thread_id,
                input_items=[{"type": "text", "text": message}],
                cwd=project_path,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                model=None,
                effort=self.config.default_effort,
                summary=self.config.default_summary,
                collaboration_mode=collaboration_mode,
                chat_id=chat_id,
                project_id=project_id,
                project_path=project_path,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
            raise
        turn_id = _extract_turn_id(result)
        if not turn_id:
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
            raise send_failed("turn/start did not return turn id")
        if operation_id:
            self.storage.update_operation(
                operation_id,
                status="running",
                phase="running",
                chat_id=chat_id,
                thread_id=thread_id,
                turn_id=turn_id,
                project_id=project_id,
                cwd=project_path,
                last_error=None,
                updated_at=_now_iso(),
                next_attempt_at=None,
                app_server_generation=result.get("_processGeneration") or client.process_generation,
            )
        if prompt_submission_id:
            self.storage.update_prompt_submission(
                prompt_submission_id,
                status="running",
                chat_id=chat_id,
                thread_id=thread_id,
                turn_id=turn_id,
                project_id=project_id,
                updated_at=_now_iso(),
            )
        status_payload = client.tracker.get_turn_status(turn_id, last_messages=10, message_max_chars=first_message_max_chars) or {}
        response = {
            "ok": True,
            "chat_id": chat_id,
            "chatId": chat_id,
            "thread_id": thread_id,
            "threadId": thread_id,
            "project_id": project_id,
            "projectId": project_id,
            "accepted": True,
            "turn_id": turn_id,
            "turnId": turn_id,
            "status": status_payload.get("status") or "running",
            "first_message": None,
            "firstMessage": {
                "role": None,
                "text": None,
                "createdAt": None,
                "truncated": False,
                "observed": False,
                "timedOut": False,
            },
            "first_message_observed": False,
            "first_message_timed_out": False,
            "first_message_truncated": False,
            "latestMessages": status_payload.get("latestMessages") or status_payload.get("last_messages") or [],
            "pollRecommended": True,
            "recommendedPollAfterSeconds": 5,
            "effectiveApprovalPolicy": approval_policy,
            "effectiveSandboxPolicy": sandbox_policy,
            "effectiveCollaborationMode": collaboration_mode,
            "effectiveCwd": project_path,
            "effectiveModel": None,
            "processGeneration": status_payload.get("processGeneration"),
            "appServerGeneration": status_payload.get("appServerGeneration") or status_payload.get("processGeneration"),
            "started_at": status_payload.get("started_at"),
            "startedAt": status_payload.get("startedAt") or status_payload.get("started_at"),
            "updated_at": status_payload.get("updated_at"),
            "updatedAt": status_payload.get("updatedAt") or status_payload.get("updated_at"),
            "note": UI_RELOAD_NOTE,
        }
        response.update(self._dedup_metadata_for_result(dedup_metadata))
        return response

    async def call(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments or {}
        started = time.monotonic()
        LOG.info("call start name=%s argument_keys=%s", name, sorted(args.keys()))
        self._schedule_recoverable_operations()
        try:
            if name == "codex_list_projects":
                result = self.codex_list_projects()
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
            if name == "codex_get_workflow_status":
                result = self.codex_get_workflow_status(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_approve_plan":
                result = self.codex_approve_plan(args)
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
            if name == "codex_restart_app_server":
                result = await self.codex_restart_app_server(args)
                LOG.info("call done name=%s elapsed_ms=%d", name, int((time.monotonic() - started) * 1000))
                return result
            if name == "codex_get_app_server_status":
                result = self.codex_get_app_server_status(args)
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
            return exc.to_dict()
        except RuntimeError as exc:
            LOG.exception("call runtime error name=%s", name)
            return send_failed(str(exc)).to_dict()
        except Exception as exc:
            LOG.exception("call unexpected error name=%s", name)
            return send_failed(str(exc)).to_dict()

    def codex_list_projects(self) -> dict[str, Any]:
        projects = [project.to_tool() for project in self.catalog.list_projects()]
        LOG.info("list_projects count=%d", len(projects))
        return _with_budget({"projects": projects}, tool_name="codex_list_projects")

    def codex_list_project_chats(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = _required_string(args, "project_id")
        if self.catalog.get_project(project_id) is None:
            raise project_not_found(project_id)
        limit = _bounded_int(args.get("limit", 100), 1, 500)
        cursor = args.get("cursor")
        offset = int(cursor) if cursor not in (None, "") else 0
        include_archived = bool(args.get("include_archived", False))
        include_preview = bool(args.get("include_preview", False))
        title_max_chars = _bounded_int(args.get("title_max_chars", 160), 20, 2000)
        preview_max_chars = _bounded_int(args.get("preview_max_chars", 200), 20, 4000)
        chats = self.catalog.list_project_chats(project_id, include_archived=include_archived)
        page = chats[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(chats) else None
        LOG.info("list_project_chats project_id=%s total=%d returned=%d", project_id, len(chats), len(page))
        result = {
            "project_id": project_id,
            "chats": [
                _chat_to_tool(chat, include_preview=include_preview, title_max_chars=title_max_chars, preview_max_chars=preview_max_chars)
                for chat in page
            ],
            "next_cursor": next_cursor,
        }
        return _with_budget(result, tool_name="codex_list_project_chats", truncated_fields=["title", "last_message_preview"])

    def codex_list_active_chats(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = args.get("project_id")
        active_window = _bounded_int(args.get("active_window_minutes", 120), 1, 1440)
        include_running = bool(args.get("include_running", True))
        include_waiting_for_user = bool(args.get("include_waiting_for_user", True))
        include_waiting_for_approval = bool(args.get("include_waiting_for_approval", True))
        include_evidence = bool(args.get("include_evidence", False))
        title_max_chars = _bounded_int(args.get("title_max_chars", 160), 20, 2000)
        self.catalog.list_projects()
        chats = self.catalog.chats.values()
        if project_id:
            if self.catalog.get_project(str(project_id)) is None:
                raise project_not_found(str(project_id))
            chats = [chat for chat in chats if chat.project_id == project_id]
        active: list[dict[str, Any]] = []
        for chat in chats:
            status, confidence, evidence = self.catalog.infer_chat_status(chat, active_window_minutes=active_window)
            pending_interactions = [
                _pending_interaction_summary(row)
                for row in self.storage.list_pending_interactions(thread_id=chat.thread_id, status="pending", limit=10)
            ]
            if pending_interactions and status not in {"running", "waiting_for_approval", "waiting_for_user", "waiting_for_user_input", "failed"}:
                status = (
                    "waiting_for_user_input"
                    if any(item.get("kind") == "user_input" for item in pending_interactions)
                    else "waiting_for_approval"
                )
                confidence = "high"
            if status == "running" and not include_running:
                continue
            if status in {"waiting_for_user", "waiting_for_user_input"} and not include_waiting_for_user:
                continue
            if status == "waiting_for_approval" and not include_waiting_for_approval:
                continue
            if status not in {"running", "waiting_for_approval", "waiting_for_user", "waiting_for_user_input", "failed"}:
                continue
            active.append(
                {
                    "chat_id": chat.chat_id,
                    "thread_id": chat.thread_id,
                    "project_id": chat.project_id,
                    "title": _truncate_text(chat.title or chat.thread_id[:16], title_max_chars)[0],
                    "status": status,
                    "last_activity_at": chat.updated_at,
                    "pending_question": next((item for item in pending_interactions if item.get("kind") == "user_input"), None),
                    "pending_approval": next((item for item in pending_interactions if item.get("kind") != "user_input"), None),
                    "pending_interactions": pending_interactions,
                    "pendingInteractions": pending_interactions,
                    "confidence": confidence,
                    "evidence": evidence if include_evidence else [],
                    "evidence_available": bool(evidence),
                }
            )
        LOG.info("list_active_chats project_id=%s returned=%d", project_id, len(active))
        return _with_budget({"active_chats": active}, tool_name="codex_list_active_chats", truncated_fields=["title", "evidence"])

    def codex_search_chats(self, args: dict[str, Any]) -> dict[str, Any]:
        query = _required_string(args, "query")
        if len(query) > 1000:
            raise invalid_argument("query must be at most 1000 characters")
        project_id = args.get("project_id")
        if project_id:
            project_id = str(project_id)
            if self.catalog.get_project(project_id) is None:
                raise project_not_found(project_id)
        include_archived = bool(args.get("include_archived", False))
        limit = _bounded_int(args.get("limit", 10), 1, 50)
        cursor = args.get("cursor")
        offset = int(cursor) if cursor not in (None, "") else 0
        include_snippets = bool(args.get("include_snippets", True))
        snippets_per_chat = _bounded_int(args.get("snippets_per_chat", 2), 0, 5)
        snippet_max_chars = _bounded_int(args.get("snippet_max_chars", 240), 80, 1000)
        refresh_index = bool(args.get("refresh_index", True))
        index_time_budget_seconds = _bounded_int(args.get("index_time_budget_seconds", 8), 1, 60)
        match_mode = str(args.get("match_mode") or "auto")
        search_index = SearchIndex(self.config, self.storage, self.catalog)
        index_status = None
        if refresh_index:
            index_status = search_index.refresh(
                include_archived=include_archived,
                time_budget_seconds=index_time_budget_seconds,
            )
        else:
            self.catalog.list_projects()
        try:
            parsed, total, results = search_index.search(
                query,
                match_mode=match_mode,
                project_id=project_id,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
                include_snippets=include_snippets,
                snippets_per_chat=snippets_per_chat,
                snippet_max_chars=snippet_max_chars,
            )
        except ValueError as exc:
            raise invalid_argument(str(exc)) from exc
        next_cursor = str(offset + limit) if offset + limit < total else None
        LOG.info(
            "search_chats query_chars=%d mode=%s total=%d returned=%d offset=%d include_archived=%s project_id=%s",
            len(query),
            parsed.match_mode,
            total,
            len(results),
            offset,
            include_archived,
            project_id,
        )
        result = {
            "query": query,
            "normalized_query": parsed.normalized,
            "match_mode": parsed.match_mode,
            "total_results": total,
            "returned_count": len(results),
            "next_cursor": next_cursor,
            "results": results,
            "index_status": index_status.to_tool()
            if index_status is not None
            else {
                "refreshed": False,
                "indexed_files": 0,
                "skipped_unchanged_files": 0,
                "pending_files": 0,
                "time_budget_exhausted": False,
            },
            "source": "chat_search_fts_index",
        }
        return _with_budget(result, tool_name="codex_search_chats", truncated_fields=["snippets", "title", "last_message_preview"])

    def codex_get_chat_status(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = _required_string(args, "chat_id")
        project_id = args.get("project_id")
        preview_max_chars = _bounded_int(args.get("preview_max_chars", 300), 20, 4000)
        chat = self.catalog.get_chat(chat_id, str(project_id) if project_id else None)
        if chat is None:
            raise thread_not_found(chat_id)
        parsed, source_info = self._load_chat_summary(
            chat,
            archived=chat.archived,
            include_tool_calls=False,
            include_tool_outputs=False,
            include_command_outputs=False,
            include_reasoning=False,
        )
        status, confidence, evidence = self.catalog.infer_chat_status(chat)
        result = {
            "chat_id": chat.chat_id,
            "thread_id": chat.thread_id,
            "project_id": chat.project_id,
            "title": _output_title(chat.title or parsed.title, 160)[0],
            "status": status,
            "status_confidence": confidence,
            "updated_at": chat.updated_at or parsed.updated_at,
            "latest_turn": _latest_turn(parsed.turns),
            "last_user_preview": _truncate_text((_last_message_by_role(parsed.messages, "user") or TranscriptMessage(None, "", None, "", None, None)).text, preview_max_chars)[0],
            "last_assistant_preview": _truncate_text((_last_message_by_role(parsed.messages, "assistant") or TranscriptMessage(None, "", None, "", None, None)).text, preview_max_chars)[0],
            "transcript": {
                "path": source_info["path"],
                "size": source_info["size"],
                "mtime": source_info["mtime"],
                "messages": len(parsed.messages),
                "turns": len(parsed.turns),
                "parse_errors": parsed.parse_errors,
                "source": source_info["source"],
            },
            "summary_cache_available": self.storage.has_summary_cache_for_thread(chat.thread_id),
            "evidence_available": bool(evidence),
        }
        return self._finalize_read_result(result, "codex_get_chat_status", chat.thread_id, None, ["title", "last_user_preview", "last_assistant_preview"])

    def codex_get_chat(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = _required_string(args, "chat_id")
        project_id = args.get("project_id")
        chat = self.catalog.get_chat(chat_id, str(project_id) if project_id else None)
        if chat is None:
            raise thread_not_found(chat_id)
        summary, source_info = self._load_chat_summary(
            chat,
            archived=chat.archived,
            include_tool_calls=bool(args.get("include_tool_calls", False)),
            include_tool_outputs=bool(args.get("include_tool_outputs", False)),
            include_command_outputs=bool(args.get("include_command_outputs", False)),
            include_reasoning=bool(args.get("include_reasoning", False)),
        )
        transcript_path = source_info["path"]
        LOG.info("get_chat chat_id=%s source=%s path=%s", chat_id, source_info["source"], transcript_path)
        selected, pagination = _select_messages(summary.messages, args.get("range") or {})
        split, expanded_to_user = _split_selected_messages(summary.messages, selected)
        summary_input, rolling_used = self._summary_input_with_rolling(chat.thread_id, str(transcript_path), split.upper)
        cache_key = _summary_cache_key(
            chat.thread_id,
            str(transcript_path),
            int(source_info["size"]),
            int(source_info["mtime_ns"]),
            _last_source_line(summary_input),
            self.config,
        )
        now = _now_iso()
        force_refresh = bool(args.get("force_refresh_summary", False))
        cached_summary = None if force_refresh else self.storage.get_summary_cache(cache_key, now)
        if cached_summary is not None:
            history_summary = cached_summary
            history_summary["cache_hit"] = True
            history_summary["cache_key"] = cache_key
            history_summary["deepseek_calls"] = 0
            history_summary["estimated_chars_sent_to_deepseek"] = 0
        else:
            summary_result = summarize_chat_history(summary_input, self.config).to_tool()
            summary_result["cache_hit"] = False
            summary_result["cache_key"] = cache_key
            summary_result["created_at"] = now
            summary_result["rolling_summary_used"] = rolling_used
            if rolling_used:
                summary_result["messages_omitted_due_to_cache_or_rollup"] = max(0, len(split.upper) - len(summary_input))
            history_summary = summary_result
            if history_summary.get("status") == "ok":
                self.storage.upsert_summary_cache(
                    {
                        "cache_key": cache_key,
                        "thread_id": chat.thread_id,
                        "transcript_path": str(transcript_path),
                        "transcript_size": int(source_info["size"]),
                        "transcript_mtime_ns": int(source_info["mtime_ns"]),
                        "boundary_line": _last_source_line(summary_input),
                        "model": str(history_summary.get("model") or ""),
                        "filter_version": _summary_filter_version(self.config),
                        "summary_json": json.dumps(history_summary, ensure_ascii=False),
                        "created_at": now,
                        "last_used_at": now,
                    }
                )
                upper_line = _last_source_line(split.upper)
                if self.config.rolling_summary_enabled and upper_line is not None:
                    self.storage.upsert_rolling_summary(
                        {
                            "thread_id": chat.thread_id,
                            "transcript_path": str(transcript_path),
                            "source_line_end": upper_line,
                            "summary_text": str(history_summary.get("text") or ""),
                            "model": str(history_summary.get("model") or ""),
                            "updated_at": now,
                        }
                    )
        if expanded_to_user:
            warnings = history_summary.setdefault("warnings", [])
            if isinstance(warnings, list):
                warnings.append("Selected range did not contain a user/OpenClaw message; raw tail was expanded back to the previous user/OpenClaw message.")
        elif split.latest_user_index is None and selected:
            warnings = history_summary.setdefault("warnings", [])
            if isinstance(warnings, list):
                warnings.append("No user/OpenClaw boundary found; selected range was summarized as history and raw tail is empty.")
        LOG.info(
            "get_chat split chat_id=%s selected=%d upper=%d lower=%d summary_status=%s summary_chars=%d",
            chat_id,
            len(selected),
            len(split.upper),
            len(split.lower),
            history_summary.get("status"),
            len(str(history_summary.get("text") or "")),
        )
        include_metadata = bool(args.get("include_metadata", True))
        status, _, _ = self.catalog.infer_chat_status(chat)
        output_format = args.get("format") or "structured"
        source = f"{source_info['source']}+deepseek_summary" if history_summary.get("status") == "ok" else f"{source_info['source']}+summary_warning"
        title, title_meta = _output_title(chat.title or summary.title)
        tail_max_messages = _bounded_int(args.get("tail_max_messages", self.config.default_tail_max_messages), 1, 1000)
        tail_max_chars = _bounded_int(args.get("tail_max_chars", self.config.default_tail_max_chars), 1000, 200000)
        response_budget_chars = _bounded_int(args.get("response_budget_chars", 50_000), 2000, 300000)
        include_items = bool(args.get("include_items", False))
        plans = [_plan_row_to_tool(row, min(response_budget_chars, 50_000)) for row in self.storage.get_thread_plans(chat.thread_id, limit=50)]
        base = {
            "chat_id": chat.chat_id,
            "thread_id": chat.thread_id,
            "project_id": chat.project_id,
            "title": title,
            **title_meta,
            "status": status,
            "history_summary": history_summary,
            "pagination": pagination,
            "source": source,
            "plans": plans,
            "latestPlan": _latest_plan(plans),
        }
        selected = _filter_output_messages(
            split.lower,
            include_operational=bool(args.get("include_tool_calls", False)),
        )
        summary_chars = len(str(history_summary.get("text") or ""))
        effective_tail_max_chars = max(1000, min(tail_max_chars, response_budget_chars - summary_chars - 2000))
        selected, tail_info = _limit_tail(selected, max_messages=tail_max_messages, max_chars=effective_tail_max_chars)
        base.update(tail_info)
        if output_format == "markdown":
            result = {**base, "markdown": _summary_to_markdown(history_summary) + "\n\n" + _messages_to_markdown(selected)}
            return self._finalize_read_result(result, "codex_get_chat", chat.thread_id, history_summary, ["title", "messages", "items"])
        if output_format == "compact":
            result = {**base, "messages": [_compact_message(item) for item in selected]}
            return self._finalize_read_result(result, "codex_get_chat", chat.thread_id, history_summary, ["title", "messages"])
        result = {**base, "messages": [_message_to_tool(item, include_metadata, include_items) for item in selected]}
        return self._finalize_read_result(result, "codex_get_chat", chat.thread_id, history_summary, ["title", "messages", "items"])

    async def codex_send_message(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = _required_string(args, "chat_id")
        message = _required_string(args, "message")
        resolved_thread_id = _optional_string(args.get("_resolved_thread_id"))
        resolved_project_path = _optional_string(args.get("_resolved_project_path"))
        if resolved_thread_id and resolved_project_path:
            return await self._send_message_resolved(
                chat_id=chat_id,
                thread_id=resolved_thread_id,
                project_id=_optional_string(args.get("project_id")),
                project_path=resolved_project_path,
                message=message,
                args=args,
                prompt_submission_id=_optional_string(args.get("_prompt_submission_id")),
                dedup_metadata=args.get("_dedup_metadata") if isinstance(args.get("_dedup_metadata"), dict) else None,
            )
        project_id = args.get("project_id")
        chat = self.catalog.get_chat(chat_id, str(project_id) if project_id else None)
        if chat is None:
            raise thread_not_found(chat_id)
        project_path = canonical_existing_path(chat.project_path)
        if not project_path or not is_allowed_path(project_path, self.config.allowed_roots):
            raise invalid_argument("Chat project path is outside the allowlist.", project_path=chat.project_path)
        status, confidence, evidence = self.catalog.infer_chat_status(chat)
        if status in {"running", "waiting_for_approval", "waiting_for_user", "waiting_for_user_input"} and confidence != "low":
            raise busy(chat.thread_id, status)
        prompt_submission_id = _optional_string(args.get("_prompt_submission_id"))
        dedup_metadata = args.get("_dedup_metadata") if isinstance(args.get("_dedup_metadata"), dict) else None
        if not bool(args.get("_skip_prompt_dedup")) and not prompt_submission_id:
            dedup_basis = str(args.get("_prompt_dedup_basis") or self._prompt_dedup_basis(str(args.get("_prompt_dedup_operation_type") or "send_message"), message))
            dedup = self._prepare_prompt_submission(
                project_id=chat.project_id,
                project_path_key=path_key(project_path),
                operation_type=str(args.get("_prompt_dedup_operation_type") or "send_message"),
                message=message,
                dedup_basis=dedup_basis,
                chat_id=chat.chat_id,
                thread_id=chat.thread_id,
                workflow_id=_optional_string(args.get("workflow_id")),
            )
            prompt_submission_id = _optional_string(dedup.get("promptSubmissionId"))
            if dedup.get("action") == "continue_existing_chat":
                existing_thread_id = _optional_string(dedup.get("existingThreadId"))
                existing_chat_id = _optional_string(dedup.get("existingChatId")) or existing_thread_id
                if not existing_thread_id or not existing_chat_id:
                    raise send_failed("Prompt duplicate has no resumable thread.", duplicateOfSubmissionId=dedup.get("duplicateOfSubmissionId"))
                return await self._send_message_resolved(
                    chat_id=existing_chat_id,
                    thread_id=existing_thread_id,
                    project_id=chat.project_id,
                    project_path=project_path,
                    message=message,
                    args=args,
                    prompt_submission_id=prompt_submission_id,
                    dedup_metadata=dedup,
                )
        elif dedup_metadata is None:
            dedup_metadata = args.get("_dedup_metadata") if isinstance(args.get("_dedup_metadata"), dict) else None
        message_preview = _redacted_preview(message)
        LOG.info(
            "send_message accepted_for_start chat_id=%s thread_id=%s status=%s confidence=%s timeout=%s approval_policy=%s sandbox_policy=%s message_chars=%d message_preview=%r",
            chat.chat_id,
            chat.thread_id,
            status,
            confidence,
            _bounded_int(args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS), 1, 7200),
            args.get("approval_policy") or self.config.default_approval_policy,
            args.get("sandbox") or self.config.default_sandbox_policy,
            len(message),
            message_preview,
        )
        return await self._send_message_resolved(
            chat_id=chat.chat_id,
            thread_id=chat.thread_id,
            project_id=chat.project_id,
            project_path=project_path,
            message=message,
            args=args,
            prompt_submission_id=prompt_submission_id,
            dedup_metadata=dedup_metadata,
        )

    async def codex_start_chat(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = _required_string(args, "project_id")
        message = _required_string(args, "message")
        project = self.catalog.get_project(project_id)
        if project is None:
            raise project_not_found(project_id)
        cwd = canonical_existing_path(args.get("cwd") or project.path)
        if not is_allowed_path(cwd, self.config.allowed_roots):
            raise invalid_argument("Requested cwd is outside the allowlist.", cwd=cwd)
        timeout_seconds = _bounded_int(args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS), 1, 7200)
        first_message_max_chars = _bounded_int(args.get("first_message_max_chars", 8000), 500, 200000)
        operation_id = _optional_string(args.get("_operation_id"))
        prompt_submission_id = _optional_string(args.get("_prompt_submission_id"))
        dedup_metadata = args.get("_dedup_metadata") if isinstance(args.get("_dedup_metadata"), dict) else None
        if not bool(args.get("_skip_prompt_dedup")) and not prompt_submission_id:
            dedup_basis = str(args.get("_prompt_dedup_basis") or self._prompt_dedup_basis(str(args.get("_prompt_dedup_operation_type") or "start_chat"), message))
            dedup = self._prepare_prompt_submission(
                project_id=project_id,
                project_path_key=path_key(cwd),
                operation_type=str(args.get("_prompt_dedup_operation_type") or "start_chat"),
                message=message,
                dedup_basis=dedup_basis,
            )
            prompt_submission_id = _optional_string(dedup.get("promptSubmissionId"))
            if dedup.get("action") == "continue_existing_chat":
                existing_thread_id = _optional_string(dedup.get("existingThreadId"))
                existing_chat_id = _optional_string(dedup.get("existingChatId")) or existing_thread_id
                if not existing_thread_id or not existing_chat_id:
                    raise send_failed("Prompt duplicate has no resumable thread.", duplicateOfSubmissionId=dedup.get("duplicateOfSubmissionId"))
                return await self._send_message_resolved(
                    chat_id=existing_chat_id,
                    thread_id=existing_thread_id,
                    project_id=project_id,
                    project_path=cwd,
                    message=message,
                    args=args,
                    prompt_submission_id=prompt_submission_id,
                    dedup_metadata=dedup,
                )
        LOG.info(
            "start_chat project_id=%s cwd=%s timeout=%s message_chars=%d message_preview=%r",
            project_id,
            cwd,
            timeout_seconds,
            len(message),
            _redacted_preview(message),
        )
        client = await self._app()
        sandbox_policy = _sandbox_policy(args.get("sandbox")) or self.config.default_sandbox_policy
        approval_policy = _approval_policy_for_start(args.get("approval_policy"), self.config.default_approval_policy)
        model = str(args.get("model")) if args.get("model") not in (None, "") else None
        collaboration_mode = _collaboration_mode(args.get("collaboration_mode"), model=model, config=self.config)
        try:
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="starting_thread", project_id=project_id, updated_at=_now_iso())
            thread = await client.thread_start(
                cwd=cwd,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                model=model,
                effort=self.config.default_effort,
                summary=self.config.default_summary,
                timeout_seconds=timeout_seconds,
            )
            thread_id = _extract_thread_id(thread)
            if not thread_id:
                if prompt_submission_id:
                    self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
                raise send_failed("thread/start did not return thread id")
            if operation_id:
                self.storage.update_operation(
                    operation_id,
                    status="starting_turn",
                    phase="starting_turn",
                    chat_id=thread_id,
                    thread_id=thread_id,
                    project_id=project_id,
                    cwd=cwd,
                    updated_at=_now_iso(),
                    app_server_generation=client.process_generation,
                )
            if prompt_submission_id:
                self.storage.update_prompt_submission(
                    prompt_submission_id,
                    status="starting_turn",
                    chat_id=thread_id,
                    thread_id=thread_id,
                    updated_at=_now_iso(),
                )
            if args.get("title"):
                await client.thread_name_set(thread_id, str(args["title"]))
            result = await client.turn_start(
                thread_id=thread_id,
                input_items=[{"type": "text", "text": message}],
                cwd=cwd,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                model=model,
                effort=self.config.default_effort,
                summary=self.config.default_summary,
                collaboration_mode=collaboration_mode,
                chat_id=thread_id,
                project_id=project_id,
                project_path=cwd,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
            raise
        turn_id = _extract_turn_id(result)
        if not turn_id:
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
            raise send_failed("turn/start did not return turn id")
        if operation_id:
            self.storage.update_operation(
                operation_id,
                status="running",
                phase="running",
                chat_id=thread_id,
                thread_id=thread_id,
                turn_id=turn_id,
                project_id=project_id,
                cwd=cwd,
                last_error=None,
                updated_at=_now_iso(),
                next_attempt_at=None,
                app_server_generation=result.get("_processGeneration") or client.process_generation,
            )
        if prompt_submission_id:
            self.storage.update_prompt_submission(
                prompt_submission_id,
                status="running",
                chat_id=thread_id,
                thread_id=thread_id,
                turn_id=turn_id,
                updated_at=_now_iso(),
            )
        status_payload = client.tracker.get_turn_status(turn_id, last_messages=10, message_max_chars=first_message_max_chars) or {}
        response = {
            "ok": True,
            "project_id": project_id,
            "projectId": project_id,
            "chat_id": thread_id,
            "chatId": thread_id,
            "thread_id": thread_id,
            "threadId": thread_id,
            "accepted": True,
            "turn_id": turn_id,
            "turnId": turn_id,
            "status": status_payload.get("status") or "running",
            "first_message": None,
            "firstMessage": {
                "role": None,
                "text": None,
                "createdAt": None,
                "truncated": False,
                "observed": False,
                "timedOut": False,
            },
            "first_message_observed": False,
            "first_message_timed_out": False,
            "first_message_truncated": False,
            "latestMessages": status_payload.get("latestMessages") or status_payload.get("last_messages") or [],
            "pollRecommended": True,
            "recommendedPollAfterSeconds": 5,
            "effectiveApprovalPolicy": approval_policy,
            "effectiveSandboxPolicy": sandbox_policy,
            "effectiveCollaborationMode": collaboration_mode,
            "effectiveCwd": cwd,
            "effectiveModel": model,
            "processGeneration": status_payload.get("processGeneration"),
            "appServerGeneration": status_payload.get("appServerGeneration") or status_payload.get("processGeneration"),
            "started_at": status_payload.get("started_at"),
            "startedAt": status_payload.get("startedAt") or status_payload.get("started_at"),
            "updated_at": status_payload.get("updated_at"),
            "updatedAt": status_payload.get("updatedAt") or status_payload.get("updated_at"),
        }
        response.update(self._dedup_metadata_for_result(dedup_metadata))
        return response

    async def codex_start_plan_workflow(self, args: dict[str, Any]) -> dict[str, Any]:
        client_request_id = _optional_string(args.get("client_request_id"))
        if client_request_id:
            existing = self.storage.get_workflow_by_client_request_id(client_request_id)
            if existing is not None:
                status = self._workflow_status_payload(
                    existing,
                    last_messages=10,
                    message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
                    include_events=True,
                )
                status["idempotent"] = True
                status["idempotencyScope"] = "start"
                return status

        project_id = _required_string(args, "project_id")
        workflow_id = "wf_" + uuid.uuid4().hex
        now = _now_iso()
        operation_args = {
            "operation_type": "start_chat",
            "project_id": project_id,
            "workflow_id": workflow_id,
            "message": _required_string(args, "message"),
            "title": _optional_string(args.get("title")),
            "cwd": _optional_string(args.get("cwd")),
            "model": _optional_string(args.get("model")),
            "sandbox": args.get("sandbox") or _sandbox_value_from_policy(self.config.default_sandbox_policy),
            "approval_policy": args.get("approval_policy") or self.config.default_approval_policy,
            "collaboration_mode": "plan",
            "client_request_id": f"workflow:{workflow_id}:plan",
            "timeout_seconds": args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS),
            "first_message_max_chars": args.get("first_message_max_chars", 8000),
        }
        start_operation = self.codex_submit_task(operation_args)
        plan_operation_id = str(start_operation.get("operationId") or "")
        if not plan_operation_id:
            raise send_failed("Plan workflow start did not create a durable plan operation.")

        self.storage.create_workflow(
            {
                "workflow_id": workflow_id,
                "workflow_kind": "plan_then_execute",
                "client_request_id": client_request_id,
                "execution_client_request_id": None,
                "current_operation_id": plan_operation_id,
                "plan_operation_id": plan_operation_id,
                "execution_operation_id": None,
                "project_id": project_id,
                "thread_id": _optional_string(start_operation.get("threadId")) or "",
                "plan_turn_id": _optional_string(start_operation.get("turnId")) or "",
                "execution_turn_id": None,
                "latest_plan_item_id": None,
                "latest_plan_hash": None,
                "latest_report_hash": None,
                "final_report_json": None,
                "phase": "planning",
                "status": "planning",
                "last_error": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "app_server_generation": start_operation.get("appServerGeneration"),
                "metadata_json": json.dumps(
                    {
                        "title": args.get("title"),
                        "startClientRequestId": client_request_id,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        self.storage.record_workflow_event(
            workflow_id,
            event_type="workflow_started",
            message="Plan workflow queued.",
            details={"planOperationId": plan_operation_id},
            created_at=now,
        )
        workflow = self.storage.get_workflow(workflow_id)
        assert workflow is not None
        status = self._workflow_status_payload(
            workflow,
            last_messages=10,
            message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
            include_events=True,
        )
        status["startOperation"] = start_operation
        status["idempotent"] = False
        return status

    def codex_get_workflow_status(self, args: dict[str, Any]) -> dict[str, Any]:
        workflow_id = _required_string(args, "workflow_id")
        workflow = self.storage.get_workflow(workflow_id)
        if workflow is None:
            raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
        return self._workflow_status_payload(
            workflow,
            last_messages=_bounded_int(args.get("last_messages", 10), 1, 50),
            message_max_chars=_bounded_int(args.get("message_max_chars", 8000), 500, 200000),
            include_events=bool(args.get("include_events", True)),
        )

    def codex_approve_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        workflow_id = _required_string(args, "workflow_id")
        workflow = self.storage.get_workflow(workflow_id)
        if workflow is None:
            raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
        workflow = self._sync_workflow_state(
            workflow,
            last_messages=10,
            message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
        )
        existing_operation_id = _optional_string(workflow.get("execution_operation_id"))
        if existing_operation_id:
            operation = self.storage.get_operation(existing_operation_id)
            if operation is not None:
                self._schedule_operation_if_needed(operation)
            status = self._workflow_status_payload(
                workflow,
                last_messages=10,
                message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
                include_events=True,
            )
            status["idempotent"] = True
            status["idempotencyScope"] = "approve"
            return status
        if _optional_string(workflow.get("execution_turn_id")):
            status = self._workflow_status_payload(
                workflow,
                last_messages=10,
                message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
                include_events=True,
            )
            status["idempotent"] = True
            status["idempotencyScope"] = "approve"
            return status

        plan_turn_id = _optional_string(workflow.get("plan_turn_id"))
        latest_plan = self.storage.get_latest_plan_for_turn(plan_turn_id) if plan_turn_id else None
        latest_plan_text = str((latest_plan or {}).get("text") or "").strip()
        if latest_plan is None or latest_plan.get("status") != "completed" or not latest_plan_text:
            raise invalid_argument(
                "Workflow has no completed Plan Mode plan item yet.",
                workflow_id=workflow_id,
                plan_turn_id=plan_turn_id,
            )

        plan_hash = prompt_hash(normalize_prompt(latest_plan_text))
        message = str(args.get("message") or "Implement the plan.")
        execution_client_request_id = (
            _optional_string(args.get("client_request_id"))
            or _optional_string(workflow.get("execution_client_request_id"))
            or f"workflow:{workflow_id}:execute"
        )
        operation = self.codex_submit_task(
            {
                "operation_type": "execute_plan",
                "workflow_id": workflow_id,
                "message": message,
                "client_request_id": execution_client_request_id,
                "approval_policy": args.get("approval_policy") or self.config.default_approval_policy,
                "sandbox": args.get("sandbox") or _sandbox_value_from_policy(self.config.default_sandbox_policy),
                "timeout_seconds": args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS),
                "first_message_max_chars": args.get("first_message_max_chars", 8000),
            }
        )
        operation_id = str(operation.get("operationId") or "")
        if not operation_id:
            raise send_failed("Plan approval did not create a durable execution operation.")
        now = _now_iso()
        self.storage.update_workflow(
            workflow_id,
            execution_client_request_id=execution_client_request_id,
            execution_operation_id=operation_id,
            current_operation_id=operation_id,
            latest_plan_item_id=str(latest_plan.get("item_id") or latest_plan.get("id") or ""),
            latest_plan_hash=plan_hash,
            phase="executing",
            status="executing",
            last_error=None,
            updated_at=now,
        )
        self.storage.record_workflow_event(
            workflow_id,
            event_type="plan_approved",
            message="Plan approved and execution operation queued.",
            details={"executionOperationId": operation_id, "latestPlanHash": plan_hash},
            created_at=now,
        )
        refreshed = self.storage.get_workflow(workflow_id) or workflow
        status = self._workflow_status_payload(
            refreshed,
            last_messages=10,
            message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
            include_events=True,
        )
        status["approveOperation"] = operation
        status["idempotent"] = False
        return status

    def codex_get_turn_status(self, args: dict[str, Any]) -> dict[str, Any]:
        turn_id = _required_string(args, "turn_id")
        thread_id = args.get("thread_id")
        thread_id = str(thread_id).strip() if thread_id not in (None, "") else None
        last_messages = _bounded_int(args.get("last_messages", 10), 1, 50)
        message_max_chars = _bounded_int(args.get("message_max_chars", 8000), 500, 200000)
        live = self._tracked_turn_status(turn_id, last_messages=last_messages, message_max_chars=message_max_chars)
        lookup_thread_id = thread_id or (str(live.get("thread_id")) if live and live.get("thread_id") else None)
        hook = self._hook_turn_status(turn_id, lookup_thread_id, last_messages=last_messages, message_max_chars=message_max_chars)
        kb = self._kb_turn_status(turn_id, lookup_thread_id, last_messages=last_messages, message_max_chars=message_max_chars)
        if live is None:
            if hook is not None:
                return hook
            if kb is None:
                raise turn_not_found(turn_id)
            return kb
        if hook is not None:
            live_status = str(live.get("status") or "unknown")
            hook_status = str(hook.get("status") or "unknown")
            if live_status in {"starting", "running", "unknown", "unknown_after_app_server_exit"} and hook_status in {"completed", "failed", "aborted", "cancelled", "canceled"}:
                hook["source"] = "app_server+hook_history"
                return hook
            if not live.get("last_messages") and hook.get("last_messages"):
                hook["source"] = "app_server+hook_history"
                return hook
        if kb is not None:
            live_status = str(live.get("status") or "unknown")
            kb_status = str(kb.get("status") or "unknown")
            if live_status in {"starting", "running", "unknown"} and kb_status in {"completed", "failed", "aborted", "cancelled", "canceled"}:
                kb["source"] = "app_server+kb_history"
                return kb
            if not live.get("last_messages") and kb.get("last_messages"):
                kb["source"] = "app_server+kb_history"
                return kb
        return live

    async def codex_execute_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        workflow_id = _optional_string(args.get("workflow_id"))
        if workflow_id and not args.get("_operation_id") and not args.get("_skip_prompt_dedup") and not bool(args.get("force", False)):
            return self.codex_approve_plan(args)
        workflow: dict[str, Any] | None = None
        if workflow_id:
            workflow = self.storage.get_workflow(workflow_id)
            if workflow is None:
                raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
            execution_turn_id = _optional_string(workflow.get("execution_turn_id"))
            if execution_turn_id:
                status = self._workflow_status_payload(
                    workflow,
                    last_messages=10,
                    message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
                    include_events=True,
                )
                status["idempotent"] = True
                status["idempotencyScope"] = "execute"
                return status
            latest_plan = self.storage.get_latest_plan_for_turn(str(workflow["plan_turn_id"]))
            force = bool(args.get("force", False))
            if not force and (latest_plan is None or latest_plan.get("status") != "completed" or not str(latest_plan.get("text") or "").strip()):
                raise invalid_argument(
                    "Workflow has no completed Plan Mode plan item yet.",
                    workflow_id=workflow_id,
                    plan_turn_id=workflow.get("plan_turn_id"),
                )

        message = args.get("message")
        if message in (None, ""):
            message = "Implement the plan."
        payload = dict(args)
        if workflow is not None:
            payload["chat_id"] = workflow["thread_id"]
            payload["project_id"] = workflow["project_id"]
            thread_id = _optional_string(workflow.get("thread_id"))
            project = self.catalog.get_project(str(workflow.get("project_id") or ""))
            project_path = canonical_existing_path(project.path) if project is not None else None
            if thread_id and project_path:
                payload["_resolved_thread_id"] = thread_id
                payload["_resolved_project_path"] = project_path
        elif not _optional_string(payload.get("chat_id")):
            raise invalid_argument("codex_execute_plan requires workflow_id or chat_id")
        payload["message"] = str(message)
        payload["collaboration_mode"] = "default"
        payload["_prompt_dedup_operation_type"] = "execute_plan"
        payload["_prompt_dedup_basis"] = self._prompt_dedup_basis("execute_plan", str(message), workflow=workflow)
        payload.pop("workflow_id", None)
        payload.pop("client_request_id", None)
        payload.pop("force", None)
        result = await self.codex_send_message(payload)
        result["plan_execution"] = True
        result["planExecution"] = True
        if workflow is not None:
            execution_turn_id = str(result.get("turnId") or "")
            now = _now_iso()
            self.storage.update_workflow(
                str(workflow["workflow_id"]),
                execution_client_request_id=_optional_string(args.get("client_request_id")),
                execution_turn_id=execution_turn_id or None,
                phase="executing",
                status="executing",
                last_error=None,
                updated_at=now,
                app_server_generation=result.get("processGeneration")
                or (self._app_server.process_generation if self._app_server is not None else workflow.get("app_server_generation")),
            )
            self.storage.record_workflow_event(
                str(workflow["workflow_id"]),
                event_type="plan_execution_started",
                message="Plan execution turn started.",
                details={"executionTurnId": execution_turn_id},
                created_at=now,
            )
            refreshed = self.storage.get_workflow(str(workflow["workflow_id"])) or workflow
            result["workflow"] = self._workflow_status_payload(
                refreshed,
                last_messages=10,
                message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
                include_events=True,
            )
            result["workflow_id"] = workflow["workflow_id"]
            result["workflowId"] = workflow["workflow_id"]
        return result

    def codex_submit_task(self, args: dict[str, Any]) -> dict[str, Any]:
        operation_type = _required_string(args, "operation_type")
        if operation_type not in {"start_chat", "send_message", "execute_plan"}:
            raise invalid_argument("Unsupported operation_type", operation_type=operation_type)
        message = _required_string(args, "message")
        explicit_client_request_id = _optional_string(args.get("client_request_id"))
        if explicit_client_request_id:
            existing = self.storage.get_operation_by_client_request_id(explicit_client_request_id)
            if existing is not None:
                self._schedule_operation_if_needed(existing)
                status = self._operation_status_payload(
                    existing,
                    last_messages=10,
                    message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
                )
                status["idempotent"] = True
                status["idempotencyScope"] = "operation"
                return status

        if operation_type == "start_chat" and not _optional_string(args.get("project_id")):
            raise invalid_argument("codex_submit_task start_chat requires project_id")
        if operation_type == "send_message" and not _optional_string(args.get("chat_id")):
            raise invalid_argument("codex_submit_task send_message requires chat_id")
        if operation_type == "execute_plan" and not (_optional_string(args.get("workflow_id")) or _optional_string(args.get("chat_id"))):
            raise invalid_argument("codex_submit_task execute_plan requires workflow_id or chat_id")

        now = _now_iso()
        operation_id = str(uuid.uuid4())
        actual_operation_type = operation_type
        actual_args = dict(args)
        actual_args.setdefault("approval_policy", self.config.default_approval_policy)
        if actual_args.get("approval_policy") in (None, ""):
            actual_args["approval_policy"] = self.config.default_approval_policy
        actual_args.setdefault("sandbox", _sandbox_value_from_policy(self.config.default_sandbox_policy))
        if actual_args.get("sandbox") in (None, ""):
            actual_args["sandbox"] = _sandbox_value_from_policy(self.config.default_sandbox_policy)
        workflow: dict[str, Any] | None = None
        initial_chat_id: str | None = _optional_string(args.get("chat_id"))
        initial_thread_id: str | None = None
        project_id: str | None = _optional_string(args.get("project_id"))
        project_path: str | None = None

        if operation_type == "start_chat":
            project = self.catalog.get_project(str(project_id))
            if project is None:
                raise project_not_found(str(project_id))
            project_path = canonical_existing_path(args.get("cwd") or project.path)
            if not is_allowed_path(project_path, self.config.allowed_roots):
                raise invalid_argument("Requested cwd is outside the allowlist.", cwd=project_path)
        elif operation_type == "send_message":
            chat = self.catalog.get_chat(str(initial_chat_id), project_id)
            if chat is None:
                raise thread_not_found(str(initial_chat_id))
            project_id = chat.project_id
            initial_chat_id = chat.chat_id
            initial_thread_id = chat.thread_id
            project_path = canonical_existing_path(chat.project_path)
            if not project_path or not is_allowed_path(project_path, self.config.allowed_roots):
                raise invalid_argument("Chat project path is outside the allowlist.", project_path=chat.project_path)
        else:
            workflow_id = _optional_string(args.get("workflow_id"))
            if workflow_id:
                workflow = self.storage.get_workflow(workflow_id)
                if workflow is None:
                    raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
                project_id = str(workflow.get("project_id") or "")
                initial_chat_id = str(workflow.get("thread_id") or "")
                initial_thread_id = initial_chat_id
            if initial_chat_id:
                chat = self.catalog.get_chat(initial_chat_id, project_id)
                if chat is not None:
                    project_id = chat.project_id
                    initial_chat_id = chat.chat_id
                    initial_thread_id = chat.thread_id
                    project_path = canonical_existing_path(chat.project_path)
            if not project_path:
                project = self.catalog.get_project(str(project_id)) if project_id else None
                if project is None:
                    raise project_not_found(str(project_id))
                project_path = canonical_existing_path(project.path)
            if not project_path or not is_allowed_path(project_path, self.config.allowed_roots):
                raise invalid_argument("Requested cwd is outside the allowlist.", cwd=project_path)
            if workflow is not None and initial_thread_id:
                actual_args["chat_id"] = initial_chat_id
                actual_args["project_id"] = project_id
                actual_args["_resolved_thread_id"] = initial_thread_id
                actual_args["_resolved_project_path"] = project_path

        dedup_basis = str(args.get("_prompt_dedup_basis") or self._prompt_dedup_basis(operation_type, message, workflow=workflow))
        dedup = self._prepare_prompt_submission(
            project_id=project_id,
            project_path_key=path_key(project_path),
            operation_type=operation_type,
            message=message,
            dedup_basis=dedup_basis,
            operation_id=operation_id,
            chat_id=initial_chat_id,
            thread_id=initial_thread_id,
            workflow_id=_optional_string(args.get("workflow_id")),
        )
        prompt_submission_id = _optional_string(dedup.get("promptSubmissionId"))
        dedup_metadata: dict[str, Any] | None = None
        if dedup.get("action") == "continue_existing_chat":
            actual_operation_type = "send_message"
            existing_chat_id = _optional_string(dedup.get("existingChatId")) or _optional_string(dedup.get("existingThreadId"))
            existing_thread_id = _optional_string(dedup.get("existingThreadId")) or existing_chat_id
            if not existing_chat_id or not existing_thread_id:
                if prompt_submission_id:
                    self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
                raise send_failed("Prompt duplicate has no resumable thread.", duplicateOfSubmissionId=dedup.get("duplicateOfSubmissionId"))
            active_turn = self._active_turn_for_thread(existing_thread_id)
            if active_turn is not None:
                if prompt_submission_id:
                    self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
                raise busy(existing_thread_id, str(active_turn.get("status") or "running"))
            dedup_metadata = dedup
            actual_args["chat_id"] = existing_chat_id
            actual_args["project_id"] = project_id
            actual_args["_resolved_thread_id"] = existing_thread_id
            actual_args["_resolved_project_path"] = project_path
            initial_chat_id = existing_chat_id
            initial_thread_id = existing_thread_id

        actual_args["_skip_prompt_dedup"] = True
        actual_args["_prompt_submission_id"] = prompt_submission_id
        if dedup_metadata is not None:
            actual_args["_dedup_metadata"] = dedup_metadata
            actual_args["original_operation_type"] = operation_type
        if operation_type == "execute_plan":
            actual_args["_prompt_dedup_operation_type"] = "execute_plan"
            actual_args["_prompt_dedup_basis"] = dedup_basis

        request_payload = _operation_request_payload(actual_args, operation_type=actual_operation_type, message=message)
        request_payload["_skip_prompt_dedup"] = True
        request_payload["_prompt_submission_id"] = prompt_submission_id
        if actual_args.get("_resolved_thread_id"):
            request_payload["_resolved_thread_id"] = actual_args.get("_resolved_thread_id")
        if actual_args.get("_resolved_project_path"):
            request_payload["_resolved_project_path"] = actual_args.get("_resolved_project_path")
        if dedup_metadata is not None:
            request_payload["_dedup_metadata"] = dedup_metadata
            request_payload["original_operation_type"] = operation_type
        if operation_type == "execute_plan":
            request_payload["_prompt_dedup_operation_type"] = "execute_plan"
            request_payload["_prompt_dedup_basis"] = dedup_basis
        request_payload["_operation_id"] = operation_id
        client_request_id = explicit_client_request_id or f"{_operation_client_request_id(request_payload)}:{uuid.uuid4().hex[:8]}"
        row = {
            "operation_id": operation_id,
            "client_request_id": client_request_id,
            "operation_type": actual_operation_type,
            "status": "queued",
            "phase": "queued",
            "project_id": project_id,
            "chat_id": initial_chat_id,
            "thread_id": initial_thread_id,
            "turn_id": None,
            "workflow_id": _optional_string(args.get("workflow_id")),
            "cwd": project_path,
            "title": _optional_string(args.get("title")),
            "request_json": json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
            "result_json": None,
            "last_error": None,
            "attempt_count": 0,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "app_server_generation": None,
        }
        created = self.storage.create_operation(row)
        operation = self.storage.get_operation(operation_id) if created else self.storage.get_operation_by_client_request_id(client_request_id)
        if operation is None:
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
            raise send_failed("Failed to create Codex operation.")
        self._schedule_operation_if_needed(operation)
        status = self._operation_status_payload(operation, last_messages=10, message_max_chars=8000)
        status["idempotent"] = False
        status.update(self._dedup_metadata_for_result(dedup_metadata))
        return status

    def codex_get_operation_status(self, args: dict[str, Any]) -> dict[str, Any]:
        operation_id = _required_string(args, "operation_id")
        operation = self.storage.get_operation(operation_id)
        if operation is None:
            raise invalid_argument("Codex operation was not found.", operation_id=operation_id)
        self._schedule_recoverable_operations()
        self._schedule_operation_if_needed(operation)
        return self._operation_status_payload(
            operation,
            last_messages=_bounded_int(args.get("last_messages", 10), 1, 50),
            message_max_chars=_bounded_int(args.get("message_max_chars", 8000), 500, 200000),
            include_events=bool(args.get("include_events", False)),
        )

    def _schedule_operation_if_needed(self, operation: dict[str, Any]) -> None:
        operation_id = str(operation.get("operation_id") or "")
        if not operation_id:
            return
        if str(operation.get("status") or "") not in OPERATION_STARTABLE_STATUSES:
            return
        task = self._operation_tasks.get(operation_id)
        if task is not None and not task.done():
            return
        if int(operation.get("attempt_count") or 0) >= int(operation.get("max_attempts") or 3) and not operation.get("turn_id"):
            self.storage.mark_operation_failed_if_attempts_exhausted(
                operation_id,
                updated_at=_now_iso(),
                message="Operation exhausted max attempts before acquiring a worker lease.",
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._run_operation(operation_id), name=f"codex-operation-{operation_id}")
        task.add_done_callback(lambda _task, _operation_id=operation_id: self._operation_tasks.pop(_operation_id, None))
        self._operation_tasks[operation_id] = task

    def _schedule_recoverable_operations(self, *, limit: int = 20) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for operation in self.storage.list_startable_operations(now=_now_iso(), limit=limit):
            self._schedule_operation_if_needed(operation)

    async def _operation_heartbeat_loop(self, operation_id: str) -> None:
        while True:
            await asyncio.sleep(OPERATION_HEARTBEAT_SECONDS)
            ok = self.storage.heartbeat_operation_lease(
                operation_id,
                lease_owner=self._worker_owner,
                now=_now_iso(),
                lease_expires_at=_future_iso(OPERATION_LEASE_TTL_SECONDS),
            )
            if not ok:
                return

    async def _run_operation(self, operation_id: str) -> None:
        operation = self.storage.acquire_operation_lease(
            operation_id,
            lease_owner=self._worker_owner,
            now=_now_iso(),
            lease_expires_at=_future_iso(OPERATION_LEASE_TTL_SECONDS),
        )
        if operation is None:
            self.storage.mark_operation_failed_if_attempts_exhausted(
                operation_id,
                updated_at=_now_iso(),
                message="Operation exhausted max attempts before acquiring a worker lease.",
            )
            return
        status = str(operation.get("status") or "")
        if status not in OPERATION_STARTABLE_STATUSES:
            self.storage.release_operation_lease(operation_id, lease_owner=self._worker_owner, updated_at=_now_iso())
            return
        if _optional_string(operation.get("turn_id")):
            self.storage.update_operation(
                operation_id,
                status="running",
                phase="running",
                next_attempt_at=None,
                updated_at=_now_iso(),
            )
            self.storage.update_prompt_submission_by_operation(operation_id, status="running", updated_at=_now_iso())
            self.storage.release_operation_lease(operation_id, lease_owner=self._worker_owner, updated_at=_now_iso())
            return
        now = _now_iso()
        self.storage.increment_operation_attempt(operation_id, started_at=now, updated_at=now)
        operation = self.storage.get_operation(operation_id) or operation
        payload: dict[str, Any] = {}
        prompt_submission_id: str | None = None
        heartbeat_task = asyncio.create_task(self._operation_heartbeat_loop(operation_id), name=f"codex-operation-heartbeat-{operation_id}")
        try:
            payload = _operation_request_from_row(operation)
            payload["_operation_id"] = operation_id
            prompt_submission_id = _optional_string(payload.get("_prompt_submission_id"))
            operation_type = str(operation.get("operation_type") or "")
            if operation_type == "start_chat" and _optional_string(operation.get("thread_id")):
                operation_type = "send_message"
                payload["chat_id"] = operation.get("thread_id")
                payload["_resolved_thread_id"] = operation.get("thread_id")
                if operation.get("cwd"):
                    payload["_resolved_project_path"] = operation.get("cwd")
            LOG.info("operation run start operation_id=%s type=%s", operation_id, operation_type)
            self.storage.update_operation(operation_id, status="starting_app_server", phase="starting_app_server", updated_at=_now_iso())
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="starting_app_server", updated_at=_now_iso())
            await self._app()
            self.storage.update_operation(operation_id, status="starting_thread", phase="starting_thread", updated_at=_now_iso())
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="starting_thread", updated_at=_now_iso())
            if operation_type == "start_chat":
                result = await self.codex_start_chat(_operation_tool_args(payload))
            elif operation_type == "send_message":
                result = await self.codex_send_message(_operation_tool_args(payload))
            elif operation_type == "execute_plan":
                result = await self.codex_execute_plan(_operation_tool_args(payload))
            else:
                raise invalid_argument("Unsupported operation_type", operation_type=operation_type)
            thread_id = _optional_string(result.get("threadId")) or _optional_string(result.get("thread_id"))
            turn_id = _optional_string(result.get("turnId")) or _optional_string(result.get("turn_id"))
            chat_id = _optional_string(result.get("chatId")) or _optional_string(result.get("chat_id")) or thread_id
            workflow = result.get("workflow") if isinstance(result.get("workflow"), dict) else None
            workflow_id = _optional_string(result.get("workflowId")) or _optional_string(result.get("workflow_id")) or _optional_string((workflow or {}).get("workflowId"))
            self.storage.update_operation(
                operation_id,
                status="running",
                phase="running",
                chat_id=chat_id or operation.get("chat_id"),
                thread_id=thread_id or operation.get("thread_id"),
                turn_id=turn_id or operation.get("turn_id"),
                workflow_id=workflow_id or operation.get("workflow_id"),
                result_json=json.dumps(result, ensure_ascii=False),
                last_error=None,
                updated_at=_now_iso(),
                next_attempt_at=None,
                app_server_generation=result.get("appServerGeneration") or result.get("processGeneration") or operation.get("app_server_generation"),
            )
            self.storage.update_prompt_submission_by_operation(
                operation_id,
                status="running",
                chat_id=chat_id or operation.get("chat_id"),
                thread_id=thread_id or operation.get("thread_id"),
                turn_id=turn_id or operation.get("turn_id"),
                workflow_id=workflow_id or operation.get("workflow_id"),
                updated_at=_now_iso(),
            )
            workflow_id_for_update = _optional_string(workflow_id) or _optional_string(operation.get("workflow_id"))
            if workflow_id_for_update and operation_type == "start_chat" and thread_id and turn_id:
                workflow_row = self.storage.get_workflow(workflow_id_for_update)
                if workflow_row is not None:
                    self.storage.update_workflow(
                        workflow_id_for_update,
                        thread_id=thread_id,
                        plan_turn_id=turn_id,
                        plan_operation_id=_optional_string(workflow_row.get("plan_operation_id")) or operation_id,
                        current_operation_id=_optional_string(workflow_row.get("current_operation_id")) or operation_id,
                        phase="planning",
                        status="planning",
                        last_error=None,
                        updated_at=_now_iso(),
                        app_server_generation=result.get("appServerGeneration") or result.get("processGeneration"),
                    )
            if workflow_id_for_update and operation_type == "execute_plan" and turn_id:
                workflow_row = self.storage.get_workflow(workflow_id_for_update)
                if workflow_row is not None:
                    self.storage.update_workflow(
                        workflow_id_for_update,
                        execution_turn_id=turn_id,
                        execution_operation_id=_optional_string(workflow_row.get("execution_operation_id")) or operation_id,
                        current_operation_id=operation_id,
                        phase="executing",
                        status="executing",
                        last_error=None,
                        updated_at=_now_iso(),
                        app_server_generation=result.get("appServerGeneration") or result.get("processGeneration"),
                    )
            original_operation_type = _optional_string(payload.get("original_operation_type"))
            if original_operation_type == "execute_plan" and operation.get("workflow_id") and turn_id:
                workflow_id_for_update = str(operation.get("workflow_id"))
                workflow = self.storage.get_workflow(workflow_id_for_update)
                if workflow is not None and not _optional_string(workflow.get("execution_turn_id")):
                    self.storage.update_workflow(
                        workflow_id_for_update,
                        execution_turn_id=turn_id,
                        execution_operation_id=_optional_string(workflow.get("execution_operation_id")) or operation_id,
                        current_operation_id=operation_id,
                        phase="executing",
                        status="executing",
                        last_error=None,
                        updated_at=_now_iso(),
                        app_server_generation=result.get("appServerGeneration") or result.get("processGeneration"),
                    )
            LOG.info("operation run accepted operation_id=%s thread_id=%s turn_id=%s", operation_id, thread_id, turn_id)
        except asyncio.CancelledError:
            current = self.storage.get_operation(operation_id) or operation
            if _optional_string(current.get("turn_id")):
                self.storage.update_operation(
                    operation_id,
                    status="running",
                    phase="running",
                    last_error="MCP server shut down after turn/start; poll tracked turn status.",
                    updated_at=_now_iso(),
                    next_attempt_at=None,
                )
                self.storage.update_prompt_submission_by_operation(operation_id, status="running", updated_at=_now_iso())
            else:
                self.storage.update_operation(
                    operation_id,
                    status="queued",
                    phase="queued",
                    last_error="MCP server shut down before turn/start; operation will be retried.",
                    updated_at=_now_iso(),
                )
                self.storage.update_prompt_submission_by_operation(operation_id, status="queued", updated_at=_now_iso())
            self.storage.release_operation_lease(operation_id, lease_owner=self._worker_owner, updated_at=_now_iso())
            raise
        except Exception as exc:
            LOG.exception("operation run failed operation_id=%s", operation_id)
            current = self.storage.get_operation(operation_id) or operation
            if _optional_string(current.get("turn_id")):
                self.storage.update_operation(
                    operation_id,
                    status="running",
                    phase="running",
                    last_error=f"Worker failed after turn/start; poll tracked turn status: {exc}",
                    updated_at=_now_iso(),
                    next_attempt_at=None,
                )
                self.storage.update_prompt_submission_by_operation(operation_id, status="running", updated_at=_now_iso())
            else:
                attempt_count = int(current.get("attempt_count") or 0)
                max_attempts = int(current.get("max_attempts") or 3)
                if attempt_count < max_attempts:
                    retry_after = min(60, max(5, attempt_count * 5))
                    self.storage.update_operation(
                        operation_id,
                        status="queued",
                        phase="queued",
                        last_error=str(exc),
                        updated_at=_now_iso(),
                        next_attempt_at=_future_iso(retry_after),
                    )
                    self.storage.update_prompt_submission_by_operation(operation_id, status="queued", updated_at=_now_iso())
                else:
                    self.storage.update_operation(
                        operation_id,
                        status="failed",
                        phase="failed",
                        last_error=str(exc),
                        updated_at=_now_iso(),
                        completed_at=_now_iso(),
                    )
                    self.storage.update_prompt_submission_by_operation(operation_id, status="failed", updated_at=_now_iso())
            self.storage.release_operation_lease(operation_id, lease_owner=self._worker_owner, updated_at=_now_iso())
        finally:
            heartbeat_task.cancel()
            with suppress(BaseException):
                await heartbeat_task
            self.storage.release_operation_lease(operation_id, lease_owner=self._worker_owner, updated_at=_now_iso())
            task = self._operation_tasks.get(operation_id)
            if task is not None and task.done():
                self._operation_tasks.pop(operation_id, None)

    def _operation_status_payload(
        self,
        operation: dict[str, Any],
        *,
        last_messages: int,
        message_max_chars: int,
        include_events: bool = False,
    ) -> dict[str, Any]:
        operation_id = str(operation.get("operation_id") or "")
        latest = self.storage.get_operation(operation_id) or operation
        turn_id = _optional_string(latest.get("turn_id"))
        thread_id = _optional_string(latest.get("thread_id"))
        turn_status: dict[str, Any] | None = None
        if turn_id:
            try:
                turn_status = self.codex_get_turn_status(
                    {
                        "turn_id": turn_id,
                        "thread_id": thread_id,
                        "last_messages": last_messages,
                        "message_max_chars": message_max_chars,
                    }
                )
                latest = self._reconcile_operation_with_turn(latest, turn_status)
            except CodexMcpError:
                turn_status = None
        response = _operation_row_to_tool(latest)
        response["operation_source"] = "durable_queue"
        response["operationSource"] = "durable_queue"
        response["lease_state"] = self._operation_lease_state(latest)
        response["leaseState"] = response["lease_state"]
        prompt_submission = self.storage.get_prompt_submission_by_operation(operation_id)
        if prompt_submission is not None:
            response["promptSubmissionId"] = prompt_submission.get("prompt_submission_id")
            response["duplicateOfSubmissionId"] = prompt_submission.get("duplicate_of_submission_id")
            if prompt_submission.get("similarity") is not None:
                response["similarity"] = round(float(prompt_submission.get("similarity") or 0), 4)
            if prompt_submission.get("duplicate_of_submission_id"):
                response["deduplicated"] = True
                response.setdefault("dedupAction", "continued_existing_chat")
            response["dedupState"] = {
                "state": "duplicate_continuation" if prompt_submission.get("duplicate_of_submission_id") else "new",
                "promptSubmissionId": prompt_submission.get("prompt_submission_id"),
                "status": prompt_submission.get("status"),
                "deduplicated": bool(prompt_submission.get("duplicate_of_submission_id")),
                "duplicateOfSubmissionId": prompt_submission.get("duplicate_of_submission_id"),
                "similarity": round(float(prompt_submission.get("similarity") or 0), 4)
                if prompt_submission.get("similarity") is not None
                else None,
            }
        else:
            response["dedupState"] = {"state": "not_recorded", "deduplicated": False}
        response["turnStatus"] = turn_status
        response["latestMessages"] = (turn_status or {}).get("latestMessages") or (turn_status or {}).get("last_messages") or []
        pending_interactions = self._pending_interactions_for_context(thread_id=thread_id, turn_id=turn_id, status="pending", limit=20)
        if not pending_interactions:
            pending_interactions = (turn_status or {}).get("pendingInteractions") or []
        response["pendingInteractions"] = pending_interactions
        if pending_interactions and str(response.get("status") or "") not in OPERATION_TERMINAL_STATUSES:
            waiting_status = "waiting_for_user_input" if any(item.get("kind") == "user_input" for item in pending_interactions) else "waiting_for_approval"
            response["status"] = waiting_status
            response["phase"] = waiting_status
            if str(latest.get("status") or "") != waiting_status:
                self.storage.update_operation(operation_id, status=waiting_status, phase=waiting_status, updated_at=_now_iso())
        response["source"] = "live" if turn_status and turn_status.get("source") == "live" else "storage"
        response["stalenessSeconds"] = _staleness_seconds(str(latest.get("updated_at") or ""))
        response["nextRecommendedAction"] = _operation_next_action(response)
        response["recommendedPollAfterSeconds"] = _operation_poll_after(response)
        response["pollRecommended"] = response["status"] not in OPERATION_TERMINAL_STATUSES
        if include_events and (thread_id or turn_id):
            response["events"] = [
                event_to_tool(row, include_payload=False)
                for row in self.storage.list_app_server_events(thread_id=thread_id, turn_id=turn_id, limit=50)
            ]
        return response

    def _operation_lease_state(self, operation: dict[str, Any]) -> dict[str, Any]:
        owner = _optional_string(operation.get("lease_owner"))
        expires_at = _optional_string(operation.get("lease_expires_at"))
        last_heartbeat_at = _optional_string(operation.get("last_heartbeat_at"))
        if not owner:
            state = "none"
        else:
            expires = _parse_iso_datetime(expires_at)
            if expires is not None and expires <= datetime.now(timezone.utc):
                state = "expired"
            elif owner == self._worker_owner:
                state = "owned_by_this_worker"
            else:
                state = "owned_by_other_worker"
        return {
            "state": state,
            "leaseOwner": owner,
            "leaseExpiresAt": expires_at,
            "lastHeartbeatAt": last_heartbeat_at,
            "nextAttemptAt": operation.get("next_attempt_at"),
            "maxAttempts": operation.get("max_attempts"),
        }

    def _reconcile_operation_with_turn(self, operation: dict[str, Any], turn_status: dict[str, Any]) -> dict[str, Any]:
        turn_state = str(turn_status.get("status") or "")
        current_status = str(operation.get("status") or "")
        next_status = current_status
        next_phase = str(operation.get("phase") or current_status or "unknown")
        completed_at = operation.get("completed_at")
        last_error = operation.get("last_error")
        if turn_state in {"completed"}:
            next_status = "completed"
            next_phase = "completed"
            completed_at = completed_at or _now_iso()
        elif turn_state in {"failed", "aborted", "cancelled", "canceled", "interrupted"}:
            next_status = "failed" if turn_state == "failed" else turn_state
            next_phase = next_status
            completed_at = completed_at or _now_iso()
            last_error = turn_status.get("last_error") or last_error
        elif turn_state == "unknown_after_app_server_exit":
            next_status = "unknown_after_app_server_exit"
            next_phase = "unknown_after_app_server_exit"
            completed_at = completed_at or _now_iso()
            last_error = turn_status.get("lastError") or turn_status.get("last_error") or last_error
        elif turn_state in {"waiting_for_approval", "waiting_for_user_input"}:
            next_status = turn_state
            next_phase = turn_state
        elif turn_state:
            next_status = "running" if current_status not in OPERATION_STARTABLE_STATUSES else current_status
            next_phase = "running" if next_status == "running" else next_phase
        if next_status != current_status or next_phase != operation.get("phase") or completed_at != operation.get("completed_at") or last_error != operation.get("last_error"):
            self.storage.update_operation(
                str(operation["operation_id"]),
                status=next_status,
                phase=next_phase,
                completed_at=completed_at,
                last_error=last_error,
                updated_at=_now_iso(),
            )
            self.storage.update_prompt_submission_by_operation(
                str(operation["operation_id"]),
                status=next_status,
                updated_at=_now_iso(),
            )
            return self.storage.get_operation(str(operation["operation_id"])) or operation
        return operation

    def _tracked_turn_status(self, turn_id: str, *, last_messages: int, message_max_chars: int) -> dict[str, Any] | None:
        tracker = self._app_server.tracker if self._app_server is not None else None
        if tracker is not None:
            status = tracker.get_turn_status(turn_id, last_messages=last_messages, message_max_chars=message_max_chars)
            if status is not None:
                process = getattr(self._app_server, "process", None)
                running = process is not None and getattr(process, "returncode", None) is None
                status["source"] = "live" if running else "storage"
                status["appServerGeneration"] = getattr(self._app_server, "process_generation", status.get("processGeneration"))
            return status
        turn = self.storage.get_tracked_turn(turn_id)
        if turn is None:
            return None
        messages = [
            {
                "role": message.get("role"),
                "created_at": message.get("created_at"),
                "text": _truncate_text(message.get("text"), message_max_chars)[0],
            }
            for message in self.storage.get_last_tracked_turn_messages(turn_id, last_messages)
        ]
        completion_observed = turn["status"] in {"completed", "failed", "aborted", "cancelled", "canceled", "interrupted"}
        last_error = _tracked_turn_last_error(turn)
        plans = [_plan_row_to_tool(row, message_max_chars) for row in self.storage.get_tracked_turn_plans(turn_id)]
        latest_plan = _latest_plan(plans)
        return {
            "ok": True,
            "thread_id": turn["thread_id"],
            "threadId": turn["thread_id"],
            "turn_id": turn["turn_id"],
            "turnId": turn["turn_id"],
            "chat_id": turn.get("chat_id"),
            "chatId": turn.get("chat_id"),
            "project_id": turn.get("project_id"),
            "projectId": turn.get("project_id"),
            "status": turn["status"],
            "completion_observed": completion_observed,
            "completionObserved": completion_observed,
            "started_at": turn.get("started_at"),
            "startedAt": turn.get("started_at"),
            "updated_at": turn.get("updated_at"),
            "updatedAt": turn.get("updated_at"),
            "completed_at": turn.get("completed_at"),
            "completedAt": turn.get("completed_at"),
            "last_messages": messages,
            "latestMessages": messages,
            "hasMore": False,
            "lastEventSeq": turn.get("last_event_seq") or 0,
            "requestId": turn.get("request_id"),
            "processGeneration": turn.get("process_generation"),
            "lastError": last_error,
            "final_message": _truncate_text(turn.get("final_message"), message_max_chars)[0],
            "finalMessage": _truncate_text(turn.get("final_message"), message_max_chars)[0],
            "plans": plans,
            "latestPlan": latest_plan,
            "pending_interactions": [
                _pending_interaction_summary(row)
                for row in self.storage.list_pending_interactions(turn_id=turn_id, status="pending", limit=20)
            ],
            "pendingInteractions": [
                _pending_interaction_summary(row)
                for row in self.storage.list_pending_interactions(turn_id=turn_id, status="pending", limit=20)
            ],
            "source": "storage",
            "appServerGeneration": turn.get("process_generation"),
            "stalenessSeconds": _min_staleness([turn.get("updated_at")]),
        }

    def _hook_turn_status(
        self,
        turn_id: str,
        thread_id: str | None,
        *,
        last_messages: int,
        message_max_chars: int,
    ) -> dict[str, Any] | None:
        hook_uri = self.catalog.hook_history.locate_thread(thread_id) if thread_id else None
        if hook_uri is None:
            hook_uri = self.catalog.hook_history.locate_turn_thread(turn_id)
        if hook_uri is None:
            return None
        hook_thread_id = self.catalog.hook_history.thread_id_from_uri(hook_uri)
        if not hook_thread_id:
            return None
        summary = self.catalog.hook_history.parse_thread(hook_thread_id)
        turn = summary.turns.get(turn_id)
        if turn is None:
            return None
        messages = [
            {
                "role": message.role,
                "created_at": message.created_at,
                "text": _truncate_text(message.text, message_max_chars)[0],
            }
            for message in summary.messages
            if message.turn_id == turn_id and message.role == "assistant"
        ][-last_messages:]
        chat = self.catalog.get_chat(summary.thread_id or hook_thread_id)
        final_message = _truncate_text(turns_last_assistant(summary.messages, turn_id), message_max_chars)[0]
        completion_observed = turn.status in {"completed", "failed", "aborted", "cancelled", "canceled", "interrupted"}
        plans = [_plan_row_to_tool(row, message_max_chars) for row in self.storage.get_tracked_turn_plans(turn_id)]
        latest_plan = _latest_plan(plans)
        return {
            "ok": True,
            "thread_id": summary.thread_id or hook_thread_id,
            "threadId": summary.thread_id or hook_thread_id,
            "turn_id": turn_id,
            "turnId": turn_id,
            "chat_id": (chat.chat_id if chat else (summary.thread_id or hook_thread_id)),
            "chatId": (chat.chat_id if chat else (summary.thread_id or hook_thread_id)),
            "project_id": chat.project_id if chat else None,
            "projectId": chat.project_id if chat else None,
            "status": turn.status,
            "completion_observed": completion_observed,
            "completionObserved": completion_observed,
            "started_at": turn.started_at,
            "startedAt": turn.started_at,
            "updated_at": turn.completed_at or summary.updated_at,
            "updatedAt": turn.completed_at or summary.updated_at,
            "completed_at": turn.completed_at,
            "completedAt": turn.completed_at,
            "last_messages": messages,
            "latestMessages": messages,
            "hasMore": self.storage.count_hook_messages(turn_id=turn_id) > len(messages),
            "lastEventSeq": None,
            "final_message": final_message,
            "finalMessage": final_message,
            "plans": plans,
            "latestPlan": latest_plan,
            "pending_interactions": [],
            "pendingInteractions": [],
            "source": "hook_history",
            "appServerGeneration": None,
            "stalenessSeconds": _min_staleness([turn.completed_at, summary.updated_at]),
        }

    def _kb_turn_status(
        self,
        turn_id: str,
        thread_id: str | None,
        *,
        last_messages: int,
        message_max_chars: int,
    ) -> dict[str, Any] | None:
        thread_dir = self.catalog.kb_history.locate_thread_dir(thread_id) if thread_id else None
        if thread_dir is None:
            matches = list(self.catalog.kb_history.root.glob(f"*/threads/*/{turn_id}.json"))
            if matches:
                thread_dir = matches[0].parent
        if thread_dir is None:
            return None
        summary = self.catalog.kb_history.parse_thread_dir(
            thread_dir,
            include_tool_calls=False,
            include_tool_outputs=False,
            include_command_outputs=False,
            include_reasoning=False,
        )
        turn = summary.turns.get(turn_id)
        if turn is None:
            return None
        messages = [
            {
                "role": message.role,
                "created_at": message.created_at,
                "text": _truncate_text(message.text, message_max_chars)[0],
            }
            for message in summary.messages
            if message.turn_id == turn_id and message.role == "assistant"
        ][-last_messages:]
        chat = self.catalog.get_chat(summary.thread_id or thread_dir.name)
        final_message = messages[-1]["text"] if messages else None
        completion_observed = turn.status in {"completed", "failed", "aborted", "cancelled", "canceled", "interrupted"}
        plans = [_plan_row_to_tool(row, message_max_chars) for row in self.storage.get_tracked_turn_plans(turn_id)]
        latest_plan = _latest_plan(plans)
        return {
            "ok": True,
            "thread_id": summary.thread_id or thread_dir.name,
            "threadId": summary.thread_id or thread_dir.name,
            "turn_id": turn_id,
            "turnId": turn_id,
            "chat_id": (chat.chat_id if chat else (summary.thread_id or thread_dir.name)),
            "chatId": (chat.chat_id if chat else (summary.thread_id or thread_dir.name)),
            "project_id": chat.project_id if chat else None,
            "projectId": chat.project_id if chat else None,
            "status": turn.status,
            "completion_observed": completion_observed,
            "completionObserved": completion_observed,
            "started_at": turn.started_at,
            "startedAt": turn.started_at,
            "updated_at": turn.completed_at or summary.updated_at,
            "updatedAt": turn.completed_at or summary.updated_at,
            "completed_at": turn.completed_at,
            "completedAt": turn.completed_at,
            "last_messages": messages,
            "latestMessages": messages,
            "hasMore": False,
            "lastEventSeq": None,
            "final_message": final_message,
            "finalMessage": final_message,
            "plans": plans,
            "latestPlan": latest_plan,
            "pending_interactions": [],
            "pendingInteractions": [],
            "source": "kb_history",
            "appServerGeneration": None,
            "stalenessSeconds": _min_staleness([turn.completed_at, summary.updated_at]),
        }

    def _workflow_status_payload(
        self,
        workflow: dict[str, Any],
        *,
        last_messages: int,
        message_max_chars: int,
        include_events: bool,
    ) -> dict[str, Any]:
        workflow = self._sync_workflow_state(workflow, last_messages=last_messages, message_max_chars=message_max_chars)
        workflow_id = str(workflow["workflow_id"])
        workflow_kind = _optional_string(workflow.get("workflow_kind")) or "plan_then_execute"
        thread_id = _optional_string(workflow.get("thread_id"))
        plan_turn_id = _optional_string(workflow.get("plan_turn_id"))
        execution_turn_id = _optional_string(workflow.get("execution_turn_id"))
        plan_operation_id = _optional_string(workflow.get("plan_operation_id"))
        execution_operation_id = _optional_string(workflow.get("execution_operation_id"))
        current_operation_id = _optional_string(workflow.get("current_operation_id"))
        plan_operation_row = self.storage.get_operation(plan_operation_id) if plan_operation_id else None
        execution_operation_row = self.storage.get_operation(execution_operation_id) if execution_operation_id else None
        plan_operation = (
            self._operation_status_payload(plan_operation_row, last_messages=last_messages, message_max_chars=message_max_chars)
            if plan_operation_row is not None
            else None
        )
        execution_operation = (
            self._operation_status_payload(execution_operation_row, last_messages=last_messages, message_max_chars=message_max_chars)
            if execution_operation_row is not None
            else None
        )
        plan_turn = (
            self._turn_status_or_none(plan_turn_id, thread_id, last_messages=last_messages, message_max_chars=message_max_chars)
            if plan_turn_id
            else None
        )
        execution_turn = (
            self._turn_status_or_none(execution_turn_id, thread_id, last_messages=last_messages, message_max_chars=message_max_chars)
            if execution_turn_id
            else None
        )
        plans = [_plan_row_to_tool(row, message_max_chars) for row in self.storage.get_tracked_turn_plans(plan_turn_id)] if plan_turn_id else []
        latest_plan = _latest_plan(plans)
        plan_updates: dict[str, Any] = {}
        if latest_plan is not None:
            latest_plan_text = str(latest_plan.get("markdown") or "")
            latest_plan_hash = prompt_hash(normalize_prompt(latest_plan_text)) if latest_plan_text.strip() else None
            latest_plan_item_id = _optional_string(latest_plan.get("itemId")) or _optional_string(latest_plan.get("item_id"))
            if latest_plan_hash and latest_plan_hash != workflow.get("latest_plan_hash"):
                plan_updates["latest_plan_hash"] = latest_plan_hash
            if latest_plan_item_id and latest_plan_item_id != workflow.get("latest_plan_item_id"):
                plan_updates["latest_plan_item_id"] = latest_plan_item_id
        if plan_updates:
            plan_updates["updated_at"] = _now_iso()
            self.storage.update_workflow(workflow_id, **plan_updates)
            workflow = self.storage.get_workflow(workflow_id) or workflow

        pending_interactions = self._pending_interactions_for_context(thread_id=thread_id, turn_id=None, status="pending", limit=20) if thread_id else []
        final_report = self._workflow_final_report(
            workflow,
            execution_turn=execution_turn,
            message_max_chars=message_max_chars,
        )
        workflow = self.storage.get_workflow(workflow_id) or workflow
        phase, status, last_error = _derive_workflow_phase(
            workflow,
            plan_turn=plan_turn,
            execution_turn=execution_turn,
            latest_plan=latest_plan,
            pending_interactions=pending_interactions,
            plan_operation=plan_operation,
            execution_operation=execution_operation,
        )
        now = _now_iso()
        completed_at = workflow.get("completed_at")
        if status in {"completed", "failed", "orphaned_after_app_server_exit"} and not completed_at:
            completed_at = now
        if phase != workflow.get("phase") or status != workflow.get("status") or last_error != workflow.get("last_error"):
            self.storage.update_workflow(
                workflow_id,
                phase=phase,
                status=status,
                last_error=last_error,
                updated_at=now,
                completed_at=completed_at,
                current_operation_id=execution_operation_id if phase in {"executing", "completed"} and execution_operation_id else current_operation_id,
                app_server_generation=self._app_server.process_generation
                if self._app_server is not None
                else workflow.get("app_server_generation"),
            )
            self.storage.record_workflow_event(
                workflow_id,
                event_type="workflow_status_changed",
                message=f"Workflow moved to {phase}.",
                details={"phase": phase, "status": status, "lastError": last_error},
                created_at=now,
            )
            workflow = self.storage.get_workflow(workflow_id) or workflow

        sources = [
            str(item.get("source") or "")
            for item in (plan_turn, execution_turn)
            if isinstance(item, dict)
        ]
        source = "live" if "live" in sources else ("kb_history" if "kb_history" in sources or "app_server+kb_history" in sources else "storage")
        staleness = _min_staleness(
            [
                workflow.get("updated_at"),
                (plan_turn or {}).get("updatedAt") or (plan_turn or {}).get("updated_at") if plan_turn else None,
                (execution_turn or {}).get("updatedAt") or (execution_turn or {}).get("updated_at") if execution_turn else None,
                latest_plan.get("updatedAt") if latest_plan else None,
                (plan_operation or {}).get("updatedAt") if plan_operation else None,
                (execution_operation or {}).get("updatedAt") if execution_operation else None,
            ]
        )
        result = {
            "ok": True,
            "workflow_id": workflow_id,
            "workflowId": workflow_id,
            "workflow_kind": workflow_kind,
            "workflowKind": workflow_kind,
            "project_id": workflow.get("project_id"),
            "projectId": workflow.get("project_id"),
            "thread_id": thread_id,
            "threadId": thread_id,
            "plan_turn_id": plan_turn_id,
            "planTurnId": plan_turn_id,
            "execution_turn_id": execution_turn_id,
            "executionTurnId": execution_turn_id,
            "current_operation_id": current_operation_id,
            "currentOperationId": current_operation_id,
            "plan_operation_id": plan_operation_id,
            "planOperationId": plan_operation_id,
            "execution_operation_id": execution_operation_id,
            "executionOperationId": execution_operation_id,
            "phase": phase,
            "status": status,
            "lastError": last_error,
            "createdAt": workflow.get("created_at"),
            "updatedAt": workflow.get("updated_at"),
            "completedAt": completed_at,
            "clientRequestId": workflow.get("client_request_id"),
            "executionClientRequestId": workflow.get("execution_client_request_id"),
            "latestPlanItemId": workflow.get("latest_plan_item_id"),
            "latestPlanHash": workflow.get("latest_plan_hash"),
            "latestReportHash": workflow.get("latest_report_hash"),
            "planOperation": plan_operation,
            "executionOperation": execution_operation,
            "planTurn": plan_turn,
            "executionTurn": execution_turn,
            "plans": plans,
            "latestPlan": latest_plan,
            "finalReport": final_report,
            "pendingInteractions": pending_interactions,
            "nextRecommendedAction": _next_workflow_action(phase),
            "recommendedPollAfterSeconds": _workflow_poll_seconds(phase),
            "pollRecommended": phase not in {"completed", "failed", "orphaned_after_app_server_exit"},
            "appServerGeneration": self._app_server.process_generation
            if self._app_server is not None
            else workflow.get("app_server_generation"),
            "source": source,
            "stalenessSeconds": staleness,
        }
        if include_events:
            result["events"] = [_workflow_event_to_tool(row) for row in self.storage.list_workflow_events(workflow_id, limit=20)]
        return result

    def _sync_workflow_state(
        self,
        workflow: dict[str, Any],
        *,
        last_messages: int,
        message_max_chars: int,
    ) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        updates: dict[str, Any] = {}
        plan_operation_id = _optional_string(workflow.get("plan_operation_id"))
        execution_operation_id = _optional_string(workflow.get("execution_operation_id"))
        plan_operation = self.storage.get_operation(plan_operation_id) if plan_operation_id else None
        execution_operation = self.storage.get_operation(execution_operation_id) if execution_operation_id else None
        if plan_operation is not None:
            self._schedule_operation_if_needed(plan_operation)
            thread_id = _optional_string(plan_operation.get("thread_id"))
            turn_id = _optional_string(plan_operation.get("turn_id"))
            if thread_id and thread_id != _optional_string(workflow.get("thread_id")):
                updates["thread_id"] = thread_id
            if turn_id and turn_id != _optional_string(workflow.get("plan_turn_id")):
                updates["plan_turn_id"] = turn_id
            if not _optional_string(workflow.get("current_operation_id")):
                updates["current_operation_id"] = plan_operation_id
        if execution_operation is not None:
            self._schedule_operation_if_needed(execution_operation)
            thread_id = _optional_string(execution_operation.get("thread_id"))
            turn_id = _optional_string(execution_operation.get("turn_id"))
            if thread_id and thread_id != _optional_string(workflow.get("thread_id")):
                updates["thread_id"] = thread_id
            if turn_id and turn_id != _optional_string(workflow.get("execution_turn_id")):
                updates["execution_turn_id"] = turn_id
            if execution_operation_id and execution_operation_id != _optional_string(workflow.get("current_operation_id")):
                updates["current_operation_id"] = execution_operation_id
        if updates:
            updates["updated_at"] = _now_iso()
            self.storage.update_workflow(workflow_id, **updates)
            workflow = self.storage.get_workflow(workflow_id) or workflow
        return workflow

    def _workflow_final_report(
        self,
        workflow: dict[str, Any],
        *,
        execution_turn: dict[str, Any] | None,
        message_max_chars: int,
    ) -> dict[str, Any] | None:
        workflow_id = str(workflow["workflow_id"])
        stored: dict[str, Any] | None = None
        try:
            loaded = json.loads(str(workflow.get("final_report_json") or "null"))
            if isinstance(loaded, dict):
                stored = loaded
        except json.JSONDecodeError:
            stored = None

        final_text = _optional_string((execution_turn or {}).get("finalMessage")) or _optional_string((execution_turn or {}).get("final_message"))
        if execution_turn is not None and str(execution_turn.get("status") or "") == "completed" and final_text:
            report_hash = prompt_hash(normalize_prompt(final_text))
            truncated, budget = _truncate_text(final_text, message_max_chars)
            report = {
                "text": truncated,
                "summary": truncated,
                "truncated": bool(budget.get("truncated")),
                "originalChars": budget.get("original_chars"),
                "returnedChars": budget.get("returned_chars"),
                "threadId": execution_turn.get("threadId") or workflow.get("thread_id"),
                "turnId": execution_turn.get("turnId") or workflow.get("execution_turn_id"),
                "source": execution_turn.get("source") or "storage",
                "readFullVia": "codex_get_chat",
            }
            if report_hash != workflow.get("latest_report_hash") or stored is None:
                self.storage.update_workflow(
                    workflow_id,
                    latest_report_hash=report_hash,
                    final_report_json=json.dumps({"text": final_text, **{k: v for k, v in report.items() if k != "text"}}, ensure_ascii=False),
                    updated_at=_now_iso(),
                )
            return report

        if stored is None:
            return None
        text = _optional_string(stored.get("text"))
        truncated, budget = _truncate_text(text, message_max_chars)
        restored = dict(stored)
        restored["text"] = truncated
        restored["summary"] = truncated
        restored["truncated"] = bool(budget.get("truncated"))
        restored["originalChars"] = budget.get("original_chars")
        restored["returnedChars"] = budget.get("returned_chars")
        return restored

    def _turn_status_or_none(
        self,
        turn_id: str,
        thread_id: str | None,
        *,
        last_messages: int,
        message_max_chars: int,
    ) -> dict[str, Any] | None:
        try:
            return self.codex_get_turn_status(
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "last_messages": last_messages,
                    "message_max_chars": message_max_chars,
                }
            )
        except CodexMcpError:
            return None

    def _load_chat_summary(
        self,
        chat: Chat,
        *,
        archived: bool,
        include_tool_calls: bool,
        include_tool_outputs: bool,
        include_command_outputs: bool,
        include_reasoning: bool,
    ) -> tuple[TranscriptSummary, dict[str, Any]]:
        transcript_path = self.catalog.locate_transcript(chat)
        if transcript_path is None:
            raise transcript_not_found(chat.chat_id)
        if transcript_path.startswith(HOOK_HISTORY_PREFIX):
            thread_id = self.catalog.hook_history.thread_id_from_uri(transcript_path) or chat.thread_id
            summary = self.catalog.hook_history.parse_thread(thread_id)
            fingerprint = self.catalog.hook_history.fingerprint(thread_id)
            return summary, {
                "path": transcript_path,
                "size": fingerprint.total_size,
                "mtime_ns": fingerprint.max_mtime_ns,
                "mtime": fingerprint.mtime,
                "source": "hook_history",
            }
        path = Path(transcript_path)
        if path.is_dir():
            summary = self.catalog.kb_history.parse_thread_dir(
                path,
                include_tool_calls=include_tool_calls,
                include_tool_outputs=include_tool_outputs,
                include_command_outputs=include_command_outputs,
                include_reasoning=include_reasoning,
            )
            fingerprint = self.catalog.kb_history.fingerprint(path)
            return summary, {
                "path": str(path),
                "size": fingerprint.total_size,
                "mtime_ns": fingerprint.max_mtime_ns,
                "mtime": fingerprint.mtime,
                "source": "kb_history",
            }
        stat = path.stat()
        summary = parse_transcript(
            path,
            archived=archived,
            include_tool_calls=include_tool_calls,
            include_tool_outputs=include_tool_outputs,
            include_command_outputs=include_command_outputs,
            include_reasoning=include_reasoning,
        )
        return summary, {
            "path": str(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "source": "transcript",
        }

    def _summary_input_with_rolling(self, thread_id: str, transcript_path: str, upper: list[TranscriptMessage]) -> tuple[list[TranscriptMessage], bool]:
        if not self.config.rolling_summary_enabled or not upper:
            return upper, False
        before_line = _last_source_line(upper)
        rolling = self.storage.get_rolling_summary(thread_id, transcript_path, before_line)
        if not rolling:
            return upper, False
        source_line_end = int(rolling.get("source_line_end") or 0)
        new_messages = [message for message in upper if (message.source_line_start or 0) > source_line_end]
        if not new_messages:
            return upper, False
        synthetic = TranscriptMessage(
            message_id=f"{thread_id}:rolling-summary:{source_line_end}",
            thread_id=thread_id,
            turn_id=None,
            role="system",
            created_at=rolling.get("updated_at"),
            text="Rolling summary before recent messages:\n" + str(rolling.get("summary_text") or ""),
            items=[],
            metadata={"source": "rolling_summary", "source_line_end": source_line_end},
            source_line_start=source_line_end,
            source_line_end=source_line_end,
        )
        limit = max(1, self.config.deepseek_recent_messages_limit - 1)
        return [synthetic] + new_messages[-limit:], True

    def _finalize_read_result(
        self,
        result: dict[str, Any],
        tool_name: str,
        thread_id: str | None,
        history_summary: dict[str, Any] | None,
        truncated_fields: list[str],
    ) -> dict[str, Any]:
        budget = _budget_for_result(result, tool_name, history_summary, truncated_fields)
        result["budget"] = budget
        try:
            self.storage.record_budget_audit(tool_name, thread_id, budget, _now_iso())
        except Exception:
            LOG.exception("budget audit failed tool=%s", tool_name)
        return result

    def _pending_interactions_for_context(
        self,
        *,
        thread_id: str | None,
        turn_id: str | None,
        status: str | None = "pending",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        if turn_id:
            for row in self.storage.list_pending_interactions(turn_id=turn_id, status=status, limit=limit):
                interaction_id = str(row.get("interaction_id") or "")
                if interaction_id and interaction_id not in seen:
                    rows.append(row)
                    seen.add(interaction_id)
        if thread_id and len(rows) < limit:
            for row in self.storage.list_pending_interactions(thread_id=thread_id, status=status, limit=limit):
                interaction_id = str(row.get("interaction_id") or "")
                if interaction_id and interaction_id not in seen:
                    rows.append(row)
                    seen.add(interaction_id)
                if len(rows) >= limit:
                    break
        return [_pending_interaction_summary(row) for row in rows[:limit]]

    def codex_list_pending_interactions(self, args: dict[str, Any]) -> dict[str, Any]:
        thread_id = args.get("thread_id")
        thread_id = str(thread_id).strip() if thread_id not in (None, "") else None
        turn_id = args.get("turn_id")
        turn_id = str(turn_id).strip() if turn_id not in (None, "") else None
        explicit_turn_id = turn_id is not None
        operation_id = _optional_string(args.get("operation_id"))
        workflow_id = _optional_string(args.get("workflow_id"))
        if operation_id:
            operation = self.storage.get_operation(operation_id)
            if operation is None:
                raise invalid_argument("Codex operation was not found.", operation_id=operation_id)
            thread_id = thread_id or _optional_string(operation.get("thread_id"))
            turn_id = turn_id or _optional_string(operation.get("turn_id"))
            workflow_id = workflow_id or _optional_string(operation.get("workflow_id"))
        if workflow_id:
            workflow = self.storage.get_workflow(workflow_id)
            if workflow is None:
                raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
            thread_id = thread_id or _optional_string(workflow.get("thread_id"))
            if explicit_turn_id:
                turn_id = turn_id or _optional_string(workflow.get("execution_turn_id")) or _optional_string(workflow.get("plan_turn_id"))
        status = args.get("status")
        status = str(status).strip() if status not in (None, "") else None
        limit = _bounded_int(args.get("limit", 50), 1, 200)
        manager = self._app_server.interactions if self._app_server is not None else PendingInteractionManager(self.storage)
        if workflow_id and thread_id and not turn_id:
            interactions = self._pending_interactions_for_context(thread_id=thread_id, turn_id=None, status=status, limit=limit)
        else:
            interactions = manager.list_interactions(thread_id=thread_id, turn_id=turn_id, status=status, limit=limit)
        return {
            "ok": True,
            "operationId": operation_id,
            "workflowId": workflow_id,
            "threadId": thread_id,
            "turnId": turn_id,
            "interactions": interactions,
            "returned_count": len(interactions),
            "returnedCount": len(interactions),
        }

    async def codex_answer_pending_interaction(self, args: dict[str, Any]) -> dict[str, Any]:
        interaction_id = _required_string(args, "interaction_id")
        if self._app_server is None:
            raise pending_interaction_unavailable(
                "Codex app-server is not live in this MCP process; pending interactions cannot be answered.",
                interaction_id=interaction_id,
            )
        return self._app_server.interactions.answer(
            interaction_id,
            args,
            current_process_generation=self._app_server.process_generation,
        )

    async def codex_interrupt_turn(self, args: dict[str, Any]) -> dict[str, Any]:
        target = self._resolve_interrupt_target(args)
        thread_id = target["threadId"]
        turn_id = target["turnId"]
        timeout_seconds = _bounded_int(args.get("timeout_seconds", 30), 1, 120)
        client = await self._app()
        result = await client.turn_interrupt(thread_id=thread_id, turn_id=turn_id, timeout_seconds=timeout_seconds)
        status = client.tracker.get_turn_status(turn_id, last_messages=10, message_max_chars=8000)
        now = _now_iso()
        operation_id = target.get("operationId")
        workflow_id = target.get("workflowId")
        if operation_id:
            operation = self.storage.get_operation(str(operation_id))
            if operation is not None and str(operation.get("status") or "") not in OPERATION_TERMINAL_STATUSES:
                self.storage.update_operation(
                    str(operation_id),
                    status="interrupted",
                    phase="interrupted",
                    last_error="Interrupted by OpenClaw.",
                    updated_at=now,
                    completed_at=now,
                )
        if workflow_id:
            workflow = self.storage.get_workflow(str(workflow_id))
            if workflow is not None and str(workflow.get("status") or "") not in {"completed", "failed", "orphaned_after_app_server_exit"}:
                self.storage.update_workflow(
                    str(workflow_id),
                    phase="failed",
                    status="interrupted",
                    last_error="Interrupted by OpenClaw.",
                    updated_at=now,
                    completed_at=now,
                )
        return {
            "ok": True,
            "interrupted": True,
            "thread_id": thread_id,
            "threadId": thread_id,
            "turn_id": turn_id,
            "turnId": turn_id,
            "operationId": operation_id,
            "workflowId": workflow_id,
            "interruptedTarget": target,
            "status": (status or {}).get("status") or "interrupted",
            "nextRecommendedAction": "inspect_diagnostics",
            "recommendedPollAfterSeconds": 0,
            "pollRecommended": False,
            "appServerResult": result,
            "turnStatus": status,
        }

    def _resolve_interrupt_target(self, args: dict[str, Any]) -> dict[str, Any]:
        thread_id = _optional_string(args.get("thread_id"))
        turn_id = _optional_string(args.get("turn_id"))
        operation_id = _optional_string(args.get("operation_id"))
        workflow_id = _optional_string(args.get("workflow_id"))
        source = "direct"

        if workflow_id:
            workflow = self.storage.get_workflow(workflow_id)
            if workflow is None:
                raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
            source = "workflow"
            thread_id = thread_id or _optional_string(workflow.get("thread_id"))
            turn_id = (
                turn_id
                or _optional_string(workflow.get("execution_turn_id"))
                or _optional_string(workflow.get("plan_turn_id"))
            )
            operation_id = (
                operation_id
                or _optional_string(workflow.get("current_operation_id"))
                or _optional_string(workflow.get("execution_operation_id"))
                or _optional_string(workflow.get("plan_operation_id"))
            )

        if operation_id:
            operation = self.storage.get_operation(operation_id)
            if operation is None:
                raise invalid_argument("Codex operation was not found.", operation_id=operation_id)
            source = "operation" if source == "direct" else source
            thread_id = thread_id or _optional_string(operation.get("thread_id"))
            turn_id = turn_id or _optional_string(operation.get("turn_id"))
            workflow_id = workflow_id or _optional_string(operation.get("workflow_id"))

        if turn_id and not thread_id:
            turn = self.storage.get_tracked_turn(turn_id)
            thread_id = _optional_string((turn or {}).get("thread_id"))

        if not thread_id or not turn_id:
            raise invalid_argument(
                "codex_interrupt_turn requires thread_id+turn_id or resolvable operation_id/workflow_id.",
                thread_id=thread_id,
                turn_id=turn_id,
                operation_id=operation_id,
                workflow_id=workflow_id,
            )

        return {
            "source": source,
            "threadId": thread_id,
            "turnId": turn_id,
            "operationId": operation_id,
            "workflowId": workflow_id,
        }

    async def codex_restart_app_server(self, args: dict[str, Any]) -> dict[str, Any]:
        start_after_restart = bool(args.get("start_after_restart", True))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", 30), 1, 120)
        force = bool(args.get("force", False))
        if self._app_server is None:
            if not start_after_restart:
                return {
                    "ok": True,
                    "restarted": False,
                    "started": False,
                    "before_pid": None,
                    "after_pid": None,
                    "active_work": {"pending_requests": 0, "active_turns": []},
                    "activeWork": {"pendingRequests": 0, "activeTurns": []},
                }
            self._app_server = CodexAppServerClient(self.config, self.storage)
        return await self._app_server.restart(start_after_restart=start_after_restart, timeout_seconds=timeout_seconds, force=force)

    def codex_get_app_server_status(self, args: dict[str, Any]) -> dict[str, Any]:
        include_recent_events = bool(args.get("include_recent_events", False))
        if self._app_server is None:
            return {
                "ok": True,
                "running": False,
                "started": False,
                "pid": None,
                "processGeneration": 0,
                "pendingRequests": 0,
                "activeTurns": [],
                "codexBinaryPath": str(self.config.codex_binary_path),
                "codexBinaryExists": self.config.codex_binary_path.exists(),
            }
        return self._app_server.status_snapshot(include_recent_events=include_recent_events)

    def codex_health_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        since_minutes = _bounded_int(args.get("since_minutes", 120), 1, 10080)
        stale_after_minutes = _bounded_int(args.get("stale_after_minutes", 30), 1, 10080)
        max_recent_errors = _bounded_int(args.get("max_recent_errors", 5), 0, 50)
        generated_at = _now_iso()
        context = self._diagnostic_context(args)
        app_status = self.codex_get_app_server_status({"include_recent_events": False})
        pending = self._pending_interactions_for_diagnostics(context, limit=20)
        active_turns = self._active_turns_snapshot(app_status)
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)).isoformat()
        stale_operations = _filter_operations_for_context(
            context,
            self.storage.list_stale_operations(stale_before=stale_before, limit=50),
        )[:20]
        since = _since_iso(since_minutes)
        recent_events = [
            event_to_tool(row, include_payload=False)
            for row in self.storage.list_app_server_events(
                thread_id=context["threadId"],
                turn_id=context["turnId"],
                process_generation=None,
                since=since,
                limit=max(50, max_recent_errors),
            )
        ]
        recent_errors = _recent_error_events(recent_events)[:max_recent_errors] if bool(args.get("include_recent_errors", True)) else []
        workflows = [context["workflow"]] if context["workflow"] is not None else self.storage.list_workflows(limit=10)
        checks = self._diagnostic_checks(
            app_status=app_status,
            pending_interactions=pending,
            workflows=workflows,
            event_pointers=recent_events,
            log_path=_diagnostic_log_path(),
            stale_operations=stale_operations,
        )
        hook_history = self._hook_history_snapshot()
        recommendations = _recommended_actions_from_checks(checks)
        if stale_operations:
            recommendations.insert(0, "recover_stale_operations")
        next_action = _health_next_action(
            app_status=app_status,
            pending_interactions=pending,
            stale_operations=stale_operations,
            recent_errors=recent_errors,
            checks=checks,
        )
        active_work = {
            "pendingRequests": app_status.get("pendingRequests", 0),
            "activeTurns": active_turns,
            "pendingInteractions": pending,
            "activeTurnCount": len(active_turns),
            "pendingInteractionCount": len(pending),
        }
        result = {
            "ok": True,
            "generatedAt": generated_at,
            "version": _contract_version_block(generated_at=generated_at),
            "overallStatus": overall_status(checks),
            "filters": {
                "operationId": context["operationId"],
                "workflowId": context["workflowId"],
                "threadId": context["threadId"],
                "turnId": context["turnId"],
                "sinceMinutes": since_minutes,
                "staleAfterMinutes": stale_after_minutes,
            },
            "appServer": {
                "running": app_status.get("running"),
                "started": app_status.get("started"),
                "pid": app_status.get("pid"),
                "processGeneration": app_status.get("processGeneration"),
                "pendingRequests": app_status.get("pendingRequests", 0),
                "codexBinaryPath": app_status.get("codexBinaryPath") or str(self.config.codex_binary_path),
                "codexBinaryExists": app_status.get("codexBinaryExists", self.config.codex_binary_path.exists()),
            },
            "activeWork": active_work,
            "staleOperations": [_operation_summary_to_tool(row) for row in stale_operations],
            "recentErrors": recent_errors,
            "paths": {
                "codexBinaryPath": str(self.config.codex_binary_path),
                "mcpStateDb": str(self.config.state_db_path),
                "kbHistoryProjectsRoot": str(self.config.kb_history_projects_root),
            },
            "hookHistory": hook_history,
            "hookHistoryStatus": hook_history["status"],
            "lastHookEventAt": hook_history["lastHookEventAt"],
            "hookInstalled": hook_history["installed"],
            "hookDbWritable": hook_history["dbWritable"],
            "configHints": {
                "defaultModel": self.config.default_model,
                "defaultApprovalPolicy": self.config.default_approval_policy,
                "defaultSandboxPolicy": self.config.default_sandbox_policy,
                "startAppServerForReadTools": self.config.start_app_server_for_read_tools,
            },
            "recommendedActions": _unique_strings(recommendations),
            "nextRecommendedAction": next_action,
            "recommendedPollAfterSeconds": 15 if active_turns or pending or stale_operations else 0,
            "pollRecommended": bool(active_turns or pending or stale_operations),
        }
        return redact_payload(result)

    def _diagnostic_context(self, args: dict[str, Any]) -> dict[str, Any]:
        operation_id = _optional_string(args.get("operation_id"))
        workflow_id = _optional_string(args.get("workflow_id"))
        thread_id = _optional_string(args.get("thread_id"))
        turn_id = _optional_string(args.get("turn_id"))
        operation = self.storage.get_operation(operation_id) if operation_id else None
        if operation is not None:
            workflow_id = workflow_id or _optional_string(operation.get("workflow_id"))
            thread_id = thread_id or _optional_string(operation.get("thread_id"))
            turn_id = turn_id or _optional_string(operation.get("turn_id"))
        workflow = self.storage.get_workflow(workflow_id) if workflow_id else None
        if workflow is not None:
            thread_id = thread_id or _optional_string(workflow.get("thread_id"))
            turn_id = (
                turn_id
                or _optional_string(workflow.get("execution_turn_id"))
                or _optional_string(workflow.get("plan_turn_id"))
            )
            operation_id = (
                operation_id
                or _optional_string(workflow.get("current_operation_id"))
                or _optional_string(workflow.get("execution_operation_id"))
                or _optional_string(workflow.get("plan_operation_id"))
            )
            if operation is None and operation_id:
                operation = self.storage.get_operation(operation_id)
        if turn_id and not thread_id:
            turn = self.storage.get_tracked_turn(turn_id)
            thread_id = _optional_string((turn or {}).get("thread_id"))
        operations = []
        if workflow_id:
            operations = self.storage.list_operations_for_workflow(workflow_id, limit=20)
        elif operation is not None:
            operations = [operation]
        prompt_submissions = self.storage.list_prompt_submissions(
            operation_id=operation_id,
            workflow_id=workflow_id if not operation_id else None,
            thread_id=thread_id if not operation_id and not workflow_id else None,
            turn_id=turn_id if not operation_id and not workflow_id else None,
            limit=20,
        )
        return {
            "operationId": operation_id,
            "workflowId": workflow_id,
            "threadId": thread_id,
            "turnId": turn_id,
            "operation": operation,
            "workflow": workflow,
            "operations": operations,
            "promptSubmissions": prompt_submissions,
            "trackedTurn": self.storage.get_tracked_turn(turn_id) if turn_id else None,
        }

    def codex_collect_diagnostics(self, args: dict[str, Any]) -> dict[str, Any]:
        since_minutes = _bounded_int(args.get("since_minutes", 120), 1, 10080)
        log_limit = _bounded_int(args.get("log_limit", 50), 1, 500)
        event_limit = _bounded_int(args.get("event_limit", 100), 1, 500)
        timeline_limit = _bounded_int(args.get("timeline_limit", 100), 1, 500)
        context = self._diagnostic_context(args)
        operation_id = context["operationId"]
        workflow_id = context["workflowId"]
        thread_id = context["threadId"]
        turn_id = context["turnId"]
        workflow = context["workflow"]
        if bool(args.get("refresh_catalog", False)):
            self.catalog.refresh()
        app_status = self.codex_get_app_server_status({"include_recent_events": True})
        pending = self._pending_interactions_for_diagnostics(context, limit=50)
        workflows = [workflow] if workflow is not None else self.storage.list_workflows(limit=20)
        active_work = {
            "pendingRequests": app_status.get("pendingRequests", 0),
            "activeTurns": app_status.get("activeTurns", []),
            "pendingInteractions": pending if pending else app_status.get("pendingInteractions", []),
        }
        since = _since_iso(since_minutes)
        events = [
            event_to_tool(row, include_payload=False)
            for row in self.storage.list_app_server_events(
                thread_id=thread_id,
                turn_id=turn_id,
                process_generation=None,
                since=since,
                limit=event_limit,
            )
        ]
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        stale_operations = _filter_operations_for_context(
            context,
            self.storage.list_stale_operations(stale_before=stale_before, limit=50),
        )[:20]
        log_path = _diagnostic_log_path()
        checks = self._diagnostic_checks(
            app_status=app_status,
            pending_interactions=pending,
            workflows=workflows,
            event_pointers=events,
            log_path=log_path,
            stale_operations=stale_operations,
        )
        hook_history = self._hook_history_snapshot()
        correlation = self._diagnostic_correlation(context)
        timeline = (
            self._diagnostic_timeline(context=context, app_events=events, pending_interactions=pending, limit=timeline_limit)
            if bool(args.get("include_timeline", True))
            else []
        )
        diagnosis_confidence = _diagnosis_confidence(checks=checks, correlation=correlation, timeline=timeline)
        result = {
            "ok": True,
            "collectedAt": _now_iso(),
            "overallStatus": overall_status(checks),
            "checks": checks,
            "filters": {
                "operationId": operation_id,
                "workflowId": workflow_id,
                "threadId": thread_id,
                "turnId": turn_id,
                "sinceMinutes": since_minutes,
            },
            "paths": {
                "codexHome": str(self.config.codex_home),
                "sessionsDir": str(self.config.sessions_dir),
                "archivedSessionsDir": str(self.config.archived_sessions_dir),
                "codexStateDb": str(self.config.codex_state_db),
                "codexLogsDb": str(self.config.codex_logs_db),
                "kbHistoryProjectsRoot": str(self.config.kb_history_projects_root),
                "mcpStateDb": str(self.config.state_db_path),
                "mcpLog": str(log_path),
                "codexBinaryPath": str(self.config.codex_binary_path),
                "allowedRoots": [str(root) for root in self.config.allowed_roots],
            },
            "config": {
                "defaultApprovalPolicy": self.config.default_approval_policy,
                "defaultSandboxPolicy": self.config.default_sandbox_policy,
                "defaultModel": self.config.default_model,
                "defaultEffort": self.config.default_effort,
                "approvalResponseTimeoutSeconds": self.config.approval_response_timeout_seconds,
                "deepseekSummaryEnabled": self.config.deepseek_summary_enabled,
                "startAppServerForReadTools": self.config.start_app_server_for_read_tools,
            },
            "appServer": app_status,
            "activeWork": active_work,
            "pendingInteractions": pending,
            "operationSummary": _operation_summary_to_tool(context["operation"]) if context["operation"] is not None else None,
            "operations": [_operation_summary_to_tool(row) for row in context["operations"]],
            "workflowSummary": [_workflow_summary_to_tool(row) for row in workflows if row is not None],
            "promptSubmissions": [_prompt_submission_summary_to_tool(row) for row in context["promptSubmissions"]],
            "correlation": correlation,
            "timeline": timeline,
            "diagnosisConfidence": diagnosis_confidence,
            "logPointers": {
                "path": str(log_path),
                "exists": log_path.exists(),
                "rotatedExisting": [str(path) for path in _rotated_log_paths(log_path) if path.exists()],
            },
            "eventPointers": events,
            "searchIndex": self._search_index_snapshot(),
            "hookHistory": hook_history,
            "hookHistoryStatus": hook_history["status"],
            "lastHookEventAt": hook_history["lastHookEventAt"],
            "hookInstalled": hook_history["installed"],
            "hookDbWritable": hook_history["dbWritable"],
        }
        if bool(args.get("include_logs", False)):
            result["logs"] = self.codex_get_diagnostic_logs(
                {
                    "source": "all",
                    "workflow_id": workflow_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "since_minutes": since_minutes,
                    "limit": log_limit,
                    "include_payload": False,
                }
            )
        return redact_payload(result)

    def codex_get_diagnostic_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        source = str(args.get("source") or "all")
        workflow_id = _optional_string(args.get("workflow_id"))
        thread_id = _optional_string(args.get("thread_id"))
        turn_id = _optional_string(args.get("turn_id"))
        workflow = self.storage.get_workflow(workflow_id) if workflow_id else None
        if workflow is not None:
            thread_id = thread_id or _optional_string(workflow.get("thread_id"))
            turn_id = turn_id or _optional_string(workflow.get("execution_turn_id")) or _optional_string(workflow.get("plan_turn_id"))
        limit = _bounded_int(args.get("limit", 100), 1, 1000)
        max_line_chars = _bounded_int(args.get("max_line_chars", 4000), 200, 20000)
        since = _since_iso(_bounded_int(args.get("since_minutes", 120), 1, 10080))
        severity = _optional_string(args.get("severity"))
        process_generation = args.get("process_generation")
        process_generation = int(process_generation) if process_generation not in (None, "") else None
        include_payload = bool(args.get("include_payload", False))
        logs: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        if source in {"all", "mcp_log"}:
            logs = read_log_files(_diagnostic_log_path(), limit=limit, severity=severity, max_line_chars=max_line_chars)
        if source in {"all", "app_server_events"}:
            events = [
                event_to_tool(row, include_payload=include_payload, max_payload_chars=max_line_chars)
                for row in self.storage.list_app_server_events(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    process_generation=process_generation,
                    since=since,
                    limit=limit,
                )
            ]
        return {
            "ok": True,
            "source": source,
            "filters": {
                "workflowId": workflow_id,
                "threadId": thread_id,
                "turnId": turn_id,
                "processGeneration": process_generation,
                "severity": severity,
            },
            "redacted": True,
            "logs": logs,
            "events": events,
            "returnedLogCount": len(logs),
            "returnedEventCount": len(events),
        }

    def codex_analyze_issue(self, args: dict[str, Any]) -> dict[str, Any]:
        problem_text = _optional_string(args.get("problem_text"))
        since_minutes = _bounded_int(args.get("since_minutes", 120), 1, 10080)
        context = self.codex_collect_diagnostics(
            {
                "operation_id": args.get("operation_id"),
                "workflow_id": args.get("workflow_id"),
                "thread_id": args.get("thread_id"),
                "turn_id": args.get("turn_id"),
                "since_minutes": since_minutes,
                "include_logs": False,
                "event_limit": 100,
            }
        )
        logs = self.codex_get_diagnostic_logs(
            {
                "source": "all",
                "workflow_id": context.get("filters", {}).get("workflowId") or args.get("workflow_id"),
                "thread_id": context.get("filters", {}).get("threadId") or args.get("thread_id"),
                "turn_id": context.get("filters", {}).get("turnId") or args.get("turn_id"),
                "since_minutes": since_minutes,
                "limit": 100,
                "include_payload": True,
            }
        )
        analysis = analyze_context(problem_text, context, logs)
        diagnosis_id = "diag_" + uuid.uuid4().hex
        created_at = _now_iso()
        if not bool(args.get("include_evidence", True)):
            for item in analysis.get("findings") or []:
                item["evidenceAvailable"] = bool(item.get("evidence"))
                item["evidence"] = []
            if analysis.get("likelyRootCause"):
                analysis["likelyRootCause"]["evidenceAvailable"] = bool(analysis["likelyRootCause"].get("evidence"))
                analysis["likelyRootCause"]["evidence"] = []
        self.storage.record_diagnostic_run(
            diagnosis_id=diagnosis_id,
            problem_text=redact_text(problem_text),
            context=context,
            summary=analysis,
            created_at=created_at,
        )
        for item in analysis.get("findings") or []:
            self.storage.record_diagnostic_finding(
                diagnosis_id=diagnosis_id,
                severity=str(item.get("severity") or "info"),
                category=str(item.get("category") or "unknown"),
                title=str(item.get("title") or ""),
                evidence=item.get("evidence") if isinstance(item.get("evidence"), list) else [],
                recommended_actions=item.get("recommendedActions") if isinstance(item.get("recommendedActions"), list) else [],
                created_at=created_at,
            )
        return {
            "ok": True,
            "diagnosisId": diagnosis_id,
            "createdAt": created_at,
            **analysis,
        }

    async def codex_repair_issue(self, args: dict[str, Any]) -> dict[str, Any]:
        requested_action = _required_string(args, "action")
        action_name = _canonical_repair_action(requested_action)
        dry_run = bool(args.get("dry_run", True))
        force = bool(args.get("force", False))
        if action_name in {"force_restart_app_server", "interrupt_turn"} and not force and not dry_run:
            raise invalid_argument("Repair action requires force=true.", action=action_name)
        repair_run_id = "repair_" + uuid.uuid4().hex
        before = self.codex_collect_diagnostics(
            {
                "operation_id": args.get("operation_id"),
                "workflow_id": args.get("workflow_id"),
                "thread_id": args.get("thread_id"),
                "turn_id": args.get("turn_id"),
                "include_logs": False,
            }
        )
        changed = False
        repair_result: dict[str, Any]
        if action_name == "cleanup_prompt_submissions":
            repair_result = self._repair_cleanup_prompt_submissions(args, dry_run=dry_run)
            changed = bool(repair_result.get("deletedPromptSubmissions"))
        elif action_name == "recover_stale_operations":
            repair_result = self._repair_recover_stale_operations(args, dry_run=dry_run)
            changed = bool(repair_result.get("resetOperationIds") or repair_result.get("runningOperationIds"))
        elif action_name == "refresh_catalog_and_history":
            repair_result = self._repair_refresh_catalog_and_history(args, dry_run=dry_run)
            changed = bool(repair_result.get("changed"))
        elif action_name == "mark_orphaned_after_exit":
            repair_result = self._repair_mark_orphaned_after_exit(args, dry_run=dry_run, force=force)
            changed = bool(repair_result.get("changed"))
        elif dry_run:
            repair_result = {"wouldRun": True, "action": action_name, "message": "Dry run only; no repair action was executed."}
        elif action_name == "restart_app_server_idle":
            repair_result = await self.codex_restart_app_server(
                {"start_after_restart": True, "timeout_seconds": _bounded_int(args.get("timeout_seconds", 30), 1, 120), "force": False}
            )
            changed = bool(repair_result.get("restarted") or repair_result.get("started"))
        elif action_name == "force_restart_app_server":
            repair_result = await self.codex_restart_app_server(
                {"start_after_restart": True, "timeout_seconds": _bounded_int(args.get("timeout_seconds", 30), 1, 120), "force": True}
            )
            changed = bool(repair_result.get("restarted") or repair_result.get("started"))
        elif action_name == "mark_stale_turns_orphaned":
            repair_result = self._repair_mark_stale_turns(args)
            changed = bool(repair_result.get("changed"))
        elif action_name == "expire_stale_pending_interactions":
            affected = self.storage.expire_pending_interactions(
                expires_before=_now_iso(),
                resolved_at=_now_iso(),
                reason="Expired by codex_repair_issue.",
            )
            repair_result = {"expiredInteractions": affected}
            changed = affected > 0
        elif action_name == "refresh_catalog":
            self.catalog.refresh()
            repair_result = {"projects": len(self.catalog.projects), "chats": len(self.catalog.chats)}
            changed = True
        elif action_name == "rebuild_search_index":
            status = SearchIndex(self.config, self.storage, self.catalog).refresh(
                include_archived=True,
                time_budget_seconds=min(_bounded_int(args.get("timeout_seconds", 30), 1, 120), 60),
            )
            repair_result = status.to_tool()
            changed = bool(status.indexed_files)
        elif action_name == "validate_paths_and_config":
            repair_result = self.codex_collect_diagnostics({"include_logs": False})
            changed = False
        elif action_name == "interrupt_turn":
            repair_result = await self.codex_interrupt_turn(
                {
                    "thread_id": args.get("thread_id"),
                    "turn_id": args.get("turn_id"),
                    "operation_id": args.get("operation_id"),
                    "workflow_id": args.get("workflow_id"),
                    "timeout_seconds": _bounded_int(args.get("timeout_seconds", 30), 1, 120),
                }
            )
            changed = bool(repair_result.get("interrupted"))
        else:
            raise invalid_argument("Unsupported repair action.", action=action_name)

        after = self.codex_collect_diagnostics(
            {
                "operation_id": args.get("operation_id"),
                "workflow_id": args.get("workflow_id"),
                "thread_id": args.get("thread_id"),
                "turn_id": args.get("turn_id"),
                "include_logs": False,
            }
        )
        created_at = _now_iso()
        self.storage.record_repair_run(
            repair_run_id=repair_run_id,
            diagnosis_id=_optional_string(args.get("diagnosis_id")),
            action=action_name,
            dry_run=dry_run,
            force=force,
            changed=changed,
            before=before,
            after=after,
            result=repair_result,
            created_at=created_at,
        )
        return {
            "ok": True,
            "repairRunId": repair_run_id,
            "action": action_name,
            "requestedAction": requested_action,
            "dryRun": dry_run,
            "force": force,
            "changed": changed,
            "before": before,
            "after": after,
            "result": redact_payload(repair_result),
            "remainingIssues": after.get("checks", []),
        }

    def _pending_interactions_for_diagnostics(self, context: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
        thread_id = context.get("threadId")
        turn_id = context.get("turnId")
        if thread_id or turn_id:
            return self._pending_interactions_for_context(thread_id=thread_id, turn_id=turn_id, status="pending", limit=limit)
        return [
            _pending_interaction_summary(row)
            for row in self.storage.list_pending_interactions(status="pending", limit=limit)
        ]

    def _active_turns_snapshot(self, app_status: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in app_status.get("activeTurns") or []:
            if not isinstance(item, dict):
                continue
            turn_id = _optional_string(item.get("turnId")) or _optional_string(item.get("turn_id"))
            if turn_id:
                seen.add(turn_id)
            rows.append(redact_payload(item))
        for turn in self.storage.get_running_tracked_turns():
            turn_id = str(turn.get("turn_id") or "")
            if turn_id in seen:
                continue
            seen.add(turn_id)
            rows.append(
                {
                    "threadId": turn.get("thread_id"),
                    "turnId": turn.get("turn_id"),
                    "status": turn.get("status"),
                    "updatedAt": turn.get("updated_at"),
                    "source": "storage",
                    "processGeneration": turn.get("process_generation"),
                    "stalenessSeconds": _staleness_seconds(str(turn.get("updated_at") or "")),
                }
            )
        return rows

    def _diagnostic_correlation(self, context: dict[str, Any]) -> dict[str, Any]:
        workflow = context.get("workflow")
        operation = context.get("operation")
        tracked_turn = context.get("trackedTurn")
        return redact_payload(
            {
                "operationId": context.get("operationId"),
                "workflowId": context.get("workflowId"),
                "threadId": context.get("threadId"),
                "turnId": context.get("turnId"),
                "operation": _operation_summary_to_tool(operation) if operation is not None else None,
                "workflow": _workflow_summary_to_tool(workflow) if workflow is not None else None,
                "trackedTurn": _tracked_turn_summary_to_tool(tracked_turn) if tracked_turn is not None else None,
                "relatedOperations": [_operation_summary_to_tool(row) for row in context.get("operations") or []],
                "promptSubmissions": [
                    _prompt_submission_summary_to_tool(row)
                    for row in context.get("promptSubmissions") or []
                ],
            }
        )

    def _diagnostic_timeline(
        self,
        *,
        context: dict[str, Any],
        app_events: list[dict[str, Any]],
        pending_interactions: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        operation = context.get("operation")
        if operation is not None:
            entries.append(
                _timeline_entry(
                    operation.get("created_at"),
                    "operation",
                    "created",
                    operation_id=operation.get("operation_id"),
                    workflow_id=operation.get("workflow_id"),
                    thread_id=operation.get("thread_id"),
                    turn_id=operation.get("turn_id"),
                    details={"operationType": operation.get("operation_type"), "status": operation.get("status")},
                )
            )
            entries.append(
                _timeline_entry(
                    operation.get("updated_at"),
                    "operation",
                    "updated",
                    operation_id=operation.get("operation_id"),
                    workflow_id=operation.get("workflow_id"),
                    thread_id=operation.get("thread_id"),
                    turn_id=operation.get("turn_id"),
                    details={"status": operation.get("status"), "phase": operation.get("phase"), "lastError": operation.get("last_error")},
                )
            )
        workflow = context.get("workflow")
        if workflow is not None:
            for event in self.storage.list_workflow_events(str(workflow.get("workflow_id")), limit=min(limit, 50)):
                entries.append(
                    _timeline_entry(
                        event.get("created_at"),
                        "workflow",
                        str(event.get("event_type") or "workflow_event"),
                        operation_id=None,
                        workflow_id=workflow.get("workflow_id"),
                        thread_id=workflow.get("thread_id"),
                        turn_id=workflow.get("execution_turn_id") or workflow.get("plan_turn_id"),
                        details={"message": event.get("message"), "details": _json_loads_dict(event.get("details_json"))},
                    )
                )
        tracked_turn = context.get("trackedTurn")
        if tracked_turn is not None:
            entries.append(
                _timeline_entry(
                    tracked_turn.get("updated_at") or tracked_turn.get("started_at"),
                    "tracked_turn",
                    str(tracked_turn.get("status") or "status"),
                    operation_id=context.get("operationId"),
                    workflow_id=context.get("workflowId"),
                    thread_id=tracked_turn.get("thread_id"),
                    turn_id=tracked_turn.get("turn_id"),
                    details={"status": tracked_turn.get("status"), "lastError": _tracked_turn_last_error(tracked_turn)},
                )
            )
        for prompt in context.get("promptSubmissions") or []:
            entries.append(
                _timeline_entry(
                    prompt.get("updated_at") or prompt.get("created_at"),
                    "prompt_submission",
                    str(prompt.get("status") or "status"),
                    operation_id=prompt.get("operation_id"),
                    workflow_id=prompt.get("workflow_id"),
                    thread_id=prompt.get("thread_id"),
                    turn_id=prompt.get("turn_id"),
                    details={
                        "promptSubmissionId": prompt.get("prompt_submission_id"),
                        "operationType": prompt.get("operation_type"),
                        "promptHash": prompt.get("prompt_hash"),
                        "duplicateOfSubmissionId": prompt.get("duplicate_of_submission_id"),
                    },
                )
            )
        for interaction in pending_interactions:
            entries.append(
                _timeline_entry(
                    interaction.get("createdAt") or interaction.get("created_at"),
                    "pending_interaction",
                    str(interaction.get("status") or "pending"),
                    operation_id=context.get("operationId"),
                    workflow_id=context.get("workflowId"),
                    thread_id=interaction.get("threadId"),
                    turn_id=interaction.get("turnId"),
                    details={"interactionId": interaction.get("interactionId"), "method": interaction.get("method")},
                )
            )
        for event in app_events:
            entries.append(
                _timeline_entry(
                    event.get("receivedAt") or event.get("received_at"),
                    "app_server_event",
                    str(event.get("method") or event.get("direction") or "event"),
                    operation_id=context.get("operationId"),
                    workflow_id=context.get("workflowId"),
                    thread_id=event.get("threadId"),
                    turn_id=event.get("turnId"),
                    details={
                        "direction": event.get("direction"),
                        "processGeneration": event.get("processGeneration"),
                        "eventId": event.get("id"),
                    },
                )
            )
        entries = [entry for entry in entries if entry.get("time")]
        entries.sort(key=lambda item: str(item.get("time") or ""))
        return redact_payload(entries[-limit:])

    def _diagnostic_checks(
        self,
        *,
        app_status: dict[str, Any],
        pending_interactions: list[dict[str, Any]],
        workflows: list[dict[str, Any] | None],
        event_pointers: list[dict[str, Any]],
        log_path: Path,
        stale_operations: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        checks.append(
            diagnostic_check(
                "codex_binary",
                "ok" if self.config.codex_binary_path.exists() else "error",
                "Codex binary exists." if self.config.codex_binary_path.exists() else "Codex binary does not exist.",
                details={"path": str(self.config.codex_binary_path), "category": "app_server_unavailable"},
                suggested_action="validate_paths_and_config",
            )
        )
        checks.append(
            diagnostic_check(
                "mcp_state_db",
                "ok" if self.config.state_db_path.exists() else "warning",
                "MCP state DB exists." if self.config.state_db_path.exists() else "MCP state DB does not exist yet.",
                details={"path": str(self.config.state_db_path), "category": "state_db"},
            )
        )
        hook_history = self._hook_history_snapshot()
        checks.append(
            diagnostic_check(
                "hook_history",
                "ok" if hook_history["status"] in {"ok", "disabled"} else "warning",
                "Hook-backed SQLite history is available."
                if hook_history["status"] == "ok"
                else ("Hook-backed SQLite history is disabled." if hook_history["status"] == "disabled" else "Hook-backed SQLite history needs installation or DB access."),
                details={
                    "category": "hook_history",
                    "installed": hook_history["installed"],
                    "dbWritable": hook_history["dbWritable"],
                    "threadCount": hook_history["threadCount"],
                    "turnCount": hook_history["turnCount"],
                    "lastHookEventAt": hook_history["lastHookEventAt"],
                },
                suggested_action="refresh_catalog_and_history" if hook_history["status"] == "ok" else "validate_paths_and_config",
            )
        )
        for name, path in (
            ("codex_home", self.config.codex_home),
            ("sessions_dir", self.config.sessions_dir),
            ("kb_history_projects_root", self.config.kb_history_projects_root),
        ):
            exists = path.exists()
            checks.append(
                diagnostic_check(
                    name,
                    "ok" if exists else "warning",
                    f"{name} exists." if exists else f"{name} is missing.",
                    details={"path": str(path), "category": "project_path"},
                    suggested_action="validate_paths_and_config",
                )
            )
        missing_roots = [str(root) for root in self.config.allowed_roots if not root.exists()]
        checks.append(
            diagnostic_check(
                "allowed_roots",
                "ok" if not missing_roots else "error",
                "All allowed roots exist." if not missing_roots else "Some allowed roots are missing.",
                details={"missingRoots": missing_roots, "category": "project_path"},
                suggested_action="validate_paths_and_config",
            )
        )
        running = bool(app_status.get("running"))
        checks.append(
            diagnostic_check(
                "app_server_running",
                "ok" if running else "warning",
                "MCP-owned Codex app-server is running." if running else "MCP-owned Codex app-server is not running.",
                details={"category": "app_server_not_running", "started": app_status.get("started")},
                suggested_action="restart_app_server_idle",
            )
        )
        if pending_interactions:
            checks.append(
                diagnostic_check(
                    "pending_interactions",
                    "warning",
                    f"{len(pending_interactions)} pending interactions are waiting for OpenClaw.",
                    details={"count": len(pending_interactions), "category": "pending_interaction_stale"},
                    suggested_action="expire_stale_pending_interactions",
                )
            )
        else:
            checks.append(diagnostic_check("pending_interactions", "ok", "No pending interactions found."))
        stale_operations = stale_operations or []
        if stale_operations:
            checks.append(
                diagnostic_check(
                    "stale_operations",
                    "warning",
                    f"{len(stale_operations)} active operations have not updated recently.",
                    details={
                        "count": len(stale_operations),
                        "operationIds": [row.get("operation_id") for row in stale_operations[:20]],
                        "category": "stale_operation",
                    },
                    suggested_action="recover_stale_operations",
                )
            )
        else:
            checks.append(diagnostic_check("stale_operations", "ok", "No stale active operations found."))
        orphaned = [item for item in workflows if item and item.get("phase") == "orphaned_after_app_server_exit"]
        if orphaned:
            checks.append(
                diagnostic_check(
                    "orphaned_workflows",
                    "warning",
                    f"{len(orphaned)} workflows are orphaned after app-server exit.",
                    details={"count": len(orphaned), "category": "app_server_stdout_closed"},
                    suggested_action="restart_app_server_idle",
                )
            )
        recent_errors = [
            item
            for item in event_pointers
            if str(item.get("method") or "").casefold() in {"turn/error", "error"} or "error" in str(item.get("method") or "").casefold()
        ]
        checks.append(
            diagnostic_check(
                "recent_app_server_events",
                "warning" if recent_errors else "ok",
                f"{len(recent_errors)} recent error events found." if recent_errors else "No recent app-server error events found.",
                details={"errorEventCount": len(recent_errors), "category": "app_server_timeout" if recent_errors else "events"},
            )
        )
        checks.append(
            diagnostic_check(
                "mcp_log",
                "ok" if log_path.exists() else "warning",
                "MCP log file exists." if log_path.exists() else "MCP log file does not exist.",
                details={"path": str(log_path), "category": "logs"},
            )
        )
        return checks

    def _search_index_snapshot(self) -> dict[str, Any]:
        docs = self.storage.connection.execute("SELECT COUNT(*) AS count FROM chat_search_docs").fetchone()
        transcripts = self.storage.connection.execute("SELECT COUNT(*) AS count FROM chat_search_transcripts").fetchone()
        latest = self.storage.connection.execute(
            "SELECT indexed_at FROM chat_search_transcripts ORDER BY indexed_at DESC LIMIT 1"
        ).fetchone()
        return {
            "docCount": int(docs["count"] if docs is not None else 0),
            "transcriptCheckpointCount": int(transcripts["count"] if transcripts is not None else 0),
            "latestIndexedAt": latest["indexed_at"] if latest is not None else None,
        }

    def _hook_history_snapshot(self) -> dict[str, Any]:
        storage_status = self.storage.hook_history_status()
        try:
            install_status = installed_hook_status(codex_home=self.config.codex_home)
        except Exception as exc:  # noqa: BLE001 - diagnostics must stay best-effort.
            install_status = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
        db_writable = False
        db_error = None
        try:
            self.storage.connection.execute("SELECT 1").fetchone()
            db_writable = True
        except Exception as exc:  # noqa: BLE001 - compact diagnostic.
            db_error = f"{type(exc).__name__}: {exc}"
        warnings: list[str] = []
        if self.config.hook_history_enabled and not bool(install_status.get("installed")):
            warnings.append("Codex hooks are not installed through codex-control-plane-mcp-hooks.")
        if not db_writable:
            warnings.append("MCP state DB is not writable from this process.")
        status = "disabled"
        if self.config.hook_history_enabled:
            status = "ok" if bool(install_status.get("installed")) and db_writable else "warning"
        return {
            "enabled": self.config.hook_history_enabled,
            "status": status,
            "installed": bool(install_status.get("installed")),
            "events": install_status.get("events") or {},
            "hooksJson": install_status.get("hooksJson"),
            "configPath": install_status.get("configPath"),
            "dbWritable": db_writable,
            "dbError": db_error,
            "threadCount": storage_status["threadCount"],
            "turnCount": storage_status["turnCount"],
            "messageCount": storage_status["messageCount"],
            "lastHookEventAt": storage_status["lastHookEventAt"],
            "warnings": warnings,
        }

    def _repair_cleanup_prompt_submissions(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        older_than_days = _bounded_int(args.get("older_than_days", 30), 1, 3650)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        result = self.storage.cleanup_prompt_submissions(older_than=cutoff, dry_run=dry_run)
        return {
            "dryRun": dry_run,
            "olderThanDays": older_than_days,
            **result,
        }

    def _repair_recover_stale_operations(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        stale_after = _bounded_int(args.get("stale_after_minutes", 30), 1, 10080)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_after)).isoformat()
        return self.storage.recover_stale_operations(
            stale_before=cutoff,
            now=_now_iso(),
            operation_id=_optional_string(args.get("operation_id")),
            dry_run=dry_run,
            limit=50,
        )

    def _repair_refresh_catalog_and_history(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        before_index = self._search_index_snapshot()
        hook_history = self._hook_history_snapshot()
        if dry_run:
            return {
                "dryRun": True,
                "wouldRefreshCatalog": True,
                "wouldRefreshSearchIndex": True,
                "catalog": {"projects": len(self.catalog.projects), "chats": len(self.catalog.chats)},
                "searchIndex": before_index,
                "hookHistory": hook_history,
                "changed": False,
            }
        self.catalog.refresh()
        status = SearchIndex(self.config, self.storage, self.catalog).refresh(
            include_archived=True,
            time_budget_seconds=min(_bounded_int(args.get("timeout_seconds", 30), 1, 120), 60),
        )
        after_index = self._search_index_snapshot()
        return {
            "dryRun": False,
            "catalog": {"projects": len(self.catalog.projects), "chats": len(self.catalog.chats)},
            "searchIndexBefore": before_index,
            "searchIndexAfter": after_index,
            "searchIndexRefresh": status.to_tool(),
            "hookHistory": self._hook_history_snapshot(),
            "changed": True,
        }

    def _repair_mark_orphaned_after_exit(self, args: dict[str, Any], *, dry_run: bool, force: bool) -> dict[str, Any]:
        app_status = self.codex_get_app_server_status({"include_recent_events": False})
        running = bool(app_status.get("running"))
        if running and not force and not dry_run:
            raise invalid_argument(
                "mark_orphaned_after_exit requires stopped app-server or force=true.",
                action="mark_orphaned_after_exit",
                app_server_running=running,
            )
        operation_id = _optional_string(args.get("operation_id"))
        workflow_id = _optional_string(args.get("workflow_id"))
        thread_id = _optional_string(args.get("thread_id"))
        turn_id = _optional_string(args.get("turn_id"))
        operations: list[dict[str, Any]] = []
        workflow = self.storage.get_workflow(workflow_id) if workflow_id else None
        if operation_id:
            operation = self.storage.get_operation(operation_id)
            if operation is not None:
                operations.append(operation)
        elif workflow_id:
            operations.extend(self.storage.list_operations_for_workflow(workflow_id, limit=20))
        else:
            operations.extend(self.storage.list_stale_operations(stale_before=_now_iso(), limit=50))
        if workflow is not None:
            thread_id = thread_id or _optional_string(workflow.get("thread_id"))
            turn_id = turn_id or _optional_string(workflow.get("execution_turn_id")) or _optional_string(workflow.get("plan_turn_id"))
        target_turn_ids = {turn_id} if turn_id else set()
        for operation in operations:
            operation_turn_id = _optional_string(operation.get("turn_id"))
            if operation_turn_id:
                target_turn_ids.add(operation_turn_id)
            thread_id = thread_id or _optional_string(operation.get("thread_id"))
            workflow_id = workflow_id or _optional_string(operation.get("workflow_id"))
        running_turns = []
        for turn in self.storage.get_running_tracked_turns():
            if thread_id and turn.get("thread_id") != thread_id:
                continue
            if target_turn_ids and turn.get("turn_id") not in target_turn_ids:
                continue
            running_turns.append(turn)
        preview = {
            "dryRun": dry_run,
            "appServerRunning": running,
            "requiresForce": running,
            "operationIds": [row.get("operation_id") for row in operations if row.get("status") in OPERATION_ACTIVE_STATUSES],
            "turnIds": [row.get("turn_id") for row in running_turns],
            "workflowId": workflow_id,
        }
        if dry_run:
            return {**preview, "wouldMarkOrphaned": True, "changed": False}
        now = _now_iso()
        marked_operations: list[str] = []
        for operation in operations:
            if str(operation.get("status") or "") not in OPERATION_ACTIVE_STATUSES:
                continue
            row_operation_id = str(operation["operation_id"])
            next_status = "unknown_after_app_server_exit" if operation.get("turn_id") else "orphaned"
            self.storage.update_operation(
                row_operation_id,
                status=next_status,
                phase=next_status,
                completed_at=now,
                updated_at=now,
                last_error="Marked orphaned after app-server exit by codex_repair_issue.",
                lease_owner=None,
                lease_expires_at=None,
                last_heartbeat_at=None,
            )
            self.storage.update_prompt_submission_by_operation(row_operation_id, status=next_status, updated_at=now)
            marked_operations.append(row_operation_id)
        marked_turns: list[str] = []
        for turn in running_turns:
            row_turn_id = str(turn.get("turn_id") or "")
            if not row_turn_id:
                continue
            self.storage.update_tracked_turn_status(
                row_turn_id,
                status="unknown_after_app_server_exit",
                updated_at=now,
                completed_at=now,
                last_error="Marked orphaned after app-server exit by codex_repair_issue.",
            )
            marked_turns.append(row_turn_id)
        marked_workflow = None
        if workflow_id:
            workflow = self.storage.get_workflow(workflow_id)
            if workflow is not None and str(workflow.get("status") or "") not in {"completed", "failed", "orphaned_after_app_server_exit"}:
                self.storage.update_workflow(
                    workflow_id,
                    phase="orphaned_after_app_server_exit",
                    status="orphaned_after_app_server_exit",
                    last_error="Marked orphaned after app-server exit by codex_repair_issue.",
                    updated_at=now,
                    completed_at=now,
                )
                marked_workflow = workflow_id
        orphaned_interactions = 0
        if not thread_id and not target_turn_ids:
            process_generation = app_status.get("processGeneration")
            process_generation = int(process_generation) if process_generation not in (None, "") else None
            orphaned_interactions = self.storage.mark_pending_interactions_orphaned(
                process_generation=process_generation,
                reason="Marked orphaned after app-server exit by codex_repair_issue.",
                resolved_at=now,
            )
        return {
            **preview,
            "markedOperationIds": marked_operations,
            "markedTurnIds": marked_turns,
            "markedWorkflowId": marked_workflow,
            "orphanedInteractions": orphaned_interactions,
            "changed": bool(marked_operations or marked_turns or marked_workflow or orphaned_interactions),
        }

    def _repair_mark_stale_turns(self, args: dict[str, Any]) -> dict[str, Any]:
        stale_after = _bounded_int(args.get("stale_after_minutes", 120), 1, 10080)
        thread_id = _optional_string(args.get("thread_id"))
        turn_id = _optional_string(args.get("turn_id"))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after)
        affected: list[str] = []
        for turn in self.storage.get_running_tracked_turns():
            if thread_id and turn.get("thread_id") != thread_id:
                continue
            if turn_id and turn.get("turn_id") != turn_id:
                continue
            updated = _parse_iso_datetime(turn.get("updated_at") or turn.get("started_at"))
            if updated is not None and updated > cutoff:
                continue
            affected.append(str(turn["turn_id"]))
            self.storage.update_tracked_turn_status(
                str(turn["turn_id"]),
                status="unknown_after_app_server_exit",
                updated_at=_now_iso(),
                completed_at=_now_iso(),
                last_error="Marked stale by codex_repair_issue.",
            )
        return {"changed": bool(affected), "markedTurnIds": affected, "staleAfterMinutes": stale_after}

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


def _operation_request_payload(args: dict[str, Any], *, operation_type: str, message: str) -> dict[str, Any]:
    keys = {
        "project_id",
        "chat_id",
        "workflow_id",
        "title",
        "cwd",
        "model",
        "collaboration_mode",
        "approval_policy",
        "sandbox",
        "force",
        "timeout_seconds",
        "first_message_max_chars",
    }
    payload = {
        "operation_type": operation_type,
        "message": message,
    }
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
        "last_error": row.get("last_error"),
        "lastError": row.get("last_error"),
        "appServerGeneration": row.get("app_server_generation"),
        "request": public_request,
    }
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
    return result


def _staleness_seconds(updated_at: str) -> int | None:
    parsed = _parse_iso_datetime(updated_at)
    if parsed is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _operation_next_action(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    if status == "queued":
        return "wait_for_background_worker"
    if status in {"starting_app_server", "starting_thread", "starting_turn"}:
        return "poll_operation_status"
    if status in {"waiting_for_approval", "waiting_for_user_input"}:
        return "answer_pending_interaction"
    if status == "running":
        return "poll_turn_status"
    if status == "completed":
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


def _redacted_preview(value: str, limit: int = 120) -> str:
    text = value.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-[redacted]", text)
    text = re.sub(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b", "[telegram-token-redacted]", text)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _approval_policy_for_send(value: Any, thread_row: Any, default_policy: str) -> str:
    selected = str(value or default_policy or "never")
    if value == "respect_existing":
        existing = getattr(thread_row, "approval_mode", None)
        if existing in {"never", "on-request", "on-failure", "untrusted"}:
            return str(existing)
        if default_policy in {"never", "on-request", "on-failure", "untrusted"}:
            return default_policy
        return "never"
    if selected == "never_auto_approve":
        return "never"
    if selected == "ask_openclaw":
        return "on-request"
    if selected in {"never", "on-request", "on-failure", "untrusted"}:
        return selected
    return "never"


def _approval_policy_for_start(value: Any, default_policy: str) -> str:
    selected = str(value or default_policy or "never")
    if selected == "ask_openclaw":
        return "on-request"
    if selected in {"never", "on-request", "on-failure", "untrusted"}:
        return selected
    return "never"


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
    return "danger-full-access"


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
    text, meta = _truncate_text(str(row.get("text") or ""), max_chars)
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
        "created_at": row.get("created_at"),
        "createdAt": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "updatedAt": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
        "completedAt": row.get("completed_at"),
        "truncated": bool(meta.get("truncated")),
        "originalChars": meta.get("original_chars"),
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
        return "plan_ready", "plan_ready", None
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


def _next_workflow_action(phase: str) -> str:
    if phase == "plan_ready":
        return "execute_plan"
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


def _workflow_poll_seconds(phase: str) -> int:
    if phase in {"waiting_for_approval", "waiting_for_user_input"}:
        return 15
    if phase == "planning":
        return 10
    if phase == "executing":
        return 30
    if phase in {"plan_ready", "completed", "failed", "orphaned_after_app_server_exit"}:
        return 0
    return 15


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
