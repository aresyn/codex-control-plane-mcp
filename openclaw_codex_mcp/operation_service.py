from __future__ import annotations

from . import tools as _tools

globals().update(_tools.__dict__)


class OperationServiceMixin:
    def _prompt_dedup_basis(self, operation_type: str, message: str, *, workflow: dict[str, Any] | None = None) -> str:
        if operation_type != "execute_plan" or workflow is None:
            return message
        latest_plan = self.storage.get_latest_plan_for_turn(str(workflow.get("plan_turn_id") or ""))
        plan_text = str((latest_plan or {}).get("text") or "").strip()
        if not plan_text:
            return message
        return f"{message}\n\n[latest completed plan]\n{plan_text}"

    def _normalize_turn_input_items(
        self,
        *,
        message: str,
        raw_items: Any,
        cwd: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if raw_items in (None, ""):
            return [{"type": "text", "text": message}], None
        if not isinstance(raw_items, list):
            raise invalid_argument("input_items must be an array of image input objects.")
        if len(raw_items) > self.config.max_image_input_items:
            raise invalid_argument(
                "Too many image input items.",
                maxItems=self.config.max_image_input_items,
                actualItems=len(raw_items),
            )

        normalized: list[dict[str, Any]] = [{"type": "text", "text": message}]
        safe_items: list[dict[str, Any]] = []
        dedup_items: list[dict[str, Any]] = []
        image_url_count = 0
        local_image_count = 0

        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise invalid_argument("input_items entries must be objects.", index=index)
            item_type = str(raw_item.get("type") or "")
            detail = _image_input_detail(raw_item.get("detail"), index=index)
            if item_type == "image":
                normalized_item, safe_item, dedup_item = _normalize_remote_image_input(raw_item, detail=detail, index=index)
                image_url_count += 1
            elif item_type == "localImage":
                normalized_item, safe_item, dedup_item = _normalize_local_image_input(
                    raw_item,
                    detail=detail,
                    index=index,
                    cwd=cwd,
                    allowed_roots=self.config.allowed_roots,
                    max_bytes=self.config.max_image_input_bytes,
                )
                local_image_count += 1
            else:
                raise invalid_argument("Unsupported input_items type.", index=index, type=item_type)
            normalized.append(normalized_item)
            safe_items.append(safe_item)
            dedup_items.append(dedup_item)

        state = {
            "provided": True,
            "count": len(safe_items),
            "imageUrlCount": image_url_count,
            "localImageCount": local_image_count,
            "types": sorted({str(item.get("type") or "") for item in safe_items}),
            "items": safe_items,
            "dedupHash": _safe_digest(dedup_items),
            "maxItems": self.config.max_image_input_items,
            "maxLocalImageBytes": self.config.max_image_input_bytes,
        }
        return normalized, state

    def _find_prompt_duplicate(
        self,
        *,
        project_path_key: str,
        normalized_prompt: str,
        normalized_hash: str,
        ignore_submission_id: str | None = None,
        ignore_operation_id: str | None = None,
        strict_hash_only: bool = False,
        resource_keys: list[str] | None = None,
        allow_historical_continuation: bool = False,
    ) -> dict[str, Any] | None:
        candidates_by_id: dict[str, tuple[dict[str, Any], float]] = {}
        for row in self.storage.find_prompt_submissions_by_hash(project_path_key, normalized_hash, limit=50):
            prompt_submission_id = str(row.get("prompt_submission_id") or "")
            if prompt_submission_id:
                candidates_by_id[prompt_submission_id] = (row, 1.0)
        if not strict_hash_only:
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
            existing_resource_keys = self._operation_resource_keys(_optional_string(row.get("operation_id")))
            resource_decision = _dedup_resource_key_decision(resource_keys or [], existing_resource_keys)
            enriched["dedupDecision"] = {
                "reason": resource_decision["reason"],
                "resourceKeysCompared": resource_decision["compared"],
                "resourceKeyOverlap": resource_decision["overlap"],
            }
            if enriched["active"] and resource_decision["reason"] == "disjoint_resource_keys":
                continue
            matches.append(enriched)

        if not matches:
            return None
        active = [row for row in matches if row.get("active")]
        if active:
            return sorted(active, key=lambda row: (float(row.get("similarity") or 0), str(row.get("updated_at") or "")), reverse=True)[0]
        if not allow_historical_continuation:
            return None
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
            dedupDecision=match.get("dedupDecision"),
            nextRecommendedAction="poll_existing_turn",
        )

    def _operation_resource_keys(self, operation_id: str | None) -> list[str]:
        if not operation_id:
            return []
        scheduling = self.storage.get_operation_scheduling(operation_id)
        if scheduling is None:
            return []
        return _safe_json_list(scheduling.get("resource_keys_json"))

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
        strict_hash_only: bool = False,
        resource_keys: list[str] | None = None,
        dedup_policy: str = "active_prompt_guard",
        allow_historical_continuation: bool = False,
    ) -> dict[str, Any]:
        normalized_prompt = normalize_prompt(dedup_basis)
        normalized_hash = prompt_hash(normalized_prompt)
        match = None
        if dedup_policy != "idempotency_only":
            match = self._find_prompt_duplicate(
                project_path_key=project_path_key,
                normalized_prompt=normalized_prompt,
                normalized_hash=normalized_hash,
                ignore_submission_id=ignore_submission_id,
                ignore_operation_id=ignore_operation_id,
                strict_hash_only=strict_hash_only,
                resource_keys=resource_keys,
                allow_historical_continuation=allow_historical_continuation,
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

    def _validate_steer_target(self, *, thread_id: str, expected_turn_id: str) -> dict[str, Any]:
        turn = self.storage.get_tracked_turn(expected_turn_id)
        if turn is None:
            raise turn_not_found(expected_turn_id)
        actual_thread_id = _optional_string(turn.get("thread_id"))
        if actual_thread_id != thread_id:
            raise invalid_argument(
                "Steer target turn does not belong to the requested thread.",
                thread_id=thread_id,
                expected_turn_id=expected_turn_id,
                actual_thread_id=actual_thread_id,
            )
        status = str(turn.get("status") or "")
        if status not in TURN_ACTIVE_STATUSES:
            raise invalid_argument(
                "Steer target turn is not active.",
                thread_id=thread_id,
                expected_turn_id=expected_turn_id,
                status=status,
            )
        return turn

    def _resolve_fork_source_context(
        self,
        *,
        source_thread_id: str,
        cwd: Any = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        explicit_cwd = _optional_string(cwd)
        resolved_project_id = project_id
        resolved_project_path: str | None = None
        resolved_chat_id: str | None = None
        source_known = False

        chat = self.catalog.get_chat(source_thread_id, resolved_project_id)
        if chat is not None:
            source_known = True
            resolved_chat_id = chat.chat_id
            resolved_project_id = chat.project_id or resolved_project_id
            resolved_project_path = _optional_string(chat.project_path)

        if not resolved_project_path:
            tracked_turn = self.storage.get_latest_tracked_turn_for_thread(source_thread_id)
            if tracked_turn is not None:
                source_known = True
                resolved_chat_id = _optional_string(tracked_turn.get("chat_id")) or source_thread_id
                resolved_project_id = _optional_string(tracked_turn.get("project_id")) or resolved_project_id
                resolved_project_path = _optional_string(tracked_turn.get("project_path"))

        if not resolved_project_path:
            operation = self.storage.get_latest_operation_for_thread(source_thread_id)
            if operation is not None:
                source_known = True
                resolved_chat_id = _optional_string(operation.get("chat_id")) or _optional_string(operation.get("thread_id")) or source_thread_id
                resolved_project_id = _optional_string(operation.get("project_id")) or resolved_project_id
                resolved_project_path = _optional_string(operation.get("cwd"))

        if not resolved_project_path:
            hook_thread = self.storage.get_hook_thread(source_thread_id)
            if hook_thread is not None:
                source_known = True
                resolved_chat_id = source_thread_id
                resolved_project_path = _optional_string(hook_thread.get("project_path"))
                if not resolved_project_id and resolved_project_path:
                    resolved_project_id = project_id_for_path(resolved_project_path)

        effective_path = canonical_existing_path(explicit_cwd or resolved_project_path)
        if not effective_path or not is_allowed_path(effective_path, self.config.allowed_roots):
            if not source_known and not explicit_cwd:
                raise thread_not_found(source_thread_id)
            raise invalid_argument("Requested cwd is outside the allowlist.", cwd=effective_path or explicit_cwd or resolved_project_path)
        if not source_known and not explicit_cwd:
            raise thread_not_found(source_thread_id)
        if not resolved_project_id:
            resolved_project_id = project_id_for_path(effective_path)
        return {
            "sourceKnown": source_known,
            "sourceThreadId": source_thread_id,
            "chatId": resolved_chat_id or source_thread_id,
            "projectId": resolved_project_id,
            "projectPath": effective_path,
        }

    def _resolve_known_thread_context(self, thread_id: str, project_id: str | None = None) -> dict[str, Any] | None:
        tracked_turn = self.storage.get_latest_tracked_turn_for_thread(thread_id)
        operation = self.storage.get_latest_operation_for_thread(thread_id)
        hook_thread = self.storage.get_hook_thread(thread_id)
        source = "unknown"
        resolved_project_id = project_id
        project_path: str | None = None
        chat_id = thread_id
        status: str | None = None

        if tracked_turn is not None:
            source = "tracked_turn"
            resolved_project_id = _optional_string(tracked_turn.get("project_id")) or resolved_project_id
            project_path = _optional_string(tracked_turn.get("project_path"))
            chat_id = _optional_string(tracked_turn.get("chat_id")) or thread_id
            status = _optional_string(tracked_turn.get("status"))
        if operation is not None and not project_path:
            source = "operation"
            resolved_project_id = _optional_string(operation.get("project_id")) or resolved_project_id
            project_path = _optional_string(operation.get("cwd"))
            chat_id = _optional_string(operation.get("chat_id")) or _optional_string(operation.get("thread_id")) or thread_id
            status = _optional_string(operation.get("status"))
        if hook_thread is not None and not project_path:
            source = "hook_history"
            project_path = _optional_string(hook_thread.get("project_path"))
            if project_path and not resolved_project_id:
                resolved_project_id = project_id_for_path(project_path)
            status = "completed"

        if project_id and resolved_project_id and project_id != resolved_project_id:
            return None
        effective_path = canonical_existing_path(project_path)
        if not effective_path or not is_allowed_path(effective_path, self.config.allowed_roots):
            return None
        if not resolved_project_id:
            resolved_project_id = project_id_for_path(effective_path)
        return {
            "threadId": thread_id,
            "chatId": chat_id,
            "projectId": resolved_project_id,
            "projectPath": effective_path,
            "status": status,
            "source": source,
            "trackedTurn": tracked_turn,
            "operation": operation,
            "hookThread": hook_thread,
        }

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

    async def _steer_turn_resolved(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        message: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = _optional_string(args.get("_operation_id"))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS), 1, 7200)
        client_user_message_id = _optional_string(args.get("_client_user_message_id")) or (
            f"mcp-steer:{operation_id}" if operation_id else None
        )
        self._validate_steer_target(thread_id=thread_id, expected_turn_id=expected_turn_id)
        client = await self._app()
        if operation_id:
            self.storage.update_operation(
                operation_id,
                status="starting_turn",
                phase="starting_turn",
                chat_id=thread_id,
                thread_id=thread_id,
                turn_id=expected_turn_id,
                updated_at=_now_iso(),
                app_server_generation=client.process_generation,
            )
        result = await client.turn_steer(
            thread_id=thread_id,
            expected_turn_id=expected_turn_id,
            input_items=[{"type": "text", "text": message}],
            client_user_message_id=client_user_message_id,
            timeout_seconds=timeout_seconds,
        )
        result_turn_id = _extract_turn_id(result) or expected_turn_id
        steer_state = {
            "accepted": True,
            "targetThreadId": thread_id,
            "targetTurnId": result_turn_id,
            "clientUserMessageId": client_user_message_id,
        }
        if operation_id:
            self.storage.update_operation(
                operation_id,
                status="running",
                phase="running",
                chat_id=thread_id,
                thread_id=thread_id,
                turn_id=result_turn_id,
                result_json=json.dumps({"turnId": result_turn_id, "steerState": steer_state, "appServerResult": result}, ensure_ascii=False),
                last_error=None,
                updated_at=_now_iso(),
                next_attempt_at=None,
                app_server_generation=result.get("_processGeneration") or client.process_generation,
            )
        return {
            "ok": True,
            "accepted": True,
            "status": "running",
            "phase": "running",
            "thread_id": thread_id,
            "threadId": thread_id,
            "turn_id": result_turn_id,
            "turnId": result_turn_id,
            "targetThreadId": thread_id,
            "targetTurnId": result_turn_id,
            "steerState": steer_state,
            "appServerGeneration": result.get("_processGeneration") or client.process_generation,
            "pollRecommended": True,
            "nextRecommendedAction": "poll_turn_status",
            "recommendedPollAfterSeconds": 15,
        }

    async def _fork_thread_resolved(
        self,
        *,
        source_thread_id: str,
        message: str | None,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = _optional_string(args.get("_operation_id"))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS), 1, 7200)
        project_id = _optional_string(args.get("project_id"))
        cwd = canonical_existing_path(args.get("cwd") or args.get("_resolved_project_path"))
        if not cwd or not is_allowed_path(cwd, self.config.allowed_roots):
            raise invalid_argument("Requested cwd is outside the allowlist.", cwd=cwd or args.get("cwd"))
        model = _optional_string(args.get("model"))
        approval_policy = _approval_policy_for_start(args.get("approval_policy"), self.config.default_approval_policy)
        sandbox_arg = args.get("sandbox")
        sandbox_mode = _sandbox_value_from_policy(
            self.config.default_sandbox_policy
            if sandbox_arg == "respect_existing"
            else (_sandbox_policy(sandbox_arg) or self.config.default_sandbox_policy)
        )
        fork_config = args.get("fork_config")
        if fork_config is not None and not isinstance(fork_config, dict):
            raise invalid_argument("fork_config must be an object when provided.")
        ephemeral = bool(args.get("ephemeral", False))
        forked_thread_id = _optional_string(args.get("_resolved_thread_id"))
        client = await self._app()
        fork_result: dict[str, Any] = {}
        generation: Any = client.process_generation
        attempted_at = _optional_string(args.get("_fork_start_attempted_at"))

        if forked_thread_id:
            fork_result = {"thread": {"id": forked_thread_id}, "cwd": cwd, "recovered": True}
        else:
            attempted_at = attempted_at or _now_iso()
            if operation_id:
                request_payload = dict(args)
                request_payload["_fork_start_attempted"] = True
                request_payload["_fork_start_attempted_at"] = attempted_at
                request_payload["_fork_source_thread_id"] = source_thread_id
                request_payload["_fork_runtime_policy"] = {
                    "approvalPolicy": approval_policy,
                    "sandbox": sandbox_mode,
                    "model": model,
                    "ephemeral": ephemeral,
                }
                self.storage.update_operation(
                    operation_id,
                    status="starting_thread",
                    phase="starting_thread",
                    request_json=json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
                    result_json=json.dumps(
                        {
                            "ok": True,
                            "operationType": "fork_thread",
                            "forkState": {
                                "accepted": False,
                                "sourceThreadId": source_thread_id,
                                "forkedThreadId": None,
                                "hasInitialMessage": bool(_optional_string(message)),
                                "cwd": cwd,
                                "model": model,
                                "ephemeral": ephemeral,
                                "turnId": None,
                                "startAttempted": True,
                                "startAttemptedAt": attempted_at,
                                "ambiguous": False,
                            },
                        },
                        ensure_ascii=False,
                    ),
                    updated_at=_now_iso(),
                    app_server_generation=client.process_generation,
                )
            try:
                fork_result = await client.thread_fork(
                    thread_id=source_thread_id,
                    cwd=cwd,
                    approval_policy=approval_policy,
                    sandbox=sandbox_mode,
                    model=model,
                    config=fork_config,
                    ephemeral=ephemeral,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                if operation_id:
                    return self._mark_fork_start_unknown_after_attempt(
                        operation_id=operation_id,
                        source_thread_id=source_thread_id,
                        cwd=cwd,
                        model=model,
                        ephemeral=ephemeral,
                        has_initial_message=bool(_optional_string(message)),
                        attempted_at=attempted_at,
                        reason=f"Worker lost certainty after thread/fork attempt: {redact_text(str(exc), max_chars=500)}",
                        generation=client.process_generation,
                    )
                raise
            forked_thread_id = _extract_thread_id(fork_result)
            if not forked_thread_id:
                raise send_failed("thread/fork did not return thread id")
            generation = fork_result.get("_processGeneration") or client.process_generation

        result_cwd = canonical_existing_path(fork_result.get("cwd") or cwd)
        if not result_cwd or not is_allowed_path(result_cwd, self.config.allowed_roots):
            raise invalid_argument("Forked thread cwd is outside the allowlist.", cwd=result_cwd or fork_result.get("cwd") or cwd)
        if not project_id:
            project_id = project_id_for_path(result_cwd)
        has_initial_message = bool(_optional_string(message))
        fork_state = {
            "accepted": True,
            "sourceThreadId": source_thread_id,
            "forkedThreadId": forked_thread_id,
            "hasInitialMessage": has_initial_message,
            "cwd": result_cwd,
            "model": model,
            "ephemeral": ephemeral,
            "turnId": None,
            "startAttempted": bool(attempted_at),
            "startAttemptedAt": attempted_at,
            "ambiguous": False,
        }
        base_result = {
            "ok": True,
            "accepted": True,
            "operationType": "fork_thread",
            "chat_id": forked_thread_id,
            "chatId": forked_thread_id,
            "thread_id": forked_thread_id,
            "threadId": forked_thread_id,
            "project_id": project_id,
            "projectId": project_id,
            "sourceThreadId": source_thread_id,
            "forkState": fork_state,
            "effectiveApprovalPolicy": approval_policy,
            "effectiveSandbox": sandbox_mode,
            "effectiveCwd": result_cwd,
            "effectiveModel": model,
            "appServerGeneration": generation,
            "forkResult": redact_payload(fork_result),
        }

        if operation_id:
            self.storage.update_operation(
                operation_id,
                status="starting_turn" if has_initial_message else "completed",
                phase="starting_turn" if has_initial_message else "completed",
                chat_id=forked_thread_id,
                thread_id=forked_thread_id,
                project_id=project_id,
                cwd=result_cwd,
                result_json=json.dumps(base_result, ensure_ascii=False),
                last_error=None,
                completed_at=None if has_initial_message else _now_iso(),
                updated_at=_now_iso(),
                next_attempt_at=None,
                app_server_generation=generation,
            )

        if not has_initial_message:
            return {
                **base_result,
                "status": "completed",
                "phase": "completed",
                "pollRecommended": False,
                "nextRecommendedAction": "read_forked_thread",
                "recommendedPollAfterSeconds": 0,
            }

        turn_args = dict(args)
        turn_args["chat_id"] = forked_thread_id
        turn_args["project_id"] = project_id
        turn_args["_resolved_thread_id"] = forked_thread_id
        turn_args["_resolved_project_path"] = result_cwd
        turn_args["_skip_prompt_dedup"] = True
        turn_args["_prompt_submission_id"] = None
        turn_result = await self._send_message_resolved(
            chat_id=forked_thread_id,
            thread_id=forked_thread_id,
            project_id=project_id,
            project_path=result_cwd,
            message=str(message or ""),
            args=turn_args,
            prompt_submission_id=None,
            dedup_metadata=None,
        )
        turn_id = _optional_string(turn_result.get("turnId")) or _optional_string(turn_result.get("turn_id"))
        fork_state["turnId"] = turn_id
        combined = {
            **turn_result,
            "operationType": "fork_thread",
            "sourceThreadId": source_thread_id,
            "forkState": fork_state,
            "forkResult": redact_payload(fork_result),
            "appServerGeneration": turn_result.get("appServerGeneration") or generation,
        }
        if operation_id:
            self.storage.update_operation(
                operation_id,
                result_json=json.dumps(combined, ensure_ascii=False),
                updated_at=_now_iso(),
                app_server_generation=combined.get("appServerGeneration") or generation,
            )
        return combined

    def _mark_fork_start_unknown_after_attempt(
        self,
        *,
        operation_id: str,
        source_thread_id: str | None,
        cwd: str | None,
        model: str | None,
        ephemeral: bool,
        has_initial_message: bool,
        attempted_at: str | None,
        reason: str,
        generation: Any = None,
    ) -> dict[str, Any]:
        now = _now_iso()
        fork_state = {
            "accepted": False,
            "sourceThreadId": source_thread_id,
            "forkedThreadId": None,
            "hasInitialMessage": has_initial_message,
            "cwd": cwd,
            "model": model,
            "ephemeral": ephemeral,
            "turnId": None,
            "startAttempted": True,
            "startAttemptedAt": attempted_at,
            "ambiguous": True,
        }
        result = {
            "ok": True,
            "accepted": False,
            "operationType": "fork_thread",
            "status": "unknown_after_app_server_exit",
            "phase": "unknown_after_app_server_exit",
            "sourceThreadId": source_thread_id,
            "forkState": fork_state,
            "appServerGeneration": generation,
            "lastError": reason,
            "pollRecommended": False,
            "nextRecommendedAction": "inspect_diagnostics",
            "recommendedPollAfterSeconds": 0,
        }
        self.storage.update_operation(
            operation_id,
            status="unknown_after_app_server_exit",
            phase="unknown_after_app_server_exit",
            result_json=json.dumps(result, ensure_ascii=False),
            last_error=reason,
            completed_at=now,
            updated_at=now,
            next_attempt_at=None,
            app_server_generation=generation,
        )
        return result

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
        _apply_plan_mode_runtime_policy(
            args,
            default_sandbox_policy=self.config.default_sandbox_policy,
            default_approval_policy=self.config.default_approval_policy,
        )
        approval_policy = _approval_policy_for_send(args.get("approval_policy"), thread_row, self.config.default_approval_policy)
        sandbox_policy = _sandbox_policy_for_send(args.get("sandbox"), thread_row, self.config.default_sandbox_policy)
        collaboration_mode = _collaboration_mode(args.get("collaboration_mode"), model=None, config=self.config)
        output_schema = args.get("_output_schema") if isinstance(args.get("_output_schema"), dict) else None
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
                input_items=_turn_start_input_items(message, args),
                cwd=project_path,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                model=None,
                effort=self.config.default_effort,
                summary=self.config.default_summary,
                collaboration_mode=collaboration_mode,
                output_schema=output_schema,
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
        input_item_state = args.get("_input_item_state")
        if isinstance(input_item_state, dict):
            response["inputItemState"] = dict(input_item_state)
        response.update(_runtime_policy_public_fields(args.get("_runtime_policy")))
        response.update(self._dedup_metadata_for_result(dedup_metadata))
        return response

    async def codex_send_message(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._should_delegate_compatibility_write("codex_send_message", args):
            return self._delegated_compatibility_write_payload("codex_send_message", args)
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
        if chat is not None:
            resolved_chat_id = chat.chat_id
            resolved_thread_id = chat.thread_id
            resolved_project_id = chat.project_id
            project_path = canonical_existing_path(chat.project_path)
            status, confidence, evidence = self.catalog.infer_chat_status(chat)
        else:
            context = self._resolve_known_thread_context(chat_id, str(project_id) if project_id else None)
            if context is None:
                raise thread_not_found(chat_id)
            resolved_chat_id = str(context["chatId"])
            resolved_thread_id = str(context["threadId"])
            resolved_project_id = _optional_string(context.get("projectId"))
            project_path = canonical_existing_path(context.get("projectPath"))
            status = _optional_string(context.get("status")) or "unknown"
            confidence = "storage"
            evidence = [{"source": context.get("source"), "status": status}]
        if not project_path or not is_allowed_path(project_path, self.config.allowed_roots):
            raise invalid_argument("Chat project path is outside the allowlist.", project_path=project_path)
        if status in {"running", "waiting_for_approval", "waiting_for_user", "waiting_for_user_input"} and confidence != "low":
            raise busy(resolved_thread_id, status)
        prompt_submission_id = _optional_string(args.get("_prompt_submission_id"))
        dedup_metadata = args.get("_dedup_metadata") if isinstance(args.get("_dedup_metadata"), dict) else None
        if not bool(args.get("_skip_prompt_dedup")) and not prompt_submission_id:
            dedup_basis = str(args.get("_prompt_dedup_basis") or self._prompt_dedup_basis(str(args.get("_prompt_dedup_operation_type") or "send_message"), message))
            dedup = self._prepare_prompt_submission(
                project_id=resolved_project_id,
                project_path_key=path_key(project_path),
                operation_type=str(args.get("_prompt_dedup_operation_type") or "send_message"),
                message=message,
                dedup_basis=dedup_basis,
                chat_id=resolved_chat_id,
                thread_id=resolved_thread_id,
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
                    project_id=resolved_project_id,
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
            resolved_chat_id,
            resolved_thread_id,
            status,
            confidence,
            _bounded_int(args.get("timeout_seconds", DEFAULT_TOOL_START_TIMEOUT_SECONDS), 1, 7200),
            args.get("approval_policy") or self.config.default_approval_policy,
            args.get("sandbox") or self.config.default_sandbox_policy,
            len(message),
            message_preview,
        )
        return await self._send_message_resolved(
            chat_id=resolved_chat_id,
            thread_id=resolved_thread_id,
            project_id=resolved_project_id,
            project_path=project_path,
            message=message,
            args=args,
            prompt_submission_id=prompt_submission_id,
            dedup_metadata=dedup_metadata,
        )

    async def codex_start_chat(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._should_delegate_compatibility_write("codex_start_chat", args):
            return self._delegated_compatibility_write_payload("codex_start_chat", args)
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
        _apply_plan_mode_runtime_policy(
            args,
            default_sandbox_policy=self.config.default_sandbox_policy,
            default_approval_policy=self.config.default_approval_policy,
        )
        sandbox_policy = _sandbox_policy(args.get("sandbox")) or self.config.default_sandbox_policy
        approval_policy = _approval_policy_for_start(args.get("approval_policy"), self.config.default_approval_policy)
        model = str(args.get("model")) if args.get("model") not in (None, "") else None
        collaboration_mode = _collaboration_mode(args.get("collaboration_mode"), model=model, config=self.config)
        output_schema = args.get("_output_schema") if isinstance(args.get("_output_schema"), dict) else None
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
                input_items=_turn_start_input_items(message, args),
                cwd=cwd,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                model=model,
                effort=self.config.default_effort,
                summary=self.config.default_summary,
                collaboration_mode=collaboration_mode,
                output_schema=output_schema,
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
        input_item_state = args.get("_input_item_state")
        if isinstance(input_item_state, dict):
            response["inputItemState"] = dict(input_item_state)
        response.update(_runtime_policy_public_fields(args.get("_runtime_policy")))
        response.update(self._dedup_metadata_for_result(dedup_metadata))
        return response

    def codex_get_turn_status(self, args: dict[str, Any]) -> dict[str, Any]:
        turn_id = _required_string(args, "turn_id")
        thread_id = args.get("thread_id")
        thread_id = str(thread_id).strip() if thread_id not in (None, "") else None
        last_messages = _bounded_int(args.get("last_messages", 10), 1, 50)
        message_max_chars = _bounded_int(args.get("message_max_chars", 8000), 500, 200000)
        progress_events = _bounded_int(args.get("progress_events", 10), 0, 100)
        progress_max_chars = _bounded_int(args.get("progress_max_chars", 2000), 200, 20000)
        live = self._tracked_turn_status(
            turn_id,
            last_messages=last_messages,
            message_max_chars=message_max_chars,
            progress_events=progress_events,
            progress_max_chars=progress_max_chars,
        )
        lookup_thread_id = thread_id or (str(live.get("thread_id")) if live and live.get("thread_id") else None)
        hook = self._hook_turn_status(turn_id, lookup_thread_id, last_messages=last_messages, message_max_chars=message_max_chars)
        kb = self._kb_turn_status(turn_id, lookup_thread_id, last_messages=last_messages, message_max_chars=message_max_chars)
        if live is None:
            if hook is not None:
                return self._attach_agent_guidance(
                    normalize_public_status_payload(
                        self._attach_progress_status(hook, turn_id, progress_events=progress_events, progress_max_chars=progress_max_chars),
                        surface="turn_status",
                    ),
                    surface="turn_status",
                )
            if kb is None:
                raise turn_not_found(turn_id)
            return self._attach_agent_guidance(
                normalize_public_status_payload(
                    self._attach_progress_status(kb, turn_id, progress_events=progress_events, progress_max_chars=progress_max_chars),
                    surface="turn_status",
                ),
                surface="turn_status",
            )
        live_status = str(live.get("status") or "unknown")
        live_unknown = live_status in {"unknown", "unknown_after_app_server_exit"}
        live_active = live_status in TURN_ACTIVE_STATUSES or live_status in {"starting", "ready"}
        if hook is not None:
            if _fallback_terminal_should_replace_live(live, hook):
                hook["source"] = "storage+hook_history"
                _mark_recovered_terminal_evidence(hook)
                self._persist_recovered_turn_terminal(turn_id, hook)
                return self._attach_agent_guidance(
                    normalize_public_status_payload(
                        self._attach_progress_status(hook, turn_id, progress_events=progress_events, progress_max_chars=progress_max_chars),
                        surface="turn_status",
                    ),
                    surface="turn_status",
                )
            if not live.get("last_messages") and hook.get("last_messages"):
                live = _merge_turn_messages(live, hook, source="app_server+hook_history")
        if kb is not None:
            if _fallback_terminal_should_replace_live(live, kb):
                kb["source"] = "storage+kb_history"
                _mark_recovered_terminal_evidence(kb)
                self._persist_recovered_turn_terminal(turn_id, kb)
                return self._attach_agent_guidance(
                    normalize_public_status_payload(
                        self._attach_progress_status(kb, turn_id, progress_events=progress_events, progress_max_chars=progress_max_chars),
                        surface="turn_status",
                    ),
                    surface="turn_status",
                )
            if not live.get("last_messages") and kb.get("last_messages"):
                live = _merge_turn_messages(live, kb, source="app_server+kb_history")
        if live_active:
            live["completion_observed"] = False
            live["completionObserved"] = False
        return self._attach_agent_guidance(normalize_public_status_payload(live, surface="turn_status"), surface="turn_status")

    async def codex_execute_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._should_delegate_compatibility_write("codex_execute_plan", args):
            return self._delegated_compatibility_write_payload("codex_execute_plan", args)
        workflow_id = _optional_string(args.get("workflow_id"))
        if workflow_id and not args.get("_operation_id") and not args.get("_skip_prompt_dedup") and not bool(args.get("force", False)):
            return self.codex_approve_plan(args)
        output_schema, output_schema_state = _validate_output_schema(args.get("output_schema"))
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
        if output_schema is not None and output_schema_state is not None:
            payload["_output_schema"] = output_schema
            payload["_output_schema_state"] = output_schema_state
            payload["output_schema_hash"] = output_schema_state["schemaHash"]
        payload.pop("output_schema", None)
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
        if operation_type not in {"start_chat", "send_message", "execute_plan", "steer_turn", "fork_thread"}:
            raise invalid_argument("Unsupported operation_type", operation_type=operation_type)
        message: str | None
        if operation_type == "fork_thread":
            message = _optional_string(args.get("message"))
        else:
            message = _required_string(args, "message")
        explicit_client_request_id = _optional_string(args.get("client_request_id"))
        agent_id = _optional_string(args.get("agent_id"))
        priority = _priority_value(args.get("priority"))
        estimated_cost_class = _estimated_cost_class_value(args.get("estimated_cost_class"))
        resource_keys = _resource_keys_value(args.get("resource_keys"))
        thread_mode = _thread_mode_value(args.get("thread_mode"), operation_type=operation_type)
        dedup_policy = _dedup_policy_value(args.get("dedup_policy"))
        allow_historical_continuation = bool(args.get("allow_historical_continuation", False))
        if operation_type == "start_chat" and not (thread_mode == "auto" and allow_historical_continuation):
            allow_historical_continuation = False
        if explicit_client_request_id:
            existing = self.storage.get_operation_by_client_request_id(explicit_client_request_id)
            if existing is not None:
                self._ensure_operation_scheduling(
                    existing,
                    agent_id=agent_id,
                    priority=priority,
                    estimated_cost_class=estimated_cost_class,
                    resource_keys=resource_keys,
                    queued_reason="idempotent_existing_operation",
                )
                if self._can_schedule_inline():
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
        if operation_type == "steer_turn" and not (_optional_string(args.get("thread_id")) and _optional_string(args.get("expected_turn_id"))):
            raise invalid_argument("codex_submit_task steer_turn requires thread_id and expected_turn_id")
        if operation_type == "fork_thread" and not _optional_string(args.get("source_thread_id")):
            raise invalid_argument("codex_submit_task fork_thread requires source_thread_id")
        if operation_type == "fork_thread" and args.get("fork_config") is not None and not isinstance(args.get("fork_config"), dict):
            raise invalid_argument("fork_config must be an object when provided.")
        if operation_type == "steer_turn" and args.get("output_schema") is not None:
            raise invalid_argument("codex_submit_task steer_turn does not support output_schema.")
        if operation_type == "fork_thread" and args.get("output_schema") is not None and not _optional_string(message):
            raise invalid_argument("codex_submit_task fork_thread output_schema requires an initial message.")
        input_items_raw = args.get("input_items")
        input_items_provided = input_items_raw not in (None, "")
        if operation_type == "steer_turn" and input_items_provided:
            raise invalid_argument("codex_submit_task steer_turn does not support input_items.")
        if operation_type == "fork_thread" and input_items_provided and not _optional_string(message):
            raise invalid_argument("codex_submit_task fork_thread input_items requires an initial message.")
        output_schema, output_schema_state = _validate_output_schema(args.get("output_schema"))

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
        _apply_plan_mode_runtime_policy(
            actual_args,
            default_sandbox_policy=self.config.default_sandbox_policy,
            default_approval_policy=self.config.default_approval_policy,
        )
        workflow: dict[str, Any] | None = None
        initial_chat_id: str | None = _optional_string(args.get("chat_id"))
        initial_thread_id: str | None = None
        project_id: str | None = _optional_string(args.get("project_id"))
        project_path: str | None = None
        input_item_state: dict[str, Any] | None = None

        if operation_type == "steer_turn":
            initial_thread_id = _required_string(args, "thread_id")
            expected_turn_id = _required_string(args, "expected_turn_id")
            target_turn = self._validate_steer_target(thread_id=initial_thread_id, expected_turn_id=expected_turn_id)
            initial_chat_id = _optional_string(target_turn.get("chat_id")) or initial_thread_id
            project_id = _optional_string(target_turn.get("project_id")) or project_id
            project_path = _optional_string(target_turn.get("project_path"))
            actual_args["chat_id"] = initial_chat_id
            actual_args["thread_id"] = initial_thread_id
            actual_args["expected_turn_id"] = expected_turn_id
            actual_args["_resolved_thread_id"] = initial_thread_id
            actual_args["_target_turn_id"] = expected_turn_id
            actual_args["_client_user_message_id"] = f"mcp-steer:{operation_id}"
        elif operation_type == "fork_thread":
            source_thread_id = _required_string(args, "source_thread_id")
            fork_source = self._resolve_fork_source_context(
                source_thread_id=source_thread_id,
                cwd=args.get("cwd"),
                project_id=project_id,
            )
            project_id = _optional_string(fork_source.get("projectId"))
            project_path = _optional_string(fork_source.get("projectPath"))
            initial_chat_id = None
            initial_thread_id = None
            actual_args["source_thread_id"] = source_thread_id
            actual_args["project_id"] = project_id
            actual_args["cwd"] = project_path
            actual_args["_source_chat_id"] = fork_source.get("chatId")
            actual_args["_source_known"] = fork_source.get("sourceKnown")
            actual_args["_resolved_project_path"] = project_path
        elif operation_type == "start_chat":
            project = self.catalog.get_project(str(project_id))
            if project is None:
                raise project_not_found(str(project_id))
            project_path = canonical_existing_path(args.get("cwd") or project.path)
            if not is_allowed_path(project_path, self.config.allowed_roots):
                raise invalid_argument("Requested cwd is outside the allowlist.", cwd=project_path)
        elif operation_type == "send_message":
            resolution = self.thread_resolver.resolve(str(initial_chat_id), project_id, refresh_catalog=True)
            if resolution is None:
                raise thread_not_found(str(initial_chat_id))
            chat = resolution.chat
            project_id = chat.project_id
            initial_chat_id = chat.chat_id
            initial_thread_id = chat.thread_id
            project_path = canonical_existing_path(chat.project_path)
            if not project_path and project_id:
                project = self.catalog.get_project(str(project_id))
                project_path = canonical_existing_path((project.path if project is not None else None))
            if not project_path or not is_allowed_path(project_path, self.config.allowed_roots):
                raise invalid_argument("Chat project path is outside the allowlist.", project_path=chat.project_path)
            actual_args["_thread_resolution"] = resolution.to_tool()
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

        if input_items_provided:
            normalized_input_items, input_item_state = self._normalize_turn_input_items(
                message=str(message or ""),
                raw_items=input_items_raw,
                cwd=str(project_path or ""),
            )
            actual_args["_input_items"] = normalized_input_items
            actual_args["_input_item_state"] = input_item_state
            actual_args.pop("input_items", None)

        dedup: dict[str, Any] = {"action": "not_applicable"}
        prompt_submission_id: str | None = None
        dedup_metadata: dict[str, Any] | None = None
        if operation_type not in {"steer_turn", "fork_thread"}:
            dedup_basis = str(args.get("_prompt_dedup_basis") or self._prompt_dedup_basis(operation_type, message, workflow=workflow))
            if output_schema_state is not None:
                dedup_basis = f"{dedup_basis}\noutput_schema:{output_schema_state['schemaHash']}"
            if input_item_state is not None:
                dedup_basis = f"{dedup_basis}\ninput_items:{input_item_state['dedupHash']}"
            try:
                dedup = self.storage._sqlite_retry(
                    lambda: self._prepare_prompt_submission(
                        project_id=project_id,
                        project_path_key=path_key(project_path),
                        operation_type=operation_type,
                        message=str(message or ""),
                        dedup_basis=dedup_basis,
                        operation_id=operation_id,
                        chat_id=initial_chat_id,
                        thread_id=initial_thread_id,
                        workflow_id=_optional_string(args.get("workflow_id")),
                        strict_hash_only=input_item_state is not None,
                        resource_keys=resource_keys,
                        dedup_policy=dedup_policy,
                        allow_historical_continuation=allow_historical_continuation,
                    ),
                    attempts=8,
                    base_delay_seconds=0.05,
                    max_delay_seconds=0.75,
                )
            except Exception as exc:
                if _is_sqlite_busy_exception(exc):
                    raise state_busy(
                        "MCP state DB is busy while recording prompt submission. Retry the same client_request_id.",
                        client_request_id=explicit_client_request_id,
                        operationType=operation_type,
                    ) from exc
                raise
            prompt_submission_id = _optional_string(dedup.get("promptSubmissionId"))
        if operation_type not in {"steer_turn", "fork_thread"} and dedup.get("action") == "continue_existing_chat":
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
        actual_args["_thread_mode"] = thread_mode
        actual_args["_dedup_policy"] = dedup_policy
        actual_args["_allow_historical_continuation"] = allow_historical_continuation
        if operation_type == "execute_plan":
            actual_args["_prompt_dedup_operation_type"] = "execute_plan"
            actual_args["_prompt_dedup_basis"] = dedup_basis
        if output_schema is not None and output_schema_state is not None:
            actual_args["_output_schema"] = output_schema
            actual_args["_output_schema_state"] = output_schema_state
            actual_args["output_schema_hash"] = output_schema_state["schemaHash"]
            actual_args.pop("output_schema", None)

        request_payload = _operation_request_payload(actual_args, operation_type=actual_operation_type, message=message)
        request_payload["_skip_prompt_dedup"] = True
        request_payload["_prompt_submission_id"] = prompt_submission_id
        if output_schema is not None and output_schema_state is not None:
            request_payload["_output_schema"] = output_schema
            request_payload["_output_schema_state"] = output_schema_state
            request_payload["output_schema_hash"] = output_schema_state["schemaHash"]
        if input_item_state is not None:
            request_payload["_input_items"] = actual_args.get("_input_items")
            request_payload["_input_item_state"] = input_item_state
        if actual_args.get("_resolved_thread_id"):
            request_payload["_resolved_thread_id"] = actual_args.get("_resolved_thread_id")
        if actual_args.get("_resolved_project_path"):
            request_payload["_resolved_project_path"] = actual_args.get("_resolved_project_path")
        if dedup_metadata is not None:
            request_payload["_dedup_metadata"] = dedup_metadata
            request_payload["original_operation_type"] = operation_type
        request_payload["thread_mode"] = thread_mode
        request_payload["dedup_policy"] = dedup_policy
        request_payload["allow_historical_continuation"] = allow_historical_continuation
        if operation_type == "execute_plan":
            request_payload["_prompt_dedup_operation_type"] = "execute_plan"
            request_payload["_prompt_dedup_basis"] = dedup_basis
        if isinstance(actual_args.get("_runtime_policy"), dict):
            request_payload["_runtime_policy"] = actual_args["_runtime_policy"]
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
            "turn_id": _optional_string(actual_args.get("expected_turn_id")) if operation_type == "steer_turn" else None,
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
            "submitter_config_fingerprint": self._config_fingerprint,
            "worker_config_summary_json": self._config_summary_json,
        }
        try:
            created = self.storage._sqlite_retry(
                lambda: self.storage.create_operation(row),
                attempts=8,
                base_delay_seconds=0.05,
                max_delay_seconds=0.75,
            )
            operation = self.storage.get_operation(operation_id) if created else self.storage.get_operation_by_client_request_id(client_request_id)
            if operation is None:
                if prompt_submission_id:
                    self.storage.update_prompt_submission(prompt_submission_id, status="failed", updated_at=_now_iso())
                raise send_failed("Failed to create Codex operation.")
            self.storage._sqlite_retry(
                lambda: self._ensure_operation_scheduling(
                    operation,
                    agent_id=agent_id,
                    priority=priority,
                    estimated_cost_class=estimated_cost_class,
                    resource_keys=resource_keys,
                    queued_reason="waiting_for_worker" if not self._can_schedule_inline() else None,
                ),
                attempts=8,
                base_delay_seconds=0.05,
                max_delay_seconds=0.75,
            )
        except Exception as exc:
            if _is_sqlite_busy_exception(exc):
                raise state_busy(
                    "MCP state DB is busy while creating the durable operation. Retry the same client_request_id.",
                    client_request_id=client_request_id,
                    operationId=operation_id,
                    operationType=operation_type,
                ) from exc
            raise
        if self._can_schedule_inline():
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
        if self._can_schedule_inline():
            self._schedule_recoverable_operations()
            self._schedule_operation_if_needed(operation)
        return self._operation_status_payload(
            operation,
            last_messages=_bounded_int(args.get("last_messages", 10), 1, 50),
            message_max_chars=_bounded_int(args.get("message_max_chars", 8000), 500, 200000),
            progress_events=_bounded_int(args.get("progress_events", 10), 0, 100),
            progress_max_chars=_bounded_int(args.get("progress_max_chars", 2000), 200, 20000),
            include_events=bool(args.get("include_events", False)),
        )

    def _schedule_operation_if_needed(self, operation: dict[str, Any]) -> None:
        operation_id = str(operation.get("operation_id") or "")
        if not operation_id:
            return
        if not self._can_execute_operations():
            return
        if str(operation.get("status") or "") not in OPERATION_STARTABLE_STATUSES:
            return
        if self._operation_config_mismatch(operation):
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
        if not self._can_execute_operations():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for operation in self.storage.list_startable_operations(
            now=_now_iso(),
            limit=limit,
            worker_config_fingerprint=self._config_fingerprint,
            allow_cross_config_recovery=self._allow_cross_config_recovery,
        ):
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
            worker_config_fingerprint=self._config_fingerprint,
            allow_cross_config_recovery=self._allow_cross_config_recovery,
            config_mismatch_message="Operation belongs to a different MCP config fingerprint. Set CODEX_MCP_ALLOW_CROSS_CONFIG_RECOVERY=1 only for manual emergency recovery.",
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
        operation_type_for_guard = str(operation.get("operation_type") or "")
        if _optional_string(operation.get("turn_id")) and operation_type_for_guard != "steer_turn":
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
            if operation_type == "fork_thread" and _optional_string(operation.get("thread_id")):
                payload["_resolved_thread_id"] = operation.get("thread_id")
                payload["chat_id"] = operation.get("thread_id")
                if operation.get("cwd"):
                    payload["cwd"] = operation.get("cwd")
                    payload["_resolved_project_path"] = operation.get("cwd")
                if not _optional_string(payload.get("message")):
                    fork_state = {
                        "accepted": True,
                        "sourceThreadId": payload.get("source_thread_id"),
                        "forkedThreadId": operation.get("thread_id"),
                        "hasInitialMessage": False,
                        "cwd": operation.get("cwd") or payload.get("cwd"),
                        "model": payload.get("model"),
                        "ephemeral": bool(payload.get("ephemeral", False)),
                        "turnId": None,
                    }
                    self.storage.update_operation(
                        operation_id,
                        status="completed",
                        phase="completed",
                        result_json=json.dumps({"ok": True, "operationType": "fork_thread", "forkState": fork_state}, ensure_ascii=False),
                        last_error=None,
                        completed_at=_now_iso(),
                        updated_at=_now_iso(),
                        next_attempt_at=None,
                    )
                    LOG.info(
                        "operation fork recovered completed operation_id=%s thread_id=%s",
                        operation_id,
                        operation.get("thread_id"),
                    )
                    return
            if (
                operation_type == "fork_thread"
                and payload.get("_fork_start_attempted")
                and not _optional_string(operation.get("thread_id"))
            ):
                self._mark_fork_start_unknown_after_attempt(
                    operation_id=operation_id,
                    source_thread_id=_optional_string(payload.get("source_thread_id")) or _optional_string(payload.get("_fork_source_thread_id")),
                    cwd=_optional_string(payload.get("cwd")) or _optional_string(operation.get("cwd")),
                    model=_optional_string(payload.get("model")),
                    ephemeral=bool(payload.get("ephemeral", False)),
                    has_initial_message=bool(_optional_string(payload.get("message"))),
                    attempted_at=_optional_string(payload.get("_fork_start_attempted_at")),
                    reason="MCP server restarted after thread/fork attempt before forked thread id was persisted.",
                    generation=operation.get("app_server_generation"),
                )
                LOG.info("operation fork marked unknown after attempted thread/fork operation_id=%s", operation_id)
                return
            if operation_type == "review_start" and payload.get("_review_start_attempted") and not _optional_string(operation.get("turn_id")):
                self._mark_review_start_unknown_after_attempt(
                    operation,
                    reason="MCP server restarted after review/start attempt before review turn id was persisted.",
                )
                LOG.info("operation review marked unknown after attempted review/start operation_id=%s", operation_id)
                return
            LOG.info("operation run start operation_id=%s type=%s", operation_id, operation_type)
            self.storage.update_operation(operation_id, status="starting_app_server", phase="starting_app_server", updated_at=_now_iso())
            if prompt_submission_id:
                self.storage.update_prompt_submission(prompt_submission_id, status="starting_app_server", updated_at=_now_iso())
            await self._app()
            if operation_type == "steer_turn":
                self.storage.update_operation(operation_id, status="starting_turn", phase="starting_turn", updated_at=_now_iso())
                result = await self._steer_turn_resolved(
                    thread_id=_required_string(payload, "thread_id"),
                    expected_turn_id=_required_string(payload, "expected_turn_id"),
                    message=str(payload.get("message") or ""),
                    args=_operation_tool_args(payload),
                )
            elif operation_type == "fork_thread":
                result = await self._fork_thread_resolved(
                    source_thread_id=_required_string(payload, "source_thread_id"),
                    message=_optional_string(payload.get("message")),
                    args=_operation_tool_args(payload),
                )
                LOG.info(
                    "operation fork accepted operation_id=%s thread_id=%s turn_id=%s",
                    operation_id,
                    result.get("threadId") or result.get("thread_id"),
                    result.get("turnId") or result.get("turn_id"),
                )
                return
            elif operation_type == "review_start":
                result = await self._review_start_resolved(args=_operation_tool_args(payload))
                LOG.info(
                    "operation review accepted operation_id=%s thread_id=%s turn_id=%s",
                    operation_id,
                    result.get("threadId") or result.get("thread_id"),
                    result.get("turnId") or result.get("turn_id"),
                )
                return
            else:
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
            elif str(current.get("operation_type") or "") == "review_start" and _operation_review_start_attempted(current):
                self._mark_review_start_unknown_after_attempt(
                    current,
                    reason="MCP server shut down after review/start attempt before review turn id was persisted.",
                )
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
            elif (
                str(current.get("operation_type") or "") == "review_start"
                and _operation_review_start_attempted(current)
                and _review_start_error_is_ambiguous(exc)
            ):
                self._mark_review_start_unknown_after_attempt(
                    current,
                    reason=f"Worker lost certainty after review/start attempt: {redact_text(str(exc), max_chars=500)}",
                )
            elif str(current.get("operation_type") or "") == "fork_thread" and _operation_request_from_row(current).get("_fork_start_attempted"):
                fork_payload = _operation_request_from_row(current)
                self._mark_fork_start_unknown_after_attempt(
                    operation_id=operation_id,
                    source_thread_id=_optional_string(fork_payload.get("source_thread_id")) or _optional_string(fork_payload.get("_fork_source_thread_id")),
                    cwd=_optional_string(current.get("cwd")) or _optional_string(fork_payload.get("cwd")),
                    model=_optional_string(fork_payload.get("model")),
                    ephemeral=bool(fork_payload.get("ephemeral", False)),
                    has_initial_message=bool(_optional_string(fork_payload.get("message"))),
                    attempted_at=_optional_string(fork_payload.get("_fork_start_attempted_at")),
                    reason=f"Worker lost certainty after thread/fork attempt: {redact_text(str(exc), max_chars=500)}",
                    generation=current.get("app_server_generation"),
                )
            elif str(current.get("operation_type") or "") == "review_start" and _operation_review_start_attempted(current):
                self.storage.update_operation(
                    operation_id,
                    status="failed",
                    phase="failed",
                    last_error=redact_text(str(exc), max_chars=500),
                    updated_at=_now_iso(),
                    completed_at=_now_iso(),
                    next_attempt_at=None,
                )
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
            with suppress(Exception):
                latest_for_locks = self.storage.get_operation(operation_id) or {}
                if str(latest_for_locks.get("status") or "") not in {"running", "first_message_received", "waiting_for_approval", "waiting_for_user_input"}:
                    self.storage.release_resource_locks_for_operation(operation_id)
            with suppress(Exception):
                latest_after_run = self.storage.get_operation(operation_id) or {}
                latest_status = str(latest_after_run.get("status") or "")
                if latest_status in OPERATION_TERMINAL_STATUSES:
                    queue_status = "completed" if latest_status == "completed" else "failed"
                    queued_reason = None
                elif latest_status in OPERATION_STARTABLE_STATUSES:
                    queue_status = "queued"
                    queued_reason = "retry_after_worker_error" if latest_after_run.get("last_error") else "waiting_for_worker"
                else:
                    queue_status = "running"
                    queued_reason = None
                self.storage.update_operation_scheduling(
                    operation_id,
                    queue_status=queue_status,
                    queued_reason=queued_reason,
                    updated_at=_now_iso(),
                    worker_id=self._worker_owner,
                )
            self.storage.release_operation_lease(operation_id, lease_owner=self._worker_owner, updated_at=_now_iso())
            task = self._operation_tasks.get(operation_id)
            if task is not None and task.done():
                self._operation_tasks.pop(operation_id, None)

    def _ensure_operation_scheduling(
        self,
        operation: dict[str, Any],
        *,
        agent_id: str | None = None,
        priority: str = "normal",
        estimated_cost_class: str = "normal",
        resource_keys: list[str] | None = None,
        queued_reason: str | None = None,
    ) -> None:
        operation_id = str(operation.get("operation_id") or "")
        if not operation_id:
            return
        existing = self.storage.get_operation_scheduling(operation_id)
        now = _now_iso()
        if existing is not None:
            return
        self.storage.upsert_operation_scheduling(
            operation_id=operation_id,
            agent_id=agent_id,
            priority=priority,
            estimated_cost_class=estimated_cost_class,
            resource_keys=resource_keys or [],
            queue_status="queued",
            queued_reason=queued_reason,
            created_at=now,
            updated_at=now,
        )

    def _operation_status_payload(
        self,
        operation: dict[str, Any],
        *,
        last_messages: int,
        message_max_chars: int,
        progress_events: int = 10,
        progress_max_chars: int = 2000,
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
                        "progress_events": progress_events,
                        "progress_max_chars": progress_max_chars,
                    }
                )
                if not (
                    str(latest.get("operation_type") or "") == "steer_turn"
                    and str(latest.get("status") or "") in OPERATION_STARTABLE_STATUSES
                ):
                    latest = self._reconcile_operation_with_turn(latest, turn_status)
            except CodexMcpError:
                turn_status = None
        response = _operation_row_to_tool(latest)
        response["operation_source"] = "durable_queue"
        response["operationSource"] = "durable_queue"
        response["lease_state"] = self._operation_lease_state(latest)
        response["leaseState"] = response["lease_state"]
        if thread_id:
            with suppress(Exception):
                self.storage.backfill_resource_lock_thread(
                    operation_id,
                    thread_id=thread_id,
                    project_id=_optional_string(latest.get("project_id")),
                )
        scheduling = self.storage.get_operation_scheduling(operation_id)
        if scheduling is not None and str(latest.get("status") or "") in OPERATION_TERMINAL_STATUSES:
            if scheduling.get("queue_status") != latest.get("status"):
                self.storage.update_operation_scheduling(
                    operation_id,
                    queue_status=str(latest.get("status") or "completed"),
                    queued_reason=None,
                    updated_at=_now_iso(),
                    slot_claim={"claimed": False},
                )
                self.storage.release_resource_locks_for_operation(operation_id)
                scheduling = self.storage.get_operation_scheduling(operation_id)
        if scheduling is not None:
            slot_claim = _safe_json_dict(scheduling.get("slot_claim_json"))
            worker_id = _optional_string(scheduling.get("worker_id"))
            worker = self.storage.get_worker(worker_id) if worker_id else None
            terminal_queue = str(latest.get("status") or "") in OPERATION_TERMINAL_STATUSES
            consumes_slot = _operation_consumes_turn_slot_for_status(latest)
            if thread_id and consumes_slot and not terminal_queue:
                with suppress(Exception):
                    self.storage.ensure_thread_active_lock_for_operation(
                        operation_id,
                        thread_id=thread_id,
                        project_id=_optional_string(latest.get("project_id")),
                        worker_id=worker_id,
                        expires_at=_future_iso(6 * 60 * 60),
                        created_at=_now_iso(),
                    )
            if not slot_claim and not terminal_queue and scheduling.get("queue_status") in {"scheduled", "running"} and consumes_slot:
                slot_claim = {
                    "claimed": True,
                    "workerId": worker_id,
                    "projectKey": latest.get("project_id") or (path_key(latest.get("cwd")) if latest.get("cwd") else None),
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "claimedAt": scheduling.get("scheduled_at"),
                    "source": "derived_from_running_operation",
                }
            response["queueState"] = {
                "queueStatus": scheduling.get("queue_status"),
                "queuedReason": scheduling.get("queued_reason"),
                "priority": scheduling.get("priority"),
                "estimatedCostClass": scheduling.get("estimated_cost_class"),
                "agentId": scheduling.get("agent_id"),
                "resourceKeys": _safe_json_list(scheduling.get("resource_keys_json")),
                "workerId": worker_id,
                "scheduledAt": scheduling.get("scheduled_at"),
                "updatedAt": scheduling.get("updated_at"),
            }
            _annotate_operation_worker_compatibility(response["queueState"], latest, self.storage)
            response["slotState"] = {"claimed": False} if terminal_queue else (slot_claim or {"claimed": False})
            response["workerState"] = {
                "workerId": worker_id,
                "status": (worker or {}).get("status"),
                "role": (worker or {}).get("role"),
                "lastHeartbeatAt": (worker or {}).get("last_heartbeat_at"),
                "stalenessSeconds": _staleness_seconds(str((worker or {}).get("last_heartbeat_at") or "")) if worker else None,
            }
            response["resourceLockState"] = {
                "locks": [
                    {
                        "lockKey": row.get("lock_key"),
                        "lockMode": row.get("lock_mode"),
                        "threadId": row.get("thread_id"),
                        "projectId": row.get("project_id"),
                        "workerId": row.get("worker_id"),
                        "expiresAt": row.get("expires_at"),
                    }
                    for row in self.storage.list_resource_locks(operation_id=operation_id, limit=20)
                ] if not terminal_queue else []
            }
        else:
            response["queueState"] = {
                "queueStatus": "legacy_inline",
                "queuedReason": None,
                "priority": "normal",
                "estimatedCostClass": "normal",
                "agentId": None,
                "resourceKeys": [],
                "workerId": None,
                "scheduledAt": None,
                "updatedAt": latest.get("updated_at"),
            }
            response["slotState"] = {"claimed": False}
            response["workerState"] = {"workerId": None, "status": None, "role": None}
            response["resourceLockState"] = {"locks": []}
        if self._operation_config_mismatch(latest):
            response["configRecoveryState"] = {
                "state": "mismatch",
                "submitterConfigFingerprint": latest.get("submitter_config_fingerprint"),
                "workerConfigFingerprint": self._config_fingerprint,
                "crossConfigRecoveryAllowed": self._allow_cross_config_recovery,
            }
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
        response["reconciliationState"] = _operation_reconciliation_state(latest, turn_status)
        response["latestMessages"] = (turn_status or {}).get("latestMessages") or (turn_status or {}).get("last_messages") or []
        if turn_status and "progressEvents" in turn_status:
            response["progressEvents"] = turn_status.get("progressEvents") or []
            response["progressEventCount"] = turn_status.get("progressEventCount", 0)
            response["latestProgressAt"] = turn_status.get("latestProgressAt")
            response["tokenUsage"] = turn_status.get("tokenUsage")
            response["modelReroutes"] = turn_status.get("modelReroutes") or []
            response["warnings"] = turn_status.get("warnings") or []
        pending_interactions = self._pending_interactions_for_context(thread_id=thread_id, turn_id=turn_id, status="pending", limit=20)
        if not pending_interactions:
            pending_interactions = (turn_status or {}).get("pendingInteractions") or []
        response["pendingInteractions"] = pending_interactions
        final_report = self._operation_final_report(
            latest,
            turn_status=turn_status,
            message_max_chars=message_max_chars,
        )
        if final_report is not None:
            response["finalReport"] = final_report
            refreshed = self.storage.get_operation(operation_id) or latest
            response["latestReportHash"] = refreshed.get("latest_report_hash")
            output_schema_state = response.get("outputSchemaState")
            if isinstance(output_schema_state, dict):
                updated_schema_state = dict(output_schema_state)
                updated_schema_state["parseStatus"] = final_report.get("structuredParseStatus")
                updated_schema_state["structuredStatus"] = final_report.get("structuredStatus")
                response["outputSchemaState"] = updated_schema_state
        pending_can_drive_operation = not (
            str(response.get("operationType") or "") == "steer_turn"
            and str(response.get("status") or "") in OPERATION_STARTABLE_STATUSES
        )
        if pending_interactions and pending_can_drive_operation and str(response.get("status") or "") not in OPERATION_TERMINAL_STATUSES:
            waiting_status = "waiting_for_user_input" if any(item.get("kind") == "user_input" for item in pending_interactions) else "waiting_for_approval"
            response["status"] = waiting_status
            response["phase"] = waiting_status
            if str(latest.get("status") or "") != waiting_status:
                self.storage.update_operation(operation_id, status=waiting_status, phase=waiting_status, updated_at=_now_iso())
        response["source"] = "live" if turn_status and turn_status.get("source") == "live" else "storage"
        operation_row_age = _staleness_seconds(str(latest.get("updated_at") or ""))
        response["operationRowAgeSeconds"] = operation_row_age
        response["stalenessSeconds"] = operation_row_age
        response["stalenessMeaning"] = "operation_row_age"
        response["turnFreshness"] = {
            "lastProgressAgeSeconds": _staleness_seconds(str(response.get("latestProgressAt") or "")),
            "turnStatusAgeSeconds": _staleness_seconds(str((turn_status or {}).get("updatedAt") or (turn_status or {}).get("updated_at") or "")),
        }
        response["workerFreshness"] = {
            "heartbeatAgeSeconds": (response.get("workerState") or {}).get("stalenessSeconds"),
        }
        response["nextRecommendedAction"] = _operation_next_action(response)
        if response.get("queueState", {}).get("queuedReason") in {"resource_lock_conflict", "write_project_slot_limit"}:
            response["nextRecommendedAction"] = "wait_for_resource_lock"
        elif response.get("queueState", {}).get("queuedReason") in {"global_slot_limit", "project_slot_limit", "agent_slot_limit", "thread_slot_limit"}:
            response["nextRecommendedAction"] = "wait_for_worker_slot"
        elif response.get("queueState", {}).get("queuedReason") in {"worker_health_degraded", "app_server_backpressure", "config_fingerprint_mismatch"}:
            response["nextRecommendedAction"] = "inspect_worker_health"
        if response.get("configRecoveryState", {}).get("state") == "mismatch":
            response["nextRecommendedAction"] = "inspect_diagnostics"
        response["recommendedPollAfterSeconds"] = _operation_poll_after(response)
        response["pollRecommended"] = response["status"] not in OPERATION_TERMINAL_STATUSES
        if response.get("configRecoveryState", {}).get("state") == "mismatch":
            response["recommendedPollAfterSeconds"] = 0
            response["pollRecommended"] = False
        if include_events and (thread_id or turn_id):
            response["events"] = [
                event_to_tool(row, include_payload=False)
                for row in self.storage.list_app_server_events(thread_id=thread_id, turn_id=turn_id, limit=50)
            ]
        return self._attach_agent_guidance(normalize_public_status_payload(response, surface="operation_status"), surface="operation_status")

    def _operation_final_report(
        self,
        operation: dict[str, Any],
        *,
        turn_status: dict[str, Any] | None,
        message_max_chars: int,
    ) -> dict[str, Any] | None:
        request_payload = _operation_request_from_row(operation)
        if _is_plan_mode_operation_request(request_payload):
            return None
        stored: dict[str, Any] | None = None
        try:
            loaded = json.loads(str(operation.get("final_report_json") or "null"))
            if isinstance(loaded, dict):
                stored = loaded
        except json.JSONDecodeError:
            stored = None
        output_schema_state = request_payload.get("_output_schema_state") if isinstance(request_payload.get("_output_schema_state"), dict) else None
        schema_hash = _optional_string((output_schema_state or {}).get("schemaHash")) or _optional_string(request_payload.get("output_schema_hash"))
        final_text = _optional_string((turn_status or {}).get("finalMessage")) or _optional_string((turn_status or {}).get("final_message"))
        if not _turn_status_has_trusted_terminal_evidence(turn_status):
            return None
        if turn_status is not None and str(turn_status.get("status") or "") == "completed" and final_text:
            report_hash, report_json = _stored_final_report_json(
                final_text=final_text,
                thread_id=(turn_status or {}).get("threadId") or operation.get("thread_id"),
                turn_id=(turn_status or {}).get("turnId") or operation.get("turn_id"),
                source=(turn_status or {}).get("source") or "storage",
                schema_hash=schema_hash,
            )
            if report_hash != operation.get("latest_report_hash") or stored is None:
                self.storage.update_operation(
                    str(operation["operation_id"]),
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
        trusted_terminal = _turn_status_has_trusted_terminal_evidence(turn_status)
        turn_active = turn_state in TURN_ACTIVE_STATUSES or turn_state in {"starting", "ready"}
        next_status = current_status
        next_phase = str(operation.get("phase") or current_status or "unknown")
        completed_at = operation.get("completed_at")
        last_error = operation.get("last_error")
        latest_report_hash = operation.get("latest_report_hash")
        final_report_json = operation.get("final_report_json")
        status_corrected = False
        if current_status in OPERATION_TERMINAL_STATUSES and not trusted_terminal:
            next_status = "running"
            next_phase = "running"
            completed_at = None
            last_error = None
            latest_report_hash = None
            final_report_json = None
            status_corrected = True
        elif turn_state in {"completed"} and trusted_terminal:
            next_status = "completed"
            next_phase = "completed"
            completed_at = turn_status.get("completedAt") or turn_status.get("completed_at") or completed_at or _now_iso()
            last_error = None
        elif turn_state in {"failed", "aborted", "cancelled", "canceled", "interrupted"} and trusted_terminal:
            next_status = "failed" if turn_state == "failed" else turn_state
            next_phase = next_status
            completed_at = turn_status.get("completedAt") or turn_status.get("completed_at") or completed_at or _now_iso()
            last_error = turn_status.get("last_error") or last_error
        elif turn_state == "unknown_after_app_server_exit" and trusted_terminal:
            next_status = "unknown_after_app_server_exit"
            next_phase = "unknown_after_app_server_exit"
            completed_at = turn_status.get("completedAt") or turn_status.get("completed_at") or completed_at or _now_iso()
            last_error = turn_status.get("lastError") or turn_status.get("last_error") or last_error
        elif turn_state in {"waiting_for_approval", "waiting_for_user_input"}:
            next_status = turn_state
            next_phase = turn_state
        elif turn_state:
            next_status = "running" if current_status not in OPERATION_STARTABLE_STATUSES else current_status
            next_phase = "running" if next_status == "running" else next_phase
        if (
            next_status != current_status
            or next_phase != operation.get("phase")
            or completed_at != operation.get("completed_at")
            or last_error != operation.get("last_error")
            or latest_report_hash != operation.get("latest_report_hash")
            or final_report_json != operation.get("final_report_json")
        ):
            self.storage.update_operation(
                str(operation["operation_id"]),
                status=next_status,
                phase=next_phase,
                completed_at=completed_at,
                last_error=last_error,
                latest_report_hash=latest_report_hash,
                final_report_json=final_report_json,
                updated_at=_now_iso(),
            )
            self.storage.update_prompt_submission_by_operation(
                str(operation["operation_id"]),
                status=next_status,
                updated_at=_now_iso(),
            )
            if next_status in OPERATION_TERMINAL_STATUSES:
                self._finalize_operation_queue_state(str(operation["operation_id"]), next_status=next_status)
            updated_operation = self.storage.get_operation(str(operation["operation_id"])) or operation
            if status_corrected:
                updated_operation = dict(updated_operation)
                updated_operation["_status_corrected"] = True
            return updated_operation
        return operation

    def _finalize_operation_queue_state(self, operation_id: str, *, next_status: str) -> None:
        with suppress(Exception):
            self.storage.update_operation_scheduling(
                operation_id,
                queue_status=next_status,
                queued_reason=None,
                updated_at=_now_iso(),
                slot_claim={"claimed": False},
            )
        with suppress(Exception):
            self.storage.release_resource_locks_for_operation(operation_id)

    def _persist_recovered_turn_terminal(self, turn_id: str, status: dict[str, Any]) -> None:
        evidence = status.get("terminalEvidence") if isinstance(status.get("terminalEvidence"), dict) else {}
        if not evidence.get("trusted"):
            return
        completed_at = _optional_string(status.get("completedAt")) or _optional_string(status.get("completed_at")) or _optional_string(evidence.get("observedAt"))
        final_message = _optional_string(status.get("finalMessage")) or _optional_string(status.get("final_message"))
        with suppress(Exception):
            self.storage.update_tracked_turn_status(
                turn_id,
                status=str(status.get("status") or "completed"),
                updated_at=completed_at or _now_iso(),
                completed_at=completed_at,
                final_message=final_message,
                last_assistant_message=final_message,
                clear_last_error=str(status.get("status") or "") == "completed",
            )

    def _operation_config_mismatch(self, operation: dict[str, Any]) -> bool:
        if self._allow_cross_config_recovery:
            return False
        submitter = _optional_string(operation.get("submitter_config_fingerprint"))
        return bool(submitter and submitter != self._config_fingerprint)

    def _attach_progress_status(
        self,
        status: dict[str, Any],
        turn_id: str,
        *,
        progress_events: int,
        progress_max_chars: int,
    ) -> dict[str, Any]:
        if progress_events <= 0 or "progressEvents" in status:
            return status
        status.update(
            turn_progress_status_fields(
                self.storage,
                turn_id,
                progress_events=progress_events,
                progress_max_chars=progress_max_chars,
            )
        )
        if status.get("tokenUsage") is not None:
            status["tokenUsage"] = _compact_turn_token_usage(status.get("tokenUsage"))
        return status

    def _tracked_turn_status(
        self,
        turn_id: str,
        *,
        last_messages: int,
        message_max_chars: int,
        progress_events: int = 10,
        progress_max_chars: int = 2000,
    ) -> dict[str, Any] | None:
        tracker = self._app_server.tracker if self._app_server is not None else None
        if tracker is not None:
            status = tracker.get_turn_status(
                turn_id,
                last_messages=last_messages,
                message_max_chars=message_max_chars,
                progress_events=progress_events,
                progress_max_chars=progress_max_chars,
            )
            if status is not None:
                process = getattr(self._app_server, "process", None)
                running = process is not None and getattr(process, "returncode", None) is None
                status["source"] = "live" if running else "storage"
                status["appServerGeneration"] = getattr(self._app_server, "process_generation", status.get("processGeneration"))
                status["latestMessages"] = _filter_public_messages(status.get("latestMessages") or status.get("last_messages") or [])
                status["last_messages"] = status["latestMessages"]
                if status.get("tokenUsage") is not None:
                    status["tokenUsage"] = _compact_turn_token_usage(status.get("tokenUsage"))
                if str(status.get("status") or "") != "completed":
                    status["finalMessage"] = None
                    status["final_message"] = None
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
            if _optional_string(message.get("text"))
        ]
        final_message = _truncate_text(turn.get("final_message"), message_max_chars)[0]
        status_value = _turn_status_with_final_message(turn["status"], final_message)
        terminal_evidence = _terminal_evidence_from_status(
            status_value,
            source="app_server",
            observed_at=turn.get("completed_at"),
            method="turn_lifecycle_event",
        )
        completion_observed = bool(terminal_evidence.get("trusted"))
        if not completion_observed or status_value != "completed":
            final_message = None
        last_error = _tracked_turn_last_error(turn)
        plans = [_plan_row_to_tool(row, message_max_chars) for row in self.storage.get_tracked_turn_plans(turn_id)]
        latest_plan = _latest_plan(plans)
        result = {
            "ok": True,
            "thread_id": turn["thread_id"],
            "threadId": turn["thread_id"],
            "turn_id": turn["turn_id"],
            "turnId": turn["turn_id"],
            "chat_id": turn.get("chat_id"),
            "chatId": turn.get("chat_id"),
            "project_id": turn.get("project_id"),
            "projectId": turn.get("project_id"),
            "status": status_value,
            "completion_observed": completion_observed,
            "completionObserved": completion_observed,
            "terminalEvidence": terminal_evidence,
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
            "final_message": final_message,
            "finalMessage": final_message,
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
        return self._attach_progress_status(result, turn_id, progress_events=progress_events, progress_max_chars=progress_max_chars)

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
            if message.turn_id == turn_id and message.role == "assistant" and _optional_string(message.text)
        ][-last_messages:]
        chat = self.catalog.get_chat(summary.thread_id or hook_thread_id)
        final_message = _truncate_text(turns_last_assistant(summary.messages, turn_id), message_max_chars)[0]
        terminal_evidence = _terminal_evidence_from_status(
            turn.status,
            source="hook_stop",
            observed_at=turn.completed_at,
            method="hook_stop",
        )
        completion_observed = bool(terminal_evidence.get("trusted"))
        if not completion_observed or turn.status != "completed":
            final_message = None
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
            "terminalEvidence": terminal_evidence,
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
            if message.turn_id == turn_id and message.role == "assistant" and _optional_string(message.text)
        ][-last_messages:]
        chat = self.catalog.get_chat(summary.thread_id or thread_dir.name)
        final_message = messages[-1]["text"] if messages else None
        terminal_evidence = _terminal_evidence_from_status(
            turn.status,
            source="transcript_terminal",
            observed_at=turn.completed_at,
            method="transcript_terminal_record",
        )
        completion_observed = bool(terminal_evidence.get("trusted"))
        if not completion_observed or turn.status != "completed":
            final_message = None
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
            "terminalEvidence": terminal_evidence,
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


