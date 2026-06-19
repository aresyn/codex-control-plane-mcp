from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def normalize_public_status_payload(payload: dict[str, Any], *, surface: str) -> dict[str, Any]:
    normalized = _normalize_value(payload)
    if isinstance(normalized, dict):
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
    debt = payload.get("historicalDebt")
    if not isinstance(debt, dict):
        return
    for key in ("staleOperations", "prematureTerminalOperations", "staleWorkflowRefs", "staleTurnRefs"):
        items = debt.get(key)
        if isinstance(items, list) and len(items) > 5:
            debt[key + "Count"] = debt.get(key + "Count") or len(items)
            debt[key + "Refs"] = [_compact_ref(item) for item in items[:5] if isinstance(item, dict)]
            debt[key] = []
            debt["detailsTruncated"] = True


def _compact_ref(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in ("operationId", "operation_id", "workflowId", "workflow_id", "threadId", "thread_id", "turnId", "turn_id", "status")
        if item.get(key) is not None
    }


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
        plan_operation.pop("finalReport", None)
        plan_operation["finalMessage"] = None
        plan_operation["final_message"] = None
        if plan_operation.get("nextRecommendedAction") == "read_final_report":
            plan_operation["nextRecommendedAction"] = "read_plan_artifact"
    latest_plan = payload.get("latestPlan")
    if isinstance(latest_plan, dict):
        payload.setdefault("planArtifactSummary", {
            "available": True,
            "status": latest_plan.get("status"),
            "planQuality": latest_plan.get("planQuality") or latest_plan.get("quality"),
            "planHash": latest_plan.get("planHash") or latest_plan.get("hash"),
        })
