from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import invalid_argument, pending_interaction_not_found, pending_interaction_unavailable
from .statuses import PENDING_INTERACTION_TERMINAL_STATUSES
from .storage import McpStorage


COMMAND_APPROVAL_METHOD = "item/commandExecution/requestApproval"
FILE_APPROVAL_METHOD = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL_METHOD = "item/permissions/requestApproval"
TOOL_USER_INPUT_METHOD = "item/tool/requestUserInput"
LEGACY_TOOL_USER_INPUT_METHOD = "tool/requestUserInput"
MCP_ELICITATION_METHOD = "mcpServer/elicitation/request"

SUPPORTED_INTERACTION_METHODS = {
    COMMAND_APPROVAL_METHOD,
    FILE_APPROVAL_METHOD,
    PERMISSIONS_APPROVAL_METHOD,
    TOOL_USER_INPUT_METHOD,
    LEGACY_TOOL_USER_INPUT_METHOD,
    MCP_ELICITATION_METHOD,
}

APPROVAL_DECISIONS = {"accept", "acceptForSession", "decline", "cancel"}
ELICITATION_ACTIONS = {"accept", "decline", "cancel"}
SECRET_KEY_FRAGMENTS = ("secret", "password", "token", "api_key", "apikey", "authorization")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_supported_interaction_method(method: str) -> bool:
    return method in SUPPORTED_INTERACTION_METHODS


@dataclass(slots=True)
class LiveInteraction:
    interaction_id: str
    app_server_request_id: Any
    method: str
    params: dict[str, Any]
    process_generation: int
    future: asyncio.Future[dict[str, Any]]


