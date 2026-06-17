# Codex Control Plane MCP

[English](README.md) | Русский

<!-- mcp-name: io.github.aresyn/codex-control-plane-mcp -->

[![CI](https://github.com/aresyn/codex-control-plane-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/aresyn/codex-control-plane-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/codex-control-plane-mcp.svg)](https://pypi.org/project/codex-control-plane-mcp/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](docs/API_CONTRACT.md)

Надежное управление Codex Desktop для долгих задач.

`codex-control-plane-mcp` превращает Codex Desktop и `codex-app-server` в
durable worker, которым MCP-клиент может управлять без хрупкой синхронной
связки. Клиент отправляет задачу, сразу получает `operationId` или `workflowId`,
poll-ит статус, утверждает Plan Mode при необходимости и читает финальный отчет.

Сервер берет на себя то, что обычно ломает тонкие wrapper-проекты: запуск
app-server, создание threads и turns, retry safety, защиту от дублей, Plan Mode,
approvals, локальную историю, диагностику и repair.

OpenClaw и Hermes остаются основными сценариями, но сервер подходит любому
локальному orchestrator, которому нужно запускать долгие задачи Codex Desktop и
не держать MCP-вызов открытым часами.

## Коротко

```text
MCP client / orchestrator
  -> отправить задачу или стартовать Plan Mode workflow
  <- сразу получить operationId или workflowId
  -> poll статуса
  -> ответить на approvals или утвердить план
  <- прочитать final report, diagnostics, threadId и turnId
```

Контракт простой:

- без многочасовых MCP-вызовов;
- без дублирующих Codex turns после retry клиента;
- без слепого fire-and-forget submit;
- локальная SQLite-запись операций, workflows, turns, hooks и diagnostics.

## Почему не вызвать Codex напрямую?

| Возможность | Тонкий Codex wrapper | Codex Control Plane MCP |
|---|---:|---:|
| Многочасовые задачи | blocking / fragile | durable async operation |
| Восстановление после timeout клиента | вручную | retry-safe `client_request_id` |
| Защита от дублей turn | нет | active prompt detection |
| Plan Mode workflow | вручную через UI | pollable workflow state |
| Approvals и вопросы | blocking / opaque | pending interactions API |
| Восстановление после restart | ad hoc | persisted operation state |
| Диагностика | только logs | health, diagnostics, repair tools |

## Текущая поддержка

- Полный live target: Windows с Codex Desktop и `codex-app-server`.
- Linux и macOS: пока только protocol-only проверки.
- Local-first: сервер не предназначен для публикации как открытый network service.

## Модель безопасности

Это local-first control plane для доверенных окружений Codex Desktop.

Не выставляйте его в сеть без authentication.

Рекомендуемая позиция для первого запуска:

- используйте `read-only` для недоверенных репозиториев;
- используйте `on-request` approval при проверке новых workflows;
- держите `state/`, `logs/`, `.env` и `.codex/` приватными.

## Что умеет сервер

- Durable async queue для write-операций Codex.
- Retry-safe обработка `client_request_id`.
- Поиск активных дублей prompt.
- SQLite leases и heartbeats для конкурирующих MCP-процессов.
- Восстановление после рестарта MCP во время `thread/start` или `turn/start`.
- Durable `turn/steer`, чтобы добавить контекст в активный turn без второго turn.
- Plan Mode workflows: план, polling, approve, execution, финальный отчет.
- Pending approvals и вопросы как pollable MCP state.
- Interrupt turns по `threadId`/`turnId`, `operationId` или `workflowId`.
- Health checks, diagnostics, issue analysis и dry-run repairs.
- Собственная hook history в SQLite для поиска, summaries и fallback reads.
- Structured MCP errors, с которыми automation code может работать напрямую.

Write/control действия идут через `codex-app-server`. Сервер не пишет во
внутренние SQLite-базы Codex и не меняет transcript-файлы Codex.

## Установка

Рекомендуемый вариант:

```powershell
pipx install codex-control-plane-mcp
```

Или запуск без установки:

```powershell
uvx codex-control-plane-mcp
```

Из GitHub:

```powershell
python -m pip install "codex-control-plane-mcp @ git+https://github.com/aresyn/codex-control-plane-mcp.git"
```

Для локальной разработки:

```powershell
git clone https://github.com/aresyn/codex-control-plane-mcp.git
cd codex-control-plane-mcp
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

## MCP client config

После установки можно сгенерировать config:

```powershell
codex-control-plane-mcp-admin init --state-db .\state\codex-mcp-state.sqlite3 --projects-root C:\Users\you\Projects
```

Минимальная stdio-запись:

```json
{
  "mcpServers": {
    "codex-control-plane": {
      "command": "codex-control-plane-mcp",
      "args": []
    }
  }
}
```

Запуск MCP stdio server:

```powershell
codex-control-plane-mcp
```

Или как Python module:

```powershell
py -m codex_control_plane_mcp.server
```

Старые команды `openclaw-codex-mcp` и `openclaw-codex-mcp-hooks` остаются
compatibility aliases на одну release line.

## Первый setup

Admin helper может напечатать более полный MCP client config, поставить hooks и
запустить protocol smoke:

```powershell
codex-control-plane-mcp-admin init --state-db .\state\codex-mcp-state.sqlite3 --projects-root C:\Users\you\Projects
```

Команда выводит JSON-блок для MCP client config. Секреты и приватные prompts она
не печатает.

Можно поставить только Codex hooks:

```powershell
codex-control-plane-mcp-hooks install --state-db .\state\codex-mcp-state.sqlite3
codex-control-plane-mcp-hooks status
codex-control-plane-mcp-hooks doctor
```

Installer делает backup `~/.codex/hooks.json`, мержит свои handlers с уже
существующими hooks, сохраняет `stateDb` абсолютным путем и пишет prompts,
видимый progress text агента, финальные ответы и turn status в MCP state DB.
Tool calls и command outputs по умолчанию не записываются. После установки или
изменения hooks перезапустите Codex.

Для turns, запущенных через `codex-app-server`, сервер дополнительно зеркалирует
принятый prompt, видимые assistant messages и turn status в ту же SQLite history.
Так search и status reads остаются полезными даже тогда, когда app-server сам не
исполняет пользовательские hooks.

## Основные workflows

Durable submit задачи:

```text
codex_submit_task
  -> operationId
codex_get_operation_status(operationId)
  -> queued / running / waiting_for_approval / completed / failed
```

Используйте тот же `client_request_id`, когда клиент повторяет запрос после
transport timeout. Retry вернет существующую operation, а не создаст еще один
turn.

Steer активного turn:

```text
codex_submit_task(operation_type="steer_turn", thread_id=..., expected_turn_id=..., message=...)
  -> operationId
codex_get_operation_status(operationId)
  -> следует за target turn до completed / failed / interrupted
```

Используйте `steer_turn` только пока target turn активен. Для завершенного
thread нужен обычный `send_message`.

Plan Mode:

```text
codex_start_plan_workflow
  -> workflowId
codex_get_workflow_status(workflowId)
  -> wait_plan / review_plan / execute_plan
codex_approve_plan(workflowId)
  -> executionOperationId
codex_get_workflow_status(workflowId)
  -> finalReport
```

Approvals и вопросы:

```text
codex_list_pending_interactions
codex_answer_pending_interaction
```

Диагностика начинается с:

```text
codex_health_summary
codex_collect_diagnostics
codex_analyze_issue
codex_repair_issue
```

Repair actions по умолчанию идут с `dry_run=true`.

## Tool surface

Stable orchestration tools:

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

Compatibility и read tools:

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

Новым клиентам лучше использовать durable operations и workflows.
Низкоуровневые write tools остаются для compatibility.

Схемы, формат ошибок, stable tool groups и правила версионирования описаны в
[docs/API_CONTRACT.md](docs/API_CONTRACT.md).

## Result contract

Каждый tool объявляет `outputSchema` и возвращает MCP `structuredContent`.

Успех:

```json
{"ok": true}
```

Domain или tool error:

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

Вызывайте `codex_health_summary` при старте и reconnect. В блоке `version` есть
`serverName`, `serverVersion`, `contractVersion`, `toolSurfaceHash` и списки
stable/compatibility tools.

## Конфигурация

Конфигурация берется из environment variables или JSON-файла, указанного в
`CODEX_CONTROL_PLANE_MCP_CONFIG`. Старое имя `OPENCLAW_CODEX_MCP_CONFIG`
сохраняется как fallback.

Основные переменные:

- `CODEX_HOME`: домашний каталог Codex. По умолчанию `%USERPROFILE%\.codex`.
- `CODEX_PROJECTS_ROOT`: корень проектов для catalog/read tools.
- `CODEX_ALLOWED_ROOTS`: allowlist путей через `;`.
- `CODEX_PROJECTS_REGISTRY`: опциональный JSON registry проектов.
- `CODEX_MCP_STATE_DB`: локальная MCP state DB.
- `CODEX_CONTROL_PLANE_MCP_LOG`: путь к log file.
- `CODEX_MCP_HOOK_HISTORY_ENABLED`: включает SQLite hook history. По умолчанию `true`.
- `CODEX_MCP_HOOK_HISTORY_MAX_TEXT_CHARS`: лимит записи одного hook-сообщения.
- `CODEX_KB_HISTORY_PROJECTS_ROOT`: опциональный legacy-корень нормализованной KB history.
- `CODEX_BINARY_PATH`: явный путь к Codex binary.
- `CODEX_MCP_DEFAULT_SANDBOX`: default sandbox для write-операций. По умолчанию `danger-full-access`.
- `CODEX_MCP_DEFAULT_APPROVAL_POLICY`: default approval policy для write-операций. По умолчанию `never`.
- `CODEX_MCP_DEFAULT_MODEL`: default Codex model для app-server.
- `CODEX_MCP_DEFAULT_EFFORT`: default effort level.
- `CODEX_MCP_APPROVAL_RESPONSE_TIMEOUT_SECONDS`: timeout pending interactions.
- `DEEPSEEK_ENV_PATH`: опциональный `.env` для DeepSeek summary settings.
- `DEEPSEEK_SUMMARY_ENABLED`: включает или отключает remote summary calls.

Write policy значения являются дефолтами, а не жесткими ограничениями.
MCP-клиент может переопределить `sandbox` или `approval_policy` в конкретном
вызове, например для разовой задачи в `read-only` или `on-request`.

Пример:

```powershell
$env:CODEX_CONTROL_PLANE_MCP_CONFIG = Join-Path (Get-Location) "examples\codex-control-plane-mcp.config.json"
$env:CODEX_MCP_DEFAULT_SANDBOX = "danger-full-access"
$env:CODEX_MCP_DEFAULT_APPROVAL_POLICY = "never"
py -m codex_control_plane_mcp.server
```

См. [examples/codex-control-plane-mcp.config.json](examples/codex-control-plane-mcp.config.json).

## Модель надежности

Сервер рассчитан на обычные сбои локальной оркестрации:

- MCP client timeout после submit задачи.
- Повторный submit с тем же `client_request_id`.
- Повторный submit без idempotency key, но с тем же активным prompt.
- Рестарт MCP между app-server `thread/start` и `turn/start`.
- Два MCP-процесса используют одну SQLite state DB.
- App-server завершился, пока turn был активен.
- Pending approval привязан к старой app-server generation.
- App-server или transcript временно недоступны, но hook history уже сохранила
  prompt, видимый текст агента, финальный ответ и completion status.

Эти случаи хранятся в durable state операций, workflows, turns, hooks и pending
interactions. Terminal statuses явные. `unknown_after_app_server_exit` не
считается успехом.

## Безопасность

- Live smoke prompts должны содержать `MCP LIVE TEST / DO NOT MODIFY FILES`.
- Repairs по умолчанию идут с `dry_run=true`.
- Forced app-server restart может пометить активные turns как unknown или
  orphaned. Предпочитайте `restart_app_server_idle`.

## Проверки

Быстрые локальные проверки:

```powershell
python -m pytest -q
python -m compileall -q openclaw_codex_mcp codex_control_plane_mcp tests scripts
git diff --check
```

Protocol-only MCP smoke:

```powershell
python .\scripts\mcp_live_smoke.py --scenario protocol
```

Safe live smoke с реальным Codex Desktop/app-server:

```powershell
python .\scripts\mcp_live_smoke.py --scenario safe-operation --cwd <PROJECT_ROOT>
```

Full live regression:

```powershell
python .\scripts\mcp_live_smoke.py --scenario full --safe-restart --cwd <PROJECT_ROOT>
```

См. [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md). Позиционирование для
публичного запуска описано в [docs/PUBLICATION_GUIDE.md](docs/PUBLICATION_GUIDE.md).

## Packaging

Локальная сборка:

```powershell
python -m pip install build
python -m build
```

Wheel включает MCP server, hook installer, admin helper и bundled Codex hook
module.

Обычный install path:

```powershell
pipx install codex-control-plane-mcp
```

или:

```powershell
uvx codex-control-plane-mcp
```

## Contributing

Перед issues с diagnostics прочитайте [CONTRIBUTING.md](CONTRIBUTING.md) и
[SECURITY.md](SECURITY.md).

Хорошие GitHub topics для repo:

`python`, `mcp`, `mcp-server`, `model-context-protocol`, `openai-codex`,
`codex`, `codex-desktop`, `agent-tools`, `ai-agents`, `developer-tools`,
`automation`, `orchestration`, `agentic-workflows`, `long-running-tasks`,
`openclaw`, `hermes`, `hermes-agent`.
