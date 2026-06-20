from __future__ import annotations

import argparse
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from external_mcp_common import (
    CLIENT_LOG_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    REPO_ROOT,
    append_log,
    current_git_commit,
    make_control_token,
    McpProcessClient,
    save_state,
    tool_surface_hash,
    utc_now,
)


class ExternalMcpDaemon:
    def __init__(self, *, host: str, port: int, execution_mode: str, timeout_seconds: int, allowed_roots: list[str] | None) -> None:
        self.host = host
        self.port = port
        self.execution_mode = execution_mode
        self.timeout_seconds = timeout_seconds
        self.allowed_roots = allowed_roots
        self.control_token = make_control_token()
        self.started_at = utc_now()
        self._lock = threading.RLock()
        self._mcp: McpProcessClient | None = None
        self._tool_count: int | None = None
        self._tool_surface_hash: str | None = None

    def start(self) -> None:
        self.restart_mcp(reason="daemon_start")

    def write_state(self) -> None:
        mcp_status = self._mcp.status() if self._mcp is not None else None
        save_state(
            {
                "pid": os.getpid(),
                "daemonPid": os.getpid(),
                "host": self.host,
                "port": self.port,
                "controlToken": self.control_token,
                "startedAt": self.started_at,
                "executionMode": self.execution_mode,
                "allowedRoots": self.allowed_roots,
                "repoRoot": str(REPO_ROOT),
                "gitCommit": current_git_commit(),
                "mcp": mcp_status,
                "toolCount": self._tool_count,
                "toolSurfaceHash": self._tool_surface_hash,
            }
        )

    def restart_mcp(self, *, reason: str) -> dict[str, Any]:
        with self._lock:
            before = self._mcp.status() if self._mcp is not None else None
            if self._mcp is not None:
                self._mcp.close()
            self._mcp = McpProcessClient(
                cwd=REPO_ROOT,
                timeout_seconds=self.timeout_seconds,
                execution_mode=self.execution_mode,
                allowed_roots=self.allowed_roots,
            )
            initialized = self._mcp.initialize()
            listed = self._mcp.tools_list()
            tools = ((listed.get("result") or {}).get("tools") or [])
            self._tool_count = len(tools)
            self._tool_surface_hash = tool_surface_hash(tools)
            self.write_state()
            after = self._mcp.status()
            append_log(CLIENT_LOG_PATH, "mcp_restarted", reason=reason, before=before, after=after)
            return {
                "ok": True,
                "reason": reason,
                "before": before,
                "after": after,
                "initialize": (initialized.get("result") or {}).get("serverInfo"),
                "toolCount": self._tool_count,
                "toolSurfaceHash": self._tool_surface_hash,
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            self.write_state()
            return {
                "ok": True,
                "daemon": {
                    "host": self.host,
                    "port": self.port,
                    "startedAt": self.started_at,
                    "executionMode": self.execution_mode,
                    "allowedRoots": self.allowed_roots,
                    "gitCommit": current_git_commit(),
                },
                "mcp": self._mcp.status() if self._mcp is not None else None,
                "toolCount": self._tool_count,
                "toolSurfaceHash": self._tool_surface_hash,
            }

    def tools_list(self) -> dict[str, Any]:
        with self._lock:
            if self._mcp is None:
                raise RuntimeError("MCP subprocess is not running.")
            response = self._mcp.tools_list()
            tools = ((response.get("result") or {}).get("tools") or [])
            self._tool_count = len(tools)
            self._tool_surface_hash = tool_surface_hash(tools)
            self.write_state()
            return {
                "ok": True,
                "toolCount": self._tool_count,
                "toolSurfaceHash": self._tool_surface_hash,
                "tools": tools,
            }

    def tool_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "")
        if not name:
            raise ValueError("tool_call requires name.")
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("tool_call arguments must be an object.")
        timeout_seconds = payload.get("timeout_seconds")
        with self._lock:
            if self._mcp is None:
                raise RuntimeError("MCP subprocess is not running.")
            started_at = utc_now()
            result = self._mcp.tool(name, arguments, timeout_seconds=timeout_seconds)
            append_log(
                CLIENT_LOG_PATH,
                "tool_call",
                tool=name,
                startedAt=started_at,
                isError=result.get("_mcpIsError"),
            )
            return {"ok": True, "tool": name, "result": result}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._mcp is not None:
                self._mcp.close()
                self._mcp = None
            self.write_state()
            append_log(CLIENT_LOG_PATH, "daemon_stop_requested")
            return {"ok": True, "stopping": True}


class Handler(BaseHTTPRequestHandler):
    daemon_ref: ExternalMcpDaemon
    server_version = "ExternalMcpClient/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        append_log(CLIENT_LOG_PATH, "http_request", client=self.client_address[0], line=format % args)

    def do_POST(self) -> None:
        try:
            if self.headers.get("X-MCP-Client-Token") != self.daemon_ref.control_token:
                self._send({"ok": False, "error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("request payload must be an object.")
            result = self._dispatch(payload)
            self._send(result)
        except Exception as exc:
            append_log(CLIENT_LOG_PATH, "handler_error", path=self.path, error=f"{type(exc).__name__}: {exc}")
            self._send(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.path == "/status":
            return self.daemon_ref.status()
        if self.path == "/restart-mcp":
            return self.daemon_ref.restart_mcp(reason=str(payload.get("reason") or "client_request"))
        if self.path == "/tools/list":
            return self.daemon_ref.tools_list()
        if self.path == "/tools/call":
            return self.daemon_ref.tool_call(payload)
        if self.path == "/stop":
            result = self.daemon_ref.stop()
            threading.Thread(target=self.server.shutdown, name="daemon-shutdown", daemon=True).start()
            return result
        raise ValueError(f"unknown endpoint: {self.path}")

    def _send(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Long-lived external MCP client daemon.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--execution-mode", default="client", choices=["inline", "client", "worker", "observe"])
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--allowed-root", action="append", default=None)
    args = parser.parse_args()

    daemon = ExternalMcpDaemon(
        host=args.host,
        port=args.port,
        execution_mode=args.execution_mode,
        timeout_seconds=args.timeout_seconds,
        allowed_roots=args.allowed_root,
    )
    daemon.start()
    Handler.daemon_ref = daemon
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    daemon.write_state()
    append_log(CLIENT_LOG_PATH, "daemon_started", host=args.host, port=args.port, executionMode=args.execution_mode)
    try:
        httpd.serve_forever()
    finally:
        daemon.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
