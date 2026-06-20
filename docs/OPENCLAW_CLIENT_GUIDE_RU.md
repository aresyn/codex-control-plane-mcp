# Инструкция для OpenClaw по работе с Codex Control Plane MCP

Русский | [English](OPENCLAW_CLIENT_GUIDE.md)

Этот документ описывает, как OpenClaw должен пользоваться
`codex-control-plane-mcp` как своим основным control plane для Codex Desktop.

Цель простая: OpenClaw отправляет задачу, быстро получает `operationId` или
`workflowId`, дальше принимает решения по статусу и не держит долгий MCP-вызов
открытым. Все долгие действия должны быть durable, pollable и retry-safe.

## Базовая модель

`codex-control-plane-mcp` скрывает от OpenClaw детали работы с
`codex-app-server`:

- запуск и проверка app-server;
- создание threads и turns;
- durable очередь операций;
- retry-idempotency через `client_request_id`;
- защита от дублей prompt и turn;
- Plan Mode workflow;
- approvals, questions и interrupts;
- progress journal;
- hook-backed SQLite history;
- diagnostics и repair actions;
- runtime inventory: модели, sandbox, hooks, skills, account status и rate
  limits.

OpenClaw не должен напрямую мутировать файлы Codex, внутренние SQLite Codex или
transcript-файлы. Все write/control действия идут через MCP tools, а MCP уже
использует `codex-app-server`.

## Обязательный стартовый handshake

При старте OpenClaw, при reconnect и после restart MCP:

1. Вызови `codex_health_summary`.
2. Проверь:
   - `ok == true`;
   - `version.serverName == "codex-control-plane-mcp"`;
   - `version.contractVersion == "1"`;
   - `version.toolSurfaceHash` присутствует;
   - нужные stable tools есть в `version.stableTools`.
3. Если OpenClaw планирует запускать новые задачи, вызови
   `codex_get_runtime_capabilities`.
4. Если `hookHistory.status` не `ok`, не блокируй работу, но добавь warning в
   свою диагностику.
5. Если `runtimeCapabilities.status == "partial"`, смотри `methodResults` и
   решай по конкретной причине.

Минимальная проверка:

```json
{
  "tool": "codex_health_summary",
  "arguments": {}
}
```

Если `codex_health_summary` недоступен или возвращает protocol error, OpenClaw
должен считать MCP несовместимым и не запускать write tools.

## Worker-архитектура для OpenClaw

Для обычной работы OpenClaw MCP-клиенты должны быть в `client` mode, а отдельный
фоновой worker - в `worker` mode.

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

В такой схеме OpenClaw-агенты только ставят задачи, отвечают на interactions и
читают status. Worker единолично владеет `codex-app-server`, queue slots, leases
и resource locks.

В долгих write-запросах OpenClaw должен передавать:

- `agent_id`: стабильный id агента, например `codex-dev` или
  `book-codex-agent`;
- `resource_keys`: write scopes, если задача может менять файлы;
- `priority`: обычно `normal`;
- `estimated_cost_class`: `light`, `normal` или `heavy`.

Если `codex_get_operation_status` вернул `queueState.queuedReason`, не создавай
новую operation. Следуй `nextRecommendedAction`:

- `wait_for_worker_slot`: продолжай poll той же operation;
- `wait_for_resource_lock`: продолжай poll той же operation или жди окончания
  конфликтующей write-задачи;
- `inspect_worker_health`: вызови `codex_get_worker_status` и
  `codex_get_concurrency_status`;
- `inspect_diagnostics`: собери diagnostics по той же operation или workflow.

Для running turn ориентируйся на worker-поля, а не на догадки локального
app-server: `slotState.claimed`, `slotClaim`, `workerState`,
`resourceLockState` и `queueState`. В client mode gateway не должен запускать
свой app-server только ради status.

## Главные правила для OpenClaw

1. Для новых долгих задач используй `codex_submit_task`.
2. Для Plan Mode используй `codex_start_plan_workflow`,
   `codex_get_workflow_status`, `codex_approve_plan`.
3. Для code review используй `codex_start_review_workflow` и
   `codex_get_workflow_status`.
4. Для добавления уточнения в активный turn используй
   `codex_submit_task(operation_type="steer_turn")`.
5. Для альтернативной ветки анализа используй
   `codex_submit_task(operation_type="fork_thread")`.
6. Для любого retry-safe write запроса всегда передавай `client_request_id`.
7. Никогда не жди завершения задачи внутри одного MCP call. Получи id и poll.
8. Следуй `nextRecommendedAction`, `recommendedPollAfterSeconds` и
   `pollRecommended`.
9. Низкоуровневые `codex_start_chat`, `codex_send_message`,
   `codex_execute_plan` используй только для совместимости.
10. Если статус терминальный, не продолжай polling, кроме ручной диагностики.

## Result contract

Каждый tool возвращает MCP `structuredContent`. OpenClaw должен читать именно
`structuredContent`, а `content[0].text` считать удобной JSON-копией для людей.

Успех:

```json
{
  "ok": true
}
```

Ошибка уровня tool:

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

Правила:

- ветвись по `error.code`, а не по `message`;
- учитывай `error.retryable`;
- JSON-RPC errors считаются ошибками протокола;
- domain errors внутри `tools/call` не означают, что MCP transport сломан.

## Безопасный публичный status

Считай MCP status agent-safe ответом, а не raw audit log. Operation и workflow
status возвращают `requestSummary`, а не raw `request`. В `requestSummary` есть
ids, runtime policy, scheduling intent, input item state, hash output schema,
resource keys и hash/размер текстовых полей. Там нет полного prompt,
instructions, title, raw image path, raw URL или raw command output.

