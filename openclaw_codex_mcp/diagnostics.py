from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*([^\s,;\"']+)")
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}")
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b")
SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "deepseek_api_key",
    "openai_api_key",
    "password",
    "secret",
    "token",
}
ACCOUNT_PRIVATE_FIELD_NAMES = {
    "accountid",
    "balance",
    "chatgptaccountid",
    "email",
    "identityid",
    "limitid",
    "limitname",
    "organizationid",
    "orgid",
    "tenantid",
    "userid",
    "workspaceid",
}

SEVERITY_SCORE = {"error": 3, "warning": 2, "info": 1, "ok": 0}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_text(value: Any, *, max_chars: int | None = None) -> str:
    text = "" if value is None else str(value)
    text = BEARER_RE.sub("Bearer [redacted]", text)
    text = EMAIL_RE.sub("[redacted-email]", text)
    text = SECRET_KEY_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = OPENAI_KEY_RE.sub("sk-[redacted]", text)
    text = TELEGRAM_TOKEN_RE.sub("[telegram-token-redacted]", text)
    if max_chars is not None and len(text) > max_chars:
        return text[: max(0, max_chars - 3)].rstrip() + "..."
    return text


def redact_payload(value: Any, *, max_string_chars: int = 4000) -> Any:
    if isinstance(value, dict):
        image_redacted = _redact_image_input_payload(value, max_string_chars=max_string_chars)
        if image_redacted is not None:
            return image_redacted
        redact_spend_values = _looks_like_spend_control(value)
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _secret_key_name(key_text):
                redacted[key_text] = "[redacted]"
            elif _account_private_key_name(key_text):
                redacted[key_text] = _redacted_account_value(key_text, item)
            elif redact_spend_values and _normalized_key(key_text) in {"limit", "used"}:
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = redact_payload(item, max_string_chars=max_string_chars)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item, max_string_chars=max_string_chars) for item in value]
    if isinstance(value, str):
        return redact_text(value, max_chars=max_string_chars)
    return value


def _redact_image_input_payload(value: dict[str, Any], *, max_string_chars: int) -> dict[str, Any] | None:
    item_type = str(value.get("type") or "")
    if item_type not in {"image", "localImage"}:
        return None
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if item_type == "image" and key_text == "url":
            url = "" if item is None else str(item)
            redacted["url"] = "[redacted-image-url]"
            redacted["urlHash"] = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] if url else None
            continue
        if item_type == "localImage" and key_text == "path":
            path = "" if item is None else str(item)
            redacted["path"] = "[redacted-local-image-path]"
            redacted["pathHash"] = hashlib.sha256(path.encode("utf-8")).hexdigest()[:32] if path else None
            suffix = Path(path).suffix.casefold() if path else ""
            if suffix:
                redacted["extension"] = suffix
            continue
        if _secret_key_name(key_text):
            redacted[key_text] = "[redacted]"
        else:
            redacted[key_text] = redact_payload(item, max_string_chars=max_string_chars)
    return redacted


def check(name: str, status: str, message: str, *, details: dict[str, Any] | None = None, suggested_action: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "message": redact_text(message, max_chars=1000),
        "details": redact_payload(details or {}),
        "suggestedAction": suggested_action,
    }


def overall_status(checks: list[dict[str, Any]]) -> str:
    if any(item.get("status") == "error" for item in checks):
        return "broken"
    if any(item.get("status") == "warning" for item in checks):
        return "degraded"
    return "healthy"


def finding(
    category: str,
    severity: str,
    title: str,
    *,
    evidence: list[Any] | None = None,
    recommended_actions: list[dict[str, Any]] | None = None,
    confidence: str = "medium",
) -> dict[str, Any]:
    return {
        "category": category,
        "severity": severity,
        "title": redact_text(title, max_chars=1000),
        "confidence": confidence,
        "evidence": redact_payload(evidence or []),
        "recommendedActions": recommended_actions or actions_for_category(category),
    }


def action(
    name: str,
    *,
    safe_to_run: bool = True,
    requires_force: bool = False,
    expected_effect: str,
    risk: str = "low",
    dry_run_default: bool = True,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": name,
        "safeToRun": safe_to_run,
        "requiresForce": requires_force,
        "dryRunDefault": dry_run_default,
        "expectedEffect": expected_effect,
        "risk": risk,
        "arguments": arguments or {},
    }


