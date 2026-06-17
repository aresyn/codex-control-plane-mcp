# Contributing

Thanks for helping improve `codex-control-plane-mcp`.

## Development setup

```powershell
git clone https://github.com/aresyn/codex-control-plane-mcp.git
cd codex-control-plane-mcp
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Run the core checks before sending a pull request:

```powershell
python -m pytest -q
python -m compileall -q openclaw_codex_mcp codex_control_plane_mcp tests scripts
git diff --check
```

## Design rules

- Keep write/control operations on the Codex app-server path.
- Do not mutate Codex internal SQLite databases or transcript files.
- Durable work must be asynchronous and pollable.
- Public tool errors must use structured MCP payloads.
- Do not log secrets, raw API keys, raw secret answers, or full sensitive prompts.
- Keep stdout reserved for MCP JSON-RPC frames.

## Pull requests

Good pull requests include:

- a small behavior summary
- tests for changed behavior
- documentation updates when public behavior changes
- the output of the checks above

Live tests that use a real Codex Desktop/app-server environment are optional for
external contributors, but required before maintainers cut a release.