Если OpenClaw нужно позже показать исходный текст задачи, он должен хранить его
в своем state. Не рассчитывай, что MCP status вернет prompt обратно. Для
сопоставления используй `requestSummary.messageSummary.sha256` и `operationId`.

Thread titles возвращаются как `titleSummary`. Token usage возвращается
укрупненно, без exact counts. Diagnostic logs с payload не являются обычной
поверхностью polling: используй их только для точечной ручной диагностики, и
там все равно действует secret redaction.

Для queue status действует жесткое правило: если `queueSummary.queued == 0` и
`blockedByLocks` пустой, `nextRecommendedAction` должен быть `none`, даже если
есть running turns. Running turns сами по себе не являются поводом создавать
retry.

Runtime inventory в `client` mode пассивный. `refresh=true` ставит worker
command. После завершения worker command следующие passive-вызовы могут вернуть
sanitized worker status snapshot.

## Идентификаторы и idempotency

OpenClaw должен хранить связь между своей задачей и MCP id:

- `operationId`: durable operation из `codex_submit_task`;
- `workflowId`: durable workflow из plan/review tools;
- `actionId`: lifecycle action, например compaction;
- `threadId`: Codex thread;
- `turnId`: Codex turn;
- `client_request_id`: строгий ключ retry-idempotency.

Рекомендованный формат `client_request_id`:

```text
openclaw:<domain-task-id>:<phase>:<stable-hash>
```

Примеры:

```text
openclaw:issue-123:analysis:v1
openclaw:issue-123:plan-workflow:v1
openclaw:issue-123:approve-plan:v1
openclaw:issue-123:review-uncommitted:v1
```

Не используй random UUID для retry одной и той же логической операции. UUID
подходит только если ты намеренно создаешь новую независимую операцию.

Не переиспользуй один `client_request_id` для разных сообщений, разных threads
или разных workflow phases.

## Как выбирать tool

| Ситуация | Tool | Что хранить |
|---|---|---|
| Новая обычная задача | `codex_submit_task` с `operation_type="start_chat"` | `operationId`, затем `threadId`, `turnId` |
| Продолжить существующий thread | `codex_submit_task` с `operation_type="send_message"` | `operationId`, `threadId`, `turnId` |
| Выполнить утвержденный plan | `codex_approve_plan` или `codex_submit_task` с `operation_type="execute_plan"` | `workflowId`, `executionOperationId` |
| Добавить уточнение в активный turn | `codex_submit_task` с `operation_type="steer_turn"` | `operationId`, target `threadId`, target `turnId` |
| Создать альтернативную ветку | `codex_submit_task` с `operation_type="fork_thread"` | `operationId`, forked `threadId` |
| Plan Mode от начала до результата | `codex_start_plan_workflow` | `workflowId` |
| Code review | `codex_start_review_workflow` | `workflowId`, `reviewThreadId`, `reviewTurnId` |
| Ответить на вопрос или approval | `codex_list_pending_interactions`, затем `codex_answer_pending_interaction` | `interactionId` |
| Прервать зависший turn | `codex_interrupt_turn` | `threadId`/`turnId` или `operationId`/`workflowId` |
| Найти старый чат | `codex_search_chats` | `threadId`, `chatId`, `projectId` |
| Прочитать чат | `codex_get_chat` | message list, source |
| Проверить runtime | `codex_get_runtime_capabilities` | cache snapshot |
| Проверить долгий run перед стартом | `codex_preflight_project_run` | ready/degraded/failed checks |
| Диагностика | `codex_collect_diagnostics` | issue summary, repair hints |
| Ремонт | `codex_repair_issue` | repair result |
| Архивировать thread | `codex_archive_thread` | lifecycle action |
| Разархивировать thread | `codex_unarchive_thread` | lifecycle action |
| Сжать thread | `codex_start_thread_compaction`, потом status | `actionId` |

## Алгоритм polling для operation

После `codex_submit_task`:

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

OpenClaw должен читать:

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

Терминальные operation statuses:

- `completed`;
- `failed`;
- `aborted`;
- `cancelled`;
- `canceled`;
- `interrupted`;
- `orphaned`;
- `unknown_after_app_server_exit`.

Только `completed` является успешным завершением. Статус
`unknown_after_app_server_exit` не является успехом.

## Алгоритм polling для workflow

Plan workflow и review workflow читаются через `codex_get_workflow_status`.

```text
workflow = codex_start_plan_workflow(...) или codex_start_review_workflow(...)
store workflow.workflowId

while true:
    status = codex_get_workflow_status(workflowId)
    handle pendingInteractions
    handle nextRecommendedAction

    if status.pollRecommended == false:
        break

    sleep(status.recommendedPollAfterSeconds or fallback)
```

Для Plan Mode основные actions:

- `wait_plan`: продолжай polling;
- `wait_for_worker_slot`: текущая plan или execution operation ждет свободный
  worker slot;
- `wait_for_resource_lock`: текущая operation ждет scoped write lock;
- `review_plan`: покажи план человеку или аналитику;
- `adopt_candidate_plan`: проверь `workflowObservation.candidatePlans` и
  вызови `codex_adopt_workflow_plan`, если кандидат подходит;
- `execute_plan`: вызови `codex_approve_plan`;
- `answer_pending_interaction`: обработай pending interaction;
- `wait_execution`: продолжай polling execution;
- `read_final_report`: забери `finalReport`;
- `inspect_diagnostics`: запусти диагностику.

Для review workflow:

