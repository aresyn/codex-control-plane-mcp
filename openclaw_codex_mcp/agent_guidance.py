from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .diagnostics import redact_payload, redact_text


SCHEMA_VERSION = "agent-guidance/v1"
TERMINAL_PROBLEM_STATUSES = {
    "failed",
    "interrupted",
    "cancelled",
    "canceled",
    "orphaned",
    "orphaned_after_app_server_exit",
    "unknown_after_app_server_exit",
}
ACTIVE_STALE_SECONDS = 30 * 60


def build_guidance_for_payload(
    payload: dict[str, Any],
    *,
    surface: str,
    attempt_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    context = _problem_context(payload, surface)
    if context is None:
        return None
    return build_guidance(context, attempt_lookup=attempt_lookup, now=now)


def build_guidance_for_error(
    error: dict[str, Any],
    *,
    tool_name: str | None = None,
    attempt_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    code = str(error.get("code") or "UNKNOWN")
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    scope_type, scope_id = _scope_from_details(details)
    evidence_refs = _evidence_refs_from_details(details)
    if code == "CODEX_DUPLICATE_PROMPT_ACTIVE":
        operation_id = (
            _optional_string(details.get("existingOperationId"))
            or _optional_string(details.get("operationId"))
            or _optional_string(details.get("operation_id"))
        )
        instruction = _instruction(
            "poll_existing_operation",
            "codex_get_operation_status",
            {"operation_id": operation_id} if operation_id else {},
            reason="A matching active prompt already owns the Codex turn.",
            expected="Reuse the existing operation instead of creating another turn.",
            stop_if="operation reaches a terminal status or asks for interaction",
        )
        context = {
            "problemState": "wait",
            "category": "duplicate_prompt",
            "summary": "An active duplicate prompt already exists. Poll it instead of submitting a new turn.",
            "scopeType": scope_type,
            "scopeId": scope_id,
            "action": "poll_existing_operation",
            "instructions": [instruction],
            "evidenceRefs": evidence_refs,
            "recommendedPollAfterSeconds": 5,
        }
        return build_guidance(context, attempt_lookup=attempt_lookup, now=now)
    if code == "CODEX_TIMEOUT":
        operation_id = _optional_string(details.get("operationId")) or _optional_string(details.get("operation_id"))
        client_request_id = _optional_string(details.get("client_request_id")) or _optional_string(details.get("clientRequestId"))
        instruction = _instruction(
            "poll_existing_operation",
            "codex_get_operation_status",
            {"operation_id": operation_id} if operation_id else {},
            reason="The write may already have been accepted before the client timed out.",
            expected="Confirm current operation state before deciding on retry.",
            stop_if="operation is found and is active or terminal",
            continue_if="operation is not found and the same client_request_id can be retried",
        )
        context = {
            "problemState": "wait" if client_request_id else "recoverable",
            "category": "timeout",
            "summary": "A timeout is not proof that Codex did nothing. Poll first, and retry only with the same client_request_id.",
            "scopeType": scope_type,
            "scopeId": scope_id,
            "action": "poll_existing_operation",
            "instructions": [instruction],
            "evidenceRefs": evidence_refs,
            "recommendedPollAfterSeconds": 5,
        }
        return build_guidance(context, attempt_lookup=attempt_lookup, now=now)
    if code in {"CODEX_PROJECT_NOT_FOUND", "CODEX_THREAD_NOT_FOUND", "CODEX_TURN_NOT_FOUND", "INVALID_ARGUMENT"}:
        context = {
            "problemState": "fatal",
            "category": "invalid_target",
            "summary": "The request target or arguments are invalid. Fix the payload or configuration before retrying.",
            "scopeType": scope_type,
            "scopeId": scope_id,
            "action": "fix_payload_or_config",
            "instructions": [
                _instruction(
                    "stop_and_ask_human",
                    None,
                    {},
                    reason="Retrying the same payload will fail again.",
                    expected="A human or orchestrator corrects the project, thread, turn, or argument values.",
                    requires_human=True,
                    risk="low",
                )
            ],
            "evidenceRefs": evidence_refs,
            "recommendedPollAfterSeconds": 0,
        }
        return build_guidance(context, attempt_lookup=attempt_lookup, now=now)
    if code == "CODEX_STATE_BUSY":
        client_request_id = _optional_string(details.get("client_request_id")) or _optional_string(details.get("clientRequestId"))
        context = {
            "problemState": "wait",
            "category": "state_busy",
            "summary": "The MCP state DB is busy. Retry the same request identity instead of minting a new turn.",
            "scopeType": scope_type,
            "scopeId": scope_id,
            "action": "retry_same_client_request_id",
            "instructions": [
                _instruction(
                    "retry_write_same_id",
                    tool_name,
                    {"client_request_id": client_request_id} if client_request_id else {},
                    reason="The durable write may have been partially recorded while SQLite was busy.",
                    expected="The same client_request_id either returns the existing operation or creates one durable operation.",
                    stop_if="a durable operationId is returned",
                    continue_if="CODEX_STATE_BUSY repeats after the cooldown",
                )
            ],
            "evidenceRefs": evidence_refs,
            "recommendedPollAfterSeconds": 5,
        }
        return build_guidance(context, attempt_lookup=attempt_lookup, now=now)
    if code in {"CODEX_APP_SERVER_UNAVAILABLE", "CODEX_SEND_FAILED", "CODEX_BUSY"}:
        action = "collect_diagnostics"
        context = {
            "problemState": "recoverable" if code != "CODEX_BUSY" else "wait",
            "category": code.casefold(),
            "summary": redact_text(error.get("message") or "Codex app-server cannot complete this request yet.", max_chars=300),
            "scopeType": scope_type,
            "scopeId": scope_id,
            "action": action,
            "instructions": [
                _instruction(
                    "collect_diagnostics",
                    "codex_collect_diagnostics",
                    _diagnostic_args_from_details(details),
                    reason="The next action depends on active work, auth, and app-server state.",
                    expected="Diagnostics identify whether to wait, answer an interaction, repair, or restart.",
                    dry_run_first=True,
                )
            ],
            "evidenceRefs": evidence_refs or ([{"type": "tool", "id": tool_name}] if tool_name else []),
            "recommendedPollAfterSeconds": 10 if code == "CODEX_BUSY" else 0,
        }
        return build_guidance(context, attempt_lookup=attempt_lookup, now=now)
    return None


def build_guidance(
    context: dict[str, Any],
    *,
    attempt_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    now = now or _now_iso()
    category = str(context.get("category") or "unknown")
    scope_type = str(context.get("scopeType") or "global")
    scope_id = str(context.get("scopeId") or "global")
    action = str(context.get("action") or "collect_diagnostics")
    target = context.get("target") if isinstance(context.get("target"), dict) else {}
    guard_category = str(context.get("guardCategory") or (f"action:{action}" if action in _REPAIR_ACTIONS else category))
    guard_key = guard_key_for(category=guard_category, scope_type=scope_type, scope_id=scope_id, action=action, target=target)
    attempt_row = attempt_lookup(guard_key) if attempt_lookup else None
    loop_guard = loop_guard_state(
        guard_key=guard_key,
        scope_type=scope_type,
        scope_id=scope_id,
        action=action,
        attempt_row=attempt_row,
        now=now,
    )
    instructions = [dict(item) for item in context.get("instructions") or [] if isinstance(item, dict)]
    if not loop_guard["allowed"]:
        instructions.insert(
            0,
            _instruction(
                "stop_and_ask_human",
                "codex_collect_diagnostics",
                _diagnostic_args_from_context(context),
                reason="The loop guard blocked more automatic recovery attempts for this scope.",
                expected="A human reviews diagnostics and decides whether to force recovery.",
                requires_human=True,
                risk="medium",
            ),
        )
    guidance = {
        "schemaVersion": SCHEMA_VERSION,
        "problemState": "blocked" if not loop_guard["allowed"] else str(context.get("problemState") or "recoverable"),
        "summary": redact_text(context.get("summary") or "MCP detected a state that needs agent attention.", max_chars=500),
        "instructions": instructions,
        "loopGuard": loop_guard,
        "evidenceRefs": redact_payload(context.get("evidenceRefs") or []),
        "reason": redact_text(context.get("reason") or context.get("summary") or "", max_chars=500),
        "expectedOutcome": redact_text(context.get("expectedOutcome") or _expected_for_action(action), max_chars=500),
        "risk": str(context.get("risk") or _risk_for_action(action)),
        "dryRunFirst": bool(context.get("dryRunFirst", action.startswith("retry_") or action in _REPAIR_ACTIONS)),
        "requiresHuman": bool(context.get("requiresHuman", False)) or not loop_guard["allowed"],
        "recommendedPollAfterSeconds": int(context.get("recommendedPollAfterSeconds") or 0),
    }
    return redact_payload(guidance)


def guidance_text(guidance: dict[str, Any]) -> str:
    summary = redact_text(guidance.get("summary"), max_chars=300)
    loop = guidance.get("loopGuard") if isinstance(guidance.get("loopGuard"), dict) else {}
    instructions = guidance.get("instructions") if isinstance(guidance.get("instructions"), list) else []
    first = next((item for item in instructions if isinstance(item, dict)), {})
    tool = first.get("toolName") or first.get("kind") or "the recommended action"
    parts = [summary.rstrip(".") + "." if summary else "MCP found a state that needs attention."]
    if not loop.get("allowed", True):
        parts.append("Do not retry this recovery automatically now; the loop guard is blocking more attempts for this scope.")
    else:
        parts.append(f"Next, run {tool} with the provided arguments.")
    if first.get("dryRunFirst"):
        parts.append("Use dry_run=true first, then execute only if the dry run is clean and the guard still allows it.")
    poll_after = guidance.get("recommendedPollAfterSeconds")
    if poll_after:
        parts.append(f"Poll again in about {poll_after} seconds if the state is still active.")
    if guidance.get("requiresHuman") or not loop.get("allowed", True):
        parts.append("If the condition repeats, collect diagnostics and ask a human instead of minting a new write request.")
    else:
        parts.append("Do not create a new turn unless the instruction explicitly says to retry or start a replacement workflow.")
    return " ".join(redact_text(part, max_chars=500) for part in parts[:6])


def guard_key_for(*, category: str, scope_type: str, scope_id: str, action: str, target: dict[str, Any] | None = None) -> str:
    normalized = json.dumps(
        {
            "category": str(category or "unknown").casefold(),
            "scopeType": str(scope_type or "global").casefold(),
            "scopeId": str(scope_id or "global"),
            "action": str(action or "collect_diagnostics").casefold(),
            "target": _safe_target(target or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def loop_guard_state(
    *,
    guard_key: str,
    scope_type: str,
    scope_id: str,
    action: str,
    attempt_row: dict[str, Any] | None,
    now: str | None = None,
) -> dict[str, Any]:
    policy = policy_for_action(action)
    now_dt = _parse_iso(now or _now_iso()) or datetime.now(timezone.utc)
    attempt_count = int((attempt_row or {}).get("attempt_count") or 0)
    last_attempt = _parse_iso((attempt_row or {}).get("last_attempt_at"))
    if last_attempt is not None and now_dt - last_attempt > timedelta(seconds=policy["windowSeconds"]):
        attempt_count = 0
    cooldown_until = _optional_string((attempt_row or {}).get("cooldown_until"))
    cooldown_dt = _parse_iso(cooldown_until)
    blocked_reason = None
    allowed = True
    if cooldown_dt is not None and cooldown_dt > now_dt:
        allowed = False
        blocked_reason = "cooldown_active"
    if attempt_count >= int(policy["maxAttempts"]):
        allowed = False
        blocked_reason = blocked_reason or "max_attempts_reached"
    return {
        "guardKey": guard_key,
        "scopeType": scope_type,
        "scopeId": scope_id,
        "action": action,
        "attemptCount": attempt_count,
        "maxAttempts": int(policy["maxAttempts"]),
        "windowSeconds": int(policy["windowSeconds"]),
        "cooldownUntil": cooldown_until,
        "allowed": allowed,
        "blockedReason": blocked_reason,
        "escalationAction": "collect_diagnostics_and_ask_human",
    }


def policy_for_action(action: str) -> dict[str, int]:
    action = str(action or "")
    if action == "retry_workflow_with_runtime_policy":
        return {"maxAttempts": 2, "windowSeconds": 24 * 60 * 60}
    if action in {"restart_app_server_idle", "force_restart_app_server"}:
        return {"maxAttempts": 1 if action.startswith("force_") else 2, "windowSeconds": 30 * 60}
    if action == "interrupt_turn":
        return {"maxAttempts": 1, "windowSeconds": 2 * 60 * 60}
    if action in _REPAIR_ACTIONS:
        return {"maxAttempts": 2, "windowSeconds": 2 * 60 * 60}
    return {"maxAttempts": 2, "windowSeconds": 2 * 60 * 60}


def cooldown_after_attempt(action: str, attempt_count: int, *, now: str | None = None, failed_forced: bool = False) -> str | None:
    policy = policy_for_action(action)
    if failed_forced:
        seconds = int(policy["windowSeconds"])
    elif attempt_count <= 1:
        seconds = 5 * 60
    elif attempt_count == 2:
        seconds = 15 * 60
    else:
        seconds = int(policy["windowSeconds"])
    return ((_parse_iso(now or _now_iso()) or datetime.now(timezone.utc)) + timedelta(seconds=seconds)).isoformat()


def attempt_scope_from_args(action: str, args: dict[str, Any]) -> tuple[str, str]:
    workflow_id = _optional_string(args.get("workflow_id")) or _optional_string(args.get("workflowId"))
    operation_id = _optional_string(args.get("operation_id")) or _optional_string(args.get("operationId"))
    turn_id = _optional_string(args.get("turn_id")) or _optional_string(args.get("turnId"))
    thread_id = _optional_string(args.get("thread_id")) or _optional_string(args.get("threadId"))
    if action == "retry_workflow_with_runtime_policy" and workflow_id:
        return "workflow", workflow_id
    if operation_id:
        return "operation", operation_id
    if workflow_id:
        return "workflow", workflow_id
    if turn_id:
        return "turn", turn_id
    if thread_id:
        return "thread", thread_id
    return "global", "global"


def build_post_repair_guidance(
    result: dict[str, Any],
    *,
    action: str,
    scope_type: str,
    scope_id: str,
    loop_guard: dict[str, Any] | None,
) -> dict[str, Any]:
    changed = bool(result.get("changed") or result.get("newWorkflowId") or result.get("interrupted") or result.get("restarted") or result.get("started"))
    next_kind = "poll_workflow" if result.get("newWorkflowId") else ("collect_diagnostics" if not changed else "poll_status")
    tool_name = "codex_get_workflow_status" if result.get("newWorkflowId") else "codex_collect_diagnostics"
    arguments = {"workflow_id": result.get("newWorkflowId")} if result.get("newWorkflowId") else _diagnostic_args_from_context({"scopeType": scope_type, "scopeId": scope_id})
    context = {
        "problemState": "wait" if changed else "recoverable",
        "category": f"post_repair:{action}",
        "summary": "Repair action completed. Verify the new state before starting any new write request.",
        "scopeType": scope_type,
        "scopeId": scope_id,
        "action": next_kind,
        "instructions": [
            _instruction(
                next_kind,
                tool_name,
                arguments,
                reason="Repair results must be verified before the client continues orchestration.",
                expected="The status endpoint confirms whether work is active, terminal, or still blocked.",
            )
        ],
        "evidenceRefs": [{"type": scope_type, "id": scope_id}, {"type": "repair_action", "id": action}],
        "recommendedPollAfterSeconds": 5 if changed else 0,
    }
    guidance = build_guidance(context, attempt_lookup=None)
    if loop_guard is not None:
        guidance["loopGuard"] = loop_guard
    return guidance


def _problem_context(payload: dict[str, Any], surface: str) -> dict[str, Any] | None:
    if _has_pending_interactions(payload):
        return _pending_interaction_context(payload, surface)
    if surface == "operation_status":
        return _operation_context(payload)
    if surface == "workflow_status":
        return _workflow_context(payload)
    if surface == "turn_status":
        return _turn_context(payload)
    if surface == "health_summary":
        return _health_context(payload)
    if surface in {"collect_diagnostics", "analyze_issue"}:
        return _diagnostics_context(payload, surface)
    if surface == "preflight_project_run":
        return _preflight_context(payload)
    return None


def _operation_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    status = str(payload.get("status") or "")
    operation_id = _optional_string(payload.get("operationId")) or _optional_string(payload.get("operation_id"))
    workflow_id = _optional_string(payload.get("workflowId")) or _optional_string(payload.get("workflow_id"))
    thread_id = _optional_string(payload.get("threadId")) or _optional_string(payload.get("thread_id"))
    turn_id = _optional_string(payload.get("turnId")) or _optional_string(payload.get("turn_id"))
    scope_type, scope_id = _best_scope(operation_id=operation_id, workflow_id=workflow_id, thread_id=thread_id, turn_id=turn_id)
    if payload.get("configRecoveryState", {}).get("state") == "mismatch":
        return _diagnostic_context(
            "blocked",
            "config_fingerprint_mismatch",
            "This operation was submitted under a different MCP configuration. Do not recover it from this worker without human approval.",
            scope_type,
            scope_id,
            operation_id=operation_id,
            workflow_id=workflow_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    if _runtime_policy_failure(payload) and workflow_id:
        return _repair_context(
            "recoverable",
            "plan_runtime_failure",
            "The workflow failed with sandbox/runtime evidence. Retry the workflow with an adjusted runtime policy, starting with dry_run=true.",
            "retry_workflow_with_runtime_policy",
            scope_type,
            scope_id,
            {"workflow_id": workflow_id, "action": "retry_workflow_with_runtime_policy", "dry_run": True},
            operation_id=operation_id,
            workflow_id=workflow_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    if status in TERMINAL_PROBLEM_STATUSES:
        return _diagnostic_context(
            "recoverable" if status != "failed" else "blocked",
            f"operation_{status}",
            f"Operation {operation_id or ''} is {status}. Diagnose before any retry.",
            scope_type,
            scope_id,
            operation_id=operation_id,
            workflow_id=workflow_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    if status in {"waiting_for_approval", "waiting_for_user_input"}:
        return _pending_interaction_context(payload, "operation_status")
    staleness = _int_or_none(payload.get("stalenessSeconds"))
    if status in {"running", "starting_turn", "starting_thread", "queued"} and staleness is not None and staleness > ACTIVE_STALE_SECONDS:
        return _repair_context(
            "recoverable",
            "stale_operation",
            "The operation has been active without recent updates. Run diagnostics and a dry-run stale operation repair before retrying.",
            "recover_stale_operations",
            scope_type,
            scope_id,
            {"action": "recover_stale_operations", "operation_id": operation_id, "dry_run": True},
            operation_id=operation_id,
            workflow_id=workflow_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    return None


def _workflow_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    status = str(payload.get("status") or payload.get("phase") or "")
    workflow_id = _optional_string(payload.get("workflowId")) or _optional_string(payload.get("workflow_id"))
    thread_id = _optional_string(payload.get("threadId")) or _optional_string(payload.get("thread_id"))
    turn_id = _optional_string(payload.get("executionTurnId")) or _optional_string(payload.get("planTurnId")) or _optional_string(payload.get("reviewTurnId"))
    scope_type, scope_id = _best_scope(workflow_id=workflow_id, thread_id=thread_id, turn_id=turn_id)
    observation = payload.get("workflowObservation") if isinstance(payload.get("workflowObservation"), dict) else {}
    if status == "plan_candidate_found" or observation.get("recoverableCandidateFound"):
        return {
            "problemState": "recoverable",
            "category": "workflow_plan_candidate_found",
            "summary": "A newer valid plan candidate exists in the thread. Review or adopt it instead of retrying the workflow blindly.",
            "scopeType": scope_type,
            "scopeId": scope_id,
            "action": "adopt_workflow_plan",
            "instructions": [
                _instruction(
                    "run_repair_dry_run",
                    "codex_collect_diagnostics",
                    {"workflow_id": workflow_id},
                    reason="Diagnostics show the candidate plan and official workflow drift.",
                    expected="The agent can decide whether to adopt the candidate or ask a human.",
                    dry_run_first=True,
                )
            ],
            "evidenceRefs": _evidence_refs(workflow_id=workflow_id, thread_id=thread_id, turn_id=turn_id),
            "recommendedPollAfterSeconds": 0,
        }
    if status in TERMINAL_PROBLEM_STATUSES or status in {"failed", "orphaned_after_app_server_exit"}:
        if _runtime_policy_failure(payload):
            return _repair_context(
                "recoverable",
                "workflow_runtime_failure",
                "The workflow failed with runtime/sandbox evidence. Use retry_workflow_with_runtime_policy with dry_run=true first.",
                "retry_workflow_with_runtime_policy",
                scope_type,
                scope_id,
                {"action": "retry_workflow_with_runtime_policy", "workflow_id": workflow_id, "dry_run": True},
                workflow_id=workflow_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
        return _diagnostic_context(
            "blocked",
            f"workflow_{status}",
            f"Workflow {workflow_id or ''} is {status}. Diagnose before creating replacement work.",
            scope_type,
            scope_id,
            workflow_id=workflow_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    staleness = _int_or_none(payload.get("stalenessSeconds"))
    if payload.get("pollRecommended") and staleness is not None and staleness > ACTIVE_STALE_SECONDS:
        return _diagnostic_context(
            "recoverable",
            "stale_workflow",
            "The workflow is still pollable but has no recent activity. Collect diagnostics before retrying.",
            scope_type,
            scope_id,
            workflow_id=workflow_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    return None


def _turn_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    status = str(payload.get("status") or "")
    thread_id = _optional_string(payload.get("threadId")) or _optional_string(payload.get("thread_id"))
    turn_id = _optional_string(payload.get("turnId")) or _optional_string(payload.get("turn_id"))
    scope_type, scope_id = _best_scope(thread_id=thread_id, turn_id=turn_id)
    if status in TERMINAL_PROBLEM_STATUSES or status == "failed":
        return _diagnostic_context(
            "recoverable" if status == "unknown_after_app_server_exit" else "blocked",
            f"turn_{status}",
            f"Turn {turn_id or ''} is {status}. Diagnose before retrying or replacing it.",
            scope_type,
            scope_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    staleness = _int_or_none(payload.get("stalenessSeconds"))
    if status in {"running", "started", "first_message_received"} and staleness is not None and staleness > ACTIVE_STALE_SECONDS:
        return _diagnostic_context(
            "recoverable",
            "stale_turn",
            "The turn is active but stale. Collect diagnostics before interrupting or restarting anything.",
            scope_type,
            scope_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    return None


def _health_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    overall = str(payload.get("overallStatus") or "")
    if overall not in {"broken", "degraded"}:
        return None
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    scope_type, scope_id = _best_scope(
        operation_id=_optional_string(filters.get("operationId")),
        workflow_id=_optional_string(filters.get("workflowId")),
        thread_id=_optional_string(filters.get("threadId")),
        turn_id=_optional_string(filters.get("turnId")),
    )
    active_work = payload.get("activeWork") if isinstance(payload.get("activeWork"), dict) else {}
    has_active_turns = bool(active_work.get("activeTurnCount") or active_work.get("activeTurns"))
    action = "collect_diagnostics" if has_active_turns else str(payload.get("nextRecommendedAction") or "collect_diagnostics")
    return {
        "problemState": "recoverable" if overall == "degraded" else "blocked",
        "category": f"health_{overall}",
        "summary": "MCP health is degraded or broken. Follow diagnostics before any restart or retry.",
        "scopeType": scope_type,
        "scopeId": scope_id,
        "action": action,
        "instructions": [
            _instruction(
                "collect_diagnostics",
                "codex_collect_diagnostics",
                _diagnostic_args_from_context({"scopeType": scope_type, "scopeId": scope_id, **filters}),
                reason="Health summary is compact. Diagnostics provide the specific repair path.",
                expected="The agent gets a prioritized recovery plan and dry-run repair arguments.",
                dry_run_first=True,
            )
        ],
        "evidenceRefs": [{"type": "health", "id": overall}],
        "recommendedPollAfterSeconds": int(payload.get("recommendedPollAfterSeconds") or 0),
    }


def _diagnostics_context(payload: dict[str, Any], surface: str) -> dict[str, Any] | None:
    overall = str(payload.get("overallStatus") or "")
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    if surface == "analyze_issue":
        if not findings:
            return None
        root = payload.get("likelyRootCause") if isinstance(payload.get("likelyRootCause"), dict) else {}
        category = str(root.get("category") or (findings[0].get("category") if findings else "diagnostic_finding"))
        actions = payload.get("recommendedRepairActions") if isinstance(payload.get("recommendedRepairActions"), list) else []
        first_action = _first_action_name(actions) or "collect_diagnostics"
        scope_type, scope_id = "diagnosis", str(payload.get("diagnosisId") or "latest")
        if first_action == "no_action" or category == "no_obvious_issue":
            return {
                "problemState": "no_action",
                "category": category,
                "summary": redact_text(root.get("title") or "No automated recovery is recommended from the available diagnostics.", max_chars=300),
                "scopeType": scope_type,
                "scopeId": scope_id,
                "action": "no_action",
                "instructions": [
                    _instruction(
                        "no_action",
                        None,
                        {},
                        reason="Diagnostics did not find a concrete recoverable MCP/Codex fault.",
                        expected="The agent does not start repair loops and continues only if new evidence appears.",
                        risk="low",
                    )
                ],
                "evidenceRefs": [],
                "recommendedPollAfterSeconds": 0,
            }
        return _repair_context(
            "recoverable" if first_action in _REPAIR_ACTIONS else "blocked",
            category,
            redact_text(root.get("title") or "Diagnostics found a likely MCP/Codex problem.", max_chars=300),
            first_action,
            scope_type,
            scope_id,
            {"action": first_action, "dry_run": True},
        )
    if overall not in {"broken", "degraded"}:
        return None
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    category = _first_problem_category(checks) or f"diagnostics_{overall}"
    recommendations = payload.get("recommendedActions") if isinstance(payload.get("recommendedActions"), list) else []
    action = str(recommendations[0]) if recommendations else "validate_paths_and_config"
    scope_type, scope_id = _best_scope(
        operation_id=_optional_string(filters.get("operationId")),
        workflow_id=_optional_string(filters.get("workflowId")),
        thread_id=_optional_string(filters.get("threadId")),
        turn_id=_optional_string(filters.get("turnId")),
    )
    return _repair_context(
        "recoverable" if overall == "degraded" else "blocked",
        category,
        "Diagnostics found a problem. Run the recommended repair as dry-run first and stop if the guard blocks it.",
        action,
        scope_type,
        scope_id,
        {**_diagnostic_args_from_context(filters), "action": action, "dry_run": True},
    )


def _preflight_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    status = str(payload.get("status") or "")
    if status == "ok":
        return None
    project_id = _optional_string(payload.get("projectId")) or _optional_string(payload.get("project_id"))
    scope_type, scope_id = ("project", project_id) if project_id else ("preflight", "latest")
    return {
        "problemState": "blocked" if status == "error" else "recoverable",
        "category": "preflight_failed",
        "summary": "Project preflight is not clean. Fix the failing checks before starting or retrying a workflow.",
        "scopeType": scope_type,
        "scopeId": scope_id,
        "action": "fix_payload_or_config",
        "instructions": [
            _instruction(
                "stop_and_ask_human" if status == "error" else "collect_diagnostics",
                "codex_collect_diagnostics",
                {"project_id": project_id} if project_id else {},
                reason="Preflight checks are designed to catch failures before Codex starts a long turn.",
                expected="The agent corrects config/auth/path/runtime policy, then reruns preflight.",
                requires_human=status == "error",
            )
        ],
        "evidenceRefs": [{"type": "project", "id": project_id}] if project_id else [],
        "recommendedPollAfterSeconds": 0,
    }


def _pending_interaction_context(payload: dict[str, Any], surface: str) -> dict[str, Any]:
    operation_id = _optional_string(payload.get("operationId")) or _optional_string(payload.get("operation_id"))
    workflow_id = _optional_string(payload.get("workflowId")) or _optional_string(payload.get("workflow_id"))
    thread_id = _optional_string(payload.get("threadId")) or _optional_string(payload.get("thread_id"))
    turn_id = _optional_string(payload.get("turnId")) or _optional_string(payload.get("turn_id"))
    scope_type, scope_id = _best_scope(operation_id=operation_id, workflow_id=workflow_id, thread_id=thread_id, turn_id=turn_id)
    return {
        "problemState": "needs_input",
        "category": "pending_interaction",
        "summary": "Codex is waiting for an approval or user answer. Do not restart the turn.",
        "scopeType": scope_type,
        "scopeId": scope_id,
        "action": "answer_pending_interaction",
        "instructions": [
            _instruction(
                "answer_pending_interaction",
                "codex_list_pending_interactions",
                {"thread_id": thread_id, "turn_id": turn_id},
                reason="The active turn is blocked waiting for OpenClaw/Hermes input.",
                expected="The client answers or expires the pending interaction, then continues polling.",
                risk="low",
                recommended_poll_after_seconds=5,
            )
        ],
        "evidenceRefs": _evidence_refs(operation_id=operation_id, workflow_id=workflow_id, thread_id=thread_id, turn_id=turn_id),
        "recommendedPollAfterSeconds": 5,
    }


def _diagnostic_context(problem_state: str, category: str, summary: str, scope_type: str, scope_id: str, **ids: Any) -> dict[str, Any]:
    return {
        "problemState": problem_state,
        "category": category,
        "summary": summary,
        "scopeType": scope_type,
        "scopeId": scope_id,
        "action": "collect_diagnostics",
        "instructions": [
            _instruction(
                "collect_diagnostics",
                "codex_collect_diagnostics",
                _diagnostic_args_from_context(ids),
                reason="The status is not enough to choose a safe recovery path.",
                expected="Diagnostics provide concrete dry-run repair arguments or a stop condition.",
                dry_run_first=True,
            )
        ],
        "evidenceRefs": _evidence_refs(**ids),
        "recommendedPollAfterSeconds": 0,
    }


def _repair_context(problem_state: str, category: str, summary: str, action: str, scope_type: str, scope_id: str, arguments: dict[str, Any], **ids: Any) -> dict[str, Any]:
    return {
        "problemState": problem_state,
        "category": category,
        "summary": summary,
        "scopeType": scope_type,
        "scopeId": scope_id,
        "action": action,
        "instructions": [
            _instruction(
                "run_repair_dry_run",
                "codex_repair_issue",
                arguments,
                reason="MCP can test this recovery path without changing state first.",
                expected="The dry run shows whether the repair is still safe and useful.",
                dry_run_first=True,
                risk=_risk_for_action(action),
            )
        ],
        "evidenceRefs": _evidence_refs(**ids),
        "recommendedPollAfterSeconds": 0,
    }


def _instruction(
    kind: str,
    tool_name: str | None,
    arguments: dict[str, Any],
    *,
    reason: str,
    expected: str,
    risk: str = "low",
    dry_run_first: bool = False,
    requires_human: bool = False,
    recommended_poll_after_seconds: int = 0,
    stop_if: str | None = None,
    continue_if: str | None = None,
) -> dict[str, Any]:
    item = {
        "kind": kind,
        "guideAction": kind,
        "guideFlow": _guide_flow_for_instruction(kind, tool_name),
        "toolName": tool_name,
        "arguments": redact_payload({key: value for key, value in arguments.items() if value not in (None, "")}),
        "reason": redact_text(reason, max_chars=500),
        "expectedOutcome": redact_text(expected, max_chars=500),
        "risk": risk,
        "dryRunFirst": dry_run_first,
        "requiresHuman": requires_human,
        "recommendedPollAfterSeconds": recommended_poll_after_seconds,
        "stopIf": stop_if or "loopGuard.allowed=false or the tool returns a terminal unrecoverable error",
        "continueIf": continue_if or "the tool result is ok and loopGuard.allowed remains true",
    }
    return item


def _guide_flow_for_instruction(kind: str, tool_name: str | None) -> str:
    if tool_name in {"codex_get_operation_status", "codex_get_queue_status", "codex_get_concurrency_status"}:
        return "long_running_poll"
    if tool_name in {"codex_get_workflow_status", "codex_approve_plan"}:
        return "plan_mode"
    if tool_name in {"codex_collect_diagnostics", "codex_analyze_issue", "codex_repair_issue"}:
        return "diagnostics_recovery"
    if tool_name in {"codex_list_pending_interactions", "codex_answer_pending_interaction"}:
        return "long_running_poll"
    if tool_name == "codex_interrupt_turn" or kind == "interrupt_turn":
        return "interrupt"
    if kind == "no_action":
        return "diagnostics_recovery"
    if kind.startswith("retry_"):
        return "new_task"
    return "startup"


_REPAIR_ACTIONS = {
    "cleanup_prompt_submissions",
    "recover_stale_operations",
    "reconcile_operations_with_tracked_turns",
    "refresh_catalog_and_history",
    "reconcile_workflow_from_thread",
    "retry_workflow_with_runtime_policy",
    "mark_orphaned_after_exit",
    "restart_app_server_idle",
    "force_restart_app_server",
    "mark_stale_turns_orphaned",
    "expire_stale_pending_interactions",
    "refresh_catalog",
    "rebuild_search_index",
    "validate_paths_and_config",
    "interrupt_turn",
    "no_action",
}


def _has_pending_interactions(payload: dict[str, Any]) -> bool:
    pending = payload.get("pendingInteractions")
    return isinstance(pending, list) and bool(pending)


def _runtime_policy_failure(payload: dict[str, Any]) -> bool:
    text = json.dumps(redact_payload(payload), ensure_ascii=False).casefold()
    return any(marker in text for marker in ("createprocessasuserw failed: 5", "windows sandbox", "access is denied"))


def _scope_from_details(details: dict[str, Any]) -> tuple[str, str]:
    return _best_scope(
        operation_id=_optional_string(details.get("operationId")) or _optional_string(details.get("operation_id")),
        workflow_id=_optional_string(details.get("workflowId")) or _optional_string(details.get("workflow_id")),
        thread_id=_optional_string(details.get("threadId")) or _optional_string(details.get("thread_id")),
        turn_id=_optional_string(details.get("turnId")) or _optional_string(details.get("turn_id")),
        project_id=_optional_string(details.get("projectId")) or _optional_string(details.get("project_id")),
    )


def _best_scope(
    *,
    operation_id: str | None = None,
    workflow_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    project_id: str | None = None,
) -> tuple[str, str]:
    if operation_id:
        return "operation", operation_id
    if workflow_id:
        return "workflow", workflow_id
    if turn_id:
        return "turn", turn_id
    if thread_id:
        return "thread", thread_id
    if project_id:
        return "project", project_id
    return "global", "global"


def _diagnostic_args_from_details(details: dict[str, Any]) -> dict[str, Any]:
    return _diagnostic_args_from_context(
        {
            "operationId": details.get("operationId") or details.get("operation_id"),
            "workflowId": details.get("workflowId") or details.get("workflow_id"),
            "threadId": details.get("threadId") or details.get("thread_id"),
            "turnId": details.get("turnId") or details.get("turn_id"),
        }
    )


def _diagnostic_args_from_context(context: dict[str, Any]) -> dict[str, Any]:
    scope_type = str(context.get("scopeType") or "")
    scope_id = _optional_string(context.get("scopeId"))
    result = {
        "operation_id": context.get("operationId") or context.get("operation_id"),
        "workflow_id": context.get("workflowId") or context.get("workflow_id"),
        "thread_id": context.get("threadId") or context.get("thread_id"),
        "turn_id": context.get("turnId") or context.get("turn_id"),
        "include_logs": False,
    }
    if scope_type == "operation" and not result["operation_id"]:
        result["operation_id"] = scope_id
    if scope_type == "workflow" and not result["workflow_id"]:
        result["workflow_id"] = scope_id
    if scope_type == "thread" and not result["thread_id"]:
        result["thread_id"] = scope_id
    if scope_type == "turn" and not result["turn_id"]:
        result["turn_id"] = scope_id
    return {key: value for key, value in result.items() if value not in (None, "")}


def _evidence_refs_from_details(details: dict[str, Any]) -> list[dict[str, str]]:
    return _evidence_refs(
        operation_id=details.get("operationId") or details.get("operation_id"),
        workflow_id=details.get("workflowId") or details.get("workflow_id"),
        thread_id=details.get("threadId") or details.get("thread_id"),
        turn_id=details.get("turnId") or details.get("turn_id"),
        project_id=details.get("projectId") or details.get("project_id"),
    )


def _evidence_refs(**ids: Any) -> list[dict[str, str]]:
    mapping = {
        "operation_id": "operation",
        "workflow_id": "workflow",
        "thread_id": "thread",
        "turn_id": "turn",
        "project_id": "project",
    }
    refs = []
    for key, ref_type in mapping.items():
        value = _optional_string(ids.get(key))
        if value:
            refs.append({"type": ref_type, "id": value})
    return refs


def _safe_target(target: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in ("operation_id", "workflow_id", "thread_id", "turn_id", "action", "sandbox", "approval_policy"):
        value = target.get(key) or target.get(_camel(key))
        if value not in (None, ""):
            safe[key] = str(value)
    return safe


def _first_problem_category(checks: list[Any]) -> str | None:
    for item in checks:
        if not isinstance(item, dict):
            continue
        if item.get("status") in {"error", "warning"}:
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            return str(details.get("category") or item.get("name") or "diagnostic_check")
    return None


def _first_action_name(actions: list[Any]) -> str | None:
    for item in actions:
        if isinstance(item, dict) and item.get("action"):
            return str(item.get("action"))
        if isinstance(item, str) and item:
            return item
    return None


def _expected_for_action(action: str) -> str:
    if action == "answer_pending_interaction":
        return "Codex receives the missing answer and the same turn can continue."
    if action == "retry_workflow_with_runtime_policy":
        return "A replacement workflow starts with safer runtime policy and lineage back to the failed workflow."
    if action == "poll_existing_operation":
        return "The client observes the existing durable operation instead of duplicating work."
    if action == "collect_diagnostics":
        return "Diagnostics explain the safe next recovery step."
    return "The agent verifies state before continuing orchestration."


def _risk_for_action(action: str) -> str:
    if action in {"force_restart_app_server", "interrupt_turn", "mark_orphaned_after_exit"}:
        return "medium"
    if action == "retry_workflow_with_runtime_policy":
        return "medium"
    return "low"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    text = _optional_string(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
