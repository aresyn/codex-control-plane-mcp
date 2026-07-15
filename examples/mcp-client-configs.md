# MCP client config examples

Replace paths with your local checkout or installed command. The snippets below
avoid secrets and use placeholder paths; copy the shape, then replace the paths
with your own project root and optional config file.

## Claude Desktop / Claude app

Use this shape in the app MCP server configuration file. This is the safest
starting point after installing the package from PyPI.

```json
{
  "mcpServers": {
    "codex-control-plane-mcp": {
      "command": "codex-control-plane-mcp",
      "args": [],
      "env": {
        "CODEX_PROJECTS_ROOT": "C:\\Users\\you\\Projects",
        "CODEX_ALLOWED_ROOTS": "C:\\Users\\you\\Projects",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## Cursor

Cursor also accepts the standard `mcpServers` map. Add this to your Cursor MCP
configuration, then restart Cursor or reload MCP servers from settings.

```json
{
  "mcpServers": {
    "codex-control-plane-mcp": {
      "command": "codex-control-plane-mcp",
      "args": [],
      "env": {
        "CODEX_PROJECTS_ROOT": "C:\\Users\\you\\Projects",
        "CODEX_ALLOWED_ROOTS": "C:\\Users\\you\\Projects",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## VS Code MCP clients

For VS Code extensions that read an MCP server map, use the same stdio command.
Keep the command as an executable and put runtime options in `env` instead of
embedding shell-specific syntax.

```json
{
  "mcpServers": {
    "codex-control-plane-mcp": {
      "command": "codex-control-plane-mcp",
      "args": [],
      "env": {
        "CODEX_PROJECTS_ROOT": "C:\\Users\\you\\Projects",
        "CODEX_ALLOWED_ROOTS": "C:\\Users\\you\\Projects",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## Local checkout

Use a local checkout when developing this repository or testing unreleased
changes.

```json
{
  "mcpServers": {
    "codex-control-plane-mcp": {
      "command": "py",
      "args": ["-m", "codex_control_plane_mcp.server"],
      "cwd": "C:\\Users\\you\\Projects\\codex-control-plane-mcp",
      "env": {
        "CODEX_CONTROL_PLANE_MCP_CONFIG": "C:\\Users\\you\\Projects\\codex-control-plane-mcp\\examples\\codex-control-plane-mcp.config.json",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## Installed package

This is the smallest installed-package config. Add `CODEX_PROJECTS_ROOT` and
`CODEX_ALLOWED_ROOTS` so the server has an explicit project boundary.

```json
{
  "mcpServers": {
    "codex-control-plane-mcp": {
      "command": "codex-control-plane-mcp",
      "env": {
        "CODEX_PROJECTS_ROOT": "C:\\Users\\you\\Projects",
        "CODEX_ALLOWED_ROOTS": "C:\\Users\\you\\Projects",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## Central worker mode

Use this split when several MCP clients share one Codex Desktop/app-server
session. Client entries submit work and poll status; one separate worker process
executes queued operations.

Client config:

```json
{
  "mcpServers": {
    "codex-control-plane-mcp": {
      "command": "codex-control-plane-mcp",
      "args": [],
      "env": {
        "CODEX_MCP_EXECUTION_MODE": "client",
        "CODEX_MCP_STATE_DB": "C:\\Users\\you\\Projects\\codex-control-plane-mcp\\state\\codex-mcp-state.sqlite3",
        "CODEX_PROJECTS_ROOT": "C:\\Users\\you\\Projects",
        "CODEX_ALLOWED_ROOTS": "C:\\Users\\you\\Projects",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

Worker command:

```powershell
$env:CODEX_MCP_EXECUTION_MODE = "worker"
$env:CODEX_MCP_STATE_DB = "C:\\Users\\you\\Projects\\codex-control-plane-mcp\\state\\codex-mcp-state.sqlite3"
$env:CODEX_PROJECTS_ROOT = "C:\\Users\\you\\Projects"
$env:CODEX_ALLOWED_ROOTS = "C:\\Users\\you\\Projects"
codex-control-plane-mcp-worker
```

## Admin helper

```powershell
codex-control-plane-mcp-admin init --state-db .\state\codex-mcp-state.sqlite3 --projects-root C:\Users\you\Projects
```

The helper prints a ready MCP config JSON block and can install the bundled
Codex hooks.

## Smoke prompt

```text
MCP LIVE TEST / DO NOT MODIFY FILES

Read-only smoke test. Do not edit files or run destructive commands. Reply with
one short sentence confirming that the MCP server can reach Codex.
```
