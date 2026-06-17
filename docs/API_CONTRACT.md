# Codex Control Plane MCP API contract

Contract version: `1`.

MCP clients should call `codex_health_summary` on startup or reconnect and
verify:

- `version.serverName == "codex-control-plane-mcp"`
- `version.contractVersion == "1"`
- `version.toolSurfaceHash` is present and stable for the installed build
- required stable tools are present in `version.stableTools`

## MCP protocol

- Server entrypoint: `python -m codex_control_plane_mcp.server`
- Hook installer entrypoint: `codex-control-plane-mcp-hooks`
- Admin helper entrypoint: `codex-control-plane-mcp-admin`
- Legacy aliases: `openclaw-codex-mcp`, `openclaw-codex-mcp-hooks`
- Transport: MCP JSON-RPC over stdio
- `stdout`: JSON-RPC frames only
- Diagnostics/logs: file-only, default `logs/server.log`
- Domain/tool errors: returned from `tools/call` with `result.isError=true`
- JSON-RPC errors: reserved for protocol errors such as invalid methods or bad request params

Every tool declares `outputSchema`. Every tool result mirrors the same payload
in:

- `result.structuredContent`
- `result.content[0].text` as formatted JSON

Success:

```json
{"ok": true}
```

Domain/tool error:

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

## Stable orchestration tools

These tools are the supported surface for long-running Codex orchestration:

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

Stable tools are asynchronous or pollable when they can trigger long work. New
fields may be added, but existing input fields and machine-readable status/error
fields must not be removed without changing `contractVersion`.

## Write policy defaults

Server-level default write policy is configured by environment/config:

- `CODEX_MCP_DEFAULT_SANDBOX`, default `danger-full-access`
- `CODEX_MCP_DEFAULT_APPROVAL_POLICY`, default `never`
- JSON config fields `default_sandbox_policy` and `default_approval_policy`

Explicit tool arguments always win over server defaults. A client may submit one
task with `sandbox="read-only"` or `approval_policy="on-request"` without
changing the server configuration.

## Durable operation types

`codex_submit_task` supports these operation types:

- `start_chat`: create a Codex thread and start a turn.
- `send_message`: resume an existing thread and start a new turn.
- `execute_plan`: execute an approved Plan Mode workflow or existing chat plan.
- `steer_turn`: send extra text to an active turn through app-server `turn/steer`.

`steer_turn` requires `thread_id`, `expected_turn_id`, and `message`. It does
not create a new turn and does not participate in prompt duplicate detection.
After app-server accepts the steering input, the operation remains `running`
and follows the target turn until the turn reaches a terminal state.

For strict retry safety, pass `client_request_id`. Reusing the same
`client_request_id` returns the same steering operation and does not send a
second `turn/steer` request. Calls without `client_request_id` are treated as
new steering commands.

Status payloads for `steer_turn` include normal operation fields plus:

- `steerState.accepted`
- `steerState.targetThreadId`
- `steerState.targetTurnId`
- `steerState.clientUserMessageId`

If the target turn is missing, MCP returns `CODEX_TURN_NOT_FOUND`. If the target
turn is terminal or belongs to another thread, MCP returns `INVALID_ARGUMENT`.

## Compatibility tools

These tools remain available for UI support, direct reads, diagnostics, and old
clients, but new long-running write paths should use durable operations and
workflows:

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

Low-level write compatibility tools return after `turn/start`. Prefer
`codex_submit_task` and polling for retry safety.

## Version block

`codex_health_summary.version` contains:

- `serverName`
- `serverVersion`
- `contractVersion`
- `toolSurfaceHash`
- `stableToolCount`
- `compatibilityToolCount`
- `stableTools`
- `compatibilityTools`
- `generatedAt`

`toolSurfaceHash` is a SHA-256 hash over tool names, descriptions, input/output
schemas, and contract groups. It is a fast compatibility probe, not a security
signature.

## Hook history block

`codex_health_summary` and `codex_collect_diagnostics` include a compact
`hookHistory` block:

- `enabled`
- `status`
- `installed`
- `events`
- `hooksJson`
- `configPath`
- `dbWritable`
- `threadCount`
- `turnCount`
- `messageCount`
- `lastHookEventAt`
- `warnings`

Top-level compatibility aliases are also returned: `hookHistoryStatus`,
`lastHookEventAt`, `hookInstalled`, and `hookDbWritable`.

Read/status tools may return these source values in addition to older values:

- `hook_history`
- `app_server+hook_history`
- `transcript+hook_history`
- `mixed`

Legacy `_kb_history` remains a fallback, but public installations should use:

```powershell
codex-control-plane-mcp-hooks install --state-db <PATH>
```

The hook installer stores `stateDb` as an absolute path even when `<PATH>` is
relative.

For write operations launched through `codex-app-server`, MCP mirrors the
accepted prompt, visible assistant messages, and turn status into the same hook
history tables. External Codex hooks remain the independent journal for normal
Codex user turns. The app-server mirror covers orchestrator-managed turns when
app-server does not run user hook commands.

## Stable error codes

Common stable error codes include:

- `INVALID_ARGUMENT`
- `CODEX_DUPLICATE_PROMPT_ACTIVE`
- `CODEX_BUSY`
- `CODEX_TIMEOUT`
- `CODEX_APP_SERVER_UNAVAILABLE`
- `CODEX_PENDING_INTERACTION_NOT_FOUND`
- `CODEX_PENDING_INTERACTION_UNAVAILABLE`
- `CODEX_THREAD_NOT_FOUND`
- `CODEX_TURN_NOT_FOUND`
- `CODEX_PROJECT_NOT_FOUND`
- `CODEX_TRANSCRIPT_NOT_FOUND`
- `CODEX_SEND_FAILED`
- `CODEX_SUMMARY_FAILED`

Clients should branch on `error.code` and treat `error.retryable` as the retry
hint. Human-readable `message` text is not a stable parsing target.

## Operational rules

- Do not mutate Codex internal SQLite or transcript files through MCP.
- Use app-server for write/control operations.
- For strict retry idempotency, pass `client_request_id`.
- Poll durable operations/workflows instead of holding long `tools/call` requests open.
- Do not run risky repairs without explicit `dry_run=false`; forced paths also require `force=true`.
- Prefer `refresh_catalog_and_history`; `refresh_catalog_and_kb` remains a compatibility alias.