class PendingInteractionManager:
    def __init__(self, storage: McpStorage) -> None:
        self.storage = storage
        self._live: dict[str, LiveInteraction] = {}

    def create(
        self,
        *,
        app_server_request_id: Any,
        method: str,
        params: dict[str, Any],
        process_generation: int,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if not is_supported_interaction_method(method):
            raise invalid_argument("Unsupported app-server interaction method", method=method)
        loop = asyncio.get_running_loop()
        interaction_id = "int_" + uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=max(1, int(timeout_seconds)))
        risk_summary = risk_summary_for_method(method, params)
        answer_schema = answer_schema_for_method(method, params)
        row = {
            "interaction_id": interaction_id,
            "app_server_request_id": str(app_server_request_id),
            "method": method,
            "thread_id": _optional_str(params.get("threadId")),
            "turn_id": _optional_str(params.get("turnId")),
            "item_id": _optional_str(params.get("itemId")),
            "status": "pending",
            "params_json": json.dumps(params, ensure_ascii=False),
            "response_json": None,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "resolved_at": None,
            "process_generation": process_generation,
            "auto_resolved": 0,
            "recommended_action": recommended_action_for_status("pending"),
            "risk_summary_json": json.dumps(risk_summary, ensure_ascii=False),
            "answer_schema_json": json.dumps(answer_schema, ensure_ascii=False),
            "response_redacted": 0,
            "last_error": None,
        }
        self.storage.upsert_pending_interaction(row)
        self.storage.record_pending_interaction_event(
            interaction_id,
            event_type="created",
            status="pending",
            details={
                "method": method,
                "kind": interaction_kind(method),
                "threadId": row["thread_id"],
                "turnId": row["turn_id"],
                "itemId": row["item_id"],
                "expiresAt": row["expires_at"],
            },
            created_at=row["created_at"],
        )
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._live[interaction_id] = LiveInteraction(
            interaction_id=interaction_id,
            app_server_request_id=app_server_request_id,
            method=method,
            params=params,
            process_generation=process_generation,
            future=future,
        )
        return self.interaction_to_tool(self.storage.get_pending_interaction(interaction_id) or row)

    async def wait_for_response(self, interaction_id: str, *, timeout_seconds: int) -> dict[str, Any]:
        live = self._live.get(interaction_id)
        if live is None:
            raise pending_interaction_unavailable("Pending interaction is not live.", interaction_id=interaction_id)
        try:
            return await asyncio.wait_for(asyncio.shield(live.future), timeout=max(1, int(timeout_seconds)))
        except asyncio.TimeoutError:
            response = default_response_for_method(live.method, live.params)
            redacted_response = _redact_response_for_storage(live.method, live.params, response)
            self.storage.update_pending_interaction(
                interaction_id,
                status="auto_declined",
                resolved_at=now_iso(),
                response=redacted_response,
                auto_resolved=True,
                response_redacted=_was_redacted(response, redacted_response),
                last_error="OpenClaw did not answer before approval timeout.",
                event_type="auto_declined",
                event_details={"reason": "approval_timeout", "autoResolved": True},
            )
            if not live.future.done():
                live.future.set_result(response)
            return response
        finally:
            self._live.pop(interaction_id, None)

    def answer(self, interaction_id: str, args: dict[str, Any], *, current_process_generation: int) -> dict[str, Any]:
        row = self.storage.get_pending_interaction(interaction_id)
        if row is None:
            raise pending_interaction_not_found(interaction_id)
        if row.get("status") != "pending":
            raise pending_interaction_unavailable(
                "Pending interaction is no longer answerable.",
                interaction_id=interaction_id,
                status=row.get("status"),
            )
        expires_at = _parse_iso(row.get("expires_at"))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            self.storage.update_pending_interaction(
                interaction_id,
                status="expired",
                resolved_at=now_iso(),
                auto_resolved=False,
                last_error="Pending interaction expired before OpenClaw answered.",
                event_type="expired",
                event_details={"reason": "answer_after_expiry"},
            )
            raise pending_interaction_unavailable(
                "Pending interaction expired before OpenClaw answered.",
                interaction_id=interaction_id,
                status="expired",
            )
        if row.get("process_generation") not in (None, current_process_generation):
            raise pending_interaction_unavailable(
                "Pending interaction belongs to a previous app-server generation.",
                interaction_id=interaction_id,
                process_generation=row.get("process_generation"),
                current_process_generation=current_process_generation,
            )
        live = self._live.get(interaction_id)
        if live is None or live.future.done():
            raise pending_interaction_unavailable("Pending interaction is not live in this MCP process.", interaction_id=interaction_id)
        params = _json_object(row.get("params_json"))
        response = build_response_for_answer(str(row["method"]), params, args)
        redacted_response = _redact_response_for_storage(str(row["method"]), params, response)
        self.storage.update_pending_interaction(
            interaction_id,
            status="answered",
            resolved_at=now_iso(),
            response=redacted_response,
            auto_resolved=False,
            response_redacted=_was_redacted(response, redacted_response),
            last_error=None,
            event_type="answered",
            event_details={"method": row["method"], "kind": interaction_kind(str(row["method"]))},
        )
        live.future.set_result(response)
        return {
            "ok": True,
            "answered": True,
            "interaction": self.interaction_to_tool(self.storage.get_pending_interaction(interaction_id) or row),
            "response": redacted_response,
            "responseRedacted": _was_redacted(response, redacted_response),
        }

    def list_interactions(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return [
            self.interaction_to_tool(row)
            for row in self.storage.list_pending_interactions(
                thread_id=thread_id,
                turn_id=turn_id,
                status=status,
                limit=limit,
            )
        ]

    def pending_count(self) -> int:
        return self.storage.count_pending_interactions(status="pending")

    def orphan_live(self, *, process_generation: int | None, reason: str) -> None:
        timestamp = now_iso()
        for interaction_id, live in list(self._live.items()):
            if process_generation is not None and live.process_generation != process_generation:
                continue
            if not live.future.done():
                live.future.set_exception(RuntimeError(reason))
            self._live.pop(interaction_id, None)
        self.storage.mark_pending_interactions_orphaned(
            process_generation=process_generation,
            reason=reason,
            resolved_at=timestamp,
        )

    def interaction_to_tool(self, row: dict[str, Any]) -> dict[str, Any]:
        return interaction_row_to_tool(row)


def build_response_for_answer(method: str, params: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    raw_response = args.get("raw_response")
    if isinstance(raw_response, dict):
        return raw_response

    if method in {COMMAND_APPROVAL_METHOD, FILE_APPROVAL_METHOD}:
        decision_payload = args.get("decision_payload")
        if decision_payload is None:
            decision_payload = args.get("decisionPayload")
        if decision_payload is None and isinstance(args.get("decision"), dict):
            decision_payload = args.get("decision")
        if isinstance(decision_payload, dict):
            _validate_decision_payload(params.get("availableDecisions"), decision_payload)
            return {"decision": decision_payload}
        decision = str(args.get("decision") or "").strip()
        if decision not in APPROVAL_DECISIONS:
            raise invalid_argument("Unsupported approval decision.", decision=decision, allowed=sorted(APPROVAL_DECISIONS))
        available = params.get("availableDecisions")
        if isinstance(available, list) and available and not _decision_available(available, decision):
            raise invalid_argument("Decision is not available for this approval request.", decision=decision, available_decisions=available)
        return {"decision": decision}

    if method == PERMISSIONS_APPROVAL_METHOD:
        permissions = args.get("permissions")
        if permissions is None:
            permissions = {}
        if not isinstance(permissions, dict):
            raise invalid_argument("permissions must be an object")
        scope = str(args.get("scope") or "turn")
        if scope not in {"turn", "session"}:
            raise invalid_argument("scope must be turn or session", scope=scope)
        response: dict[str, Any] = {"permissions": permissions, "scope": scope}
        if "strict_auto_review" in args:
            response["strictAutoReview"] = bool(args.get("strict_auto_review"))
        if "strictAutoReview" in args:
            response["strictAutoReview"] = bool(args.get("strictAutoReview"))
        return response

    if method in {TOOL_USER_INPUT_METHOD, LEGACY_TOOL_USER_INPUT_METHOD}:
        answers = args.get("answers")
        if not isinstance(answers, dict):
            raise invalid_argument("answers must be an object mapping question ids to answers")
        _validate_question_ids(params.get("questions"), answers)
        return {"answers": {str(key): {"answers": _answer_list(value)} for key, value in answers.items()}}

    if method == MCP_ELICITATION_METHOD:
        action = str(args.get("action") or "").strip()
        if action not in ELICITATION_ACTIONS:
            raise invalid_argument("Unsupported elicitation action.", action=action, allowed=sorted(ELICITATION_ACTIONS))
        content = args.get("content") if "content" in args else None
        if content is not None and not isinstance(content, dict):
            raise invalid_argument("content must be an object or null")
        return {"action": action, "content": content, "_meta": args.get("_meta", args.get("meta"))}

    raise invalid_argument("Unsupported interaction method", method=method)


def default_response_for_method(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method in {COMMAND_APPROVAL_METHOD, FILE_APPROVAL_METHOD}:
        available = params.get("availableDecisions")
        if isinstance(available, list) and available and not _decision_available(available, "decline") and _decision_available(available, "cancel"):
            return {"decision": "cancel"}
        return {"decision": "decline"}
    if method == PERMISSIONS_APPROVAL_METHOD:
        return {"permissions": {}, "scope": "turn"}
    if method in {TOOL_USER_INPUT_METHOD, LEGACY_TOOL_USER_INPUT_METHOD}:
        questions = params.get("questions")
        if not isinstance(questions, list):
            return {"answers": {}}
        return {
            "answers": {
                str(question.get("id")): {"answers": []}
                for question in questions
                if isinstance(question, dict) and question.get("id")
            }
        }
    if method == MCP_ELICITATION_METHOD:
        return {"action": "decline", "content": None, "_meta": None}
    return {}


def interaction_kind(method: str) -> str:
    if method in {COMMAND_APPROVAL_METHOD, FILE_APPROVAL_METHOD}:
        return "approval"
    if method == PERMISSIONS_APPROVAL_METHOD:
        return "permissions"
    if method in {TOOL_USER_INPUT_METHOD, LEGACY_TOOL_USER_INPUT_METHOD}:
        return "user_input"
    if method == MCP_ELICITATION_METHOD:
        return "elicitation"
    return "unknown"


def prompt_for(method: str, params: dict[str, Any]) -> str | None:
    if method == COMMAND_APPROVAL_METHOD:
        reason = params.get("reason")
        command = params.get("command")
        if reason and command:
            return f"{reason}: {command}"
        return str(reason or command or "Approve command execution?")
    if method == FILE_APPROVAL_METHOD:
        return str(params.get("reason") or params.get("grantRoot") or "Approve file changes?")
    if method == PERMISSIONS_APPROVAL_METHOD:
        return str(params.get("reason") or "Approve requested permissions?")
    if method in {TOOL_USER_INPUT_METHOD, LEGACY_TOOL_USER_INPUT_METHOD}:
        questions = params.get("questions")
        if isinstance(questions, list):
            texts = [str(question.get("question")) for question in questions if isinstance(question, dict) and question.get("question")]
            if texts:
                return "\n".join(texts)
        return "Codex requested user input."
    if method == MCP_ELICITATION_METHOD:
        return str(params.get("message") or "Codex requested MCP elicitation input.")
    return None


def interaction_row_to_tool(row: dict[str, Any]) -> dict[str, Any]:
    params = _json_object(row.get("params_json"))
    response = _json_object(row.get("response_json"))
    method = str(row.get("method") or "")
    status = str(row.get("status") or "")
    risk_summary = _json_object(row.get("risk_summary_json")) or risk_summary_for_method(method, params)
    answer_schema = _json_object(row.get("answer_schema_json")) or answer_schema_for_method(method, params)
    recommended_action = str(row.get("recommended_action") or recommended_action_for_status(status))
    payload = {
        "interaction_id": row.get("interaction_id"),
        "interactionId": row.get("interaction_id"),
        "method": method,
        "kind": interaction_kind(method),
        "thread_id": row.get("thread_id"),
        "threadId": row.get("thread_id"),
        "turn_id": row.get("turn_id"),
        "turnId": row.get("turn_id"),
        "item_id": row.get("item_id"),
        "itemId": row.get("item_id"),
        "status": status,
        "terminal": status in PENDING_INTERACTION_TERMINAL_STATUSES,
        "prompt": prompt_for(method, params),
        "availableDecisions": params.get("availableDecisions"),
        "questions": _questions_for_tool(params.get("questions")),
        "answerSchema": answer_schema,
        "recommendedAction": recommended_action,
        "riskSummary": risk_summary,
        "expires_at": row.get("expires_at"),
        "expiresAt": row.get("expires_at"),
        "created_at": row.get("created_at"),
        "createdAt": row.get("created_at"),
        "resolved_at": row.get("resolved_at"),
        "resolvedAt": row.get("resolved_at"),
        "processGeneration": row.get("process_generation"),
        "autoResolved": bool(row.get("auto_resolved")),
        "responseRedacted": bool(row.get("response_redacted")),
        "lastError": row.get("last_error"),
        "params": _redact_sensitive_payload(params),
    }
    if response:
        payload["response"] = response
    return payload


def recommended_action_for_status(status: str) -> str:
    if status == "pending":
        return "answer_pending_interaction"
    if status in {"expired", "failed", "orphaned_after_app_server_exit"}:
        return "inspect_diagnostics"
    return "none"


def risk_summary_for_method(method: str, params: dict[str, Any]) -> dict[str, Any]:
    kind = interaction_kind(method)
    if method == COMMAND_APPROVAL_METHOD:
        command = str(params.get("command") or "")
        lowered = command.casefold()
        destructive = any(token in lowered for token in ("remove-item", "del ", " rmdir", " rm ", "format ", "git reset --hard"))
        return {
            "level": "high" if destructive else "medium",
            "category": "command_execution",
            "summary": "Codex requests permission to run a command.",
            "commandPreview": _truncate(command, 500),
        }
    if method == FILE_APPROVAL_METHOD:
        return {
            "level": "medium",
            "category": "file_change",
            "summary": "Codex requests permission to change files.",
            "grantRoot": _optional_str(params.get("grantRoot")),
        }
    if method == PERMISSIONS_APPROVAL_METHOD:
        return {
            "level": "high",
            "category": "permissions",
            "summary": "Codex requests broader permissions.",
            "scope": _optional_str(params.get("scope")),
        }
    if kind == "user_input":
        questions = _questions_for_tool(params.get("questions"))
        return {
            "level": "low",
            "category": "user_input",
            "summary": f"Codex asks {len(questions)} question(s).",
            "secretQuestionCount": sum(1 for question in questions if question.get("isSecret")),
        }
    if kind == "elicitation":
        return {
            "level": "low",
            "category": "elicitation",
            "summary": "Codex requests MCP elicitation input.",
        }
    return {"level": "medium", "category": "unknown", "summary": "Codex requests OpenClaw input."}


def answer_schema_for_method(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method in {COMMAND_APPROVAL_METHOD, FILE_APPROVAL_METHOD}:
        return {
            "type": "approval_decision",
            "required": ["decision"],
            "decisions": _available_decisions_for_tool(params.get("availableDecisions")),
            "rawResponseAllowed": True,
        }
    if method == PERMISSIONS_APPROVAL_METHOD:
        return {
            "type": "permissions",
            "required": ["permissions"],
            "scope": ["turn", "session"],
            "rawResponseAllowed": True,
        }
    if method in {TOOL_USER_INPUT_METHOD, LEGACY_TOOL_USER_INPUT_METHOD}:
        return {
            "type": "question_answers",
            "required": ["answers"],
            "questions": _questions_for_tool(params.get("questions")),
            "rawResponseAllowed": True,
        }
    if method == MCP_ELICITATION_METHOD:
        return {
            "type": "elicitation",
            "required": ["action"],
            "actions": sorted(ELICITATION_ACTIONS),
            "rawResponseAllowed": True,
        }
    return {"type": "unknown", "required": [], "rawResponseAllowed": True}


def _answer_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _decision_available(available: list[Any], decision: str) -> bool:
    for item in available:
        if item == decision:
            return True
        if isinstance(item, dict) and decision in item:
            return True
        if isinstance(item, dict) and item.get("decision") == decision:
            return True
    return False


def _validate_decision_payload(available: Any, decision_payload: dict[str, Any]) -> None:
    if not decision_payload:
        raise invalid_argument("decision_payload must not be empty")
    if not isinstance(available, list) or not available:
        return
    payload_keys = set(decision_payload)
    for item in available:
        if isinstance(item, dict):
            item_keys = set(item)
            if payload_keys & item_keys:
                return
            if item.get("decision") in payload_keys:
                return
        elif item in payload_keys:
            return
    raise invalid_argument(
        "Decision payload is not available for this approval request.",
        decision_payload=decision_payload,
        available_decisions=available,
    )


def _validate_question_ids(questions: Any, answers: dict[str, Any]) -> None:
    if not isinstance(questions, list) or not questions:
        return
    allowed = {
        str(question.get("id"))
        for question in questions
        if isinstance(question, dict) and question.get("id")
    }
    if not allowed:
        return
    unknown = sorted(str(key) for key in answers if str(key) not in allowed)
    if unknown:
        raise invalid_argument("answers contains unknown question ids", unknown_question_ids=unknown, allowed_question_ids=sorted(allowed))


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _redact_response_for_storage(method: str, params: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    redacted = _redact_sensitive_payload(response)
    if method not in {TOOL_USER_INPUT_METHOD, LEGACY_TOOL_USER_INPUT_METHOD}:
        return redacted
    secret_ids: set[str] = set()
    questions = params.get("questions")
    if isinstance(questions, list):
        for question in questions:
            if isinstance(question, dict) and (question.get("is_secret") or question.get("isSecret")) and question.get("id"):
                secret_ids.add(str(question["id"]))
    if not secret_ids:
        return redacted
    redacted = json.loads(json.dumps(redacted, ensure_ascii=False))
    answers = redacted.get("answers")
    if isinstance(answers, dict):
        for question_id in secret_ids:
            if question_id in answers:
                answers[question_id] = {"answers": ["[redacted]"]}
    return redacted


def _questions_for_tool(questions: Any) -> list[dict[str, Any]]:
    if not isinstance(questions, list):
        return []
    result: list[dict[str, Any]] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        item = {
            "id": _optional_str(question.get("id")),
            "question": _optional_str(question.get("question")),
            "isSecret": bool(question.get("isSecret") or question.get("is_secret")),
        }
        if question.get("type") is not None:
            item["type"] = str(question.get("type"))
        if isinstance(question.get("options"), list):
            item["options"] = [str(option) for option in question.get("options")]
        result.append(item)
    return result


def _available_decisions_for_tool(available: Any) -> list[Any]:
    if not isinstance(available, list):
        return []
    return _redact_sensitive_payload(available)


def _redact_sensitive_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(fragment in key_text.casefold() for fragment in SECRET_KEY_FRAGMENTS):
                result[key_text] = "[redacted]"
            else:
                result[key_text] = _redact_sensitive_payload(item)
        return result
    if isinstance(value, list):
        return [_redact_sensitive_payload(item) for item in value]
    return value


def _was_redacted(original: dict[str, Any], redacted: dict[str, Any]) -> bool:
    return json.dumps(original, ensure_ascii=False, sort_keys=True) != json.dumps(redacted, ensure_ascii=False, sort_keys=True)


def _parse_iso(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _truncate(value: Any, max_chars: int) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."
