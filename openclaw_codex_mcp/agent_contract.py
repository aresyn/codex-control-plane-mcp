from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


GUIDE_VERSION = "codex-mcp-agent-guide/v1"


ROLE_READ_ONLY = "read_only"
ROLE_PRIMARY_WRITE = "primary_write"
ROLE_POLL_STATUS = "poll_status"
ROLE_WORKFLOW = "workflow"
ROLE_DIAGNOSTICS = "diagnostics"
ROLE_LIFECYCLE = "lifecycle"
ROLE_COMPATIBILITY = "compatibility"


TOOL_CONTRACT: dict[str, dict[str, Any]] = {
    "codex_list_projects": {
        "role": ROLE_READ_ONLY,
        "description": "List known Codex projects from registry, hook history, transcripts, and cached Codex state. Use this before preflight or submit when you need a project reference; later tools accept projectId, project name, or project path and return canonical projectId. Next call codex_preflight_project_run for a concrete project.",
        "nextTools": ["codex_preflight_project_run", "codex_list_project_chats", "codex_submit_task"],
        "passiveRead": True,
    },
    "codex_list_project_chats": {
        "role": ROLE_READ_ONLY,
        "description": "List chats for one project from the bounded read model. Use this to find existing threads before continuation or review. Next call codex_get_chat_status, codex_get_chat, or codex_submit_task.",
        "nextTools": ["codex_get_chat_status", "codex_get_chat", "codex_submit_task"],
        "passiveRead": True,
    },
    "codex_list_active_chats": {
        "role": ROLE_READ_ONLY,
        "description": "List chats that look active from tracked, hook, transcript, or cached evidence. Use this for operator inspection, not for creating retries. Next call codex_get_turn_status or codex_get_operation_status when ids are available.",
        "nextTools": ["codex_get_turn_status", "codex_get_operation_status"],
        "passiveRead": True,
    },
    "codex_search_chats": {
        "role": ROLE_READ_ONLY,
        "description": "Search chat history through the MCP-owned index and safe fallback sources. Use this for discovery or recovery when ids were lost. Do not use search results as proof that a turn is still active.",
        "nextTools": ["codex_get_chat_status", "codex_get_chat", "codex_get_turn_status"],
        "passiveRead": True,
    },
    "codex_get_chat_status": {
        "role": ROLE_READ_ONLY,
        "description": "Read lightweight chat status and safe previews. Use this to inspect a known thread without starting live work. Next call codex_get_chat for content or codex_submit_task for a new operation.",
        "nextTools": ["codex_get_chat", "codex_submit_task"],
        "passiveRead": True,
    },
    "codex_get_chat": {
        "role": ROLE_READ_ONLY,
        "description": "Read bounded chat content from hook history, transcripts, or legacy fallback. Use this for context recovery and final report inspection. It is not a write path and should not trigger retries.",
        "nextTools": ["codex_get_chat_status", "codex_submit_task"],
        "passiveRead": True,
    },
    "codex_send_message": {
        "role": ROLE_COMPATIBILITY,
        "description": "Compatibility write for sending a message to an existing Codex thread. Prefer codex_submit_task with operation_type='send_message' for durable long work. In client mode this delegates to the durable queue.",
        "nextTools": ["codex_submit_task", "codex_get_operation_status"],
        "avoidWhen": ["Use codex_submit_task for new automation and retry-safe writes."],
        "mayStartTurn": True,
        "idempotency": "recommended",
    },
    "codex_start_chat": {
        "role": ROLE_COMPATIBILITY,
        "description": "Compatibility write for starting a new Codex chat. Prefer codex_submit_task with operation_type='start_chat' for durable long work. In client mode this delegates to the durable queue.",
        "nextTools": ["codex_submit_task", "codex_get_operation_status"],
        "avoidWhen": ["Use codex_submit_task for new automation and retry-safe writes."],
        "mayStartTurn": True,
        "idempotency": "recommended",
    },
    "codex_start_plan_workflow": {
        "role": ROLE_WORKFLOW,
        "description": "Start a durable Plan Mode workflow and return workflowId immediately. Use this when a plan must be prepared before implementation. Next poll codex_get_workflow_status, then call codex_approve_plan when latestPlan is ready.",
        "nextTools": ["codex_get_workflow_status", "codex_approve_plan"],
        "mayStartTurn": True,
        "idempotency": "recommended",
    },
    "codex_start_review_workflow": {
        "role": ROLE_WORKFLOW,
        "description": "Start a durable Codex review workflow and return workflowId immediately. Use this for code review tasks. Next poll codex_get_workflow_status for progress and final report.",
        "nextTools": ["codex_get_workflow_status"],
        "mayStartTurn": True,
        "idempotency": "recommended",
    },
    "codex_get_workflow_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Poll workflow state from storage by default. Use this for Plan Mode, execution, and review workflows. Follow nextRecommendedAction and do not create replacement work unless guidance tells you to.",
        "nextTools": ["codex_approve_plan", "codex_collect_diagnostics", "codex_repair_issue"],
        "passiveRead": True,
    },
    "codex_adopt_workflow_plan": {
        "role": ROLE_WORKFLOW,
        "description": "Adopt a valid newer Plan Mode candidate already present in the workflow thread. Use this only when status or diagnostics reports an adoptable plan. Next poll codex_get_workflow_status.",
        "nextTools": ["codex_get_workflow_status", "codex_approve_plan"],
        "idempotency": "recommended",
    },
    "codex_approve_plan": {
        "role": ROLE_WORKFLOW,
        "description": "Approve the latest ready plan and queue execution. Use this after codex_get_workflow_status reports plan_ready and a valid latestPlan. Next poll codex_get_workflow_status with the same workflowId.",
        "nextTools": ["codex_get_workflow_status"],
        "mayStartTurn": True,
        "idempotency": "recommended",
    },
    "codex_preflight_project_run": {
        "role": ROLE_READ_ONLY,
        "description": "Check whether a project is safe to use before a Codex run. Use this after project discovery and before write operations. Do not treat skipped worker-managed account checks as hard auth failures.",
        "nextTools": ["codex_submit_task", "codex_start_plan_workflow", "codex_collect_diagnostics"],
        "passiveRead": True,
    },
    "codex_get_turn_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Read one tracked Codex turn, including safe progress and terminal evidence. Use this when you have threadId and turnId. Do not infer stalled state from row age alone.",
        "nextTools": ["codex_collect_diagnostics", "codex_interrupt_turn"],
        "passiveRead": True,
    },
    "codex_execute_plan": {
        "role": ROLE_COMPATIBILITY,
        "description": "Compatibility write for executing an approved plan. Prefer codex_approve_plan or codex_submit_task with operation_type='execute_plan'. In client mode this delegates to durable workflow execution.",
        "nextTools": ["codex_approve_plan", "codex_get_workflow_status", "codex_submit_task"],
        "avoidWhen": ["Use codex_approve_plan for managed Plan Mode workflows."],
        "mayStartTurn": True,
        "idempotency": "recommended",
    },
    "codex_submit_task": {
        "role": ROLE_PRIMARY_WRITE,
        "description": "Queue a durable Codex write operation and return operationId immediately. For project-scoped work, pass project_id from codex_list_projects.projectId; project name or project path are accepted aliases and MCP stores the canonical projectId. Always pass client_request_id and poll codex_get_operation_status.",
        "nextTools": ["codex_get_operation_status", "codex_list_pending_interactions", "codex_collect_diagnostics"],
        "mayStartTurn": True,
        "idempotency": "required",
    },
    "codex_get_operation_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Poll a durable operation from storage. Use this after codex_submit_task and follow nextRecommendedAction, pollRecommended, queueState, and agentGuidance. Never create a new retry while an existing operation is active.",
        "nextTools": ["codex_list_pending_interactions", "codex_collect_diagnostics", "codex_repair_issue"],
        "passiveRead": True,
    },
    "codex_list_pending_interactions": {
        "role": ROLE_READ_ONLY,
        "description": "List pending approvals, input requests, or elicitation requests. Use this when operation or workflow status reports pending interaction. Next answer with codex_answer_pending_interaction or ask a human.",
        "nextTools": ["codex_answer_pending_interaction", "codex_get_operation_status", "codex_get_workflow_status"],
        "passiveRead": True,
    },
    "codex_answer_pending_interaction": {
        "role": ROLE_LIFECYCLE,
        "description": "Answer one pending Codex interaction so a turn can continue. Use this only for a listed interaction id. Next poll the owning operation, turn, or workflow.",
        "nextTools": ["codex_get_operation_status", "codex_get_workflow_status", "codex_get_turn_status"],
        "idempotency": "recommended",
    },
    "codex_interrupt_turn": {
        "role": ROLE_LIFECYCLE,
        "description": "Interrupt a running Codex turn by direct ids or durable operation/workflow context. Use this for explicit cancellation or stop conditions. Next poll status until terminal evidence is visible.",
        "nextTools": ["codex_get_operation_status", "codex_get_turn_status", "codex_collect_diagnostics"],
        "idempotency": "recommended",
    },
    "codex_archive_thread": {
        "role": ROLE_LIFECYCLE,
        "description": "Archive a known Codex thread through the worker or app-server command lane. Use this only when the thread has no active work. Next poll codex_get_worker_command_status when commandId is returned.",
        "nextTools": ["codex_get_worker_command_status", "codex_get_chat_status"],
        "idempotency": "recommended",
    },
    "codex_unarchive_thread": {
        "role": ROLE_LIFECYCLE,
        "description": "Unarchive a known Codex thread through the worker or app-server command lane. Use this only for an existing archived thread. Next poll codex_get_worker_command_status when commandId is returned.",
        "nextTools": ["codex_get_worker_command_status", "codex_get_chat_status"],
        "idempotency": "recommended",
    },
    "codex_start_thread_compaction": {
        "role": ROLE_LIFECYCLE,
        "description": "Start context compaction for a known thread and return actionId. Use this after active work is terminal. Next poll codex_get_thread_compaction_status.",
        "nextTools": ["codex_get_thread_compaction_status"],
        "idempotency": "recommended",
    },
    "codex_get_thread_compaction_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Poll a thread compaction action. Use this with actionId from codex_start_thread_compaction. It is passive and should return a bounded status or guidance.",
        "nextTools": ["codex_collect_diagnostics", "codex_analyze_issue"],
        "passiveRead": True,
    },
    "codex_get_worker_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Read central worker heartbeat and execution-mode state without starting app-server. Use this when health or queue guidance says inspect_worker_health. Next compare with queue and concurrency status.",
        "nextTools": ["codex_get_queue_status", "codex_get_concurrency_status", "codex_get_app_server_status"],
        "passiveRead": True,
    },
    "codex_get_queue_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Read durable queue state, queued reasons, running operations, and worker assignment. Use this to understand slot pressure or lock waits. Do not retry when queued work already exists.",
        "nextTools": ["codex_get_concurrency_status", "codex_get_operation_status", "codex_collect_diagnostics"],
        "passiveRead": True,
    },
    "codex_get_concurrency_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Read active turn counts and scheduler resource locks. Use this with queue status when diagnosing parallel work. Active locks are not a retry instruction by themselves.",
        "nextTools": ["codex_get_queue_status", "codex_get_worker_status", "codex_collect_diagnostics"],
        "passiveRead": True,
    },
    "codex_get_worker_command_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Poll a worker command created by a client-mode control action. Use this for archive, unarchive, compaction, restart, runtime refresh, and delegated lifecycle commands. Keep include_result=false unless the result is needed.",
        "nextTools": ["codex_get_operation_status", "codex_get_thread_compaction_status", "codex_collect_diagnostics"],
        "passiveRead": True,
    },
    "codex_restart_app_server": {
        "role": ROLE_LIFECYCLE,
        "description": "Restart only the MCP-owned codex-app-server subprocess. Use this only when guidance recommends restart and active work is absent or explicitly handled. In client mode this delegates to the worker command lane.",
        "nextTools": ["codex_get_worker_command_status", "codex_get_app_server_status", "codex_health_summary"],
        "avoidWhen": ["Do not restart blindly while active turns, pending requests, or pending interactions exist."],
        "idempotency": "recommended",
    },
    "codex_get_app_server_status": {
        "role": ROLE_POLL_STATUS,
        "description": "Read MCP-owned app-server status without starting it. Use this with worker, queue, and concurrency status to verify active work. In client mode prefer worker-derived active turns over local guesses.",
        "nextTools": ["codex_get_worker_status", "codex_get_queue_status", "codex_get_concurrency_status"],
        "passiveRead": True,
    },
    "codex_get_runtime_capabilities": {
        "role": ROLE_READ_ONLY,
        "description": "Read compact runtime capabilities, models, permissions, hooks, account state, and supported methods. Use this after health or before new work. In client mode refresh queues a worker command.",
        "nextTools": ["codex_get_worker_command_status", "codex_preflight_project_run"],
        "passiveRead": True,
    },
    "codex_health_summary": {
        "role": ROLE_READ_ONLY,
        "description": "Read compact MCP readiness and contract metadata. Use this first on startup, reconnect, and after MCP restart. Next inspect runtime capabilities or follow agentGuidance if health is degraded.",
        "nextTools": ["codex_get_runtime_capabilities", "codex_preflight_project_run", "codex_collect_diagnostics"],
        "passiveRead": True,
    },
    "codex_get_agent_contract": {
        "role": ROLE_READ_ONLY,
        "description": "Read the machine-readable agent guide for this MCP server. Use this when tools/list metadata was unavailable or when a client wants the full contract examples. It is passive and the next normal startup call is codex_health_summary.",
        "nextTools": ["codex_health_summary", "codex_get_runtime_capabilities", "codex_preflight_project_run"],
        "passiveRead": True,
    },
    "codex_collect_diagnostics": {
        "role": ROLE_DIAGNOSTICS,
        "description": "Collect a scoped diagnostic snapshot with compact evidence and guidance. Use this before repair when status reports failed, stale, orphaned, or degraded state. It does not execute repairs.",
        "nextTools": ["codex_analyze_issue", "codex_repair_issue"],
        "passiveRead": True,
    },
    "codex_get_diagnostic_logs": {
        "role": ROLE_DIAGNOSTICS,
        "description": "Read redacted diagnostic log and app-server audit entries with filters. Use this only for targeted troubleshooting, not normal polling. Raw payload mode is for local audit and remains secret-redacted.",
        "nextTools": ["codex_analyze_issue", "codex_collect_diagnostics"],
        "passiveRead": True,
    },
    "codex_analyze_issue": {
        "role": ROLE_DIAGNOSTICS,
        "description": "Analyze scoped diagnostics and recommend safe next actions. Use this after collect_diagnostics or when a human needs a compact root-cause summary. Follow agentGuidance rather than inventing retries.",
        "nextTools": ["codex_collect_diagnostics", "codex_repair_issue"],
        "passiveRead": True,
    },
    "codex_repair_issue": {
        "role": ROLE_DIAGNOSTICS,
        "description": "Run an allowlisted repair action with dry-run first by default. Use this only when diagnostics or agentGuidance recommends a specific action. Stop when loopGuard.allowed is false.",
        "nextTools": ["codex_get_operation_status", "codex_get_workflow_status", "codex_collect_diagnostics"],
        "idempotency": "recommended",
    },
}


