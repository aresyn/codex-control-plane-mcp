# OpenClaw client guide for Codex Control Plane MCP

[Русский](OPENCLAW_CLIENT_GUIDE_RU.md) | English

This guide explains how OpenClaw should use `codex-control-plane-mcp` as the
main control plane for Codex Desktop.

The contract is simple: OpenClaw submits work, receives an `operationId` or
`workflowId` quickly, then polls status until the work reaches a terminal state.
Long Codex work must be durable, pollable, and retry-safe. OpenClaw should not
keep one MCP call open for a long-running Codex task.

## Mental model

`codex-control-plane-mcp` handles the parts that OpenClaw should not implement
itself:

- starting and checking `codex-app-server`;
- creating Codex threads and turns;
- durable operation queue;
- retry idempotency through `client_request_id`;
- duplicate prompt and duplicate turn protection;
- Plan Mode workflow state;
- approvals, questions, and interrupts;
- app-server progress journal;
- hook-backed SQLite history;
- diagnostics and repair actions;
- runtime inventory for models, sandbox readiness, hooks, skills, account
  status, and rate limits.

OpenClaw must not mutate Codex internal SQLite databases, transcript files, or
session files directly. All write/control work goes through MCP tools, and MCP
talks to `codex-app-server`.

## Startup handshake

On startup, reconnect, and after MCP restart:

1. Call `codex_health_summary`.
2. Verify:
   - `ok == true`;
   - `version.serverName == "codex-control-plane-mcp"`;
   - `version.contractVersion == "1"`;
   - `version.toolSurfaceHash` exists;
   - required stable tools are present in `version.stableTools`.
3. If OpenClaw may start new work, call `codex_get_runtime_capabilities`.
4. If `hookHistory.status` is not `ok`, continue with a warning. Hook history is
   a read fallback, not the write path.
5. If `runtimeCapabilities.status == "partial"`, inspect `methodResults` and
   decide based on the failed method.

Minimal startup call:

```json
{
  "tool": "codex_health_summary",
  "arguments": {}
}
```

If `codex_health_summary` is missing or returns a JSON-RPC protocol error,
OpenClaw should treat the MCP server as incompatible and must not call write
tools.

## Worker architecture for OpenClaw

For normal OpenClaw operation, MCP clients should run in `client` mode and a
single background worker should run in `worker` mode.

Client mode:

```powershell
CODEX_MCP_EXECUTION_MODE=client
codex-control-plane-mcp
```

Worker mode:

```powershell
CODEX_MCP_EXECUTION_MODE=worker
codex-control-plane-mcp-worker
```

In this architecture OpenClaw agents only submit tasks, answer interactions, and
read status. The worker owns `codex-app-server`, queue slots, leases, and
resource locks.

OpenClaw should pass these fields on long-running writes:

- `agent_id`: stable agent id, for example `codex-dev` or `book-codex-agent`;
- `resource_keys`: affected write scopes when the task may edit files;
- `priority`: usually `normal`;
- `estimated_cost_class`: `light`, `normal`, or `heavy`.

When `codex_get_operation_status` returns `queueState.queuedReason`, do not mint
a new operation. Follow `nextRecommendedAction`:

- `wait_for_worker_slot`: keep polling the same operation;
- `wait_for_resource_lock`: keep polling the same operation, or wait for the
  conflicting scoped write to finish;
- `inspect_worker_health`: call `codex_get_worker_status` and
  `codex_get_concurrency_status`;
- `inspect_diagnostics`: collect diagnostics for the same operation or workflow.

For a running turn, prefer the worker fields over local app-server guesses:
`slotState.claimed`, `slotClaim`, `workerState`, `resourceLockState`, and
`queueState`. In client mode the gateway must not start its own app-server just
to answer status.

## Core rules

1. Use `codex_submit_task` for new long-running tasks.
2. Use `codex_start_plan_workflow`, `codex_get_workflow_status`, and
   `codex_approve_plan` for Plan Mode.
3. Use `codex_start_review_workflow` and `codex_get_workflow_status` for code
   review.
4. Use `codex_submit_task(operation_type="steer_turn")` to add instructions to
   an active turn without creating a second turn.
5. Use `codex_submit_task(operation_type="fork_thread")` for alternate branches
   of an existing thread.
6. Always pass `client_request_id` for retry-safe write requests.
7. Never wait for task completion inside the submit call. Submit, store the id,
   then poll.
8. Follow `nextRecommendedAction`, `recommendedPollAfterSeconds`, and
   `pollRecommended`.
9. Treat low-level `codex_start_chat`, `codex_send_message`, and
   `codex_execute_plan` as compatibility tools.
10. Stop polling when `pollRecommended == false`, unless a human asks for manual
    diagnostics.

## Result contract

Every tool returns MCP `structuredContent`. OpenClaw should read
`structuredContent`. `content[0].text` is a formatted JSON copy for humans.

Success:

```json
{
  "ok": true
}
```

Tool/domain error:

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

Rules:

- branch on `error.code`, not on `message`;
- use `error.retryable` as the retry hint;
- JSON-RPC errors are protocol errors;
- a domain error inside `tools/call` does not mean the MCP transport is broken.

## Identifiers and idempotency

OpenClaw should persist these identifiers:

- `operationId`: durable operation from `codex_submit_task`;
- `workflowId`: durable workflow from plan/review tools;
- `actionId`: lifecycle action, such as compaction;
- `threadId`: Codex thread;
- `turnId`: Codex turn;
- `client_request_id`: strict retry idempotency key.

Recommended `client_request_id` format:

```text
openclaw:<domain-task-id>:<phase>:<stable-hash-or-version>
```

Examples:

```text
openclaw:issue-123:analysis:v1
openclaw:issue-123:plan-workflow:v1
openclaw:issue-123:approve-plan:v1
openclaw:repo-456:review-uncommitted:v1
```

Do not use a fresh random UUID when retrying the same logical operation. A UUID
is fine only when OpenClaw intentionally creates a new independent operation.

