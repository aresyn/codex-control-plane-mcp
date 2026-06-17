# Security policy

## Supported versions

`codex-control-plane-mcp` is currently pre-1.0. Security fixes are shipped on the
latest release line only.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities or secret leaks.

Report security issues through GitHub private vulnerability reporting after the
repository is published. Until then, contact the repository maintainer directly.

Please include:

- affected version or commit
- operating system and Python version
- reproduction steps
- whether the issue can expose prompts, logs, Codex state, environment files, or API keys

## Security boundaries

This server is a local control plane for Codex Desktop. Do not expose it as a
network service without an authentication and authorization layer.

The MCP server:

- never mutates Codex internal SQLite databases or transcript files
- writes only to its own local state/log directories
- redacts known secret patterns from diagnostics
- keeps MCP protocol output on stdout separate from logs

User prompts and Codex transcripts can contain sensitive content. Treat state
databases, logs, summaries, and diagnostics as local private data.