TOOL_GROUPS: list[dict[str, Any]] = [
    {
        "id": "readiness",
        "purpose": "Check server compatibility, runtime readiness, and project safety before new work.",
        "preferredTools": ["codex_get_agent_contract", "codex_health_summary", "codex_get_runtime_capabilities", "codex_preflight_project_run"],
    },
    {
        "id": "durable_operations",
        "purpose": "Submit long-running Codex work and poll it without holding one MCP call open.",
        "preferredTools": ["codex_submit_task", "codex_get_operation_status"],
    },
    {
        "id": "workflows",
        "purpose": "Run Plan Mode and review workflows as durable state machines.",
        "preferredTools": ["codex_start_plan_workflow", "codex_start_review_workflow", "codex_get_workflow_status", "codex_approve_plan"],
    },
    {
        "id": "worker_scheduler",
        "purpose": "Inspect central worker health, queue state, concurrency limits, app-server state, and worker commands.",
        "preferredTools": ["codex_get_worker_status", "codex_get_queue_status", "codex_get_concurrency_status", "codex_get_worker_command_status"],
    },
    {
        "id": "read_history",
        "purpose": "Discover projects, chats, active turns, and bounded history without starting live work.",
        "preferredTools": ["codex_list_projects", "codex_list_project_chats", "codex_search_chats", "codex_get_chat_status", "codex_get_chat", "codex_get_turn_status"],
    },
    {
        "id": "active_turn_control",
        "purpose": "Handle pending interactions, steer active turns, and interrupt test or user-requested turns.",
        "preferredTools": ["codex_list_pending_interactions", "codex_answer_pending_interaction", "codex_submit_task", "codex_interrupt_turn"],
    },
    {
        "id": "lifecycle",
        "purpose": "Archive, unarchive, compact, and poll thread lifecycle actions.",
        "preferredTools": ["codex_archive_thread", "codex_unarchive_thread", "codex_start_thread_compaction", "codex_get_thread_compaction_status"],
    },
    {
        "id": "diagnostics_repair",
        "purpose": "Collect scoped diagnostics, analyze likely cause, and run guarded repair actions.",
        "preferredTools": ["codex_collect_diagnostics", "codex_analyze_issue", "codex_repair_issue"],
    },
    {
        "id": "compatibility",
        "purpose": "Compatibility write tools kept for older clients. New agents should use durable operations and workflows instead.",
        "preferredTools": ["codex_start_chat", "codex_send_message", "codex_execute_plan"],
    },
]


