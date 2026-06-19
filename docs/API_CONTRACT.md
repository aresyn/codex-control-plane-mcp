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
- `codex_start_review_workflow`
- `codex_get_workflow_status`
- `codex_adopt_workflow_plan`
- `codex_approve_plan`
- `codex_list_pending_interactions`
- `codex_answer_pending_interaction`
- `codex_interrupt_turn`
- `codex_archive_thread`
- `codex_unarchive_thread`
- `codex_start_thread_compaction`
- `codex_get_thread_compaction_status`
- `codex_get_worker_status`
- `codex_get_queue_status`
- `codex_get_concurrency_status`
- `codex_get_worker_command_status`
- `codex_get_runtime_capabilities`
- `codex_preflight_project_run`
- `codex_health_summary`
- `codex_collect_diagnostics`
- `codex_repair_issue`

Stable tools are asynchronous or pollable when they can trigger long work. New
fields may be added, but existing input fields and machine-readable status/error
fields must not be removed without changing `contractVersion`.

## Write policy defaults

Server-level default write policy is configured by environment/config:

- `CODEX_MCP_DEFAULT_SANDBOX`, default `read-only`
- `CODEX_MCP_DEFAULT_APPROVAL_POLICY`, default `on-request`
- JSON config fields `default_sandbox_policy` and `default_approval_policy`

Explicit tool arguments always win over server defaults. A client may submit one
task with a stricter or more permissive policy without changing the server
configuration.

Plan Mode has a sandbox floor. When `codex_start_plan_workflow` or
`codex_submit_task` with `collaboration_mode="plan"` resolves to `read-only`,
MCP sends `workspace-write` to app-server instead. Status output includes
`requestedSandbox`, `effectiveSandbox`, `runtimePolicyAdjusted`, and
`runtimePolicy` so clients can audit the adjustment. Non-plan write operations
keep normal sandbox semantics.

## Execution modes and central worker

`CODEX_MCP_EXECUTION_MODE` controls whether an MCP process may execute queued
operations:

- `inline`: default. The stdio server can submit, poll, and execute operations.
- `client`: submit/status/read surface only. It never picks up queued durable
  operations and delegates control actions to the worker command queue.
- `worker`: long-running scheduler. It owns app-server, leases, queue slots, and
  resource locks.
- `observe`: read-only heartbeat process for rollout checks. It never acquires
  leases.

In `client` mode, `codex_submit_task` creates a durable operation and returns a
fast ACK. `codex_get_operation_status` remains passive and does not call
`_schedule_recoverable_operations`.

The worker uses these limits:

- `CODEX_MCP_MAX_ACTIVE_TURNS_GLOBAL`, default `4`
- `CODEX_MCP_MAX_ACTIVE_TURNS_PER_PROJECT`, default `3`
- `CODEX_MCP_MAX_ACTIVE_TURNS_PER_AGENT`, default `3`
- `CODEX_MCP_MAX_ACTIVE_TURNS_PER_THREAD`, default `1`
- `CODEX_MCP_MAX_ACTIVE_WRITE_TURNS_PER_PROJECT`, default `1`
- `CODEX_MCP_MAX_APP_SERVER_PENDING_REQUESTS`, default `8`

`codex_submit_task` accepts scheduling hints:

- `agent_id`: stable orchestrator id such as `codex-dev`.
- `resource_keys`: write scopes for parallel work in one project.
- `priority`: `low`, `normal`, or `high`.
- `estimated_cost_class`: `light`, `normal`, or `heavy`.

If a write turn uses `workspace-write` or `danger-full-access` without
`resource_keys`, the worker takes a broad `project:<cwd>:write` lock. With
disjoint `resource_keys`, write turns in one project may run in parallel.

Operation status adds:

- `queueState`
- `workerState`
- `slotState`
- `resourceLockState`

Queued operations use `nextRecommendedAction="wait_for_worker_slot"` when a
limit or lock blocks scheduling. Worker health problems use
`nextRecommendedAction="inspect_worker_health"`.

## Durable operation types

`codex_submit_task` supports these operation types:

- `start_chat`: create a Codex thread and start a turn.
- `send_message`: resume an existing thread and start a new turn.
- `execute_plan`: execute an approved Plan Mode workflow or existing chat plan.
- `steer_turn`: send extra text to an active turn through app-server `turn/steer`.
- `fork_thread`: fork an existing thread through app-server `thread/fork`.

`codex_submit_task` also accepts optional `input_items` for operations that
start a new turn: `start_chat`, `send_message`, `execute_plan`, and
`fork_thread` when `message` is present. Supported v1 items are:

- `{"type": "image", "url": "https://...", "detail": "auto|low|high|original"}`
- `{"type": "localImage", "path": "...", "detail": "auto|low|high|original"}`

Remote image URLs must use `http` or `https`; `data:` and `file:` URLs are
rejected. Local image paths are resolved against the effective `cwd` when
relative, must point to an existing file under `CODEX_ALLOWED_ROOTS`, and must
use `.png`, `.jpg`, `.jpeg`, `.webp`, or `.gif`. Defaults allow 10 image items
and 20,000,000 bytes per local image. Deployments can override these with
`CODEX_MCP_MAX_IMAGE_INPUT_ITEMS` and `CODEX_MCP_MAX_IMAGE_INPUT_BYTES`.

Operation status includes `inputItemState` when image inputs were accepted.
The state contains counts, item types, detail, file extension, size, and hashes.
It does not include raw image bytes, raw URLs, or full local image paths. MCP
passes the raw URL/path only to `codex-app-server` for the live `turn/start`
request.

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

`fork_thread` requires `source_thread_id`. `message` is optional only for this
operation type. Without `message`, MCP completes the operation as soon as
app-server returns the forked thread id. With `message`, MCP starts the first
turn in the forked thread and the operation follows that turn to a terminal
state.

`fork_thread` does not participate in prompt duplicate detection because two
similar fork requests may be intentional. Pass `client_request_id` for strict
retry safety. Reusing the same `client_request_id` returns the same operation
and does not call `thread/fork` again. Calls without `client_request_id` create
new fork requests.

Inputs specific to `fork_thread`:

- `source_thread_id`: source thread to fork from.
- `message`: optional first user message for the forked thread.
- `cwd`: optional working directory override, inside `CODEX_ALLOWED_ROOTS`.
- `model`: optional model override.
- `approval_policy`: optional approval policy for the fork and first turn.
- `sandbox`: optional sandbox mode for the fork and first turn.
- `fork_config`: optional object passed to app-server as `config`.
- `ephemeral`: default `false`.

Status payloads for `fork_thread` include normal operation fields plus:

- `forkState.accepted`
- `forkState.sourceThreadId`
- `forkState.forkedThreadId`
- `forkState.hasInitialMessage`
- `forkState.cwd`
- `forkState.model`
- `forkState.ephemeral`
- `forkState.turnId`

After fork creation, top-level `threadId` means the forked thread id. For a
fork-only operation, `nextRecommendedAction` is `read_forked_thread`. For a
fork with an initial message, running status uses `poll_turn_status`.

## Thread lifecycle management

Lifecycle tools call app-server maintenance methods for known threads. They do
not use `codex_submit_task` and do not create durable Codex turns.

Tools:

- `codex_archive_thread(thread_id, project_id=null, timeout_seconds=30,
  refresh_catalog=true)`
- `codex_unarchive_thread(thread_id, project_id=null, timeout_seconds=30,
  refresh_catalog=true)`
- `codex_start_thread_compaction(thread_id, project_id=null,
  timeout_seconds=30)`
- `codex_get_thread_compaction_status(action_id, include_events=false)`

`thread_id` must be known through the catalog, tracked turns, or hook history.
Unknown threads return `CODEX_THREAD_NOT_FOUND`. If the thread has an active
turn or pending interaction, MCP returns `CODEX_BUSY` and does not call
app-server.

Archive and unarchive return completed lifecycle audit actions after
app-server ACK. A successful call refreshes the catalog by default so read and
search tools see the archived state sooner.

Compaction is pollable. `codex_start_thread_compaction` calls
`thread/compact/start`, stores a lightweight lifecycle action, and returns:

- `actionId`
- `actionType="compact"`
- `threadId`
- `status="running"`
- `threadState`
- `nextRecommendedAction="poll_thread_compaction"`
- `recommendedPollAfterSeconds`
- `pollRecommended=true`

`codex_get_thread_compaction_status` returns `running` until MCP observes a
matching app-server `thread/compacted` event for the same thread. Completed
responses include `observedEventId` and `targetTurnId`. If the MCP-owned
app-server exits before that event is observed, the action becomes
`unknown_after_app_server_exit` with `nextRecommendedAction="inspect_diagnostics"`.

Public `thread/delete` is intentionally not exposed in this contract. It is a
destructive action and needs a separate confirmation and threat model.

## Plan workflows and recovery

`codex_start_plan_workflow` starts a durable Plan Mode workflow. Poll with
`codex_get_workflow_status`. The normal path is:

1. Start planning with `codex_start_plan_workflow`.
2. Poll until the workflow asks for plan review.
3. Approve with `codex_approve_plan` only after the plan is valid for the task.
4. Poll the same workflow until the final report is ready.

Plan Mode never starts as `read-only`. If the caller passes `read-only`, or the
server default is `read-only`, MCP raises the effective sandbox to
`workspace-write`. More permissive values are passed through. The workflow ack
and later status include:

- `requestedSandbox`: sandbox requested by the caller or server default.
- `effectiveSandbox`: sandbox sent to app-server.
- `runtimePolicyAdjusted`: `true` when MCP raised `read-only` to
  `workspace-write`.
- `runtimePolicy`: compact policy block with approval policy and sandbox floor.

`latestPlan` includes quality fields:

- `planQuality`: `valid_plan`, `blocker`, `question`, `partial`,
  `needs_review`, or `unknown`.
- `quality`: same value for compact clients.
- `valid`: `true` only when MCP has a trusted usable plan artifact.

`codex_approve_plan` rejects blocker, question, partial, and unknown plan
artifacts. A fallback assistant message is not treated as a valid plan unless it
contains an explicit plan artifact such as `<proposed_plan>...</proposed_plan>`.

`codex_get_workflow_status` also returns `workflowObservation` for recovery:

- `officialPlanTurnId`: turn currently attached to the workflow.
- `officialPlanQuality`: quality classification for that official plan.
- `latestThreadTurnId`: latest turn known in the workflow thread.
- `threadAdvancedAfterOfficialTurn`: later turns exist in the same thread.
- `recoverableCandidateFound`: a later valid plan/report candidate was found.
- `candidatePlans`: candidate plans from the same thread with `turnId`,
  `planHash`, `quality`, `planQuality`, and `markdown`.
- `candidateReports`: future report candidates from the same observation pass.
- `importStatus`: transcript import status, when MCP refreshed tracking from a
  local transcript.

When `nextRecommendedAction == "adopt_candidate_plan"`, the client should show
the candidate to a human or policy engine, then call:

```json
{
  "tool": "codex_adopt_workflow_plan",
  "arguments": {
    "workflow_id": "WORKFLOW_ID",
    "candidate_turn_id": "CANDIDATE_TURN_ID",
    "candidate_plan_hash": "CANDIDATE_PLAN_HASH",
    "client_request_id": "CLIENT_RETRY_KEY"
  }
}
```

`codex_adopt_workflow_plan` updates the workflow's official plan turn and latest
plan hash without starting execution. It is idempotent for the same workflow and
candidate hash. After adoption, poll `codex_get_workflow_status` again and use
the normal approve path.

When a workflow has already failed or become orphaned because of a bad runtime
policy, use `codex_repair_issue` with
`action="retry_workflow_with_runtime_policy"`. The action defaults to
`dry_run=true`.

Dry run returns the planned request, runtime policy, and source workflow. With
`dry_run=false`, MCP creates a new workflow, links it to the old workflow, and
does not revive the old terminal turn. `codex_get_workflow_status` exposes the
link through `workflowRetryState`:

- `replacesWorkflowId`: source workflow replaced by this workflow.
- `replacedByWorkflowId`: replacement workflow for the current workflow.
- `retryOfWorkflowId`: original workflow id for the retry.
- `retryReason`: optional operator/client reason.
- `retryCreatedAt`: retry creation time.

Diagnostics may report:

- `workflow_thread_drift`: the thread has advanced after the official workflow
  turn.
- `workflow_recoverable_candidate_found`: a later valid candidate can be
  adopted.
- `invalid_plan_artifact`: the official plan is a blocker, question, partial
  artifact, or otherwise unsafe to approve automatically.

## Code review workflows

`codex_start_review_workflow` starts a durable pollable code review through
app-server `review/start`. It is a workflow tool, not a public
`codex_submit_task` operation type. Internally MCP stores a `review_start`
operation so retry, restart recovery, diagnostics, and final report extraction
use the same durable state model as other long-running work.

Inputs:

- `thread_id`: optional existing source thread. When present, default delivery
  is `detached`.
- `project_id` or `cwd`: used when MCP must create a service source thread.
  When no existing thread is passed, default delivery is `inline`.
- `target_type`: `uncommitted_changes`, `base_branch`, `commit`, or `custom`.
- `base_branch`: required for `target_type="base_branch"`.
- `commit_sha`: required for `target_type="commit"`.
- `commit_title`: optional title for commit review.
- `instructions`: required for `target_type="custom"`.
- `delivery`: optional `inline` or `detached`.
- `client_request_id`: strict start idempotency key.
- `model`, `sandbox`, and `approval_policy`: optional per-call overrides.

Target mapping to app-server:

- `uncommitted_changes` maps to `{ "type": "uncommittedChanges" }`.
- `base_branch` maps to `{ "type": "baseBranch", "branch": "..." }`.
- `commit` maps to `{ "type": "commit", "sha": "...", "title": "..." }`.
- `custom` maps to `{ "type": "custom", "instructions": "..." }`.

PR URLs and raw diffs are not separate v1 targets. Use a local checkout with
`base_branch`, or use `custom` instructions when the client has already prepared
the context.

Validation and safety:

- The source thread must be known through catalog, tracked turns, or hook
  history, unless MCP creates a service thread from `project_id` or `cwd`.
- `cwd` must be inside `CODEX_ALLOWED_ROOTS`.
- If the source thread has an active turn or pending interaction, MCP returns
  `CODEX_BUSY` and does not call app-server.
- Review workflows do not write files by themselves, but they run inside the
  selected Codex sandbox and approval policy.

`codex_start_review_workflow` returns a fast workflow ack with:

- `workflowId`
- `workflowKind="code_review"`
- `phase`
- `status`
- `reviewOperationId`
- `currentOperationId`
- nullable `reviewSourceThreadId`, `reviewThreadId`, and `reviewTurnId`
- `reviewTarget`
- `reviewDelivery`
- `nextRecommendedAction`

Poll with `codex_get_workflow_status`. Review workflow status includes:

- `reviewOperation`
- `reviewTurn`
- `reviewTarget`
- `reviewDelivery`
- `finalReport`
- `pendingInteractions`
- `currentOperationId`
- `reviewOperationId`
- `reviewSourceThreadId`
- `reviewThreadId`
- `reviewTurnId`

Phases are `queued`, `starting_thread`, `starting_review`, `reviewing`,
`completed`, `failed`, and `orphaned`. Recommended actions are `wait_review`,
`read_review_report`, `answer_pending_interaction`, and `inspect_diagnostics`.

`codex_get_operation_status` for the internal `review_start` operation includes
`reviewState`:

- `reviewState.accepted`
- `reviewState.sourceThreadId`
- `reviewState.reviewThreadId`
- `reviewState.reviewTurnId`
- `reviewState.target`
- `reviewState.delivery`
- `reviewState.startAttempted`

If MCP restarts after `review/start` was attempted but before the review turn id
is persisted, MCP does not start a second review blindly. It marks the operation
`unknown_after_app_server_exit` and the workflow moves to `orphaned` with
`nextRecommendedAction="inspect_diagnostics"`.

When the review turn completes, MCP extracts the final assistant message into
`finalReport`. Valid JSON is exposed as structured report data. Plain text is
kept as readable `finalReport.text` with `threadId`, `turnId`, and
`readFullVia`.

## Workflow thread goals

`codex_start_plan_workflow` can mirror an explicit high-level goal into the
Codex thread through app-server `thread/goal/set`.

Inputs:

- `goal`: optional objective text. If omitted, MCP does not write a thread goal.
- `goal_token_budget`: optional positive integer passed as `tokenBudget`.
- `goal_completion_action`: `clear`, `set_complete`, or `leave`. Default
  `clear`.
- `goal_completion_objective`: optional objective used with `set_complete`.

Goal sync starts after the workflow has a `threadId`, but normal workflow polling
is passive. `codex_get_workflow_status` does not call app-server goal methods
unless the caller passes `refresh_live_goal=true`.

When `refresh_live_goal=true`, MCP performs best-effort sync and returns:

- `threadGoal.configured`
- `threadGoal.managed`
- `threadGoal.syncState`
- `threadGoal.completionAction`
- `threadGoal.desiredObjective`
- `threadGoal.tokenBudget`
- `threadGoal.currentGoal`
- `threadGoal.lastSyncedAt`
- `threadGoal.clearedAt`
- `threadGoal.lastError`
- `threadGoal.available`
- `threadGoal.liveRefreshPerformed`

Common `syncState` values:

- `not_configured`: no explicit `goal` was supplied.
- `pending_thread`: goal is stored, but the workflow thread is not known yet.
- `active`: MCP set the app-server thread goal.
- `cleared`: MCP cleared its managed goal after completion.
- `complete`: MCP marked the managed goal complete after completion.
- `left`: MCP left the managed goal unchanged after completion.
- `external_override`: the current app-server goal no longer matches MCP's
  managed goal, so MCP skipped completion cleanup.
- `unsupported`: the local app-server does not support the goal method.
- `error`: app-server goal sync failed without failing the workflow.

MCP does not auto-generate goals from prompt or title. It redacts and truncates
goal text in public status and workflow events.

Clients should keep frequent polling passive. Use `refresh_live_goal=true` only
for an explicit goal sync check or a repair flow.

## Stalled turn supervision

MCP reports stalled turns in `codex_health_summary.stallSupervisor` without
calling app-server:

- `mode`: `diagnose_only` or `interrupt`
- `timeoutSeconds`: configured inactivity threshold
- `stalledTurnCount`
- `stalledTurns`
- `automaticInterruptEnabled`
- `nextRecommendedAction`

Public defaults are conservative:

- `CODEX_MCP_TURN_STALL_TIMEOUT_SECONDS=900`
- `CODEX_MCP_STALLED_TURN_ACTION=diagnose_only`

`diagnose_only` never interrupts a turn by itself. Agents should collect
diagnostics and run repair actions with `dry_run=true` before retrying or
interrupting work.

## Structured final reports

`codex_submit_task`, `codex_approve_plan`, and compatibility
`codex_execute_plan` accept optional `output_schema`.

The field must be a JSON object. MCP validates that it is serializable, non
empty, within the size limit, and compatible with Codex strict structured
outputs before it calls app-server. Object schemas must set
`additionalProperties` to `false`. Invalid schemas return `INVALID_ARGUMENT`
and do not start a Codex turn.

Supported operation types:

- `start_chat`, `send_message`, and `execute_plan`: `output_schema` is passed to
  app-server `turn/start` as `outputSchema`.
- `fork_thread`: `output_schema` is accepted only when the fork request also has
  an initial `message`.
- `steer_turn`: `output_schema` is rejected because `turn/steer` does not start
  a final-answer turn.
- `codex_start_plan_workflow`: planning turns do not accept `output_schema`.
  Use `codex_approve_plan(..., output_schema={...})` for execution output.

Status output does not echo the raw schema. It exposes:

- `outputSchemaState.provided`
- `outputSchemaState.applied`
- `outputSchemaState.schemaHash`
- `outputSchemaState.schemaChars`
- `outputSchemaState.parseStatus`
- `outputSchemaState.structuredStatus`

When a turn completes, MCP stores the final assistant message in the operation
row. Workflow execution also copies the same report into the workflow row.

`codex_get_operation_status` and `codex_get_workflow_status` may return:

- `finalReport.text`: readable final assistant text, truncated by the requested
  message budget.
- `finalReport.summary`: same compact text for clients that expect a summary
  field.