- `wait_review`: продолжай polling;
- `read_review_report`: забери `finalReport`;
- `answer_pending_interaction`: обработай pending interaction;
- `inspect_diagnostics`: запусти диагностику.

### Перепроверка workflow при восстановлении

Не переводь run OpenClaw в blocked только по `workflow.status`. Codex thread
может продвинуться после официального workflow turn: например, когда человек
открыл Codex Desktop, дал доступ к файлам или написал уточнение в тот же thread.

Если workflow вернул `failed`, `plan_needs_review`, `plan_candidate_found`,
`orphaned` или подозрительный `plan_ready`, сначала сделай проверки:

1. Прочитай `workflowObservation`.
2. Если `recoverableCandidateFound == true`, проверь `candidatePlans`.
3. Вызови `codex_get_chat` для `threadId` workflow.
4. Вызови `codex_collect_diagnostics` с `workflow_id`.
5. Проверь внешний результат бизнес-задачи: id комментария YouTrack, изменение
   файла или маркер отчета.

`workflowObservation` показывает расхождение между workflow и живым thread.
Важные поля:

- `officialPlanTurnId`: turn, который сейчас привязан к workflow.
- `expectedExecutionTurnId`: ожидаемый execution turn после approval.
- `officialPlanQuality`: оценка качества официального плана.
- `threadAdvancedAfterOfficialTurn`: в этом же thread есть более поздние
  untracked turns. Для нормального ожидаемого execution turn MCP не должен
  выставлять этот флаг.
- `recoverableCandidateFound`: найден более поздний пригодный план или отчет.
- `candidatePlans`: пригодные планы из того же thread, с `turnId`,
  `planHash`, `quality` и `markdown`.

Плановый результат читай из `latestPlan`. Не считай
`planOperation.finalReport` планом. Если MCP вернул
`planOperation.planArtifactSummary`, используй его только как компактную ссылку
на артефакт плана.

Если `nextRecommendedAction == "adopt_candidate_plan"`, правильный путь
восстановления такой:

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

После adoption снова вызови `codex_get_workflow_status`. Вызывай
`codex_approve_plan` только когда `latestPlan.planQuality == "valid_plan"` и
план соответствует задаче.

## Новая задача: `start_chat`

Используй, когда нужно начать отдельную задачу в проекте Codex.

Сначала найди проект:

```json
{
  "tool": "codex_list_projects",
  "arguments": {}
}
```

Потом отправь задачу:

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

Ожидаемый быстрый ответ:

```json
{
  "ok": true,
  "operationId": "...",
  "status": "queued",
  "pollRecommended": true
}
```

Дальше только `codex_get_operation_status`.

## Продолжить thread: `send_message`

Используй, когда задача уже имеет `threadId` или `chat_id`, и нужно создать
новый turn.

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

Не используй `send_message`, если target turn еще активен. Для активного turn
используй `steer_turn`.

## Уточнить активный turn: `steer_turn`

`steer_turn` добавляет текст в уже активный Codex turn и не создает второй turn.

Используй, когда:

- Codex уже работает;
- появился новый комментарий или уточнение;
- нужно добавить ограничение, формат ответа или дополнительный контекст;
- нельзя создавать новый turn.

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

После ACK steering operation остается `running` и следует за target turn до
терминального статуса.

Не применяй prompt dedup к steering. Если нет `client_request_id`, каждый вызов
считается новой steering-командой.

Ошибки:

- `CODEX_TURN_NOT_FOUND`: target turn неизвестен;
- `INVALID_ARGUMENT`: turn уже terminal или не совпадает с thread;
- `CODEX_BUSY`: если действие конфликтует с текущим состоянием.

## Fork thread

`fork_thread` создает новую ветку от существующего thread.

Fork без первого сообщения:

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

Fork с первым сообщением:

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

В status:

- top-level `threadId` после fork означает forked thread;
- source thread находится в `forkState.sourceThreadId`;
- fork-only operation завершается сразу после создания thread;
- fork-plus-message дальше отслеживается как обычный turn operation.

Prompt dedup для `fork_thread` выключен. Это правильно: два похожих fork могут
быть намеренными. Retry-safety обеспечивает только `client_request_id`.

## Image inputs

Изображения передаются через `input_items` только для операций, которые запускают
новый turn:

- `start_chat`;
- `send_message`;
- `execute_plan`;
- `fork_thread` с `message`.

Пример:

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

Правила:

- `localImage.path` должен существовать и быть внутри `CODEX_ALLOWED_ROOTS`;
- относительный path считается относительно effective `cwd`;
- разрешены `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`;
- `data:` и `file:` URL запрещены;
- raw bytes не сохраняются;
- raw URL и полный локальный path не возвращаются в status/diagnostics;
- status содержит только `inputItemState` с безопасной метаинформацией.

Если `input_items` переданы в `steer_turn` или fork-only запрос без `message`,
MCP вернет `INVALID_ARGUMENT`.

## Structured final reports

Если OpenClaw нужен машинно-читаемый результат, передавай `output_schema`.

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

OpenClaw должен читать:

- `outputSchemaState`;
- `finalReport.text`;
- `finalReport.structured`;
- `finalReport.structuredStatus`;
- `finalReport.structuredParseStatus`;
- `finalReport.threadId`;
- `finalReport.turnId`;
- `finalReport.readFullVia`.

Если `finalReport.structured == null`, не считай задачу проваленной
автоматически. Сначала проверь `status`, `finalReport.text` и
`structuredParseStatus`.

## Plan Mode workflow

Используй Plan Mode, когда перед исполнением нужен отдельный review плана.

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