def _priority_value(value: Any) -> str:
    selected = str(value or "normal").strip().lower()
    if selected not in {"low", "normal", "high"}:
        raise invalid_argument("Unsupported operation priority.", priority=selected)
    return selected


def _estimated_cost_class_value(value: Any) -> str:
    selected = str(value or "normal").strip().lower()
    if selected not in {"light", "normal", "heavy"}:
        raise invalid_argument("Unsupported estimated_cost_class.", estimated_cost_class=selected)
    return selected


def _resource_keys_value(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise invalid_argument("resource_keys must be an array of strings.")
    keys: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        key = str(raw or "").strip()
        if not key:
            raise invalid_argument("resource_keys entries must be non-empty strings.", index=index)
        if len(key) > 300:
            raise invalid_argument("resource_keys entry is too long.", index=index, maxLength=300)
        normalized = re.sub(r"\s+", " ", key)
        if normalized not in seen:
            seen.add(normalized)
            keys.append(normalized)
    if len(keys) > 50:
        raise invalid_argument("Too many resource_keys.", maxItems=50, actualItems=len(keys))
    return keys


def _thread_mode_value(value: Any, *, operation_type: str) -> str:
    if value in (None, ""):
        if operation_type == "start_chat":
            return "new_thread"
        if operation_type in {"send_message", "execute_plan", "steer_turn"}:
            return "continue_thread"
        return "auto"
    selected = str(value).strip()
    if selected not in {"new_thread", "continue_thread", "auto"}:
        raise invalid_argument("Unsupported thread_mode.", thread_mode=selected)
    if operation_type == "start_chat" and selected == "continue_thread":
        raise invalid_argument("start_chat cannot use thread_mode=continue_thread.")
    if operation_type in {"send_message", "execute_plan", "steer_turn"} and selected == "new_thread":
        raise invalid_argument(f"{operation_type} cannot use thread_mode=new_thread.", operation_type=operation_type)
    return selected


def _dedup_policy_value(value: Any) -> str:
    if value in (None, ""):
        return "active_prompt_guard"
    selected = str(value).strip()
    if selected not in {"idempotency_only", "active_prompt_guard", "allow_parallel_with_resource_keys"}:
        raise invalid_argument("Unsupported dedup_policy.", dedup_policy=selected)
    return selected


def _dedup_resource_key_decision(current: list[str], existing: list[str]) -> dict[str, Any]:
    current_set = {str(item).casefold() for item in current if str(item).strip()}
    existing_set = {str(item).casefold() for item in existing if str(item).strip()}
    overlap = sorted(current_set & existing_set)
    compared = bool(current_set and existing_set)
    if compared and not overlap:
        reason = "disjoint_resource_keys"
    elif compared:
        reason = "overlapping_resource_keys"
    else:
        reason = "missing_resource_keys"
    return {
        "reason": reason,
        "compared": compared,
        "overlap": overlap,
    }


def _annotate_operation_worker_compatibility(queue_state: dict[str, Any], operation: dict[str, Any], storage: Any) -> None:
    submitter = _optional_string(operation.get("submitter_config_fingerprint"))
    live_fingerprints: set[str] = set()
    for row in storage.list_workers(limit=100):
        if str(row.get("role") or "") != "worker":
            continue
        if str(row.get("status") or "") != "running":
            continue
        staleness = _staleness_seconds(str(row.get("last_heartbeat_at") or ""))
        if staleness is not None and int(staleness) >= 120:
            continue
        fingerprint = _optional_string(row.get("config_fingerprint"))
        if fingerprint:
            live_fingerprints.add(fingerprint)
    if not submitter or not live_fingerprints:
        queue_state["workerCompatibility"] = {
            "compatibleWorkerAvailable": bool(live_fingerprints),
            "reason": "unknown_submitter_config" if not submitter else "no_live_worker",
        }
        return
    if submitter in live_fingerprints:
        queue_state["workerCompatibility"] = {
            "compatibleWorkerAvailable": True,
            "reason": "compatible_worker_available",
        }
        return
    queue_state["workerCompatibility"] = {
        "compatibleWorkerAvailable": False,
        "reason": "config_fingerprint_mismatch",
        "submitterConfigFingerprint": submitter,
        "liveWorkerConfigFingerprints": sorted(live_fingerprints),
    }
    if queue_state.get("queueStatus") == "queued" and queue_state.get("queuedReason") == "waiting_for_worker":
        queue_state["queuedReason"] = "config_fingerprint_mismatch"


def _operation_consumes_turn_slot_for_status(operation: dict[str, Any]) -> bool:
    operation_type = str(operation.get("operation_type") or "")
    request = _operation_request_from_row(operation)
    if operation_type == "steer_turn":
        return False
    if operation_type == "fork_thread" and not _optional_string(request.get("message")):
        return False
    return True


def _is_plan_mode_operation_request(request: dict[str, Any]) -> bool:
    collaboration_mode = _optional_string(request.get("collaboration_mode")) or _optional_string(request.get("collaborationMode"))
    return collaboration_mode == "plan"


def _filter_public_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    filtered: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = _optional_string(message.get("text"))
        if not text:
            continue
        copied = dict(message)
        copied["text"] = text
        filtered.append(copied)
    return filtered


def _compact_turn_token_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("precision") == "coarse" and "totalTokensBand" in value:
        return dict(value)
    totals = _collect_token_counts(value)
    return {
        "available": bool(totals),
        "precision": "coarse",
        "totalTokensBand": _token_count_band(totals.get("totalTokens")),
        "inputTokensBand": _token_count_band(totals.get("inputTokens")),
        "outputTokensBand": _token_count_band(totals.get("outputTokens")),
        "bucketCount": _count_usage_buckets(value),
        "identityRedacted": True,
    }


def _collect_token_counts(value: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    if isinstance(value, dict):
        for key, raw in value.items():
            if key in {"totalTokens", "inputTokens", "outputTokens"}:
                try:
                    result[key] = max(result.get(key, 0), int(raw))
                except (TypeError, ValueError):
                    pass
            nested = _collect_token_counts(raw)
            for nested_key, nested_value in nested.items():
                result[nested_key] = max(result.get(nested_key, 0), nested_value)
    elif isinstance(value, list):
        for item in value:
            nested = _collect_token_counts(item)
            for nested_key, nested_value in nested.items():
                result[nested_key] = max(result.get(nested_key, 0), nested_value)
    return result


def _count_usage_buckets(value: Any) -> int:
    if isinstance(value, dict):
        count = 1 if any(key in value for key in {"totalTokens", "inputTokens", "outputTokens"}) else 0
        return count + sum(_count_usage_buckets(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_usage_buckets(item) for item in value)
    return 0


def _token_count_band(value: int | None) -> str | None:
    if value is None:
        return None
    if value <= 0:
        return "0"
    if value < 1_000:
        return "<1k"
    if value < 10_000:
        return "1k-10k"
    if value < 100_000:
        return "10k-100k"
    if value < 1_000_000:
        return "100k-1m"
    return "1m+"


def _safe_json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []

