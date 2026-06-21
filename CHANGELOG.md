# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project uses semantic
versioning after the public `0.1.0` release.

## [Unreleased]

## [0.2.1] - 2026-06-21

### Fixed

- Return canonical path-derived project ids from cached non-registry projects so
  `codex_list_projects`, preflight, chat tools, workflows, and durable task
  submission resolve the same project reference consistently.
- Keep stale cached project ids as aliases for compatibility while guiding
  agents to use the canonical `projectId` returned by `codex_list_projects`.
- Make Windows catalog regression tests deterministic when temporary paths are
  canonicalized differently before and after cleanup.

## [0.2.0] - 2026-06-20

### Added

- Add central MCP worker mode with `codex-control-plane-mcp-worker`.
- Add `CODEX_MCP_EXECUTION_MODE=inline|client|worker|observe`.
- Add worker, queue, concurrency, and worker command status tools.
- Add scheduler metadata for `agent_id`, `resource_keys`, priority, and
  estimated cost class.
- Add a self-describing agent contract with `codexMcpGuide`,
  `codex_get_agent_contract`, tool annotations, startup flows, recovery rules,
  and stable guide hashes.
- Add external MCP client tooling for live testing without restarting the Codex
  Desktop session.

### Changed

- `client` mode submit/status calls are passive and do not execute queued
  operations.
- Worker scheduling enforces global, per-project, per-agent, per-thread, and
  write resource limits before starting a turn.
- Stabilize the worker-first control plane for long-running parallel Codex
  tasks across different projects.
- Route write/control work through durable operations or worker commands, while
  status/read tools use bounded passive responses by default.
- Normalize queue, lock, worker, health, workflow, diagnostics, and guidance
  responses so agents can poll safely and avoid retry loops.
- Redact agent-facing responses consistently to avoid leaking raw prompts,
  local paths, account data, tokens, raw logs, or exact private usage details.

## [0.1.4] - 2026-06-19

### Added

- Add agent guidance for status, diagnostics, preflight, and structured error
  responses so MCP clients get a concrete next step instead of a dead-end
  failure.
- Add a persistent recovery loop guard ledger that tracks repair, restart, and
  interrupt attempts by stable scope and action.

### Changed

- Document `agentGuidance`, `agentGuidanceText`, and loop guard behavior in the
  API contract and OpenClaw client guides.
- Keep Plan Mode runtime recovery explicit: repair actions still start with
  `dry_run=true`, and clients must stop when `loopGuard.allowed=false`.

## [0.1.3] - 2026-06-18

### Added

- Add a richer app-server progress journal for assistant deltas, plan deltas, reasoning summaries, token usage, model reroutes, warnings, and safe diff statistics.
- Add runtime capabilities for models, permission profiles, sandbox readiness, hooks, skills, provider features, redacted account status, usage bands, and rate-limit state.
- Add durable `fork_thread`, `steer_turn`, structured final reports through `output_schema`, image inputs, thread lifecycle tools, workflow goal sync, and code review workflows.
- Restore and update the thin-wrapper comparison guide from PR #9.

### Changed

- Keep public defaults safe with `read-only` sandbox and `on-request` approval unless a caller or local config explicitly overrides them.
- Expand README and API documentation to cover the completed roadmap features and the privacy contract for account and rate-limit data.
- Align package, runtime server version, and MCP Registry metadata at `0.1.3`.

## [0.1.2] - 2026-06-17

### Added

- Add official MCP Registry metadata in `server.json`.
- Add a manual GitHub Actions workflow for publishing to the MCP Registry after PyPI is live.
- Add the PyPI README ownership proof for `io.github.aresyn/codex-control-plane-mcp`.

### Changed

- Keep package, runtime server version, and app-server client info aligned at `0.1.2`.
- Expand package keywords with Hermes, MCP server, automation, and orchestration terms.

## [0.1.1] - 2026-06-17

### Changed

- Make PyPI the first install path in the public README and PyPI long description.
- Add a PyPI badge to the English and Russian README files.
- Publish the patch release from the current clean public snapshot so GitHub deployments point at a visible release tag.

## [0.1.0] - 2026-06-17

### Added

- Public packaging as `codex-control-plane-mcp` with `Codex Control Plane MCP` branding.
- Compatibility aliases for the older `openclaw-codex-mcp` console commands and import package.
- Durable async operation queue for Codex app-server writes.
- Workflow-first Plan Mode orchestration with approve and execution polling.
- Pending interaction, approval, question, and interrupt control.
- Health, diagnostics, and allowlisted repair tools.
- MCP contract tests and live smoke harness.
- Public release metadata, license, security policy, contribution guide, CI, examples, and quickstart docs.
