# Thin wrappers vs. a durable control plane

A thin Codex wrapper is often the right tool. Use one when the caller can keep a
single request open, a human is watching the run, and losing local process state
is acceptable.

Use Codex Control Plane MCP when Codex needs to behave like a durable local
worker: submit work, return an ID immediately, poll status later, and recover
safely from client retries or MCP restarts.

## When a thin wrapper is enough

A small wrapper around Codex Desktop or `codex-app-server` is usually enough for:

- short read-only prompts that finish inside the MCP client's timeout window;
- one-off interactive sessions where a human can notice failures and retry;
- experiments where duplicate turns or lost status are not expensive;
- scripts that already own their own queue, retry, and persistence layer;
- local demos that do not need Plan Mode approval or pending-question handling.

In these cases the wrapper can stay simple: call Codex, stream or wait for the
answer, and let the caller decide what to do if the process exits.

## When to use Codex Control Plane MCP

A durable control plane is useful when the caller needs operational guarantees
instead of a best-effort command wrapper:

| Need | Why a thin wrapper is fragile | What the control plane adds |
| --- | --- | --- |
| Long-running work | The MCP request can time out while Codex is still running. | `codex_submit_task` returns an `operationId` immediately and status is pollable. |
| Safe retries | Retrying after a network or client timeout can create duplicate Codex turns. | `client_request_id` and active prompt detection make retries idempotent. |
| Plan Mode | The plan, approval, execution, and final report are hard to coordinate as one flow. | Workflow tools expose `workflowId`, plan review, approval, execution, and final report state. |
| Approvals and questions | Human prompts can block an otherwise automated caller. | Pending interactions are listed and answered through explicit MCP tools. |
| Restart recovery | In-memory wrapper state disappears when the MCP server restarts. | Operations, workflows, turns, hooks, and diagnostics are persisted in SQLite. |
| Diagnostics | Logs alone are hard for automation to interpret. | Health, diagnostics, issue analysis, and dry-run repair tools return structured results. |

## Decision checklist

Choose a thin wrapper if all of these are true:

1. The task is short enough for one MCP call.
2. Duplicate submissions are harmless.
3. A human is present to approve, answer, or retry.
4. You do not need a durable local history of operations or turns.

Choose Codex Control Plane MCP if any of these are true:

1. The task may run for minutes or hours.
2. The client may retry after a timeout.
3. You need Plan Mode to be machine-drivable.
4. You need to surface approvals or questions without blocking the original call.
5. You need restart recovery, diagnostics, or a pollable final report.

## Migration path

You do not need to migrate everything at once. A common path is:

1. Keep the thin wrapper for short read-only prompts.
2. Route write tasks through `codex_submit_task` with a stable `client_request_id`.
3. Add `codex_get_operation_status` polling and final-report handling.
4. Move plan-heavy workflows to `codex_start_plan_workflow` and
   `codex_approve_plan`.
5. Add startup health checks with `codex_health_summary`.

That keeps simple calls simple while giving long-running or risky Codex work a
stable lifecycle.