- `finalReport.structured`: parsed JSON object when the final message is valid
  JSON or contains a fenced `json` block. Otherwise `null`.
- `finalReport.structuredStatus`: `parsed` or `not_available`.
- `finalReport.structuredParseStatus`: `valid_json`, `plain_text`, or `empty`.
- `finalReport.schemaHash`: hash of the requested `output_schema`, when
  provided.
- `finalReport.threadId`, `finalReport.turnId`, and `finalReport.readFullVia`.

Plain text remains a valid final report. MCP does not extract hidden
chain-of-thought, and final reports do not include raw tool payloads or command
output.

## Turn progress journal

`codex_get_turn_status` and `codex_get_operation_status` return compact progress
data for tracked app-server turns by default.

Inputs:

- `progress_events`: number of recent progress events to return. Default `10`,
  max `100`. Use `0` to omit the progress block.
- `progress_max_chars`: max text returned for one progress event. Default
  `2000`.

Status payloads may include:

- `progressEvents`
- `progressEventCount`
- `latestProgressAt`
- `tokenUsage`
- `modelReroutes`
- `warnings`

Supported progress sources:

- `item/agentMessage/delta`
- `item/plan/delta`
- `item/reasoning/summaryPartAdded`
- `item/reasoning/summaryTextDelta`
- `thread/tokenUsage/updated`
- `model/rerouted`
- `warning`
- `configWarning`
- `guardianWarning`

`turn/diff/updated` is stored as safe metadata only. MCP keeps diff size and
line counts, but not the unified diff text. The progress journal also avoids raw
tool payloads and command output by default. It records only app-server-visible
progress summaries and does not expose hidden chain-of-thought.

`codex_collect_diagnostics` includes the same data in `progressJournal` and adds
progress entries to `timeline` with `source="turn_progress"`.

## Runtime capabilities

`codex_get_runtime_capabilities` is a read-only inventory endpoint for MCP
clients that need to understand the local Codex runtime before starting work.
It may start the MCP-owned app-server if it is not already running.

Input fields:

- `refresh`: default `false`. When `true`, bypasses the in-memory cache.
- `cwd`: optional working directory used for permission profile, hooks, and
  skills resolution. It must be inside `CODEX_ALLOWED_ROOTS`.
- `timeout_seconds`: per-method timeout. Default `2`, max `30`.
- `include_models`: default `true`.
- `include_hooks`: default `true`.
- `include_skills`: default `true`.
- `include_account`: default `true`. When `false`, skips all account, usage,
  and rate-limit inventory calls.

The tool caches one snapshot for five minutes per `cwd` and include-flag set.
Inventory calls are best effort. A timeout or error in one app-server method
does not fail the whole tool. The response stays `ok=true` and reports the
method state in `methodResults`.

Top-level result fields:

- `runtimeCapabilities`
- `cacheState`
- `methodResults`
- `warnings`
- `recommendedPollAfterSeconds=0`
- `pollRecommended=false`

`runtimeCapabilities` includes:

- `status`: `ok`, `partial`, or `unavailable`.
- `appServer`: process state plus redacted initialize metadata.
- `schemaMethods`: compact static method manifest with source, version, hash,
  method count, and method names.
- `models`: `id`, `model`, `displayName`, `isDefault`, `hidden`,
  `inputModalities`, reasoning effort fields, and service tier count.
- `permissionProfiles`: `id` and `description`.
- `sandboxReadiness`: Windows sandbox readiness status.
- `hooks`: counts grouped by cwd, event, source, trust, enabled state, and
  handler type. Raw hook commands and source paths are not returned.
- `skills`: counts grouped by cwd, scope, and enabled state. Skill names may be
  returned, but absolute paths are not returned.
- `modelProviderCapabilities`: `webSearch`, `imageGeneration`, and
  `namespaceTools`.
- `accountStatus`: `authenticated`, `requiresOpenaiAuth`, `accountType`,
  `planType`, `emailPresent`, and `identityRedacted=true`.
- `accountUsage`: availability, daily bucket count, and coarse bands for
  lifetime usage, peak daily usage, streaks, and longest turn duration.
- `rateLimits`: credit availability, unlimited flag, rate-limit reached state,
  safe used percentages, reset/window minutes, bucket count, and redacted bucket
  identities.

