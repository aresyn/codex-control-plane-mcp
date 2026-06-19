from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .config import is_path_under


_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\[^\s\"'<>|]+|\\\\[^\s\"'<>|]+)")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_TOKEN_RE = re.compile(r"(?i)\b(?:sk|sess|tok|key|bearer)[-_A-Za-z0-9.]{12,}\b")
_SECRET_KEYS = {
    "email",
    "accountid",
    "account_id",
    "userid",
    "user_id",
    "token",
    "apikey",
    "api_key",
    "authorization",
    "bearer",
    "secret",
    "password",
}
_SAFE_TOKEN_KEYS = {
    "tokenbudget",
    "token_budget",
    "goaltokenbudget",
    "goal_token_budget",
}
_PATH_KEY_MARKERS = (
    "path",
    "dir",
    "file",
    "codexhome",
    "sessions",
    "statedb",
    "logsdb",
    "binary",
    "transcript",
)


class AgentSafeRedactor:
    """Privacy boundary for agent-facing MCP responses.

    The redactor is intentionally conservative for public/status surfaces:
    it preserves paths under configured allowed roots, but collapses Codex
    home, state DB, logs, app binaries, profile paths, and token-like text.
    """

    def __init__(self, *, allowed_roots: list[Path], private_roots: list[Path]) -> None:
        self.allowed_roots = [Path(root) for root in allowed_roots]
        self.private_roots = [Path(root) for root in private_roots if str(root)]
        self.user_profile = os.environ.get("USERPROFILE") or str(Path.home())

    def redact(self, value: Any, *, mode: str = "agent_safe") -> Any:
        if mode == "raw_audit":
            return self._redact_secrets(value)
        return self._redact_agent_safe(value)

    def _redact_agent_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._redact_key_value(str(key), item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_agent_safe(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def _redact_key_value(self, key: str, value: Any) -> Any:
        key_norm = key.replace("-", "_").casefold()
        if key_norm.replace("_", "") in _SAFE_TOKEN_KEYS or key_norm in _SAFE_TOKEN_KEYS:
            return value
        if key_norm in _SECRET_KEYS or any(marker in key_norm for marker in ("token", "secret", "password", "authorization")):
            if value in (None, "", False):
                return value
            return "[redacted]"
        if isinstance(value, str) and any(marker in key_norm for marker in _PATH_KEY_MARKERS):
            return self._redact_path_text(value)
        if key_norm == "tokenusage" and isinstance(value, dict):
            return _coarse_token_usage(value)
        return self._redact_agent_safe(value)

    def _redact_secrets(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._redact_key_value(str(key), item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_secrets(item) for item in value]
        if isinstance(value, str):
            return _TOKEN_RE.sub("[redacted-token]", _EMAIL_RE.sub("[redacted-email]", value))
        return value

    def _redact_text(self, text: str) -> str:
        text = _TOKEN_RE.sub("[redacted-token]", _EMAIL_RE.sub("[redacted-email]", text))
        return _WINDOWS_PATH_RE.sub(lambda match: self._redact_path_text(match.group(0)), text)

    def _redact_path_text(self, text: str) -> str:
        if not text or not _looks_like_path(text):
            return self._redact_text(text) if _WINDOWS_PATH_RE.search(text) else text
        if self._is_public_allowed_path(text) and not self._is_private_path(text):
            return text
        return _path_ref(text)

    def _is_public_allowed_path(self, text: str) -> bool:
        try:
            return any(is_path_under(text, root) for root in self.allowed_roots)
        except Exception:
            return False

    def _is_private_path(self, text: str) -> bool:
        lowered = text.casefold()
        if ".codex" in lowered or "\\appdata\\" in lowered:
            return True
        try:
            if self.user_profile and is_path_under(text, self.user_profile):
                return True
            return any(is_path_under(text, root) for root in self.private_roots)
        except Exception:
            return True


def _looks_like_path(text: str) -> bool:
    return bool(_WINDOWS_PATH_RE.search(text) or "/" in text or "\\" in text)


def _path_ref(text: str) -> str:
    normalized = text.replace("/", "\\")
    name = Path(normalized).name or "path"
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"[redacted-path:{name}:{digest}]"


def _coarse_token_usage(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("precision") == "coarse":
        return dict(value)
    counts: list[int] = []
    for item in value.values():
        if isinstance(item, int):
            counts.append(item)
        elif isinstance(item, dict):
            for nested in item.values():
                if isinstance(nested, int):
                    counts.append(nested)
    total = max(counts) if counts else None
    if total is None:
        band = "unknown"
    elif total < 1_000:
        band = "<1k"
    elif total < 10_000:
        band = "1k-10k"
    elif total < 100_000:
        band = "10k-100k"
    else:
        band = "100k+"
    return {"available": bool(counts), "precision": "coarse", "totalTokensBand": band}
