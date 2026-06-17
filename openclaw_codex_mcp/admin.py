from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .hook_installer import doctor_hooks, install_hooks, hook_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and diagnose Codex Control Plane MCP local setup.")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--codex-home", type=Path, default=None)
    init_parser.add_argument("--state-db", type=Path, default=Path("state") / "codex-mcp-state.sqlite3")
    init_parser.add_argument("--projects-root", type=Path, default=Path.cwd())
    init_parser.add_argument("--allowed-root", action="append", type=Path, default=None)
    init_parser.add_argument("--skip-hooks", action="store_true")
    init_parser.add_argument("--skip-smoke", action="store_true")

    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--codex-home", type=Path, default=None)
    doctor_parser.add_argument("--state-db", type=Path, default=None)
    doctor_parser.add_argument("--skip-smoke", action="store_true")

    config_parser = sub.add_parser("print-config")
    config_parser.add_argument("--state-db", type=Path, default=Path("state") / "codex-mcp-state.sqlite3")
    config_parser.add_argument("--projects-root", type=Path, default=Path.cwd())
    config_parser.add_argument("--allowed-root", action="append", type=Path, default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = _init(args)
        elif args.command == "doctor":
            result = _doctor(args)
        else:
            result = {
                "ok": True,
                "mcpClientConfig": _mcp_client_config(
                    state_db=_absolute_path(args.state_db),
                    projects_root=_absolute_path(args.projects_root),
                    allowed_roots=[_absolute_path(path) for path in (args.allowed_root or [args.projects_root])],
                ),
            }
    except Exception as exc:  # noqa: BLE001 - admin CLI should report structured output.
        result = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def _init(args: argparse.Namespace) -> dict[str, Any]:
    state_db = _absolute_path(args.state_db)
    projects_root = _absolute_path(args.projects_root)
    allowed_roots = [_absolute_path(path) for path in (args.allowed_root or [projects_root])]
    hook_result = {"skipped": True}
    if not args.skip_hooks:
        hook_result = install_hooks(codex_home=args.codex_home, state_db=state_db)
    smoke_result = {"skipped": True}
    if not args.skip_smoke:
        smoke_result = _protocol_smoke(state_db=state_db)
    return {
        "ok": bool(hook_result.get("ok", True)) and bool(smoke_result.get("ok", True)),
        "stateDb": str(state_db),
        "hooks": hook_result,
        "protocolSmoke": smoke_result,
        "mcpClientConfig": _mcp_client_config(state_db=state_db, projects_root=projects_root, allowed_roots=allowed_roots),
    }


def _doctor(args: argparse.Namespace) -> dict[str, Any]:
    hook_result = doctor_hooks(codex_home=args.codex_home, state_db=args.state_db)
    smoke_result = {"skipped": True}
    if not args.skip_smoke:
        smoke_result = _protocol_smoke(state_db=args.state_db)
    return {
        "ok": bool(hook_result.get("ok")) and bool(smoke_result.get("ok", True)),
        "hooks": hook_result,
        "protocolSmoke": smoke_result,
        "status": hook_status(codex_home=args.codex_home),
    }


def _mcp_client_config(*, state_db: Path, projects_root: Path, allowed_roots: list[Path]) -> dict[str, Any]:
    return {
        "mcpServers": {
            "codex-control-plane-mcp": {
                "command": "codex-control-plane-mcp",
                "env": {
                    "CODEX_MCP_STATE_DB": str(state_db),
                    "CODEX_PROJECTS_ROOT": str(projects_root),
                    "CODEX_ALLOWED_ROOTS": ";".join(str(path) for path in allowed_roots),
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
            }
        }
    }


def _protocol_smoke(*, state_db: Path | None) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if state_db is not None:
        env["CODEX_MCP_STATE_DB"] = str(_absolute_path(state_db))
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-01-10"}}
    process = subprocess.run(
        [sys.executable, "-m", "codex_control_plane_mcp.server"],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=15,
        check=False,
    )
    stdout = process.stdout.strip().splitlines()
    response: dict[str, Any] = {}
    if stdout:
        response = json.loads(stdout[0])
    server_info = ((response.get("result") or {}).get("serverInfo") or {}) if isinstance(response, dict) else {}
    return {
        "ok": process.returncode == 0 and server_info.get("name") == "codex-control-plane-mcp",
        "returnCode": process.returncode,
        "serverInfo": server_info,
    }


def _absolute_path(value: Path) -> Path:
    return value.expanduser().resolve(strict=False)


if __name__ == "__main__":
    raise SystemExit(main())
