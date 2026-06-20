from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any


def normalize_public_status_payload(payload: dict[str, Any], *, surface: str) -> dict[str, Any]:
    normalized = _normalize_value(payload)
    if isinstance(normalized, dict):
        _replace_raw_requests(normalized)
        _compact_titles_and_thread_state(normalized)
        _cap_historical_debt(normalized)
        _dedup_message_blocks(normalized)
        _specialize_workflow_payload(normalized)
        normalized.setdefault("statusContract", {"version": "status-v2", "surface": surface})
    return normalized if isinstance(normalized, dict) else payload


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        timestamp_corrected = False
        for key, item in value.items():
            normalized = _normalize_value(item)
            if _is_timestamp_key(str(key)):
                safe, corrected = _safe_timestamp(normalized)
                normalized = safe
                timestamp_corrected = timestamp_corrected or corrected
            result[str(key)] = normalized
        if timestamp_corrected:
            result["timestampCorrected"] = True
        return result
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def _is_timestamp_key(key: str) -> bool:
    lowered = key.casefold()
    return lowered.endswith("at") or lowered in {"created_at", "updated_at", "completed_at", "observed_at"}


def _safe_timestamp(value: Any) -> tuple[Any, bool]:
    if value in (None, ""):
        return value, False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value, False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed.year < 2020:
        return None, True
    return value, False


def _cap_historical_debt(payload: dict[str, Any]) -> None:
    for key in ("staleOperations", "prematureTerminalOperations", "staleWorkflowRefs", "staleTurnRefs"):
        items = payload.get(key)
        if isinstance(items, list) and len(items) > 5:
            payload[key + "Count"] = payload.get(key + "Count") or len(items)
            payload[key + "Refs"] = [_compact_ref(item) for item in items[:5] if isinstance(item, dict)]
            payload[key] = []
            payload["detailsTruncated"] = True
    debt = payload.get("historicalDebt")
    if isinstance(debt, dict):
        _cap_historical_debt(debt)


def _compact_ref(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in ("operationId", "operation_id", "workflowId", "workflow_id", "threadId", "thread_id", "turnId", "turn_id", "status")
        if item.get(key) is not None
    }


def _replace_raw_requests(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in list(payload.items()):
            if key in {"request", "requestPayload", "request_json", "requestJson"} and isinstance(value, dict):
                payload[key + "Summary"] = _request_summary(value)
                payload.pop(key, None)
                continue
            if key in {"reviewTarget", "target"} and isinstance(value, dict):
                payload[key] = _target_summary(value)
                continue
            if key in {"instructions", "prompt", "message", "title"} and isinstance(value, str) and _looks_like_public_status_text_context(payload):
                payload[key + "Summary"] = _text_summary(value, include_preview=False)
                payload.pop(key, None)
                continue
            _replace_raw_requests(value)
    elif isinstance(payload, list):
        for item in payload:
            _replace_raw_requests(item)


def _request_summary(request: dict[str, Any]) -> dict[str, Any]:
    operation_type = request.get("operation_type") or request.get("operationType")
    summary: dict[str, Any] = {
        "operationType": operation_type,
        "targetType": request.get("target_type") or request.get("targetType"),
        "threadMode": request.get("thread_mode") or request.get("threadMode"),
        "dedupPolicy": request.get("dedup_policy") or request.get("dedupPolicy"),
        "allowHistoricalContinuation": bool(request.get("allow_historical_continuation") or request.get("allowHistoricalContinuation")),
        "delivery": request.get("delivery"),
        "runtimePolicy": _runtime_policy_summary(request),
        "threadRef": _id_ref(request.get("thread_id") or request.get("threadId")),
        "sourceThreadRef": _id_ref(request.get("source_thread_id") or request.get("sourceThreadId")),
        "projectRef": _id_ref(request.get("project_id") or request.get("projectId")),
        "workflowRef": _id_ref(request.get("workflow_id") or request.get("workflowId")),
    }
    for text_key in ("message", "instructions", "title", "commit_title", "goal"):
        value = request.get(text_key)
        if isinstance(value, str) and value.strip():
            summary[_camel(text_key) + "Summary"] = _text_summary(value, include_preview=False)
    input_state = request.get("_input_item_state") or request.get("inputItemState")
    if isinstance(input_state, dict):
        summary["inputItemState"] = input_state
    elif isinstance(request.get("input_items"), list):
        summary["inputItemState"] = {"count": len(request["input_items"])}
    output_schema_state = request.get("_output_schema_state")
    if isinstance(output_schema_state, dict):
        summary["outputSchemaHash"] = output_schema_state.get("schemaHash") or output_schema_state.get("schema_hash")
    elif request.get("output_schema_hash"):
        summary["outputSchemaHash"] = request.get("output_schema_hash")
    resource_keys = request.get("resource_keys") or request.get("resourceKeys")
    if isinstance(resource_keys, list):
        summary["resourceKeys"] = [str(item)[:120] for item in resource_keys[:20]]
    return {key: value for key, value in summary.items() if value not in (None, "", {}, [])}


def _target_summary(target: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in target.items():
        if str(key) in {"instructions", "prompt", "message", "rawDiff", "diff", "patch"} and isinstance(value, str):
            result[str(key) + "Summary"] = _text_summary(value, include_preview=False)
            continue
        if isinstance(value, str) and len(value) > 240:
            result[str(key) + "Summary"] = _text_summary(value, include_preview=False)
            continue
        result[str(key)] = value
    return result


def _runtime_policy_summary(request: dict[str, Any]) -> dict[str, Any]:
    policy = request.get("_runtime_policy")
    if isinstance(policy, dict):
        return {
            key: policy.get(key)
            for key in (
                "requestedSandbox",
                "effectiveSandbox",
                "approvalPolicy",
                "runtimePolicyAdjusted",
                "source",
            )
            if policy.get(key) is not None
        }
    result = {
        "sandbox": request.get("sandbox"),
        "approvalPolicy": request.get("approval_policy") or request.get("approvalPolicy"),
        "model": request.get("model"),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _id_ref(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _text_summary(value: str, *, include_preview: bool = True) -> dict[str, Any]:
    text = value.strip()
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    result = {
        "chars": len(text),
        "sha256": digest,
        "truncated": len(text) > 160,
    }
    if include_preview:
        result["preview"] = _preview(text)
    return result


def _preview(value: str, *, max_chars: int = 160) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _looks_like_public_status_text_context(payload: dict[str, Any]) -> bool:
    return any(
        key in payload
        for key in (
            "operationId",
            "operation_id",
            "workflowId",
            "workflow_id",
            "threadId",
            "thread_id",
            "turnId",
            "turn_id",
            "actionId",
            "action_id",
        )
    )


def _compact_titles_and_thread_state(payload: Any) -> None:
    if isinstance(payload, dict):
        thread_state = payload.get("threadState")
        if isinstance(thread_state, dict):
            title = thread_state.get("title")
            if isinstance(title, str) and title:
                thread_state["titleSummary"] = _text_summary(title, include_preview=False)
                thread_state.pop("title", None)
        title = payload.get("title")
        if isinstance(title, str) and title and _looks_like_public_status_text_context(payload):
            payload["titleSummary"] = _text_summary(title, include_preview=False)
            payload.pop("title", None)
        for value in payload.values():
            _compact_titles_and_thread_state(value)
    elif isinstance(payload, list):
        for item in payload:
            _compact_titles_and_thread_state(item)


def _dedup_message_blocks(payload: dict[str, Any]) -> None:
    for key in ("latestMessages", "last_messages", "messages"):
        if isinstance(payload.get(key), list):
            payload[key] = _dedup_messages(payload[key])
    for value in payload.values():
        if isinstance(value, dict):
            _dedup_message_blocks(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _dedup_message_blocks(item)


def _dedup_messages(messages: list[Any]) -> list[Any]:
    seen: set[tuple[str, str, str, str]] = set()
    result: list[Any] = []
    for item in messages:
        if not isinstance(item, dict):
            result.append(item)
            continue
        text = str(item.get("text") or "").strip()
        role = str(item.get("role") or "")
        if role == "assistant" and not text:
            continue
        created = str(item.get("createdAt") or item.get("created_at") or "")[:19]
        key = (str(item.get("turnId") or item.get("turn_id") or ""), role, text, created)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _specialize_workflow_payload(payload: dict[str, Any]) -> None:
    plan_operation = payload.get("planOperation")
    if isinstance(plan_operation, dict):
        _clear_plan_final_fields(plan_operation)
        if plan_operation.get("nextRecommendedAction") == "read_final_report":
            plan_operation["nextRecommendedAction"] = "read_plan_artifact"
    plan_turn = payload.get("planTurn")
    if isinstance(plan_turn, dict):
        _clear_plan_final_fields(plan_turn)
    latest_plan = payload.get("latestPlan")
    if isinstance(latest_plan, dict):
        payload.setdefault("planArtifactSummary", {
            "available": True,
            "status": latest_plan.get("status"),
            "planQuality": latest_plan.get("planQuality") or latest_plan.get("quality"),
            "planHash": latest_plan.get("planHash") or latest_plan.get("hash"),
        })


def _clear_plan_final_fields(value: dict[str, Any]) -> None:
    value.pop("finalReport", None)
    value.pop("final_report", None)
    value["finalMessage"] = None
    value["final_message"] = None
    turn_status = value.get("turnStatus")
    if isinstance(turn_status, dict):
        turn_status.pop("finalReport", None)
        turn_status.pop("final_report", None)
        turn_status["finalMessage"] = None
        turn_status["final_message"] = None
