# External MCP Client

This helper lets us test the MCP server like a real external client without
restarting the current Codex Desktop session.

The client has two modes:

- `oneshot`: starts `python -m codex_control_plane_mcp.server`, runs one command,
  then exits.
- `daemon`: starts a long-lived localhost supervisor. The daemon owns one MCP
  stdio subprocess and can restart only that subprocess after code changes.

State and logs are stored under `work/external_mcp_client/`.

## Common Commands

```powershell
python .\scripts\external_mcp_client.py smoke

python .\scripts\external_mcp_client.py daemon-start
python .\scripts\external_mcp_client.py daemon-status
python .\scripts\external_mcp_client.py tools-list --compact
python .\scripts\external_mcp_client.py call codex_get_worker_status --daemon --json "{}"

python .\scripts\external_mcp_client.py daemon-restart-mcp --reason after_code_change
python .\scripts\external_mcp_client.py run-live-test --scenario baseline --archive-report
python .\scripts\external_mcp_client.py daemon-stop
```

By default the helper loads the environment from the local `.codex/config.toml`
MCP entry named `openclaw-codex`, then applies:

```text
CODEX_MCP_EXECUTION_MODE=client
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
```

That keeps the external test client fingerprint aligned with the central worker.
For local sandbox live tests, point the helper at your own Codex projects root:

```text
CODEX_ALLOWED_ROOTS=<path-to-your-codex-projects-root>;<optional-second-root>
```

If the local entry is absent, `daemon-start` falls back to the existing parent
directory of the three test projects. For another machine or a narrower test
scope, pass roots explicitly:

```powershell
python .\scripts\external_mcp_client.py daemon-start --allowed-root <path-to-your-codex-projects-root>
```

The live-test scenarios look for three sandbox projects named `TestProject1`,
`TestProject2`, and `TestProject3` under the configured project root. You can
override that discovery with `CODEX_MCP_TEST_PROJECT_ROOT` or provide exact
paths through `CODEX_MCP_TEST_SANDBOXES`.

The daemon listens on `127.0.0.1:18891` and requires a random control token from
`work/external_mcp_client/state.json`.

## Why This Exists

Codex Desktop only reloads MCP entries when the session is restarted. That is
inconvenient during MCP development. This external client starts the MCP server
directly from the current checkout, so after a code change we can run:

```powershell
python .\scripts\external_mcp_client.py daemon-restart-mcp --reason after_fix
```

Then live tests use the updated MCP code without touching the Codex Desktop
session.

## Baseline Scenario

The baseline scenario checks:

- worker status;
- queue status;
- concurrency status;
- app-server status;
- health summary;
- runtime capabilities refresh;
- sandbox project discovery;
- sandbox preflight for:
  - `<sandbox-root>\TestProject1`
  - `<sandbox-root>\TestProject2`
  - `<sandbox-root>\TestProject3`

Findings are written to `corrective_action_plan.md`. The old report can be
archived automatically with `--archive-report`.