def actions_for_category(category: str) -> list[dict[str, Any]]:
    if category == "codex_auth_required":
        return [
            action(
                "reauthenticate_codex_home",
                safe_to_run=False,
                expected_effect="Run Codex login or provision auth.json for the configured CODEX_HOME, then retry the failed turn.",
                risk="medium",
            )
        ]
    if category in {"app_server_not_running", "app_server_unavailable"}:
        return [action("restart_app_server_idle", expected_effect="Start or restart the MCP-owned Codex app-server when idle.")]
    if category in {"app_server_stdout_closed", "broken_pipe", "app_server_exited"}:
        return [
            action("restart_app_server_idle", expected_effect="Restart app-server if no active work remains."),
            action(
                "mark_orphaned_after_exit",
                safe_to_run=False,
                requires_force=True,
                expected_effect="Mark active durable work as unknown/orphaned after app-server exit.",
                risk="medium",
            ),
        ]
    if category == "stale_operation":
        return [action("recover_stale_operations", expected_effect="Reset recoverable queued/starting operations without starting duplicate turns.")]
    if category in {"premature_terminal_operation", "terminal_reconcile_stuck"}:
        return [
            action(
                "reconcile_operations_with_tracked_turns",
                expected_effect="Recompute durable operation statuses and final reports from trusted tracked turn evidence.",
            )
        ]
    if category == "stale_turn":
        return [action("mark_orphaned_after_exit", expected_effect="Close stale tracked running turns as unknown after app-server exit.")]
    if category in {"pending_interaction_stale", "pending_interaction_orphaned", "pending_approval", "pending_user_input"}:
        return [action("expire_stale_pending_interactions", expected_effect="Move expired pending interactions to expired status.")]
    if category in {"catalog_stale", "project_path", "path_casing_mismatch", "kb_history_stale", "hook_history", "search_index"}:
        return [action("refresh_catalog_and_history", expected_effect="Refresh project/chat cache and MCP-owned history/search index.")]
    if category in {"model_or_config_error", "old_codex_binary", "client_timeout", "app_server_timeout"}:
        return [action("validate_paths_and_config", expected_effect="Re-run read-only config and path diagnostics.")]
    if category == "duplicate_prompt":
        return [action("cleanup_prompt_submissions", expected_effect="Remove old terminal prompt-submission rows; active duplicates should be polled or inspected.")]
    if category == "turn_needs_interrupt":
        return [
            action(
                "interrupt_turn",
                safe_to_run=False,
                requires_force=True,
                expected_effect="Interrupt a specific live Codex turn.",
                risk="medium",
            )
        ]
    return [action("validate_paths_and_config", expected_effect="Re-run read-only config and path diagnostics.")]


