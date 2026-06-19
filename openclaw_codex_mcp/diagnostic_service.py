from __future__ import annotations

from . import tools as _tools
from .active_work import worker_active_turns_snapshot

globals().update(_tools.__dict__)


def _diagnostic_workflow_metadata(workflow: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(workflow.get("metadata_json") or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _diagnostic_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _compact_analysis_evidence(analysis: dict[str, Any]) -> bool:
    truncated = False
    containers: list[dict[str, Any]] = []
    containers.extend(item for item in analysis.get("findings") or [] if isinstance(item, dict))
    root_cause = analysis.get("likelyRootCause")
    if isinstance(root_cause, dict):
        containers.append(root_cause)
    for item in containers:
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            continue
        compacted: list[dict[str, Any]] = []
        for entry in evidence[:10]:
            compacted_entry, entry_truncated = _compact_evidence_entry(entry)
            compacted.append(compacted_entry)
            truncated = truncated or entry_truncated
        if len(evidence) > len(compacted):
            truncated = True
        item["evidence"] = compacted
        item["evidenceTruncated"] = truncated
    return truncated


def _compact_evidence_entry(entry: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(entry, dict):
        text = redact_text(entry, max_chars=500)
        return {"kind": "text", "summary": text}, len(str(entry)) > len(text)
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    summary_source = (
        entry.get("message")
        or entry.get("summary")
        or entry.get("raw")
        or payload.get("method")
        or payload.get("type")
        or entry.get("method")
        or entry.get("eventType")
        or entry.get("category")
        or ""
    )
    summary = redact_text(summary_source, max_chars=500)
    compact = {
        "kind": str(entry.get("source") or entry.get("kind") or entry.get("type") or "evidence"),
        "eventId": entry.get("eventId") or entry.get("id"),
        "method": entry.get("method") or payload.get("method"),
        "category": entry.get("category"),
        "severity": entry.get("severity"),
        "timestamp": entry.get("timestamp") or entry.get("createdAt") or entry.get("receivedAt"),
        "operationId": entry.get("operationId") or payload.get("operationId"),
        "workflowId": entry.get("workflowId") or payload.get("workflowId"),
        "threadId": entry.get("threadId") or payload.get("threadId") or payload.get("reviewThreadId"),
        "turnId": entry.get("turnId") or payload.get("turnId") or payload.get("reviewTurnId"),
        "summary": summary,
    }
    compact = {key: value for key, value in compact.items() if value not in (None, "", [])}
    raw_size = len(json.dumps(entry, ensure_ascii=False, default=str))
    compact_size = len(json.dumps(compact, ensure_ascii=False, default=str))
    return compact, raw_size > compact_size or raw_size > 500


class DiagnosticServiceMixin:
    def codex_health_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        since_minutes = _bounded_int(args.get("since_minutes", 120), 1, 10080)
        stale_after_minutes = _bounded_int(args.get("stale_after_minutes", 30), 1, 10080)
        max_recent_errors = _bounded_int(args.get("max_recent_errors", 5), 0, 50)
        generated_at = _now_iso()
        context = self._diagnostic_context(args)
        app_status = self.codex_get_app_server_status({"include_recent_events": False})
        pending = self._pending_interactions_for_diagnostics(context, limit=20)
        active_turns = self._active_turns_snapshot(app_status)
        stall_supervisor = self._stall_supervisor_snapshot(active_turns, pending)
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)).isoformat()
        stale_operations = _filter_operations_for_context(
            context,
            self.storage.list_stale_operations(stale_before=stale_before, limit=50),
        )[:20]
        premature_terminal_operations = self._premature_terminal_operations(context=context, limit=20)
        scoped_request = bool(context["operationId"] or context["workflowId"] or context["threadId"] or context["turnId"])
        check_stale_operations = stale_operations if scoped_request else []
        check_premature_terminal_operations = premature_terminal_operations if scoped_request else []
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
        historical_workflows = self.storage.list_workflows(limit=50) if not scoped_request else []
        workflows = [context["workflow"]] if context["workflow"] is not None else []
        checks = self._diagnostic_checks(
            app_status=app_status,
            pending_interactions=pending,
            workflows=workflows,
            event_pointers=recent_events,
            log_path=_diagnostic_log_path(),
            stale_operations=check_stale_operations,
            premature_terminal_operations=check_premature_terminal_operations,
        )
        hook_history = self._hook_history_snapshot()
        recommendations = _recommended_actions_from_checks(checks)
        if check_stale_operations:
            recommendations.insert(0, "recover_stale_operations")
        next_action = _health_next_action(
            app_status=app_status,
            pending_interactions=pending,
            stale_operations=check_stale_operations,
            recent_errors=recent_errors,
            checks=checks,
        )
        active_work = {
            "pendingRequests": app_status.get("pendingRequests", 0),
            "activeTurns": active_turns,
            "pendingInteractions": pending,
            "activeTurnCount": len(active_turns),
            "pendingInteractionCount": len(pending),
            "staleActiveRecordsExcluded": app_status.get("staleActiveRecordsExcluded", 0),
        }
        runtime_cache_age = (
            int(time.monotonic() - self._runtime_capabilities_cache_at)
            if self._runtime_capabilities_cache_at is not None and self._runtime_capabilities_cache is not None
            else None
        )
        historical_debt = {
            "staleOperationCount": len(stale_operations),
            "prematureTerminalOperationCount": len(premature_terminal_operations),
            "orphanedWorkflowCount": len([row for row in historical_workflows if row and row.get("phase") == "orphaned_after_app_server_exit"]),
            "staleOperations": [_operation_summary_to_tool(row) for row in stale_operations[:10]],
            "prematureTerminalOperations": [_operation_summary_to_tool(row) for row in premature_terminal_operations[:10]],
            "blocksReadiness": scoped_request and bool(stale_operations or premature_terminal_operations),
            "nextRecommendedAction": "run_targeted_cleanup" if (stale_operations or premature_terminal_operations) else "none",
            "agentGuidanceText": (
                "Это исторический долг state DB. Он не должен блокировать новые задачи, пока worker, очередь и app-server сейчас готовы."
                if (stale_operations or premature_terminal_operations) and not scoped_request
                else None
            ),
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
            "stallSupervisor": stall_supervisor,
            "historicalDebt": historical_debt,
            "staleOperations": [_operation_summary_to_tool(row) for row in stale_operations],
            "prematureTerminalOperations": [_operation_summary_to_tool(row) for row in premature_terminal_operations],
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
            "runtimeCapabilities": runtime_health_subset(
                self._runtime_capabilities_cache,
                cache_age_seconds=runtime_cache_age,
            ),
            "configHints": {
                "defaultModel": self.config.default_model,
                "defaultApprovalPolicy": self.config.default_approval_policy,
                "defaultSandboxPolicy": self.config.default_sandbox_policy,
                "startAppServerForReadTools": self.config.start_app_server_for_read_tools,
                "turnStallTimeoutSeconds": self.config.turn_stall_timeout_seconds,
                "stalledTurnAction": self.config.stalled_turn_action,
            },
            "recommendedActions": _unique_strings(recommendations),
            "nextRecommendedAction": next_action,
            "recommendedPollAfterSeconds": 15 if active_turns or pending or check_stale_operations else 0,
            "pollRecommended": bool(active_turns or pending or check_stale_operations),
        }
        return redact_payload(self._attach_agent_guidance(result, surface="health_summary"))

    def _stall_supervisor_snapshot(self, active_turns: list[dict[str, Any]], pending: list[dict[str, Any]]) -> dict[str, Any]:
        timeout_seconds = int(getattr(self.config, "turn_stall_timeout_seconds", 900) or 900)
        action = str(getattr(self.config, "stalled_turn_action", "diagnose_only") or "diagnose_only")
        pending_turn_ids = {
            _optional_string(item.get("turnId")) or _optional_string(item.get("turn_id"))
            for item in pending
            if isinstance(item, dict)
        }
        pending_thread_ids = {
            _optional_string(item.get("threadId")) or _optional_string(item.get("thread_id"))
            for item in pending
            if isinstance(item, dict)
        }
        pending_turn_ids.discard(None)
        pending_thread_ids.discard(None)
        stalled: list[dict[str, Any]] = []
        for turn in active_turns:
            staleness = _diagnostic_int_or_none(turn.get("stalenessSeconds"))
            turn_id = _optional_string(turn.get("turnId")) or _optional_string(turn.get("turn_id"))
            thread_id = _optional_string(turn.get("threadId")) or _optional_string(turn.get("thread_id"))
            has_pending = bool((turn_id and turn_id in pending_turn_ids) or (thread_id and thread_id in pending_thread_ids))
            if staleness is None or staleness < timeout_seconds or has_pending:
                continue
            stalled.append(
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "status": turn.get("status"),
                    "updatedAt": turn.get("updatedAt") or turn.get("updated_at"),
                    "stalenessSeconds": staleness,
                    "hasPendingInteraction": has_pending,
                }
            )
        return {
            "enabled": True,
            "mode": action,
            "timeoutSeconds": timeout_seconds,
            "stalledTurnCount": len(stalled),
            "stalledTurns": stalled[:20],
            "automaticInterruptEnabled": action == "interrupt",
            "nextRecommendedAction": "mark_stale_turns_orphaned" if stalled and action == "diagnose_only" else None,
        }

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
        scoped_request = bool(operation_id or workflow_id or thread_id or turn_id)
        workflows = [workflow] if workflow is not None else ([] if scoped_request else self.storage.list_workflows(limit=20))
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
        premature_terminal_operations = self._premature_terminal_operations(context=context, limit=20)
        terminal_reconcile_checks = self._terminal_reconcile_stuck_checks(context)
        log_path = _diagnostic_log_path()
        checks = self._diagnostic_checks(
            app_status=app_status,
            pending_interactions=pending,
            workflows=workflows,
            event_pointers=events,
            log_path=log_path,
            stale_operations=stale_operations,
            premature_terminal_operations=premature_terminal_operations,
        )
        checks.extend(terminal_reconcile_checks)
        scoped_checks = checks
        background_checks: list[dict[str, Any]] = []
        hook_history = self._hook_history_snapshot()
        correlation = self._diagnostic_correlation(context)
        progress_journal = self._progress_journal_snapshot(context, limit=event_limit)
        workflow_observation = None
        if workflow is not None:
            try:
                workflow_status = self._workflow_status_payload(
                    workflow,
                    last_messages=5,
                    message_max_chars=8000,
                    include_events=False,
                )
                workflow_observation = workflow_status.get("workflowObservation")
            except Exception as exc:
                workflow_observation = {"available": False, "error": redact_text(str(exc), max_chars=500)}
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
            "scopedFindings": scoped_checks,
            "backgroundFindings": background_checks,
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
            "prematureTerminalOperations": [_operation_summary_to_tool(row) for row in premature_terminal_operations],
            "operationSummary": _operation_summary_to_tool(context["operation"]) if context["operation"] is not None else None,
            "operations": [_operation_summary_to_tool(row) for row in context["operations"]],
            "workflowSummary": [_workflow_summary_to_tool(row) for row in workflows if row is not None],
            "workflowObservation": workflow_observation,
            "promptSubmissions": [_prompt_submission_summary_to_tool(row) for row in context["promptSubmissions"]],
            "correlation": correlation,
            "progressJournal": progress_journal,
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
        return redact_payload(self._attach_agent_guidance(result, surface="collect_diagnostics"))

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
        started = time.monotonic()
        problem_text = _optional_string(args.get("problem_text"))
        since_minutes = _bounded_int(args.get("since_minutes", 120), 1, 10080)
        scoped_request = bool(args.get("operation_id") or args.get("workflow_id") or args.get("thread_id") or args.get("turn_id"))
        context = self.codex_collect_diagnostics(
            {
                "operation_id": args.get("operation_id"),
                "workflow_id": args.get("workflow_id"),
                "thread_id": args.get("thread_id"),
                "turn_id": args.get("turn_id"),
                "since_minutes": since_minutes,
                "include_logs": False,
                "event_limit": _bounded_int(args.get("event_limit", 50), 1, 100),
                "timeline_limit": _bounded_int(args.get("timeline_limit", 50), 1, 100),
            }
        )
        scoped_terminal_reconcile = any(
            isinstance(item, dict) and (
                item.get("name") == "terminal_reconcile_stuck"
                or (isinstance(item.get("details"), dict) and item["details"].get("category") == "terminal_reconcile_stuck")
            )
            for item in context.get("checks") or []
        )
        logs: dict[str, Any]
        if scoped_terminal_reconcile or time.monotonic() - started > 8:
            logs = {"ok": True, "source": "skipped", "logs": [], "events": [], "returnedLogCount": 0, "returnedEventCount": 0}
        else:
            logs = self.codex_get_diagnostic_logs(
                {
                    "source": "app_server_events" if scoped_request else "all",
                    "workflow_id": context.get("filters", {}).get("workflowId") or args.get("workflow_id"),
                    "thread_id": context.get("filters", {}).get("threadId") or args.get("thread_id"),
                    "turn_id": context.get("filters", {}).get("turnId") or args.get("turn_id"),
                    "since_minutes": min(since_minutes, 60) if scoped_request else since_minutes,
                    "limit": _bounded_int(args.get("event_limit", 50), 1, 100),
                    "max_line_chars": 500,
                    "include_payload": False,
                }
            )
        analysis = analyze_context(problem_text, context, logs)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        analysis_timed_out = elapsed_ms > 10_000
        if analysis_timed_out:
            analysis["analysisTimedOut"] = True
        evidence_truncated = _compact_analysis_evidence(analysis)
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
        result = {
            "ok": True,
            "diagnosisId": diagnosis_id,
            "createdAt": created_at,
            "analysisElapsedMs": elapsed_ms,
            "analysisTimedOut": analysis_timed_out,
            "analysisScope": "scoped" if scoped_request else "global",
            "evidenceTruncated": evidence_truncated,
            **analysis,
        }
        return self._attach_agent_guidance(result, surface="analyze_issue")

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
        elif action_name == "reconcile_operations_with_tracked_turns":
            repair_result = self._repair_reconcile_operations_with_tracked_turns(args, dry_run=dry_run)
            changed = bool(repair_result.get("correctedOperationIds") or repair_result.get("refreshedFinalReportOperationIds"))
        elif action_name == "refresh_catalog_and_history":
            repair_result = self._repair_refresh_catalog_and_history(args, dry_run=dry_run)
            changed = bool(repair_result.get("changed"))
        elif action_name == "reconcile_workflow_from_thread":
            repair_result = self._repair_reconcile_workflow_from_thread(args, dry_run=dry_run)
            changed = bool(repair_result.get("changed"))
        elif action_name == "retry_workflow_with_runtime_policy":
            repair_result = await self._repair_retry_workflow_with_runtime_policy(args, dry_run=dry_run, force=force)
            changed = bool(repair_result.get("changed"))
        elif action_name == "mark_orphaned_after_exit":
            repair_result = self._repair_mark_orphaned_after_exit(args, dry_run=dry_run, force=force)
            changed = bool(repair_result.get("changed"))
        elif dry_run:
            repair_result = {"wouldRun": True, "action": action_name, "message": "Dry run only; no repair action was executed."}
        elif action_name == "restart_app_server_idle":
            repair_result = await self.codex_restart_app_server(
                {"start_after_restart": True, "timeout_seconds": _bounded_int(args.get("timeout_seconds", 30), 1, 120), "force": False, "_from_repair": True}
            )
            changed = bool(repair_result.get("restarted") or repair_result.get("started"))
        elif action_name == "force_restart_app_server":
            repair_result = await self.codex_restart_app_server(
                {"start_after_restart": True, "timeout_seconds": _bounded_int(args.get("timeout_seconds", 30), 1, 120), "force": True, "_from_repair": True}
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
                    "_from_repair": True,
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
        attempt_status = "dry_run" if dry_run else ("succeeded" if changed else "completed_no_change")
        loop_guard = self._record_guidance_attempt(
            action=action_name,
            args=args,
            result=repair_result,
            status=attempt_status,
            count_attempt=not dry_run,
            force=force,
        )
        scope_type, scope_id = attempt_scope_from_args(action_name, args)
        post_repair_guidance = build_post_repair_guidance(
            {"changed": changed, **(repair_result if isinstance(repair_result, dict) else {})},
            action=action_name,
            scope_type=scope_type,
            scope_id=scope_id,
            loop_guard=loop_guard,
        )
        result = {
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
            "loopGuard": loop_guard,
            "postRepairGuidance": post_repair_guidance,
            "postRepairGuidanceText": guidance_text(post_repair_guidance),
        }
        return result

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
        if app_status.get("workerManaged"):
            return [
                redact_payload(item)
                for item in worker_active_turns_snapshot(self.storage).get("activeTurns", [])
                if isinstance(item, dict)
            ]
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

    def _progress_journal_snapshot(self, context: dict[str, Any], *, limit: int) -> dict[str, Any]:
        turn_id = _optional_string(context.get("turnId"))
        thread_id = _optional_string(context.get("threadId"))
        rows = self.storage.list_tracked_turn_progress_events(
            thread_id=thread_id if not turn_id else None,
            turn_id=turn_id,
            limit=limit,
        )
        if turn_id:
            summary = turn_progress_status_fields(self.storage, turn_id, progress_events=min(limit, 100), progress_max_chars=1000)
        else:
            summary = {
                "progressEvents": [progress_event_to_tool(row, 1000) for row in rows],
                "progressEventCount": self.storage.count_tracked_turn_progress_events(thread_id=thread_id),
                "latestProgressAt": rows[-1].get("created_at") if rows else None,
                "tokenUsage": None,
                "modelReroutes": [],
                "warnings": [progress_event_to_tool(row, 1000) for row in rows if row.get("category") == "warning"][-10:],
            }
        return redact_payload(
            {
                "returnedEventCount": len(rows),
                "events": [progress_event_to_tool(row, 1000) for row in rows],
                "eventCount": summary.get("progressEventCount", len(rows)),
                "latestProgressAt": summary.get("latestProgressAt"),
                "tokenUsage": summary.get("tokenUsage"),
                "modelReroutes": summary.get("modelReroutes") or [],
                "warnings": summary.get("warnings") or [],
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
        for progress in self.storage.list_tracked_turn_progress_events(
            thread_id=context.get("threadId") if not context.get("turnId") else None,
            turn_id=context.get("turnId"),
            limit=min(limit, 100),
        ):
            event = progress_event_to_tool(progress, 1000)
            entries.append(
                _timeline_entry(
                    progress.get("created_at"),
                    "turn_progress",
                    str(progress.get("category") or progress.get("event_type") or "progress"),
                    operation_id=context.get("operationId"),
                    workflow_id=context.get("workflowId"),
                    thread_id=progress.get("thread_id"),
                    turn_id=progress.get("turn_id"),
                    details={
                        "eventType": progress.get("event_type"),
                        "severity": progress.get("severity"),
                        "itemId": progress.get("item_id"),
                        "text": event.get("text"),
                        "metadata": event.get("metadata"),
                    },
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
        premature_terminal_operations: list[dict[str, Any]] | None = None,
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
        has_auth_file = (self.config.codex_home / "auth.json").exists()
        has_auth_env = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"))
        checks.append(
            diagnostic_check(
                "codex_auth",
                "ok" if has_auth_file or has_auth_env else "warning",
                "Codex authentication is available for this CODEX_HOME."
                if has_auth_file or has_auth_env
                else "No Codex auth.json or API-key environment was found for this CODEX_HOME.",
                details={
                    "category": "codex_auth_required",
                    "codexHome": str(self.config.codex_home),
                    "authJsonPresent": has_auth_file,
                    "apiKeyEnvPresent": has_auth_env,
                },
                suggested_action="reauthenticate_codex_home",
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
        premature_terminal_operations = premature_terminal_operations or []
        if premature_terminal_operations:
            checks.append(
                diagnostic_check(
                    "premature_terminal_operation",
                    "error",
                    f"{len(premature_terminal_operations)} operations are terminal while their tracked turns are not trusted terminal.",
                    details={
                        "count": len(premature_terminal_operations),
                        "operationIds": [row.get("operation_id") for row in premature_terminal_operations[:20]],
                        "category": "premature_terminal_operation",
                    },
                    suggested_action="reconcile_operations_with_tracked_turns",
                )
            )
        else:
            checks.append(diagnostic_check("premature_terminal_operation", "ok", "No premature terminal operations found."))
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
            "configExists": bool(install_status.get("configExists") or (install_status.get("configPath") and Path(str(install_status.get("configPath"))).exists())),
            "stateDb": str(self.config.state_db_path),
            "stateDbExists": self.config.state_db_path.exists(),
            "dbWritable": db_writable,
            "dbError": db_error,
            "threadCount": storage_status["threadCount"],
            "turnCount": storage_status["turnCount"],
            "messageCount": storage_status["messageCount"],
            "lastHookEventAt": storage_status["lastHookEventAt"],
            "source": "mcp_state_db+codex_hooks_json",
            "warnings": warnings,
        }

    def _premature_terminal_operations(self, *, context: dict[str, Any] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        terminal_placeholders = ",".join("?" for _ in OPERATION_TERMINAL_STATUSES)
        clauses = [
            f"operations.status IN ({terminal_placeholders})",
            "operations.turn_id IS NOT NULL",
            """
            (
              turns.turn_id IS NULL
              OR turns.status NOT IN ('completed', 'failed', 'aborted', 'cancelled', 'canceled', 'interrupted', 'unknown_after_app_server_exit')
              OR turns.completed_at IS NULL
            )
            """,
        ]
        params: list[Any] = list(OPERATION_TERMINAL_STATUSES)
        context = context or {}
        operation_id = _optional_string(context.get("operationId"))
        workflow_id = _optional_string(context.get("workflowId"))
        thread_id = _optional_string(context.get("threadId"))
        turn_id = _optional_string(context.get("turnId"))
        if operation_id:
            clauses.append("operations.operation_id = ?")
            params.append(operation_id)
        if workflow_id:
            clauses.append("operations.workflow_id = ?")
            params.append(workflow_id)
        if thread_id:
            clauses.append("operations.thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("operations.turn_id = ?")
            params.append(turn_id)
        where = " AND ".join(f"({clause})" for clause in clauses)
        rows = self.storage.connection.execute(
            f"""
            SELECT operations.*
            FROM codex_operations operations
            LEFT JOIN tracked_turns turns ON turns.turn_id = operations.turn_id
            WHERE {where}
            ORDER BY operations.updated_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def _terminal_reconcile_stuck_checks(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        operation = context.get("operation") if isinstance(context.get("operation"), dict) else None
        if operation is None:
            return []
        operation_status = str(operation.get("status") or "")
        if operation_status in OPERATION_TERMINAL_STATUSES:
            return []
        turn_id = _optional_string(operation.get("turn_id"))
        if not turn_id:
            return []
        thread_id = _optional_string(operation.get("thread_id"))
        try:
            turn_status = self.codex_get_turn_status(
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "last_messages": 3,
                    "message_max_chars": 2000,
                    "progress_events": 0,
                }
            )
        except Exception:
            return []
        if not _turn_status_has_trusted_terminal_evidence(turn_status):
            return []
        turn_state = str(turn_status.get("status") or "")
        if turn_state not in TURN_TERMINAL_STATUSES:
            return []
        evidence = turn_status.get("terminalEvidence") if isinstance(turn_status.get("terminalEvidence"), dict) else {}
        return [
            diagnostic_check(
                "terminal_reconcile_stuck",
                "warning",
                "Operation is still non-terminal, but the same turn has trusted terminal evidence.",
                operationId=operation.get("operation_id"),
                threadId=thread_id,
                turnId=turn_id,
                operationStatus=operation_status,
                turnStatus=turn_state,
                terminalEvidence=evidence,
                category="terminal_reconcile_stuck",
                suggestedAction="reconcile_operations_with_tracked_turns",
            )
        ]

    def _terminal_reconcile_candidate_operations(self, *, context: dict[str, Any] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        terminal_placeholders = ",".join("?" for _ in OPERATION_TERMINAL_STATUSES)
        clauses = [
            f"operations.status NOT IN ({terminal_placeholders})",
            "operations.turn_id IS NOT NULL",
        ]
        params: list[Any] = list(OPERATION_TERMINAL_STATUSES)
        context = context or {}
        operation_id = _optional_string(context.get("operationId"))
        workflow_id = _optional_string(context.get("workflowId"))
        thread_id = _optional_string(context.get("threadId"))
        turn_id = _optional_string(context.get("turnId"))
        if operation_id:
            clauses.append("operations.operation_id = ?")
            params.append(operation_id)
        if workflow_id:
            clauses.append("operations.workflow_id = ?")
            params.append(workflow_id)
        if thread_id:
            clauses.append("operations.thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("operations.turn_id = ?")
            params.append(turn_id)
        where = " AND ".join(f"({clause})" for clause in clauses)
        rows = self.storage.connection.execute(
            f"""
            SELECT operations.*
            FROM codex_operations operations
            WHERE {where}
            ORDER BY operations.updated_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

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

    def _repair_reconcile_operations_with_tracked_turns(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        context = self._diagnostic_context(args)
        operations_by_id: dict[str, dict[str, Any]] = {}
        for row in self._premature_terminal_operations(context=context, limit=100):
            operations_by_id[str(row.get("operation_id") or "")] = row
        for row in self._terminal_reconcile_candidate_operations(context=context, limit=100):
            operations_by_id[str(row.get("operation_id") or "")] = row
        operations = [row for key, row in operations_by_id.items() if key]
        corrected: list[str] = []
        refreshed_reports: list[str] = []
        previews: list[dict[str, Any]] = []
        for operation in operations:
            operation_id = str(operation.get("operation_id") or "")
            turn_id = _optional_string(operation.get("turn_id"))
            thread_id = _optional_string(operation.get("thread_id"))
            if not operation_id or not turn_id:
                continue
            try:
                turn_status = self.codex_get_turn_status(
                    {
                        "turn_id": turn_id,
                        "thread_id": thread_id,
                        "last_messages": 10,
                        "message_max_chars": 8000,
                        "progress_events": 0,
                    }
                )
            except Exception:
                previews.append({"operationId": operation_id, "action": "skip", "reason": "tracked turn missing"})
                continue
            before_status = str(operation.get("status") or "")
            before_report_hash = operation.get("latest_report_hash")
            if dry_run:
                target_status = "running"
                if _turn_status_has_trusted_terminal_evidence(turn_status):
                    target_status = str(turn_status.get("status") or "unknown")
                previews.append(
                    {
                        "operationId": operation_id,
                        "turnId": turn_id,
                        "threadId": thread_id,
                        "currentStatus": before_status,
                        "targetStatus": target_status,
                        "trustedTerminal": _turn_status_has_trusted_terminal_evidence(turn_status),
                    }
                )
                continue
            updated = self._reconcile_operation_with_turn(operation, turn_status)
            after_status = str(updated.get("status") or "")
            if after_status != before_status:
                corrected.append(operation_id)
            final_report = self._operation_final_report(updated, turn_status=turn_status, message_max_chars=8000)
            refreshed = self.storage.get_operation(operation_id) or updated
            if final_report is not None and refreshed.get("latest_report_hash") != before_report_hash:
                refreshed_reports.append(operation_id)
        return {
            "dryRun": dry_run,
            "inspectedOperationIds": [row.get("operation_id") for row in operations],
            "wouldReconcile": dry_run,
            "previews": previews,
            "correctedOperationIds": corrected,
            "refreshedFinalReportOperationIds": refreshed_reports,
        }

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

    def _repair_reconcile_workflow_from_thread(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        workflow_id = _required_string(args, "workflow_id")
        workflow = self.storage.get_workflow(workflow_id)
        if workflow is None:
            raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
        thread_id = _optional_string(workflow.get("thread_id"))
        if dry_run:
            return {
                "dryRun": True,
                "changed": False,
                "workflowId": workflow_id,
                "threadId": thread_id,
                "wouldImportTranscript": bool(thread_id),
                "wouldRecomputeWorkflowObservation": True,
            }
        import_status = self._refresh_workflow_thread_tracking(workflow, thread_id=thread_id)
        status = self._workflow_status_payload(
            self.storage.get_workflow(workflow_id) or workflow,
            last_messages=10,
            message_max_chars=8000,
            include_events=True,
        )
        observation = status.get("workflowObservation") or {}
        return {
            "dryRun": dry_run,
            "changed": bool(import_status.get("imported")) and not dry_run,
            "workflowId": workflow_id,
            "threadId": thread_id,
            "importStatus": import_status,
            "workflowObservation": observation,
            "candidatePlans": observation.get("candidatePlans") if isinstance(observation, dict) else [],
            "nextRecommendedAction": status.get("nextRecommendedAction"),
            "message": "Review candidatePlans and call codex_adopt_workflow_plan explicitly when a candidate is acceptable.",
        }

    async def _repair_retry_workflow_with_runtime_policy(
        self,
        args: dict[str, Any],
        *,
        dry_run: bool,
        force: bool,
    ) -> dict[str, Any]:
        workflow_id = _required_string(args, "workflow_id")
        workflow = self.storage.get_workflow(workflow_id)
        if workflow is None:
            raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
        if (_optional_string(workflow.get("workflow_kind")) or "plan_then_execute") != "plan_then_execute":
            raise invalid_argument(
                "retry_workflow_with_runtime_policy currently supports only plan_then_execute workflows.",
                workflow_id=workflow_id,
                workflow_kind=workflow.get("workflow_kind"),
            )
        phase = str(workflow.get("phase") or "")
        status = str(workflow.get("status") or "")
        retryable_states = {
            "completed",
            "failed",
            "orphaned",
            "orphaned_after_app_server_exit",
            "unknown_after_app_server_exit",
        }
        retryable = phase in retryable_states or status in retryable_states
        if not dry_run and not force and not retryable:
            raise invalid_argument(
                "retry_workflow_with_runtime_policy requires a terminal/failed/orphaned workflow or force=true.",
                workflow_id=workflow_id,
                phase=phase,
                status=status,
            )
        plan_operation_id = _optional_string(workflow.get("plan_operation_id"))
        plan_operation = self.storage.get_operation(plan_operation_id) if plan_operation_id else None
        if plan_operation is None:
            raise invalid_argument(
                "Source workflow has no durable plan operation to retry from.",
                workflow_id=workflow_id,
                plan_operation_id=plan_operation_id,
            )
        request_payload = _operation_request_from_row(plan_operation)
        message = _optional_string(request_payload.get("message"))
        if not message:
            raise invalid_argument("Source workflow plan operation has no retryable message.", workflow_id=workflow_id)
        sandbox = _optional_string(args.get("sandbox")) or _sandbox_value_from_policy(self.config.default_sandbox_policy)
        approval_policy = _optional_string(args.get("approval_policy")) or self.config.default_approval_policy
        reason = _bounded_optional_text(args.get("reason"), field_name="reason", max_chars=4000)
        retry_client_request_id = (
            _optional_string(args.get("client_request_id"))
            or f"repair:retry_workflow_with_runtime_policy:{workflow_id}:{prompt_hash(normalize_prompt(sandbox + ':' + approval_policy))}"
        )
        retry_args: dict[str, Any] = {
            "project_id": workflow.get("project_id") or request_payload.get("project_id"),
            "message": message,
            "title": request_payload.get("title"),
            "cwd": request_payload.get("cwd"),
            "model": request_payload.get("model"),
            "sandbox": sandbox,
            "approval_policy": approval_policy,
            "client_request_id": retry_client_request_id,
            "goal": workflow.get("goal_objective"),
            "goal_token_budget": workflow.get("goal_token_budget"),
            "goal_completion_action": workflow.get("goal_completion_action") or "clear",
            "goal_completion_objective": workflow.get("goal_completion_objective"),
            "timeout_seconds": _bounded_int(
                request_payload.get("timeout_seconds", args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS)),
                1,
                7200,
            ),
            "first_message_max_chars": _bounded_int(
                request_payload.get("first_message_max_chars", 8000),
                500,
                200000,
            ),
            "_skip_prompt_dedup": True,
            "_prompt_dedup_basis": f"retry_workflow:{workflow_id}:{retry_client_request_id}:{prompt_hash(normalize_prompt(message))}",
        }
        retry_args = {key: value for key, value in retry_args.items() if value not in (None, "")}
        preview_request = {
            key: value
            for key, value in retry_args.items()
            if key not in {"message"} and not str(key).startswith("_")
        }
        preview_request.update(
            {
                "messagePreview": _redacted_preview(message, 160),
                "messageChars": len(message),
                "messageHash": prompt_hash(normalize_prompt(message)),
            }
        )
        runtime_policy = _plan_mode_runtime_policy(
            {
                "sandbox": sandbox,
                "approval_policy": approval_policy,
                "collaboration_mode": "plan",
            },
            default_sandbox_policy=self.config.default_sandbox_policy,
            default_approval_policy=self.config.default_approval_policy,
        )
        source_workflow = {
            "workflowId": workflow_id,
            "phase": phase,
            "status": status,
            "threadId": _optional_string(workflow.get("thread_id")),
            "planTurnId": _optional_string(workflow.get("plan_turn_id")),
            "planOperationId": plan_operation_id,
        }
        if dry_run:
            return {
                "dryRun": True,
                "changed": False,
                "wouldCreateWorkflow": True,
                "sourceWorkflow": source_workflow,
                "plannedRequest": preview_request,
                "runtimePolicy": runtime_policy,
                "replacesWorkflowId": workflow_id,
                "nextRecommendedAction": "run_repair_with_dry_run_false",
            }

        started = await self.codex_start_plan_workflow(retry_args)
        new_workflow_id = _optional_string(started.get("workflowId"))
        if not new_workflow_id:
            raise send_failed("Workflow retry did not create a new workflow.")
        now = _now_iso()
        new_workflow = self.storage.get_workflow(new_workflow_id)
        if new_workflow is not None:
            new_metadata = _diagnostic_workflow_metadata(new_workflow)
            existing_replaces = _optional_string(new_metadata.get("replacesWorkflowId"))
            if existing_replaces and existing_replaces != workflow_id:
                raise invalid_argument(
                    "client_request_id already belongs to retry of another workflow.",
                    client_request_id=retry_client_request_id,
                    existing_replaces_workflow_id=existing_replaces,
                    requested_replaces_workflow_id=workflow_id,
                )
            new_metadata.update(
                {
                    "replacesWorkflowId": workflow_id,
                    "retryOfWorkflowId": workflow_id,
                    "retryReason": reason,
                    "retryCreatedAt": now,
                    "runtimePolicy": started.get("runtimePolicy") or runtime_policy,
                    "sourcePlanOperationId": plan_operation_id,
                }
            )
            self.storage.update_workflow(
                new_workflow_id,
                metadata_json=json.dumps(new_metadata, ensure_ascii=False, sort_keys=True),
                updated_at=now,
            )
        source_metadata = _diagnostic_workflow_metadata(workflow)
        source_metadata["replacedByWorkflowId"] = new_workflow_id
        source_metadata["retryReason"] = reason
        source_metadata["retryCreatedAt"] = now
        self.storage.update_workflow(
            workflow_id,
            metadata_json=json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
            updated_at=now,
        )
        self.storage.record_workflow_event(
            workflow_id,
            event_type="workflow_retry_created",
            message="Workflow retry created with adjusted runtime policy.",
            details={
                "newWorkflowId": new_workflow_id,
                "clientRequestId": retry_client_request_id,
                "runtimePolicy": started.get("runtimePolicy") or runtime_policy,
                "reason": reason,
            },
            created_at=now,
        )
        self.storage.record_workflow_event(
            new_workflow_id,
            event_type="workflow_retry_lineage_attached",
            message="Workflow retry linked to source workflow.",
            details={
                "replacesWorkflowId": workflow_id,
                "sourcePlanOperationId": plan_operation_id,
                "reason": reason,
            },
            created_at=now,
        )
        new_status = self.codex_get_workflow_status({"workflow_id": new_workflow_id, "include_events": True})
        return {
            "dryRun": False,
            "changed": True,
            "newWorkflowId": new_workflow_id,
            "replacesWorkflowId": workflow_id,
            "newPlanOperationId": started.get("planOperationId"),
            "runtimePolicy": new_status.get("runtimePolicy") or started.get("runtimePolicy") or runtime_policy,
            "sourceWorkflow": source_workflow,
            "newWorkflow": new_status,
            "idempotent": bool(started.get("idempotent")),
            "nextRecommendedAction": "poll_workflow",
            "recommendedPollAfterSeconds": new_status.get("recommendedPollAfterSeconds", 1),
            "pollRecommended": True,
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