CAPABILITY_MAP: dict[str, Any] = {
    "selfDescription": {
        "contractTool": "codex_get_agent_contract",
        "startupTool": "codex_health_summary",
        "primaryWriteTool": "codex_submit_task",
    },
    "durableOperations": {
        "primaryTool": "codex_submit_task",
        "statusTool": "codex_get_operation_status",
        "operationTypes": ["start_chat", "send_message", "execute_plan", "steer_turn", "fork_thread"],
    },
    "planWorkflow": {
        "startTool": "codex_start_plan_workflow",
        "statusTool": "codex_get_workflow_status",
        "approveTool": "codex_approve_plan",
    },
    "reviewWorkflow": {
        "startTool": "codex_start_review_workflow",
        "statusTool": "codex_get_workflow_status",
    },
    "workerScheduler": {
        "workerStatusTool": "codex_get_worker_status",
        "queueStatusTool": "codex_get_queue_status",
        "concurrencyStatusTool": "codex_get_concurrency_status",
        "commandStatusTool": "codex_get_worker_command_status",
    },
    "diagnostics": {
        "collectTool": "codex_collect_diagnostics",
        "analyzeTool": "codex_analyze_issue",
        "repairTool": "codex_repair_issue",
        "rawAuditTool": "codex_get_diagnostic_logs",
    },
}


