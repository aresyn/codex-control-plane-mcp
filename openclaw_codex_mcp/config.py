from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
DEFAULT_PROJECTS_ROOT = Path(os.environ.get("CODEX_PROJECTS_ROOT") or Path.cwd())
DEFAULT_PROJECTS_REGISTRY = DEFAULT_PROJECTS_ROOT / "projects.json"
DEFAULT_KB_HISTORY_PROJECTS_ROOT = DEFAULT_PROJECTS_ROOT / "_kb_history" / "projects"
DEFAULT_DEEPSEEK_ENV = Path(os.environ.get("DEEPSEEK_ENV_PATH") or Path.cwd() / ".env")
DEFAULT_LOCAL_CODEX_BIN = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "OpenAI" / "Codex" / "bin"
DEFAULT_REAL_CODEX = DEFAULT_LOCAL_CODEX_BIN / "codex.exe"
DEFAULT_SANDBOX_CODEX = DEFAULT_CODEX_HOME / ".sandbox-bin" / "codex.exe"


@dataclass(slots=True)
class ServerConfig:
    codex_home: Path = DEFAULT_CODEX_HOME
    sessions_dir: Path = DEFAULT_CODEX_HOME / "sessions"
    archived_sessions_dir: Path = DEFAULT_CODEX_HOME / "archived_sessions"
    codex_state_db: Path = DEFAULT_CODEX_HOME / "state_5.sqlite"
    codex_logs_db: Path = DEFAULT_CODEX_HOME / "logs_2.sqlite"
    projects_root: Path = DEFAULT_PROJECTS_ROOT
    projects_registry_path: Path = DEFAULT_PROJECTS_REGISTRY
    kb_history_projects_root: Path = DEFAULT_KB_HISTORY_PROJECTS_ROOT
    codex_binary_path: Path = DEFAULT_REAL_CODEX
    state_db_path: Path = Path("state/codex-mcp-state.sqlite3")
    allowed_roots: list[Path] = field(default_factory=lambda: [DEFAULT_PROJECTS_ROOT])
    default_approval_policy: str = "on-request"
    default_sandbox_policy: dict = field(default_factory=lambda: {"type": "readOnly"})
    default_model: str = "gpt-5.5"
    default_effort: str = "xhigh"
    default_summary: str = "auto"
    start_app_server_for_read_tools: bool = False
    approval_response_timeout_seconds: int = 900
    deepseek_env_path: Path = DEFAULT_DEEPSEEK_ENV
    deepseek_summary_enabled: bool = True
    deepseek_summary_required: bool = False
    deepseek_max_input_chars_per_chunk: int = 60_000
    deepseek_recent_messages_limit: int = 60
    deepseek_small_history_message_limit: int = 5
    deepseek_small_history_chars: int = 4_000
    deepseek_max_summary_tokens: int = 2_000
    deepseek_temperature: float = 0.1
    deepseek_summary_timeout_cap_seconds: int = 45
    deepseek_summary_max_retries_cap: int = 1
    rolling_summary_enabled: bool = True
    default_tail_max_messages: int = 80
    default_tail_max_chars: int = 30_000
    hook_history_enabled: bool = True
    hook_history_max_text_chars: int = 20_000
    max_image_input_items: int = 10
    max_image_input_bytes: int = 20_000_000
    turn_stall_timeout_seconds: int = 900
    stalled_turn_action: str = "diagnose_only"

    @classmethod
    def load(cls, base_dir: Path | None = None) -> "ServerConfig":
        base_dir = base_dir or Path.cwd()
        payload: dict = {}
        config_path = os.environ.get("CODEX_CONTROL_PLANE_MCP_CONFIG") or os.environ.get("OPENCLAW_CODEX_MCP_CONFIG")
        if config_path:
            with open(config_path, "r", encoding="utf-8-sig") as fh:
                payload = json.load(fh)

        codex_home = Path(os.environ.get("CODEX_HOME") or payload.get("codex_home") or DEFAULT_CODEX_HOME)
        projects_root = Path(os.environ.get("CODEX_PROJECTS_ROOT") or payload.get("projects_root") or base_dir)
        codex_binary = _discover_codex_binary(payload)
        state_db = Path(os.environ.get("CODEX_MCP_STATE_DB") or payload.get("state_db_path") or (base_dir / "state" / "codex-mcp-state.sqlite3"))
        allowed_raw = os.environ.get("CODEX_ALLOWED_ROOTS") or payload.get("allowed_roots")
        if isinstance(allowed_raw, str):
            allowed_roots = [Path(item) for item in allowed_raw.split(";") if item.strip()]
        elif isinstance(allowed_raw, list):
            allowed_roots = [Path(str(item)) for item in allowed_raw]
        else:
            allowed_roots = [projects_root]

        return cls(
            codex_home=codex_home,
            sessions_dir=Path(payload.get("sessions_dir") or codex_home / "sessions"),
            archived_sessions_dir=Path(payload.get("archived_sessions_dir") or codex_home / "archived_sessions"),
            codex_state_db=Path(payload.get("codex_state_db") or codex_home / "state_5.sqlite"),
            codex_logs_db=Path(payload.get("codex_logs_db") or codex_home / "logs_2.sqlite"),
            projects_root=projects_root,
            projects_registry_path=Path(
                os.environ.get("CODEX_PROJECTS_REGISTRY") or payload.get("projects_registry_path") or projects_root / "projects.json"
            ),
            kb_history_projects_root=Path(
                os.environ.get("CODEX_KB_HISTORY_PROJECTS_ROOT")
                or payload.get("kb_history_projects_root")
                or projects_root / "_kb_history" / "projects"
            ),
            codex_binary_path=codex_binary,
            state_db_path=state_db,
            allowed_roots=allowed_roots,
            default_approval_policy=_approval_policy_value(
                os.environ.get("CODEX_MCP_DEFAULT_APPROVAL_POLICY")
                or payload.get("default_approval_policy"),
                "on-request",
            ),
            default_sandbox_policy=_sandbox_policy_value(
                os.environ.get("CODEX_MCP_DEFAULT_SANDBOX")
                or os.environ.get("CODEX_MCP_DEFAULT_SANDBOX_POLICY")
                or payload.get("default_sandbox_policy"),
                {"type": "readOnly"},
            ),
            default_model=str(os.environ.get("CODEX_MCP_DEFAULT_MODEL") or payload.get("default_model") or "gpt-5.5"),
            default_effort=str(os.environ.get("CODEX_MCP_DEFAULT_EFFORT") or payload.get("default_effort") or "xhigh"),
            default_summary=str(payload.get("default_summary") or "auto"),
            start_app_server_for_read_tools=bool(payload.get("start_app_server_for_read_tools", False)),
            approval_response_timeout_seconds=_int_value(
                os.environ.get("CODEX_MCP_APPROVAL_RESPONSE_TIMEOUT_SECONDS")
                or payload.get("approval_response_timeout_seconds"),
                900,
            ),
            deepseek_env_path=Path(
                os.environ.get("DEEPSEEK_ENV_PATH")
                or payload.get("deepseek_env_path")
                or str(DEFAULT_DEEPSEEK_ENV)
            ),
            deepseek_summary_enabled=_bool_value(
                os.environ.get("DEEPSEEK_SUMMARY_ENABLED")
                if os.environ.get("DEEPSEEK_SUMMARY_ENABLED") is not None
                else payload.get("deepseek_summary_enabled", True)
            ),
            deepseek_summary_required=_bool_value(
                os.environ.get("DEEPSEEK_SUMMARY_REQUIRED")
                if os.environ.get("DEEPSEEK_SUMMARY_REQUIRED") is not None
                else payload.get("deepseek_summary_required", False)
            ),
            deepseek_max_input_chars_per_chunk=_int_value(
                os.environ.get("DEEPSEEK_MAX_INPUT_CHARS_PER_CHUNK")
                or payload.get("deepseek_max_input_chars_per_chunk"),
                60_000,
            ),
            deepseek_recent_messages_limit=_int_value(
                os.environ.get("DEEPSEEK_RECENT_MESSAGES_LIMIT")
                or payload.get("deepseek_recent_messages_limit"),
                60,
            ),
            deepseek_small_history_message_limit=_int_value(
                os.environ.get("DEEPSEEK_SMALL_HISTORY_MESSAGE_LIMIT")
                or payload.get("deepseek_small_history_message_limit"),
                5,
            ),
            deepseek_small_history_chars=_int_value(
                os.environ.get("DEEPSEEK_SMALL_HISTORY_CHARS")
                or payload.get("deepseek_small_history_chars"),
                4_000,
            ),
            deepseek_max_summary_tokens=_int_value(
                os.environ.get("DEEPSEEK_MAX_SUMMARY_TOKENS")
                or payload.get("deepseek_max_summary_tokens"),
                2_000,
            ),
            deepseek_temperature=_float_value(
                os.environ.get("DEEPSEEK_TEMPERATURE") or payload.get("deepseek_temperature"),
                0.1,
            ),
            deepseek_summary_timeout_cap_seconds=_int_value(
                os.environ.get("DEEPSEEK_SUMMARY_TIMEOUT_CAP_SECONDS")
                or payload.get("deepseek_summary_timeout_cap_seconds"),
                45,
            ),
            deepseek_summary_max_retries_cap=_int_value(
                os.environ.get("DEEPSEEK_SUMMARY_MAX_RETRIES_CAP")
                or payload.get("deepseek_summary_max_retries_cap"),
                1,
            ),
            rolling_summary_enabled=_bool_value(
                os.environ.get("ROLLING_SUMMARY_ENABLED")
                if os.environ.get("ROLLING_SUMMARY_ENABLED") is not None
                else payload.get("rolling_summary_enabled", True)
            ),
            default_tail_max_messages=_int_value(
                os.environ.get("CODEX_MCP_TAIL_MAX_MESSAGES") or payload.get("default_tail_max_messages"),
                80,
            ),
            default_tail_max_chars=_int_value(
                os.environ.get("CODEX_MCP_TAIL_MAX_CHARS") or payload.get("default_tail_max_chars"),
                30_000,
            ),
            hook_history_enabled=_bool_value(
                os.environ.get("CODEX_MCP_HOOK_HISTORY_ENABLED")
                if os.environ.get("CODEX_MCP_HOOK_HISTORY_ENABLED") is not None
                else payload.get("hook_history_enabled", True)
            ),
            hook_history_max_text_chars=_int_value(
                os.environ.get("CODEX_MCP_HOOK_HISTORY_MAX_TEXT_CHARS")
                or payload.get("hook_history_max_text_chars"),
                20_000,
            ),
            max_image_input_items=_int_value(
                os.environ.get("CODEX_MCP_MAX_IMAGE_INPUT_ITEMS")
                or payload.get("max_image_input_items"),
                10,
            ),
            max_image_input_bytes=_int_value(
                os.environ.get("CODEX_MCP_MAX_IMAGE_INPUT_BYTES")
                or payload.get("max_image_input_bytes"),
                20_000_000,
            ),
            turn_stall_timeout_seconds=_int_value(
                os.environ.get("CODEX_MCP_TURN_STALL_TIMEOUT_SECONDS")
                or payload.get("turn_stall_timeout_seconds"),
                900,
            ),
            stalled_turn_action=_stalled_turn_action_value(
                os.environ.get("CODEX_MCP_STALLED_TURN_ACTION")
                or payload.get("stalled_turn_action"),
                "diagnose_only",
            ),
        )


