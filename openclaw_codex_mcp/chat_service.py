from __future__ import annotations

from . import tools as _tools
from .search import SearchIndexStatus

globals().update(_tools.__dict__)


class ChatServiceMixin:
    def codex_list_projects(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        compact = bool(args.get("compact", True))
        limit = _bounded_int(args.get("limit", 200), 1, 1000)
        refresh = bool(args.get("refresh", False))
        include_private_details = bool(args.get("include_private_details", False))
        root_filters = [path_key(item) for item in (args.get("roots") or []) if str(item or "").strip()]
        started = time.monotonic()
        time_budget_seconds = _bounded_int(args.get("time_budget_seconds", 5), 1, 60)
        cache_hit = False
        refresh_skipped_reason = None
        if refresh:
            if compact and root_filters and not include_private_details:
                projects_raw = self.catalog.load_cached_projects()
                cache_hit = bool(projects_raw)
                refresh_skipped_reason = "scoped_compact_refresh_uses_cache"
                if not projects_raw:
                    projects_raw = self.catalog.list_projects()
            else:
                self.catalog.refresh()
                projects_raw = self.catalog.list_projects()
        else:
            projects_raw = self.catalog.load_cached_projects()
            cache_hit = bool(projects_raw)
            if not projects_raw:
                projects_raw = self.catalog.list_projects()
        if root_filters:
            projects_raw = [
                project
                for project in projects_raw
                if any(project.normalized_path_key.startswith(root_filter) or path_key(project.path).startswith(root_filter) for root_filter in root_filters)
            ]
        total = len(projects_raw)
        page = projects_raw[:limit]
        projects = [
            _project_to_tool(project, compact=compact, include_private_details=include_private_details)
            for project in page
        ]
        LOG.info("list_projects count=%d returned=%d compact=%s cache_hit=%s", total, len(projects), compact, cache_hit)
        result = {
            "projects": projects,
            "totalCount": total,
            "returnedCount": len(projects),
            "truncated": total > len(projects),
            "compact": compact,
            "cacheState": {
                "hit": cache_hit,
                "source": "storage_cache" if cache_hit else "catalog_refresh",
                "refreshRequested": refresh,
                "refreshSkippedReason": refresh_skipped_reason,
                "timeBudgetSeconds": time_budget_seconds,
                "timeBudgetExhausted": (time.monotonic() - started) > time_budget_seconds,
            },
        }
        return _with_budget(result, tool_name="codex_list_projects")

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
        include_snippets = bool(args.get("include_snippets", bool(project_id)))
        snippets_per_chat = _bounded_int(args.get("snippets_per_chat", 2), 0, 5)
        snippet_max_chars = _bounded_int(args.get("snippet_max_chars", 240), 80, 1000)
        refresh_index = bool(args.get("refresh_index", True))
        index_time_budget_seconds = _bounded_int(args.get("index_time_budget_seconds", 8), 1, 60)
        started = time.monotonic()
        match_mode = str(args.get("match_mode") or "auto")
        search_index = SearchIndex(self.config, self.storage, self.catalog)
        index_status = None
        if refresh_index:
            if not project_id and index_time_budget_seconds <= 3:
                index_status = SearchIndexStatus(
                    refreshed=False,
                    indexed_files=0,
                    skipped_unchanged_files=0,
                    pending_files=0,
                    time_budget_exhausted=True,
                )
            else:
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
        exhausted = bool((result["index_status"] or {}).get("time_budget_exhausted"))
        if time.monotonic() - started > index_time_budget_seconds:
            exhausted = True
            result["index_status"]["time_budget_exhausted"] = True
            result["index_status"]["end_to_end_time_budget_exhausted"] = True
        result["nextRecommendedAction"] = "retry_without_refresh_or_increase_budget" if exhausted else "none"
        result["recommendedPollAfterSeconds"] = 0
        result["pollRecommended"] = False
        return _with_budget(result, tool_name="codex_search_chats", truncated_fields=["snippets", "title", "last_message_preview"])

    def codex_get_chat_status(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = _required_string(args, "chat_id")
        project_id = args.get("project_id")
        preview_max_chars = _bounded_int(args.get("preview_max_chars", 300), 20, 4000)
        chat = self._resolve_chat_for_read(chat_id, str(project_id) if project_id else None)
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
        chat = self._resolve_chat_for_read(chat_id, str(project_id) if project_id else None)
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
        if transcript_path.startswith(TRACKED_TURN_HISTORY_PREFIX):
            thread_id = transcript_path[len(TRACKED_TURN_HISTORY_PREFIX) :] or chat.thread_id
            summary = self._tracked_turn_transcript_summary(thread_id, chat=chat)
            fingerprint_size = sum(len(str(message.text or "")) for message in summary.messages)
            return summary, {
                "path": transcript_path,
                "size": fingerprint_size,
                "mtime_ns": _stable_mtime_ns(summary.updated_at),
                "mtime": summary.updated_at,
                "source": "tracked_turn",
            }
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

    def _resolve_chat_for_read(self, chat_id: str, project_id: str | None) -> Chat | None:
        resolution = self.thread_resolver.resolve(chat_id, project_id, refresh_catalog=False)
        return resolution.chat if resolution is not None else None

    def _refresh_thread_tracking_from_transcript(self, thread_id: str, *, chat: Chat | None = None) -> dict[str, Any]:
        if not thread_id:
            return {"imported": False, "reason": "missing_thread_id"}
        target_chat = chat or self._resolve_chat_for_read(thread_id, None)
        if target_chat is None:
            return {"imported": False, "reason": "chat_not_found"}
        transcript_path = self.catalog.locate_transcript(target_chat)
        if not transcript_path:
            return {"imported": False, "reason": "transcript_not_found"}
        if str(transcript_path).startswith((HOOK_HISTORY_PREFIX, TRACKED_TURN_HISTORY_PREFIX)):
            return {"imported": False, "reason": "virtual_history_source", "source": transcript_path.split(":", 1)[0]}
        path = Path(transcript_path)
        if not path.exists() or not path.is_file():
            return {"imported": False, "reason": "transcript_not_readable"}
        try:
            return import_transcript_to_tracking(self.storage, path, archived=target_chat.archived)
        except Exception as exc:
            LOG.warning("transcript import failed thread_id=%s path=%s error=%s", thread_id, path, exc)
            return {"imported": False, "reason": "transcript_import_failed", "error": redact_text(str(exc), max_chars=500)}

    def _tracked_turn_transcript_summary(self, thread_id: str, *, chat: Chat) -> TranscriptSummary:
        turn_rows = self.storage.list_tracked_turns_for_thread(thread_id)
        if not turn_rows:
            latest = self.storage.get_latest_tracked_turn_for_thread(thread_id)
            turn_rows = [latest] if latest is not None else []
        turns: dict[str, TranscriptTurn] = {}
        messages: list[TranscriptMessage] = []
        for turn in turn_rows:
            if turn is None:
                continue
            turn_id = str(turn.get("turn_id") or "")
            if not turn_id:
                continue
            turns[turn_id] = TranscriptTurn(
                turn_id=turn_id,
                thread_id=thread_id,
                started_at=_optional_string(turn.get("started_at")),
                completed_at=_optional_string(turn.get("completed_at")),
                status=str(turn.get("status") or "unknown"),
            )
            for row in self.storage.get_last_tracked_turn_messages(turn_id, 10_000):
                messages.append(
                    TranscriptMessage(
                        message_id=str(row.get("id") or row.get("event_hash") or ""),
                        thread_id=thread_id,
                        turn_id=turn_id,
                        role=str(row.get("role") or "assistant"),
                        created_at=_optional_string(row.get("created_at")),
                        text=_optional_string(row.get("text")),
                        items=[],
                        metadata={"source": "tracked_turn"},
                    )
                )
        messages.sort(key=lambda item: item.created_at or "")
        created_at = min((str(turn.get("started_at")) for turn in turn_rows if turn and turn.get("started_at")), default=chat.created_at)
        updated_at = max((str(turn.get("updated_at")) for turn in turn_rows if turn and turn.get("updated_at")), default=chat.updated_at)
        return TranscriptSummary(
            thread_id=thread_id,
            title=chat.title,
            project_path=chat.project_path,
            created_at=created_at,
            updated_at=updated_at,
            transcript_path=f"{TRACKED_TURN_HISTORY_PREFIX}{thread_id}",
            messages=messages,
            turns=turns,
            parse_errors=0,
            archived=False,
        )

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


def _project_to_tool(project: Project, *, compact: bool, include_private_details: bool) -> dict[str, Any]:
    payload = {
        "project_id": project.project_id,
        "projectId": project.project_id,
        "name": project.name,
        "last_activity_at": project.last_activity_at,
        "lastActivityAt": project.last_activity_at,
        "source": project.source
        if project.source in {"app_server", "sqlite", "transcript_index", "registry", "disk_scan", "hook_history", "kb_history", "mixed"}
        else "mixed",
    }
    if include_private_details or not compact:
        payload["path"] = project.path
        payload["normalizedPathKey"] = project.normalized_path_key
    return payload