USAGE_FLOWS: list[dict[str, Any]] = [
    {
        "id": "startup",
        "goal": "Verify compatibility and readiness before submitting work.",
        "steps": ["codex_health_summary", "codex_get_runtime_capabilities", "codex_list_projects", "codex_preflight_project_run"],
    },
    {
        "id": "new_task",
        "goal": "Start durable work without holding a long MCP call open.",
        "steps": ["codex_submit_task", "codex_get_operation_status", "codex_list_pending_interactions"],
    },
    {
        "id": "long_running_poll",
        "goal": "Track active work and avoid duplicate retries.",
        "steps": ["codex_get_operation_status", "codex_get_queue_status", "codex_get_concurrency_status", "codex_collect_diagnostics"],
    },
    {
        "id": "plan_mode",
        "goal": "Prepare a plan, approve it, and follow execution.",
        "steps": ["codex_start_plan_workflow", "codex_get_workflow_status", "codex_approve_plan", "codex_get_workflow_status"],
    },
    {
        "id": "review",
        "goal": "Run a Codex review and poll report state.",
        "steps": ["codex_start_review_workflow", "codex_get_workflow_status"],
    },
    {
        "id": "steer",
        "goal": "Add context to an active turn without creating a new turn.",
        "steps": ["codex_get_operation_status", "codex_submit_task", "codex_get_operation_status"],
    },
    {
        "id": "interrupt",
        "goal": "Stop an active turn intentionally.",
        "steps": ["codex_interrupt_turn", "codex_get_operation_status", "codex_collect_diagnostics"],
    },
    {
        "id": "lifecycle",
        "goal": "Run thread archive, unarchive, or compaction through pollable commands.",
        "steps": ["codex_archive_thread", "codex_get_worker_command_status", "codex_start_thread_compaction", "codex_get_thread_compaction_status"],
    },
    {
        "id": "diagnostics_recovery",
        "goal": "Recover only through scoped diagnostics and guarded repair.",
        "steps": ["codex_collect_diagnostics", "codex_analyze_issue", "codex_repair_issue"],
    },
]