У Plan Mode есть нижняя граница sandbox. Не запрашивай `read-only` для Plan
Mode намеренно. Если клиент все же передал `read-only`, MCP повысит effective
sandbox до `workspace-write` и вернет `runtimePolicyAdjusted=true`,
`requestedSandbox` и `effectiveSandbox` в workflow status.

Повторный approve с тем же workflow не должен создавать второй execution turn.
OpenClaw должен читать `executionOperationId` и дальше отслеживать workflow.

Если `nextRecommendedAction == "review_plan"`, покажи план человеку или
вызывающей системе. Если план пустой или не завершен, не вызывай approve.

## Workflow thread goals

Если OpenClaw ведет свою цель, передавай ее явно:

```json
{
  "goal": "Investigate the issue and prepare an analyst-facing report",
  "goal_token_budget": 50000,
  "goal_completion_action": "clear"
}
```

Смотри `threadGoal` в `codex_get_workflow_status`. Обычный workflow polling
пассивный и не вызывает live goal methods в app-server. Если нужно
синхронизировать или проверить цель, вызывай `codex_get_workflow_status` с
`refresh_live_goal=true`.

- `syncState == "active"`: цель установлена;
- `pending_thread`: thread еще не создан;
- `cleared`: MCP очистил managed goal после завершения;
- `complete`: MCP пометил managed goal complete;
- `left`: MCP оставил goal как есть;
- `external_override`: цель в Codex изменилась извне, MCP не трогает ее;
- `unsupported`: app-server не поддерживает goal methods;
- `error`: goal sync не удался, но workflow может продолжаться.

Не пытайся сам выводить goal из prompt. Если цель нужна, передай ее явно.

## Code review workflow

Используй `codex_start_review_workflow`, когда OpenClaw хочет получить review
по локальному checkout.

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

Review относительно branch:

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

Поддерживаемые `target_type`:

- `uncommitted_changes`;
- `base_branch`;
- `commit`;
- `custom`.

Для PR URL в v1 не передавай raw diff. Используй локальный checkout и
`base_branch`, либо `custom` instructions.

Poll через `codex_get_workflow_status`. Финальный отчет ищи в `finalReport`.

## Pending interactions

Если operation или workflow возвращает `pendingInteractions` или
`nextRecommendedAction == "answer_pending_interaction"`:

1. Вызови `codex_list_pending_interactions`.
2. Найди interaction по `operationId`, `workflowId`, `threadId` или `turnId`.
3. Покажи вопрос человеку или применяй policy OpenClaw.
4. Ответь через `codex_answer_pending_interaction`.

Пример:

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

Если interaction относится к старой app-server generation, MCP может вернуть
`CODEX_PENDING_INTERACTION_UNAVAILABLE`. Тогда переходи к диагностике.

## Interrupt

Используй `codex_interrupt_turn`, если нужно остановить активный turn.

Возможные входы:

- `thread_id` и `turn_id`;
- `operation_id`;
- `workflow_id`.

Пример:

```json
{
  "tool": "codex_interrupt_turn",
  "arguments": {
    "operation_id": "OPERATION_ID",
    "reason": "User cancelled the task."
  }
}
```

После interrupt продолжай polling. Ожидаемые terminal states:
`interrupted`, `cancelled`, `canceled` или `unknown_after_app_server_exit`.

## Thread lifecycle

Архивировать:

```json
{
  "tool": "codex_archive_thread",
  "arguments": {
    "thread_id": "THREAD_ID",
    "refresh_catalog": true
  }
}
```

Разархивировать:

```json
{
  "tool": "codex_unarchive_thread",
  "arguments": {
    "thread_id": "THREAD_ID",
    "refresh_catalog": true
  }
}
```

Сжать thread:

```json
{
  "tool": "codex_start_thread_compaction",
  "arguments": {
    "thread_id": "THREAD_ID"
  }
}
```

Проверить compaction:

```json
{
  "tool": "codex_get_thread_compaction_status",
  "arguments": {
    "action_id": "ACTION_ID",
    "include_events": false
  }
}
```

Lifecycle tools откажутся работать с неизвестным thread или thread с active turn
и вернут `CODEX_THREAD_NOT_FOUND` или `CODEX_BUSY`.

## Read and search tools

Используй read tools для UI, lookup и восстановления контекста:

### `codex_list_projects`

Вернет проекты, доступные через catalog, registry, hook history и legacy
fallbacks.

Когда использовать:

- перед `start_chat`;
- чтобы сопоставить локальный путь с `projectId`;
- для проверки path casing.

Для обычного polling и UI-списков используй compact mode:

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

`refresh=true` включай только для явного обновления catalog. Перед выводом
полного списка проверяй `cacheState` и `truncated`.

### `codex_list_project_chats`

Вернет chats конкретного проекта.

Когда использовать:

- чтобы показать историю проекта;
- чтобы найти последний thread по задаче;
- чтобы проверить archived filters.

### `codex_list_active_chats`

Вернет active/running chats.

Когда использовать:

- перед restart app-server;
- перед запуском потенциально конфликтующего действия;
- для UI активных задач.

### `codex_search_chats`

Ищи по prompt, ответам, hook history, transcripts и summary.

Когда использовать:

- найти thread по issue id;
- проверить, была ли похожая задача;
- найти итоговый отчет после сбоя app-server.

Если refresh запрошен с маленьким time budget, MCP может вернуть частичный
результат с `timeBudgetExhausted=true`. Тогда повтори поиск без refresh или
осознанно увеличь budget.

### `codex_get_chat_status`

Дает компактный статус thread/chat.

Когда использовать:

- перед `send_message`;
- перед archive/unarchive/compact;
- чтобы понять source: `app_server`, `hook_history`, `transcript`,
  `tracked_turn`, `tracked_turn+hook_history`, `app_server+hook_history`,
  `transcript+hook_history` или `mixed`. Для свежих threads сначала ожидай
  `tracked_turn` или hook history, а legacy KB должен быть только fallback.

### `codex_get_chat`

Читает сообщения thread/chat.

Когда использовать:

- получить полный ответ после `finalReport.readFullVia`;
- показать историю человеку;
- проверить, что hooks или transcripts сохранили результат.

Если catalog еще не увидел свежий thread, MCP все равно может собрать summary
из tracked turns и hook history. Считай `CODEX_THREAD_NOT_FOUND` окончательным
только после search и diagnostics.

### `codex_get_turn_status`

Читает статус конкретного turn.

Когда использовать:

- если у OpenClaw есть `threadId` и `turnId`, но нет `operationId`;
- для progress journal;
- для восстановления после внешних действий пользователя в Codex Desktop.

Поля:

- `progress_events`: default `10`, max `100`, `0` отключает progress block;
- `progress_max_chars`: default `2000`.

## Runtime capabilities

Вызывай перед сложной задачей, после reconnect или при диагностике:

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

Как принимать решения:

- нет подходящей модели: не запускай задачу, покажи проблему;
- sandbox not ready: выбери безопасный режим или попроси человека исправить
  environment;
- hooks not installed: работа может идти, но история будет слабее;
- account unauthenticated: не запускай write operation, если Codex требует auth;
- rate limit reached: отложи задачу и используй `recommendedPollAfterSeconds`
  или свой backoff;
- `status == "partial"`: смотри конкретный `methodResults`.

В `client` execution mode этот tool по умолчанию пассивный. Он может вернуть
последний worker snapshot с `cacheSource="worker_registry"` и
`workerRuntimeSnapshot`. При `refresh=true` MCP должен попросить worker обновить
inventory и может вернуть `refreshCommandId`; проверь status worker command или
повтори capabilities после обновления snapshot.

Account fields уже redacted. Не пытайся извлечь email или account id из
diagnostics.

## Preflight проекта

Перед многочасовым run вызывай `codex_preflight_project_run`. Он проверяет путь
проекта, allowed roots, Codex home, auth, hooks, runtime inventory и, если
нужно, пробный live-старт turn.

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

Как принимать решение:

- `status == "ready"`: run можно запускать.
- `status == "degraded"`: запускай только если осознанно принимаешь слабое
  место для этой задачи.
- `status == "failed"`: не запускай. Прочитай `checks` и исправь окружение или
  выбери другую runtime policy.

`live_probe=true` используй только когда клиенту разрешено создать маленький
безопасный Codex turn. Probe использует маркер
`MCP PREFLIGHT / DO NOT MODIFY FILES` и возвращает обычный durable operation id.

## Health summary

`codex_health_summary` легкий и безопасный. Его можно вызывать часто.

Используй для:

- startup handshake;
- reconnect;
- dashboard;
- проверка stale operations;
- проверка pending interactions;
- проверка hook history;
- решение, можно ли restart app-server.

Если `pollRecommended == true`, продолжай health polling или переходи в
diagnostics. Это значит, что есть active turns, pending interactions или stale
operations.

Сначала оцени текущую readiness-картину. Старые orphaned или stale записи
попадают в `historicalDebt`; сами по себе они не означают, что runtime сейчас
broken. Для них используй `historicalDebt.nextRecommendedAction`, обычно
`run_targeted_cleanup`, вне горячего path.

Перед retry долгого turn смотри `stallSupervisor`:

- `mode == "diagnose_only"`: не прерывай turn автоматически;
- `stalledTurnCount > 0`: собери diagnostics и запусти repair с `dry_run=true`;
- `automaticInterruptEnabled == true`: только тогда локальная автоматизация может
  прерывать stale turns.

## Diagnostics

При любой непонятной проблеме сначала вызывай:

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

Можно диагностировать по:

- `operation_id`;
- `workflow_id`;
- `thread_id`;
- `turn_id`;
- `project_id`;
- `chat_id`.

Читай:

- `summary`;
- `scopedFindings`;
- `backgroundFindings`;
- `timeline`;
- `progressJournal`;
- `hookHistory`;
- `issues`;
- `recommendedActions`;
- `repairActions`.

Решения принимай по `scopedFindings`. `backgroundFindings` это исторический или
соседний контекст, он не должен перебивать свежий scoped match по текущей operation,
workflow, thread или turn. `codex_analyze_issue` использует тот же принцип и
должен возвращать compact evidence refs вместо raw payload. Если пришло
`evidenceTruncated=true`, запускай targeted diagnostics, а не широкий raw log.

### Agent guidance

Если status, diagnostics, preflight или structured error вернули
`agentGuidance`, считай этот блок главным контрактом для следующего шага.

Сначала читай:

- `problemState`;
- `summary`;
- `instructions`;
- `loopGuard`;
- `evidenceRefs`;
- `agentGuidanceText`.

Правила для OpenClaw:

1. Выполняй `agentGuidance.instructions` перед тем, как переводить run в blocked.
2. Если instruction содержит `dryRunFirst=true`, сначала вызови repair с
   `dry_run=true`.
3. Если `loopGuard.allowed=false`, останови автоматический recovery для этого
   scope. Собери diagnostics и попроси человека.
4. После `CODEX_TIMEOUT` poll-ь существующую operation или повторяй запрос с
   тем же `client_request_id`. Новый id не создавай.
5. Pending approvals и user input нужно ответить или истечь. Не перезапускай
   turn, чтобы обойти ожидание.
