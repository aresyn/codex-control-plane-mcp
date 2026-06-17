# MCP client config examples

Replace paths with your local checkout or installed command.

## Local checkout

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