GLOBAL_RULES: list[str] = [
    "Call codex_health_summary on startup, reconnect, and after MCP restart.",
    "Use codex_submit_task for long-running writes and poll codex_get_operation_status.",
    "Always pass a stable client_request_id for retry-safe write requests.",
    "After CODEX_TIMEOUT or CODEX_STATE_BUSY, retry the same client_request_id instead of minting a new request.",
    "Do not create a replacement operation while an existing operation is active, queued, or recoverable.",
    "Follow agentGuidance.instructions before marking work blocked.",
    "Run risky repair actions with dry_run=true first.",
    "Stop automatic recovery when loopGuard.allowed is false.",
    "In client mode, write and lifecycle actions are delegated to the central worker.",
    "Project arguments accept the canonical projectId, the listed project name, or the project path; MCP canonicalizes them before durable writes.",
]


RUNTIME_LIMITS: dict[str, Any] = {
    "readSurface": "Status and read tools are passive and bounded by default.",
    "redaction": "Public responses are agent_safe by default: no raw prompts, raw private paths, tokens, account ids, or exact token counts.",
    "planModeSandboxFloor": "Plan Mode never sends read-only to app-server; MCP raises it to workspace-write or a configured stronger policy.",
    "workerSlots": {
        "globalDefault": 4,
        "perProjectDefault": 3,
        "perAgentDefault": 3,
        "perThreadDefault": 1,
        "writeWithoutResourceKeys": "workspace-write and danger-full-access turns take a broad project write lock unless resource_keys are provided.",
    },
    "terminalEvidence": "Operations become terminal only from trusted app-server, hook Stop, or transcript terminal evidence.",
}