6. Auth и rate limit guidance означает ждать или просить человека. Повторный
   запуск turns здесь только тратит время.

`agentGuidanceText` подходит для логов и сообщений оператору. Решения принимай
по `agentGuidance.instructions`.

По умолчанию не запрашивай raw payload. Если нужен `include_payload=true`, не
показывай результат пользователю без redaction.

## Analyze issue

`codex_analyze_issue` помогает классифицировать известную проблему MCP state.

Используй после diagnostics, если OpenClaw хочет получить компактное объяснение
и repair options.

```json
{
  "tool": "codex_analyze_issue",
  "arguments": {
    "operation_id": "OPERATION_ID"
  }
}
```

Это не заменяет доменную разработку. Это диагностика MCP/app-server layer.

## Repair

`codex_repair_issue` должен запускаться осторожно.

Правило OpenClaw:

1. Сначала `dry_run=true`.
2. Покажи planned changes человеку или своей policy engine.
3. Только потом `dry_run=false`.
4. Для опасных действий требуй `force=true`, если tool этого просит.

Пример:

```json
{
  "tool": "codex_repair_issue",
  "arguments": {
    "action": "refresh_catalog_and_history",
    "dry_run": true
  }
}
```

`refresh_catalog_and_kb` остается legacy alias. Новый путь:
`refresh_catalog_and_history`.

Если Plan Mode workflow упал из-за неверного sandbox или approval policy,
используй `retry_workflow_with_runtime_policy`. Action создает новый workflow и
связывает его со старым через `workflowRetryState`.

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

После успешного dry run повтори вызов с `dry_run=false`, сохрани
`newWorkflowId` и poll-ь новый workflow. Старый terminal turn не оживляй.

## App-server status and restart

Проверить:

```json
{
  "tool": "codex_get_app_server_status",
  "arguments": {}
}
```

Restart:

```json
{
  "tool": "codex_restart_app_server",
  "arguments": {
    "mode": "restart_app_server_idle"
  }
}
```

Правила:

- перед restart вызови `codex_health_summary`;
- не restart-и app-server при active turns или pending interactions;
- если нужен forced restart, сначала сохрани diagnostics;
- после restart сделай `codex_health_summary`;
- operations, которые стали `unknown_after_app_server_exit`, не считай
  успешными.

## Progress journal

OpenClaw должен использовать progress journal для UX и early diagnostics.

В `codex_get_operation_status` и `codex_get_turn_status` смотри:

- `progressEvents`;
- `progressEventCount`;
- `latestProgressAt`;
- `tokenUsage`;
- `modelReroutes`;
- `warnings`.

Полезная стратегия:

- если нет final assistant message, но есть fresh `progressEvents`, задача
  жива;
- если `warnings` растут, покажи их оператору;
- если есть `modelReroutes`, обнови UI, но не считай это ошибкой;
- `tokenUsage` в публичном status используй только как coarse bands, без exact
  token counts;
- если `latestProgressAt` старый и turn active, собери diagnostics.

MCP не сохраняет hidden chain-of-thought, raw command output, raw tool payloads
и full diffs.

## Hook history

Hook history это второй локальный источник чтения.

OpenClaw должен использовать его через обычные read/status/search tools. Не
нужно читать hook tables напрямую.

Поля, на которые стоит смотреть:

- `hookHistory.enabled`;
- `hookHistory.installed`;
- `hookHistory.dbWritable`;
- `hookHistory.lastEventAt`;
- `hookHistory.threadCount`;
- `hookHistory.turnCount`;
- `hookHistory.warnings`.

Если hook history выключен или hooks не установлены:

- write operations все равно работают;
- search/status fallback может быть слабее;
- предложи установить hooks через `codex-control-plane-mcp-hooks install`;
- не блокируй критичную задачу только из-за отсутствия hooks.

## Ошибки и стратегия обработки

| Error code | Что значит | Что делать OpenClaw |
|---|---|---|
| `INVALID_ARGUMENT` | Неверные аргументы | Исправить payload, не retry без изменений |
| `CODEX_PROJECT_NOT_FOUND` | Проект неизвестен | Вызвать `codex_list_projects`, проверить allowed roots |
| `CODEX_THREAD_NOT_FOUND` | Thread неизвестен | Поискать через `codex_search_chats`, проверить source id |
| `CODEX_TURN_NOT_FOUND` | Turn неизвестен | Получить status чата, не использовать stale turn id |
| `CODEX_DUPLICATE_PROMPT_ACTIVE` | Уже есть активная похожая задача | Продолжить existing operation/thread из details |
| `CODEX_BUSY` | Thread или app-server занят | Poll active turn или pending interaction |
| `CODEX_TIMEOUT` | Timeout app-server/tool | Если есть `client_request_id`, retry безопасен |
| `CODEX_APP_SERVER_UNAVAILABLE` | app-server недоступен | Health, diagnostics, restart только если idle |
| `CODEX_PENDING_INTERACTION_NOT_FOUND` | Interaction не найден | Refresh pending list, проверить workflow/operation |
| `CODEX_PENDING_INTERACTION_UNAVAILABLE` | Interaction нельзя доставить | Diagnostics, возможно app-server generation устарела |
| `CODEX_TRANSCRIPT_NOT_FOUND` | Нет transcript | Попробовать hook history, diagnostics |
| `CODEX_SEND_FAILED` | app-server не принял write | Не считать задачу начатой, смотреть operation status |
| `CODEX_SUMMARY_FAILED` | Summary не построен | Читать raw chat, не блокировать completion |

Если `error.retryable == true`, retry допустим. Но write retry должен идти с тем
же `client_request_id`.

