from __future__ import annotations

from tests.helpers import *


class OperationLeaseStorageTests(unittest.TestCase):
    def test_workflow_state_defaults_and_operation_links_update(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                now = "2026-05-25T00:00:00+00:00"
                storage.create_workflow(
                    {
                        "workflow_id": "wf-storage",
                        "client_request_id": None,
                        "project_id": "project",
                        "thread_id": "",
                        "plan_turn_id": "",
                        "phase": "planning",
                        "status": "planning",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                created = storage.get_workflow("wf-storage") or {}
                storage.update_workflow(
                    "wf-storage",
                    current_operation_id="op-plan",
                    plan_operation_id="op-plan",
                    review_operation_id="op-review",
                    review_source_thread_id="thread-source",
                    review_thread_id="thread-review",
                    review_turn_id="turn-review",
                    review_target_json='{"type":"uncommittedChanges"}',
                    review_delivery="detached",
                    latest_plan_item_id="plan-1",
                    latest_plan_hash="hash-plan",
                    final_report_json='{"text":"done"}',
                    latest_report_hash="hash-report",
                    updated_at="2026-05-25T00:00:01+00:00",
                )
                updated = storage.get_workflow("wf-storage") or {}
            finally:
                storage.close()

        self.assertEqual("plan_then_execute", created["workflow_kind"])
        self.assertEqual("", created["thread_id"])
        self.assertIsNone(created["current_operation_id"])
        self.assertIsNone(created["review_operation_id"])
        self.assertIsNone(created["review_source_thread_id"])
        self.assertIsNone(created["review_thread_id"])
        self.assertIsNone(created["review_turn_id"])
        self.assertIsNone(created["goal_objective"])
        self.assertEqual("clear", created["goal_completion_action"])
        self.assertEqual("not_configured", created["goal_sync_state"])
        self.assertEqual("op-plan", updated["current_operation_id"])
        self.assertEqual("op-plan", updated["plan_operation_id"])
        self.assertEqual("op-review", updated["review_operation_id"])
        self.assertEqual("thread-source", updated["review_source_thread_id"])
        self.assertEqual("thread-review", updated["review_thread_id"])
        self.assertEqual("turn-review", updated["review_turn_id"])
        self.assertEqual('{"type":"uncommittedChanges"}', updated["review_target_json"])
        self.assertEqual("detached", updated["review_delivery"])
        self.assertEqual("plan-1", updated["latest_plan_item_id"])
        self.assertEqual("hash-plan", updated["latest_plan_hash"])
        self.assertEqual("hash-report", updated["latest_report_hash"])
        self.assertEqual('{"text":"done"}', updated["final_report_json"])

    def test_thread_lifecycle_action_storage_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                row = {
                    "action_id": "tla-storage",
                    "action_type": "compact",
                    "thread_id": "thread-storage",
                    "project_id": "project-storage",
                    "status": "running",
                    "created_at": "2026-05-25T00:00:00+00:00",
                    "updated_at": "2026-05-25T00:00:00+00:00",
                    "request_json": '{"thread_id":"thread-storage"}',
                    "app_server_generation": 3,
                }
                created = storage.create_thread_lifecycle_action(row)
                duplicate = storage.create_thread_lifecycle_action(row)
                storage.update_thread_lifecycle_action(
                    "tla-storage",
                    status="completed",
                    updated_at="2026-05-25T00:00:05+00:00",
                    completed_at="2026-05-25T00:00:05+00:00",
                    result_json='{"done":true}',
                    observed_event_id=17,
                    target_turn_id="turn-storage",
                )
                fetched = storage.get_thread_lifecycle_action("tla-storage") or {}
                listed = storage.list_thread_lifecycle_actions(thread_id="thread-storage")
            finally:
                storage.close()

        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual("completed", fetched["status"])
        self.assertEqual(17, fetched["observed_event_id"])
        self.assertEqual("turn-storage", fetched["target_turn_id"])
        self.assertEqual(["tla-storage"], [item["action_id"] for item in listed])

    def test_operation_lease_acquire_release_and_expired_pickup(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                storage.create_operation(_storage_operation_row("op-lease"))
                now = "2026-05-25T00:00:00+00:00"
                first = storage.acquire_operation_lease(
                    "op-lease",
                    lease_owner="worker-1",
                    now=now,
                    lease_expires_at="2026-05-25T00:02:00+00:00",
                )
                blocked = storage.acquire_operation_lease(
                    "op-lease",
                    lease_owner="worker-2",
                    now="2026-05-25T00:00:30+00:00",
                    lease_expires_at="2026-05-25T00:02:30+00:00",
                )
                heartbeat = storage.heartbeat_operation_lease(
                    "op-lease",
                    lease_owner="worker-1",
                    now="2026-05-25T00:01:00+00:00",
                    lease_expires_at="2026-05-25T00:03:00+00:00",
                )
                storage.release_operation_lease("op-lease", lease_owner="worker-1", updated_at="2026-05-25T00:01:01+00:00")
                second = storage.acquire_operation_lease(
                    "op-lease",
                    lease_owner="worker-2",
                    now="2026-05-25T00:01:02+00:00",
                    lease_expires_at="2026-05-25T00:03:02+00:00",
                )
            finally:
                storage.close()

        self.assertIsNotNone(first)
        self.assertEqual("worker-1", first["lease_owner"])
        self.assertIsNone(blocked)
        self.assertTrue(heartbeat)
        self.assertIsNotNone(second)
        self.assertEqual("worker-2", second["lease_owner"])

    def test_operation_lease_rejects_config_fingerprint_mismatch(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                row = _storage_operation_row("op-fingerprint")
                row["submitter_config_fingerprint"] = "submitter-a"
                storage.create_operation(row)
                acquired = storage.acquire_operation_lease(
                    "op-fingerprint",
                    lease_owner="worker-b",
                    now="2026-05-25T00:00:00+00:00",
                    lease_expires_at="2026-05-25T00:02:00+00:00",
                    worker_config_fingerprint="worker-b",
                )
                listed = storage.list_startable_operations(
                    now="2026-05-25T00:00:00+00:00",
                    worker_config_fingerprint="worker-b",
                )
                emergency = storage.acquire_operation_lease(
                    "op-fingerprint",
                    lease_owner="worker-b",
                    now="2026-05-25T00:00:01+00:00",
                    lease_expires_at="2026-05-25T00:02:01+00:00",
                    worker_config_fingerprint="worker-b",
                    allow_cross_config_recovery=True,
                )
            finally:
                storage.close()

        self.assertIsNone(acquired)
        self.assertEqual([], [item["operation_id"] for item in listed])
        self.assertIsNotNone(emergency)
        self.assertEqual("worker-b", emergency["lease_owner"])

    def test_progress_events_are_idempotent_and_summarized(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                storage.upsert_tracked_turn(
                    {
                        "turn_id": "turn-progress",
                        "thread_id": "thread-progress",
                        "chat_id": "thread-progress",
                        "project_id": "project",
                        "project_path": str(Path(tmp)),
                        "status": "ready",
                        "started_at": "2026-05-25T00:00:00+00:00",
                        "updated_at": "2026-05-25T00:00:00+00:00",
                        "completed_at": None,
                        "first_message_at": None,
                        "final_message": None,
                        "last_error": None,
                        "source": "app_server",
                    }
                )
                row = {
                    "event_hash": "progress-hash",
                    "turn_id": "turn-progress",
                    "thread_id": "thread-progress",
                    "event_type": "warning",
                    "category": "warning",
                    "severity": "warning",
                    "item_id": None,
                    "sequence": 0,
                    "text": "safe warning",
                    "metadata_json": "{}",
                    "created_at": "2026-05-25T00:00:01+00:00",
                    "truncated": 0,
                }
                first = storage.record_tracked_turn_progress_event(row)
                duplicate = storage.record_tracked_turn_progress_event(row)
                storage.record_tracked_turn_progress_event(
                    {
                        **row,
                        "event_hash": "token-hash",
                        "event_type": "thread/tokenUsage/updated",
                        "category": "token_usage",
                        "severity": "info",
                        "text": "Token usage updated.",
                        "metadata_json": json.dumps({"tokenUsage": {"total": {"totalTokens": 42}}}),
                        "created_at": "2026-05-25T00:00:02+00:00",
                    }
                )
                events = storage.list_tracked_turn_progress_events(turn_id="turn-progress", limit=10)
                summary = storage.tracked_turn_progress_summary("turn-progress")
            finally:
                storage.close()

        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertEqual(2, len(events))
        self.assertEqual(2, summary["eventCount"])
        self.assertEqual("token_usage", summary["tokenUsageEvent"]["category"])
        self.assertEqual(1, len(summary["warnings"]))

    def test_turn_tracker_ignores_non_turn_ready_and_finalizes_only_on_turn_completed(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                tracker = TurnTracker(storage)
                tracker.register_turn(
                    turn_id="turn-incident",
                    thread_id="thread-incident",
                    chat_id="thread-incident",
                    project_id="project",
                    project_path=str(Path(tmp)),
                    status="running",
                    started_at="2026-05-25T00:00:00+00:00",
                    user_message="fresh user prompt",
                )
                tracker.record_event(
                    {
                        "method": "mcpServer/startupStatus/updated",
                        "params": {"threadId": "thread-incident", "status": "ready"},
                    },
                    received_at="2026-05-25T00:00:01+00:00",
                )
                tracker.record_event(
                    {
                        "method": "item/created",
                        "params": {
                            "threadId": "thread-incident",
                            "turnId": "turn-incident",
                            "item": {"type": "agentMessage", "text": "intermediate assistant text"},
                        },
                    },
                    received_at="2026-05-25T00:00:02+00:00",
                )
                before_terminal = tracker.get_turn_status("turn-incident", last_messages=10, message_max_chars=8000)
                stored_before = storage.get_tracked_turn("turn-incident") or {}
                tracker.record_event(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "thread-incident", "turnId": "turn-incident"},
                    },
                    received_at="2026-05-25T00:00:03+00:00",
                )
                after_terminal = tracker.get_turn_status("turn-incident", last_messages=10, message_max_chars=8000)
                stored_after = storage.get_tracked_turn("turn-incident") or {}
            finally:
                storage.close()

        self.assertIsNotNone(before_terminal)
        self.assertEqual("first_message_received", before_terminal["status"])
        self.assertFalse(before_terminal["completionObserved"])
        self.assertIsNone(before_terminal["finalMessage"])
        self.assertIsNone(stored_before["final_message"])
        self.assertEqual("intermediate assistant text", stored_before["last_assistant_message"])
        self.assertEqual("completed", after_terminal["status"])
        self.assertTrue(after_terminal["completionObserved"])
        self.assertTrue(after_terminal["terminalEvidence"]["trusted"])
        self.assertEqual("intermediate assistant text", stored_after["final_message"])

    def test_startup_recovery_resets_starting_without_turn_and_preserves_turn(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                storage.create_operation(_storage_operation_row("op-reset", status="starting_thread"))
                storage.create_operation(
                    _storage_operation_row("op-running", status="starting_turn", thread_id="thread-r", turn_id="turn-r")
                )
                storage.update_operation(
                    "op-reset",
                    lease_owner="old-worker",
                    lease_expires_at="2026-05-25T00:00:00+00:00",
                    updated_at="2026-05-25T00:00:00+00:00",
                )
                recovered = storage.recover_startup_operations(now="2026-05-25T00:05:00+00:00")
                reset = storage.get_operation("op-reset") or {}
                running = storage.get_operation("op-running") or {}
            finally:
                storage.close()

        self.assertEqual(["op-reset"], recovered["resetOperationIds"])
        self.assertEqual(["op-running"], recovered["runningOperationIds"])
        self.assertEqual("queued", reset["status"])
        self.assertIsNone(reset["lease_owner"])
        self.assertEqual("running", running["status"])
        self.assertEqual("turn-r", running["turn_id"])

    def test_cleanup_prompt_submissions_deletes_only_old_terminal_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = McpStorage(Path(tmp) / "mcp.sqlite")
            storage.connect()
            try:
                old = "2026-05-01T00:00:00+00:00"
                fresh = "2026-05-25T00:00:00+00:00"
                for prompt_id, status, updated_at in [
                    ("ps-old-done", "completed", old),
                    ("ps-old-active", "queued", old),
                    ("ps-fresh-done", "completed", fresh),
                ]:
                    storage.create_prompt_submission(
                        {
                            "prompt_submission_id": prompt_id,
                            "project_id": "project-test",
                            "project_path_key": "project-test",
                            "operation_type": "start_chat",
                            "prompt_hash": prompt_id,
                            "prompt_normalized": f"normalized {prompt_id}",
                            "prompt_preview": "preview",
                            "operation_id": None,
                            "chat_id": None,
                            "thread_id": None,
                            "turn_id": None,
                            "workflow_id": None,
                            "status": status,
                            "duplicate_of_submission_id": None,
                            "similarity": None,
                            "created_at": updated_at,
                            "updated_at": updated_at,
                        }
                    )
                dry = storage.cleanup_prompt_submissions(older_than="2026-05-10T00:00:00+00:00", dry_run=True)
                real = storage.cleanup_prompt_submissions(older_than="2026-05-10T00:00:00+00:00", dry_run=False)
                remaining = storage.list_prompt_submissions_for_project("project-test", limit=10)
            finally:
                storage.close()

        self.assertEqual(1, dry["matchedPromptSubmissions"])
        self.assertEqual(0, dry["deletedPromptSubmissions"])
        self.assertEqual(1, real["deletedPromptSubmissions"])
        self.assertEqual({"ps-old-active", "ps-fresh-done"}, {row["prompt_submission_id"] for row in remaining})
