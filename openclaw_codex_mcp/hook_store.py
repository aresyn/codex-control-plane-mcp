from __future__ import annotations

from . import storage as _storage

globals().update(_storage.__dict__)


class HookStoreMixin:
    def upsert_hook_thread(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO codex_hook_threads(
              thread_id, session_id, project_path, project_path_key, title,
              created_at, updated_at, transcript_path, source
            )
            VALUES(
              :thread_id, :session_id, :project_path, :project_path_key, :title,
              :created_at, :updated_at, :transcript_path, :source
            )
            ON CONFLICT(thread_id) DO UPDATE SET
              session_id=COALESCE(excluded.session_id, codex_hook_threads.session_id),
              project_path=COALESCE(excluded.project_path, codex_hook_threads.project_path),
              project_path_key=COALESCE(excluded.project_path_key, codex_hook_threads.project_path_key),
              title=COALESCE(excluded.title, codex_hook_threads.title),
              created_at=COALESCE(codex_hook_threads.created_at, excluded.created_at),
              updated_at=MAX(COALESCE(codex_hook_threads.updated_at, ''), COALESCE(excluded.updated_at, '')),
              transcript_path=COALESCE(excluded.transcript_path, codex_hook_threads.transcript_path),
              source=excluded.source
            """,
            {"source": "hook_history", **row},
        )

    def upsert_hook_turn(self, row: dict[str, Any]) -> None:
        payload = {"clear_last_error": 0, **row}
        self.connection.execute(
            """
            INSERT INTO codex_hook_turns(
              turn_id, thread_id, status, started_at, updated_at, completed_at,
              model, permission_mode, last_assistant_message, last_error
            )
            VALUES(
              :turn_id, :thread_id, :status, :started_at, :updated_at, :completed_at,
              :model, :permission_mode, :last_assistant_message, :last_error
            )
            ON CONFLICT(turn_id) DO UPDATE SET
              thread_id=excluded.thread_id,
              status=excluded.status,
              started_at=COALESCE(codex_hook_turns.started_at, excluded.started_at),
              updated_at=MAX(COALESCE(codex_hook_turns.updated_at, ''), COALESCE(excluded.updated_at, '')),
              completed_at=COALESCE(excluded.completed_at, codex_hook_turns.completed_at),
              model=COALESCE(excluded.model, codex_hook_turns.model),
              permission_mode=COALESCE(excluded.permission_mode, codex_hook_turns.permission_mode),
              last_assistant_message=COALESCE(excluded.last_assistant_message, codex_hook_turns.last_assistant_message),
              last_error=CASE
                WHEN :clear_last_error THEN NULL
                ELSE COALESCE(excluded.last_error, codex_hook_turns.last_error)
              END
            """,
            payload,
        )

    def record_hook_message(self, row: dict[str, Any]) -> bool:
        existing = self.connection.execute(
            """
            SELECT id, message_id
            FROM codex_hook_messages
            WHERE message_id = ? OR event_hash = ?
            LIMIT 1
            """,
            (row["message_id"], row["event_hash"]),
        ).fetchone()
        if existing is not None:
            row_id = int(existing["id"])
            if existing["message_id"] == row["message_id"]:
                self.connection.execute(
                    """
                    UPDATE codex_hook_messages SET
                      event_hash=:event_hash,
                      thread_id=:thread_id,
                      turn_id=:turn_id,
                      role=:role,
                      text=:text,
                      created_at=:created_at,
                      sequence=:sequence,
                      hook_event_name=:hook_event_name,
                      text_kind=:text_kind
                    WHERE id=:row_id
                    """,
                    {**row, "row_id": row_id},
                )
                self.connection.execute("DELETE FROM codex_hook_messages_fts WHERE rowid = ?", (row_id,))
                self.connection.execute(
                    """
                    INSERT INTO codex_hook_messages_fts(rowid, message_id, thread_id, turn_id, role, text)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        row["message_id"],
                        row["thread_id"],
                        row.get("turn_id"),
                        row["role"],
                        row["text"],
                    ),
                )
            return False
        cursor = self.connection.execute(
            """
            INSERT INTO codex_hook_messages(
              message_id, event_hash, thread_id, turn_id, role, text, created_at,
              sequence, hook_event_name, text_kind
            )
            VALUES(
              :message_id, :event_hash, :thread_id, :turn_id, :role, :text,
              :created_at, :sequence, :hook_event_name, :text_kind
            )
            """,
            row,
        )
        inserted = cursor.rowcount > 0
        if inserted:
            self.connection.execute(
                """
                INSERT INTO codex_hook_messages_fts(rowid, message_id, thread_id, turn_id, role, text)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    int(cursor.lastrowid),
                    row["message_id"],
                    row["thread_id"],
                    row.get("turn_id"),
                    row["role"],
                    row["text"],
                ),
            )
        return inserted

    def get_hook_thread(self, thread_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_hook_threads WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_hook_threads(self, *, limit: int = 10_000) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_hook_threads
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_hook_thread_by_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT threads.*
            FROM codex_hook_turns turns
            JOIN codex_hook_threads threads ON threads.thread_id = turns.thread_id
            WHERE turns.turn_id = ?
            LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_hook_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM codex_hook_turns WHERE turn_id = ? LIMIT 1",
            (turn_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_hook_turns(self, thread_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM codex_hook_turns
            WHERE thread_id = ?
            ORDER BY COALESCE(started_at, updated_at, completed_at), turn_id
            """,
            (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_hook_messages(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM codex_hook_messages
            {where}
            ORDER BY id ASC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_hook_messages(self, *, thread_id: str | None = None, turn_id: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        row = self.connection.execute(f"SELECT COUNT(*) AS count FROM codex_hook_messages {where}", params).fetchone()
        return int(row["count"] if row is not None else 0)

    def hook_history_status(self) -> dict[str, Any]:
        threads = self.connection.execute("SELECT COUNT(*) AS count FROM codex_hook_threads").fetchone()
        turns = self.connection.execute("SELECT COUNT(*) AS count FROM codex_hook_turns").fetchone()
        messages = self.connection.execute("SELECT COUNT(*) AS count FROM codex_hook_messages").fetchone()
        latest = self.connection.execute(
            """
            SELECT MAX(updated_at) AS updated_at
            FROM (
              SELECT updated_at FROM codex_hook_threads
              UNION ALL
              SELECT updated_at FROM codex_hook_turns
              UNION ALL
              SELECT created_at AS updated_at FROM codex_hook_messages
            )
            """
        ).fetchone()
        return {
            "threadCount": int(threads["count"] if threads is not None else 0),
            "turnCount": int(turns["count"] if turns is not None else 0),
            "messageCount": int(messages["count"] if messages is not None else 0),
            "lastHookEventAt": latest["updated_at"] if latest is not None else None,
        }
