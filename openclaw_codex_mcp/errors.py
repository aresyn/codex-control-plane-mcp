from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CodexMcpError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "retryable": self.retryable,
            }
        }


def invalid_argument(message: str, **details: Any) -> CodexMcpError:
    return CodexMcpError("INVALID_ARGUMENT", message, details, retryable=False)


def thread_not_found(thread_id: str) -> CodexMcpError:
    return CodexMcpError(
        "CODEX_THREAD_NOT_FOUND",
        f"Codex thread was not found: {thread_id}",
        {"thread_id": thread_id},
    )


def turn_not_found(turn_id: str) -> CodexMcpError:
    return CodexMcpError(
        "CODEX_TURN_NOT_FOUND",
        f"Codex turn was not found: {turn_id}",
        {"turn_id": turn_id},
    )


def project_not_found(project_id: str) -> CodexMcpError:
    return CodexMcpError(
        "CODEX_PROJECT_NOT_FOUND",
        f"Codex project was not found: {project_id}",
        {"project_id": project_id},
    )


def transcript_not_found(chat_id: str) -> CodexMcpError:
    return CodexMcpError(
        "CODEX_TRANSCRIPT_NOT_FOUND",
        f"Transcript was not found for chat: {chat_id}",
        {"chat_id": chat_id},
        retryable=True,
    )


def app_server_unavailable(message: str) -> CodexMcpError:
    return CodexMcpError(
        "CODEX_APP_SERVER_UNAVAILABLE",
        message,
        retryable=True,
    )


def send_failed(message: str, **details: Any) -> CodexMcpError:
    return CodexMcpError("CODEX_SEND_FAILED", message, details, retryable=False)


def timeout(message: str, **details: Any) -> CodexMcpError:
    return CodexMcpError("CODEX_TIMEOUT", message, details, retryable=True)


def summary_failed(message: str, **details: Any) -> CodexMcpError:
    return CodexMcpError("CODEX_SUMMARY_FAILED", message, details, retryable=True)


def busy(thread_id: str, status: str) -> CodexMcpError:
    return CodexMcpError(
        "CODEX_BUSY",
        f"Codex thread is active and v1 does not queue writes: {thread_id}",
        {"thread_id": thread_id, "status": status},
        retryable=True,
    )


def duplicate_prompt_active(**details: Any) -> CodexMcpError:
    return CodexMcpError(
        "CODEX_DUPLICATE_PROMPT_ACTIVE",
        "A similar prompt already has an active Codex turn in this project.",
        details,
        retryable=False,
    )


def app_server_busy(message: str, **details: Any) -> CodexMcpError:
    return CodexMcpError("CODEX_BUSY", message, details, retryable=True)


def pending_interaction_not_found(interaction_id: str) -> CodexMcpError:
    return CodexMcpError(
        "CODEX_PENDING_INTERACTION_NOT_FOUND",
        f"Pending Codex interaction was not found: {interaction_id}",
        {"interaction_id": interaction_id},
    )


def pending_interaction_unavailable(message: str, **details: Any) -> CodexMcpError:
    return CodexMcpError("CODEX_PENDING_INTERACTION_UNAVAILABLE", message, details, retryable=True)