The account blocks never return raw email, account identifiers, credit balance,
spend-control `limit` or `used`, daily usage dates, daily bucket values, exact
usage counts, or raw rate-limit ids. Public bucket ids such as `codex` may be
shown; other bucket identities are represented by a short hash.

`codex_health_summary.runtimeCapabilities` contains only a compact subset from
the last collected runtime snapshot: status, cache age, model count, default
model, sandbox readiness, provider capabilities, account authentication state,
account and plan type, rate-limit reached state, credits availability, usage
availability, and warning count. Health summary does not collect inventory on
its own and does not include identity fields, balances, or exact usage values.

## Project preflight

`codex_preflight_project_run` is a read-only guard for long-running work. It is
meant for clients that need a quick "can I start this run?" answer before
submitting a plan workflow, review workflow, or durable operation.

Inputs:

- `project_id`: optional project id from `codex_list_projects`.
- `cwd`: optional project root. If both `project_id` and `cwd` are present,
  `cwd` must match the allowed project path.
- `model`: optional model the client plans to use.
- `sandbox`: optional sandbox mode the client plans to use.
- `approval_policy`: optional approval policy the client plans to use.
- `workflow_kind`: optional hint such as `plan_then_execute` or `code_review`.
- `live_probe`: default `false`. When `true`, MCP starts a tiny safe Codex turn
  with marker `MCP PREFLIGHT / DO NOT MODIFY FILES`.
- `timeout_seconds`: short timeout for runtime inventory.

Result fields:

- `status`: `ready`, `degraded`, or `failed`.
- `checks`: machine-readable checks for path, allowed roots, Codex home, auth,
  hooks, runtime inventory, and optional live probe.
- `runtimeCapabilities`: compact runtime subset.
- `probeOperation`: durable operation ack when `live_probe=true`.
- `nextRecommendedAction`: `start_run`, `inspect_warnings`, or `fix_environment`.
- `pollRecommended=false`.

Preflight does not replace `codex_get_runtime_capabilities`; it combines the
parts OpenClaw needs before a concrete project run.

## Agent guidance

Status, diagnostics, preflight, repair, and selected structured error responses
may include these additive fields:

- `agentGuidance`
- `agentGuidanceText`
- `recoveryAttemptState`

`agentGuidance.schemaVersion` is `agent-guidance/v1`.

`agentGuidance.problemState` is one of:

- `wait`
- `recoverable`
- `needs_input`
- `blocked`
- `fatal`
- `no_action`

`agentGuidance.instructions` is the preferred automation contract for
OpenClaw/Hermes. Each instruction includes a `kind`, optional `toolName`,
redacted `arguments`, `reason`, `expectedOutcome`, `risk`, `dryRunFirst`,
`requiresHuman`, `stopIf`, and `continueIf`.

`agentGuidance.loopGuard` prevents recovery loops. The guard key is stable for
the same problem scope and action. Default limits:

- same repair action on the same scope: two attempts per two hours;
- app-server restart on the same scope: two attempts per 30 minutes;
- workflow runtime retry from the same workflow: two retries per 24 hours;
- forced or destructive actions: one failed forced attempt blocks further
  automatic recovery.

When `loopGuard.allowed=false`, the client must stop automatic recovery for that
scope, collect diagnostics, and ask a human. Status and diagnostic methods never
execute repair by themselves. `codex_repair_issue(dry_run=false)`,
`codex_restart_app_server`, and `codex_interrupt_turn` record guarded attempts.
Dry runs are recorded for audit but do not consume the retry budget.

Known guidance rules:

- active duplicate prompt: poll the existing operation;
- `CODEX_TIMEOUT` with a `client_request_id`: poll first, then retry only with
  the same id if needed;
- failed Plan Mode with sandbox evidence: run
  `retry_workflow_with_runtime_policy` as dry run first;
- app-server unavailable while turns are active: collect diagnostics before
  restart;
- pending interaction: answer or expire it, do not restart the turn;
- auth or rate limit problem: wait or ask a human;
- invalid argument, missing project, missing thread, or missing turn: fix the
  payload or configuration before retrying.

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
