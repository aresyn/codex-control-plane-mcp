from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openclaw_codex_mcp.transcripts import infer_transcript_tail_status, parse_transcript, read_session_meta


class TranscriptTests(unittest.TestCase):
    def test_parse_messages_turns_and_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-test.jsonl"
            rows = [
                {
                    "timestamp": "2026-05-24T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "thread-1", "timestamp": "2026-05-24T10:00:00Z", "cwd": r"D:\Projects\Example"},
                },
                {
                    "timestamp": "2026-05-24T10:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn-1", "model": "gpt-5.5", "approval_policy": "never", "sandbox_policy": {"type": "read-only"}},
                },
                {
                    "timestamp": "2026-05-24T10:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                },
                {
                    "timestamp": "2026-05-24T10:00:03Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                },
                {
                    "timestamp": "2026-05-24T10:00:04Z",
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "tool_a", "arguments": "{\"x\":1}"},
                },
                {
                    "timestamp": "2026-05-24T10:00:05Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "world"}]},
                },
                {
                    "timestamp": "2026-05-24T10:00:06Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-1"},
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            meta = read_session_meta(path)
            self.assertEqual(meta["thread_id"], "thread-1")

            parsed = parse_transcript(path, include_tool_calls=True)
            self.assertEqual(parsed.thread_id, "thread-1")
            self.assertEqual(parsed.project_path, r"D:\Projects\Example")
            self.assertEqual(parsed.turns["turn-1"].status, "completed")
            self.assertEqual([item.role for item in parsed.messages], ["user", "tool", "assistant"])
            self.assertEqual(parsed.messages[0].text, "hello")

    def test_infer_tail_status_without_full_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-running.jsonl"
            rows = [
                {
                    "timestamp": "2026-05-24T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "thread-1", "timestamp": "2026-05-24T10:00:00Z", "cwd": r"D:\Projects\Example"},
                },
                {"timestamp": "2026-05-24T10:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1"}},
                {"timestamp": "2026-05-24T10:00:02Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            status, confidence, evidence = infer_transcript_tail_status(path)

            self.assertEqual(status, "running")
            self.assertEqual(confidence, "medium")
            self.assertTrue(evidence)


if __name__ == "__main__":
    unittest.main()