def analyze_context(problem_text: str | None, diagnostics: dict[str, Any], logs: dict[str, Any] | None = None) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    text = (problem_text or "").casefold()
    log_entries = (logs or {}).get("logs") or []
    event_entries = (logs or {}).get("events") or []
    combined_log_text = "\n".join(str(item.get("message") or item.get("raw") or "") for item in log_entries).casefold()
    combined_event_text = "\n".join(json.dumps(item, ensure_ascii=False) for item in event_entries).casefold()
    haystack = "\n".join([text, combined_log_text, combined_event_text])

    for item in diagnostics.get("checks") or []:
        status = item.get("status")
        if status not in {"warning", "error"}:
            continue
        category = str(item.get("details", {}).get("category") or item.get("name") or "diagnostic_check")
        findings.append(
            finding(
                category,
                "error" if status == "error" else "warning",
                str(item.get("message") or item.get("name") or category),
                evidence=[item],
                confidence="high",
            )
        )

    if _auth_error_in_text(haystack):
        findings.append(
            finding(
                "codex_auth_required",
                "error",
                "Codex app-server could not authenticate to the OpenAI API.",
                evidence=_matching_evidence(
                    log_entries,
                    event_entries,
                    ("401 unauthorized", "missing bearer", "missing basic authentication", "requiresopenaiauth"),
                ),
                confidence="high",
            )
        )

    if any(marker in haystack for marker in ("stdout closed", "broken pipe", "transport closed")):
        findings.append(
            finding(
                "app_server_stdout_closed",
                "error",
                "Codex app-server transport appears closed or broken.",
                evidence=_matching_evidence(log_entries, event_entries, ("stdout closed", "broken pipe", "transport closed")),
                confidence="high",
            )
        )
    if "timeout" in haystack or "timed out" in haystack:
        findings.append(
            finding(
                "client_timeout" if "client" in haystack else "app_server_timeout",
                "warning",
                "Recent evidence contains app-server or turn timeout signals.",
                evidence=_matching_evidence(log_entries, event_entries, ("timeout", "timed out")),
                recommended_actions=[action("validate_paths_and_config", expected_effect="Check path/config health before restart or retry.")],
                confidence="medium",
            )
        )
    if any(marker in haystack for marker in ("createprocessasuserw failed: 5", "windows sandbox: runner error", "access is denied")):
        findings.append(
            finding(
                "windows_sandbox_spawn_denied",
                "error",
                "Codex could not spawn a Windows sandboxed process for the configured project run.",
                evidence=_matching_evidence(
                    log_entries,
                    event_entries,
                    ("createprocessasuserw failed: 5", "windows sandbox: runner error", "access is denied"),
                ),
                recommended_actions=[
                    action("validate_paths_and_config", expected_effect="Run project preflight and verify sandbox/path configuration before retry."),
                ],
                confidence="high",
            )
        )
    if any(marker in haystack for marker in ("duplicate prompt", "codex_duplicate_prompt_active", "duplicate_prompt_active")):
        findings.append(
            finding(
                "duplicate_prompt",
                "warning",
                "Recent evidence indicates duplicate prompt protection was triggered.",
                evidence=_matching_evidence(log_entries, event_entries, ("duplicate prompt", "codex_duplicate_prompt_active", "duplicate_prompt_active")),
                confidence="high",
            )
        )
    if any(marker in haystack for marker in ("app-server exited", "app server exited", "process exited", "exit code")):
        findings.append(
            finding(
                "app_server_exited",
                "error",
                "Recent evidence indicates the MCP-owned Codex app-server exited.",
                evidence=_matching_evidence(log_entries, event_entries, ("app-server exited", "app server exited", "process exited", "exit code")),
                confidence="high",
            )
        )
    if any(marker in haystack for marker in ("model error", "invalid model", "model_not_found", "approval policy", "sandbox policy", "config error")):
        findings.append(
            finding(
                "model_or_config_error",
                "error",
                "Recent evidence indicates a model, approval policy, sandbox, or configuration error.",
                evidence=_matching_evidence(log_entries, event_entries, ("model error", "invalid model", "model_not_found", "approval policy", "sandbox policy", "config error")),
                confidence="medium",
            )
        )
    if any(marker in haystack for marker in ("old codex", "outdated codex", "unsupported codex", "codex binary")):
        findings.append(
            finding(
                "old_codex_binary",
                "warning",
                "Recent evidence may indicate an outdated or invalid Codex binary.",
                evidence=_matching_evidence(log_entries, event_entries, ("old codex", "outdated codex", "unsupported codex", "codex binary")),
                confidence="medium",
            )
        )
    if any(marker in haystack for marker in ("kb history stale", "stale kb", "search index stale")):
        findings.append(
            finding(
                "kb_history_stale",
                "warning",
                "Recent evidence indicates stale KB history or search index state.",
                evidence=_matching_evidence(log_entries, event_entries, ("kb history stale", "stale kb", "search index stale")),
                confidence="medium",
            )
        )
    if any(marker in haystack for marker in ("path casing", "case mismatch", "duplicate project")):
        findings.append(
            finding(
                "path_casing_mismatch",
                "warning",
                "Recent evidence indicates a project path casing or duplicate project mismatch.",
                evidence=_matching_evidence(log_entries, event_entries, ("path casing", "case mismatch", "duplicate project")),
                confidence="medium",
            )
        )
    workflow_observation = diagnostics.get("workflowObservation") if isinstance(diagnostics.get("workflowObservation"), dict) else None
    if workflow_observation:
        if workflow_observation.get("threadAdvancedAfterOfficialTurn"):
            findings.append(
                finding(
                    "workflow_thread_drift",
                    "warning",
                    "Workflow state is older than the observed Codex thread tail.",
                    evidence=[redact_payload(workflow_observation)],
                    confidence=str(workflow_observation.get("confidence") or "medium"),
                )
            )
        if workflow_observation.get("recoverableCandidateFound"):
            findings.append(
                finding(
                    "workflow_recoverable_candidate_found",
                    "warning",
                    "A newer valid plan/report candidate is available for workflow recovery.",
                    evidence=[redact_payload(workflow_observation)],
                    confidence=str(workflow_observation.get("confidence") or "medium"),
                )
            )
        if workflow_observation.get("officialPlanQuality") in {"blocker", "refusal"}:
            findings.append(
                finding(
                    "invalid_plan_artifact",
                    "error",
                    "The official workflow plan artifact is a blocker/refusal, not an executable plan.",
                    evidence=[redact_payload(workflow_observation)],
                    confidence="high",
                )
            )

    for turn in diagnostics.get("activeWork", {}).get("activeTurns") or []:
        status = str(turn.get("status") or "")
        staleness = _seconds_since(turn.get("updated_at") or turn.get("updatedAt"))
        if status in {"running", "started", "first_message_received"} and staleness is not None and staleness > 30 * 60:
            findings.append(
                finding(
                    "stale_turn",
                    "warning",
                    f"Tracked turn {turn.get('turn_id') or turn.get('turnId')} has been active without recent updates.",
                    evidence=[{"turn": redact_payload(turn), "stalenessSeconds": staleness}],
                    confidence="medium",
                )
            )

    for operation in diagnostics.get("staleOperations") or []:
        findings.append(
            finding(
                "stale_operation",
                "warning",
                f"Durable operation {operation.get('operationId')} appears stale.",
                evidence=[{"operation": redact_payload(operation)}],
                confidence="high",
            )
        )

    for submission in diagnostics.get("promptSubmissions") or []:
        if submission.get("duplicateOfSubmissionId"):
            findings.append(
                finding(
                    "duplicate_prompt",
                    "warning",
                    "A correlated prompt submission was recorded as duplicate.",
                    evidence=[{"promptSubmission": redact_payload(submission)}],
                    confidence="high",
                )
            )

    pending = diagnostics.get("pendingInteractions") or diagnostics.get("activeWork", {}).get("pendingInteractions") or []
    if isinstance(pending, int):
        pending_count = pending
    else:
        pending_count = len(pending)
    if pending_count:
        pending_category = "pending_interaction_stale"
        if isinstance(pending, list):
            methods = " ".join(str(item.get("method") or "") for item in pending).casefold()
            if "approval" in methods:
                pending_category = "pending_approval"
            elif "userinput" in methods or "elicitation" in methods:
                pending_category = "pending_user_input"
        findings.append(
            finding(
                pending_category,
                "warning",
                f"There are {pending_count} pending Codex interactions waiting for OpenClaw.",
                evidence=[{"pendingInteractionCount": pending_count}],
                confidence="high",
            )
        )

    if not findings:
        findings.append(
            finding(
                "no_obvious_issue",
                "info",
                "No obvious MCP/Codex problem was detected from the available diagnostics.",
                evidence=[],
                recommended_actions=[action("no_action", expected_effect="No automated repair is recommended from the available evidence.")],
                confidence="medium",
            )
        )

    findings = _deduplicate_findings(findings)
    root = likely_root_cause(findings)
    return {
        "findings": findings,
        "likelyRootCause": root,
        "confidence": root.get("confidence") if root else "low",
        "diagnosisConfidence": diagnostics.get("diagnosisConfidence") or (root.get("confidence") if root else "low"),
        "recommendedRepairActions": _unique_actions(findings),
        "nextDiagnosticSteps": next_steps_for_findings(findings),
    }


