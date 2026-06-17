from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from .config import ServerConfig
from .deepseek_client import DeepSeekClient, DeepSeekClientError, load_deepseek_settings
from .logging_utils import get_logger
from .models import TranscriptMessage


LOG = get_logger("chat_summarizer")
MAX_RENDERED_MESSAGE_CHARS = 12_000
MEANINGFUL_SUMMARY_ROLES = {"user", "assistant", "system"}


class SummaryClient(Protocol):
    def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ):
        ...


@dataclass(slots=True)
class ChatHistorySplit:
    upper: list[TranscriptMessage]
    lower: list[TranscriptMessage]
    latest_user_index: int | None


@dataclass(slots=True)
class HistorySummaryResult:
    mode: str = "before_latest_user"
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    status: str = "skipped"
    text: str = ""
    messages_seen: int = 0
    messages_summarized: int = 0
    messages_filtered_out: int = 0
    messages_omitted_due_to_limit: int = 0
    messages_omitted_due_to_cache_or_rollup: int = 0
    estimated_chars_sent_to_deepseek: int = 0
    deepseek_calls: int = 0
    cache_hit: bool = False
    cache_key: str | None = None
    created_at: str | None = None
    rolling_summary_used: bool = False
    source_line_start: int | None = None
    source_line_end: int | None = None
    warnings: list[str] = field(default_factory=list)
    chunks: int = 0

    def to_tool(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "text": self.text,
            "messages_seen": self.messages_seen,
            "messages_summarized": self.messages_summarized,
            "messages_filtered_out": self.messages_filtered_out,
            "messages_omitted_due_to_limit": self.messages_omitted_due_to_limit,
            "messages_omitted_due_to_cache_or_rollup": self.messages_omitted_due_to_cache_or_rollup,
            "estimated_chars_sent_to_deepseek": self.estimated_chars_sent_to_deepseek,
            "deepseek_calls": self.deepseek_calls,
            "cache_hit": self.cache_hit,
            "cache_key": self.cache_key,
            "created_at": self.created_at,
            "rolling_summary_used": self.rolling_summary_used,
            "source_line_start": self.source_line_start,
            "source_line_end": self.source_line_end,
            "warnings": self.warnings,
            "chunks": self.chunks,
        }


def split_before_latest_user(messages: list[TranscriptMessage]) -> ChatHistorySplit:
    latest_user_index: int | None = None
    for index, message in enumerate(messages):
        if message.role == "user":
            latest_user_index = index
    if latest_user_index is None:
        return ChatHistorySplit(upper=messages, lower=[], latest_user_index=None)
    return ChatHistorySplit(
        upper=messages[:latest_user_index],
        lower=messages[latest_user_index:],
        latest_user_index=latest_user_index,
    )


def summarize_chat_history(
    messages: list[TranscriptMessage],
    config: ServerConfig,
    *,
    client: SummaryClient | None = None,
) -> HistorySummaryResult:
    settings = load_deepseek_settings(config)
    meaningful_messages = filter_meaningful_messages_for_summary(messages)
    limited_messages = limit_recent_messages_for_single_request(
        meaningful_messages,
        max_messages=config.deepseek_recent_messages_limit,
        max_chars=config.deepseek_max_input_chars_per_chunk,
    )
    result = HistorySummaryResult(
        model=settings.model,
        messages_seen=len(messages),
        messages_summarized=len(limited_messages),
        messages_filtered_out=len(messages) - len(meaningful_messages),
        messages_omitted_due_to_limit=len(meaningful_messages) - len(limited_messages),
        source_line_start=_first_line(limited_messages),
        source_line_end=_last_line(limited_messages),
    )
    if not messages:
        result.status = "skipped"
        result.warnings.append("No history exists before the latest user/orchestrator message.")
        return result
    if not meaningful_messages:
        result.status = "skipped"
        result.warnings.append("No meaningful user/assistant/system messages exist before the latest user/orchestrator message.")
        return result
    if not limited_messages:
        result.status = "skipped"
        result.warnings.append("No meaningful messages fit the DeepSeek single-request input limit.")
        return result
    rendered = render_messages_for_summary(limited_messages)
    if len(limited_messages) <= config.deepseek_small_history_message_limit or len(rendered) <= config.deepseek_small_history_chars:
        result.status = "skipped_small_history"
        result.text = local_small_history_summary(limited_messages)
        result.chunks = 0
        result.estimated_chars_sent_to_deepseek = 0
        result.deepseek_calls = 0
        return result
    if not config.deepseek_summary_enabled:
        result.status = "skipped"
        result.warnings.append("DeepSeek summary is disabled by MCP configuration.")
        return result
    if not settings.api_key_present:
        result.status = "failed"
        result.warnings.append("DeepSeek API key is not configured.")
        return result

    result.chunks = 1
    result.estimated_chars_sent_to_deepseek = len(rendered)
    result.deepseek_calls = 1
    LOG.info(
        "summary start messages_seen=%d meaningful_total=%d summarized=%d filtered_out=%d omitted_due_to_limit=%d chars=%d chunks=%d model=%s",
        len(messages),
        len(meaningful_messages),
        len(limited_messages),
        result.messages_filtered_out,
        result.messages_omitted_due_to_limit,
        len(rendered),
        result.chunks,
        settings.model,
    )

    summary_client = client or DeepSeekClient(settings)
    try:
        response = summary_client.chat_completion(
            _summary_messages(rendered),
            max_tokens=config.deepseek_max_summary_tokens,
            temperature=config.deepseek_temperature,
        )
        result.text = response.content.strip()
        result.model = getattr(response, "model", settings.model)
        result.status = "ok" if result.text else "failed"
        if not result.text:
            result.warnings.append("DeepSeek returned an empty summary.")
    except DeepSeekClientError as exc:
        result.status = "failed"
        result.warnings.append(str(exc))
        LOG.warning("summary failed retryable=%s status_code=%s error=%s", exc.retryable, exc.status_code, exc)
    except Exception as exc:
        result.status = "failed"
        result.warnings.append(f"DeepSeek summary failed: {exc}")
        LOG.exception("summary unexpected failure")

    LOG.info(
        "summary done status=%s messages=%d chunks=%d summary_chars=%d warnings=%d",
        result.status,
        result.messages_summarized,
        result.chunks,
        len(result.text),
        len(result.warnings),
    )
    return result


