# Release checklist

Use this checklist before tagging a public release or pushing a public snapshot.

## Public hygiene

Run before release:

```powershell
git status --short --ignored
git ls-files
git ls-files .codex/config.toml
rg -n --glob '!tests/**' --glob '!docs/RELEASE_CHECKLIST.md' "sk-[A-Za-z0-9_-]{12,}|Bearer [A-Za-z0-9._~+/=-]{12,}|DEEPSEEK_API_KEY=.+" .
rg -n --glob '!tests/**' --glob '!docs/RELEASE_CHECKLIST.md' "<add-your-private-path-or-domain-patterns-here>" .
```

Expected result:

- no tracked `state/`, `logs/`, `.env`, `.codex/`, caches, or build artifacts
- no personal absolute paths in public docs or code defaults
- no real API keys, tokens, env files, private prompts, private issue URLs, or full transcripts
- `git ls-files .codex/config.toml` prints nothing

Publish GitHub from a clean snapshot commit, not from the private development
history.

## Automated checks

Run from the repository root:

```powershell
python -m pytest -q
python -m compileall -q openclaw_codex_mcp codex_control_plane_mcp tests scripts
python -m build
git diff --check
```

Expected result:

- pytest passes
- compileall exits with code 0
- build produces wheel and sdist
- `git diff --check` emits no whitespace errors

## Protocol smoke

Run:

```powershell
python .\scripts\mcp_live_smoke.py --scenario protocol
```

Expected result:

- `ok=true`
- `initialize.protocolVersion` is returned
- `initialize.serverInfo.name == "codex-control-plane-mcp"`
- `toolsList.hasOutputSchema=true`
- `toolsList.hasHealthSummary=true`
- invalid tool call returns structured `INVALID_ARGUMENT`
- invalid MCP method returns JSON-RPC `-32601`
- no debug/log text appears on stdout

## Hook history smoke

Run after installing the package in the target environment:

```powershell
codex-control-plane-mcp-hooks install --state-db .\state\codex-mcp-state.sqlite3
codex-control-plane-mcp-hooks status
codex-control-plane-mcp-hooks doctor
```

Expected result:

- existing `~/.codex/hooks.json` is backed up before changes
- user hooks remain present
- hook config stores `stateDb` as an absolute path
- `UserPromptSubmit`, `Stop`, `SessionStart`, `PreCompact`, and `PostCompact` are installed
- `doctor.dbWritable=true`
- `codex_health_summary.hookHistory` is present after MCP reconnect
- app-server live smoke writes at least the accepted prompt and one visible assistant message into `codex_hook_messages`

## Live smoke

Use only safe prompts with the marker `MCP LIVE TEST / DO NOT MODIFY FILES`.

Recommended:

```powershell
python .\scripts\mcp_live_smoke.py --scenario safe-operation --cwd <PROJECT_ROOT>
```

For a full regression, run only when app-server is healthy and no important work
is active:

```powershell
python .\scripts\mcp_live_smoke.py --scenario full --safe-restart --cwd <PROJECT_ROOT>
```

Expected live checks:

- current project path casing has exactly one match when possible
- `codex_submit_task` returns an `operationId`
- operation reaches a terminal status or reports a clear diagnostic next action
- duplicate prompt behavior is machine-readable
- diagnostics include `diagnosisConfidence`
- diagnostics include `hookHistory`
- repair dry-run does not change state
- safe restart runs only when active work is zero

## Reconnect probe

After an MCP client reconnects, call:

```text
codex_health_summary
```

Check:

- `version.serverName == "codex-control-plane-mcp"`
- `version.contractVersion == "1"`
- `version.toolSurfaceHash` is known for the deployed build
- all stable tools are listed in `version.stableTools`
- `overallStatus` is `healthy` or a degraded state with actionable diagnostics

## Git clean rule

Every completed iteration must end with a commit.

Before the final report:

```powershell
git status --short
```

Expected result in the private working repo: only intentionally ignored local
files may remain untracked or modified. The public export snapshot must be clean.

## Packaging

Build and inspect the release artifacts:

```powershell
python -m pip install build
python -m build
Get-ChildItem dist
```

Install in a clean virtual environment and verify scripts:

```powershell
py -m venv .venv-release
.\.venv-release\Scripts\Activate.ps1
python -m pip install .\dist\codex_control_plane_mcp-0.1.0-py3-none-any.whl
python -c "import codex_control_plane_mcp, openclaw_codex_mcp; print(codex_control_plane_mcp.__version__, openclaw_codex_mcp.__version__)"
codex-control-plane-mcp-hooks status --codex-home .\tmp-codex-home
codex-control-plane-mcp-admin print-config
```

The server command is stdio-first. Use the protocol smoke for runtime
verification instead of expecting a long `--help` screen.

## GitHub launch

- license, security policy, contributing guide, code of conduct, changelog exist
- CI is green on the release commit
- README quickstart works on a clean checkout
- repository URL is `https://github.com/aresyn/codex-control-plane-mcp`
- GitHub topics are configured: `mcp`, `model-context-protocol`, `openai-codex`, `codex-desktop`, `agent-tools`, `python`, `openclaw`
- release notes mention Windows/Codex Desktop as the full live target

## Troubleshooting

If a client does not see a new tool:

- restart/reconnect the MCP server
- run `python .\scripts\mcp_live_smoke.py --scenario protocol`
- compare `codex_health_summary.version.toolSurfaceHash`

If app-server appears stuck:

- call `codex_health_summary`
- call `codex_collect_diagnostics` with `operation_id` or `workflow_id`
- run `codex_repair_issue` first with default `dry_run=true`
- run `restart_app_server_idle` only when active work is zero

If a prompt may have duplicated:

- poll `codex_get_operation_status`
- inspect `dedupState`
- use `codex_analyze_issue` and `codex_collect_diagnostics` before repair