def likely_root_cause(findings: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(findings, key=lambda item: SEVERITY_SCORE.get(str(item.get("severity")), 0), reverse=True)
    return ranked[0] if ranked else {}


def next_steps_for_findings(findings: list[dict[str, Any]]) -> list[str]:
    categories = {str(item.get("category")) for item in findings}
    steps: list[str] = []
    if "codex_auth_required" in categories:
        steps.append("Verify the configured CODEX_HOME has Codex authentication, for example auth.json or an equivalent login.")
    if "windows_sandbox_spawn_denied" in categories:
        steps.append("Run codex_preflight_project_run for the affected cwd and sandbox before retrying the long workflow.")
    if "workflow_recoverable_candidate_found" in categories:
        steps.append("Review workflowObservation.candidatePlans and adopt the valid candidate before approving execution.")
    if categories & {"app_server_stdout_closed", "app_server_not_running", "app_server_exited"}:
        steps.append("Inspect recent MCP log lines and app_server_events around the last processGeneration.")
    if "terminal_reconcile_stuck" in categories:
        steps.append("Run codex_repair_issue(action='reconcile_operations_with_tracked_turns', dry_run=true) for the scoped operation, then execute once if the preview only affects that operation.")
    if categories & {"stale_turn", "stale_operation"}:
        steps.append("Check affected durable operation/turn status before running the recommended dry-run repair.")
    if categories & {"pending_interaction_stale", "pending_approval", "pending_user_input"}:
        steps.append("List pending interactions and answer or expire the stale requests.")
    if categories & {"path_casing_mismatch", "kb_history_stale", "hook_history", "catalog_stale"}:
        steps.append("Refresh catalog/history and verify the project path casing used by the MCP client.")
    if not steps:
        steps.append("Collect diagnostics again with include_logs=true if the issue is still visible.")
    return steps


def parse_log_line(line: str) -> dict[str, Any]:
    text = redact_text(line.rstrip("\n"), max_chars=4000)
    severity = "info"
    for candidate in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        if f" {candidate} " in f" {text} ":
            severity = candidate.lower()
            break
    return {"severity": severity, "message": text, "raw": text}


def read_log_files(log_path: Path, *, limit: int, severity: str | None = None, max_line_chars: int = 4000) -> list[dict[str, Any]]:
    paths = [log_path.with_name(log_path.name + f".{index}") for index in range(5, 0, -1)]
    paths.append(log_path)
    lines: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in content:
            entry = parse_log_line(redact_text(line, max_chars=max_line_chars))
            entry["path"] = str(path)
            if severity and entry["severity"] != severity:
                continue
            lines.append(entry)
    return lines[-limit:]


def event_to_tool(row: dict[str, Any], *, include_payload: bool, max_payload_chars: int = 8000) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if include_payload:
        try:
            loaded = json.loads(str(row.get("payload_json") or "{}"))
            payload = redact_payload(loaded, max_string_chars=max_payload_chars) if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            payload = {"raw": redact_text(row.get("payload_json"), max_chars=max_payload_chars)}
    return {
        "id": row.get("id"),
        "direction": row.get("direction"),
        "jsonrpcId": row.get("jsonrpc_id"),
        "method": row.get("method"),
        "threadId": row.get("thread_id"),
        "turnId": row.get("turn_id"),
        "processGeneration": row.get("process_generation"),
        "receivedAt": row.get("received_at"),
        "payload": payload if include_payload else None,
    }


def _secret_key_name(value: str) -> bool:
    normalized = _normalized_key(value)
    return normalized in SECRET_FIELD_NAMES or any(token in normalized for token in ("apikey", "secret", "token", "password"))


def _account_private_key_name(value: str) -> bool:
    return _normalized_key(value) in ACCOUNT_PRIVATE_FIELD_NAMES


def _redacted_account_value(key: str, value: Any) -> Any:
    normalized = _normalized_key(key)
    if normalized == "email":
        return {"present": isinstance(value, str) and bool(value.strip()), "redacted": True}
    return "[redacted]"


def _looks_like_spend_control(value: dict[str, Any]) -> bool:
    keys = {_normalized_key(str(key)) for key in value.keys()}
    return bool({"limit", "used"} & keys) and ("remainingpercent" in keys or "resetsat" in keys)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _matching_evidence(logs: list[dict[str, Any]], events: list[dict[str, Any]], markers: tuple[str, ...]) -> list[Any]:
    evidence: list[Any] = []
    for entry in logs:
        text = str(entry.get("message") or entry.get("raw") or "").casefold()
        if any(marker in text for marker in markers):
            evidence.append(entry)
    for entry in events:
        text = json.dumps(entry, ensure_ascii=False).casefold()
        if any(marker in text for marker in markers):
            evidence.append(entry)
    return evidence[:10]


def _auth_error_in_text(text: str) -> bool:
    return (
        ("401 unauthorized" in text and ("missing bearer" in text or "basic authentication" in text))
        or "requiresopenaiauth" in text
        or "codex auth required" in text
    )


def _deduplicate_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in findings:
        key = (str(item.get("category")), str(item.get("title")))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _unique_actions(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: dict[str, dict[str, Any]] = {}
    for item in findings:
        for candidate in item.get("recommendedActions") or []:
            name = str(candidate.get("action") or "")
            if name and name not in actions:
                actions[name] = candidate
    return list(actions.values())


def _seconds_since(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
