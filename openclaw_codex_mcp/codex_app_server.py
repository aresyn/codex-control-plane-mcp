from __future__ import annotations

import asyncio
import json
import os
import time
from asyncio.subprocess import Process
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from .config import ServerConfig
from .errors import app_server_busy, app_server_unavailable, timeout
from . import __version__
from .logging_utils import get_logger
from .pending_interactions import PendingInteractionManager, is_supported_interaction_method
from .storage import McpStorage
from .turn_tracker import TurnTracker


LOG = get_logger("codex_app_server")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CodexAppServerClient:
    def __init__(self, config: ServerConfig, storage: McpStorage) -> None:
        self.config = config
        self.storage = storage
        self.process: Process | None = None
        self._request_id = 0
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=1000)
        self.tracker = TurnTracker(
            storage,
            hook_history_enabled=config.hook_history_enabled,
            hook_history_max_text_chars=config.hook_history_max_text_chars,
        )
        self.interactions = PendingInteractionManager(storage)
        self.process_generation = 0
        self.started_at: str | None = None
        self.last_error: str | None = None
        self.initialize_result: dict[str, Any] | None = None

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        if not self.config.codex_binary_path.exists():
            raise app_server_unavailable(f"Codex binary does not exist: {self.config.codex_binary_path}")
        self.process_generation += 1
        self.started_at = _now_iso()
        self.last_error = None
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.config.codex_home)
        LOG.info(
            "starting codex app-server binary=%s cwd=%s codex_home=%s",
            self.config.codex_binary_path,
            self.config.projects_root,
            self.config.codex_home,
        )
        self.process = await asyncio.create_subprocess_exec(
            str(self.config.codex_binary_path),
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.config.projects_root),
            env=env,
        )
        LOG.info("codex app-server process started pid=%s generation=%s", self.process.pid, self.process_generation)
        self._stdout_task = asyncio.create_task(self._read_stdout_loop(), name="codex-app-server-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr_loop(), name="codex-app-server-stderr")
        initialize_result = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-01-10",
                "clientInfo": {"name": "codex-control-plane-mcp", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
            timeout_seconds=30,
        )
        self.initialize_result = initialize_result if isinstance(initialize_result, dict) else {"result": initialize_result}
        await self.notify("initialized", {})
        LOG.info("codex app-server initialized pid=%s generation=%s", self.process.pid, self.process_generation)

    async def stop(self) -> None:
        LOG.info("stopping codex app-server pending=%d", len(self._pending))
        if self.tracker.running_turns():
            self.tracker.mark_active_turns_unknown(
                process_generation=self.process_generation,
                reason="Codex app-server was stopped by MCP server.",
            )
        self.interactions.orphan_live(
            process_generation=self.process_generation,
            reason="Codex app-server was stopped by MCP server.",
        )
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with suppress(BaseException):
                    await task
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None

    async def restart(self, *, start_after_restart: bool, timeout_seconds: int, force: bool = False) -> dict[str, Any]:
        if self.has_active_work() and not force:
            raise app_server_busy(
                "Codex app-server has active MCP work and was not restarted.",
                **self.active_work_snapshot(),
            )
        before_pid = self.process.pid if self.process is not None and self.process.returncode is None else None
        before_generation = self.process_generation
        if force:
            self.tracker.mark_active_turns_unknown(
                process_generation=self.process_generation,
                reason="Codex app-server was force-restarted by MCP client.",
            )
            self.interactions.orphan_live(
                process_generation=self.process_generation,
                reason="Codex app-server was force-restarted by MCP client.",
            )
        await self.stop()
        after_pid = None
        started = False
        if start_after_restart:
            try:
                await asyncio.wait_for(self.start(), timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise timeout("Codex app-server restart timed out.", action="restart", timeout_seconds=timeout_seconds) from exc
            after_pid = self.process.pid if self.process is not None and self.process.returncode is None else None
            started = after_pid is not None
        return {
            "ok": True,
            "restarted": before_pid is not None,
            "started": started,
            "before_pid": before_pid,
            "after_pid": after_pid,
            "beforePid": before_pid,
            "afterPid": after_pid,
            "beforeProcessGeneration": before_generation,
            "processGeneration": self.process_generation,
            "active_work": self.active_work_snapshot(),
            "activeWork": _camel_active_work(self.active_work_snapshot()),
        }

    def has_active_work(self) -> bool:
        return bool(self._pending or self.tracker.running_turns() or self.interactions.pending_count())

    def active_work_snapshot(self) -> dict[str, Any]:
        return {
            "pending_requests": len(self._pending),
            "active_turns": self.tracker.running_turns(),
            "pending_interactions": self.interactions.pending_count(),
        }

    def status_snapshot(self, *, include_recent_events: bool = False) -> dict[str, Any]:
        running = self.process is not None and self.process.returncode is None
        snapshot = {
            "ok": True,
            "running": running,
            "started": self.started_at is not None,
            "pid": self.process.pid if running and self.process is not None else None,
            "returnCode": self.process.returncode if self.process is not None else None,
            "startedAt": self.started_at,
            "processGeneration": self.process_generation,
            "pendingRequests": len(self._pending),
            "activeTurns": self.tracker.running_turns(),
            "pendingInteractions": self.interactions.list_interactions(status="pending", limit=50),
            "recentEventCount": len(self._recent_events),
            "lastError": self.last_error,
            "codexBinaryPath": str(self.config.codex_binary_path),
            "codexBinaryExists": self.config.codex_binary_path.exists(),
            "codexHome": str(self.config.codex_home),
        }
        if include_recent_events:
            snapshot["recentEvents"] = list(self._recent_events)[-20:]
        return snapshot

    async def request(self, method: str, params: dict[str, Any] | None, timeout_seconds: float | None = None) -> Any:
        await self._ensure_running()
        self._request_id += 1
        request_id = self._request_id
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        LOG.info("app-server request start id=%s method=%s timeout=%s", request_id, method, timeout_seconds)
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            if timeout_seconds is None:
                result = await future
            else:
                result = await asyncio.wait_for(future, timeout=timeout_seconds)
            if isinstance(result, dict):
                result = dict(result)
                result.setdefault("_requestId", request_id)
                result.setdefault("_processGeneration", self.process_generation)
            LOG.info("app-server request ok id=%s method=%s elapsed_ms=%d", request_id, method, int((time.monotonic() - started) * 1000))
            return result
        except asyncio.TimeoutError as exc:
            future.cancel()
            LOG.warning("app-server request timeout id=%s method=%s elapsed_ms=%d", request_id, method, int((time.monotonic() - started) * 1000))
            self.last_error = f"request timeout: {method}"
            raise timeout(f"Codex app-server request timed out: {method}", method=method) from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def respond_success(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def thread_start(
        self,
        *,
        cwd: str,
        approval_policy: str,
        sandbox_policy: dict[str, Any],
        model: str | None,
        effort: str,
        summary: str,
        timeout_seconds: float | None = 60,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": cwd,
            "approvalPolicy": approval_policy,
            "sandboxPolicy": sandbox_policy,
            "serviceName": "codex-control-plane-mcp",
            "effort": effort,
            "summary": summary,
        }
        if model:
            params["model"] = model
        return await self.request("thread/start", params, timeout_seconds=timeout_seconds)

    async def thread_resume(self, thread_id: str, cwd: str, timeout_seconds: float | None = 60) -> dict[str, Any]:
        return await self.request("thread/resume", {"threadId": thread_id, "cwd": cwd}, timeout_seconds=timeout_seconds)

    async def thread_fork(
        self,
        *,
        thread_id: str,
        cwd: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        config: dict[str, Any] | None = None,
        ephemeral: bool = False,
        timeout_seconds: float | None = 60,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "ephemeral": bool(ephemeral)}
        if cwd:
            params["cwd"] = cwd
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox:
            params["sandbox"] = sandbox
        if model:
            params["model"] = model
        if config is not None:
            params["config"] = config
        result = await self.request("thread/fork", params, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"result": result}

    async def thread_list(self, cwd: str) -> dict[str, Any]:
        return await self.request("thread/list", {"cwd": cwd}, timeout_seconds=60)

    async def thread_read(self, thread_id: str) -> dict[str, Any]:
        return await self.request("thread/read", {"threadId": thread_id}, timeout_seconds=60)

    async def thread_name_set(self, thread_id: str, name: str) -> dict[str, Any]:
        return await self.request("thread/name/set", {"threadId": thread_id, "name": name}, timeout_seconds=30)

    async def thread_archive(self, thread_id: str, timeout_seconds: float | None = 30) -> dict[str, Any]:
        result = await self.request("thread/archive", {"threadId": thread_id}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"result": result}

    async def thread_unarchive(self, thread_id: str, timeout_seconds: float | None = 30) -> dict[str, Any]:
        result = await self.request("thread/unarchive", {"threadId": thread_id}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"result": result}

    async def thread_compact_start(self, thread_id: str, timeout_seconds: float | None = 30) -> dict[str, Any]:
        result = await self.request("thread/compact/start", {"threadId": thread_id}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"result": result}

    async def thread_goal_get(self, thread_id: str, timeout_seconds: float | None = 2) -> dict[str, Any]:
        result = await self.request("thread/goal/get", {"threadId": thread_id}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"goal": result}

    async def thread_goal_set(
        self,
        thread_id: str,
        *,
        objective: str | None,
        status: str | None = "active",
        token_budget: int | None = None,
        timeout_seconds: float | None = 5,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if objective is not None:
            params["objective"] = objective
        if status is not None:
            params["status"] = status
        if token_budget is not None:
            params["tokenBudget"] = int(token_budget)
        result = await self.request("thread/goal/set", params, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"goal": result}

    async def thread_goal_clear(self, thread_id: str, timeout_seconds: float | None = 5) -> dict[str, Any]:
        result = await self.request("thread/goal/clear", {"threadId": thread_id}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"cleared": bool(result)}

    async def review_start(
        self,
        *,
        thread_id: str,
        target: dict[str, Any],
        delivery: str | None = None,
        timeout_seconds: float | None = 60,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "target": target}
        if delivery:
            params["delivery"] = delivery
        result = await self.request("review/start", params, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"result": result}

    async def turn_start(
        self,
        *,
        thread_id: str,
        input_items: list[dict[str, Any]],
        cwd: str,
        approval_policy: str,
        sandbox_policy: dict[str, Any],
        model: str | None,
        effort: str,
        summary: str,
        collaboration_mode: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        chat_id: str | None = None,
        project_id: str | None = None,
        project_path: str | None = None,
        timeout_seconds: float | None = 60,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
            "cwd": cwd,
            "approvalPolicy": approval_policy,
            "sandboxPolicy": sandbox_policy,
            "effort": effort,
            "summary": summary,
        }
        if model:
            params["model"] = model
        if collaboration_mode is not None:
            params["collaborationMode"] = collaboration_mode
        if output_schema is not None:
            params["outputSchema"] = output_schema
        result = await self.request("turn/start", params, timeout_seconds=timeout_seconds)
        turn_id = _result_turn_id(result)
        if turn_id:
            user_message = _first_text_input(input_items)
            self.tracker.register_turn(
                turn_id=turn_id,
                thread_id=thread_id,
                chat_id=chat_id,
                project_id=project_id,
                project_path=project_path or cwd,
                status="running",
                started_at=_now_iso(),
                user_message=user_message,
                model=model,
                permission_mode=approval_policy,
                request_id=str(result.get("_requestId")) if isinstance(result, dict) and result.get("_requestId") is not None else None,
                process_generation=int(result.get("_processGeneration") or self.process_generation) if isinstance(result, dict) else self.process_generation,
            )
        return result

    async def turn_interrupt(self, *, thread_id: str, turn_id: str, timeout_seconds: float | None = 30) -> dict[str, Any]:
        result = await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout_seconds=timeout_seconds)
        self.tracker.mark_turn_interrupted(turn_id, reason="Interrupted by MCP client.")
        return result if isinstance(result, dict) else {"result": result}

    async def turn_steer(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        input_items: list[dict[str, Any]],
        client_user_message_id: str | None = None,
        timeout_seconds: float | None = 60,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": input_items,
        }
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        result = await self.request("turn/steer", params, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"result": result}

    async def model_list(
        self,
        *,
        limit: int | None = 100,
        include_hidden: bool | None = False,
        cursor: str | None = None,
        timeout_seconds: float | None = 2,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "includeHidden": include_hidden}
        if cursor:
            params["cursor"] = cursor
        result = await self.request("model/list", params, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"data": result}

    async def permission_profile_list(
        self,
        *,
        cwd: str | None = None,
        limit: int | None = 100,
        cursor: str | None = None,
        timeout_seconds: float | None = 2,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cwd:
            params["cwd"] = cwd
        if cursor:
            params["cursor"] = cursor
        result = await self.request("permissionProfile/list", params, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"data": result}

    async def windows_sandbox_readiness(self, *, timeout_seconds: float | None = 2) -> dict[str, Any]:
        result = await self.request("windowsSandbox/readiness", None, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"status": result}

    async def hooks_list(self, *, cwds: list[str], timeout_seconds: float | None = 2) -> dict[str, Any]:
        result = await self.request("hooks/list", {"cwds": cwds}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"data": result}

    async def skills_list(self, *, cwds: list[str], force_reload: bool = False, timeout_seconds: float | None = 2) -> dict[str, Any]:
        result = await self.request(
            "skills/list",
            {"cwds": cwds, "forceReload": force_reload},
            timeout_seconds=timeout_seconds,
        )
        return result if isinstance(result, dict) else {"data": result}

    async def model_provider_capabilities_read(self, *, timeout_seconds: float | None = 2) -> dict[str, Any]:
        result = await self.request("modelProvider/capabilities/read", {}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"result": result}

    async def account_read(self, *, refresh_token: bool = False, timeout_seconds: float | None = 2) -> dict[str, Any]:
        result = await self.request("account/read", {"refreshToken": bool(refresh_token)}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"account": result}

    async def account_usage_read(self, *, timeout_seconds: float | None = 2) -> dict[str, Any]:
        result = await self.request("account/usage/read", {}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"summary": result}

    async def account_rate_limits_read(self, *, timeout_seconds: float | None = 2) -> dict[str, Any]:
        result = await self.request("account/rateLimits/read", {}, timeout_seconds=timeout_seconds)
        return result if isinstance(result, dict) else {"rateLimits": result}

    async def _ensure_running(self) -> None:
        if self.process is None or self.process.returncode is not None:
            await self.start()

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise app_server_unavailable("Codex app-server is not running.")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            self.process.stdin.write(line.encode("utf-8"))
            await self.process.stdin.drain()
        self._record_app_server_event_best_effort("outbound", payload, _now_iso())

    async def _read_stdout_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        buffer = bytearray()
        while True:
            chunk = await self.process.stdout.read(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                idx = buffer.find(b"\n")
                if idx < 0:
                    break
                line = bytes(buffer[:idx])
                del buffer[: idx + 1]
                await self._handle_stdout_line(line)
        if buffer:
            await self._handle_stdout_line(bytes(buffer))
        self.last_error = "Codex app-server stdout closed."
        self.tracker.mark_active_turns_unknown(process_generation=self.process_generation, reason=self.last_error)
        self.interactions.orphan_live(process_generation=self.process_generation, reason=self.last_error)
        LOG.warning("codex app-server stdout closed pending=%d generation=%s", len(self._pending), self.process_generation)
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeError("Codex app-server stdout closed."))

    async def _read_stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            LOG.info("codex app-server stderr: %s", text[:1000])
        LOG.info("codex app-server stderr closed")

    async def _handle_stdout_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            LOG.warning("failed to decode app-server stdout line: %s", text[:500])
            return
        received_at = _now_iso()
        if "method" in payload and payload.get("id") is not None:
            try:
                await self._handle_server_request(payload)
            except Exception as exc:
                LOG.exception("app-server server-request handling failed method=%s id=%s", payload.get("method"), payload.get("id"))
                self.last_error = f"server request handling failed: {payload.get('method')}: {exc}"
                with suppress(Exception):
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": payload.get("id"),
                            "error": {"code": -32603, "message": "MCP failed to handle app-server request."},
                        }
                    )
            self._record_app_server_event_best_effort("inbound", payload, received_at)
            return
        if "method" in payload:
            self._recent_events.append(payload)
            self._record_tracker_event_best_effort(payload, received_at)
            self._record_app_server_event_best_effort("inbound", payload, received_at)
            return
        request_id = payload.get("id")
        future = self._pending.get(request_id)
        if future is None:
            self._record_app_server_event_best_effort("inbound", payload, received_at)
            return
        if "error" in payload:
            message = str((payload.get("error") or {}).get("message") or "Codex app-server error")
            LOG.warning("app-server response error id=%s message=%s", request_id, message)
            if not future.done():
                future.set_exception(RuntimeError(message))
        else:
            result = payload.get("result")
            self._record_thread_snapshot_best_effort(result, received_at)
            if not future.done():
                future.set_result(result)
        self._record_app_server_event_best_effort("inbound", payload, received_at)

    async def _handle_server_request(self, payload: dict[str, Any]) -> None:
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        request_id = payload.get("id")
        event = {"jsonrpc": "2.0", "method": method, "params": params}
        self._recent_events.append(event)
        self._record_tracker_event_best_effort(event, _now_iso())
        LOG.info("app-server server-request method=%s id=%s", method, request_id)

        if is_supported_interaction_method(method):
            interaction = self.interactions.create(
                app_server_request_id=request_id,
                method=method,
                params=params,
                process_generation=self.process_generation,
                timeout_seconds=self.config.approval_response_timeout_seconds,
            )
            self.tracker.mark_pending_interaction(method, params)
            asyncio.create_task(
                self._resolve_pending_interaction(str(interaction["interactionId"]), request_id),
                name=f"codex-pending-interaction-{interaction['interactionId']}",
            )
            return
        await self.respond_success(request_id, {})

    def _record_app_server_event_best_effort(self, direction: str, payload: dict[str, Any], received_at: str) -> None:
        try:
            self.storage.record_app_server_event(
                direction,
                payload,
                received_at,
                process_generation=self.process_generation,
            )
        except Exception as exc:
            self.last_error = f"app-server audit write failed: {exc}"
            LOG.warning(
                "app-server audit write failed direction=%s method=%s id=%s error=%s",
                direction,
                payload.get("method"),
                payload.get("id"),
                exc,
            )

    def _record_tracker_event_best_effort(self, payload: dict[str, Any], received_at: str) -> None:
        try:
            self.tracker.record_event(payload, received_at=received_at)
        except Exception as exc:
            self.last_error = f"turn tracker event write failed: {exc}"
            LOG.warning(
                "turn tracker event write failed method=%s error=%s",
                payload.get("method"),
                exc,
            )

    def _record_thread_snapshot_best_effort(self, result: Any, received_at: str) -> None:
        try:
            self.tracker.record_thread_snapshot(result, received_at=received_at)
        except Exception as exc:
            self.last_error = f"thread snapshot write failed: {exc}"
            LOG.warning("thread snapshot write failed error=%s", exc)

    async def _resolve_pending_interaction(self, interaction_id: str, request_id: Any) -> None:
        try:
            response = await self.interactions.wait_for_response(
                interaction_id,
                timeout_seconds=self.config.approval_response_timeout_seconds,
            )
            await self.respond_success(request_id, response)
            row = self.storage.get_pending_interaction(interaction_id)
            if row is not None:
                params = {}
                try:
                    params = json.loads(str(row.get("params_json") or "{}"))
                except json.JSONDecodeError:
                    params = {}
                self.tracker.mark_interaction_resolved(str(row.get("method") or ""), params)
        except Exception as exc:
            LOG.warning("pending interaction resolution failed interaction_id=%s request_id=%s error=%s", interaction_id, request_id, exc)
            self.storage.update_pending_interaction(
                interaction_id,
                status="failed",
                resolved_at=_now_iso(),
                last_error=str(exc),
                event_type="failed",
                event_details={"reason": str(exc)},
            )


def _camel_active_work(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "pendingRequests": snapshot.get("pending_requests", 0),
        "activeTurns": snapshot.get("active_turns", []),
        "pendingInteractions": snapshot.get("pending_interactions", 0),
    }


def _first_text_input(input_items: list[dict[str, Any]]) -> str | None:
    for item in input_items:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            return text
    return None


def _result_turn_id(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("turnId"):
        return str(result["turnId"])
    turn = result.get("turn")
    if isinstance(turn, dict) and turn.get("id"):
        return str(turn["id"])
    return None