## Duplicate prompt handling

Для `start_chat`, `send_message` и `execute_plan` MCP защищает от активных
дублей prompt.

Если OpenClaw получил `CODEX_DUPLICATE_PROMPT_ACTIVE`:

1. Не создавай новый turn.
2. Возьми `operationId`, `threadId` или `turnId` из `error.details`, если они
   есть.
3. Poll-и existing operation или turn.
4. Сообщи пользователю, что задача уже выполняется.

Если duplicate относится к completed operation, MCP может вернуть existing
thread для продолжения. Не воспринимай это как ошибку, если `ok == true`.

`steer_turn` и `fork_thread` не участвуют в prompt dedup. Для них важен
`client_request_id`.

## Выбор sandbox и approval policy

Публичные безопасные дефолты сервера:

- `CODEX_MCP_DEFAULT_SANDBOX=read-only`;
- `CODEX_MCP_DEFAULT_APPROVAL_POLICY=on-request`.

OpenClaw может переопределять их в каждом tool call:

```json
{
  "sandbox": "read-only",
  "approval_policy": "on-request"
}
```

Рекомендации:

- read-only анализ: `read-only` и `on-request`;
- работа с недоверенным репозиторием: `read-only`;
- Plan Mode planning: минимум `workspace-write`;
- задача, где Codex может менять файлы: требуй явного решения пользователя или
  policy OpenClaw;
- не меняй server defaults ради одной задачи, передай override в call.

## Как OpenClaw должен вести состояние

Для каждой задачи храни:

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

После каждого poll обновляй сохраненные ids. `threadId` и `turnId` могут
появиться не сразу.

Если OpenClaw перезапустился:

1. Восстанови свои `operationId` и `workflowId`.
2. Вызови `codex_health_summary`.
3. Poll-и незавершенные operations/workflows.
4. Если id потерян, используй `codex_search_chats` по external task id,
   marker или prompt hash.

## Рекомендуемые prompt markers

Для задач OpenClaw добавляй в prompt короткий стабильный marker:

```text
OpenClaw task: <id>
Mode: analysis
Safety: read-only unless explicitly approved
```

Для live smoke:

```text
MCP LIVE TEST / DO NOT MODIFY FILES / <timestamp>
```

Marker помогает search, diagnostics и человеку в Codex Desktop.

## Практические сценарии

### Анализ issue без изменения файлов

1. `codex_list_projects`.
2. Найти `project_id`.
3. `codex_submit_task(operation_type="start_chat", sandbox="read-only")`.
4. Poll `codex_get_operation_status`.
5. Если `answer_pending_interaction`, обработать pending interaction.
6. Если `completed`, прочитать `finalReport` или `latestMessages`.
7. Если `failed` или `unknown_after_app_server_exit`, собрать diagnostics.

### Попросить активного Codex добавить технический план

1. Из operation status взять `threadId` и `turnId`.
2. Убедиться, что turn active.
3. Вызвать `codex_submit_task(operation_type="steer_turn")`.
4. Poll steering operation или исходный operation.
5. Проверить, что итоговый ответ учитывает steering message.

### Plan Mode с утверждением

1. `codex_start_plan_workflow`.
2. Poll `codex_get_workflow_status`.
3. Если action `adopt_candidate_plan`, проверь кандидата и вызови
   `codex_adopt_workflow_plan`.
4. Когда action `review_plan`, покажи plan человеку или policy engine.
5. `codex_approve_plan` с новым `client_request_id`.
6. Poll workflow до `read_final_report`.
7. Сохранить `finalReport`.

### Code review

1. `codex_get_runtime_capabilities(cwd=...)`.
2. Если sandbox/account ok, `codex_start_review_workflow`.
3. Poll `codex_get_workflow_status`.
4. Если pending interaction, ответить.
5. На `read_review_report` сохранить `finalReport`.

### Восстановление после таймаута клиента

1. Повтори исходный write call с тем же `client_request_id`.
2. Если MCP вернул existing `operationId`, отслеживай его.
3. Если получил duplicate active, отслеживай operation из details.
4. Если не нашел operation, используй `codex_search_chats` по marker.

### App-server пропал во время turn

1. `codex_get_operation_status`.
2. Если `unknown_after_app_server_exit`, не считать успехом.
3. `codex_collect_diagnostics`.
4. Проверить `hookHistory` и `codex_get_chat`.
5. Если hook history показывает completed turn, можно использовать read result,
   но status все равно нужно отметить как recovered by history в OpenClaw.
6. Если result нет, предложить повтор задачи с новым `client_request_id`.

### Thread занят

Если получил `CODEX_BUSY`:

1. `codex_get_chat_status`.
2. `codex_get_turn_status` для active turn.
3. Если нужно добавить контекст, используй `steer_turn`.
4. Если нужно ждать, отслеживай до terminal.
5. Не запускай `send_message`, archive, unarchive или compaction пока busy.

## Public method reference

### Stable orchestration tools

#### `codex_submit_task`

Главный write path для durable операций.

Operation types:

- `start_chat`;
- `send_message`;
- `execute_plan`;
- `steer_turn`;
- `fork_thread`.

Всегда предпочитай этот tool низкоуровневым write tools.

#### `codex_get_operation_status`

Основной polling endpoint для operations.

Читай:

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
- `pollRecommended`.

#### `codex_start_plan_workflow`

Создает durable Plan Mode workflow и возвращает `workflowId`.

#### `codex_start_review_workflow`

Создает durable code review workflow через app-server `review/start`.

#### `codex_get_workflow_status`

Основной polling endpoint для plan и review workflows.

#### `codex_adopt_workflow_plan`

