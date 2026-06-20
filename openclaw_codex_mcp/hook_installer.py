from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import DEFAULT_CODEX_HOME, ServerConfig
from .storage import McpStorage


HOOK_EVENTS = ("UserPromptSubmit", "Stop", "SessionStart", "PreCompact", "PostCompact")
HOOK_MARKER = "codex_control_plane_mcp.hooks.codex_sqlite_journal"
LEGACY_HOOK_MARKERS = ("openclaw_codex_mcp.hooks.codex_sqlite_journal",)
DEFAULT_CONFIG_NAME = "codex-control-plane-mcp-hooks.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Codex Control Plane MCP hook-backed SQLite history.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "uninstall", "status", "doctor"):
        command = sub.add_parser(name)
        command.add_argument("--codex-home", type=Path, default=None)
        command.add_argument("--hooks-json", type=Path, default=None)
        command.add_argument("--config", type=Path, default=None)
        command.add_argument("--state-db", type=Path, default=None)
        command.add_argument("--max-text-chars", type=int, default=20_000)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            result = install_hooks(
                codex_home=args.codex_home,
                hooks_json=args.hooks_json,
                config_path=args.config,
                state_db=args.state_db,
                max_text_chars=args.max_text_chars,
            )
        elif args.command == "uninstall":
            result = uninstall_hooks(codex_home=args.codex_home, hooks_json=args.hooks_json, config_path=args.config)
        elif args.command == "doctor":
            result = doctor_hooks(codex_home=args.codex_home, hooks_json=args.hooks_json, config_path=args.config, state_db=args.state_db)
        else:
            result = hook_status(codex_home=args.codex_home, hooks_json=args.hooks_json, config_path=args.config)
    except Exception as exc:  # noqa: BLE001 - CLI should return structured output.
        result = {"ok": False, "error": {"message": str(exc), "type": type(exc).__name__}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def install_hooks(
    *,
    codex_home: Path | None = None,
    hooks_json: Path | None = None,
    config_path: Path | None = None,
    state_db: Path | None = None,
    max_text_chars: int = 20_000,
) -> dict[str, Any]:
    codex_home = _absolute_path(_codex_home(codex_home))
    hooks_json = _absolute_path(hooks_json or codex_home / "hooks.json")
    config_path = _absolute_path(config_path or codex_home / DEFAULT_CONFIG_NAME)
    state_db = _absolute_path(state_db or ServerConfig.load(Path.cwd()).state_db_path)
    hooks_json.parent.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_hooks(hooks_json)
    backup = _backup_file(hooks_json)
    changed_events: list[str] = []
    hooks = payload.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if _event_has_handler(groups, current_only=True):
            continue
        groups = _remove_our_handlers(groups)
        hooks[event] = groups
        group: dict[str, Any] = {"hooks": [_hook_handler(config_path)]}
        if event == "SessionStart":
            group["matcher"] = "startup|resume|clear|compact"
        elif event in {"PreCompact", "PostCompact"}:
            group["matcher"] = "manual|auto"
        groups.append(group)
        changed_events.append(event)
    _write_json(config_path, _hook_config_payload(state_db=state_db, max_text_chars=max_text_chars))
    _write_json(hooks_json, payload)
    return {
        "ok": True,
        "installed": True,
        "changed": bool(changed_events),
        "changedEvents": changed_events,
        "hooksJson": str(hooks_json),
        "configPath": str(config_path),
        "stateDb": str(state_db),
        "backupPath": str(backup) if backup else None,
    }


def uninstall_hooks(
    *,
    codex_home: Path | None = None,
    hooks_json: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    codex_home = _absolute_path(_codex_home(codex_home))
    hooks_json = _absolute_path(hooks_json or codex_home / "hooks.json")
    config_path = _absolute_path(config_path or codex_home / DEFAULT_CONFIG_NAME)
    payload = _read_hooks(hooks_json)
    backup = _backup_file(hooks_json)
    removed = 0
    hooks = payload.get("hooks") if isinstance(payload.get("hooks"), dict) else {}
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                new_groups.append(group)
                continue
            kept = [handler for handler in handlers if not _is_our_handler(handler)]
            removed += len(handlers) - len(kept)
            if kept:
                next_group = dict(group)
                next_group["hooks"] = kept
                new_groups.append(next_group)
        if new_groups:
            hooks[event] = new_groups
        else:
            hooks.pop(event, None)
    payload["hooks"] = hooks
    _write_json(hooks_json, payload)
    return {
        "ok": True,
        "installed": False,
        "removedHandlers": removed,
        "hooksJson": str(hooks_json),
        "configPath": str(config_path),
        "backupPath": str(backup) if backup else None,
    }


def hook_status(
    *,
    codex_home: Path | None = None,
    hooks_json: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    codex_home = _absolute_path(_codex_home(codex_home))
    hooks_json = _absolute_path(hooks_json or codex_home / "hooks.json")
    config_path = _absolute_path(config_path or codex_home / DEFAULT_CONFIG_NAME)
    payload = _read_hooks(hooks_json)
    hooks = payload.get("hooks") if isinstance(payload.get("hooks"), dict) else {}
    events = {
        event: _event_has_handler(hooks.get(event) if isinstance(hooks.get(event), list) else [])
        for event in HOOK_EVENTS
    }
    config = _read_json(config_path)
    state_db = Path(str(config.get("stateDb"))) if config.get("stateDb") else None
    return {
        "ok": True,
        "installed": all(events.values()),
        "events": events,
        "hooksJson": str(hooks_json),
        "hooksJsonExists": hooks_json.exists(),
        "configPath": str(config_path),
        "configExists": config_path.exists(),
        "stateDb": str(state_db) if state_db else None,
        "stateDbExists": bool(state_db and state_db.exists()),
    }


def doctor_hooks(
    *,
    codex_home: Path | None = None,
    hooks_json: Path | None = None,
    config_path: Path | None = None,
    state_db: Path | None = None,
) -> dict[str, Any]:
    status = hook_status(codex_home=codex_home, hooks_json=hooks_json, config_path=config_path)
    configured_state = state_db or (Path(status["stateDb"]) if status.get("stateDb") else None)
    db_writable = False
    db_error = None
    if configured_state is not None:
        try:
            storage = McpStorage(configured_state)
            storage.connect()
            storage.close()
            db_writable = True
        except Exception as exc:  # noqa: BLE001 - report as diagnostic.
            db_error = f"{type(exc).__name__}: {exc}"
    return {
        **status,
        "doctor": {
            "dbWritable": db_writable,
            "dbError": db_error,
            "allEventsInstalled": bool(status.get("installed")),
        },
    }


def _codex_home(value: Path | None) -> Path:
    return value or Path(os.environ.get("CODEX_HOME") or DEFAULT_CODEX_HOME)


def _absolute_path(value: Path) -> Path:
    return value.expanduser().resolve(strict=False)


def _read_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    return _read_json(path) or {"hooks": {}}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.codex-control-plane-backup-{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def _hook_handler(config_path: Path) -> dict[str, Any]:
    command = shlex.join([sys.executable, "-m", HOOK_MARKER, "--config", str(config_path)])
    command_windows = f"& {_quote(sys.executable)} -m {HOOK_MARKER} --config {_quote(config_path)}"
    return {
        "type": "command",
        "command": command,
        "commandWindows": command_windows,
        "timeout": 30,
        "statusMessage": "Recording Codex Control Plane MCP history",
    }


def _hook_config_payload(*, state_db: Path, max_text_chars: int) -> dict[str, Any]:
    return {
        "stateDb": str(state_db),
        "maxTextChars": max(1000, int(max_text_chars)),
        "installedBy": "codex-control-plane-mcp",
        "version": __version__,
        "installedAt": datetime.now(timezone.utc).isoformat(),
    }


def _event_has_handler(groups: Any, *, current_only: bool = False) -> bool:
    if not isinstance(groups, list):
        return False
    return any(
        _is_our_handler(handler, current_only=current_only)
        for group in groups
        if isinstance(group, dict)
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    )


def _remove_our_handlers(groups: Any) -> list[Any]:
    if not isinstance(groups, list):
        return []
    new_groups: list[Any] = []
    for group in groups:
        if not isinstance(group, dict):
            new_groups.append(group)
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            new_groups.append(group)
            continue
        kept = [handler for handler in handlers if not _is_our_handler(handler)]
        if kept:
            next_group = dict(group)
            next_group["hooks"] = kept
            new_groups.append(next_group)
    return new_groups


def _is_our_handler(handler: Any, *, current_only: bool = False) -> bool:
    if not isinstance(handler, dict):
        return False
    markers = (HOOK_MARKER,) if current_only else (HOOK_MARKER, *LEGACY_HOOK_MARKERS)
    command = str(handler.get("command") or "")
    command_windows = str(handler.get("commandWindows") or "")
    return any(marker in command or marker in command_windows for marker in markers)


def _quote(value: object) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


if __name__ == "__main__":
    raise SystemExit(main())
