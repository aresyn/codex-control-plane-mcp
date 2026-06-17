from __future__ import annotations

import json
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import clean_windows_path
from .errors import CodexMcpError


@dataclass(slots=True)
class CodexThreadRow:
    thread_id: str
    rollout_path: str
    cwd: str
    title: str
    preview: str
    created_at: str | None
    updated_at: str | None
    archived: bool
    source: str | None
    thread_source: str | None
    model: str | None
    reasoning_effort: str | None
    sandbox_policy: dict[str, Any] | None
    approval_mode: str | None


def ms_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return datetime.fromtimestamp(numeric / 1000.0, timezone.utc).isoformat()


class CodexStateReader:
    def __init__(self, state_db_path: Path) -> None:
        self.state_db_path = state_db_path

    def list_threads(self) -> list[CodexThreadRow]:
        if not self.state_db_path.exists():
            return []
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{self.state_db_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                """
                SELECT id, rollout_path, cwd, title, preview, created_at_ms, updated_at_ms,
                       archived, source, thread_source, model, reasoning_effort,
                       sandbox_policy, approval_mode
                FROM threads
                ORDER BY updated_at_ms DESC
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise CodexMcpError(
                "CODEX_INTERNAL_STATE_READ_ERROR",
                f"Failed to read Codex state database: {exc}",
                {"path": str(self.state_db_path)},
                retryable=True,
            ) from exc
        finally:
            with suppress(Exception):
                if connection is not None:
                    connection.close()

        result: list[CodexThreadRow] = []
        for row in rows:
            sandbox_policy = None
            raw_sandbox = row["sandbox_policy"]
            if isinstance(raw_sandbox, str) and raw_sandbox.strip():
                try:
                    sandbox_policy = json.loads(raw_sandbox)
                except json.JSONDecodeError:
                    sandbox_policy = {"raw": raw_sandbox}
            result.append(
                CodexThreadRow(
                    thread_id=str(row["id"]),
                    rollout_path=clean_windows_path(row["rollout_path"]),
                    cwd=clean_windows_path(row["cwd"]),
                    title=str(row["title"] or ""),
                    preview=str(row["preview"] or ""),
                    created_at=ms_to_iso(row["created_at_ms"]),
                    updated_at=ms_to_iso(row["updated_at_ms"]),
                    archived=bool(row["archived"]),
                    source=row["source"],
                    thread_source=row["thread_source"],
                    model=row["model"],
                    reasoning_effort=row["reasoning_effort"],
                    sandbox_policy=sandbox_policy,
                    approval_mode=row["approval_mode"],
                )
            )
        return result