Do not reuse one `client_request_id` for different messages, threads, workflows,
or phases.

## Choosing the right tool

| Situation | Tool | Persist |
|---|---|---|
| New normal task | `codex_submit_task` with `operation_type="start_chat"` | `operationId`, then `threadId`, `turnId` |
| Continue an existing thread | `codex_submit_task` with `operation_type="send_message"` | `operationId`, `threadId`, `turnId` |
| Execute an approved plan | `codex_approve_plan` or `codex_submit_task` with `operation_type="execute_plan"` | `workflowId`, `executionOperationId` |
| Add text to an active turn | `codex_submit_task` with `operation_type="steer_turn"` | `operationId`, target `threadId`, target `turnId` |
| Create an alternate branch | `codex_submit_task` with `operation_type="fork_thread"` | `operationId`, forked `threadId` |
| Plan Mode end to end | `codex_start_plan_workflow` | `workflowId` |
| Code review | `codex_start_review_workflow` | `workflowId`, `reviewThreadId`, `reviewTurnId` |
| Answer approval or question | `codex_list_pending_interactions`, then `codex_answer_pending_interaction` | `interactionId` |
| Stop a turn | `codex_interrupt_turn` | `threadId`/`turnId` or `operationId`/`workflowId` |
| Find old work | `codex_search_chats` | `threadId`, `chatId`, `projectId` |
| Read a chat | `codex_get_chat` | messages, source |
| Check runtime | `codex_get_runtime_capabilities` | cached runtime snapshot |
| Preflight a long run | `codex_preflight_project_run` | ready/degraded/failed checks |
| Diagnose a failure | `codex_collect_diagnostics` | issue summary, repair hints |
| Repair MCP state | `codex_repair_issue` | repair result |
| Archive a thread | `codex_archive_thread` | lifecycle action |
| Unarchive a thread | `codex_unarchive_thread` | lifecycle action |
| Compact a thread | `codex_start_thread_compaction`, then status | `actionId` |

## Polling an operation

After `codex_submit_task`:

```text
operation = codex_submit_task(...)
store operation.operationId

while true:
    status = codex_get_operation_status(operationId)
    handle pendingInteractions if present
    handle nextRecommendedAction

    if status.pollRecommended == false:
        break

    sleep(status.recommendedPollAfterSeconds or fallback)
```

Read these fields:

- `status`;
- `phase`;
- `operationType`;
- `threadId`;
- `turnId`;
- `turnStatus`;
- `latestMessages`;
- `progressEvents`;
- `pendingInteractions`;
- `finalReport`;
- `nextRecommendedAction`;
- `recommendedPollAfterSeconds`;
- `pollRecommended`.

Terminal operation statuses:

- `completed`;
- `failed`;
- `aborted`;
- `cancelled`;
- `canceled`;
- `interrupted`;
- `orphaned`;
- `unknown_after_app_server_exit`.

Only `completed` is successful. `unknown_after_app_server_exit` is not success.

## Polling a workflow

Plan workflows and review workflows are read through `codex_get_workflow_status`.

```text
workflow = codex_start_plan_workflow(...) or codex_start_review_workflow(...)
store workflow.workflowId

while true:
    status = codex_get_workflow_status(workflowId)
    handle pendingInteractions
    handle nextRecommendedAction

    if status.pollRecommended == false:
        break

    sleep(status.recommendedPollAfterSeconds or fallback)
```

Plan workflow actions:

- `wait_plan`: keep polling;
- `wait_for_worker_slot`: the current plan or execution operation is queued by
  worker capacity;
- `wait_for_resource_lock`: the current operation is waiting on a scoped write
  lock;
- `review_plan`: show the plan to a human or policy engine;
- `adopt_candidate_plan`: review `workflowObservation.candidatePlans` and call
  `codex_adopt_workflow_plan` if the candidate is valid;
- `execute_plan`: call `codex_approve_plan`;
- `answer_pending_interaction`: handle pending approval or question;
- `wait_execution`: keep polling execution;
- `read_final_report`: read `finalReport`;
- `inspect_diagnostics`: run diagnostics.

Review workflow actions:

- `wait_review`: keep polling;
- `read_review_report`: read `finalReport`;
- `answer_pending_interaction`: handle pending approval or question;
- `inspect_diagnostics`: run diagnostics.

### Workflow recovery cross-check

Do not mark an OpenClaw run as blocked from `workflow.status` alone. A Codex
thread can advance after the official workflow turn, especially when a human
opens Codex Desktop, grants access, or sends a follow-up message in the same
thread.

When a workflow returns `failed`, `plan_needs_review`, `plan_candidate_found`,
`orphaned`, or a suspicious `plan_ready`, do this before blocking the run:

1. Read `workflowObservation`.
2. If `recoverableCandidateFound == true`, inspect `candidatePlans`.
3. Call `codex_get_chat` for the workflow `threadId`.
4. Call `codex_collect_diagnostics` with `workflow_id`.
5. Verify the external side effect for the business task, such as a YouTrack
   comment id, a file change, or a report marker.

`workflowObservation` is a drift detector. Important fields:

- `officialPlanTurnId`: the turn currently attached to the workflow.
- `expectedExecutionTurnId`: the expected execution turn after approval.
- `officialPlanQuality`: MCP's quality classification for that plan.
- `threadAdvancedAfterOfficialTurn`: later untracked turns exist in the same
  thread. MCP should not set it for the normal expected execution turn.
- `recoverableCandidateFound`: a later valid plan/report candidate was found.
- `candidatePlans`: valid plan candidates from the same thread, with
  `turnId`, `planHash`, `quality`, and `markdown`.

Read Plan Mode output from `latestPlan`. Do not treat
`planOperation.finalReport` as the plan. If MCP returns
`planOperation.planArtifactSummary`, use it only as a compact pointer to the
plan artifact.

If `nextRecommendedAction == "adopt_candidate_plan"`, the correct recovery path
is:

```json
{
  "tool": "codex_adopt_workflow_plan",
  "arguments": {
    "workflow_id": "WORKFLOW_ID",
    "candidate_turn_id": "CANDIDATE_TURN_ID",
    "candidate_plan_hash": "CANDIDATE_PLAN_HASH",
    "client_request_id": "openclaw:issue-123:adopt-plan:v1"
  }
}
```

After adoption, poll `codex_get_workflow_status` again. Only call
`codex_approve_plan` when `latestPlan.planQuality == "valid_plan"` and the
plan matches the task.

## New task: `start_chat`

Use this when OpenClaw wants a new Codex thread for a project.

First list projects:

```json
{
  "tool": "codex_list_projects",
  "arguments": {}
}
```

Then submit work:

```json
{
  "tool": "codex_submit_task",
  "arguments": {
    "operation_type": "start_chat",
    "project_id": "PROJECT_ID",
    "message": "Analyze the issue and write a concise report.",
    "client_request_id": "openclaw:issue-123:analysis:v1",
    "sandbox": "read-only",
    "approval_policy": "on-request"
  }
}
```

Expected quick ack:

```json
{
  "ok": true,
  "operationId": "...",
  "status": "queued",
  "pollRecommended": true
}
```

After that, poll only with `codex_get_operation_status`.

## Continue a thread: `send_message`

Use this when OpenClaw has an existing `threadId` or `chat_id` and needs a new
turn.

```json
{
  "tool": "codex_submit_task",
  "arguments": {
    "operation_type": "send_message",
    "chat_id": "THREAD_OR_CHAT_ID",
    "message": "Add a short technical plan for developers.",
    "client_request_id": "openclaw:issue-123:developer-plan:v1"
  }
}
```

Do not use `send_message` while the target turn is still active. Use
`steer_turn` instead.

## Steer an active turn

`steer_turn` sends extra text into an active Codex turn without creating another
turn.

Use it when:

- Codex is already working;
- a new comment or instruction arrives;
- OpenClaw must add a format constraint or extra context;
- creating a second turn would be wrong.

```json
{
  "tool": "codex_submit_task",
  "arguments": {
    "operation_type": "steer_turn",
    "thread_id": "THREAD_ID",
    "expected_turn_id": "TURN_ID",
    "message": "Also include a developer-facing implementation plan.",
    "client_request_id": "openclaw:issue-123:steer-technical-plan:v1"
  }
}
```

After app-server accepts the steer request, the steering operation stays
`running` and follows the target turn until the target turn reaches a terminal
state.

Prompt duplicate detection does not apply to steering. Without
`client_request_id`, every call is treated as a new steering command.

Common errors:

- `CODEX_TURN_NOT_FOUND`: target turn is unknown;
- `INVALID_ARGUMENT`: target turn is terminal or does not match the thread;
- `CODEX_BUSY`: the requested action conflicts with current state.

## Fork a thread

`fork_thread` creates a new branch from an existing Codex thread.

Fork only:

```json
{
  "tool": "codex_submit_task",
  "arguments": {
    "operation_type": "fork_thread",
    "source_thread_id": "SOURCE_THREAD_ID",
    "client_request_id": "openclaw:issue-123:fork:v1"
  }
}
```

Fork and start work in the fork:

```json
{
  "tool": "codex_submit_task",
  "arguments": {
    "operation_type": "fork_thread",
    "source_thread_id": "SOURCE_THREAD_ID",
    "message": "Check an alternative solution path.",
    "client_request_id": "openclaw:issue-123:fork-alt-solution:v1",
    "sandbox": "read-only",
    "approval_policy": "on-request"
  }
}
```

Status rules:

- top-level `threadId` means the forked thread after fork creation;
- the source thread is in `forkState.sourceThreadId`;
- fork-only completes after app-server returns the forked thread id;
- fork with a message follows the first turn in the forked thread.

Prompt duplicate detection is disabled for `fork_thread` because similar fork
requests can be intentional. Use `client_request_id` for retry safety.

## Image inputs

Images are accepted through `input_items` only for operations that start a turn:

- `start_chat`;
- `send_message`;
- `execute_plan`;
- `fork_thread` with `message`.

Example:

```json
{
  "tool": "codex_submit_task",
  "arguments": {
    "operation_type": "start_chat",
    "project_id": "PROJECT_ID",
    "message": "Analyze the screenshot and explain the visible problem.",
    "input_items": [
      {
        "type": "localImage",
        "path": ".\\screenshots\\error.png",
        "detail": "high"
      },
      {
        "type": "image",
        "url": "https://example.com/screenshot.png",
        "detail": "low"
      }
    ],
    "client_request_id": "openclaw:issue-123:screenshot-analysis:v1"
  }
}
```

Rules:

- `localImage.path` must exist and be inside `CODEX_ALLOWED_ROOTS`;
- relative paths are resolved against effective `cwd`;
- allowed extensions are `.png`, `.jpg`, `.jpeg`, `.webp`, and `.gif`;
- `data:` and `file:` URLs are rejected;
- raw bytes are not stored;
- raw URLs and full local paths are not returned in public status or diagnostics;
- status returns only safe `inputItemState` metadata.

If `input_items` are passed to `steer_turn`, or to fork-only without `message`,
MCP returns `INVALID_ARGUMENT`.

## Structured final reports

Use `output_schema` when OpenClaw needs machine-readable output.

```json
{
  "tool": "codex_submit_task",
  "arguments": {
    "operation_type": "start_chat",
    "project_id": "PROJECT_ID",
    "message": "Analyze the problem and return structured result.",
    "client_request_id": "openclaw:issue-123:structured-analysis:v1",
    "output_schema": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "summary": {"type": "string"},
        "rootCause": {"type": "string"},
        "proposedFix": {"type": "string"},
        "risk": {"type": "string"}
      },
      "required": ["summary", "rootCause", "proposedFix", "risk"]
    }
  }
}
```

