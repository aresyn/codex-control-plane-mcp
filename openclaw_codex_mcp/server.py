from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from .errors import CodexMcpError
from .logging_utils import configure_logging, get_logger
from .protocol import call_tool_result
from .tools import ToolService, tools_list_payload


SERVER_INFO = {"name": "codex-control-plane-mcp", "version": __version__}
LOG = get_logger("server")


class StdioMcpServer:
    def __init__(self) -> None:
        configure_logging(Path(__file__).resolve().parents[1])
        self.service = ToolService()
        self._send_lock = asyncio.Lock()
        self._pending = 0
        self._input_closed = False

    async def run(self) -> None:
        LOG.info("mcp server run started")
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if line == "":
                    self._input_closed = True
                    LOG.info("stdin closed pending=%s", self._pending)
                    if self._pending == 0:
                        return
                    await asyncio.sleep(0.05)
                    continue
                if not line.strip():
                    continue
                await self._handle_line(line)
        finally:
            LOG.info("mcp server shutting down")
            await self.service.close()

    async def _handle_line(self, line: str) -> None:
        line = line.lstrip("\ufeff")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            LOG.warning("json parse error length=%s error=%s", len(line), exc)
            await self._send_error(None, -32700, "Parse error", {"message": str(exc)})
            return
        if "id" not in message:
            LOG.info("notification method=%s", message.get("method"))
            await self._handle_notification(message)
            return
        self._pending += 1
        method = str(message.get("method") or "")
        request_id = message.get("id")
        started = time.monotonic()
        LOG.info("request start id=%s method=%s", request_id, method)
        try:
            result = await self._handle_request(method, message.get("params") or {})
            await self._send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
            LOG.info("request ok id=%s method=%s elapsed_ms=%d", request_id, method, int((time.monotonic() - started) * 1000))
        except RpcError as exc:
            LOG.warning(
                "request rpc error id=%s method=%s code=%s message=%s elapsed_ms=%d",
                request_id,
                method,
                exc.code,
                exc.message,
                int((time.monotonic() - started) * 1000),
            )
            await self._send_error(message.get("id"), exc.code, exc.message, exc.data)
        except Exception as exc:
            LOG.error(
                "request internal error id=%s method=%s elapsed_ms=%d error=%s\n%s",
                request_id,
                method,
                int((time.monotonic() - started) * 1000),
                exc,
                traceback.format_exc(),
            )
            await self._send_error(message.get("id"), -32603, "Internal error", {"message": str(exc)})
        finally:
            self._pending -= 1

    async def _handle_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        if method == "tools/list":
            return tools_list_payload()
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RpcError(-32602, "params.name must be a non-empty string")
            LOG.info("tool call start name=%s argument_keys=%s", name.strip(), sorted((params.get("arguments") or {}).keys()))
            result = await self.service.call(name.strip(), params.get("arguments") or {})
            tool_result = call_tool_result(result)
            is_error = bool(tool_result.get("isError"))
            LOG.info("tool call done name=%s is_error=%s", name.strip(), is_error)
            return tool_result
        raise RpcError(-32601, f"Method not found: {method}")

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        if message.get("method") in {"notifications/initialized", "initialized"}:
            return

    async def _send_error(self, request_id: Any, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        LOG.warning("sending error id=%s code=%s message=%s data_keys=%s", request_id, code, message, sorted((data or {}).keys()))
        await self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message, "data": data or {}}})

    async def _send(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def main() -> None:
    try:
        asyncio.run(StdioMcpServer().run())
    except KeyboardInterrupt:
        LOG.info("keyboard interrupt")
        return
    except Exception as exc:
        configure_logging(Path(__file__).resolve().parents[1])
        LOG.critical("fatal server error: %s\n%s", exc, traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
