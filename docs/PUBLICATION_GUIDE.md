# Publication guide

Use this guide when preparing the public GitHub launch for
`codex-control-plane-mcp`.

## Positioning

Lead with the control-plane story:

- async `operationId` / `workflowId` instead of long blocking MCP calls
- duplicate prompt and duplicate turn protection
- Plan Mode workflow: plan, poll, approve, execute, final report
- pending approvals/questions visible to the orchestrator
- hook-backed SQLite history for search, summaries, and fallback reads
- health, diagnostics, and safe repairs for local Codex app-server work

Be explicit about the current support line. The full live target is Windows with
Codex Desktop and app-server. Protocol-only checks can run elsewhere, but do not
market Linux/macOS as fully supported until live workflow tests exist there.

## GitHub repo setup

Target repository:

```text
https://github.com/aresyn/codex-control-plane-mcp
```

Recommended topics:

```text
ai-agents
agentic-workflows
agent-tools
automation
codex
codex-desktop
developer-tools
long-running-tasks
mcp
mcp-server
model-context-protocol
openai-codex
openclaw
orchestration
python
```

Recommended settings:

- enable private vulnerability reporting
- enable Dependabot security updates
- protect `main` with required CI after the first successful workflow run
- use GitHub releases for every tagged version
- pin a launch issue that tracks install feedback

## Launch assets

Prepare before announcing:

- a README that explains the durable workflow in the first screen
- a copy-paste PyPI install command
- a copy-paste MCP client config snippet
- a short release note with known limitations
- a safe demo prompt containing `MCP LIVE TEST / DO NOT MODIFY FILES`

The demo video/GIF is intentionally out of scope for this launch iteration.

## Discovery channels

After the first public release:

- submit to MCP directories and awesome lists where install is reproducible
- share the durable workflow difference from thin CLI wrapper MCP servers
- open a short discussion/post for Codex Desktop users
- ask early users for MCP client config examples and install failures

Do not submit to package directories that require Docker-only or remote-hosted
execution until that mode is tested. This server is local-first.

## README checklist

- clear first sentence
- badges that work after the repository is public
- quickstart in the first screen
- compact workflow block
- tool surface grouped by stable vs compatibility
- honest limitations
- security and privacy notes
- links to API contract, release checklist, and examples