def filter_meaningful_messages_for_summary(messages: list[TranscriptMessage]) -> list[TranscriptMessage]:
    return [
        message
        for message in messages
        if message.role in MEANINGFUL_SUMMARY_ROLES and bool((message.text or "").strip())
    ]


def limit_recent_messages_for_single_request(
    messages: list[TranscriptMessage],
    *,
    max_messages: int,
    max_chars: int,
) -> list[TranscriptMessage]:
    max_messages = max(1, max_messages)
    max_chars = max(10_000, max_chars)
    recent = messages[-max_messages:]
    while recent and len(render_messages_for_summary(recent)) > max_chars:
        recent = recent[1:]
    return recent


def local_small_history_summary(messages: list[TranscriptMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        role = message.role
        text = redact_sensitive_text(message.text or "").strip().replace("\r", " ").replace("\n", " ")
        if len(text) > 500:
            text = text[:500].rstrip() + "..."
        if text:
            parts.append(f"{role}: {text}")
    return "\n".join(parts)


def render_messages_for_summary(messages: list[TranscriptMessage]) -> str:
    blocks: list[str] = []
    for ordinal, message in enumerate(messages, 1):
        text = redact_sensitive_text(message.text or "")
        metadata = [
            f"#{ordinal}",
            f"role={message.role}",
        ]
        if message.created_at:
            metadata.append(f"time={message.created_at}")
        if message.turn_id:
            metadata.append(f"turn_id={message.turn_id}")
        if message.source_line_start:
            end = message.source_line_end or message.source_line_start
            metadata.append(f"lines={message.source_line_start}-{end}")
        if len(text) > MAX_RENDERED_MESSAGE_CHARS:
            text = text[:MAX_RENDERED_MESSAGE_CHARS] + "\n[message truncated before summarization]"
        blocks.append("[" + " ".join(metadata) + "]\n" + text)
    return "\n\n".join(blocks)


def redact_sensitive_text(value: str) -> str:
    text = value
    text = re.sub(r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*([^\s'\";]+)", r"\1=[redacted]", text)
    text = re.sub(r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._\-]+", "Authorization: Bearer [redacted]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-[redacted]", text)
    text = re.sub(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b", "[telegram-token-redacted]", text)
    return text


def chunk_text(text: str, *, max_chars: int) -> list[str]:
    max_chars = max(max_chars, 10_000)
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in text.split("\n\n"):
        block_len = len(block) + 2
        if block_len > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            for start in range(0, len(block), max_chars):
                chunks.append(block[start : start + max_chars])
            continue
        if current and current_len + block_len > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(block)
        current_len += block_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _summary_messages(history: str, *, chunk_label: str | None = None) -> list[dict[str, str]]:
    label = f"\nЭто {chunk_label}." if chunk_label else ""
    return [
        {
            "role": "system",
            "content": (
                "Ты сжимаешь верхнюю часть истории Codex-чата для MCP-клиента. "
                "Пиши по-русски, кратко и фактически. Сохрани важные решения, ошибки, команды, "
                "измененные файлы, статусы, открытые вопросы и контекст текущей задачи. "
                "Не выдумывай факты. Не включай секреты, токены, API keys или пароли."
            ),
        },
        {
            "role": "user",
            "content": (
                "Сожми следующую историю до короткого summary. "
                "Формат: 5-12 bullets или компактные абзацы, только полезный контекст."
                f"{label}\n\n{history}"
            ),
        },
    ]


def _reduce_messages(partials: list[str]) -> list[dict[str, str]]:
    joined = "\n\n---\n\n".join(f"Частичное summary {idx + 1}:\n{text}" for idx, text in enumerate(partials))
    return [
        {
            "role": "system",
            "content": (
                "Ты объединяешь частичные summary истории Codex-чата. "
                "Пиши по-русски, без повторов, только факты и текущий полезный контекст. "
                "Не включай секреты."
            ),
        },
        {
            "role": "user",
            "content": f"Объедини эти summary в одно короткое итоговое summary:\n\n{joined}",
        },
    ]


def _first_line(messages: list[TranscriptMessage]) -> int | None:
    for message in messages:
        if message.source_line_start is not None:
            return message.source_line_start
    return None


def _last_line(messages: list[TranscriptMessage]) -> int | None:
    for message in reversed(messages):
        if message.source_line_end is not None:
            return message.source_line_end
        if message.source_line_start is not None:
            return message.source_line_start
    return None