def clean_windows_path(value: str | Path | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return text


def canonical_existing_path(value: str | Path | None) -> str:
    text = clean_windows_path(value)
    if not text:
        return ""
    path = Path(text)
    try:
        if path.exists():
            return _canonical_platform_path(path.resolve())
    except OSError:
        return text
    return text


def path_key(value: str | Path | None) -> str:
    text = canonical_existing_path(value)
    if not text:
        text = clean_windows_path(value)
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def is_path_under(path: str | Path, root: str | Path) -> bool:
    child = path_key(path)
    parent = path_key(root)
    if not child or not parent:
        return False
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def is_allowed_path(path: str | Path, allowed_roots: list[Path]) -> bool:
    return any(is_path_under(path, root) for root in allowed_roots)


def _canonical_platform_path(path: Path) -> str:
    text = str(path)
    if os.name != "nt":
        return text
    return _windows_user_profile_alias(_windows_long_path(_windows_final_path(text)))


def _windows_final_path(text: str) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_final_path_name = kernel32.GetFinalPathNameByHandleW
        get_final_path_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_final_path_name.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        share_read = 0x00000001
        share_write = 0x00000002
        share_delete = 0x00000004
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
        handle = create_file(
            text,
            0,
            share_read | share_write | share_delete,
            None,
            open_existing,
            file_flag_backup_semantics,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            return text
        try:
            required = int(get_final_path_name(handle, None, 0, 0))
            if required <= 0:
                return text
            buffer = ctypes.create_unicode_buffer(required + 1)
            written = int(get_final_path_name(handle, buffer, len(buffer), 0))
            if written <= 0:
                return text
            return _strip_windows_extended_prefix(str(buffer.value))
        finally:
            close_handle(handle)
    except Exception:
        return text


def _windows_long_path(text: str) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_long_path_name = kernel32.GetLongPathNameW
        get_long_path_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_long_path_name.restype = wintypes.DWORD
        required = int(get_long_path_name(text, None, 0))
        if required <= 0:
            return text
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = int(get_long_path_name(text, buffer, len(buffer)))
        if written <= 0:
            return text
        return str(buffer.value)
    except Exception:
        return text


def _windows_user_profile_alias(text: str) -> str:
    profile = os.environ.get("USERPROFILE")
    if not profile:
        return text
    try:
        path = Path(text)
        home = Path(profile)
        path_parts = path.parts
        home_parts = home.parts
        if len(path_parts) < len(home_parts):
            return text
        candidate = Path(*path_parts[: len(home_parts)])
        if candidate.exists() and home.exists() and os.path.samefile(candidate, home):
            return str(home.joinpath(*path_parts[len(home_parts) :]))
    except OSError:
        return text
    return text


def _strip_windows_extended_prefix(text: str) -> str:
    if text.startswith("\\\\?\\UNC\\"):
        return "\\" + text[7:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def _discover_codex_binary(payload: dict) -> Path:
    configured = os.environ.get("CODEX_BINARY_PATH") or payload.get("codex_binary_path")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(str(configured)))
    candidates.extend(_desktop_codex_candidates())
    path_codex = shutil.which("codex.exe") or shutil.which("codex")
    if path_codex:
        candidates.append(Path(path_codex))
    candidates.extend([DEFAULT_REAL_CODEX, DEFAULT_SANDBOX_CODEX])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else DEFAULT_REAL_CODEX


def _desktop_codex_candidates() -> list[Path]:
    root = DEFAULT_LOCAL_CODEX_BIN
    if not root.exists():
        return []
    candidates: list[Path] = []
    try:
        for item in root.iterdir():
            if not item.is_dir():
                continue
            binary = item / "codex.exe"
            if binary.exists():
                candidates.append(binary)
    except OSError:
        return []
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    direct_binary = root / "codex.exe"
    if direct_binary.exists():
        candidates.append(direct_binary)
    return candidates


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_value(value: object, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _float_value(value: object, default: float) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _approval_policy_value(value: object, default: str) -> str:
    selected = str(value or default).strip()
    if selected in {"never", "on-request", "on-failure", "untrusted", "ask_openclaw"}:
        return selected
    raise ValueError(f"Unsupported default approval policy: {selected}")


def _sandbox_policy_value(value: object, default: dict[str, str]) -> dict[str, str]:
    if isinstance(value, dict):
        selected = value
    elif value in (None, ""):
        selected = default
    else:
        text = str(value).strip()
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Unsupported default sandbox policy JSON: {text}") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"Unsupported default sandbox policy JSON: {text}")
            selected = loaded
        else:
            selected = {"type": _sandbox_policy_type(text)}
    policy_type = str(selected.get("type") or "").strip()
    normalized = _sandbox_policy_type(policy_type)
    return {"type": normalized}


def _sandbox_policy_type(value: str) -> str:
    aliases = {
        "read-only": "readOnly",
        "readonly": "readOnly",
        "readOnly": "readOnly",
        "workspace-write": "workspaceWrite",
        "workspacewrite": "workspaceWrite",
        "workspaceWrite": "workspaceWrite",
        "danger-full-access": "dangerFullAccess",
        "dangerfullaccess": "dangerFullAccess",
        "dangerFullAccess": "dangerFullAccess",
    }
    key = value.strip()
    normalized_key = key.replace("_", "-")
    if normalized_key in aliases:
        return aliases[normalized_key]
    compact_key = key.replace("_", "").replace("-", "")
    if compact_key in aliases:
        return aliases[compact_key]
    raise ValueError(f"Unsupported default sandbox policy: {value}")


def _stalled_turn_action_value(value: object, default: str) -> str:
    selected = str(value or default).strip()
    if selected in {"diagnose_only", "interrupt"}:
        return selected
    raise ValueError(f"Unsupported stalled turn action: {selected}")
