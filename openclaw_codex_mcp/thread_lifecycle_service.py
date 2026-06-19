from __future__ import annotations

from . import tools as _tools

globals().update(_tools.__dict__)


class ThreadLifecycleServiceMixin:
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
                turn_id = (
                    turn_id
                    or _optional_string(workflow.get("review_turn_id"))
                    or _optional_string(workflow.get("execution_turn_id"))
                    or _optional_string(workflow.get("plan_turn_id"))
                )
        raw_status = args.get("status", "pending")
        status = str(raw_status).strip() if raw_status not in (None, "") else None
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
        from_repair = bool(args.get("_from_repair", False))
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
                    last_error="Interrupted by MCP client request",
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
                    last_error="Interrupted by MCP client request",
                    updated_at=now,
                    completed_at=now,
                )
        response = {
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
        if not from_repair:
            loop_guard = self._record_guidance_attempt(
                action="interrupt_turn",
                args={**args, "thread_id": thread_id, "turn_id": turn_id},
                result=response,
                status="succeeded",
                count_attempt=True,
                force=True,
            )
            response["loopGuard"] = loop_guard
            post_guidance = build_post_repair_guidance(
                {"changed": True, **response},
                action="interrupt_turn",
                scope_type=loop_guard["scopeType"],
                scope_id=loop_guard["scopeId"],
                loop_guard=loop_guard,
            )
            response["postRepairGuidance"] = post_guidance
            response["postRepairGuidanceText"] = guidance_text(post_guidance)
        return response

    def _resolve_interrupt_target(self, args: dict[str, Any]) -> dict[str, Any]:
        thread_id = _optional_string(args.get("thread_id"))
        turn_id = _optional_string(args.get("turn_id"))
        explicit_turn_id = turn_id
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
                or _optional_string(workflow.get("review_turn_id"))
                or _optional_string(workflow.get("execution_turn_id"))
                or _optional_string(workflow.get("plan_turn_id"))
            )
            operation_id = (
                operation_id
                or _optional_string(workflow.get("current_operation_id"))
                or _optional_string(workflow.get("review_operation_id"))
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

        if thread_id and explicit_turn_id is None:
            active_turn = self._active_turn_for_thread(thread_id)
            active_turn_id = _optional_string((active_turn or {}).get("turn_id"))
            if active_turn_id and active_turn_id != turn_id:
                turn_id = active_turn_id
                source = f"{source}+active_turn"

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

    def _resolve_lifecycle_thread_context(self, *, thread_id: str, project_id: str | None = None) -> dict[str, Any]:
        resolution = self.thread_resolver.resolve(thread_id, project_id, refresh_catalog=True)
        known_thread = self._resolve_known_thread_context(thread_id, project_id) if resolution is None else None
        if resolution is None and known_thread is None:
            raise thread_not_found(thread_id)
        chat = resolution.chat if resolution is not None else None
        resolved_project_id = (
            _optional_string(chat.project_id) if chat is not None else _optional_string((known_thread or {}).get("projectId"))
        ) or project_id
        if project_id and resolved_project_id and project_id != resolved_project_id:
            raise thread_not_found(thread_id)
        return {
            "threadId": thread_id,
            "projectId": resolved_project_id,
            "projectPath": _optional_string(chat.project_path) if chat is not None else _optional_string((known_thread or {}).get("projectPath")),
            "archived": bool(chat.archived) if chat is not None and chat.archived is not None else None,
            "source": resolution.source if resolution is not None else (_optional_string((known_thread or {}).get("source")) or "operation"),
            "chat": chat,
            "threadResolution": resolution.to_tool() if resolution is not None else None,
        }

    def _assert_thread_lifecycle_safe(self, thread_id: str) -> None:
        active_turn = self._active_turn_for_thread(thread_id)
        if active_turn is not None:
            raise busy(thread_id, str(active_turn.get("status") or "running"))
        pending = self._pending_interactions_for_context(thread_id=thread_id, turn_id=None, status="pending", limit=1)
        if pending:
            raise busy(thread_id, "pending_interaction")

    def _thread_lifecycle_state(
        self,
        *,
        thread_id: str,
        project_id: str | None = None,
        expected_archived: bool | None = None,
    ) -> dict[str, Any]:
        resolution = self.thread_resolver.resolve(thread_id, project_id, refresh_catalog=False)
        chat = resolution.chat if resolution is not None else None
        tracked_turn = self.storage.get_latest_tracked_turn_for_thread(thread_id)
        known_thread = self._resolve_known_thread_context(thread_id, project_id)
        pending = self._pending_interactions_for_context(thread_id=thread_id, turn_id=None, status="pending", limit=20)
        active_turn = self._active_turn_for_thread(thread_id)
        archived = expected_archived
        if archived is None and chat is not None:
            archived = bool(chat.archived)
        known = bool(chat is not None or tracked_turn is not None or self.storage.get_hook_thread(thread_id) is not None or known_thread is not None)
        known_source = (
            resolution.source if resolution is not None else (
                "tracked_turn" if tracked_turn is not None else (
                    "hook_history" if self.storage.get_hook_thread(thread_id) is not None else _optional_string((known_thread or {}).get("source"))
                )
            )
        )
        return {
            "known": known,
            "threadId": thread_id,
            "projectId": (chat.project_id if chat is not None else None)
            or _optional_string((tracked_turn or {}).get("project_id"))
            or _optional_string((known_thread or {}).get("projectId"))
            or project_id,
            "title": chat.title if chat is not None else None,
            "archived": archived,
            "source": known_source,
            "latestTurnId": _optional_string((tracked_turn or {}).get("turn_id")),
            "latestTurnStatus": _optional_string((tracked_turn or {}).get("status")),
            "activeTurnId": _optional_string((active_turn or {}).get("turn_id")),
            "pendingInteractionCount": len(pending),
        }

    def _create_lifecycle_action(
        self,
        *,
        action_type: str,
        thread_id: str,
        project_id: str | None,
        request: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        now = _now_iso()
        action_id = "tla_" + uuid.uuid4().hex
        row = {
            "action_id": action_id,
            "action_type": action_type,
            "thread_id": thread_id,
            "project_id": project_id,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "request_json": json.dumps(request, ensure_ascii=False, sort_keys=True),
        }
        self.storage.create_thread_lifecycle_action(row)
        return self.storage.get_thread_lifecycle_action(action_id) or row

    def _thread_lifecycle_action_to_tool(
        self,
        action: dict[str, Any],
        *,
        include_events: bool = False,
        expected_archived: bool | None = None,
    ) -> dict[str, Any]:
        result_payload: dict[str, Any] = {}
        try:
            loaded = json.loads(str(action.get("result_json") or "{}"))
            if isinstance(loaded, dict):
                result_payload = loaded
        except json.JSONDecodeError:
            result_payload = {}
        action_type = str(action.get("action_type") or "")
        status = str(action.get("status") or "unknown")
        thread_id = str(action.get("thread_id") or "")
        terminal = status in {"completed", "failed", "unknown_after_app_server_exit"}
        if status == "running":
            next_action = "poll_thread_compaction"
            poll_after = 5
        elif status == "completed" and action_type == "compact":
            next_action = "read_thread_status"
            poll_after = 0
        elif status == "completed":
            next_action = "read_thread_status"
            poll_after = 0
        elif status == "unknown_after_app_server_exit":
            next_action = "inspect_diagnostics"
            poll_after = 0
        else:
            next_action = "inspect_diagnostics"
            poll_after = 0
        response = {
            "ok": True,
            "actionId": action.get("action_id"),
            "actionType": action_type,
            "threadId": thread_id,
            "projectId": action.get("project_id"),
            "status": status,
            "createdAt": action.get("created_at"),
            "updatedAt": action.get("updated_at"),
            "completedAt": action.get("completed_at"),
            "lastError": action.get("last_error"),
            "appServerGeneration": action.get("app_server_generation"),
            "observedEventId": action.get("observed_event_id"),
            "targetTurnId": action.get("target_turn_id"),
            "threadState": result_payload.get("threadState")
            or self._thread_lifecycle_state(thread_id=thread_id, project_id=_optional_string(action.get("project_id")), expected_archived=expected_archived),
            "nextRecommendedAction": next_action,
            "recommendedPollAfterSeconds": poll_after,
            "pollRecommended": not terminal,
        }
        if result_payload.get("appServerResult") is not None:
            response["appServerResult"] = _compact_lifecycle_app_server_result(result_payload.get("appServerResult"))
        if include_events:
            response["events"] = [
                event_to_tool(row, include_payload=False)
                for row in self.storage.list_app_server_events(thread_id=thread_id, since=str(action.get("created_at") or ""), limit=50)
            ]
        return response

    async def _run_thread_lifecycle_action(
        self,
        *,
        action_type: str,
        args: dict[str, Any],
        expected_archived: bool,
    ) -> dict[str, Any]:
        thread_id = _required_string(args, "thread_id")
        project_id = _optional_string(args.get("project_id"))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", 30), 1, 120)
        refresh_catalog = bool(args.get("refresh_catalog", True))
        context = self._resolve_lifecycle_thread_context(thread_id=thread_id, project_id=project_id)
        self._assert_thread_lifecycle_safe(thread_id)
        action = self._create_lifecycle_action(
            action_type=action_type,
            thread_id=thread_id,
            project_id=_optional_string(context.get("projectId")),
            request={
                "thread_id": thread_id,
                "project_id": project_id,
                "timeout_seconds": timeout_seconds,
                "refresh_catalog": refresh_catalog,
            },
            status="starting",
        )
        client = await self._app()
        try:
            if action_type == "archive":
                app_result = await client.thread_archive(thread_id, timeout_seconds=timeout_seconds)
            elif action_type == "unarchive":
                app_result = await client.thread_unarchive(thread_id, timeout_seconds=timeout_seconds)
            else:
                raise invalid_argument("Unsupported lifecycle action.", action_type=action_type)
        except Exception as exc:
            now = _now_iso()
            self.storage.update_thread_lifecycle_action(
                str(action["action_id"]),
                status="failed",
                updated_at=now,
                completed_at=now,
                last_error=redact_text(str(exc)),
                app_server_generation=client.process_generation,
            )
            raise
        if refresh_catalog:
            with suppress(Exception):
                self.catalog.refresh()
        now = _now_iso()
        thread_state = self._thread_lifecycle_state(
            thread_id=thread_id,
            project_id=_optional_string(context.get("projectId")),
            expected_archived=expected_archived,
        )
        self.storage.update_thread_lifecycle_action(
            str(action["action_id"]),
            status="completed",
            updated_at=now,
            completed_at=now,
            result_json=json.dumps(
                {
                    "appServerResult": app_result,
                    "threadState": thread_state,
                },
                ensure_ascii=False,
            ),
            last_error=None,
            app_server_generation=app_result.get("_processGeneration") or client.process_generation,
        )
        updated = self.storage.get_thread_lifecycle_action(str(action["action_id"])) or action
        return self._thread_lifecycle_action_to_tool(updated, expected_archived=expected_archived)

    async def codex_archive_thread(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._run_thread_lifecycle_action(action_type="archive", args=args, expected_archived=True)

    async def codex_unarchive_thread(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._run_thread_lifecycle_action(action_type="unarchive", args=args, expected_archived=False)

    async def codex_start_thread_compaction(self, args: dict[str, Any]) -> dict[str, Any]:
        thread_id = _required_string(args, "thread_id")
        project_id = _optional_string(args.get("project_id"))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", 30), 1, 120)
        context = self._resolve_lifecycle_thread_context(thread_id=thread_id, project_id=project_id)
        self._assert_thread_lifecycle_safe(thread_id)
        action = self._create_lifecycle_action(
            action_type="compact",
            thread_id=thread_id,
            project_id=_optional_string(context.get("projectId")),
            request={"thread_id": thread_id, "project_id": project_id, "timeout_seconds": timeout_seconds},
            status="starting",
        )
        client = await self._app()
        try:
            app_result = await client.thread_compact_start(thread_id, timeout_seconds=timeout_seconds)
        except Exception as exc:
            now = _now_iso()
            self.storage.update_thread_lifecycle_action(
                str(action["action_id"]),
                status="failed",
                updated_at=now,
                completed_at=now,
                last_error=redact_text(str(exc)),
                app_server_generation=client.process_generation,
            )
            raise
        now = _now_iso()
        thread_state = self._thread_lifecycle_state(thread_id=thread_id, project_id=_optional_string(context.get("projectId")))
        self.storage.update_thread_lifecycle_action(
            str(action["action_id"]),
            status="running",
            updated_at=now,
            result_json=json.dumps({"appServerResult": app_result, "threadState": thread_state}, ensure_ascii=False),
            last_error=None,
            app_server_generation=app_result.get("_processGeneration") or client.process_generation,
        )
        updated = self.storage.get_thread_lifecycle_action(str(action["action_id"])) or action
        return self._thread_lifecycle_action_to_tool(updated)

    def _reconcile_thread_compaction_action(self, action: dict[str, Any]) -> dict[str, Any]:
        if str(action.get("action_type") or "") != "compact":
            return action
        status = str(action.get("status") or "")
        if status != "running":
            return action
        thread_id = str(action.get("thread_id") or "")
        created_at = str(action.get("created_at") or "")
        for event in self.storage.list_app_server_events(thread_id=thread_id, since=created_at, limit=500):
            if event.get("method") != "thread/compacted":
                continue
            now = _now_iso()
            target_turn_id = _optional_string(event.get("turn_id"))
            self.storage.update_thread_lifecycle_action(
                str(action["action_id"]),
                status="completed",
                updated_at=now,
                completed_at=now,
                observed_event_id=event.get("id"),
                target_turn_id=target_turn_id,
                last_error=None,
            )
            return self.storage.get_thread_lifecycle_action(str(action["action_id"])) or action
        generation = int(action.get("app_server_generation") or 0)
        process = getattr(self._app_server, "process", None) if self._app_server is not None else None
        app_running = self._app_server is not None and process is not None and getattr(process, "returncode", None) is None
        same_generation = self._app_server is not None and self._app_server.process_generation == generation
        if generation and (not app_running or not same_generation):
            now = _now_iso()
            self.storage.update_thread_lifecycle_action(
                str(action["action_id"]),
                status="unknown_after_app_server_exit",
                updated_at=now,
                completed_at=now,
                last_error="Codex app-server exited before thread/compacted was observed.",
            )
            return self.storage.get_thread_lifecycle_action(str(action["action_id"])) or action
        return action

    def codex_get_thread_compaction_status(self, args: dict[str, Any]) -> dict[str, Any]:
        action_id = _required_string(args, "action_id")
        include_events = bool(args.get("include_events", False))
        action = self.storage.get_thread_lifecycle_action(action_id)
        if action is None:
            raise invalid_argument("Thread lifecycle action was not found.", action_id=action_id)
        action = self._reconcile_thread_compaction_action(action)
        return self._thread_lifecycle_action_to_tool(action, include_events=include_events)


def _compact_lifecycle_app_server_result(value: Any) -> Any:
    redacted = redact_payload(value)
    return _strip_lifecycle_raw_paths(redacted)


def _strip_lifecycle_raw_paths(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower()
            if lower in {"path", "transcriptpath", "sessionpath"} and isinstance(item, str):
                cleaned[str(key)] = "[redacted-path]"
                continue
            cleaned[str(key)] = _strip_lifecycle_raw_paths(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_lifecycle_raw_paths(item) for item in value]
    return value