FULL_EXAMPLES: dict[str, Any] = {
    "durableStartChat": {
        "tool": "codex_submit_task",
        "arguments": {
            "operation_type": "start_chat",
            "project_id": "<projectId|projectName|projectPath>",
            "message": "MCP task text",
            "client_request_id": "<stable id>",
            "agent_id": "<agent id>",
            "resource_keys": ["project:area"],
        },
        "next": {"tool": "codex_get_operation_status", "arguments": {"operation_id": "<operationId>"}},
    },
    "planWorkflow": {
        "start": {"tool": "codex_start_plan_workflow", "arguments": {"project_id": "<projectId|projectName|projectPath>", "message": "Prepare a plan"}},
        "poll": {"tool": "codex_get_workflow_status", "arguments": {"workflow_id": "<workflowId>"}},
        "approve": {"tool": "codex_approve_plan", "arguments": {"workflow_id": "<workflowId>", "client_request_id": "<stable id>"}},
    },
    "safeRecovery": {
        "diagnostics": {"tool": "codex_collect_diagnostics", "arguments": {"operation_id": "<operationId>"}},
        "repairDryRun": {"tool": "codex_repair_issue", "arguments": {"operation_id": "<operationId>", "action": "<recommended action>", "dry_run": True}},
    },
}


def tool_metadata(name: str) -> dict[str, Any]:
    meta = dict(TOOL_CONTRACT.get(name) or {})
    role = str(meta.get("role") or ROLE_READ_ONLY)
    meta.setdefault("role", role)
    meta.setdefault("preferred", role != ROLE_COMPATIBILITY)
    meta.setdefault("nextTools", [])
    meta.setdefault("avoidWhen", [])
    meta.setdefault("idempotency", "not_applicable")
    meta.setdefault("passiveRead", role in {ROLE_READ_ONLY, ROLE_POLL_STATUS, ROLE_DIAGNOSTICS})
    meta.setdefault("mayStartTurn", False)
    return meta