OpenClaw should read:

- `outputSchemaState`;
- `finalReport.text`;
- `finalReport.structured`;
- `finalReport.structuredStatus`;
- `finalReport.structuredParseStatus`;
- `finalReport.threadId`;
- `finalReport.turnId`;
- `finalReport.readFullVia`.

If `finalReport.structured == null`, do not automatically treat the task as
failed. Check `status`, `finalReport.text`, and `structuredParseStatus` first.

## Plan Mode workflow

Use Plan Mode when a plan should be reviewed before execution.

Start:

```json
{
  "tool": "codex_start_plan_workflow",
  "arguments": {
    "project_id": "PROJECT_ID",
    "message": "Prepare an implementation plan. Do not change files yet.",
    "client_request_id": "openclaw:issue-123:plan:v1",
    "sandbox": "workspace-write",
    "approval_policy": "on-request",
    "goal": "Prepare a safe implementation plan for issue 123",
    "goal_completion_action": "clear"
  }
}
```

Poll:

```json
{
  "tool": "codex_get_workflow_status",
  "arguments": {
    "workflow_id": "WORKFLOW_ID"
  }
}
```

Approve:

```json
{
  "tool": "codex_approve_plan",
  "arguments": {
    "workflow_id": "WORKFLOW_ID",
    "client_request_id": "openclaw:issue-123:approve-plan:v1",
    "message": "Implement the approved plan.",
    "sandbox": "workspace-write",
    "approval_policy": "on-request"
  }
}
```

Plan Mode has a sandbox floor. Do not intentionally request `read-only` for
Plan Mode. If a caller does, MCP raises the effective sandbox to
`workspace-write` and returns `runtimePolicyAdjusted=true`,
`requestedSandbox`, and `effectiveSandbox` in workflow status.

Repeated approve for the same workflow must not create a second execution turn.
OpenClaw should store `executionOperationId` and keep polling the workflow.

If `nextRecommendedAction == "review_plan"`, show the plan to a human or policy
engine. Do not approve an empty or incomplete plan.

## Workflow thread goals

If OpenClaw owns a high-level goal, pass it explicitly:

```json
{
  "goal": "Investigate the issue and prepare an analyst-facing report",
  "goal_token_budget": 50000,
  "goal_completion_action": "clear"
}
```

Read `threadGoal` from `codex_get_workflow_status`. Normal workflow polling is
passive and does not call live app-server goal methods. If you need to sync or
verify the goal, call `codex_get_workflow_status` with `refresh_live_goal=true`.

- `syncState == "active"`: the goal was set;
- `pending_thread`: the workflow thread is not known yet;
- `cleared`: MCP cleared its managed goal after completion;
- `complete`: MCP marked its managed goal complete;
- `left`: MCP left the goal unchanged;
- `external_override`: someone changed the goal in Codex, so MCP skipped cleanup;
- `unsupported`: app-server does not support goal methods;
- `error`: goal sync failed, but the workflow may continue.

MCP does not infer goals from prompts. If a goal matters, pass it explicitly.

## Code review workflow

Use `codex_start_review_workflow` when OpenClaw wants Codex to review a local
checkout.

Review uncommitted changes:

```json
{
  "tool": "codex_start_review_workflow",
  "arguments": {
    "cwd": "PROJECT_ROOT",
    "target_type": "uncommitted_changes",
    "client_request_id": "openclaw:repo-123:review-uncommitted:v1",
    "sandbox": "read-only",
    "approval_policy": "on-request"
  }
}
```

Review against a branch:

```json
{
  "tool": "codex_start_review_workflow",
  "arguments": {
    "cwd": "PROJECT_ROOT",
    "target_type": "base_branch",
    "base_branch": "main",
    "instructions": "Focus on correctness and missing tests.",
    "client_request_id": "openclaw:repo-123:review-main:v1"
  }
}
```

Supported `target_type` values:

- `uncommitted_changes`;
- `base_branch`;
- `commit`;
- `custom`.

For PR URLs, do not pass raw diffs in v1. Use a local checkout with
`base_branch`, or use `custom` instructions.

Poll with `codex_get_workflow_status`. Read the result from `finalReport`.

## Pending interactions

If operation/workflow status contains `pendingInteractions`, or
`nextRecommendedAction == "answer_pending_interaction"`:

1. Call `codex_list_pending_interactions`.
2. Match by `operationId`, `workflowId`, `threadId`, or `turnId`.
3. Show the question to a human or apply OpenClaw policy.
4. Answer with `codex_answer_pending_interaction`.

Example:

```json
{
  "tool": "codex_answer_pending_interaction",
  "arguments": {
    "interaction_id": "INTERACTION_ID",
    "decision": "approve",
    "message": "Approved by OpenClaw policy."
  }
}
```

If the interaction belongs to an old app-server generation, MCP may return
`CODEX_PENDING_INTERACTION_UNAVAILABLE`. Move to diagnostics.

## Interrupt

Use `codex_interrupt_turn` to stop an active turn.

Accepted targets:

- `thread_id` and `turn_id`;
- `operation_id`;
- `workflow_id`.

Example:

```json
{
  "tool": "codex_interrupt_turn",
  "arguments": {
    "operation_id": "OPERATION_ID",
    "reason": "User cancelled the task."
  }
}
```

After interrupt, keep polling. Expected terminal states are `interrupted`,
`cancelled`, `canceled`, or `unknown_after_app_server_exit`.

## Thread lifecycle

Archive:

```json
{
  "tool": "codex_archive_thread",
  "arguments": {
    "thread_id": "THREAD_ID",
    "refresh_catalog": true
  }
}
```

Unarchive:

```json
{
  "tool": "codex_unarchive_thread",
  "arguments": {
    "thread_id": "THREAD_ID",
    "refresh_catalog": true
  }
}
```

Start compaction:

```json
{
  "tool": "codex_start_thread_compaction",
  "arguments": {
    "thread_id": "THREAD_ID"
  }
}
```

