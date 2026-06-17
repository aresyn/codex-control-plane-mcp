from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openclaw_codex_mcp.chat_summarizer import (
    chunk_text,
    filter_meaningful_messages_for_summary,
    limit_recent_messages_for_single_request,
    redact_sensitive_text,
    split_before_latest_user,
    summarize_chat_history,
)
from openclaw_codex_mcp.config import ServerConfig
from openclaw_codex_mcp.deepseek_client import load_deepseek_settings, read_dotenv
from openclaw_codex_mcp.models import TranscriptMessage


def msg(role: str, text: str, line: int, turn_id: str | None = None) -> TranscriptMessage:
    return TranscriptMessage(
        message_id=f"{role}-{line}",
        thread_id="thread-1",
        turn_id=turn_id,
        role=role,
        created_at=f"2026-05-25T00:00:{line:02d}Z",
        text=text,
        items=[{"type": "text", "text": text, "metadata": {}}],
        metadata={},
        source_line_start=line,
        source_line_end=line,
    )


class FakeSummaryClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def chat_completion(self, messages: list[dict[str, str]], *, max_tokens: int, temperature: float):
        self.calls.append(messages)
        return SimpleNamespace(content="Краткое summary", model="deepseek-v4-flash")


class ChatSummarizerTests(unittest.TestCase):
    def test_split_before_latest_user(self) -> None:
        messages = [
            msg("user", "first", 1),
            msg("assistant", "answer", 2),
            msg("user", "latest", 3),
            msg("assistant", "tail", 4),
            msg("tool", "tool tail", 5),
        ]

        split = split_before_latest_user(messages)

        self.assertEqual(["first", "answer"], [item.text for item in split.upper])
        self.assertEqual(["latest", "tail", "tool tail"], [item.text for item in split.lower])
        self.assertEqual(2, split.latest_user_index)

    def test_split_without_user_summarizes_selected_range(self) -> None:
        messages = [msg("assistant", "answer", 1), msg("tool", "tool", 2)]

        split = split_before_latest_user(messages)

        self.assertEqual(messages, split.upper)
        self.assertEqual([], split.lower)
        self.assertIsNone(split.latest_user_index)

    def test_summarize_redacts_secrets_before_deepseek(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "DEEPSEEK_API_KEY=test-key\nDEEPSEEK_SUMMARY_MODEL=deepseek-v4-flash\n",
                encoding="utf-8",
            )
            config = ServerConfig(
                deepseek_env_path=env_path,
                deepseek_small_history_message_limit=0,
                deepseek_small_history_chars=0,
            )
            client = FakeSummaryClient()

            result = summarize_chat_history(
                [msg("user", "token=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ", 1)],
                config,
                client=client,
            )

            self.assertEqual("ok", result.status)
            self.assertEqual("Краткое summary", result.text)
            prompt = client.calls[0][1]["content"]
            self.assertNotIn("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ", prompt)
            self.assertIn("token=[redacted]", prompt)

    def test_summarize_sends_only_meaningful_messages_to_deepseek(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            config = ServerConfig(
                deepseek_env_path=env_path,
                deepseek_small_history_message_limit=0,
                deepseek_small_history_chars=0,
            )
            client = FakeSummaryClient()
            messages = [
                msg("user", "meaningful user request", 1),
                msg("tool", "command execution log should not be sent", 2),
                msg("event", "task_started", 3),
                msg("assistant", "meaningful assistant response", 4),
            ]

            result = summarize_chat_history(messages, config, client=client)

            self.assertEqual("ok", result.status)
            self.assertEqual(4, result.messages_seen)
            self.assertEqual(2, result.messages_summarized)
            self.assertEqual(2, result.messages_filtered_out)
            self.assertEqual(0, result.messages_omitted_due_to_limit)
            self.assertEqual(1, len(client.calls))
            prompt = client.calls[0][1]["content"]
            self.assertIn("meaningful user request", prompt)
            self.assertIn("meaningful assistant response", prompt)
            self.assertNotIn("command execution log should not be sent", prompt)
            self.assertNotIn("task_started", prompt)

    def test_meaningful_filter_excludes_tool_and_event_records(self) -> None:
        messages = [
            msg("user", "user", 1),
            msg("assistant", "assistant", 2),
            msg("tool", "tool call", 3),
            msg("event", "event", 4),
            msg("system", "", 5),
        ]

        filtered = filter_meaningful_messages_for_summary(messages)

        self.assertEqual(["user", "assistant"], [item.role for item in filtered])

    def test_summary_uses_recent_meaningful_message_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            config = ServerConfig(
                deepseek_env_path=env_path,
                deepseek_recent_messages_limit=3,
                deepseek_max_input_chars_per_chunk=60_000,
                deepseek_small_history_message_limit=0,
                deepseek_small_history_chars=0,
            )
            client = FakeSummaryClient()
            messages = [msg("user", f"message-{index}", index) for index in range(1, 8)]

            result = summarize_chat_history(messages, config, client=client)

            self.assertEqual("ok", result.status)
            self.assertEqual(7, result.messages_seen)
            self.assertEqual(3, result.messages_summarized)
            self.assertEqual(4, result.messages_omitted_due_to_limit)
            self.assertEqual(1, result.chunks)
            self.assertEqual(1, len(client.calls))
            prompt = client.calls[0][1]["content"]
            self.assertNotIn("message-1", prompt)
            self.assertIn("message-5", prompt)
            self.assertIn("message-7", prompt)

    def test_recent_message_limiter_obeys_count_limit(self) -> None:
        messages = [msg("assistant", f"message-{index}", index) for index in range(1, 6)]

        limited = limit_recent_messages_for_single_request(messages, max_messages=2, max_chars=60_000)

        self.assertEqual(["message-4", "message-5"], [item.text for item in limited])

    def test_missing_key_returns_failed_summary_without_raw_upper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = ServerConfig(
                deepseek_env_path=Path(tmp) / ".env",
                deepseek_small_history_message_limit=0,
                deepseek_small_history_chars=0,
            )

            result = summarize_chat_history([msg("user", "upper", 1)], config)

            self.assertEqual("failed", result.status)
            self.assertEqual("", result.text)
            self.assertTrue(result.warnings)

    def test_small_history_skip_avoids_deepseek_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("DEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
            config = ServerConfig(deepseek_env_path=env_path)
            client = FakeSummaryClient()

            result = summarize_chat_history([msg("user", "short upper", 1)], config, client=client)

            self.assertEqual("skipped_small_history", result.status)
            self.assertIn("short upper", result.text)
            self.assertEqual(0, result.deepseek_calls)
            self.assertEqual([], client.calls)

    def test_dotenv_settings_are_loaded_without_secret_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "DEEPSEEK_API_KEY='secret-value'\n"
                "DEEPSEEK_BASE_URL=https://api.deepseek.com\n"
                "DEEPSEEK_SUMMARY_MODEL=deepseek-v4-flash\n"
                "DEEPSEEK_TIMEOUT_SECONDS=7\n"
                "DEEPSEEK_MAX_RETRIES=3\n",
                encoding="utf-8",
            )

            values = read_dotenv(env_path)
            settings = load_deepseek_settings(
                ServerConfig(deepseek_env_path=env_path, deepseek_summary_max_retries_cap=3)
            )

            self.assertEqual("secret-value", values["DEEPSEEK_API_KEY"])
            self.assertTrue(settings.api_key_present)
            self.assertEqual("deepseek-v4-flash", settings.model)
            self.assertEqual("https://api.deepseek.com/chat/completions", settings.chat_completions_url)
            self.assertEqual(7, settings.timeout_seconds)
            self.assertEqual(3, settings.max_retries)

    def test_deepseek_timeout_and_retry_caps_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "DEEPSEEK_API_KEY='secret-value'\n"
                "DEEPSEEK_TIMEOUT_SECONDS=600\n"
                "DEEPSEEK_MAX_RETRIES=5\n",
                encoding="utf-8",
            )

            settings = load_deepseek_settings(
                ServerConfig(
                    deepseek_env_path=env_path,
                    deepseek_summary_timeout_cap_seconds=12,
                    deepseek_summary_max_retries_cap=1,
                )
            )

            self.assertEqual(12, settings.timeout_seconds)
            self.assertEqual(1, settings.max_retries)

    def test_chunking_and_redaction_helpers(self) -> None:
        chunks = chunk_text("a" * 25_000, max_chars=10_000)

        self.assertEqual(3, len(chunks))
        self.assertIn("api_key=[redacted]", redact_sensitive_text("api_key=abc123"))


if __name__ == "__main__":
    unittest.main()
