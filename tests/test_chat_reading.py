from __future__ import annotations

from tests.helpers import *


class McpDefinitionTests(unittest.TestCase):
    def test_get_chat_falls_back_to_tracked_turn_when_catalog_misses_fresh_thread(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                tracker = TurnTracker(service.storage)
                tracker.register_turn(
                    turn_id="turn-fresh",
                    thread_id="thread-fresh",
                    chat_id="thread-fresh",
                    project_id="project",
                    project_path=str(root),
                    status="running",
                    started_at="2026-05-25T00:00:00+00:00",
                    user_message="fresh user prompt",
                )
                tracker.record_event(
                    {
                        "method": "item/created",
                        "params": {
                            "threadId": "thread-fresh",
                            "turnId": "turn-fresh",
                            "item": {"type": "agentMessage", "text": "fresh tracked response"},
                        },
                    },
                    received_at="2026-05-25T00:00:01+00:00",
                )
                chat_status = service.codex_get_chat_status({"chat_id": "thread-fresh"})
                chat = service.codex_get_chat({"chat_id": "thread-fresh", "format": "compact"})
            finally:
                asyncio.run(service.close())

        self.assertIn(chat_status["transcript"]["source"], {"hook_history", "tracked_turn"})
        self.assertTrue(chat["source"].startswith(("hook_history+", "tracked_turn+")))
        self.assertIn("fresh tracked response", json.dumps(chat["messages"], ensure_ascii=False))

    def test_search_query_parser_handles_modes_and_escaping(self) -> None:
        phrase = build_fts_query('"точная фраза"', "auto")
        all_terms = build_fts_query("alpha beta!", "all_terms")
        any_term = build_fts_query("alpha beta", "any_term")
        punctuation = build_fts_query("!!!", "auto")

        self.assertEqual("phrase", phrase.match_mode)
        self.assertEqual('"точная фраза"', phrase.fts_query)
        self.assertEqual("all_terms", all_terms.match_mode)
        self.assertEqual('"alpha" "beta"', all_terms.fts_query)
        self.assertEqual('"alpha" OR "beta"', any_term.fts_query)
        self.assertEqual('"__openclaw_no_terms__"', punctuation.fts_query)

    def test_project_cache_upsert_deduplicates_by_path(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "state.sqlite3")
            storage.connect()
            try:
                base = {
                    "project_id": "registry-id",
                    "name": "OpenClaw",
                    "path": "D:\\CodexProjects\\OpenClaw",
                    "normalized_path_key": "d:/codexprojects/openclaw",
                    "created_at": None,
                    "last_activity_at": None,
                    "source": "mixed",
                    "updated_at": "2026-05-25T00:00:00+00:00",
                }
                storage.upsert_project(base)
                storage.upsert_project(
                    {
                        **base,
                        "project_id": "computed-id",
                        "name": "OpenClaw updated",
                        "source": "sqlite",
                        "updated_at": "2026-05-25T00:01:00+00:00",
                    }
                )
                storage.commit()

                rows = storage.connection.execute("SELECT project_id, name, source FROM projects").fetchall()
                self.assertEqual(1, len(rows))
                self.assertEqual("registry-id", rows[0]["project_id"])
                self.assertEqual("OpenClaw updated", rows[0]["name"])
                self.assertEqual("sqlite", rows[0]["source"])
            finally:
                storage.close()

    def test_catalog_prefers_canonical_existing_path_over_registry_casing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Vibecoding1c"
            project.mkdir()
            wrong_case = project.parent / "vibecoding1c"
            if os.name != "nt" and not wrong_case.exists():
                try:
                    wrong_case.symlink_to(project, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"case-alias symlink is not available: {exc}")
            wrong_case_path = str(wrong_case)
            registry = root / "projects.json"
            registry.write_text(
                json.dumps(
                    [
                        {
                            "project_id": "registry-vibe-id",
                            "name": "vibecoding1c",
                            "root_path": wrong_case_path,
                            "created_at": "2026-05-25T00:00:00+00:00",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            state_db = root / ".codex" / "state_5.sqlite"
            state_db.parent.mkdir(parents=True)
            _create_threads_db(
                state_db,
                [
                    {
                        "id": "thread-vibe",
                        "rollout_path": None,
                        "cwd": str(project),
                        "title": "Vibe",
                        "preview": "",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667200000,
                        "archived": 0,
                    }
                ],
            )
            config = _search_service_config(root, state_db)
            config.projects_registry_path = registry
            config.allowed_roots = [project]
            service = ToolService(config)
            try:
                projects = service.codex_list_projects()["projects"]
                chats = service.codex_list_project_chats({"project_id": "registry-vibe-id", "include_archived": True})["chats"]
                vibe_project = next(item for item in projects if item["project_id"] == "registry-vibe-id")
                self.assertEqual("Vibecoding1c", vibe_project["name"])
                self.assertTrue(os.path.samefile(project, vibe_project["path"]))
                self.assertTrue(os.path.samefile(project, chats[0]["project_path"]))
            finally:
                asyncio.run(service.close())

    def test_submit_task_start_chat_is_durable_idempotent_and_reconciles_completion(self) -> None:
        async def scenario() -> tuple[dict, dict, dict, dict, list[dict], dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "Project"
                project.mkdir()
                sessions = root / ".codex" / "sessions"
                sessions.mkdir(parents=True)
                transcript = sessions / "rollout-thread-existing.jsonl"
                _write_transcript(transcript, "thread-existing", project, [("user", "hello")])
                state_db = root / ".codex" / "state_5.sqlite"
                _create_threads_db(
                    state_db,
                    [
                        {
                            "id": "thread-existing",
                            "rollout_path": str(transcript),
                            "cwd": str(project),
                            "title": "Existing",
                            "preview": "",
                            "created_at_ms": 1779667200000,
                            "updated_at_ms": 1779667200000,
                            "archived": 0,
                        }
                    ],
                )
                service = ToolService(_search_service_config(root, state_db))
                fake = FakeAppServer(service.storage, first_message="operation first")
                service._app_server = fake  # type: ignore[assignment]
                project_id = project_id_for_path(str(project))
                try:
                    submitted = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "do durable work",
                            "client_request_id": "operation-start-1",
                        },
                    )
                    accepted = submitted
                    for _ in range(50):
                        accepted = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                        if accepted.get("turnId"):
                            break
                        await asyncio.sleep(0.01)
                    repeated = await service.call(
                        "codex_submit_task",
                        {
                            "operation_type": "start_chat",
                            "project_id": project_id,
                            "message": "do durable work",
                            "client_request_id": "operation-start-1",
                        },
                    )
                    fake.tracker.record_event(
                        {
                            "method": "thread/tokenUsage/updated",
                            "params": {
                                "threadId": accepted["threadId"],
                                "turnId": accepted["turnId"],
                                "tokenUsage": {
                                    "last": {
                                        "cachedInputTokens": 0,
                                        "inputTokens": 1,
                                        "outputTokens": 2,
                                        "reasoningOutputTokens": 3,
                                        "totalTokens": 6,
                                    },
                                    "total": {
                                        "cachedInputTokens": 0,
                                        "inputTokens": 1,
                                        "outputTokens": 2,
                                        "reasoningOutputTokens": 3,
                                        "totalTokens": 6,
                                    },
                                    "modelContextWindow": 128000,
                                },
                            },
                        },
                        received_at="2026-05-25T00:00:01+00:00",
                    )
                    fake.tracker.record_event(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": accepted["threadId"],
                                "turnId": accepted["turnId"],
                                "status": "completed",
                            },
                        },
                        received_at="2026-05-25T00:00:02+00:00",
                    )
                    completed = service.codex_get_operation_status({"operation_id": submitted["operationId"]})
                    stored = service.storage.get_operation(str(submitted["operationId"])) or {}
                    return submitted, accepted, repeated, completed, fake.turn_start_calls, stored
                finally:
                    await service.close()

        submitted, accepted, repeated, completed, calls, stored = asyncio.run(scenario())

        self.assertEqual("operation-start-1", submitted["clientRequestId"])
        self.assertIn(submitted["status"], {"queued", "starting_app_server", "starting_thread", "running"})
        self.assertEqual("running", accepted["status"])
        self.assertEqual("thread-new", accepted["threadId"])
        self.assertEqual("turn-fake", accepted["turnId"])
        self.assertEqual("durable_queue", accepted["operationSource"])
        self.assertIn(accepted["leaseState"]["state"], {"none", "owned_by_this_worker"})
        self.assertEqual("new", accepted["dedupState"]["state"])
        self.assertEqual(1, accepted["attemptCount"])
        self.assertEqual(["operation first"], [item["text"] for item in accepted["latestMessages"]])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(submitted["operationId"], repeated["operationId"])
        self.assertEqual(1, len(calls))
        self.assertEqual(6, completed["tokenUsage"]["total"]["totalTokens"])
        self.assertTrue(any(item["category"] == "token_usage" for item in completed["progressEvents"]))
        self.assertEqual("completed", completed["status"])
        self.assertEqual("completed", completed["phase"])
        self.assertFalse(completed["pollRecommended"])
        self.assertEqual("completed", stored["status"])

    def test_search_chats_ranks_groups_filters_and_excludes_operational_records(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_a = root / "ProjectA"
            project_b = root / "ProjectB"
            project_a.mkdir()
            project_b.mkdir()
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            transcript_title = sessions / "rollout-title.jsonl"
            transcript_message = sessions / "rollout-message.jsonl"
            transcript_archived = sessions / "rollout-archived.jsonl"
            _write_transcript(transcript_title, "thread-title", project_a, [("user", "ordinary request")])
            _write_transcript(
                transcript_message,
                "thread-message",
                project_a,
                [("user", "please investigate alpha beta in logs"), ("assistant", "alpha beta was found")],
                extra_rows=[
                    {
                        "timestamp": "2026-05-25T00:00:08Z",
                        "type": "response_item",
                        "payload": {"type": "function_call", "name": "shell", "arguments": "secret tool phrase"},
                    }
                ],
            )
            _write_transcript(transcript_archived, "thread-archived", project_b, [("user", "alpha beta archived note")])
            state_db = root / ".codex" / "state_5.sqlite"
            _create_threads_db(
                state_db,
                [
                    {
                        "id": "thread-title",
                        "rollout_path": str(transcript_title),
                        "cwd": str(project_a),
                        "title": "Alpha Beta title match",
                        "preview": "metadata preview",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667300000,
                        "archived": 0,
                    },
                    {
                        "id": "thread-message",
                        "rollout_path": str(transcript_message),
                        "cwd": str(project_a),
                        "title": "Message only",
                        "preview": "other preview",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667250000,
                        "archived": 0,
                    },
                    {
                        "id": "thread-archived",
                        "rollout_path": str(transcript_archived),
                        "cwd": str(project_b),
                        "title": "Archived",
                        "preview": "alpha beta archived",
                        "created_at_ms": 1779667200000,
                        "updated_at_ms": 1779667350000,
                        "archived": 1,
                    },
                ],
            )
            service = ToolService(_search_service_config(root, state_db))
            try:
                result = service.codex_search_chats({"query": "alpha beta", "match_mode": "all_terms", "limit": 10})
                archived = service.codex_search_chats({"query": "alpha beta", "include_archived": True, "limit": 10})
                project_b_result = service.codex_search_chats(
                    {
                        "query": "alpha beta",
                        "include_archived": True,
                        "project_id": project_id_for_path(str(project_b)),
                        "limit": 10,
                    }
                )
                tool_only = service.codex_search_chats({"query": "secret tool phrase", "limit": 10})
            finally:
                asyncio.run(service.close())

            self.assertEqual(2, result["total_results"])
            self.assertEqual("thread-title", result["results"][0]["thread_id"])
            self.assertEqual("thread-message", result["results"][1]["thread_id"])
            self.assertEqual(["message"], result["results"][1]["matched_fields"])
            self.assertEqual(3, archived["total_results"])
            self.assertEqual(["thread-archived"], [item["thread_id"] for item in project_b_result["results"]])
            self.assertEqual(0, tool_only["total_results"])
            self.assertEqual(0, result["budget"]["deepseek_calls"])

    def test_search_chats_limit_total_and_transcript_invalidation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Project"
            project.mkdir()
            sessions = root / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            state_rows = []
            for index in range(12):
                thread_id = f"thread-{index:02d}"
                transcript = sessions / f"rollout-{thread_id}.jsonl"
                _write_transcript(transcript, thread_id, project, [("user", f"needle before message {index}")])
                state_rows.append(
                    {
                        "id": thread_id,
                        "rollout_path": str(transcript),
                        "cwd": str(project),
                        "title": f"Chat {index}",
                        "preview": "",
                        "created_at_ms": 1779667200000 + index,
                        "updated_at_ms": 1779667200000 + index,
                        "archived": 0,
                    }
                )
            state_db = root / ".codex" / "state_5.sqlite"
            _create_threads_db(state_db, state_rows)
            config = _search_service_config(root, state_db)
            service = ToolService(config)
            try:
                first = service.codex_search_chats({"query": "needle before", "limit": 10})
                changed_transcript = sessions / "rollout-thread-00.jsonl"
                _write_transcript(changed_transcript, "thread-00", project, [("user", "needle after replacement")])
                second = service.codex_search_chats({"query": "needle after", "limit": 10})
            finally:
                asyncio.run(service.close())

            self.assertEqual(12, first["total_results"])
            self.assertEqual(10, first["returned_count"])
            self.assertEqual("10", first["next_cursor"])
            self.assertGreaterEqual(second["total_results"], 1)
            self.assertEqual("thread-00", second["results"][0]["thread_id"])
            self.assertGreaterEqual(second["index_status"]["indexed_files"], 1)

    def test_kb_history_drives_project_search_status_and_get_chat(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            kb_root = root / "kb_history" / "projects"
            _write_kb_turn(
                kb_root,
                project,
                "kb-thread",
                "turn-old",
                [("user", "old kb user alpha"), ("assistant", "old kb assistant")],
                created_at="2026-05-25T00:00:00Z",
                updated_at="2026-05-25T00:00:10Z",
            )
            _write_kb_turn(
                kb_root,
                project,
                "kb-thread",
                "turn-new",
                [("user", "latest kb user needle"), ("assistant", "latest kb assistant needle")],
                created_at="2026-05-25T00:01:00Z",
                updated_at="2026-05-25T00:01:10Z",
            )
            config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
            service = ToolService(config)
            try:
                projects = service.codex_list_projects()["projects"]
                search = service.codex_search_chats({"query": "needle", "limit": 10})
                status = service.codex_get_chat_status({"chat_id": "kb-thread"})
                chat = service.codex_get_chat({"chat_id": "kb-thread", "range": {"mode": "all"}, "format": "structured"})
                turn_status = service.codex_get_turn_status({"turn_id": "turn-new", "thread_id": "kb-thread"})
            finally:
                asyncio.run(service.close())

            self.assertIn(path_key(project), [path_key(item["path"]) for item in projects])
            self.assertEqual("chat_search_fts_index", search["source"])
            self.assertEqual(1, search["total_results"])
            self.assertEqual("kb-thread", search["results"][0]["thread_id"])
            self.assertEqual("kb_history", status["transcript"]["source"])
            self.assertEqual("latest kb user needle", status["last_user_preview"])
            self.assertEqual("latest kb assistant needle", status["last_assistant_preview"])
            self.assertTrue(chat["source"].startswith("kb_history+"))
            self.assertEqual(["latest kb user needle", "latest kb assistant needle"], [item["text"] for item in chat["messages"]])
            self.assertEqual("kb_history", turn_status["source"])
            self.assertEqual("completed", turn_status["status"])
            self.assertEqual(["latest kb assistant needle"], [item["text"] for item in turn_status["last_messages"]])

    def test_summary_cache_and_rolling_summary_storage(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "state.sqlite3")
            storage.connect()
            try:
                summary = {
                    "mode": "before_latest_user",
                    "status": "ok",
                    "text": "cached summary",
                    "deepseek_calls": 1,
                    "estimated_chars_sent_to_deepseek": 500,
                }
                storage.upsert_summary_cache(
                    {
                        "cache_key": "cache-1",
                        "thread_id": "thread-1",
                        "transcript_path": "rollout-thread-1.jsonl",
                        "transcript_size": 100,
                        "transcript_mtime_ns": 200,
                        "boundary_line": 10,
                        "model": "deepseek-v4-flash",
                        "filter_version": "test",
                        "summary_json": json.dumps(summary),
                        "created_at": "2026-05-25T00:00:00+00:00",
                        "last_used_at": "2026-05-25T00:00:00+00:00",
                    }
                )
                storage.upsert_rolling_summary(
                    {
                        "thread_id": "thread-1",
                        "transcript_path": "rollout-thread-1.jsonl",
                        "source_line_end": 10,
                        "summary_text": "rolling summary",
                        "model": "deepseek-v4-flash",
                        "updated_at": "2026-05-25T00:01:00+00:00",
                    }
                )

                cached = storage.get_summary_cache("cache-1", "2026-05-25T00:02:00+00:00")
                rolling = storage.get_rolling_summary("thread-1", "rollout-thread-1.jsonl", 20)

                self.assertIsNotNone(cached)
                self.assertEqual("cached summary", cached["text"])
                self.assertTrue(storage.has_summary_cache_for_thread("thread-1"))
                self.assertIsNotNone(rolling)
                self.assertEqual("rolling summary", rolling["summary_text"])
            finally:
                storage.close()

    def test_get_chat_returns_summary_and_latest_raw_tail(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            codex_home = root / ".codex"
            sessions = codex_home / "sessions"
            archived = codex_home / "archived_sessions"
            sessions.mkdir(parents=True)
            archived.mkdir(parents=True)
            transcript = sessions / "rollout-thread-1.jsonl"
            rows = [
                {"timestamp": "2026-05-25T00:00:00Z", "type": "session_meta", "payload": {"id": "thread-1", "cwd": str(project)}},
                {"timestamp": "2026-05-25T00:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1"}},
                {"timestamp": "2026-05-25T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "old user"}},
                {
                    "timestamp": "2026-05-25T00:00:03Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": "old assistant"},
                },
                {"timestamp": "2026-05-25T00:00:04Z", "type": "turn_context", "payload": {"turn_id": "turn-2"}},
                {"timestamp": "2026-05-25T00:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "latest user"}},
                {
                    "timestamp": "2026-05-25T00:00:06Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": "latest assistant"},
                },
                {"timestamp": "2026-05-25T00:00:06Z", "type": "event_msg", "payload": {"type": "context_compacted"}},
                {"timestamp": "2026-05-25T00:00:07Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-2"}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            config = ServerConfig(
                codex_home=codex_home,
                sessions_dir=sessions,
                archived_sessions_dir=archived,
                codex_state_db=codex_home / "state_5.sqlite",
                codex_logs_db=codex_home / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[root],
                deepseek_env_path=root / "missing.env",
            )
            service = ToolService(config)
            try:
                service.storage.upsert_tracked_plan_item(
                    {
                        "item_id": "plan-chat",
                        "turn_id": "turn-2",
                        "thread_id": "thread-1",
                        "status": "completed",
                        "text": "Chat plan",
                        "created_at": "2026-05-25T00:00:05+00:00",
                        "updated_at": "2026-05-25T00:00:06+00:00",
                        "completed_at": "2026-05-25T00:00:06+00:00",
                        "sequence": 1,
                        "payload_json": "{}",
                    }
                )
                result = service.codex_get_chat({"chat_id": "thread-1", "range": {"mode": "all"}, "format": "structured"})
            finally:
                asyncio.run(service.close())

            self.assertEqual("skipped_small_history", result["history_summary"]["status"])
            self.assertEqual(2, result["history_summary"]["messages_summarized"])
            self.assertEqual(0, result["history_summary"]["deepseek_calls"])
            self.assertEqual(["latest user", "latest assistant"], [item["text"] for item in result["messages"]])
            self.assertEqual(["user", "assistant"], [item["role"] for item in result["messages"]])
            self.assertNotIn("old user", [item["text"] for item in result["messages"]])
            self.assertEqual("Chat plan", result["latestPlan"]["markdown"])
            self.assertIn("budget", result)

    def test_get_chat_tail_cap_and_items_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            codex_home = root / ".codex"
            sessions = codex_home / "sessions"
            archived = codex_home / "archived_sessions"
            sessions.mkdir(parents=True)
            archived.mkdir(parents=True)
            transcript = sessions / "rollout-thread-tail.jsonl"
            rows = [
                {"timestamp": "2026-05-25T00:00:00Z", "type": "session_meta", "payload": {"id": "thread-tail", "cwd": str(project)}},
                {"timestamp": "2026-05-25T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "old user"}},
                {"timestamp": "2026-05-25T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "old assistant"}},
                {"timestamp": "2026-05-25T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "latest user"}},
                {"timestamp": "2026-05-25T00:00:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "tail 1"}},
                {"timestamp": "2026-05-25T00:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "tail 2"}},
                {"timestamp": "2026-05-25T00:00:06Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "tail 3"}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            config = ServerConfig(
                codex_home=codex_home,
                sessions_dir=sessions,
                archived_sessions_dir=archived,
                codex_state_db=codex_home / "state_5.sqlite",
                codex_logs_db=codex_home / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[root],
                deepseek_env_path=root / "missing.env",
            )
            service = ToolService(config)
            try:
                result = service.codex_get_chat(
                    {
                        "chat_id": "thread-tail",
                        "range": {"mode": "all"},
                        "format": "structured",
                        "tail_max_messages": 2,
                    }
                )
            finally:
                asyncio.run(service.close())

            self.assertTrue(result["tail_truncated"])
            self.assertEqual(2, len(result["messages"]))
            self.assertEqual(["tail 2", "tail 3"], [item["text"] for item in result["messages"]])
            self.assertTrue(all(item["items"] == [] for item in result["messages"]))
            self.assertTrue(all(item["items_available"] for item in result["messages"]))

    def test_get_chat_status_is_lightweight(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "OpenClaw"
            project.mkdir()
            codex_home = root / ".codex"
            sessions = codex_home / "sessions"
            archived = codex_home / "archived_sessions"
            sessions.mkdir(parents=True)
            archived.mkdir(parents=True)
            transcript = sessions / "rollout-thread-status.jsonl"
            rows = [
                {"timestamp": "2026-05-25T00:00:00Z", "type": "session_meta", "payload": {"id": "thread-status", "cwd": str(project)}},
                {"timestamp": "2026-05-25T00:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1"}},
                {"timestamp": "2026-05-25T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "status user"}},
                {"timestamp": "2026-05-25T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "status assistant"}},
            ]
            transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            config = ServerConfig(
                codex_home=codex_home,
                sessions_dir=sessions,
                archived_sessions_dir=archived,
                codex_state_db=codex_home / "state_5.sqlite",
                codex_logs_db=codex_home / "logs_2.sqlite",
                projects_root=root,
                projects_registry_path=root / "projects.json",
                codex_binary_path=root / "codex.exe",
                state_db_path=root / "mcp.sqlite",
                allowed_roots=[root],
                deepseek_env_path=root / "missing.env",
            )
            service = ToolService(config)
            try:
                result = service.codex_get_chat_status({"chat_id": "thread-status"})
            finally:
                asyncio.run(service.close())

            self.assertEqual("thread-status", result["thread_id"])
            self.assertEqual("status user", result["last_user_preview"])
            self.assertEqual("status assistant", result["last_assistant_preview"])
            self.assertNotIn("messages", result)
            self.assertNotIn("history_summary", result)
            self.assertEqual(0, result["budget"]["deepseek_calls"])

    def test_split_selected_messages_expands_tail_to_previous_user(self) -> None:
        from openclaw_codex_mcp.models import TranscriptMessage

        def message(role: str, text: str, line: int) -> TranscriptMessage:
            return TranscriptMessage(
                message_id=str(line),
                thread_id="thread-1",
                turn_id="turn-1",
                role=role,
                created_at=None,
                text=text,
                items=[],
                metadata={},
                source_line_start=line,
                source_line_end=line,
            )

        messages = [
            message("user", "old", 1),
            message("assistant", "old answer", 2),
            message("user", "latest", 3),
            message("assistant", "tail 1", 4),
            message("tool", "tail 2", 5),
        ]

        split, expanded = _split_selected_messages(messages, messages[-2:])

        self.assertTrue(expanded)
        self.assertEqual(["old", "old answer"], [item.text for item in split.upper])
        self.assertEqual(["latest", "tail 1", "tail 2"], [item.text for item in split.lower])