Poll compaction:

```json
{
  "tool": "codex_get_thread_compaction_status",
  "arguments": {
    "action_id": "ACTION_ID",
    "include_events": false
  }
}
```

Lifecycle tools refuse unknown threads and busy threads. Expect
`CODEX_THREAD_NOT_FOUND` or `CODEX_BUSY`.

## Read and search tools

Use read tools for UI, lookup, and recovery.

### `codex_list_projects`

Returns projects from catalog, registry, hook history, and fallback sources.

Use it before `start_chat`, to map a local path to `projectId`, and to check path
casing.

Use compact mode for normal polling and UI lists:

```json
{
  "tool": "codex_list_projects",
  "arguments": {
    "compact": true,
    "limit": 50,
    "refresh": false,
    "include_private_details": false
  }
}
```

Only set `refresh=true` for an explicit catalog refresh. Check `cacheState` and
`truncated` before assuming the list is complete.

### `codex_list_project_chats`

Lists chats for one project. Use it for project history, last thread lookup, and
archived filters.

### `codex_list_active_chats`

Lists active/running chats. Use it before app-server restart and before actions
that could conflict with active work.

### `codex_search_chats`

Searches prompts, replies, hook history, transcripts, and summaries. Use it to
find a thread by issue id, marker, or old report.

If a refresh is requested with a small time budget, MCP may return partial
results with `timeBudgetExhausted=true`. In that case, retry without refresh or
increase the budget deliberately.

### `codex_get_chat_status`

Returns compact chat/thread status. Use it before `send_message`, lifecycle
actions, and recovery.

Possible source values include `app_server`, `hook_history`, `transcript`,
`tracked_turn`, `tracked_turn+hook_history`, `app_server+hook_history`,
`transcript+hook_history`, and `mixed`. Fresh threads should normally come from
`tracked_turn` or hook history before legacy KB fallback.

### `codex_get_chat`

Reads full chat/thread history. Use it after `finalReport.readFullVia`, for UI,
or to verify fallback history.

If the catalog has not seen a fresh thread yet, MCP can still build a summary
from tracked turns and hook history. Treat `CODEX_THREAD_NOT_FOUND` as final
only after search and diagnostics also fail.

### `codex_get_turn_status`

Reads one turn, including progress journal. Use it when OpenClaw has
`threadId` and `turnId` but not an `operationId`.

Inputs:

- `progress_events`: default `10`, max `100`, `0` disables progress block;
- `progress_max_chars`: default `2000`.

## Runtime capabilities

Call this before complex work, after reconnect, or during diagnostics:

```json
{
  "tool": "codex_get_runtime_capabilities",
  "arguments": {
    "refresh": false,
    "cwd": "PROJECT_ROOT",
    "timeout_seconds": 2,
    "include_models": true,
    "include_hooks": true,
    "include_skills": true,
    "include_account": true
  }
}
```

Decision rules:

- no suitable model: do not start the task;
- sandbox not ready: choose a safe mode or ask the user to fix the environment;
- hooks not installed: work may continue, but read fallback is weaker;
- account unauthenticated: do not start write work if Codex requires auth;
- rate limit reached: postpone work and use backoff;
- `status == "partial"`: inspect `methodResults`.

In `client` execution mode, this tool is passive by default. It may return the
last worker snapshot with `cacheSource="worker_registry"` and
`workerRuntimeSnapshot`. With `refresh=true`, MCP should ask the worker to
refresh inventory and may return `refreshCommandId`; poll worker command status
or call capabilities again after the worker updates the snapshot.

Account fields are redacted. Do not try to extract email or account identifiers
from diagnostics.

## Project preflight

Before a multi-hour run, call `codex_preflight_project_run`. It checks the
project path, allowed roots, Codex home, auth state, hooks, runtime inventory,
and optional live turn startup.

```json
{
  "tool": "codex_preflight_project_run",
  "arguments": {
    "project_id": "PROJECT_ID",
    "cwd": "PROJECT_ROOT",
    "model": "gpt-5.4",
    "sandbox": "read-only",
    "approval_policy": "on-request",
    "workflow_kind": "plan_then_execute",
    "live_probe": false,
    "timeout_seconds": 2
  }
}
```

Decision rules:

- `status == "ready"`: the run can start.
- `status == "degraded"`: start only if the degraded check is acceptable for
  this task.
- `status == "failed"`: do not start. Read `checks` and fix the environment or
  change the runtime policy.

Use `live_probe=true` only when the client is allowed to create a small safe
Codex turn. The probe uses a `MCP PREFLIGHT / DO NOT MODIFY FILES` marker and
returns a normal durable operation id.

## Health summary

`codex_health_summary` is lightweight. Call it for:

- startup handshake;
- reconnect;
- dashboard status;
- stale operation checks;
- pending interaction checks;
- hook history status;
- app-server restart decisions.

If `pollRecommended == true`, there is active work, pending interaction, or
stale operation state. Continue health polling or run diagnostics.

Read current readiness before reacting to historical records. Old orphaned or
stale rows are reported under `historicalDebt`; they do not make the current
runtime broken by themselves. Use `historicalDebt.nextRecommendedAction`, usually
`run_targeted_cleanup`, outside the hot path.

Read `stallSupervisor` before deciding to retry a long-running turn:

- `mode == "diagnose_only"`: do not interrupt automatically;
- `stalledTurnCount > 0`: collect diagnostics and run repair with `dry_run=true`;
- `automaticInterruptEnabled == true`: only then may an automated local policy
  interrupt stale turns.

## Diagnostics

For unclear failures, start with:

```json
{
  "tool": "codex_collect_diagnostics",
  "arguments": {
    "operation_id": "OPERATION_ID",
    "include_events": true,
    "include_payload": false
  }
}
```

You can diagnose by:

- `operation_id`;
- `workflow_id`;
- `thread_id`;
- `turn_id`;
- `project_id`;
- `chat_id`.

Read:

- `summary`;
- `scopedFindings`;
- `backgroundFindings`;
- `timeline`;
- `progressJournal`;
- `hookHistory`;
- `issues`;
- `recommendedActions`;
- `repairActions`.

Scoped findings are the decision input. Background findings are historical or
nearby context and should not override a fresh matching operation, workflow,
thread, or turn. `codex_analyze_issue` follows the same rule and should return
compact evidence refs instead of raw payloads. If `evidenceTruncated=true`, call
targeted diagnostics rather than broad raw logs.

### Agent guidance

When a status, diagnostic, preflight, or structured error response contains
`agentGuidance`, treat it as the next automation contract.

Read these fields first:

- `problemState`;
- `summary`;
- `instructions`;
- `loopGuard`;
- `evidenceRefs`;
- `agentGuidanceText`.

Rules for OpenClaw:

1. Follow `agentGuidance.instructions` before marking a run blocked.
2. If an instruction says `dryRunFirst=true`, call the suggested repair with
   `dry_run=true` before making changes.
3. If `loopGuard.allowed=false`, stop automatic recovery for that scope. Collect
   diagnostics and ask a human.
4. After `CODEX_TIMEOUT`, poll the existing operation or retry with the same
   `client_request_id`. Do not mint a new id.
5. Pending approvals or user input must be answered or expired. Do not restart
   the turn to get around them.
6. Auth and rate-limit guidance means wait or ask a human. Retrying turns will
   waste time.

`agentGuidanceText` is for logs and operator messages. Use
`agentGuidance.instructions` for decisions.

Do not request raw payloads by default. If `include_payload=true` is needed, do
not show the result to a user without redaction.

## Analyze issue

`codex_analyze_issue` classifies a known MCP/app-server state issue and returns
repair guidance.

```json
{
  "tool": "codex_analyze_issue",
  "arguments": {
    "operation_id": "OPERATION_ID"
  }
}
```

This is diagnostics for the MCP/app-server layer. It is not a substitute for
domain analysis inside Codex.

## Repair

Use `codex_repair_issue` carefully.

OpenClaw rule:

1. Run with `dry_run=true`.
2. Show planned changes to a human or policy engine.
3. Run with `dry_run=false` only after approval.
4. Use `force=true` only when the repair action explicitly requires it and the
   operator approved the risk.

Example:

```json
{
  "tool": "codex_repair_issue",
  "arguments": {
    "action": "refresh_catalog_and_history",
    "dry_run": true
  }
}
```

`refresh_catalog_and_kb` is a legacy alias. Prefer
`refresh_catalog_and_history`.

For a failed Plan Mode workflow caused by a bad sandbox or approval policy, use
`retry_workflow_with_runtime_policy`. It creates a new workflow and links it to
the old one through `workflowRetryState`.

```json
{
  "tool": "codex_repair_issue",
  "arguments": {
    "action": "retry_workflow_with_runtime_policy",
    "workflow_id": "OLD_WORKFLOW_ID",
    "sandbox": "workspace-write",
    "approval_policy": "on-request",
    "client_request_id": "openclaw:issue-123:retry-plan:v1",
    "reason": "Retry with a writable Plan Mode sandbox.",
    "dry_run": true
  }
}
```

After a successful dry run, repeat with `dry_run=false`, save `newWorkflowId`,
and poll the new workflow. Do not try to revive the old terminal turn.

## App-server status and restart

Check app-server:

```json
{
  "tool": "codex_get_app_server_status",
  "arguments": {}
}
```

Restart when idle:

```json
{
  "tool": "codex_restart_app_server",
  "arguments": {
    "mode": "restart_app_server_idle"
  }
}
```

Rules:

- call `codex_health_summary` before restart;
- do not restart while there are active turns or pending interactions;
- before forced restart, collect diagnostics;
- after restart, call `codex_health_summary`;
- operations that become `unknown_after_app_server_exit` are not successful.

## Progress journal

Use progress journal for UI and early diagnostics.

Read these fields from `codex_get_operation_status` and
`codex_get_turn_status`:

- `progressEvents`;
- `progressEventCount`;
- `latestProgressAt`;
- `tokenUsage`;
- `modelReroutes`;
- `warnings`.

Useful interpretation:

- no final assistant message but fresh `progressEvents`: the task is still alive;
- growing `warnings`: show them to the operator;
- `modelReroutes`: update UI, but do not treat it as failure;
- `tokenUsage`: use only coarse bands in public status, not exact token counts;
- stale `latestProgressAt` while the turn is active: collect diagnostics.

MCP does not store hidden chain-of-thought, raw command output, raw tool
payloads, or full diffs.

## Hook history

Hook history is a second local read source. OpenClaw should access it only
through normal read/status/search tools, not by reading tables directly.

Fields worth checking:

- `hookHistory.enabled`;
- `hookHistory.installed`;
- `hookHistory.dbWritable`;
- `hookHistory.lastEventAt`;
- `hookHistory.threadCount`;
- `hookHistory.turnCount`;
- `hookHistory.warnings`.

If hook history is disabled or hooks are not installed:

- write operations still work;
- search/status fallback is weaker;
- suggest `codex-control-plane-mcp-hooks install`;
- do not block critical work solely because hooks are missing.

## Error handling

