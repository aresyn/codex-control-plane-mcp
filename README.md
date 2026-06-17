# Codex Control Plane MCP

English | [Русский](README.ru.md)

[![CI](https://github.com/aresyn/codex-control-plane-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/aresyn/codex-control-plane-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/codex-control-plane-mcp.svg)](https://pypi.org/project/codex-control-plane-mcp/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](docs/API_CONTRACT.md)

<p align="center">
  <img src="https://raw.githubusercontent.com/aresyn/codex-control-plane-mcp/main/assets/repo_image.png" alt="Codex Control Plane MCP" width="100%">
</p>

Reliable Codex Desktop automation for long tasks.

`codex-control-plane-mcp` turns Codex Desktop and `codex-app-server` into a
durable worker that an MCP client can drive safely. Send a task, get an
`operationId` or `workflowId` right away, poll until the work finishes, approve
Plan Mode when needed, then read the final report.

The server handles the awkward parts that thin wrappers usually leave to the
caller: app-server startup, thread and turn creation, retry safety, duplicate
prompt protection, Plan Mode, approvals, local history, diagnostics, and repair.

OpenClaw and Hermes are first-class clients, but the server is useful for any
local orchestrator that needs Codex Desktop to do long-running work without
holding one MCP call open for hours.

## The short version

```text
MCP client / orchestrator
  -> submit a task or start a Plan Mode workflow
  <- receive operationId or workflowId immediately
  -> poll status
  -> answer approvals or approve the plan
  <- read final report, diagnostics, threadId, and turnId
```

That gives you a simple contract:

- no multi-hour MCP calls;
- no duplicate Codex turns after a client retry;
- no blind fire-and-forget task submission;
- a local SQLite record of operations, workflows, turns, hooks, and diagnostics.

## Why not just call Codex directly?

| Capability | Thin Codex wrapper | Codex Control Plane MCP |
|---|---:|---:|
| Multi-hour tasks | blocking / fragile | durable async operation |
| Client timeout recovery | manual | retry-safe `client_request_id` |
| Duplicate turn protection | no | active prompt detection |
| Plan Mode workflow | human / manual | pollable workflow state |
| Approvals and questions | blocking / opaque | pending interactions API |
| Restart recovery | ad hoc | persisted operation state |
| Diagnostics | logs only | health, diagnostics, repair tools |

## Current support

- Full live target: Windows with Codex Desktop and `codex-app-server`.
- Linux and macOS: protocol-only checks for now.
- Local-first: not intended to be exposed as a public network service.

## Security model

This is a local-first control plane for trusted Codex Desktop environments.

Do not expose it as a network service without authentication.

Recommended first-run posture:

- use `read-only` for untrusted repositories;
- use `on-request` approval when testing new workflows;
- keep `state/`, `logs/`, `.env`, and `.codex/` private.

## What it does

- Durable async queue for Codex write operations.
- Retry-safe `client_request_id` handling.
- Active duplicate prompt detection.
- SQLite leases and heartbeats for competing MCP processes.
- Recovery after MCP restart during `thread/start` or `turn/start`.
- Plan Mode workflows: start plan, poll, approve, execute, read final report.
- Pending approvals and questions exposed as pollable MCP state.
- Turn interrupts by `threadId`/`turnId`, `operationId`, or `workflowId`.
- Health checks, diagnostics, issue analysis, and dry-run repairs.
- MCP-owned hook history in SQLite for search, summaries, and fallback reads.
- Structured MCP errors that automation code can branch on.

Write and control actions go through `codex-app-server`. The server does not
mutate Codex internal SQLite databases or transcript files.

## Install

Recommended:

```powershell
pipx install codex-control-plane-mcp
```

Or run directly:

```powershell
uvx codex-control-plane-mcp
```

From GitHub:

```powershell
python -m pip install "codex-control-plane-mcp @ git+https://github.com/aresyn/codex-control-plane-mcp.git"
```

For local development:

```powershell
git clone https://github.com/aresyn/codex-control-plane-mcp.git
cd codex-control-plane-mcp
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

## MCP client config

After installation, generate a config:

```powershell
codex-control-plane-mcp-admin init --state-db .\state\codex-mcp-state.sqlite3 --projects-root C:\Users\you\Projects
```

Minimal stdio entry:

```json
{
  "mcpServers": {
    "codex-control-plane": {
      "command": "codex-control-plane-mcp",
      "args": []
    }
  }
}
```

Run the MCP stdio server:

```powershell
codex-control-plane-mcp
```

Or run it as a module:

```powershell
py -m codex_control_plane_mcp.server
```

The old `openclaw-codex-mcp` and `openclaw-codex-mcp-hooks` commands remain as
compatibility aliases for one release line.

## First setup

The admin helper can generate a fuller client config, install hooks, and run a
protocol smoke:

```powershell
codex-control-plane-mcp-admin init --state-db .\state\codex-mcp-state.sqlite3 --projects-root C:\Users\you\Projects
```

The command prints a JSON block you can copy into an MCP client config. It does
not print secrets or private prompts.

You can also install only the Codex hooks:

```powershell
codex-control-plane-mcp-hooks install --state-db .\state\codex-mcp-state.sqlite3
codex-control-plane-mcp-hooks status
codex-control-plane-mcp-hooks doctor
```

The installer backs up `~/.codex/hooks.json`, merges its handlers with your
existing hooks, stores `stateDb` as an absolute path, and writes prompts, visible
agent progress text, final answers, and turn status into the MCP state DB. Tool
calls and command outputs are not recorded by default. Restart Codex after
installing or changing hooks.

For turns launched through `codex-app-server`, the server mirrors the accepted
prompt, visible assistant messages, and turn status into the same SQLite
history. That keeps search and status reads useful even when app-server does not
execute user hooks itself.

## Main workflows

Submit a durable task:

```text
codex_submit_task
  -> operationId
codex_get_operation_status(operationId)
  -> queued / running / waiting_for_approval / completed / failed
```

Use the same `client_request_id` when a caller retries after a transport timeout.
The retry returns the existing operation instead of creating another turn.

Drive Plan Mode:

```text
codex_start_plan_workflow
  -> workflowId
codex_get_workflow_status(workflowId)
  -> wait_plan / review_plan / execute_plan
codex_approve_plan(workflowId)
  -> executionOperationId
codex_get_workflow_status(workflowId)
  -> finalReport
```

Handle approvals and questions:

```text
codex_list_pending_interactions
codex_answer_pending_interaction
```

Start diagnostics with:

```text
codex_health_summary
codex_collect_diagnostics
codex_analyze_issue
codex_repair_issue
```

Repair actions default to `dry_run=true`.

## Tool surface

Stable orchestration tools:

- `codex_submit_task`
- `codex_get_operation_status`
- `codex_start_plan_workflow`
- `codex_get_workflow_status`
- `codex_approve_plan`
- `codex_list_pending_interactions`
- `codex_answer_pending_interaction`
- `codex_interrupt_turn`
- `codex_health_summary`
- `codex_collect_diagnostics`
- `codex_repair_issue`

Compatibility and read tools:

- `codex_start_chat`
- `codex_send_message`
- `codex_execute_plan`
- `codex_list_projects`
- `codex_list_project_chats`
- `codex_list_active_chats`
- `codex_search_chats`
- `codex_get_chat_status`
- `codex_get_chat`
- `codex_get_turn_status`
- `codex_restart_app_server`
- `codex_get_app_server_status`
- `codex_get_diagnostic_logs`
- `codex_analyze_issue`

New clients should use durable operations and workflows. Low-level write tools
stay available for compatibility.

See [docs/API_CONTRACT.md](docs/API_CONTRACT.md) for schemas, error shape,
stable tool groups, and versioning rules.

## Result contract

Every tool declares an `outputSchema` and returns MCP `structuredContent`.

Success:

```json
{"ok": true}
```

Domain or tool error:

```json
{
  "ok": false,
  "error": {
    "code": "CODEX_ERROR_CODE",
    "message": "Human readable message",
    "details": {},
    "retryable": false
  }
}
```

Call `codex_health_summary` on startup and reconnect. The `version` block
contains `serverName`, `serverVersion`, `contractVersion`, `toolSurfaceHash`,
and stable/compatibility tool lists.

## Configuration

Configuration can come from environment variables or from a JSON file referenced
by `CODEX_CONTROL_PLANE_MCP_CONFIG`. The old `OPENCLAW_CODEX_MCP_CONFIG` name is
still accepted as a fallback.

Common variables:

- `CODEX_HOME`: Codex home directory. Defaults to `%USERPROFILE%\.codex`.
- `CODEX_PROJECTS_ROOT`: project root scanned by catalog and read tools.
- `CODEX_ALLOWED_ROOTS`: semicolon-separated path allowlist.
- `CODEX_PROJECTS_REGISTRY`: optional JSON project registry.
- `CODEX_MCP_STATE_DB`: local MCP state DB.
- `CODEX_CONTROL_PLANE_MCP_LOG`: log file path.
- `CODEX_MCP_HOOK_HISTORY_ENABLED`: enables SQLite hook history. Defaults to `true`.
- `CODEX_MCP_HOOK_HISTORY_MAX_TEXT_CHARS`: per-message hook capture limit.
- `CODEX_KB_HISTORY_PROJECTS_ROOT`: optional legacy normalized KB history root.
- `CODEX_BINARY_PATH`: optional explicit Codex binary path.
- `CODEX_MCP_DEFAULT_SANDBOX`: default write sandbox. Defaults to `danger-full-access`.
- `CODEX_MCP_DEFAULT_APPROVAL_POLICY`: default write approval policy. Defaults to `never`.
- `CODEX_MCP_DEFAULT_MODEL`: default Codex model passed to app-server.
- `CODEX_MCP_DEFAULT_EFFORT`: default effort level.
- `CODEX_MCP_APPROVAL_RESPONSE_TIMEOUT_SECONDS`: pending interaction timeout.
- `DEEPSEEK_ENV_PATH`: optional `.env` file for DeepSeek summary settings.
- `DEEPSEEK_SUMMARY_ENABLED`: enables or disables remote summary calls.

The write policy values are defaults, not hard limits. A client call can pass
`sandbox` or `approval_policy` explicitly, for example to run one task in
`read-only` or `on-request` mode.

Example:

```powershell
$env:CODEX_CONTROL_PLANE_MCP_CONFIG = Join-Path (Get-Location) "examples\codex-control-plane-mcp.config.json"
$env:CODEX_MCP_DEFAULT_SANDBOX = "danger-full-access"
$env:CODEX_MCP_DEFAULT_APPROVAL_POLICY = "never"
py -m codex_control_plane_mcp.server
```

See [examples/codex-control-plane-mcp.config.json](examples/codex-control-plane-mcp.config.json).

## Reliability model

The server is built for common local orchestration failures:

- MCP client timeout after task submission.
- Repeated submit with the same `client_request_id`.
- Repeated submit without an idempotency key but with the same active prompt.
- MCP process restart between app-server `thread/start` and `turn/start`.
- Two MCP processes sharing one SQLite state DB.
- App-server exit while a turn is active.
- Pending approval tied to an old app-server generation.
- App-server or transcript gaps where hook history still captured the prompt,
  visible agent text, final answer, and completion status.

These cases are stored in durable operation, workflow, turn, hook, and pending
interaction state. Terminal statuses are explicit.
`unknown_after_app_server_exit` is not treated as success.

## Safety

- Live smoke prompts must include `MCP LIVE TEST / DO NOT MODIFY FILES`.
- Repairs default to `dry_run=true`.
- Forced app-server restart can mark active turns as unknown or orphaned. Prefer
  `restart_app_server_idle`.

## Checks

Fast local checks:

```powershell
python -m pytest -q
python -m compileall -q openclaw_codex_mcp codex_control_plane_mcp tests scripts
git diff --check
```

Protocol-only MCP smoke:

```powershell
python .\scripts\mcp_live_smoke.py --scenario protocol
```

Safe live smoke with real Codex Desktop/app-server:

```powershell
python .\scripts\mcp_live_smoke.py --scenario safe-operation --cwd <PROJECT_ROOT>
```

Full live regression:

```powershell
python .\scripts\mcp_live_smoke.py --scenario full --safe-restart --cwd <PROJECT_ROOT>
```

See [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md). For public launch
positioning, see [docs/PUBLICATION_GUIDE.md](docs/PUBLICATION_GUIDE.md).

## Packaging

Build locally:

```powershell
python -m pip install build
python -m build
```

The wheel includes the MCP server, the hook installer, the admin helper, and the
bundled Codex hook module.

The normal install path is:

```powershell
pipx install codex-control-plane-mcp
```

or:

```powershell
uvx codex-control-plane-mcp
```

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before
opening issues that include diagnostics.

Good GitHub topics for this repo:

`python`, `mcp`, `mcp-server`, `model-context-protocol`, `openai-codex`,
`codex`, `codex-desktop`, `agent-tools`, `ai-agents`, `developer-tools`,
`automation`, `orchestration`, `agentic-workflows`, `long-running-tasks`,
`openclaw`.
