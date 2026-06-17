from __future__ import annotations

import json
from typing import Any


GENERIC_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ok"],
    "properties": {
        "ok": {"type": "boolean"},
        "error": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "details": {"type": "object"},
                "retryable": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
    },
    "additionalProperties": True,
}


def with_output_schema(tool: dict[str, Any]) -> dict[str, Any]:
    tool.setdefault("outputSchema", GENERIC_OUTPUT_SCHEMA)
    return tool


def normalize_tool_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        error = dict(result["error"])
        return {
            "ok": False,
            "error": {
                "code": str(error.get("code") or "ERROR"),
                "message": str(error.get("message") or "Unknown error"),
                "details": error.get("details") if isinstance(error.get("details"), dict) else {},
                "retryable": bool(error.get("retryable", False)),
            },
        }

    if isinstance(result, dict):
        payload = dict(result)
    else:
        payload = {"result": result}
    payload.setdefault("ok", True)
    return payload


def call_tool_result(result: Any) -> dict[str, Any]:
    structured = normalize_tool_payload(result)
    is_error = not bool(structured.get("ok"))
    return {
        "isError": is_error,
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False, indent=2)}],
        "structuredContent": structured,
    }
