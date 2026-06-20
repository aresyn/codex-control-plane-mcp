from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = REPO_ROOT / "work" / "external_mcp_client"
STATE_PATH = WORK_DIR / "state.json"
CLIENT_LOG_PATH = WORK_DIR / "client.log"
MCP_STDERR_LOG_PATH = WORK_DIR / "mcp.stderr.log"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18891
DEFAULT_PROTOCOL_VERSION = "2025-01-10"
TERMINAL_COMMAND_STATUSES = {"completed", "failed", "timed_out", "cancelled", "canceled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_work_dir() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)


def append_log(path: Path, message: str, **fields: Any) -> None:
    ensure_work_dir()
    record = {"ts": utc_now(), "message": message}
    record.update(fields)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def current_git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def default_mcp_env(*, execution_mode: str = "client", allowed_roots: list[str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(local_mcp_entry_env())
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["CODEX_MCP_EXECUTION_MODE"] = execution_mode
    if allowed_roots:
        env["CODEX_ALLOWED_ROOTS"] = ";".join(str(Path(item)) for item in allowed_roots if str(item).strip())
    return env


def local_mcp_entry_env(server_name: str = "openclaw-codex") -> dict[str, str]:
    config_path = REPO_ROOT / ".codex" / "config.toml"
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    mcp_servers = payload.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        return {}
    server = mcp_servers.get(server_name)
    if not isinstance(server, dict):
        return {}
    env = server.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items() if value is not None}


def tool_surface_hash(tools: list[dict[str, Any]]) -> str:
    names = sorted(str(tool.get("name") or "") for tool in tools)
    payload = json.dumps(names, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_state() -> dict[str, Any] | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def save_state(state: dict[str, Any]) -> None:
    ensure_work_dir()
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def make_control_token() -> str:
    return secrets.token_urlsafe(32)


class McpProcessClient:
    def __init__(
        self,
        *,
        cwd: Path = REPO_ROOT,
        timeout_seconds: int = 60,
        execution_mode: str = "client",
        allowed_roots: list[str] | None = None,
    ) -> None:
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.execution_mode = execution_mode
        self.allowed_roots = allowed_roots
        self.next_id = 1
        self.started_at = utc_now()
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._stderr_done = threading.Event()
        env = default_mcp_env(execution_mode=execution_mode, allowed_roots=allowed_roots)
        ensure_work_dir()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "codex_control_plane_mcp.server"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._stdout_reader = threading.Thread(target=self._read_stdout, name="mcp-stdout-reader", daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, name="mcp-stderr-reader", daemon=True)
        self._stdout_reader.start()
        self._stderr_reader.start()

    @property
    def pid(self) -> int | None:
        return self.process.pid

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            self._stdout_lines.put(None)
            return
        try:
            for line in self.process.stdout:
                self._stdout_lines.put(line)
        finally:
            self._stdout_lines.put(None)

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            self._stderr_done.set()
            return
        try:
            with MCP_STDERR_LOG_PATH.open("a", encoding="utf-8") as fh:
                for line in self.process.stderr:
                    fh.write(line)
        finally:
            self._stderr_done.set()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("MCP subprocess stdio pipes are not available.")
        if self.process.poll() is not None:
            raise RuntimeError(f"MCP subprocess already exited with code {self.process.returncode}.")
        request_id = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + (timeout_seconds or self.timeout_seconds)
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                line = self._stdout_lines.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError(f"MCP subprocess exited early with code {self.process.returncode}.")
                continue
            if line is None:
                raise RuntimeError(f"MCP subprocess stdout closed with code {self.process.poll()}.")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"MCP stdout contained non-JSON data: {line!r}") from exc
            if response.get("id") == request_id:
                return response
        raise TimeoutError(f"MCP request timed out: {method}")

    def initialize(self) -> dict[str, Any]:
        return self.request("initialize", {"protocolVersion": DEFAULT_PROTOCOL_VERSION})

    def tools_list(self) -> dict[str, Any]:
        return self.request("tools/list", {})

    def tool(self, name: str, arguments: dict[str, Any] | None = None, *, timeout_seconds: int | None = None) -> dict[str, Any]:
        response = self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout_seconds=timeout_seconds,
        )
        if "error" in response:
            raise RuntimeError(f"JSON-RPC error for tool {name}: {response['error']}")
        result = response.get("result") or {}
        structured = dict(result.get("structuredContent") or {})
        structured["_mcpIsError"] = bool(result.get("isError"))
        return structured

    def status(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "returnCode": self.process.poll(),
            "startedAt": self.started_at,
            "executionMode": self.execution_mode,
            "allowedRoots": self.allowed_roots,
        }

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


def post_daemon(path: str, payload: dict[str, Any] | None = None, *, timeout_seconds: int = 60) -> dict[str, Any]:
    state = load_state()
    if not state:
        raise RuntimeError("external MCP daemon state file was not found.")
    port = int(state.get("port") or DEFAULT_PORT)
    token = str(state.get("controlToken") or "")
    url = f"http://{DEFAULT_HOST}:{port}{path}"
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("X-MCP-Client-Token", token)
    with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)
