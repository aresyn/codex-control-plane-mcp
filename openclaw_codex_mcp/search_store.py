from __future__ import annotations

from . import storage as _storage

globals().update(_storage.__dict__)


class SearchStoreMixin:
    def get_summary_cache(self, cache_key: str, used_at: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT summary_json, created_at
            FROM summary_cache
            WHERE cache_key = ?
            LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        self.connection.execute(
            "UPDATE summary_cache SET last_used_at = ? WHERE cache_key = ?",
            (used_at, cache_key),
        )
        self.connection.commit()
        payload = json.loads(row["summary_json"])
        payload["created_at"] = payload.get("created_at") or row["created_at"]
        return payload

    def upsert_summary_cache(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO summary_cache(
              cache_key, thread_id, transcript_path, transcript_size, transcript_mtime_ns,
              boundary_line, model, filter_version, summary_json, created_at, last_used_at
            )
            VALUES(
              :cache_key, :thread_id, :transcript_path, :transcript_size, :transcript_mtime_ns,
              :boundary_line, :model, :filter_version, :summary_json, :created_at, :last_used_at
            )
            ON CONFLICT(cache_key) DO UPDATE SET
              summary_json=excluded.summary_json,
              last_used_at=excluded.last_used_at
            """,
            row,
        )
        self.connection.commit()

    def has_summary_cache_for_thread(self, thread_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM summary_cache WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row is not None

    def get_rolling_summary(self, thread_id: str, transcript_path: str, before_line: int | None) -> dict[str, Any] | None:
        if before_line is None:
            return None
        row = self.connection.execute(
            """
            SELECT thread_id, transcript_path, source_line_end, summary_text, model, updated_at
            FROM rolling_summaries
            WHERE thread_id = ? AND transcript_path = ? AND source_line_end < ?
            ORDER BY source_line_end DESC
            LIMIT 1
            """,
            (thread_id, transcript_path, before_line),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_rolling_summary(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO rolling_summaries(thread_id, transcript_path, source_line_end, summary_text, model, updated_at)
            VALUES(:thread_id, :transcript_path, :source_line_end, :summary_text, :model, :updated_at)
            ON CONFLICT(thread_id) DO UPDATE SET
              transcript_path=excluded.transcript_path,
              source_line_end=excluded.source_line_end,
              summary_text=excluded.summary_text,
              model=excluded.model,
              updated_at=excluded.updated_at
            """,
            row,
        )
        self.connection.commit()

    def record_budget_audit(self, tool_name: str, thread_id: str | None, budget: dict[str, Any], created_at: str) -> None:
        self.connection.execute(
            """
            INSERT INTO budget_audit(tool_name, thread_id, budget_json, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (tool_name, thread_id, json.dumps(budget, ensure_ascii=False), created_at),
        )
        self.connection.commit()
