from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ServerConfig
from .logging_utils import get_logger


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
LOG = get_logger("deepseek_client")


@dataclass(slots=True)
class DeepSeekSettings:
    api_key: str | None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 60
    max_retries: int = 2
    env_path: Path | None = None

    @property
    def api_key_present(self) -> bool:
        return bool(self.api_key)

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


@dataclass(slots=True)
class DeepSeekResponse:
    content: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


class DeepSeekClientError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class DeepSeekClient:
    def __init__(self, settings: DeepSeekSettings) -> None:
        self.settings = settings

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> DeepSeekResponse:
        if not self.settings.api_key:
            raise DeepSeekClientError("DeepSeek API key is not configured.", retryable=False)

        payload = {
            "model": self.settings.model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.settings.chat_completions_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
            },
        )

        attempts = max(0, self.settings.max_retries) + 1
        last_error: DeepSeekClientError | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    raw = response.read()
                data = json.loads(raw.decode("utf-8", errors="replace"))
                return _parse_response(data, fallback_model=self.settings.model)
            except urllib.error.HTTPError as exc:
                status_code = int(exc.code)
                retryable = status_code == 429 or status_code >= 500
                body_preview = _safe_error_body(exc)
                last_error = DeepSeekClientError(
                    f"DeepSeek HTTP error {status_code}: {body_preview}",
                    retryable=retryable,
                    status_code=status_code,
                )
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = DeepSeekClientError(f"DeepSeek request failed: {exc}", retryable=True)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise DeepSeekClientError(f"DeepSeek response parse failed: {exc}", retryable=False) from exc

            if attempt < attempts - 1 and last_error and last_error.retryable:
                time.sleep(min(2**attempt, 8))
                continue
            break

        raise last_error or DeepSeekClientError("DeepSeek request failed.", retryable=True)


def load_deepseek_settings(config: ServerConfig) -> DeepSeekSettings:
    env_values = read_dotenv(config.deepseek_env_path)
    api_key = os.environ.get("DEEPSEEK_API_KEY") or env_values.get("DEEPSEEK_API_KEY")
    base_url = os.environ.get("DEEPSEEK_BASE_URL") or env_values.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("DEEPSEEK_SUMMARY_MODEL") or env_values.get("DEEPSEEK_SUMMARY_MODEL") or DEFAULT_MODEL
    timeout_seconds = _positive_int(
        os.environ.get("DEEPSEEK_TIMEOUT_SECONDS") or env_values.get("DEEPSEEK_TIMEOUT_SECONDS"),
        60,
    )
    timeout_seconds = min(timeout_seconds, max(1, config.deepseek_summary_timeout_cap_seconds))
    max_retries = _non_negative_int(
        os.environ.get("DEEPSEEK_MAX_RETRIES") or env_values.get("DEEPSEEK_MAX_RETRIES"),
        2,
    )
    max_retries = min(max_retries, max(0, config.deepseek_summary_max_retries_cap))
    settings = DeepSeekSettings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        env_path=config.deepseek_env_path,
    )
    LOG.info(
        "deepseek settings loaded env_path=%s key_present=%s base_url=%s model=%s timeout=%s retries=%s",
        config.deepseek_env_path,
        settings.api_key_present,
        settings.base_url,
        settings.model,
        settings.timeout_seconds,
        settings.max_retries,
    )
    return settings


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        LOG.warning("deepseek env file unavailable path=%s", path)
        return values

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _parse_response(data: dict[str, Any], *, fallback_model: str) -> DeepSeekResponse:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("missing choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ValueError("missing message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("missing content")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    model = str(data.get("model") or fallback_model)
    return DeepSeekResponse(content=content, model=model, usage=usage)


def _safe_error_body(exc: urllib.error.HTTPError, limit: int = 1000) -> str:
    try:
        text = exc.read(limit).decode("utf-8", errors="replace")
    except Exception:
        return ""
    return text.replace("\r", " ").replace("\n", " ")[:limit]


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