Принимает более поздний валидный план из того же workflow thread. Используй
только после проверки `workflowObservation.candidatePlans`.

#### `codex_approve_plan`

Запускает execution после готового plan. Повторный approve не должен создавать
второй execution turn.

#### `codex_list_pending_interactions`

Возвращает активные approvals/questions.

#### `codex_answer_pending_interaction`

Отвечает на approval/question.

#### `codex_interrupt_turn`

Прерывает активный turn по operation, workflow или thread/turn ids.

#### `codex_archive_thread`

Архивирует известный thread.

#### `codex_unarchive_thread`

Разархивирует известный thread.

#### `codex_start_thread_compaction`

Стартует асинхронную compaction и возвращает `actionId`.

#### `codex_get_thread_compaction_status`

Poll endpoint для compaction action.

#### `codex_get_runtime_capabilities`

Runtime inventory: models, permissions, sandbox, hooks, skills, provider
capabilities, account status, usage bands и rate limits.

#### `codex_preflight_project_run`

Проверяет, готов ли конкретный project run к старту. Используй перед
многочасовой автономной работой.

#### `codex_health_summary`

Легкая health summary для startup, reconnect, dashboard и restart decisions.

#### `codex_collect_diagnostics`

Собирает timeline, progress, hook history, issues и repair hints.

#### `codex_repair_issue`

Выполняет safe repair actions. Сначала всегда `dry_run=true`.

### Read and compatibility tools

#### `codex_list_projects`

Каталог проектов.

#### `codex_list_project_chats`

Список chats по проекту.

#### `codex_list_active_chats`

Активные chats и turns.

#### `codex_search_chats`

Поиск по chats, transcripts, hook history и summaries.

#### `codex_get_chat_status`

Компактный статус chat/thread.

#### `codex_get_chat`

Полная история chat/thread.

#### `codex_get_turn_status`

Статус конкретного turn, включая progress journal.

#### `codex_start_chat`

Compatibility layer. Для новых клиентов используй
`codex_submit_task(operation_type="start_chat")`.

#### `codex_send_message`

Compatibility layer. Для новых клиентов используй
`codex_submit_task(operation_type="send_message")`.

#### `codex_execute_plan`

Compatibility layer. С `workflow_id` делегирует в workflow path. Для новых
клиентов предпочитай `codex_approve_plan` или
`codex_submit_task(operation_type="execute_plan")`.

#### `codex_restart_app_server`

Restart app-server. Используй только после health/diagnostics и без active
turns, кроме явного forced режима.

#### `codex_get_app_server_status`

Статус MCP-owned app-server.

#### `codex_get_diagnostic_logs`

Raw audit surface. По умолчанию не проси payload.

#### `codex_analyze_issue`

Классификация MCP/app-server проблемы и рекомендации.

## Чеклист перед каждым write action

Перед `codex_submit_task`, `codex_start_plan_workflow`,
`codex_start_review_workflow` или lifecycle action:

- есть ли `client_request_id`, если возможен retry;
- выбран ли правильный `project_id`, `cwd`, `thread_id` или `workflow_id`;
- нет ли active turn в target thread;
- выбран безопасный `sandbox`;
- выбран правильный `approval_policy`;
- есть ли marker OpenClaw task id в prompt;
- если есть image input, путь внутри allowed roots;
- если есть `output_schema`, он strict и небольшой;
- если это repair, сначала `dry_run=true`.

## Чеклист после completion

После terminal status:

- если `status == "completed"`, сохранить `threadId`, `turnId`,
  `finalReport` или final message;
- если `failed`, `interrupted`, `orphaned` или `unknown_after_app_server_exit`,
  вызвать diagnostics;
- если `finalReport.readFullVia` задан, можно прочитать полный chat через
  `codex_get_chat`;
- если был workflow, сохранить `workflowKind`, `phase`, operation ids и report
  hash;
- если был pending interaction, убедиться, что он terminal;
- если hooks предупреждают о проблеме, показать warning, но не ломать уже
  завершенный результат.

## Что OpenClaw не должен делать

- Не держать write MCP call открытым до окончания Codex task.
- Не создавать второй turn, если `steer_turn` подходит лучше.
- Не retry-ить write request с новым `client_request_id` после transport
  timeout.
- Не считать `unknown_after_app_server_exit` успехом.
- Не парсить human-readable `message` как machine contract.
- Не читать внутреннюю SQLite Codex напрямую.
- Не публиковать raw diagnostics с payload пользователю без redaction.
- Не запускать forced restart при active turns без явного решения.
- Не использовать низкоуровневые write compatibility tools для новых сценариев.

## Минимальный state machine для OpenClaw

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

Для каждой итерации polling OpenClaw должен уважать `nextRecommendedAction`.
Если action неизвестен, безопасное поведение: подождать
`recommendedPollAfterSeconds`, затем poll again. Если action неизвестен и
status terminal, не продолжать polling бесконечно, собрать diagnostics.

## Быстрая проверка интеграции OpenClaw

1. `codex_health_summary`.
2. `codex_get_runtime_capabilities(refresh=true, include_account=false)`.
3. `codex_list_projects`.
4. Запустить safe prompt:

```text
MCP LIVE TEST / DO NOT MODIFY FILES / OpenClaw integration smoke
```

5. Poll `codex_get_operation_status` до terminal.
6. Проверить `threadId`, `turnId`, `finalReport` или latest message.
7. Вызвать `codex_collect_diagnostics` для созданного turn.
8. Проверить, что нет duplicate project из-за path casing.

Если эти шаги прошли, OpenClaw может считать MCP server готовым к обычной
работе.
