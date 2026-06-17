# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project uses semantic
versioning after the public `0.1.0` release.

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
