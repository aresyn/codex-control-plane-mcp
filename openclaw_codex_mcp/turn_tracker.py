from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from .config import path_key
from .pending_interactions import interaction_row_to_tool
from .statuses import TURN_ACTIVE_STATUSES, TURN_COMPLETION_OBSERVED_STATUSES, TURN_TERMINAL_STATUSES
from .storage import McpStorage


ACTIVE_STATUSES = TURN_ACTIVE_STATUSES
COMPLETION_OBSERVED_STATUSES = TURN_COMPLETION_OBSERVED_STATUSES
TERMINAL_STATUSES = TURN_TERMINAL_STATUSES
DEFAULT_HOOK_JOURNAL_TEXT_LIMIT = 20_000
DEFAULT_PROGRESS_TEXT_LIMIT = 20_000
WAITING_FOR_OPENCLAW_ERROR = "Waiting for OpenClaw response."
PROGRESS_EVENT_METHODS = {
    "item/agentMessage/delta",
    "item/plan/delta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/summaryTextDelta",
    "turn/diff/updated",
    "thread/tokenUsage/updated",
    "model/rerouted",
    "warning",
    "configWarning",
    "guardianWarning",
}
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", re.IGNORECASE),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*([^\s]+)"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurnTracker:
    def __init__(
        self,
        storage: McpStorage,
        *,
        hook_history_enabled: bool = True,
        hook_history_max_text_chars: int = DEFAULT_HOOK_JOURNAL_TEXT_LIMIT,
    ) -> None:
        self.storage = storage
        self.hook_history_enabled = hook_history_enabled
        self.hook_history_max_text_chars = max(1000, hook_history_max_text_chars)
        self._first_message_waiters: dict[str, list[asyncio.Future[dict[str, Any] | None]]] = {}
        self._thread_active_turn: dict[str, str] = {}

    def register_turn(
        self,
        *,
        turn_id: str,
        thread_id: str,
        chat_id: str | None,
        project_id: str | None,
        project_path: str | None,
        status: str = "running",
        started_at: str | None = None,
        user_message: str | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
        request_id: str | None = None,
        process_generation: int | None = None,
    ) -> dict[str, Any]:
        current = self.storage.get_tracked_turn(turn_id)
        effective_status = str((current or {}).get("status") or status)
        if effective_status not in TERMINAL_STATUSES:
            effective_status = status
        timestamp = started_at or str((current or {}).get("started_at") or now_iso())
        self.storage.upsert_tracked_turn(
            {
                "turn_id": turn_id,
                "thread_id": thread_id,
                "chat_id": chat_id,
                "project_id": project_id,
                "project_path": project_path,
                "status": effective_status,
                "started_at": timestamp,
                "updated_at": now_iso(),
                "completed_at": (current or {}).get("completed_at"),
                "first_message_at": (current or {}).get("first_message_at"),
                "final_message": (current or {}).get("final_message"),
                "last_assistant_message": (current or {}).get("last_assistant_message"),
                "last_error": (current or {}).get("last_error"),
                "source": "app_server",
                "accepted_at": (current or {}).get("accepted_at") or timestamp,
                "request_id": request_id or (current or {}).get("request_id"),
                "process_generation": process_generation if process_generation is not None else (current or {}).get("process_generation"),
                "last_event_seq": int((current or {}).get("last_event_seq") or 0),
            }
        )
        self._mirror_turn_started(
            turn_id=turn_id,
            thread_id=thread_id,
            project_path=project_path,
            status=effective_status,
            timestamp=timestamp,
            user_message=user_message,
            model=model,
            permission_mode=permission_mode,
        )
        if effective_status in ACTIVE_STATUSES:
            self._thread_active_turn[thread_id] = turn_id
        return self.storage.get_tracked_turn(turn_id) or {}

    def record_event(self, payload: dict[str, Any], *, received_at: str | None = None) -> None:
        received_at = received_at or now_iso()
        thread_id = _extract_thread_id(payload)
        turn_id = _extract_turn_id(payload)
        if turn_id is None and thread_id is not None:
            turn_id = self._thread_active_turn.get(thread_id)
        if turn_id is None:
            return
        if thread_id is None:
            current = self.storage.get_tracked_turn(turn_id)
            thread_id = str((current or {}).get("thread_id") or "")
        if not thread_id:
            return

        current = self.storage.get_tracked_turn(turn_id)
        if current is None:
            self.storage.upsert_tracked_turn(
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "chat_id": None,
                    "project_id": None,
                    "project_path": None,
                    "status": "running",
                    "started_at": received_at,
                    "updated_at": received_at,
                    "completed_at": None,
                    "first_message_at": None,
                    "final_message": None,
                    "last_error": None,
                    "source": "app_server",
                }
            )
            current = self.storage.get_tracked_turn(turn_id)

        self._record_plan_event(payload, thread_id=thread_id, turn_id=turn_id, received_at=received_at)
        self._record_progress_event(payload, thread_id=thread_id, turn_id=turn_id, received_at=received_at)

        message = _assistant_message(payload, turn_id=turn_id, thread_id=thread_id, received_at=received_at)
        if message is not None:
            inserted = self.storage.record_tracked_turn_message(message)
            if inserted:
                self._mirror_assistant_message(message, received_at=received_at)
            if inserted:
                current_status = str((current or {}).get("status") or "")
                if current_status in {"accepted", "started", "running", ""}:
                    self.storage.update_tracked_turn_status(
                        turn_id,
                        status="first_message_received",
                        updated_at=received_at,
                    )
                self._resolve_first_message(turn_id, message)

        status = _event_status(payload)
        if status:
            current_after_message = self.storage.get_tracked_turn(turn_id) or current or {}
            final_message = None
            if status in TERMINAL_STATUSES:
                final_message = (
                    message["text"]
                    if message is not None
                    else str(current_after_message.get("last_assistant_message") or "")
                )
                final_message = final_message if final_message.strip() else None
            failure_status = status in {"failed", "aborted", "cancelled", "canceled"}
            last_error = _event_error(payload) if failure_status else None
            self.storage.update_tracked_turn_status(
                turn_id,
                status=status,
                updated_at=received_at,
                completed_at=received_at if status in TERMINAL_STATUSES else None,
                final_message=final_message,
                last_assistant_message=(message["text"] if message is not None else None),
                last_error=last_error,
                clear_last_error=status in TERMINAL_STATUSES and not failure_status,
            )
            self._mirror_turn_status(
                turn_id=turn_id,
                thread_id=thread_id,
                status=status,
                updated_at=received_at,
                completed_at=received_at if status in TERMINAL_STATUSES else None,
                final_message=final_message,
                last_error=last_error,
                clear_last_error=status in TERMINAL_STATUSES and not failure_status,
            )
            if status in TERMINAL_STATUSES:
                if thread_id in self._thread_active_turn and self._thread_active_turn[thread_id] == turn_id:
                    self._thread_active_turn.pop(thread_id, None)
                self._resolve_first_message(turn_id, None)

    def record_thread_snapshot(self, payload: Any, *, received_at: str | None = None) -> None:
        received_at = received_at or now_iso()
        if not isinstance(payload, dict):
            return
        thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else payload
        if not isinstance(thread, dict):
            return
        thread_id = str(thread.get("id") or thread.get("threadId") or "")
        if not thread_id:
            return
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return
        for raw_turn in turns:
            if not isinstance(raw_turn, dict):
                continue
            turn_id = str(raw_turn.get("id") or raw_turn.get("turnId") or "")
            if not turn_id:
                continue
            status = _normalize_status(raw_turn.get("status") or "unknown")
            current = self.storage.get_tracked_turn(turn_id)
            self.storage.upsert_tracked_turn(
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "chat_id": thread_id,
                    "project_id": (current or {}).get("project_id"),
                    "project_path": (current or {}).get("project_path"),
                    "status": status,
                    "started_at": _ms_to_iso(raw_turn.get("startedAt")) or (current or {}).get("started_at") or received_at,
                    "updated_at": received_at,
                    "completed_at": _ms_to_iso(raw_turn.get("completedAt")) or (current or {}).get("completed_at"),
                    "first_message_at": (current or {}).get("first_message_at"),
                    "final_message": (current or {}).get("final_message"),
                    "last_assistant_message": (current or {}).get("last_assistant_message"),
                    "last_error": _snapshot_turn_error(raw_turn) or (current or {}).get("last_error"),
                    "clear_last_error": status in TERMINAL_STATUSES and _snapshot_turn_error(raw_turn) is None,
                    "source": "app_server",
                    "accepted_at": (current or {}).get("accepted_at") or received_at,
                    "request_id": (current or {}).get("request_id"),
                    "process_generation": (current or {}).get("process_generation"),
                    "last_event_seq": int((current or {}).get("last_event_seq") or 0),
                }
            )
            self._mirror_turn_status(
                turn_id=turn_id,
                thread_id=thread_id,
                status=status,
                updated_at=received_at,
                completed_at=_ms_to_iso(raw_turn.get("completedAt")) or (current or {}).get("completed_at"),
                final_message=(current or {}).get("final_message"),
                last_error=_snapshot_turn_error(raw_turn) or (current or {}).get("last_error"),
                clear_last_error=status in TERMINAL_STATUSES and _snapshot_turn_error(raw_turn) is None,
            )
            items = raw_turn.get("items")
            if not isinstance(items, list):
                continue
            for raw_item in items:
                if not isinstance(raw_item, dict) or raw_item.get("type") != "plan":
                    continue
                text = raw_item.get("text")
                if not isinstance(text, str):
                    continue
                item_id = str(raw_item.get("id") or f"{turn_id}-plan")
                self.storage.upsert_tracked_plan_item(
                    {
                        "item_id": item_id,
                        "turn_id": turn_id,
                        "thread_id": thread_id,
                        "status": "completed" if status in TERMINAL_STATUSES else "in_progress",
                        "text": text,
                        "created_at": _ms_to_iso(raw_turn.get("startedAt")) or received_at,
                        "updated_at": received_at,
                        "completed_at": _ms_to_iso(raw_turn.get("completedAt")) if status in TERMINAL_STATUSES else None,
                        "sequence": 0,
                        "payload_json": json.dumps(raw_item, ensure_ascii=False),
                    }
                )

    async def wait_first_message(self, turn_id: str, timeout_seconds: int) -> tuple[dict[str, Any] | None, bool]:
        messages = self.storage.get_last_tracked_turn_messages(turn_id, 1)
        if messages:
            return messages[-1], False
        current = self.storage.get_tracked_turn(turn_id)
        if current and str(current.get("status") or "") in TERMINAL_STATUSES:
            return None, False

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any] | None] = loop.create_future()
        self._first_message_waiters.setdefault(turn_id, []).append(future)
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds), False
        except asyncio.TimeoutError:
            return None, True
        finally:
            waiters = self._first_message_waiters.get(turn_id)
            if waiters is not None:
                with suppress(ValueError):
                    waiters.remove(future)
                if not waiters:
                    self._first_message_waiters.pop(turn_id, None)

    def get_turn_status(
        self,
        turn_id: str,
        *,
        last_messages: int,
        message_max_chars: int,
        progress_events: int = 10,
        progress_max_chars: int = 2000,
    ) -> dict[str, Any] | None:
        turn = self.storage.get_tracked_turn(turn_id)
        if turn is None:
            return None
        messages = [
            _message_to_tool(message, message_max_chars)
            for message in self.storage.get_last_tracked_turn_messages(turn_id, last_messages)
        ]
        message_count = self.storage.count_tracked_turn_messages(turn_id)
        final_message = _truncate(turn.get("final_message"), message_max_chars)
        status = _status_with_final_message(turn["status"], final_message)
        completion_observed = status in COMPLETION_OBSERVED_STATUSES
        terminal_evidence = _terminal_evidence(turn, status)
        if not terminal_evidence.get("trusted"):
            final_message = None
        last_error = _visible_last_error(turn)
        pending_interactions = [
            interaction_row_to_tool(row)
            for row in self.storage.list_pending_interactions(turn_id=turn_id, status="pending", limit=20)
        ]
        plans = [_plan_to_tool(row, message_max_chars) for row in self.storage.get_tracked_turn_plans(turn_id)]
        latest_plan = plans[-1] if plans else None
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
            "status": status,
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
            "hasMore": message_count > len(messages),
            "lastEventSeq": int(turn.get("last_event_seq") or 0),
            "requestId": turn.get("request_id"),
            "processGeneration": turn.get("process_generation"),
            "lastError": last_error,
            "final_message": final_message,
            "finalMessage": final_message,
            "plans": plans,
            "latestPlan": latest_plan,
            "pending_interactions": pending_interactions,
            "pendingInteractions": pending_interactions,
            "source": turn.get("source") or "storage",
            "appServerGeneration": turn.get("process_generation"),
            "stalenessSeconds": _staleness_seconds(turn.get("updated_at")),
        }
        result.update(turn_progress_status_fields(self.storage, turn_id, progress_events=progress_events, progress_max_chars=progress_max_chars))
        return result

    def running_turns(self) -> list[dict[str, Any]]:
        return self.storage.get_running_tracked_turns()

    def mark_turn_interrupted(self, turn_id: str, *, reason: str) -> None:
        turn = self.storage.get_tracked_turn(turn_id)
        if turn is None:
            return
        self.storage.update_tracked_turn_status(
            turn_id,
            status="interrupted",
            updated_at=now_iso(),
            completed_at=now_iso(),
            last_error=reason,
        )
        thread_id = str(turn.get("thread_id") or "")
        if thread_id in self._thread_active_turn and self._thread_active_turn[thread_id] == turn_id:
            self._thread_active_turn.pop(thread_id, None)
        self._resolve_first_message(turn_id, None)

    def mark_active_turns_unknown(self, *, process_generation: int | None, reason: str) -> None:
        active_turns = self.storage.get_running_tracked_turns()
        timestamp = now_iso()
        for turn in active_turns:
            if process_generation is not None and turn.get("process_generation") not in (None, process_generation):
                continue
            self.storage.update_tracked_turn_status(
                str(turn["turn_id"]),
                status="unknown_after_app_server_exit",
                updated_at=timestamp,
                completed_at=timestamp,
                last_error=reason,
            )
            thread_id = str(turn.get("thread_id") or "")
            if thread_id in self._thread_active_turn and self._thread_active_turn[thread_id] == turn["turn_id"]:
                self._thread_active_turn.pop(thread_id, None)
            self._resolve_first_message(str(turn["turn_id"]), None)

    def mark_pending_interaction(self, method: str, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId") or "")
        turn_id = str(params.get("turnId") or "")
        if not turn_id and thread_id:
            turn_id = self._thread_active_turn.get(thread_id, "")
        if not turn_id:
            return
        status = "waiting_for_user_input" if method in {"item/tool/requestUserInput", "tool/requestUserInput"} else "waiting_for_approval"
        self.storage.update_tracked_turn_status(
            turn_id,
            status=status,
            updated_at=now_iso(),
            last_error=WAITING_FOR_OPENCLAW_ERROR,
        )

    def mark_interaction_resolved(self, method: str, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId") or "")
        turn_id = str(params.get("turnId") or "")
        if not turn_id and thread_id:
            turn_id = self._thread_active_turn.get(thread_id, "")
        if not turn_id:
            return
        turn = self.storage.get_tracked_turn(turn_id)
        if turn is None:
            return
        if str(turn.get("status") or "") in {"waiting_for_approval", "waiting_for_user_input"}:
            self.storage.update_tracked_turn_status(
                turn_id,
                status="running",
                updated_at=now_iso(),
                clear_last_error=True,
            )

    def mark_approval_auto_declined(self, params: dict[str, Any], *, reason: str) -> None:
        self.mark_pending_interaction("item/commandExecution/requestApproval", params)

    def _record_plan_event(self, payload: dict[str, Any], *, thread_id: str, turn_id: str, received_at: str) -> None:
        method = str(payload.get("method") or "")
        params = _payload_params(payload)
        if method == "item/plan/delta":
            item_id = str(params.get("itemId") or f"{turn_id}-plan")
            delta = params.get("delta")
            if isinstance(delta, str):
                self.storage.append_tracked_plan_delta(
                    {
                        "event_hash": _event_hash(payload),
                        "item_id": item_id,
                        "turn_id": turn_id,
                        "thread_id": thread_id,
                        "event_type": method,
                        "created_at": received_at,
                        "payload_json": json.dumps(payload, ensure_ascii=False),
                        "delta": delta,
                        "sequence": int(params.get("sequence") or 0),
                    }
                )
            return
        if method == "turn/plan/updated":
            plan = params.get("plan")
            item: dict[str, Any] = plan if isinstance(plan, dict) else {}
            text = item.get("text") if item else params.get("text") or params.get("markdown")
            if isinstance(plan, str):
                text = plan
            if isinstance(text, str):
                self.storage.upsert_tracked_plan_item(
                    {
                        "item_id": str(item.get("id") or params.get("itemId") or f"{turn_id}-plan"),
                        "turn_id": turn_id,
                        "thread_id": thread_id,
                        "status": _normalize_status(params.get("status") or item.get("status") or "in_progress"),
                        "text": text,
                        "created_at": received_at,
                        "updated_at": received_at,
                        "completed_at": None,
                        "sequence": int(params.get("sequence") or 0),
                        "payload_json": json.dumps(payload, ensure_ascii=False),
                    }
                )
            return
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "plan":
            return
        text = item.get("text")
        if not isinstance(text, str):
            return
        completed = method == "item/completed"
        self.storage.upsert_tracked_plan_item(
            {
                "item_id": str(item.get("id") or params.get("itemId") or f"{turn_id}-plan"),
                "turn_id": turn_id,
                "thread_id": thread_id,
                "status": "completed" if completed else "in_progress",
                "text": text,
                "created_at": received_at,
                "updated_at": received_at,
                "completed_at": received_at if completed else None,
                "sequence": int(params.get("sequence") or 0),
                "payload_json": json.dumps(payload, ensure_ascii=False),
            }
        )

    def _record_progress_event(self, payload: dict[str, Any], *, thread_id: str, turn_id: str, received_at: str) -> None:
        event = _progress_event(payload, thread_id=thread_id, turn_id=turn_id, received_at=received_at, max_chars=self.hook_history_max_text_chars)
        if event is None:
            return
        self.storage.record_tracked_turn_progress_event(event)

    def _resolve_first_message(self, turn_id: str, message: dict[str, Any] | None) -> None:
        waiters = self._first_message_waiters.pop(turn_id, [])
        for future in waiters:
            if not future.done():
                future.set_result(message)

    def _mirror_turn_started(
        self,
        *,
        turn_id: str,
        thread_id: str,
        project_path: str | None,
        status: str,
        timestamp: str,
        user_message: str | None,
        model: str | None,
        permission_mode: str | None,
    ) -> None:
        if not self.hook_history_enabled:
            return
        self._upsert_hook_thread(thread_id=thread_id, project_path=project_path, timestamp=timestamp)
        self.storage.upsert_hook_turn(
            {
                "turn_id": turn_id,
                "thread_id": thread_id,
                "status": status,
                "started_at": timestamp,
                "updated_at": timestamp,
                "completed_at": None,
                "model": model,
                "permission_mode": permission_mode,
                "last_assistant_message": None,
                "last_error": None,
            }
        )
        if user_message:
            self._record_hook_message(
                message_id=f"app-server:{turn_id}:user-prompt",
                thread_id=thread_id,
                turn_id=turn_id,
                role="user",
                text=user_message,
                created_at=timestamp,
                sequence=0,
                hook_event_name="AppServerTurnStart",
                text_kind="prompt",
            )
        self.storage.commit()

    def _mirror_assistant_message(self, message: dict[str, Any], *, received_at: str) -> None:
        if not self.hook_history_enabled:
            return
        thread_id = str(message.get("thread_id") or "")
        turn_id = str(message.get("turn_id") or "")
        text = str(message.get("text") or "")
        if not thread_id or not turn_id or not text:
            return
        self._record_hook_message(
            message_id=f"app-server:{turn_id}:assistant:{str(message.get('event_hash') or '')[:16]}",
            event_hash=str(message.get("event_hash") or ""),
            thread_id=thread_id,
            turn_id=turn_id,
            role="assistant",
            text=text,
            created_at=str(message.get("created_at") or received_at),
            sequence=int(message.get("sequence") or 0),
            hook_event_name=str(message.get("event_type") or "AppServerAssistantMessage"),
            text_kind="assistant_visible",
        )
        self.storage.commit()

    def _mirror_turn_status(
        self,
        *,
        turn_id: str,
        thread_id: str,
        status: str,
        updated_at: str,
        completed_at: str | None,
        final_message: str | None,
        last_error: str | None,
        clear_last_error: bool = False,
    ) -> None:
        if not self.hook_history_enabled:
            return
        current = self.storage.get_hook_turn(turn_id) or {}
        self.storage.upsert_hook_turn(
            {
                "turn_id": turn_id,
                "thread_id": thread_id,
                "status": status,
                "started_at": current.get("started_at") or updated_at,
                "updated_at": updated_at,
                "completed_at": completed_at,
                "model": current.get("model"),
                "permission_mode": current.get("permission_mode"),
                "last_assistant_message": final_message,
                "last_error": last_error,
                "clear_last_error": clear_last_error,
            }
        )
        self.storage.commit()

    def _upsert_hook_thread(self, *, thread_id: str, project_path: str | None, timestamp: str) -> None:
        self.storage.upsert_hook_thread(
            {
                "thread_id": thread_id,
                "session_id": thread_id,
                "project_path": project_path,
                "project_path_key": path_key(project_path) if project_path else None,
                "title": None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "transcript_path": None,
                "source": "app_server_journal",
            }
        )

    def _record_hook_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        turn_id: str,
        role: str,
        text: str,
        created_at: str,
        sequence: int,
        hook_event_name: str,
        text_kind: str,
        event_hash: str | None = None,
    ) -> None:
        cleaned = _journal_text(text, self.hook_history_max_text_chars)
        event_hash = event_hash or _stable_hash(
            {
                "message_id": message_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "role": role,
                "text": cleaned,
                "hook_event_name": hook_event_name,
                "text_kind": text_kind,
            }
        )
        self.storage.record_hook_message(
            {
                "message_id": message_id,
                "event_hash": event_hash,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "role": role,
                "text": cleaned,
                "created_at": created_at,
                "sequence": sequence,
                "hook_event_name": hook_event_name,
                "text_kind": text_kind,
            }
        )


def _payload_params(payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params")
    if isinstance(params, dict):
        return params
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return {}


def _extract_thread_id(payload: dict[str, Any]) -> str | None:
    params = _payload_params(payload)
    if params.get("threadId"):
        return str(params["threadId"])
    thread = params.get("thread")
    if isinstance(thread, dict) and thread.get("id"):
        return str(thread["id"])
    return None


def _extract_turn_id(payload: dict[str, Any]) -> str | None:
    params = _payload_params(payload)
    if params.get("turnId"):
        return str(params["turnId"])
    turn = params.get("turn")
    if isinstance(turn, dict) and turn.get("id"):
        return str(turn["id"])
    return None


def _assistant_message(payload: dict[str, Any], *, turn_id: str, thread_id: str, received_at: str) -> dict[str, Any] | None:
    params = _payload_params(payload)
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type not in {"agentMessage", "assistantMessage"}:
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    created_at = str(params.get("createdAt") or params.get("created_at") or received_at)
    return {
        "event_hash": _event_hash(payload),
        "turn_id": turn_id,
        "thread_id": thread_id,
        "role": "assistant",
        "text": text,
        "created_at": created_at,
        "sequence": int(params.get("sequence") or 0),
        "event_type": item_type,
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }


def _event_status(payload: dict[str, Any]) -> str | None:
    method = str(payload.get("method") or "")
    params = _payload_params(payload)
    status = params.get("status")
    turn = params.get("turn")
    if status is None and isinstance(turn, dict):
        status = turn.get("status")
    if method == "turn/completed":
        return _normalize_status(status or "completed")
    if method in {"turn/aborted", "turn/cancelled", "turn/canceled"}:
        return "aborted"
    if method == "turn/error":
        return "failed"
    if method in {"item/tool/requestUserInput", "tool/requestUserInput"}:
        return "waiting_for_user_input"
    if "requestApproval" in method or method == "mcpServer/elicitation/request":
        return "waiting_for_approval"
    if method in {"turn/started", "turn/start"}:
        return "running"
    if method in {"turn/updated", "turn/status/updated"} and status:
        return _normalize_status(status)
    return None


def _normalize_status(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("type") or value.get("status") or value.get("state") or "unknown"
    normalized = str(value or "").strip().lower()
    if normalized in {"idle", "complete", "completed", "done"}:
        return "completed"
    if normalized in {"in_progress", "inprogress", "running", "started"}:
        return "running"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"interrupted", "interrupt"}:
        return "interrupted"
    if normalized in {"failed", "error"}:
        return "failed"
    if normalized == "unknown_after_app_server_exit":
        return "unknown_after_app_server_exit"
    if normalized == "aborted":
        return "aborted"
    return normalized or "unknown"


def _status_with_final_message(status: Any, final_message: str | None) -> str:
    return _normalize_status(status)


def _terminal_evidence(turn: dict[str, Any], status: str) -> dict[str, Any]:
    completed_at = turn.get("completed_at")
    trusted = status in TERMINAL_STATUSES and bool(completed_at)
    return {
        "trusted": trusted,
        "source": "app_server" if trusted else None,
        "method": "turn_lifecycle_event" if trusted else None,
        "observedAt": completed_at if trusted else None,
    }


def _event_error(payload: dict[str, Any]) -> str | None:
    params = _payload_params(payload)
    for key in ("error", "message", "reason"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = value.get("message")
            if isinstance(nested, str) and nested:
                return nested
    return None


def _progress_event(
    payload: dict[str, Any],
    *,
    thread_id: str,
    turn_id: str,
    received_at: str,
    max_chars: int,
) -> dict[str, Any] | None:
    method = str(payload.get("method") or "")
    if method not in PROGRESS_EVENT_METHODS:
        return None
    params = _payload_params(payload)
    category = method.replace("/", "_")
    severity = "info"
    item_id = _optional_str(params.get("itemId"))
    sequence = _safe_int(params.get("sequence"), 0)
    text: str | None = None
    metadata: dict[str, Any] = {}

    if method == "item/agentMessage/delta":
        category = "agent_message_delta"
        text, truncated = _clean_progress_text(params.get("delta"), max_chars)
        metadata = {"itemId": item_id}
    elif method == "item/plan/delta":
        category = "plan_delta"
        text, truncated = _clean_progress_text(params.get("delta"), max_chars)
        metadata = {"itemId": item_id}
    elif method == "item/reasoning/summaryPartAdded":
        category = "reasoning_summary"
        sequence = _safe_int(params.get("summaryIndex"), sequence)
        text = "Reasoning summary part added."
        truncated = False
        metadata = {"itemId": item_id, "summaryIndex": params.get("summaryIndex")}
    elif method == "item/reasoning/summaryTextDelta":
        category = "reasoning_summary"
        sequence = _safe_int(params.get("summaryIndex"), sequence)
        text, truncated = _clean_progress_text(params.get("delta"), max_chars)
        metadata = {"itemId": item_id, "summaryIndex": params.get("summaryIndex")}
    elif method == "turn/diff/updated":
        category = "diff_updated"
        diff = params.get("diff") if isinstance(params.get("diff"), str) else ""
        text = "Turn diff updated."
        truncated = False
        metadata = _diff_stats(diff)
    elif method == "thread/tokenUsage/updated":
        category = "token_usage"
        text = "Token usage updated."
        truncated = False
        metadata = {"tokenUsage": _safe_metadata(params.get("tokenUsage"))}
    elif method == "model/rerouted":
        category = "model_reroute"
        from_model = _optional_str(params.get("fromModel"))
        to_model = _optional_str(params.get("toModel"))
        reason = _optional_str(params.get("reason"))
        text, truncated = _clean_progress_text(f"Model rerouted from {from_model or '?'} to {to_model or '?'}: {reason or 'unknown'}", max_chars)
        metadata = {"fromModel": from_model, "toModel": to_model, "reason": reason}
    elif method in {"warning", "guardianWarning"}:
        category = "warning"
        severity = "warning"
        text, truncated = _clean_progress_text(params.get("message"), max_chars)
        metadata = {"warningType": method}
    elif method == "configWarning":
        category = "warning"
        severity = "warning"
        summary = params.get("summary")
        details = params.get("details")
        joined = f"{summary or ''}\n{details or ''}".strip()
        text, truncated = _clean_progress_text(joined, max_chars)
        metadata = {
            "warningType": method,
            "path": _clean_metadata_string(params.get("path"), max_chars=1000),
            "range": _safe_metadata(params.get("range")),
        }
    else:
        return None

    if not text and not metadata:
        return None
    return {
        "event_hash": _progress_event_hash(payload, received_at),
        "turn_id": turn_id,
        "thread_id": thread_id,
        "event_type": method,
        "category": category,
        "severity": severity,
        "item_id": item_id,
        "sequence": sequence,
        "text": text,
        "metadata_json": json.dumps(_safe_metadata(metadata), ensure_ascii=False, sort_keys=True),
        "created_at": received_at,
        "truncated": int(bool(truncated)),
    }


def turn_progress_status_fields(
    storage: McpStorage,
    turn_id: str,
    *,
    progress_events: int = 10,
    progress_max_chars: int = 2000,
) -> dict[str, Any]:
    if progress_events <= 0:
        return {}
    rows = storage.list_tracked_turn_progress_events(turn_id=turn_id, limit=progress_events)
    summary = storage.tracked_turn_progress_summary(turn_id)
    token_event = summary.get("tokenUsageEvent")
    token_metadata = _json_loads_dict((token_event or {}).get("metadata_json"))
    return {
        "progressEvents": [_progress_event_to_tool(row, progress_max_chars) for row in rows],
        "progressEventCount": summary.get("eventCount", 0),
        "latestProgressAt": summary.get("latestProgressAt"),
        "tokenUsage": token_metadata.get("tokenUsage") if token_metadata else None,
        "modelReroutes": [_model_reroute_to_tool(row, progress_max_chars) for row in summary.get("modelReroutes") or []],
        "warnings": [_progress_event_to_tool(row, progress_max_chars) for row in summary.get("warnings") or []],
    }


def progress_event_to_tool(row: dict[str, Any], max_chars: int = 2000) -> dict[str, Any]:
    return _progress_event_to_tool(row, max_chars)


def _progress_event_to_tool(row: dict[str, Any], max_chars: int) -> dict[str, Any]:
    text, budget = _truncate_with_budget(row.get("text"), max_chars)
    metadata = _json_loads_dict(row.get("metadata_json"))
    return {
        "id": row.get("id"),
        "eventHash": row.get("event_hash"),
        "eventType": row.get("event_type"),
        "category": row.get("category"),
        "severity": row.get("severity"),
        "threadId": row.get("thread_id"),
        "turnId": row.get("turn_id"),
        "itemId": row.get("item_id"),
        "sequence": row.get("sequence"),
        "createdAt": row.get("created_at"),
        "text": text,
        "metadata": metadata,
        "truncated": bool(row.get("truncated")) or bool(budget["truncated"]),
        "originalChars": budget["original_chars"],
        "returnedChars": budget["returned_chars"],
    }


def _model_reroute_to_tool(row: dict[str, Any], max_chars: int) -> dict[str, Any]:
    event = _progress_event_to_tool(row, max_chars)
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return {
        "createdAt": event.get("createdAt"),
        "fromModel": metadata.get("fromModel"),
        "toModel": metadata.get("toModel"),
        "reason": metadata.get("reason"),
        "event": event,
    }


def _progress_event_hash(payload: dict[str, Any], received_at: str) -> str:
    return _stable_hash({"payload": payload, "received_at": received_at})


def _clean_progress_text(value: Any, max_chars: int) -> tuple[str | None, bool]:
    if value in (None, ""):
        return None, False
    raw = str(value)
    cleaned = _journal_text(raw, max_chars)
    return cleaned, len(raw) > max_chars


def _clean_metadata_string(value: Any, *, max_chars: int) -> str | None:
    text, _truncated = _clean_progress_text(value, max_chars)
    return text


def _safe_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_metadata(item) for key, item in value.items() if key not in {"diff", "payload", "toolCall", "toolOutput", "commandOutput"}}
    if isinstance(value, list):
        return [_safe_metadata(item) for item in value[:100]]
    if isinstance(value, str):
        return _clean_metadata_string(value, max_chars=2000)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _clean_metadata_string(value, max_chars=2000)


def _diff_stats(diff: str) -> dict[str, Any]:
    lines = diff.splitlines()
    return {
        "charCount": len(diff),
        "lineCount": len(lines),
        "addedLineCount": sum(1 for line in lines if line.startswith("+") and not line.startswith("+++")),
        "removedLineCount": sum(1 for line in lines if line.startswith("-") and not line.startswith("---")),
        "fileMarkerCount": sum(1 for line in lines if line.startswith("diff --git ") or line.startswith("+++ ")),
    }


def _json_loads_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        loaded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _event_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _stable_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8", errors="replace")).hexdigest()


def _journal_text(value: Any, max_chars: int) -> str:
    text = "" if value is None else str(value)
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)"):
            text = pattern.sub(lambda match: f"{match.group(1)}=[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    if len(text) <= max_chars:
        return text
    marker = "\n[message truncated by OpenClaw hook history]"
    return text[: max(0, max_chars - len(marker))].rstrip() + marker


def _message_to_tool(message: dict[str, Any], max_chars: int) -> dict[str, Any]:
    return {
        "role": message.get("role"),
        "created_at": message.get("created_at"),
        "createdAt": message.get("created_at"),
        "eventType": message.get("event_type"),
        "sequence": message.get("sequence"),
        "text": _truncate(message.get("text"), max_chars),
    }


def _truncate(value: Any, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _truncate_with_budget(value: Any, max_chars: int) -> tuple[str | None, dict[str, Any]]:
    if value is None:
        return None, {"truncated": False, "original_chars": 0, "returned_chars": 0}
    text = str(value)
    truncated = len(text) > max_chars
    visible = _truncate(text, max_chars)
    return visible, {
        "truncated": truncated,
        "original_chars": len(text),
        "returned_chars": len(visible or ""),
    }


def _visible_last_error(turn: dict[str, Any]) -> str | None:
    last_error = turn.get("last_error")
    if turn.get("status") == "completed" and last_error == WAITING_FOR_OPENCLAW_ERROR:
        return None
    return last_error


def _plan_to_tool(row: dict[str, Any], max_chars: int) -> dict[str, Any]:
    text = _truncate(row.get("text"), max_chars)
    return {
        "item_id": row.get("item_id"),
        "itemId": row.get("item_id"),
        "thread_id": row.get("thread_id"),
        "threadId": row.get("thread_id"),
        "turn_id": row.get("turn_id"),
        "turnId": row.get("turn_id"),
        "status": row.get("status"),
        "markdown": text,
        "text": text,
        "created_at": row.get("created_at"),
        "createdAt": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "updatedAt": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
        "completedAt": row.get("completed_at"),
        "truncated": isinstance(row.get("text"), str) and len(str(row.get("text"))) > max_chars,
    }


def _staleness_seconds(updated_at: Any) -> int | None:
    if not updated_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _ms_to_iso(value: Any) -> str | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return datetime.fromtimestamp(parsed / 1000, timezone.utc).isoformat()


def _snapshot_turn_error(turn: dict[str, Any]) -> str | None:
    error = turn.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return None
