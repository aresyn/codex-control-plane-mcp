from __future__ import annotations

from . import tools as _tools

globals().update(_tools.__dict__)


def _workflow_metadata(workflow: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(workflow.get("metadata_json") or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _workflow_retry_state(workflow: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else _workflow_metadata(workflow)
    return {
        "replacesWorkflowId": metadata.get("replacesWorkflowId"),
        "replacedByWorkflowId": metadata.get("replacedByWorkflowId"),
        "retryOfWorkflowId": metadata.get("retryOfWorkflowId"),
        "retryReason": metadata.get("retryReason"),
        "retryCreatedAt": metadata.get("retryCreatedAt"),
    }


class WorkflowServiceMixin:
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
        goal_objective = _bounded_optional_text(args.get("goal"), field_name="goal", max_chars=20000)
        goal_token_budget = _optional_bounded_int(args.get("goal_token_budget"), 1, 10000000, field_name="goal_token_budget")
        goal_completion_action = _optional_string(args.get("goal_completion_action")) or "clear"
        if goal_completion_action not in GOAL_COMPLETION_ACTIONS:
            raise invalid_argument("Unsupported goal_completion_action.", goal_completion_action=goal_completion_action)
        goal_completion_objective = _bounded_optional_text(
            args.get("goal_completion_objective"),
            field_name="goal_completion_objective",
            max_chars=20000,
        )
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
                "goal_objective": goal_objective,
                "goal_token_budget": goal_token_budget,
                "goal_completion_action": goal_completion_action,
                "goal_completion_objective": goal_completion_objective,
                "goal_sync_state": "pending_thread" if goal_objective else "not_configured",
                "goal_app_server_json": None,
                "goal_last_error": None,
                "goal_last_synced_at": None,
                "goal_cleared_at": None,
                "goal_managed_hash": _workflow_goal_hash(goal_objective, goal_token_budget) if goal_objective else None,
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
                        "goalConfigured": bool(goal_objective),
                        "runtimePolicy": start_operation.get("runtimePolicy"),
                    },
                    ensure_ascii=False,
                ),
            }
        )
        self.storage.record_workflow_event(
            workflow_id,
            event_type="workflow_started",
            message="Plan workflow queued.",
            details={"planOperationId": plan_operation_id, "goalConfigured": bool(goal_objective)},
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

    async def codex_get_workflow_status_async(self, args: dict[str, Any]) -> dict[str, Any]:
        workflow_id = _required_string(args, "workflow_id")
        workflow = self.storage.get_workflow(workflow_id)
        if workflow is None:
            raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
        last_messages = _bounded_int(args.get("last_messages", 10), 1, 50)
        message_max_chars = _bounded_int(args.get("message_max_chars", 8000), 500, 200000)
        include_events = bool(args.get("include_events", True))
        result = self._workflow_status_payload(
            workflow,
            last_messages=last_messages,
            message_max_chars=message_max_chars,
            include_events=include_events,
        )
        if str(workflow.get("workflow_kind") or "") == "code_review":
            return result
        synced = await self._sync_workflow_goal_for_status(workflow_id, phase=str(result.get("phase") or ""))
        if synced is not None:
            result["threadGoal"] = self._workflow_goal_status(synced)
            if include_events:
                result["events"] = [_workflow_event_to_tool(row) for row in self.storage.list_workflow_events(workflow_id, limit=20)]
        return result

    async def _sync_workflow_goal_for_status(self, workflow_id: str, *, phase: str) -> dict[str, Any] | None:
        workflow = self.storage.get_workflow(workflow_id)
        if workflow is None:
            return None
        thread_id = _optional_string(workflow.get("thread_id"))
        objective = _optional_string(workflow.get("goal_objective"))
        if not thread_id:
            if objective and workflow.get("goal_sync_state") != "pending_thread":
                self.storage.update_workflow(workflow_id, goal_sync_state="pending_thread", goal_last_error=None, updated_at=_now_iso())
                workflow = self.storage.get_workflow(workflow_id) or workflow
            return workflow
        if not objective:
            return await self._observe_unmanaged_workflow_goal(workflow)
        if phase == "completed" or workflow.get("phase") == "completed":
            return await self._sync_completed_workflow_goal(workflow)
        return await self._sync_active_workflow_goal(workflow)

    async def _observe_unmanaged_workflow_goal(self, workflow: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        thread_id = _optional_string(workflow.get("thread_id"))
        if not thread_id or self._app_server is None or self._app_server.process is None or self._app_server.process.returncode is not None:
            return workflow
        try:
            result = await self._app_server.thread_goal_get(thread_id, timeout_seconds=2)
        except Exception as exc:
            state = "unsupported" if _is_goal_unsupported_error(exc) else "error"
            self._update_workflow_goal_state(
                workflow_id,
                goal_sync_state=state,
                goal_last_error=redact_text(str(exc), max_chars=500),
                event_type="goal_sync_unsupported" if state == "unsupported" else "goal_sync_failed",
                event_message="Codex thread goal could not be read.",
                event_details={"threadId": thread_id, "state": state},
            )
            return self.storage.get_workflow(workflow_id) or workflow
        goal = _extract_thread_goal(result)
        self.storage.update_workflow(
            workflow_id,
            goal_sync_state="observed" if goal is not None else "not_configured",
            goal_app_server_json=_thread_goal_json(goal),
            goal_last_error=None,
            goal_last_synced_at=_now_iso(),
            updated_at=_now_iso(),
            app_server_generation=result.get("_processGeneration") or self._app_server.process_generation,
        )
        return self.storage.get_workflow(workflow_id) or workflow

    async def _sync_active_workflow_goal(self, workflow: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        thread_id = _optional_string(workflow.get("thread_id"))
        objective = _optional_string(workflow.get("goal_objective"))
        if not thread_id or not objective:
            return workflow
        state = str(workflow.get("goal_sync_state") or "not_configured")
        token_budget = _optional_bounded_int(workflow.get("goal_token_budget"), 1, 10000000, field_name="goal_token_budget")
        managed_hash = _optional_string(workflow.get("goal_managed_hash")) or _workflow_goal_hash(objective, token_budget)
        if state == "external_override":
            return workflow
        if state == "error":
            stored_goal = _workflow_goal_from_json(workflow.get("goal_app_server_json"))
            if stored_goal is not None and _thread_goal_hash(stored_goal) == managed_hash:
                self._update_workflow_goal_state(
                    workflow_id,
                    goal_sync_state="active",
                    goal_app_server_json=_thread_goal_json(stored_goal),
                    goal_last_error=None,
                    goal_last_synced_at=_now_iso(),
                    app_server_generation=workflow.get("app_server_generation"),
                    event_type="goal_set_observed",
                    event_message="Codex thread goal already matches MCP managed goal.",
                    event_details={"threadId": thread_id, "tokenBudget": token_budget},
                )
                return self.storage.get_workflow(workflow_id) or workflow
        client = await self._app()
        if state == "error":
            try:
                result = await client.thread_goal_get(thread_id, timeout_seconds=2)
            except Exception:
                result = None
            if isinstance(result, dict):
                current = _extract_thread_goal(result)
                current_hash = _thread_goal_hash(current)
                if current is not None and current_hash == managed_hash:
                    self._update_workflow_goal_state(
                        workflow_id,
                        goal_sync_state="active",
                        goal_app_server_json=_thread_goal_json(current),
                        goal_last_error=None,
                        goal_last_synced_at=_now_iso(),
                        app_server_generation=result.get("_processGeneration") or client.process_generation,
                        event_type="goal_set_observed",
                        event_message="Codex thread goal already matches MCP managed goal.",
                        event_details={"threadId": thread_id, "tokenBudget": token_budget},
                    )
                    return self.storage.get_workflow(workflow_id) or workflow
                if current is not None and current_hash and current_hash != managed_hash:
                    self._update_workflow_goal_state(
                        workflow_id,
                        goal_sync_state="external_override",
                        goal_app_server_json=_thread_goal_json(current),
                        goal_last_error=None,
                        goal_last_synced_at=_now_iso(),
                        app_server_generation=result.get("_processGeneration") or client.process_generation,
                        event_type="goal_external_override",
                        event_message="Codex thread goal changed outside MCP management.",
                        event_details={"threadId": thread_id},
                    )
                    return self.storage.get_workflow(workflow_id) or workflow
        if state == "active":
            try:
                result = await client.thread_goal_get(thread_id, timeout_seconds=2)
            except Exception as exc:
                return self._mark_workflow_goal_error(workflow, exc, action="read")
            current = _extract_thread_goal(result)
            current_hash = _thread_goal_hash(current)
            if current is not None and current_hash and current_hash != managed_hash:
                self._update_workflow_goal_state(
                    workflow_id,
                    goal_sync_state="external_override",
                    goal_app_server_json=_thread_goal_json(current),
                    goal_last_error=None,
                    goal_last_synced_at=_now_iso(),
                    app_server_generation=result.get("_processGeneration") or client.process_generation,
                    event_type="goal_external_override",
                    event_message="Codex thread goal changed outside MCP management.",
                    event_details={"threadId": thread_id},
                )
                return self.storage.get_workflow(workflow_id) or workflow
            self.storage.update_workflow(
                workflow_id,
                goal_app_server_json=_thread_goal_json(current),
                goal_last_error=None,
                goal_last_synced_at=_now_iso(),
                updated_at=_now_iso(),
                app_server_generation=result.get("_processGeneration") or client.process_generation,
            )
            return self.storage.get_workflow(workflow_id) or workflow
        try:
            result = await client.thread_goal_set(
                thread_id,
                objective=objective,
                status="active",
                token_budget=token_budget,
                timeout_seconds=5,
            )
        except Exception as exc:
            return self._mark_workflow_goal_error(workflow, exc, action="set")
        goal = _extract_thread_goal(result)
        self._update_workflow_goal_state(
            workflow_id,
            goal_sync_state="active",
            goal_app_server_json=_thread_goal_json(goal),
            goal_last_error=None,
            goal_last_synced_at=_now_iso(),
            goal_cleared_at=None,
            goal_managed_hash=managed_hash,
            app_server_generation=result.get("_processGeneration") or client.process_generation,
            event_type="goal_set",
            event_message="Codex thread goal set for workflow.",
            event_details={"threadId": thread_id, "tokenBudget": token_budget},
        )
        return self.storage.get_workflow(workflow_id) or workflow

    async def _sync_completed_workflow_goal(self, workflow: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        thread_id = _optional_string(workflow.get("thread_id"))
        objective = _optional_string(workflow.get("goal_objective"))
        if not thread_id or not objective:
            return workflow
        state = str(workflow.get("goal_sync_state") or "not_configured")
        if state in {"cleared", "complete", "left", "external_override", "unsupported"}:
            return workflow
        if state not in {"active", "observed"}:
            return workflow
        completion_action = _optional_string(workflow.get("goal_completion_action")) or "clear"
        if completion_action not in GOAL_COMPLETION_ACTIONS:
            completion_action = "clear"
        token_budget = _optional_bounded_int(workflow.get("goal_token_budget"), 1, 10000000, field_name="goal_token_budget")
        managed_hash = _optional_string(workflow.get("goal_managed_hash")) or _workflow_goal_hash(objective, token_budget)
        client = await self._app()
        try:
            current_result = await client.thread_goal_get(thread_id, timeout_seconds=2)
        except Exception as exc:
            return self._mark_workflow_goal_error(workflow, exc, action="read")
        current = _extract_thread_goal(current_result)
        current_hash = _thread_goal_hash(current)
        if current is None:
            self._update_workflow_goal_state(
                workflow_id,
                goal_sync_state="cleared",
                goal_app_server_json=None,
                goal_last_error=None,
                goal_last_synced_at=_now_iso(),
                goal_cleared_at=_now_iso(),
                app_server_generation=current_result.get("_processGeneration") or client.process_generation,
            )
            return self.storage.get_workflow(workflow_id) or workflow
        if current_hash and current_hash != managed_hash:
            self._update_workflow_goal_state(
                workflow_id,
                goal_sync_state="external_override",
                goal_app_server_json=_thread_goal_json(current),
                goal_last_error=None,
                goal_last_synced_at=_now_iso(),
                app_server_generation=current_result.get("_processGeneration") or client.process_generation,
                event_type="goal_external_override",
                event_message="Codex thread goal changed outside MCP management; completion action skipped.",
                event_details={"threadId": thread_id, "completionAction": completion_action},
            )
            return self.storage.get_workflow(workflow_id) or workflow
        if completion_action == "leave":
            self._update_workflow_goal_state(
                workflow_id,
                goal_sync_state="left",
                goal_app_server_json=_thread_goal_json(current),
                goal_last_error=None,
                goal_last_synced_at=_now_iso(),
                app_server_generation=current_result.get("_processGeneration") or client.process_generation,
                event_type="goal_left",
                event_message="Codex thread goal left unchanged after workflow completion.",
                event_details={"threadId": thread_id},
            )
            return self.storage.get_workflow(workflow_id) or workflow
        if completion_action == "set_complete":
            completion_objective = _optional_string(workflow.get("goal_completion_objective")) or objective
            try:
                result = await client.thread_goal_set(
                    thread_id,
                    objective=completion_objective,
                    status="complete",
                    token_budget=token_budget,
                    timeout_seconds=5,
                )
            except Exception as exc:
                return self._mark_workflow_goal_error(workflow, exc, action="set_complete")
            goal = _extract_thread_goal(result)
            self._update_workflow_goal_state(
                workflow_id,
                goal_sync_state="complete",
                goal_app_server_json=_thread_goal_json(goal),
                goal_last_error=None,
                goal_last_synced_at=_now_iso(),
                goal_managed_hash=_workflow_goal_hash(completion_objective, token_budget),
                app_server_generation=result.get("_processGeneration") or client.process_generation,
                event_type="goal_completed",
                event_message="Codex thread goal marked complete after workflow completion.",
                event_details={"threadId": thread_id},
            )
            return self.storage.get_workflow(workflow_id) or workflow
        try:
            result = await client.thread_goal_clear(thread_id, timeout_seconds=5)
        except Exception as exc:
            return self._mark_workflow_goal_error(workflow, exc, action="clear")
        self._update_workflow_goal_state(
            workflow_id,
            goal_sync_state="cleared",
            goal_app_server_json=None,
            goal_last_error=None,
            goal_last_synced_at=_now_iso(),
            goal_cleared_at=_now_iso(),
            app_server_generation=result.get("_processGeneration") or client.process_generation,
            event_type="goal_cleared",
            event_message="Codex thread goal cleared after workflow completion.",
            event_details={"threadId": thread_id, "cleared": bool(result.get("cleared", True))},
        )
        return self.storage.get_workflow(workflow_id) or workflow

    def _mark_workflow_goal_error(self, workflow: dict[str, Any], exc: Exception, *, action: str) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        state = "unsupported" if _is_goal_unsupported_error(exc) else "error"
        self._update_workflow_goal_state(
            workflow_id,
            goal_sync_state=state,
            goal_last_error=redact_text(str(exc), max_chars=500),
            event_type="goal_sync_unsupported" if state == "unsupported" else "goal_sync_failed",
            event_message=f"Codex thread goal {action} failed.",
            event_details={"action": action, "state": state},
        )
        return self.storage.get_workflow(workflow_id) or workflow

    def _update_workflow_goal_state(
        self,
        workflow_id: str,
        *,
        event_type: str | None = None,
        event_message: str | None = None,
        event_details: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        now = _now_iso()
        update_fields = {**fields, "updated_at": now}
        self.storage.update_workflow(workflow_id, **update_fields)
        if event_type:
            self.storage.record_workflow_event(
                workflow_id,
                event_type=event_type,
                message=event_message or event_type,
                details=event_details or {},
                created_at=now,
            )

    def _workflow_goal_status(self, workflow: dict[str, Any]) -> dict[str, Any]:
        objective = _optional_string(workflow.get("goal_objective"))
        current_goal = _workflow_goal_from_json(workflow.get("goal_app_server_json"))
        sync_state = str(workflow.get("goal_sync_state") or ("pending_thread" if objective else "not_configured"))
        return {
            "configured": bool(objective),
            "managed": bool(objective) and sync_state in {"pending_thread", "active", "complete", "cleared", "left"},
            "syncState": sync_state,
            "completionAction": workflow.get("goal_completion_action") or "clear",
            "desiredObjective": redact_text(objective, max_chars=1000) if objective else None,
            "tokenBudget": workflow.get("goal_token_budget"),
            "currentGoal": current_goal,
            "lastSyncedAt": workflow.get("goal_last_synced_at"),
            "clearedAt": workflow.get("goal_cleared_at"),
            "lastError": workflow.get("goal_last_error"),
            "available": sync_state != "unsupported",
        }

    def codex_adopt_workflow_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        workflow_id = _required_string(args, "workflow_id")
        candidate_turn_id = _required_string(args, "candidate_turn_id")
        candidate_plan_hash = _required_string(args, "candidate_plan_hash")
        workflow = self.storage.get_workflow(workflow_id)
        if workflow is None:
            raise invalid_argument("Codex workflow was not found.", workflow_id=workflow_id)
        if str(workflow.get("workflow_kind") or "plan_then_execute") != "plan_then_execute":
            raise invalid_argument("Only plan_then_execute workflows can adopt a plan.", workflow_id=workflow_id)
        if _optional_string(workflow.get("execution_operation_id")) or _optional_string(workflow.get("execution_turn_id")):
            raise invalid_argument("Workflow execution already started; plan adoption is no longer allowed.", workflow_id=workflow_id)

        thread_id = _optional_string(workflow.get("thread_id"))
        self._refresh_workflow_thread_tracking(workflow, thread_id=thread_id)
        plan_rows = self.storage.get_tracked_turn_plans(candidate_turn_id)
        matching: dict[str, Any] | None = None
        for row in plan_rows:
            text = str(row.get("text") or "")
            if plan_hash_for_text(text) == candidate_plan_hash:
                matching = row
                break
        if matching is None:
            raise invalid_argument(
                "Candidate plan was not found for this workflow thread.",
                workflow_id=workflow_id,
                candidate_turn_id=candidate_turn_id,
                candidate_plan_hash=candidate_plan_hash,
            )
        if thread_id and _optional_string(matching.get("thread_id")) != thread_id:
            raise invalid_argument(
                "Candidate plan belongs to a different thread.",
                workflow_id=workflow_id,
                workflow_thread_id=thread_id,
                candidate_thread_id=matching.get("thread_id"),
            )
        quality = classify_plan_artifact(matching.get("text"), matching.get("payload_json"))
        if quality != "valid_plan":
            raise invalid_argument(
                "Candidate plan is not a valid proposed_plan artifact.",
                workflow_id=workflow_id,
                candidate_turn_id=candidate_turn_id,
                planQuality=quality,
            )

        already_adopted = (
            _optional_string(workflow.get("plan_turn_id")) == candidate_turn_id
            and _optional_string(workflow.get("latest_plan_hash")) == candidate_plan_hash
        )
        now = _now_iso()
        if not already_adopted:
            self.storage.update_workflow(
                workflow_id,
                plan_turn_id=candidate_turn_id,
                latest_plan_item_id=str(matching.get("item_id") or matching.get("id") or ""),
                latest_plan_hash=candidate_plan_hash,
                phase="plan_ready",
                status="plan_ready",
                last_error=None,
                completed_at=None,
                updated_at=now,
            )
            self.storage.record_workflow_event(
                workflow_id,
                event_type="workflow_plan_adopted",
                message="Workflow plan adopted from a newer candidate turn.",
                details={
                    "candidateTurnId": candidate_turn_id,
                    "candidatePlanHash": candidate_plan_hash,
                    "clientRequestId": _optional_string(args.get("client_request_id")),
                    "adoptionNote": _optional_string(args.get("adoption_note")),
                },
                created_at=now,
            )
        refreshed = self.storage.get_workflow(workflow_id) or workflow
        status = self._workflow_status_payload(
            refreshed,
            last_messages=10,
            message_max_chars=_bounded_int(args.get("message_max_chars", 8000), 500, 200000),
            include_events=True,
        )
        status["idempotent"] = already_adopted
        status["idempotencyScope"] = "adopt_plan"
        status["adoptedPlan"] = {
            "turnId": candidate_turn_id,
            "planHash": candidate_plan_hash,
            "itemId": matching.get("item_id"),
            "quality": quality,
        }
        return status

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
        latest_plan_quality = classify_plan_artifact(latest_plan_text, latest_plan.get("payload_json"))
        if latest_plan_quality != "valid_plan":
            raise invalid_argument(
                "Workflow latest plan is not a valid proposed_plan artifact; review or adopt a valid candidate plan first.",
                workflow_id=workflow_id,
                plan_turn_id=plan_turn_id,
                planQuality=latest_plan_quality,
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
                "output_schema": args.get("output_schema"),
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

    def _workflow_status_payload(
        self,
        workflow: dict[str, Any],
        *,
        last_messages: int,
        message_max_chars: int,
        include_events: bool,
    ) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        workflow_kind = _optional_string(workflow.get("workflow_kind")) or "plan_then_execute"
        if workflow_kind == "code_review":
            return self._review_workflow_status_payload(
                workflow,
                last_messages=last_messages,
                message_max_chars=message_max_chars,
                include_events=include_events,
            )
        workflow = self._sync_workflow_state(workflow, last_messages=last_messages, message_max_chars=message_max_chars)
        metadata = _workflow_metadata(workflow)
        thread_id = _optional_string(workflow.get("thread_id"))
        thread_refresh = self._refresh_workflow_thread_tracking(workflow, thread_id=thread_id)
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
        plan_rows = self.storage.get_tracked_turn_plans(plan_turn_id) if plan_turn_id else []
        if plan_turn_id and not plan_rows and str((plan_turn or {}).get("status") or "") == "completed":
            fallback_plan = self._fallback_plan_text_for_completed_turn(
                turn_id=plan_turn_id,
                thread_id=thread_id,
                turn_status=plan_turn,
                max_chars=message_max_chars,
            )
            if fallback_plan:
                now = _now_iso()
                self.storage.upsert_tracked_plan_item(
                    {
                        "item_id": f"{plan_turn_id}:assistant-final-plan",
                        "turn_id": plan_turn_id,
                        "thread_id": thread_id,
                        "status": "completed",
                        "text": fallback_plan,
                        "created_at": (plan_turn or {}).get("completedAt") or now,
                        "updated_at": now,
                        "completed_at": (plan_turn or {}).get("completedAt") or now,
                        "sequence": 0,
                        "payload_json": json.dumps({"source": "assistant_final_message_fallback"}, ensure_ascii=False),
                    }
                )
                plan_rows = self.storage.get_tracked_turn_plans(plan_turn_id)
        plans = [_plan_row_to_tool(row, message_max_chars) for row in plan_rows] if plan_turn_id else []
        latest_plan = _latest_plan(plans)
        workflow_observation = self._workflow_observation(
            workflow,
            thread_id=thread_id,
            official_plan_turn_id=plan_turn_id,
            latest_plan=latest_plan,
            thread_refresh=thread_refresh,
            message_max_chars=message_max_chars,
        )
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
        if (
            workflow_observation.get("recoverableCandidateFound")
            and not execution_turn_id
            and not execution_operation_id
            and phase in {"failed", "plan_ready", "plan_needs_review", "orphaned_after_app_server_exit"}
        ):
            phase = "plan_candidate_found"
            status = "plan_candidate_found"
            last_error = None
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
        runtime_policy = metadata.get("runtimePolicy")
        if not isinstance(runtime_policy, dict) and isinstance(plan_operation, dict):
            runtime_policy = plan_operation.get("runtimePolicy")
        runtime_policy_fields = _runtime_policy_public_fields(runtime_policy)
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
            "workflowRetryState": _workflow_retry_state(workflow, metadata),
            "planOperation": plan_operation,
            "executionOperation": execution_operation,
            "planTurn": plan_turn,
            "executionTurn": execution_turn,
            "plans": plans,
            "latestPlan": latest_plan,
            "workflowObservation": workflow_observation,
            "finalReport": final_report,
            "threadGoal": self._workflow_goal_status(workflow),
            "pendingInteractions": pending_interactions,
            "nextRecommendedAction": _next_workflow_action(phase),
            "recommendedPollAfterSeconds": _workflow_poll_seconds(phase),
            "pollRecommended": phase not in {"plan_ready", "plan_needs_review", "plan_candidate_found", "completed", "failed", "orphaned_after_app_server_exit"},
            "appServerGeneration": self._app_server.process_generation
            if self._app_server is not None
            else workflow.get("app_server_generation"),
            "source": source,
            "stalenessSeconds": staleness,
        }
        result.update(runtime_policy_fields)
        if include_events:
            result["events"] = [_workflow_event_to_tool(row) for row in self.storage.list_workflow_events(workflow_id, limit=20)]
        return result

    def _refresh_workflow_thread_tracking(self, workflow: dict[str, Any], *, thread_id: str | None) -> dict[str, Any]:
        if not thread_id:
            return {"imported": False, "reason": "missing_thread_id"}
        status = str(workflow.get("status") or "")
        phase = str(workflow.get("phase") or "")
        should_refresh = status in {
            "failed",
            "plan_ready",
            "plan_needs_review",
            "completed",
            "orphaned_after_app_server_exit",
            "unknown_after_app_server_exit",
        } or phase in {
            "failed",
            "plan_ready",
            "plan_needs_review",
            "completed",
            "orphaned_after_app_server_exit",
            "unknown_after_app_server_exit",
        }
        if not should_refresh:
            return {"imported": False, "reason": "not_needed"}
        refresh = getattr(self, "_refresh_thread_tracking_from_transcript", None)
        if not callable(refresh):
            return {"imported": False, "reason": "importer_unavailable"}
        return refresh(thread_id)

    def _workflow_observation(
        self,
        workflow: dict[str, Any],
        *,
        thread_id: str | None,
        official_plan_turn_id: str | None,
        latest_plan: dict[str, Any] | None,
        thread_refresh: dict[str, Any],
        message_max_chars: int,
    ) -> dict[str, Any]:
        if not thread_id:
            return {
                "available": False,
                "reason": "thread_not_known_yet",
                "candidatePlans": [],
                "candidateReports": [],
                "warnings": [],
            }

        latest_turn = self.storage.get_latest_tracked_turn_for_thread(thread_id)
        official_quality = (
            str(latest_plan.get("planQuality") or latest_plan.get("quality") or classify_plan_text(latest_plan.get("markdown") or latest_plan.get("text")))
            if latest_plan
            else None
        )
        plan_rows = self.storage.get_thread_plans(thread_id, limit=100)
        candidate_plans: list[dict[str, Any]] = []
        for row in plan_rows:
            turn_id = str(row.get("turn_id") or "")
            text = str(row.get("text") or "")
            candidate_quality = classify_plan_artifact(text, row.get("payload_json"))
            candidate = plan_candidate_payload(
                turn_id=turn_id,
                thread_id=str(row.get("thread_id") or thread_id),
                item_id=str(row.get("item_id") or ""),
                text=text,
                source=_plan_source(row),
                created_at=_optional_string(row.get("created_at")),
                updated_at=_optional_string(row.get("updated_at")),
                completed_at=_optional_string(row.get("completed_at")),
                max_chars=message_max_chars,
            )
            candidate["quality"] = candidate_quality
            candidate["planQuality"] = candidate_quality
            candidate["valid"] = candidate_quality == "valid_plan"
            candidate["requiresReview"] = candidate_quality in {"needs_review", "partial", "unknown"}
            candidate["isBlocker"] = candidate_quality in {"blocker", "refusal"}
            if turn_id == official_plan_turn_id:
                continue
            if candidate_quality == "valid_plan":
                candidate_plans.append(candidate)

        latest_turn_id = _optional_string((latest_turn or {}).get("turn_id"))
        latest_turn_updated = _optional_string((latest_turn or {}).get("updated_at")) or _optional_string((latest_turn or {}).get("completed_at"))
        official_turn = self.storage.get_tracked_turn(official_plan_turn_id) if official_plan_turn_id else None
        official_updated = _optional_string((official_turn or {}).get("updated_at")) or _optional_string((official_turn or {}).get("completed_at"))
        thread_advanced = bool(
            latest_turn_id
            and official_plan_turn_id
            and latest_turn_id != official_plan_turn_id
            and (not official_updated or not latest_turn_updated or latest_turn_updated >= official_updated)
        )
        warnings: list[str] = []
        if official_quality in {"blocker", "refusal"}:
            warnings.append("Official workflow plan turn completed with a blocker/refusal.")
        if thread_advanced:
            warnings.append("Thread has newer turn activity after the official workflow plan turn.")
        if candidate_plans:
            warnings.append("A newer valid plan candidate is available in the same thread.")

        source_parts = []
        if latest_turn and latest_turn.get("source"):
            source_parts.append(str(latest_turn.get("source")))
        if thread_refresh.get("source"):
            source_parts.append(str(thread_refresh.get("source")))
        source = "+".join(sorted(set(source_parts))) or "storage"
        return {
            "available": True,
            "officialPlanTurnId": official_plan_turn_id,
            "officialPlanQuality": official_quality,
            "latestThreadTurnId": latest_turn_id,
            "latestThreadStatus": (latest_turn or {}).get("status"),
            "latestThreadUpdatedAt": latest_turn_updated,
            "threadAdvancedAfterOfficialTurn": thread_advanced,
            "recoverableCandidateFound": bool(candidate_plans),
            "candidatePlans": candidate_plans,
            "candidateReports": [],
            "importStatus": thread_refresh,
            "source": source,
            "confidence": "high" if candidate_plans else ("medium" if latest_turn else "low"),
            "warnings": warnings,
        }

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

    def _fallback_plan_text_for_completed_turn(
        self,
        *,
        turn_id: str,
        thread_id: str | None,
        turn_status: dict[str, Any] | None,
        max_chars: int,
    ) -> str | None:
        stored_turn = self.storage.get_tracked_turn(turn_id)
        candidates: list[Any] = [
            (stored_turn or {}).get("final_message"),
            (turn_status or {}).get("finalMessage"),
            (turn_status or {}).get("final_message"),
        ]
        for message in (turn_status or {}).get("latestMessages") or (turn_status or {}).get("last_messages") or []:
            if isinstance(message, dict) and message.get("role") == "assistant":
                candidates.append(message.get("text"))
        if thread_id:
            try:
                chat = self.codex_get_chat(
                    {
                        "chat_id": thread_id,
                        "message_limit": 20,
                        "message_max_chars": max(4000, min(max_chars, 50_000)),
                    }
                )
            except Exception:
                chat = {}
            for message in chat.get("messages") or []:
                if isinstance(message, dict) and message.get("role") == "assistant":
                    candidates.append(message.get("text"))
        for candidate in reversed(candidates):
            text = _optional_string(candidate)
            if text:
                return _truncate_text(text, max_chars)[0]
        return None

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

        execution_operation_id = _optional_string(workflow.get("execution_operation_id"))
        execution_operation = self.storage.get_operation(execution_operation_id) if execution_operation_id else None
        request_payload = _operation_request_from_row(execution_operation) if execution_operation is not None else {}
        output_schema_state = request_payload.get("_output_schema_state") if isinstance(request_payload.get("_output_schema_state"), dict) else None
        schema_hash = _optional_string((output_schema_state or {}).get("schemaHash")) or _optional_string(request_payload.get("output_schema_hash"))
        final_text = _optional_string((execution_turn or {}).get("finalMessage")) or _optional_string((execution_turn or {}).get("final_message"))
        if execution_turn is not None and str(execution_turn.get("status") or "") == "completed" and final_text:
            report_hash, report_json = _stored_final_report_json(
                final_text=final_text,
                thread_id=execution_turn.get("threadId") or workflow.get("thread_id"),
                turn_id=execution_turn.get("turnId") or workflow.get("execution_turn_id"),
                source=execution_turn.get("source") or "storage",
                schema_hash=schema_hash,
            )
            if report_hash != workflow.get("latest_report_hash") or stored is None:
                self.storage.update_workflow(
                    workflow_id,
                    latest_report_hash=report_hash,
                    final_report_json=report_json,
                    updated_at=_now_iso(),
                )
            try:
                stored = json.loads(report_json)
            except json.JSONDecodeError:
                stored = None
            if stored is not None:
                return _report_for_status(stored, message_max_chars=message_max_chars)

        if stored is None:
            return None
        return _report_for_status(stored, message_max_chars=message_max_chars)

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


def _plan_source(row: dict[str, Any]) -> str:
    raw = row.get("payload_json")
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("source"):
                return str(payload.get("source"))
        except json.JSONDecodeError:
            return "storage"
    return "storage"