| Error code | Meaning | OpenClaw action |
|---|---|---|
| `INVALID_ARGUMENT` | Bad arguments | Fix payload, do not retry unchanged |
| `CODEX_PROJECT_NOT_FOUND` | Unknown project | Call `codex_list_projects`, check allowed roots |
| `CODEX_THREAD_NOT_FOUND` | Unknown thread | Search with `codex_search_chats`, verify id |
| `CODEX_TURN_NOT_FOUND` | Unknown turn | Refresh chat status, do not use stale turn id |
| `CODEX_DUPLICATE_PROMPT_ACTIVE` | Similar active task exists | Continue existing operation/thread from details |
| `CODEX_BUSY` | Thread or app-server is busy | Poll active turn or pending interaction |
| `CODEX_TIMEOUT` | App-server/tool timeout | Retry with same `client_request_id` if retryable |
| `CODEX_APP_SERVER_UNAVAILABLE` | app-server unavailable | Health, diagnostics, restart only if idle |
| `CODEX_PENDING_INTERACTION_NOT_FOUND` | Interaction missing | Refresh pending list |
| `CODEX_PENDING_INTERACTION_UNAVAILABLE` | Interaction cannot be delivered | Diagnostics, generation may be stale |
| `CODEX_TRANSCRIPT_NOT_FOUND` | No transcript | Try hook history, then diagnostics |
| `CODEX_SEND_FAILED` | app-server rejected write | Do not assume task started |
| `CODEX_SUMMARY_FAILED` | Summary failed | Read raw chat, do not block completion |

If `error.retryable == true`, retry is allowed. For write retries, use the same
`client_request_id`.

## Duplicate prompt handling

MCP protects `start_chat`, `send_message`, and `execute_plan` from active
duplicate prompts.

If OpenClaw receives `CODEX_DUPLICATE_PROMPT_ACTIVE`:

1. Do not create another turn.
2. Read `operationId`, `threadId`, or `turnId` from `error.details` if present.
3. Poll the existing operation or turn.
4. Tell the user that the task is already running.

If a duplicate points to completed work, MCP may return an existing thread for
continuation. This is not an error when `ok == true`.

`steer_turn` and `fork_thread` do not use prompt duplicate detection. For those
paths, `client_request_id` is the retry-safety key.

## Sandbox and approval policy

Public safe defaults:

- `CODEX_MCP_DEFAULT_SANDBOX=read-only`;
- `CODEX_MCP_DEFAULT_APPROVAL_POLICY=on-request`.

OpenClaw can override them per call:

```json
{
  "sandbox": "read-only",
  "approval_policy": "on-request"
}
```

Recommendations:

- read-only analysis: `read-only` and `on-request`;
- untrusted repository: `read-only`;
- Plan Mode planning: at least `workspace-write`;
- tasks that may edit files: require explicit user or policy approval;
- do not change server defaults for one task. Pass per-call overrides.

## OpenClaw state

For each task, store:

```json
{
  "openclawTaskId": "...",
  "mcpOperationId": "...",
  "mcpWorkflowId": "...",
  "threadId": "...",
  "turnId": "...",
  "clientRequestId": "...",
  "lastStatus": "...",
  "nextRecommendedAction": "...",
  "lastPollAt": "...",
  "finalReport": {}
}
```

Update stored ids after each poll. `threadId` and `turnId` may appear after the
initial ack.

After OpenClaw restarts:

1. Restore saved `operationId` and `workflowId`.
2. Call `codex_health_summary`.
3. Poll unfinished operations and workflows.
4. If ids were lost, use `codex_search_chats` by external task id, marker, or
   prompt text.

## Prompt markers

For OpenClaw tasks, include a short stable marker in the prompt:

```text
OpenClaw task: <id>
Mode: analysis
Safety: read-only unless explicitly approved
```

For live smoke:

```text
MCP LIVE TEST / DO NOT MODIFY FILES / <timestamp>
```

Markers help search, diagnostics, and humans in Codex Desktop.

## Practical scenarios

### Analyze an issue without editing files

1. Call `codex_list_projects`.
2. Pick `project_id`.
3. Call `codex_submit_task(operation_type="start_chat", sandbox="read-only")`.
4. Poll `codex_get_operation_status`.
5. If action is `answer_pending_interaction`, answer it.
6. If status is `completed`, read `finalReport` or `latestMessages`.
7. If status is failed or unknown, collect diagnostics.

### Ask active Codex to add a technical plan

1. Read `threadId` and `turnId` from operation status.
2. Confirm the turn is active.
3. Call `codex_submit_task(operation_type="steer_turn")`.
4. Poll steering operation or the original operation.
5. Check that the final answer includes the steering instruction.

### Plan Mode with approval

1. Call `codex_start_plan_workflow`.
2. Poll `codex_get_workflow_status`.
3. If action is `adopt_candidate_plan`, review the candidate and call
   `codex_adopt_workflow_plan`.
4. When action is `review_plan`, show the plan to a human or policy engine.
5. Call `codex_approve_plan` with a new `client_request_id`.
6. Poll workflow until `read_final_report`.
7. Store `finalReport`.

### Code review

1. Call `codex_get_runtime_capabilities(cwd=...)`.
2. If sandbox/account state is acceptable, call `codex_start_review_workflow`.
3. Poll `codex_get_workflow_status`.
4. Answer pending interactions if needed.
5. On `read_review_report`, store `finalReport`.

### Client timeout recovery

1. Repeat the same write call with the same `client_request_id`.
2. If MCP returns an existing `operationId`, poll it.
3. If MCP returns active duplicate details, poll the referenced operation/turn.
4. If no operation is known, search chats by marker.

### app-server exited during a turn

1. Call `codex_get_operation_status`.
2. If status is `unknown_after_app_server_exit`, do not treat it as success.
3. Call `codex_collect_diagnostics`.
4. Check `hookHistory` and `codex_get_chat`.
5. If hook history shows a completed result, OpenClaw may use that read result,
   but should mark it as recovered by history.
6. If no result exists, offer to repeat the task with a new `client_request_id`.

### Thread is busy

If MCP returns `CODEX_BUSY`:

1. Call `codex_get_chat_status`.
2. Call `codex_get_turn_status` for the active turn.
3. If OpenClaw needs to add context, use `steer_turn`.
4. If it needs to wait, poll until terminal.
5. Do not call `send_message`, archive, unarchive, or compaction while busy.

## Public method reference

### Stable orchestration tools

#### `codex_submit_task`

Main write path for durable operations.

Operation types:

- `start_chat`;
- `send_message`;
- `execute_plan`;
- `steer_turn`;
- `fork_thread`.

