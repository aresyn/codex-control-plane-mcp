from __future__ import annotations

from . import storage as _storage

globals().update(_storage.__dict__)


class TurnStoreMixin:
    def upsert_tracked_turn(self, row: dict[str, Any]) -> None:
        payload = {
            "accepted_at": row.get("accepted_at") or row.get("started_at"),
            "request_id": row.get("request_id"),
            "process_generation": row.get("process_generation"),
            "last_event_seq": int(row.get("last_event_seq") or 0),
            "last_assistant_message": row.get("last_assistant_message"),
            "clear_last_error": int(bool(row.get("clear_last_error"))),
            **row,
        }
        self.connection.execute(
            """
            INSERT INTO tracked_turns(
              turn_id, thread_id, chat_id, project_id, project_path, status,
              started_at, updated_at, completed_at, first_message_at,
              final_message, last_assistant_message, last_error, accepted_at, request_id,
              process_generation, last_event_seq, source
            )
            VALUES(
              :turn_id, :thread_id, :chat_id, :project_id, :project_path, :status,
              :started_at, :updated_at, :completed_at, :first_message_at,
              :final_message, :last_assistant_message, :last_error, :accepted_at, :request_id,
              :process_generation, :last_event_seq, :source
            )
            ON CONFLICT(turn_id) DO UPDATE SET
              thread_id=excluded.thread_id,
              chat_id=COALESCE(excluded.chat_id, tracked_turns.chat_id),
              project_id=COALESCE(excluded.project_id, tracked_turns.project_id),
              project_path=COALESCE(excluded.project_path, tracked_turns.project_path),
              status=excluded.status,
              started_at=COALESCE(tracked_turns.started_at, excluded.started_at),
              updated_at=excluded.updated_at,
              completed_at=COALESCE(excluded.completed_at, tracked_turns.completed_at),
              first_message_at=COALESCE(tracked_turns.first_message_at, excluded.first_message_at),
              final_message=COALESCE(excluded.final_message, tracked_turns.final_message),
              last_assistant_message=COALESCE(excluded.last_assistant_message, tracked_turns.last_assistant_message),
              accepted_at=COALESCE(tracked_turns.accepted_at, excluded.accepted_at),
              request_id=COALESCE(excluded.request_id, tracked_turns.request_id),
              process_generation=COALESCE(excluded.process_generation, tracked_turns.process_generation),
              last_event_seq=MAX(tracked_turns.last_event_seq, excluded.last_event_seq),
              last_error=CASE
                WHEN :clear_last_error THEN NULL
                ELSE COALESCE(excluded.last_error, tracked_turns.last_error)
              END,
              source=excluded.source
            """,
            payload,
        )
        self.connection.commit()

    def update_tracked_turn_status(
        self,
        turn_id: str,
        *,
        status: str,
        updated_at: str,
        completed_at: str | None = None,
        final_message: str | None = None,
        last_assistant_message: str | None = None,
        last_error: str | None = None,
        clear_last_error: bool = False,
    ) -> None:
        self.connection.execute(
            """
            UPDATE tracked_turns SET
              status = ?,
              updated_at = ?,
              completed_at = COALESCE(?, completed_at),
              final_message = COALESCE(?, final_message),
              last_assistant_message = COALESCE(?, last_assistant_message),
              last_error = CASE
                WHEN ? THEN NULL
                ELSE COALESCE(?, last_error)
              END
            WHERE turn_id = ?
            """,
            (
                status,
                updated_at,
                completed_at,
                final_message,
                last_assistant_message,
                int(clear_last_error),
                last_error,
                turn_id,
            ),
        )
        self.connection.commit()

    def record_tracked_turn_message(self, row: dict[str, Any]) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO tracked_turn_messages(
              event_hash, turn_id, thread_id, role, text, created_at, sequence,
              event_type, payload_json
            )
            VALUES(
              :event_hash, :turn_id, :thread_id, :role, :text, :created_at,
              :sequence, :event_type, :payload_json
            )
            """,
            row,
        )
        inserted = cursor.rowcount > 0
        if inserted:
            event_id = cursor.lastrowid
            self.connection.execute(
                """
                UPDATE tracked_turns SET
                  updated_at = COALESCE(:created_at, updated_at),
                  first_message_at = COALESCE(first_message_at, :created_at),
                  last_assistant_message = :text,
                  last_event_seq = MAX(last_event_seq, :event_id)
                WHERE turn_id = :turn_id
                """,
                {**row, "event_id": event_id},
            )
        self.connection.commit()
        return inserted

    def get_tracked_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tracked_turns WHERE turn_id = ? LIMIT 1",
            (turn_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_latest_tracked_turn_for_thread(self, thread_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM tracked_turns
            WHERE thread_id = ?
            ORDER BY updated_at DESC, started_at DESC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_tracked_turns_for_thread(self, thread_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_turns
            WHERE thread_id = ?
            ORDER BY COALESCE(started_at, updated_at), turn_id
            LIMIT ?
            """,
            (thread_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_running_tracked_turns(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_turns
            WHERE status IN ('starting', 'running')
               OR status IN ('accepted', 'started', 'first_message_received', 'waiting_for_approval', 'waiting_for_user_input')
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_last_tracked_turn_messages(self, turn_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_turn_messages
            WHERE turn_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (turn_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def count_tracked_turn_messages(self, turn_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM tracked_turn_messages WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def record_tracked_turn_progress_event(self, row: dict[str, Any]) -> bool:
        payload = {"severity": "info", "metadata_json": "{}", "truncated": 0, **row}
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO tracked_turn_progress_events(
              event_hash, turn_id, thread_id, event_type, category, severity,
              item_id, sequence, text, metadata_json, created_at, truncated
            )
            VALUES(
              :event_hash, :turn_id, :thread_id, :event_type, :category, :severity,
              :item_id, :sequence, :text, :metadata_json, :created_at, :truncated
            )
            """,
            payload,
        )
        inserted = cursor.rowcount > 0
        if inserted:
            event_id = cursor.lastrowid
            self.connection.execute(
                """
                UPDATE tracked_turns SET
                  updated_at = COALESCE(:created_at, updated_at),
                  last_event_seq = MAX(last_event_seq, :event_id)
                WHERE turn_id = :turn_id
                """,
                {**payload, "event_id": event_id},
            )
        self.connection.commit()
        return inserted

    def list_tracked_turn_progress_events(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        categories: list[str] | tuple[str, ...] | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        if categories:
            placeholders = ",".join("?" for _ in categories)
            clauses.append(f"category IN ({placeholders})")
            params.extend(categories)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM tracked_turn_progress_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def count_tracked_turn_progress_events(self, *, thread_id: str | None = None, turn_id: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        row = self.connection.execute(f"SELECT COUNT(*) AS count FROM tracked_turn_progress_events {where}", params).fetchone()
        return int(row["count"] if row is not None else 0)

    def tracked_turn_progress_summary(self, turn_id: str, *, warning_limit: int = 10, model_reroute_limit: int = 10) -> dict[str, Any]:
        count = self.count_tracked_turn_progress_events(turn_id=turn_id)
        latest = self.connection.execute(
            """
            SELECT created_at
            FROM tracked_turn_progress_events
            WHERE turn_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        token_usage = self.connection.execute(
            """
            SELECT *
            FROM tracked_turn_progress_events
            WHERE turn_id = ? AND category = 'token_usage'
            ORDER BY id DESC
            LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        warnings = self.list_tracked_turn_progress_events(
            turn_id=turn_id,
            categories=("warning",),
            limit=warning_limit,
        )
        model_reroutes = self.list_tracked_turn_progress_events(
            turn_id=turn_id,
            categories=("model_reroute",),
            limit=model_reroute_limit,
        )
        return {
            "eventCount": count,
            "latestProgressAt": latest["created_at"] if latest is not None else None,
            "tokenUsageEvent": dict(token_usage) if token_usage is not None else None,
            "warnings": warnings,
            "modelReroutes": model_reroutes,
        }

    def append_tracked_plan_delta(self, row: dict[str, Any]) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO tracked_plan_events(
              event_hash, item_id, turn_id, thread_id, event_type, created_at, payload_json
            )
            VALUES(
              :event_hash, :item_id, :turn_id, :thread_id, :event_type, :created_at, :payload_json
            )
            """,
            row,
        )
        inserted = cursor.rowcount > 0
        if inserted:
            self.connection.execute(
                """
                INSERT INTO tracked_plan_items(
                  item_id, turn_id, thread_id, status, text, created_at,
                  updated_at, completed_at, sequence, payload_json
                )
                VALUES(
                  :item_id, :turn_id, :thread_id, 'in_progress', :delta,
                  :created_at, :created_at, NULL, :sequence, :payload_json
                )
                ON CONFLICT(turn_id, item_id) DO UPDATE SET
                  text=tracked_plan_items.text || excluded.text,
                  status=CASE
                    WHEN tracked_plan_items.status = 'completed' THEN tracked_plan_items.status
                    ELSE 'in_progress'
                  END,
                  updated_at=excluded.updated_at,
                  sequence=MAX(tracked_plan_items.sequence, excluded.sequence),
                  payload_json=excluded.payload_json
                """,
                row,
            )
        self.connection.commit()
        return inserted

    def upsert_tracked_plan_item(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO tracked_plan_items(
              item_id, turn_id, thread_id, status, text, created_at,
              updated_at, completed_at, sequence, payload_json
            )
            VALUES(
              :item_id, :turn_id, :thread_id, :status, :text, :created_at,
              :updated_at, :completed_at, :sequence, :payload_json
            )
            ON CONFLICT(turn_id, item_id) DO UPDATE SET
              thread_id=excluded.thread_id,
              status=excluded.status,
              text=excluded.text,
              created_at=COALESCE(tracked_plan_items.created_at, excluded.created_at),
              updated_at=excluded.updated_at,
              completed_at=COALESCE(excluded.completed_at, tracked_plan_items.completed_at),
              sequence=MAX(tracked_plan_items.sequence, excluded.sequence),
              payload_json=excluded.payload_json
            """,
            row,
        )
        self.connection.commit()

    def get_tracked_turn_plans(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_plan_items
            WHERE turn_id = ?
            ORDER BY id ASC
            """,
            (turn_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_thread_plans(self, thread_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM tracked_plan_items
            WHERE thread_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (thread_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_latest_plan_for_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT *
            FROM tracked_plan_items
            WHERE turn_id = ?
            ORDER BY
              CASE status WHEN 'completed' THEN 1 ELSE 0 END DESC,
              COALESCE(completed_at, updated_at, created_at) DESC,
              id DESC
            LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        return dict(row) if row is not None else None
