from __future__ import annotations

from . import tools as _tools

globals().update(_tools.__dict__)


class ReviewServiceMixin:
    def _assert_review_source_ready(self, thread_id: str) -> None:
        active_turn = self._active_turn_for_thread(thread_id)
        if active_turn is not None:
            raise busy(thread_id, str(active_turn.get("status") or "running"))
        pending = self._pending_interactions_for_context(thread_id=thread_id, turn_id=None, status="pending", limit=1)
        if pending:
            raise busy(thread_id, "pending_interaction")

    def _update_operation_request_fields(self, operation_id: str, fields: dict[str, Any]) -> None:
        operation = self.storage.get_operation(operation_id)
        if operation is None:
            return
        payload = _operation_request_from_row(operation)
        payload.update(fields)
        self.storage.update_operation(
            operation_id,
            request_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            updated_at=_now_iso(),
        )

    def _mark_review_start_unknown_after_attempt(self, operation: dict[str, Any], *, reason: str) -> None:
        operation_id = str(operation.get("operation_id") or "")
        if not operation_id:
            return
        now = _now_iso()
        self.storage.update_operation(
            operation_id,
            status="unknown_after_app_server_exit",
            phase="unknown_after_app_server_exit",
            last_error=reason,
            completed_at=now,
            updated_at=now,
            next_attempt_at=None,
        )
        workflow_id = _optional_string(operation.get("workflow_id"))
        if workflow_id:
            self.storage.update_workflow(
                workflow_id,
                phase="orphaned",
                status="unknown_after_app_server_exit",
                last_error=reason,
                completed_at=now,
                updated_at=now,
            )
            self.storage.record_workflow_event(
                workflow_id,
                event_type="review_start_unknown",
                message="Code review start could not be safely retried.",
                details={"operationId": operation_id, "reason": reason},
                created_at=now,
            )

    async def _review_start_resolved(self, *, args: dict[str, Any]) -> dict[str, Any]:
        operation_id = _optional_string(args.get("_operation_id"))
        workflow_id = _optional_string(args.get("workflow_id"))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS), 1, 7200)
        project_id = _optional_string(args.get("project_id"))
        cwd = canonical_existing_path(args.get("cwd") or args.get("_resolved_project_path"))
        if not cwd or not is_allowed_path(cwd, self.config.allowed_roots):
            raise invalid_argument("Requested cwd is outside the allowlist.", cwd=cwd or args.get("cwd"))
        if not project_id:
            project_id = project_id_for_path(cwd)
        target = args.get("_review_target") if isinstance(args.get("_review_target"), dict) else _review_target_from_args(args)
        delivery = _optional_string(args.get("_review_delivery")) or _optional_string(args.get("delivery")) or "inline"
        if delivery not in {"inline", "detached"}:
            raise invalid_argument("Unsupported review delivery.", delivery=delivery)
        source_thread_id = _optional_string(args.get("_review_source_thread_id")) or _optional_string(args.get("thread_id"))
        model = _optional_string(args.get("model"))
        approval_policy = _approval_policy_for_start(args.get("approval_policy"), self.config.default_approval_policy)
        sandbox_policy = _sandbox_policy(args.get("sandbox")) or self.config.default_sandbox_policy
        client = await self._app()
        generation: Any = client.process_generation

        if not source_thread_id:
            if operation_id:
                self.storage.update_operation(
                    operation_id,
                    status="starting_thread",
                    phase="starting_thread",
                    project_id=project_id,
                    cwd=cwd,
                    updated_at=_now_iso(),
                    app_server_generation=client.process_generation,
                )
            if workflow_id:
                self.storage.update_workflow(
                    workflow_id,
                    phase="starting_thread",
                    status="starting_thread",
                    updated_at=_now_iso(),
                    app_server_generation=client.process_generation,
                )
            thread = await client.thread_start(
                cwd=cwd,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                model=model,
                effort=self.config.default_effort,
                summary=self.config.default_summary,
                timeout_seconds=timeout_seconds,
            )
            source_thread_id = _extract_thread_id(thread)
            if not source_thread_id:
                raise send_failed("thread/start did not return thread id for review workflow.")
            generation = thread.get("_processGeneration") or client.process_generation
            if operation_id:
                self._update_operation_request_fields(
                    operation_id,
                    {
                        "_review_source_thread_id": source_thread_id,
                        "thread_id": source_thread_id,
                        "_review_source_known": True,
                    },
                )
                self.storage.update_operation(
                    operation_id,
                    chat_id=source_thread_id,
                    updated_at=_now_iso(),
                    app_server_generation=generation,
                )
            if workflow_id:
                self.storage.update_workflow(
                    workflow_id,
                    review_source_thread_id=source_thread_id,
                    updated_at=_now_iso(),
                    app_server_generation=generation,
                )
                self.storage.record_workflow_event(
                    workflow_id,
                    event_type="review_source_thread_started",
                    message="Source thread created for code review workflow.",
                    details={"sourceThreadId": source_thread_id},
                    created_at=_now_iso(),
                )

        self._assert_review_source_ready(source_thread_id)
        if operation_id:
            self.storage.update_operation(
                operation_id,
                status="starting_review",
                phase="starting_review",
                chat_id=source_thread_id,
                project_id=project_id,
                cwd=cwd,
                updated_at=_now_iso(),
                app_server_generation=generation,
            )
            self._update_operation_request_fields(
                operation_id,
                {
                    "_review_start_attempted": True,
                    "_review_start_attempted_at": _now_iso(),
                    "_review_source_thread_id": source_thread_id,
                    "_review_target": target,
                    "_review_delivery": delivery,
                },
            )
        if workflow_id:
            self.storage.update_workflow(
                workflow_id,
                phase="starting_review",
                status="starting_review",
                review_source_thread_id=source_thread_id,
                review_target_json=json.dumps(target, ensure_ascii=False, sort_keys=True),
                review_delivery=delivery,
                updated_at=_now_iso(),
                app_server_generation=generation,
            )

        review_result = await client.review_start(
            thread_id=source_thread_id,
            target=target,
            delivery=delivery,
            timeout_seconds=timeout_seconds,
        )
        review_thread_id = _extract_review_thread_id(review_result) or source_thread_id
        turn = review_result.get("turn") if isinstance(review_result.get("turn"), dict) else {}
        review_turn_id = _extract_review_turn_id(review_result)
        if not review_turn_id:
            raise send_failed("review/start did not return review turn id.")
        generation = review_result.get("_processGeneration") or client.process_generation
        client.tracker.register_turn(
            turn_id=review_turn_id,
            thread_id=review_thread_id,
            chat_id=review_thread_id,
            project_id=project_id,
            project_path=cwd,
            status=_review_turn_initial_status(turn),
            started_at=_now_iso(),
            user_message=_review_target_label(target),
            model=model,
            permission_mode=approval_policy,
            request_id=str(review_result.get("_requestId")) if review_result.get("_requestId") is not None else None,
            process_generation=int(generation) if isinstance(generation, int) else client.process_generation,
        )
        review_state = {
            "accepted": True,
            "sourceThreadId": source_thread_id,
            "reviewThreadId": review_thread_id,
            "reviewTurnId": review_turn_id,
            "target": target,
            "delivery": delivery,
        }
        result_payload = {
            "ok": True,
            "accepted": True,
            "operationType": "review_start",
            "workflowId": workflow_id,
            "chat_id": review_thread_id,
            "chatId": review_thread_id,
            "thread_id": review_thread_id,
            "threadId": review_thread_id,
            "turn_id": review_turn_id,
            "turnId": review_turn_id,
            "project_id": project_id,
            "projectId": project_id,
            "sourceThreadId": source_thread_id,
            "reviewThreadId": review_thread_id,
            "reviewTurnId": review_turn_id,
            "reviewTarget": target,
            "reviewDelivery": delivery,
            "reviewState": review_state,
            "status": "running",
            "phase": "reviewing",
            "appServerGeneration": generation,
            "pollRecommended": True,
            "nextRecommendedAction": "wait_review",
            "recommendedPollAfterSeconds": 15,
        }
        if operation_id:
            self.storage.update_operation(
                operation_id,
                status="running",
                phase="running",
                chat_id=review_thread_id,
                thread_id=review_thread_id,
                turn_id=review_turn_id,
                project_id=project_id,
                cwd=cwd,
                workflow_id=workflow_id,
                result_json=json.dumps({**result_payload, "appServerResult": review_result}, ensure_ascii=False),
                last_error=None,
                updated_at=_now_iso(),
                next_attempt_at=None,
                app_server_generation=generation,
            )
        if workflow_id:
            self.storage.update_workflow(
                workflow_id,
                current_operation_id=operation_id,
                review_operation_id=operation_id,
                review_source_thread_id=source_thread_id,
                review_thread_id=review_thread_id,
                review_turn_id=review_turn_id,
                thread_id=review_thread_id,
                phase="reviewing",
                status="reviewing",
                last_error=None,
                updated_at=_now_iso(),
                app_server_generation=generation,
            )
            self.storage.record_workflow_event(
                workflow_id,
                event_type="review_started",
                message="Code review turn started.",
                details={"sourceThreadId": source_thread_id, "reviewThreadId": review_thread_id, "reviewTurnId": review_turn_id},
                created_at=_now_iso(),
            )
        return result_payload

    def codex_start_review_workflow(self, args: dict[str, Any]) -> dict[str, Any]:
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

        target = _review_target_from_args(args)
        requested_thread_id = _optional_string(args.get("thread_id"))
        requested_project_id = _optional_string(args.get("project_id"))
        cwd: str | None = None
        source_thread_id: str | None = None
        project_id: str | None = requested_project_id
        source_known = False

        if requested_thread_id:
            context = self._resolve_lifecycle_thread_context(thread_id=requested_thread_id, project_id=project_id)
            self._assert_thread_lifecycle_safe(requested_thread_id)
            source_thread_id = requested_thread_id
            project_id = _optional_string(context.get("projectId")) or project_id
            cwd = canonical_existing_path(args.get("cwd") or context.get("projectPath"))
            source_known = True
        else:
            if project_id:
                project = self.catalog.get_project(project_id)
                if project is None:
                    raise project_not_found(project_id)
                cwd = canonical_existing_path(args.get("cwd") or project.path)
            else:
                cwd_arg = _required_string(args, "cwd")
                cwd = canonical_existing_path(cwd_arg)
                project_id = project_id_for_path(cwd)
            if not cwd:
                raise invalid_argument("Review workflow requires thread_id or resolvable project_id/cwd.")

        if not cwd or not is_allowed_path(cwd, self.config.allowed_roots):
            raise invalid_argument("Requested cwd is outside the allowlist.", cwd=cwd or args.get("cwd"))
        if not project_id:
            project_id = project_id_for_path(cwd)

        delivery = _optional_string(args.get("delivery"))
        if not delivery:
            delivery = "detached" if source_thread_id else "inline"
        if delivery not in {"inline", "detached"}:
            raise invalid_argument("Unsupported review delivery.", delivery=delivery)

        workflow_id = "wf_" + uuid.uuid4().hex
        operation_id = str(uuid.uuid4())
        now = _now_iso()
        approval_policy = args.get("approval_policy") or self.config.default_approval_policy
        sandbox = args.get("sandbox") or _sandbox_value_from_policy(self.config.default_sandbox_policy)
        request_payload = {
            "operation_type": "review_start",
            "workflow_id": workflow_id,
            "project_id": project_id,
            "thread_id": source_thread_id,
            "cwd": cwd,
            "target_type": args.get("target_type"),
            "base_branch": _optional_string(args.get("base_branch")),
            "commit_sha": _optional_string(args.get("commit_sha")),
            "commit_title": _optional_string(args.get("commit_title")),
            "instructions": _optional_string(args.get("instructions")),
            "delivery": delivery,
            "model": _optional_string(args.get("model")),
            "sandbox": sandbox,
            "approval_policy": approval_policy,
            "timeout_seconds": args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS),
            "_operation_id": operation_id,
            "_review_target": target,
            "_review_delivery": delivery,
            "_review_source_thread_id": source_thread_id,
            "_review_source_known": source_known,
            "_resolved_project_path": cwd,
        }
        self.storage.create_operation(
            {
                "operation_id": operation_id,
                "client_request_id": f"workflow:{workflow_id}:review",
                "operation_type": "review_start",
                "status": "queued",
                "phase": "queued",
                "project_id": project_id,
                "chat_id": source_thread_id,
                "thread_id": None,
                "turn_id": None,
                "workflow_id": workflow_id,
                "cwd": cwd,
                "title": None,
                "request_json": json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
                "result_json": None,
                "last_error": None,
                "attempt_count": 0,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "app_server_generation": None,
                "submitter_config_fingerprint": self._config_fingerprint,
                "worker_config_summary_json": self._config_summary_json,
            }
        )
        self.storage.create_workflow(
            {
                "workflow_id": workflow_id,
                "workflow_kind": "code_review",
                "client_request_id": client_request_id,
                "execution_client_request_id": None,
                "current_operation_id": operation_id,
                "plan_operation_id": None,
                "execution_operation_id": None,
                "review_operation_id": operation_id,
                "review_source_thread_id": source_thread_id,
                "review_thread_id": None,
                "review_turn_id": None,
                "review_target_json": json.dumps(target, ensure_ascii=False, sort_keys=True),
                "review_delivery": delivery,
                "project_id": project_id,
                "thread_id": "",
                "plan_turn_id": "",
                "execution_turn_id": None,
                "latest_plan_item_id": None,
                "latest_plan_hash": None,
                "latest_report_hash": None,
                "final_report_json": None,
                "phase": "queued",
                "status": "queued",
                "last_error": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "app_server_generation": None,
                "metadata_json": json.dumps(
                    {
                        "startClientRequestId": client_request_id,
                        "sourceThreadKnown": source_known,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        self.storage.record_workflow_event(
            workflow_id,
            event_type="review_workflow_started",
            message="Code review workflow queued.",
            details={"reviewOperationId": operation_id, "delivery": delivery, "target": target},
            created_at=now,
        )
        operation = self.storage.get_operation(operation_id)
        if operation is not None:
            self._schedule_operation_if_needed(operation)
        workflow = self.storage.get_workflow(workflow_id)
        assert workflow is not None
        status = self._workflow_status_payload(
            workflow,
            last_messages=10,
            message_max_chars=_bounded_int(args.get("first_message_max_chars", 8000), 500, 200000),
            include_events=True,
        )
        status["reviewStartOperation"] = self._operation_status_payload(operation, last_messages=10, message_max_chars=8000) if operation else None
        status["idempotent"] = False
        return status

    def _sync_review_workflow_state(self, workflow: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(workflow["workflow_id"])
        updates: dict[str, Any] = {}
        review_operation_id = _optional_string(workflow.get("review_operation_id")) or _optional_string(workflow.get("current_operation_id"))
        review_operation = self.storage.get_operation(review_operation_id) if review_operation_id else None
        if review_operation is not None:
            self._schedule_operation_if_needed(review_operation)
            request_payload = _operation_request_from_row(review_operation)
            source_thread_id = _optional_string(request_payload.get("_review_source_thread_id")) or _optional_string(request_payload.get("thread_id"))
            review_thread_id = _optional_string(review_operation.get("thread_id"))
            review_turn_id = _optional_string(review_operation.get("turn_id"))
            if review_operation_id and review_operation_id != _optional_string(workflow.get("review_operation_id")):
                updates["review_operation_id"] = review_operation_id
            if review_operation_id and review_operation_id != _optional_string(workflow.get("current_operation_id")):
                updates["current_operation_id"] = review_operation_id
            if source_thread_id and source_thread_id != _optional_string(workflow.get("review_source_thread_id")):
                updates["review_source_thread_id"] = source_thread_id
            if review_thread_id and review_thread_id != _optional_string(workflow.get("review_thread_id")):
                updates["review_thread_id"] = review_thread_id
                updates["thread_id"] = review_thread_id
            if review_turn_id and review_turn_id != _optional_string(workflow.get("review_turn_id")):
                updates["review_turn_id"] = review_turn_id
            target = request_payload.get("_review_target") if isinstance(request_payload.get("_review_target"), dict) else None
            if target and not _optional_string(workflow.get("review_target_json")):
                updates["review_target_json"] = json.dumps(target, ensure_ascii=False, sort_keys=True)
            delivery = _optional_string(request_payload.get("_review_delivery")) or _optional_string(request_payload.get("delivery"))
            if delivery and delivery != _optional_string(workflow.get("review_delivery")):
                updates["review_delivery"] = delivery
        if updates:
            updates["updated_at"] = _now_iso()
            self.storage.update_workflow(workflow_id, **updates)
            workflow = self.storage.get_workflow(workflow_id) or workflow
        return workflow

    def _review_workflow_status_payload(
        self,
        workflow: dict[str, Any],
        *,
        last_messages: int,
        message_max_chars: int,
        include_events: bool,
    ) -> dict[str, Any]:
        workflow = self._sync_review_workflow_state(workflow)
        workflow_id = str(workflow["workflow_id"])
        review_operation_id = _optional_string(workflow.get("review_operation_id")) or _optional_string(workflow.get("current_operation_id"))
        review_source_thread_id = _optional_string(workflow.get("review_source_thread_id"))
        review_thread_id = _optional_string(workflow.get("review_thread_id")) or _optional_string(workflow.get("thread_id"))
        review_turn_id = _optional_string(workflow.get("review_turn_id"))
        review_operation_row = self.storage.get_operation(review_operation_id) if review_operation_id else None
        review_operation = (
            self._operation_status_payload(review_operation_row, last_messages=last_messages, message_max_chars=message_max_chars)
            if review_operation_row is not None
            else None
        )
        workflow = self._sync_review_workflow_state(self.storage.get_workflow(workflow_id) or workflow)
        review_source_thread_id = _optional_string(workflow.get("review_source_thread_id")) or review_source_thread_id
        review_thread_id = _optional_string(workflow.get("review_thread_id")) or _optional_string(workflow.get("thread_id")) or review_thread_id
        review_turn_id = _optional_string(workflow.get("review_turn_id")) or _optional_string((review_operation or {}).get("turnId")) or review_turn_id
        review_turn = (
            self._turn_status_or_none(review_turn_id, review_thread_id, last_messages=last_messages, message_max_chars=message_max_chars)
            if review_turn_id
            else None
        )
        pending_thread_id = review_thread_id or review_source_thread_id
        pending_interactions = (
            self._pending_interactions_for_context(thread_id=pending_thread_id, turn_id=review_turn_id, status="pending", limit=20)
            if pending_thread_id
            else []
        )
        final_report = self._review_workflow_final_report(
            workflow,
            review_turn=review_turn,
            message_max_chars=message_max_chars,
        )
        workflow = self.storage.get_workflow(workflow_id) or workflow
        refreshed_review_turn_id = _optional_string(workflow.get("review_turn_id"))
        if refreshed_review_turn_id and refreshed_review_turn_id != review_turn_id:
            review_turn_id = refreshed_review_turn_id
            review_thread_id = _optional_string(workflow.get("review_thread_id")) or _optional_string(workflow.get("thread_id")) or review_thread_id
            review_turn = self._turn_status_or_none(
                review_turn_id,
                review_thread_id,
                last_messages=last_messages,
                message_max_chars=message_max_chars,
            )
        review_operation_row = self.storage.get_operation(review_operation_id) if review_operation_id else None
        review_operation = (
            self._operation_status_payload(review_operation_row, last_messages=last_messages, message_max_chars=message_max_chars)
            if review_operation_row is not None
            else review_operation
        )
        phase, status, last_error = _derive_review_workflow_phase(
            workflow,
            review_turn=review_turn,
            review_operation=review_operation,
            pending_interactions=pending_interactions,
        )
        now = _now_iso()
        completed_at = workflow.get("completed_at")
        if status in {"completed", "failed", "unknown_after_app_server_exit", "interrupted", "cancelled", "canceled"} and not completed_at:
            completed_at = now
        if phase != workflow.get("phase") or status != workflow.get("status") or last_error != workflow.get("last_error"):
            self.storage.update_workflow(
                workflow_id,
                phase=phase,
                status=status,
                last_error=last_error,
                updated_at=now,
                completed_at=completed_at,
                current_operation_id=review_operation_id,
                app_server_generation=self._app_server.process_generation if self._app_server is not None else workflow.get("app_server_generation"),
            )
            self.storage.record_workflow_event(
                workflow_id,
                event_type="review_workflow_status_changed",
                message=f"Review workflow moved to {phase}.",
                details={"phase": phase, "status": status, "lastError": last_error},
                created_at=now,
            )
            workflow = self.storage.get_workflow(workflow_id) or workflow
        review_target = _workflow_review_target(workflow)
        sources = [str(item.get("source") or "") for item in (review_turn,) if isinstance(item, dict)]
        source = "live" if "live" in sources else ("hook_history" if "hook_history" in sources else "storage")
        staleness = _min_staleness(
            [
                workflow.get("updated_at"),
                (review_turn or {}).get("updatedAt") or (review_turn or {}).get("updated_at") if review_turn else None,
                (review_operation or {}).get("updatedAt") if review_operation else None,
            ]
        )
        result = {
            "ok": True,
            "workflow_id": workflow_id,
            "workflowId": workflow_id,
            "workflow_kind": "code_review",
            "workflowKind": "code_review",
            "project_id": workflow.get("project_id"),
            "projectId": workflow.get("project_id"),
            "thread_id": review_thread_id,
            "threadId": review_thread_id,
            "review_source_thread_id": review_source_thread_id,
            "reviewSourceThreadId": review_source_thread_id,
            "review_thread_id": review_thread_id,
            "reviewThreadId": review_thread_id,
            "review_turn_id": review_turn_id,
            "reviewTurnId": review_turn_id,
            "current_operation_id": review_operation_id,
            "currentOperationId": review_operation_id,
            "review_operation_id": review_operation_id,
            "reviewOperationId": review_operation_id,
            "plan_operation_id": None,
            "planOperationId": None,
            "execution_operation_id": None,
            "executionOperationId": None,
            "phase": phase,
            "status": status,
            "lastError": last_error,
            "createdAt": workflow.get("created_at"),
            "updatedAt": workflow.get("updated_at"),
            "completedAt": completed_at,
            "clientRequestId": workflow.get("client_request_id"),
            "latestReportHash": workflow.get("latest_report_hash"),
            "reviewTarget": review_target,
            "reviewDelivery": workflow.get("review_delivery"),
            "reviewOperation": review_operation,
            "reviewTurn": review_turn,
            "planOperation": None,
            "executionOperation": None,
            "planTurn": None,
            "executionTurn": None,
            "plans": [],
            "latestPlan": None,
            "finalReport": final_report,
            "threadGoal": self._workflow_goal_status(workflow),
            "pendingInteractions": pending_interactions,
            "nextRecommendedAction": _next_review_workflow_action(phase, status),
            "recommendedPollAfterSeconds": _review_workflow_poll_seconds(phase, status),
            "pollRecommended": status not in {"completed", "failed", "unknown_after_app_server_exit", "interrupted", "cancelled", "canceled"},
            "appServerGeneration": self._app_server.process_generation if self._app_server is not None else workflow.get("app_server_generation"),
            "source": source,
            "stalenessSeconds": staleness,
        }
        if include_events:
            result["events"] = [_workflow_event_to_tool(row) for row in self.storage.list_workflow_events(workflow_id, limit=20)]
        return self._attach_agent_guidance(result, surface="workflow_status")

    def _review_workflow_final_report(
        self,
        workflow: dict[str, Any],
        *,
        review_turn: dict[str, Any] | None,
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
        final_text = _optional_string((review_turn or {}).get("finalMessage")) or _optional_string((review_turn or {}).get("final_message"))
        if review_turn is not None and str(review_turn.get("status") or "") == "completed" and final_text:
            report_hash, report_json = _stored_final_report_json(
                final_text=final_text,
                thread_id=review_turn.get("threadId") or workflow.get("review_thread_id") or workflow.get("thread_id"),
                turn_id=review_turn.get("turnId") or workflow.get("review_turn_id"),
                source=review_turn.get("source") or "storage",
                schema_hash=None,
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
        if stored is not None:
            return _report_for_status(stored, message_max_chars=message_max_chars)
        history_report = self._review_history_final_report(workflow, message_max_chars=message_max_chars)
        if history_report is not None:
            return history_report
        return None

    def _review_history_final_report(self, workflow: dict[str, Any], *, message_max_chars: int) -> dict[str, Any] | None:
        review_thread_id = _optional_string(workflow.get("review_thread_id")) or _optional_string(workflow.get("thread_id"))
        if not review_thread_id:
            return None
        try:
            self.catalog.refresh()
            chat = self.catalog.get_chat(review_thread_id)
        except Exception as exc:  # pragma: no cover - defensive fallback path
            LOG.debug("review history fallback catalog refresh failed workflow_id=%s error=%s", workflow.get("workflow_id"), exc)
            return None
        if chat is None:
            return None
        try:
            summary, source_info = self._load_chat_summary(
                chat,
                archived=chat.archived,
                include_tool_calls=False,
                include_tool_outputs=False,
                include_command_outputs=False,
                include_reasoning=False,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback path
            LOG.debug("review history fallback parse failed workflow_id=%s error=%s", workflow.get("workflow_id"), exc)
            return None
        message = _last_message_by_role(summary.messages, "assistant")
        final_text = _optional_string(message.text if message else None)
        if not message or not final_text:
            return None
        workflow_id = str(workflow["workflow_id"])
        actual_turn_id = _optional_string(message.turn_id) or _optional_string(workflow.get("review_turn_id"))
        report_hash, report_json = _stored_final_report_json(
            final_text=final_text,
            thread_id=review_thread_id,
            turn_id=actual_turn_id,
            source=source_info.get("source") or chat.source or "transcript",
            schema_hash=None,
        )
        now = _now_iso()
        workflow_updates: dict[str, Any] = {
            "latest_report_hash": report_hash,
            "final_report_json": report_json,
            "updated_at": now,
        }
        if actual_turn_id:
            workflow_updates["review_turn_id"] = actual_turn_id
        self.storage.update_workflow(workflow_id, **workflow_updates)
        if report_hash != workflow.get("latest_report_hash"):
            self.storage.record_workflow_event(
                workflow_id,
                event_type="review_report_loaded_from_history",
                message="Review final report loaded from thread history.",
                details={"threadId": review_thread_id, "turnId": actual_turn_id, "source": source_info.get("source")},
                created_at=now,
            )
        if actual_turn_id:
            self.storage.upsert_tracked_turn(
                {
                    "turn_id": actual_turn_id,
                    "thread_id": review_thread_id,
                    "chat_id": review_thread_id,
                    "project_id": workflow.get("project_id") or chat.project_id,
                    "project_path": chat.project_path,
                    "status": "completed",
                    "started_at": message.created_at,
                    "updated_at": message.created_at or now,
                    "completed_at": message.created_at or now,
                    "first_message_at": message.created_at,
                    "final_message": final_text,
                    "last_error": None,
                    "clear_last_error": True,
                    "source": "transcript",
                }
            )
            self.storage.record_tracked_turn_message(
                {
                    "event_hash": hashlib.sha256(
                        f"review-history:{review_thread_id}:{actual_turn_id}:{message.message_id or final_text}".encode("utf-8")
                    ).hexdigest(),
                    "turn_id": actual_turn_id,
                    "thread_id": review_thread_id,
                    "role": "assistant",
                    "text": final_text,
                    "created_at": message.created_at or now,
                    "sequence": 0,
                    "event_type": "review_history_final_report",
                    "payload_json": "{}",
                }
            )
        operation_id = _optional_string(workflow.get("review_operation_id")) or _optional_string(workflow.get("current_operation_id"))
        operation = self.storage.get_operation(operation_id) if operation_id else None
        if operation is not None and str(operation.get("status") or "") not in {"failed", "unknown_after_app_server_exit", "interrupted", "cancelled", "canceled"}:
            self.storage.update_operation(
                str(operation["operation_id"]),
                status="completed",
                phase="completed",
                thread_id=review_thread_id,
                turn_id=actual_turn_id or operation.get("turn_id"),
                latest_report_hash=report_hash,
                final_report_json=report_json,
                last_error=None,
                updated_at=now,
                completed_at=message.created_at or now,
                next_attempt_at=None,
            )
        try:
            stored = json.loads(report_json)
        except json.JSONDecodeError:
            return None
        return _report_for_status(stored, message_max_chars=message_max_chars)