Prefer this tool over low-level write tools.

#### `codex_get_operation_status`

Main polling endpoint for operations.

#### `codex_start_plan_workflow`

Creates a durable Plan Mode workflow and returns `workflowId`.

#### `codex_start_review_workflow`

Creates a durable code review workflow through app-server `review/start`.

#### `codex_get_workflow_status`

Main polling endpoint for plan and review workflows.

#### `codex_adopt_workflow_plan`

Adopts a later valid plan candidate from the same workflow thread. Use it only
after checking `workflowObservation.candidatePlans`.

#### `codex_approve_plan`

Starts execution after a completed plan. Repeated approve must not create a
second execution turn.

#### `codex_list_pending_interactions`

Lists active approvals/questions.

#### `codex_answer_pending_interaction`

Answers an approval/question.

#### `codex_interrupt_turn`

Interrupts an active turn by operation, workflow, or thread/turn ids.

#### `codex_archive_thread`

Archives a known thread.

#### `codex_unarchive_thread`

Unarchives a known thread.

#### `codex_start_thread_compaction`

Starts asynchronous thread compaction and returns `actionId`.

#### `codex_get_thread_compaction_status`

Poll endpoint for compaction.

#### `codex_get_runtime_capabilities`

Runtime inventory for models, permission profiles, sandbox readiness, hooks,
skills, provider capabilities, account status, usage bands, and rate limits.

#### `codex_preflight_project_run`

Checks whether a concrete project run is ready to start. Use it before
multi-hour autonomous work.

#### `codex_health_summary`

Lightweight health summary for startup, reconnect, dashboards, and restart
decisions.

#### `codex_collect_diagnostics`

Collects timeline, progress journal, hook history, issues, and repair hints.

#### `codex_repair_issue`

Runs safe repair actions. Start with `dry_run=true`.

### Read and compatibility tools

#### `codex_list_projects`

Project catalog.

#### `codex_list_project_chats`

Chats for one project.

#### `codex_list_active_chats`

Active chats and turns.

#### `codex_search_chats`

Search over chats, transcripts, hook history, and summaries.

#### `codex_get_chat_status`

Compact chat/thread status.

#### `codex_get_chat`

Full chat/thread history.

#### `codex_get_turn_status`

Turn status, including progress journal.

#### `codex_start_chat`

Compatibility layer. New clients should use
`codex_submit_task(operation_type="start_chat")`.

#### `codex_send_message`

Compatibility layer. New clients should use
`codex_submit_task(operation_type="send_message")`.

#### `codex_execute_plan`

Compatibility layer. With `workflow_id`, it delegates to the workflow path. New
clients should prefer `codex_approve_plan` or
`codex_submit_task(operation_type="execute_plan")`.

#### `codex_restart_app_server`

Restarts app-server. Use only after health/diagnostics and no active turns,
unless an operator explicitly accepts a forced restart.

#### `codex_get_app_server_status`

MCP-owned app-server status.

#### `codex_get_diagnostic_logs`

Raw audit surface. Do not request payloads by default.

#### `codex_analyze_issue`

Classifies MCP/app-server issues and suggests next actions.

## Pre-write checklist

Before `codex_submit_task`, `codex_start_plan_workflow`,
`codex_start_review_workflow`, or lifecycle actions:

- `client_request_id` is present when retry is possible;
- correct `project_id`, `cwd`, `thread_id`, or `workflow_id` is selected;
- target thread has no active turn, unless using `steer_turn`;
- sandbox is safe for the task;
- approval policy is correct;
- prompt contains an OpenClaw task marker;
- image inputs are inside allowed roots;
- `output_schema`, if present, is strict and small;
- repair actions start with `dry_run=true`.

## Completion checklist

After terminal status:

- if `status == "completed"`, store `threadId`, `turnId`, `finalReport`, or the
  final message;
- if status is `failed`, `interrupted`, `orphaned`, or
  `unknown_after_app_server_exit`, collect diagnostics;
- if `finalReport.readFullVia` is present, read the full chat with
  `codex_get_chat` when needed;
- for workflows, store `workflowKind`, `phase`, operation ids, and report hash;
- if pending interactions were involved, verify that they are terminal;
- if hooks report warnings, surface a warning without breaking a completed
  result.

## What OpenClaw should not do

- Do not hold a write MCP call open until Codex finishes.
- Do not create a second turn when `steer_turn` is the right tool.
- Do not retry a write request with a new `client_request_id` after transport
  timeout.
- Do not treat `unknown_after_app_server_exit` as success.
- Do not parse human-readable `message` as a machine contract.
- Do not read Codex internal SQLite directly.
- Do not show raw diagnostics payloads to users without redaction.
- Do not force app-server restart during active turns unless policy allows it.
- Do not use low-level write compatibility tools for new workflows.

## Minimal OpenClaw state machine

```text
new_task
  -> submit_or_start_workflow
  -> poll
  -> pending_interaction? answer
  -> needs_approval? approve
  -> terminal?
       completed -> store final report
       failed/interrupted/orphaned/unknown -> diagnostics
  -> done
```

For every polling step, follow `nextRecommendedAction`. If OpenClaw sees an
unknown non-terminal action, wait `recommendedPollAfterSeconds` and poll again.
If the action is unknown and the status is terminal, stop polling and collect
diagnostics.

## Integration smoke test

1. Call `codex_health_summary`.
2. Call `codex_get_runtime_capabilities(refresh=true, include_account=false)`.
3. Call `codex_list_projects`.
4. Start a safe prompt:

```text
MCP LIVE TEST / DO NOT MODIFY FILES / OpenClaw integration smoke
```

5. Poll `codex_get_operation_status` to terminal state.
6. Verify `threadId`, `turnId`, `finalReport`, or latest message.
7. Call `codex_collect_diagnostics` for the created turn.
8. Verify that no duplicate project was created due to path casing.

If these steps pass, OpenClaw can treat the MCP server as ready for normal use.
