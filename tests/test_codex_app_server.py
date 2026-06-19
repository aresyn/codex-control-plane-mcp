from __future__ import annotations

from unittest.mock import patch

from tests.helpers import *

from openclaw_codex_mcp.codex_app_server import CodexAppServerClient


class _FakeProcess:
    pid = 4242
    returncode = None
    stdin = object()
    stdout = object()
    stderr = object()

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class _FakeStdin:
    def __init__(self, *, drain_error: Exception | None = None) -> None:
        self.lines: list[bytes] = []
        self.drain_error = drain_error

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error


class CodexAppServerClientTests(unittest.TestCase):
    def test_start_passes_configured_codex_home_to_app_server_env(self) -> None:
        async def scenario() -> tuple[dict, dict]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                config.codex_binary_path.write_text("", encoding="utf-8")
                storage = McpStorage(root / "mcp.sqlite")
                storage.connect()
                client = CodexAppServerClient(config, storage)
                captured: dict[str, object] = {}

                async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> _FakeProcess:
                    captured["args"] = args
                    captured["kwargs"] = kwargs
                    return _FakeProcess()

                async def fake_request(method: str, params: dict | None, timeout_seconds: float | None = None) -> dict:
                    return {"method": method, "params": params, "timeoutSeconds": timeout_seconds}

                async def fake_notify(method: str, params: dict) -> None:
                    captured["notify"] = {"method": method, "params": params}

                async def noop() -> None:
                    return None

                client.request = fake_request  # type: ignore[method-assign]
                client.notify = fake_notify  # type: ignore[method-assign]
                client._read_stdout_loop = noop  # type: ignore[method-assign]
                client._read_stderr_loop = noop  # type: ignore[method-assign]

                old_home = os.environ.get("CODEX_HOME")
                os.environ["CODEX_HOME"] = str(root / "wrong-home")
                try:
                    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                        await client.start()
                    status = client.status_snapshot()
                    return captured, status
                finally:
                    await client.stop()
                    storage.close()
                    if old_home is None:
                        os.environ.pop("CODEX_HOME", None)
                    else:
                        os.environ["CODEX_HOME"] = old_home

        captured, status = asyncio.run(scenario())
        kwargs = captured["kwargs"]
        self.assertIsInstance(kwargs, dict)
        env = kwargs["env"]
        self.assertIsInstance(env, dict)
        self.assertEqual(str(status["codexHome"]), env["CODEX_HOME"])
        self.assertNotEqual(env["CODEX_HOME"], str(Path(status["codexHome"]).parent / "wrong-home"))
        self.assertEqual("app-server", captured["args"][1])

    def test_audit_write_failure_does_not_leak_pending_request(self) -> None:
        async def scenario() -> tuple[dict, int, str | None]:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                storage = McpStorage(root / "mcp.sqlite")
                storage.connect()
                client = CodexAppServerClient(config, storage)
                process = _FakeProcess()
                process.stdin = _FakeStdin()
                client.process = process  # type: ignore[assignment]

                def fail_audit(*args: object, **kwargs: object) -> None:
                    raise sqlite3.OperationalError("database is locked")

                storage.record_app_server_event = fail_audit  # type: ignore[method-assign]
                try:
                    task = asyncio.create_task(client.request("model/list", {}, timeout_seconds=1))
                    await asyncio.sleep(0)
                    self.assertEqual(1, len(client._pending))
                    await client._handle_stdout_line(b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}')
                    result = await task
                    return result, len(client._pending), client.last_error
                finally:
                    await client.stop()
                    storage.close()

        result, pending_count, last_error = asyncio.run(scenario())

        self.assertEqual({"ok": True, "_requestId": 1, "_processGeneration": 0}, result)
        self.assertEqual(0, pending_count)
        self.assertIn("audit write failed", last_error or "")

    def test_send_failure_cleans_pending_request(self) -> None:
        async def scenario() -> int:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = _search_service_config(root, root / ".codex" / "state_5.sqlite")
                storage = McpStorage(root / "mcp.sqlite")
                storage.connect()
                client = CodexAppServerClient(config, storage)
                process = _FakeProcess()
                process.stdin = _FakeStdin(drain_error=BrokenPipeError("pipe closed"))
                client.process = process  # type: ignore[assignment]
                try:
                    with self.assertRaises(BrokenPipeError):
                        await client.request("model/list", {}, timeout_seconds=1)
                    return len(client._pending)
                finally:
                    await client.stop()
                    storage.close()

        pending_count = asyncio.run(scenario())

        self.assertEqual(0, pending_count)