def apply_agent_contract_to_tools(tools: list[dict[str, Any]]) -> None:
    for tool in tools:
        name = str(tool.get("name") or "")
        meta = tool_metadata(name)
        if meta.get("description"):
            tool["description"] = str(meta["description"])
        annotations = dict(tool.get("annotations") or {})
        annotations["codexMcp"] = {
            "guideVersion": GUIDE_VERSION,
            "role": meta["role"],
            "preferred": bool(meta["preferred"]),
            "nextTools": list(meta["nextTools"]),
            "avoidWhen": list(meta["avoidWhen"]),
            "idempotency": meta["idempotency"],
            "passiveRead": bool(meta["passiveRead"]),
            "mayStartTurn": bool(meta["mayStartTurn"]),
        }
        tool["annotations"] = annotations


def tool_groups() -> list[dict[str, Any]]:
    return copy.deepcopy(TOOL_GROUPS)


def _filtered_capability_map() -> dict[str, Any]:
    return copy.deepcopy(CAPABILITY_MAP)


def _filtered_usage_flows() -> list[dict[str, Any]]:
    return copy.deepcopy(USAGE_FLOWS)


def _base_guide(*, contract_version: str, tool_surface_hash: str, client_type: str | None = None) -> dict[str, Any]:
    return {
        "version": GUIDE_VERSION,
        "contractVersion": contract_version,
        "toolSurfaceHash": tool_surface_hash,
        "clientType": client_type or "generic-agent",
        "recommendedStartupTool": "codex_health_summary",
        "recommendedPrimaryWriteTool": "codex_submit_task",
        "recommendedStartupFlow": "startup",
        "capabilityMap": _filtered_capability_map(),
        "usageFlows": _filtered_usage_flows(),
        "globalRules": list(GLOBAL_RULES),
        "runtimeLimits": copy.deepcopy(RUNTIME_LIMITS),
        "toolGroups": tool_groups(),
    }


def guide_hash(guide: dict[str, Any]) -> str:
    payload = copy.deepcopy(guide)
    payload.pop("guideHash", None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_agent_contract(
    *,
    contract_version: str,
    tool_surface_hash: str,
    detail: str = "compact",
    client_type: str | None = None,
    include_examples: bool = False,
) -> dict[str, Any]:
    selected_detail = detail if detail in {"compact", "full"} else "compact"
    guide = _base_guide(contract_version=contract_version, tool_surface_hash=tool_surface_hash, client_type=client_type)
    guide["detail"] = selected_detail
    if selected_detail == "full" or include_examples:
        guide["examples"] = copy.deepcopy(FULL_EXAMPLES)
    guide["guideHash"] = guide_hash(guide)
    return guide


def compact_tools_list_contract(*, contract_version: str, tool_surface_hash: str) -> dict[str, Any]:
    guide = build_agent_contract(contract_version=contract_version, tool_surface_hash=tool_surface_hash, detail="compact")
    return {
        "codexMcpGuide": guide,
        "recommendedStartupTool": guide["recommendedStartupTool"],
        "recommendedPrimaryWriteTool": guide["recommendedPrimaryWriteTool"],
        "toolGroups": guide["toolGroups"],
    }
