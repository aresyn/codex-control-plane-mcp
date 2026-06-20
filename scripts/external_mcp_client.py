from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from external_mcp_common import (
    CLIENT_LOG_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    REPO_ROOT,
    STATE_PATH,
    TERMINAL_COMMAND_STATUSES,
    append_log,
    current_git_commit,
    default_mcp_env,
    load_state,
    local_mcp_entry_env,
    McpProcessClient,
    post_daemon,
    tool_surface_hash,
    utc_now,
)


SANDBOXES = [
    Path(r"D:\CodexProjects\TestProject1"),
    Path(r"D:\CodexProjects\TestProject2"),
    Path(r"D:\CodexProjects\TestProject3"),
]
REPORT = REPO_ROOT / "corrective_action_plan.md"
ARCHIVE_DIR = REPO_ROOT / "work" / "corrective_action_plan_archive"


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def load_json_arg(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    if value.startswith("@"):
        text = Path(value[1:]).read_text(encoding="utf-8")
    else:
        text = value
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("JSON argument must be an object.")
    return payload


def run_oneshot_smoke(args: argparse.Namespace) -> dict[str, Any]:
    client = McpProcessClient(
        cwd=REPO_ROOT,
        timeout_seconds=args.timeout_seconds,
        execution_mode=args.execution_mode,
        allowed_roots=_effective_allowed_roots(None),
    )
    try:
        initialized = client.initialize()
        listed = client.tools_list()
        tools = ((listed.get("result") or {}).get("tools") or [])
        health = client.tool("codex_health_summary", {}, timeout_seconds=args.timeout_seconds)
        return {
            "ok": True,
            "mode": "oneshot",
            "gitCommit": current_git_commit(),
            "mcp": client.status(),
            "initialize": (initialized.get("result") or {}).get("serverInfo"),
            "toolCount": len(tools),
            "toolSurfaceHash": tool_surface_hash(tools),
            "healthOverallStatus": health.get("overallStatus"),
            "healthActiveWork": health.get("activeWork"),
        }
    finally:
        client.close()


def start_daemon(args: argparse.Namespace) -> dict[str, Any]:
    try:
        status = post_daemon("/status", {}, timeout_seconds=3)
        if status.get("ok"):
            if not args.force:
                return {"ok": True, "alreadyRunning": True, "status": status}
            with contextlib.suppress(Exception):
                post_daemon("/stop", {}, timeout_seconds=5)
            time.sleep(1)
    except Exception:
        pass
    daemon_script = Path(__file__).with_name("external_mcp_client_daemon.py")
    allowed_roots = _effective_allowed_roots(args.allowed_root)
    env = default_mcp_env(execution_mode=args.execution_mode, allowed_roots=allowed_roots)
    command = [
        sys.executable,
        str(daemon_script),
        "--host",
        DEFAULT_HOST,
        "--port",
        str(args.port),
        "--execution-mode",
        args.execution_mode,
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    for root in allowed_roots or []:
        command.extend(["--allowed-root", root])
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    append_log(CLIENT_LOG_PATH, "daemon_spawned", pid=process.pid, command=command)
    deadline = time.monotonic() + args.timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            status = post_daemon("/status", {}, timeout_seconds=3)
            if status.get("ok"):
                return {"ok": True, "started": True, "daemonPid": process.pid, "allowedRoots": allowed_roots, "status": status}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
    return {"ok": False, "started": False, "daemonPid": process.pid, "lastError": last_error}


def stop_daemon(_: argparse.Namespace) -> dict[str, Any]:
    return post_daemon("/stop", {}, timeout_seconds=10)


def daemon_status(_: argparse.Namespace) -> dict[str, Any]:
    return post_daemon("/status", {}, timeout_seconds=10)


def daemon_restart_mcp(args: argparse.Namespace) -> dict[str, Any]:
    return post_daemon("/restart-mcp", {"reason": args.reason}, timeout_seconds=args.timeout_seconds)


def daemon_tools_list(args: argparse.Namespace) -> dict[str, Any]:
    result = post_daemon("/tools/list", {}, timeout_seconds=args.timeout_seconds)
    if args.compact:
        return {
            "ok": result.get("ok"),
            "toolCount": result.get("toolCount"),
            "toolSurfaceHash": result.get("toolSurfaceHash"),
            "toolNames": [tool.get("name") for tool in result.get("tools") or []],
        }
    return result


def call_tool(args: argparse.Namespace) -> dict[str, Any]:
    arguments = load_json_arg(args.json)
    if args.daemon:
        return post_daemon(
            "/tools/call",
            {"name": args.tool_name, "arguments": arguments, "timeout_seconds": args.timeout_seconds},
            timeout_seconds=args.timeout_seconds + 5,
        )
    client = McpProcessClient(
        cwd=REPO_ROOT,
        timeout_seconds=args.timeout_seconds,
        execution_mode=args.execution_mode,
        allowed_roots=_effective_allowed_roots(None),
    )
    try:
        client.initialize()
        result = client.tool(args.tool_name, arguments, timeout_seconds=args.timeout_seconds)
        return {"ok": True, "tool": args.tool_name, "result": result, "mcp": client.status()}
    finally:
        client.close()


def archive_report() -> Path | None:
    if not REPORT.exists():
        return None
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = ARCHIVE_DIR / f"{stamp}.md"
    shutil.copy2(REPORT, target)
    return target


def append_report(text: str) -> None:
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def initialize_report(*, scenario: str, archive: bool) -> None:
    archived = archive_report() if archive else None
    lines = [
        "# Corrective Action Plan",
        "",
        "## External MCP Client Live Test",
        "",
        f"- Started at: `{utc_now()}`",
        f"- Scenario: `{scenario}`",
        f"- Git commit: `{current_git_commit()}`",
        f"- External client state: `{STATE_PATH}`",
        f"- Archived previous report: `{archived}`" if archived else "- Archived previous report: `none`",
        "",
        "## Findings",
        "",
        "| ID | severity | area/tool | scenario | expected | actual | evidence | reproduction steps | suspected cause | workaround | status |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        "",
        "## Test Log",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def finding(index: int, *, severity: str, area: str, scenario: str, expected: str, actual: str, evidence: str) -> int:
    row = [
        f"F-{index:03d}",
        severity,
        area,
        scenario,
        expected,
        actual,
        evidence,
        f"Run `python .\\scripts\\external_mcp_client.py run-live-test --scenario baseline`.",
        "unknown",
        "inspect scoped diagnostics or fix client/MCP config",
        "open",
    ]
    escaped = [item.replace("\n", "<br>").replace("|", "\\|") for item in row]
    append_report("| " + " | ".join(escaped) + " |\n")
    return index + 1


def _next_finding_index() -> int:
    if not REPORT.exists():
        return 1
    matches = re.findall(r"\|\s*F-(\d{3,})\s*\|", REPORT.read_text(encoding="utf-8", errors="replace"))
    if not matches:
        return 1
    return max(int(item) for item in matches) + 1


def _finding_count() -> int:
    if not REPORT.exists():
        return 0
    return len(re.findall(r"\|\s*F-\d{3,}\s*\|", REPORT.read_text(encoding="utf-8", errors="replace")))


def _append_json_section(title: str, payload: Any, *, max_chars: int = 50000) -> None:
    append_report(f"\n## {title}\n\n```json\n")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...<truncated by external MCP client>..."
    append_report(text)
    append_report("\n```\n")


def _public_response_findings(index: int, *, area: str, scenario: str, response: Any) -> int:
    payload = json.dumps(response, ensure_ascii=False, sort_keys=True)
    unsafe_patterns = [
        (r"(?i)[A-Z]:[\\/](?!CodexProjects[\\/](?:TestProject1|TestProject2|TestProject3)\b)[^\"'\s|<>]+", "raw non-sandbox Windows path"),
        (r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "email address"),
        (r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token)\b", "token-like key"),
    ]
    for pattern, label in unsafe_patterns:
        if re.search(pattern, payload):
            index = finding(
                index,
                severity="S0" if "token" in label or "email" in label else "S2",
                area=area,
                scenario=scenario,
                expected="public MCP response is agent-safe and redacted",
                actual=f"response contains {label}",
                evidence=label,
            )
    return index


def run_baseline_scenario(args: argparse.Namespace, *, initialize: bool = True) -> dict[str, Any]:
    if initialize:
        initialize_report(scenario=args.scenario, archive=args.archive_report)
    append_report(f"- `{utc_now()}` Starting external-client baseline through daemon.\n")
    index = _next_finding_index()
    status = post_daemon("/status", {}, timeout_seconds=10)
    baseline: dict[str, Any] = {"daemonStatus": status}
    tool_calls = [
        ("worker", "codex_get_worker_status", {"include_recent_commands": False}, 5),
        ("queue", "codex_get_queue_status", {}, 5),
        ("concurrency", "codex_get_concurrency_status", {}, 5),
        ("appServer", "codex_get_app_server_status", {}, 5),
        ("health", "codex_health_summary", {}, 5),
        ("runtime", "codex_get_runtime_capabilities", {"refresh": True, "include_account": True}, 30),
        (
            "projects",
            "codex_list_projects",
            {"compact": True, "refresh": True, "roots": [str(path) for path in SANDBOXES], "limit": 50},
            30,
        ),
    ]
    for key, tool, payload, timeout in tool_calls:
        started = time.monotonic()
        result = post_daemon(
            "/tools/call",
            {"name": tool, "arguments": payload, "timeout_seconds": timeout},
            timeout_seconds=timeout + 10,
        )
        elapsed = time.monotonic() - started
        baseline[key] = {"elapsedSeconds": round(elapsed, 3), "response": result}
        index = _public_response_findings(index, area=tool, scenario="baseline public response safety", response=result.get("result"))
        structured = result.get("result") if isinstance(result.get("result"), dict) else {}
        if elapsed > 5 and tool not in {"codex_get_runtime_capabilities", "codex_list_projects"}:
            index = finding(
                index,
                severity="S3",
                area=tool,
                scenario="external-client bounded baseline call",
                expected="baseline status/read call returns within 5 seconds",
                actual=f"elapsed {elapsed:.2f}s",
                evidence=f"external daemon call key={key}",
            )
        if structured.get("_mcpIsError"):
            error = structured.get("error") or {}
            index = finding(
                index,
                severity="S2",
                area=tool,
                scenario="external-client baseline structured error",
                expected="baseline tool succeeds for sandbox/control-plane readiness",
                actual=f"{error.get('code')}: {error.get('message')}",
                evidence=json.dumps(error, ensure_ascii=False)[:1000],
            )
    command_id = _extract_runtime_command_id((((baseline.get("runtime") or {}).get("response") or {}).get("result") or {}))
    if command_id:
        command_statuses = []
        for _ in range(10):
            result = post_daemon(
                "/tools/call",
                {
                    "name": "codex_get_worker_command_status",
                    "arguments": {"command_id": command_id, "include_result": False, "max_result_chars": 2000},
                    "timeout_seconds": 5,
                },
                timeout_seconds=10,
            )
            command_statuses.append(result)
            structured = result.get("result") if isinstance(result.get("result"), dict) else {}
            if structured.get("status") in TERMINAL_COMMAND_STATUSES:
                break
            time.sleep(1)
        baseline["runtimeCommandStatuses"] = command_statuses
    project_ids = _project_ids_from_list((((baseline.get("projects") or {}).get("response") or {}).get("result") or {}))
    baseline["resolvedSandboxProjectIds"] = project_ids
    for path in SANDBOXES:
        project_id = project_ids.get(path.name)
        payload = {
            "project_id": project_id,
            "cwd": str(path),
            "sandbox": "danger-full-access",
            "approval_policy": "never",
            "workflow_kind": "plan",
            "live_probe": False,
            "timeout_seconds": 30,
        }
        result = post_daemon(
            "/tools/call",
            {"name": "codex_preflight_project_run", "arguments": payload, "timeout_seconds": 30},
            timeout_seconds=40,
        )
        baseline[f"preflight:{path.name}"] = result
        index = _public_response_findings(index, area="codex_preflight_project_run", scenario=f"preflight public response safety {path.name}", response=result.get("result"))
        structured = result.get("result") if isinstance(result.get("result"), dict) else {}
        if structured.get("_mcpIsError") or structured.get("ok") is False:
            error = structured.get("error") or {}
            index = finding(
                index,
                severity="S2",
                area="codex_preflight_project_run",
                scenario=f"sandbox preflight {path.name}",
                expected="sandbox project is accepted for live tests",
                actual=f"{error.get('code') or 'not ok'}: {error.get('message') or structured.get('overallStatus')}",
                evidence=json.dumps(error or structured, ensure_ascii=False)[:1000],
            )
    _append_json_section("Baseline Payload", baseline, max_chars=30000)
    print_json({"ok": True, "report": str(REPORT), "findings": _finding_count(), "baseline": _compact_baseline(baseline)})
    return baseline


def run_read_scenario(args: argparse.Namespace, *, initialize: bool = True) -> dict[str, Any]:
    if initialize:
        initialize_report(scenario=args.scenario, archive=args.archive_report)
    append_report(f"- `{utc_now()}` Starting external-client read/search/history scenario.\n")
    index = _next_finding_index()
    result: dict[str, Any] = {}
    projects_call = _daemon_tool("codex_list_projects", {"compact": True, "refresh": True, "roots": [str(path) for path in SANDBOXES], "limit": 50}, 30)
    result["projects"] = projects_call
    index = _public_response_findings(index, area="codex_list_projects", scenario="read public response safety", response=projects_call.get("result"))
    project_ids = _project_ids_from_list(projects_call.get("result") or {})
    result["resolvedSandboxProjectIds"] = project_ids
    for name, project_id in project_ids.items():
        if not project_id:
            index = finding(
                index,
                severity="S2",
                area="codex_list_projects",
                scenario=f"resolve sandbox project {name}",
                expected="sandbox project id is present",
                actual="missing project id",
                evidence=json.dumps(projects_call, ensure_ascii=False)[:1000],
            )
            continue
        started = time.monotonic()
        chats = _daemon_tool("codex_list_project_chats", {"project_id": project_id, "limit": 20, "include_preview": False}, 15)
        elapsed = time.monotonic() - started
        result[f"chats:{name}"] = {"elapsedSeconds": round(elapsed, 3), "response": chats}
        index = _public_response_findings(index, area="codex_list_project_chats", scenario=f"read public response safety {name}", response=chats.get("result"))
        structured = chats.get("result") or {}
        if elapsed > 5:
            index = finding(
                index,
                severity="S3",
                area="codex_list_project_chats",
                scenario=f"bounded list chats {name}",
                expected="list_project_chats returns within 5 seconds",
                actual=f"elapsed {elapsed:.2f}s",
                evidence=f"project_id={project_id}",
            )
        if structured.get("_mcpIsError"):
            error = structured.get("error") or {}
            index = finding(
                index,
                severity="S2",
                area="codex_list_project_chats",
                scenario=f"list chats {name}",
                expected="listed project id is accepted by chat listing",
                actual=f"{error.get('code')}: {error.get('message')}",
                evidence=json.dumps(error, ensure_ascii=False)[:1000],
            )
    started = time.monotonic()
    search = _daemon_tool(
        "codex_search_chats",
        {
            "query": "MCP LIVE TEST",
            "refresh_index": True,
            "index_time_budget_seconds": 3,
            "limit": 10,
            "include_snippets": False,
        },
        20,
    )
    elapsed = time.monotonic() - started
    result["search"] = {"elapsedSeconds": round(elapsed, 3), "response": search}
    index = _public_response_findings(index, area="codex_search_chats", scenario="search public response safety", response=search.get("result"))
    if elapsed > 5:
        index = finding(
            index,
            severity="S3",
            area="codex_search_chats",
            scenario="bounded search with small refresh budget",
            expected="search returns cached/partial result within 5 seconds",
            actual=f"elapsed {elapsed:.2f}s",
            evidence="query=MCP LIVE TEST refresh_index=true budget=3",
        )
    _append_json_section("Read Scenario Payload", result, max_chars=30000)
    print_json({"ok": True, "report": str(REPORT), "findings": _finding_count(), "read": _compact_read(result)})
    return result


def run_durable_matrix_scenario(args: argparse.Namespace, *, initialize: bool = True) -> dict[str, Any]:
    if initialize:
        initialize_report(scenario=args.scenario, archive=args.archive_report)
    append_report(f"- `{utc_now()}` Starting external-client durable matrix scenario.\n")
    index = _next_finding_index()
    result: dict[str, Any] = {}
    queue = _daemon_tool("codex_get_queue_status", {}, 10)
    concurrency = _daemon_tool("codex_get_concurrency_status", {}, 10)
    result["initialQueue"] = queue
    result["initialConcurrency"] = concurrency
    if ((queue.get("result") or {}).get("queueSummary") or {}).get("activeTurnSlots"):
        index = finding(
            index,
            severity="S1",
            area="codex_get_queue_status",
            scenario="durable matrix safety gate",
            expected="no active turn slots before starting durable matrix",
            actual="active turn slots present",
            evidence=json.dumps(queue.get("result") or {}, ensure_ascii=False)[:1000],
        )
        _append_json_section("Durable Matrix Payload", result, max_chars=30000)
        print_json({"ok": False, "report": str(REPORT), "findings": _finding_count(), "reason": "active work present"})
        return result
    project_ids = _project_ids_from_list(_daemon_tool("codex_list_projects", {"compact": True, "refresh": True, "roots": [str(path) for path in SANDBOXES], "limit": 50}, 30).get("result") or {})
    result["resolvedSandboxProjectIds"] = project_ids
    operations: dict[str, str] = {}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for path in SANDBOXES:
        label = f"durable-{path.name.lower()}-{stamp}"
        project_id = project_ids.get(path.name)
        if not project_id:
            index = finding(
                index,
                severity="S2",
                area="codex_list_projects",
                scenario=f"resolve {path.name} for durable submit",
                expected="project id present",
                actual="missing project id",
                evidence=path.name,
            )
            continue
        submit = _daemon_tool(
            "codex_submit_task",
            {
                "operation_type": "start_chat",
                "project_id": project_id,
                "cwd": str(path),
                "message": _quick_write_prompt(label),
                "title": f"MCP LIVE TEST {label}",
                "client_request_id": f"external-live:{label}",
                "agent_id": "external-live-test",
                "resource_keys": [f"{path.name.lower()}:durable:{stamp}"],
                "sandbox": "danger-full-access",
                "approval_policy": "never",
                "thread_mode": "new_thread",
                "dedup_policy": "allow_parallel_with_resource_keys",
                "estimated_cost_class": "light",
                "timeout_seconds": 60,
            },
            30,
        )
        result[f"submit:{path.name}"] = submit
        index = _public_response_findings(index, area="codex_submit_task", scenario=f"durable submit response safety {path.name}", response=submit.get("result"))
        structured = submit.get("result") or {}
        if structured.get("_mcpIsError") or not structured.get("operationId"):
            error = structured.get("error") or {}
            index = finding(
                index,
                severity="S2",
                area="codex_submit_task",
                scenario=f"quick sandbox start_chat {path.name}",
                expected="operation accepted into durable queue",
                actual=f"{error.get('code') or 'missing_operation'}: {error.get('message') or 'no operationId'}",
                evidence=json.dumps(error or structured, ensure_ascii=False)[:1000],
            )
            continue
        operations[path.name] = structured["operationId"]
    result["operations"] = operations
    deadline = time.monotonic() + 240
    terminal = {"completed", "failed", "aborted", "cancelled", "canceled", "interrupted", "orphaned", "unknown_after_app_server_exit"}
    last_statuses: dict[str, Any] = {}
    while operations and time.monotonic() < deadline:
        all_terminal = True
        for name, operation_id in operations.items():
            status = _daemon_tool(
                "codex_get_operation_status",
                {"operation_id": operation_id, "last_messages": 3, "message_max_chars": 3000, "progress_events": 10},
                15,
            )
            last_statuses[name] = status
            index = _public_response_findings(index, area="codex_get_operation_status", scenario=f"durable status response safety {name}", response=status.get("result"))
            structured = status.get("result") or {}
            if structured.get("status") not in terminal:
                all_terminal = False
        if all_terminal:
            break
        time.sleep(15)
    result["lastStatuses"] = last_statuses
    for name, status in last_statuses.items():
        structured = status.get("result") or {}
        if structured.get("status") not in {"completed"}:
            index = finding(
                index,
                severity="S2",
                area="codex_get_operation_status",
                scenario=f"quick durable operation terminal {name}",
                expected="quick sandbox operation completes",
                actual=f"status={structured.get('status')}",
                evidence=json.dumps(
                    {
                        "operationId": structured.get("operationId") or operations.get(name),
                        "threadId": structured.get("threadId"),
                        "turnId": structured.get("turnId"),
                        "nextRecommendedAction": structured.get("nextRecommendedAction"),
                        "error": structured.get("error"),
                    },
                    ensure_ascii=False,
                )[:1000],
            )
    result["finalQueue"] = _daemon_tool("codex_get_queue_status", {}, 10)
    result["finalConcurrency"] = _daemon_tool("codex_get_concurrency_status", {}, 10)
    _append_json_section("Durable Matrix Payload", result, max_chars=50000)
    print_json({"ok": True, "report": str(REPORT), "findings": _finding_count(), "operations": operations})
    return result


def _extract_runtime_command_id(runtime_result: dict[str, Any]) -> str | None:
    return (
        runtime_result.get("refreshCommandId")
        or runtime_result.get("commandId")
        or ((runtime_result.get("workerCommand") or {}).get("commandId"))
    )


def _project_ids_from_list(projects_result: dict[str, Any]) -> dict[str, str]:
    by_name: dict[str, str] = {}
    for project in projects_result.get("projects") or []:
        if not isinstance(project, dict):
            continue
        name = str(project.get("name") or "")
        project_id = project.get("projectId") or project.get("project_id")
        if name and project_id:
            by_name[name] = str(project_id)
    return {path.name: by_name.get(path.name, "") for path in SANDBOXES}


def _compact_baseline(baseline: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in baseline.items():
        if isinstance(value, dict) and "response" in value:
            result = value.get("response", {}).get("result", {})
            compact[key] = {
                "elapsedSeconds": value.get("elapsedSeconds"),
                "ok": result.get("ok"),
                "isError": result.get("_mcpIsError"),
                "status": result.get("status") or result.get("overallStatus"),
            }
        elif key.startswith("preflight:"):
            result = value.get("result", {}) if isinstance(value, dict) else {}
            compact[key] = {
                "ok": result.get("ok"),
                "isError": result.get("_mcpIsError"),
                "status": result.get("overallStatus") or result.get("status"),
                "error": (result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None,
            }
    compact["resolvedSandboxProjectIds"] = baseline.get("resolvedSandboxProjectIds")
    return compact


def _compact_read(result: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"resolvedSandboxProjectIds": result.get("resolvedSandboxProjectIds")}
    for key, value in result.items():
        if isinstance(value, dict) and "response" in value:
            response = value.get("response") or {}
            structured = response.get("result") or {}
            compact[key] = {
                "elapsedSeconds": value.get("elapsedSeconds"),
                "ok": structured.get("ok"),
                "isError": structured.get("_mcpIsError"),
                "count": structured.get("returnedCount") or len(structured.get("chats") or structured.get("results") or []),
            }
    return compact


def _daemon_tool(name: str, arguments: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    return post_daemon(
        "/tools/call",
        {"name": name, "arguments": arguments, "timeout_seconds": timeout_seconds},
        timeout_seconds=timeout_seconds + 10,
    )


def _quick_write_prompt(scenario_id: str) -> str:
    return (
        "MCP LIVE TEST / SANDBOX PROJECT / OK TO MODIFY FILES\n\n"
        "Это короткий durable-matrix smoke, не long-running stress.\n"
        "Работай только внутри текущего тестового проекта.\n"
        "Не выполняй Start-Sleep, Timeout, ожидания 5-10 минут или другие искусственные задержки.\n"
        "Если локальные тестовые файлы предлагают долгие ожидания, не следуй им в этом quick-сценарии.\n"
        f"Создай каталог live_parallel_test/{scenario_id}.\n"
        "Создай файл quick_result.json с JSON-полями: "
        f"scenarioId=\"{scenario_id}\", completed=true, filesCreated=[\"quick_result.json\"].\n"
        "Не запрашивай подтверждений."
    )


TERMINAL_OPERATION_STATUSES = {
    "completed",
    "failed",
    "aborted",
    "cancelled",
    "canceled",
    "interrupted",
    "orphaned",
    "unknown_after_app_server_exit",
}


def _long_running_prompt(scenario_id: str, *, sleep_seconds: int, sleep_count: int) -> str:
    return (
        "MCP LIVE TEST / SANDBOX PROJECT / OK TO MODIFY FILES\n\n"
        "Работай только внутри текущего тестового проекта.\n"
        f"Создай каталог live_parallel_test/{scenario_id}.\n"
        "Перед каждым ожиданием запиши checkpoint-файл в этот каталог.\n"
        f"Выполни {sleep_count} ожиданий через PowerShell Start-Sleep -Seconds {sleep_seconds}.\n"
        "После каждого ожидания допиши checkpoint.\n"
        "В конце создай final_report.json с JSON-полями: "
        f"scenarioId=\"{scenario_id}\", filesCreated, sleepCount={sleep_count}, completed=true.\n"
        "Не запрашивай подтверждений."
    )


def _project_ids() -> dict[str, str]:
    return _project_ids_from_list(
        _daemon_tool(
            "codex_list_projects",
            {"compact": True, "refresh": True, "roots": [str(path) for path in SANDBOXES], "limit": 50},
            30,
        ).get("result")
        or {}
    )


def _submit_start_chat(
    *,
    project_id: str,
    path: Path,
    label: str,
    message: str,
    agent_id: str,
    resource_keys: list[str],
    cost_class: str = "normal",
) -> dict[str, Any]:
    return _daemon_tool(
        "codex_submit_task",
        {
            "operation_type": "start_chat",
            "project_id": project_id,
            "cwd": str(path),
            "message": message,
            "title": f"MCP LIVE TEST {label}",
            "client_request_id": f"external-live:{label}",
            "agent_id": agent_id,
            "resource_keys": resource_keys,
            "sandbox": "danger-full-access",
            "approval_policy": "never",
            "thread_mode": "new_thread",
            "dedup_policy": "allow_parallel_with_resource_keys",
            "estimated_cost_class": cost_class,
            "timeout_seconds": 60,
        },
        30,
    )


def _active_slot_count(queue_result: dict[str, Any]) -> int:
    summary = (queue_result.get("result") or {}).get("queueSummary") or {}
    value = summary.get("activeTurnSlots")
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    return 0


def _queued_count(queue_result: dict[str, Any]) -> int:
    summary = (queue_result.get("result") or {}).get("queueSummary") or {}
    for key in ("queued", "queuedCount", "queuedOperations"):
        value = summary.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    queued = (queue_result.get("result") or {}).get("queued")
    if isinstance(queued, list):
        return len(queued)
    return 0


def _status_compact(status_call: dict[str, Any]) -> dict[str, Any]:
    status = status_call.get("result") or {}
    return {
        "operationId": status.get("operationId"),
        "operationType": status.get("operationType"),
        "status": status.get("status"),
        "threadId": status.get("threadId"),
        "turnId": status.get("turnId"),
        "nextRecommendedAction": status.get("nextRecommendedAction"),
        "queueState": status.get("queueState"),
        "workerState": status.get("workerState"),
        "terminalEvidence": status.get("terminalEvidence"),
        "error": status.get("error"),
    }


def _poll_operation_statuses(operation_ids: dict[str, str], *, progress_events: int = 10) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for name, operation_id in operation_ids.items():
        statuses[name] = _daemon_tool(
            "codex_get_operation_status",
            {
                "operation_id": operation_id,
                "last_messages": 3,
                "message_max_chars": 3000,
                "progress_events": progress_events,
                "progress_max_chars": 2000,
            },
            15,
        )
    return statuses


def _interrupt_operation(operation_id: str) -> dict[str, Any]:
    return _daemon_tool("codex_interrupt_turn", {"operation_id": operation_id, "timeout_seconds": 30}, 40)


def _tool_error_summary(response: dict[str, Any]) -> str | None:
    structured = response.get("result") or {}
    if not structured.get("_mcpIsError") and structured.get("ok") is not False:
        return None
    error = structured.get("error") or {}
    if isinstance(error, dict):
        return f"{error.get('code') or 'error'}: {error.get('message') or structured.get('status')}"
    return str(structured.get("status") or "tool error")


def _record_tool_error(
    index: int,
    *,
    response: dict[str, Any],
    area: str,
    scenario: str,
    expected: str,
    severity: str = "S2",
) -> int:
    summary = _tool_error_summary(response)
    if not summary:
        return index
    return finding(
        index,
        severity=severity,
        area=area,
        scenario=scenario,
        expected=expected,
        actual=summary,
        evidence=json.dumps((response.get("result") or {}).get("error") or response.get("result") or {}, ensure_ascii=False)[:1000],
    )


def _poll_worker_command(command_id: str, *, timeout_seconds: int = 180, include_result: bool = True) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    statuses: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        status = _daemon_tool(
            "codex_get_worker_command_status",
            {"command_id": command_id, "include_result": include_result, "max_result_chars": 4000},
            15,
        )
        statuses.append(status)
        structured = status.get("result") or {}
        if structured.get("status") in TERMINAL_COMMAND_STATUSES:
            break
        time.sleep(2)
    return statuses


def _extract_action_id_from_command_statuses(command_statuses: list[dict[str, Any]]) -> str | None:
    for status in reversed(command_statuses):
        structured = status.get("result") or {}
        result = structured.get("result")
        if isinstance(result, dict):
            action_id = result.get("actionId") or result.get("action_id")
            if action_id:
                return str(action_id)
        action_id = structured.get("actionId") or structured.get("action_id")
        if action_id:
            return str(action_id)
    return None


def _record_lifecycle_command_result(
    index: int,
    *,
    area: str,
    scenario: str,
    response: dict[str, Any],
    command_statuses: list[dict[str, Any]],
) -> int:
    index = _record_tool_error(
        index,
        response=response,
        area=area,
        scenario=scenario,
        expected="lifecycle call delegates to worker command or completes directly",
    )
    if not command_statuses:
        structured = response.get("result") or {}
        if structured.get("commandId"):
            return finding(
                index,
                severity="S2",
                area=area,
                scenario=scenario,
                expected="worker command status is pollable",
                actual="commandId returned but no command status was collected",
                evidence=f"commandId={structured.get('commandId')}",
            )
        return index
    latest = command_statuses[-1].get("result") or {}
    status = latest.get("status")
    if status not in TERMINAL_COMMAND_STATUSES:
        return finding(
            index,
            severity="S2",
            area=area,
            scenario=scenario,
            expected="worker command reaches terminal status within scenario timeout",
            actual=f"status={status}",
            evidence=json.dumps(latest, ensure_ascii=False)[:1000],
        )
    if status != "completed":
        return finding(
            index,
            severity="S2",
            area=area,
            scenario=scenario,
            expected="lifecycle worker command completes successfully",
            actual=f"status={status}; lastError={latest.get('lastError')}",
            evidence=json.dumps(latest.get("result") or latest.get("lastError") or latest, ensure_ascii=False)[:1000],
        )
    nested = latest.get("result")
    if isinstance(nested, dict) and (nested.get("_mcpIsError") or nested.get("ok") is False):
        return finding(
            index,
            severity="S2",
            area=area,
            scenario=scenario,
            expected="lifecycle command result is successful",
            actual=json.dumps(nested.get("error") or nested, ensure_ascii=False)[:500],
            evidence=json.dumps(nested.get("error") or nested, ensure_ascii=False)[:1000],
        )
    return index


def _stop_test_operations(operation_ids: dict[str, str], statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    interrupted: dict[str, Any] = {}
    for name, operation_id in operation_ids.items():
        status = (statuses.get(name, {}).get("result") or {}).get("status")
        if status not in TERMINAL_OPERATION_STATUSES:
            interrupted[name] = _interrupt_operation(operation_id)
    return interrupted


def _wait_for_thread_turn(operation_id: str, *, timeout_seconds: int = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _daemon_tool(
            "codex_get_operation_status",
            {"operation_id": operation_id, "last_messages": 2, "message_max_chars": 1000, "progress_events": 5},
            15,
        )
        structured = latest.get("result") or {}
        if structured.get("threadId") and structured.get("turnId"):
            return latest
        if structured.get("status") in TERMINAL_OPERATION_STATUSES:
            return latest
        time.sleep(5)
    return latest


def run_parallel_stress_scenario(args: argparse.Namespace, *, initialize: bool = True) -> dict[str, Any]:
    if initialize:
        initialize_report(scenario=args.scenario, archive=args.archive_report)
    append_report(f"- `{utc_now()}` Starting external-client parallel stress scenario.\n")
    index = _next_finding_index()
    result: dict[str, Any] = {}
    queue = _daemon_tool("codex_get_queue_status", {}, 10)
    concurrency = _daemon_tool("codex_get_concurrency_status", {}, 10)
    result["initialQueue"] = queue
    result["initialConcurrency"] = concurrency
    if _active_slot_count(queue):
        index = finding(
            index,
            severity="S1",
            area="codex_get_queue_status",
            scenario="parallel stress safety gate",
            expected="no active test/work slots before starting stress",
            actual="active turn slots present",
            evidence=json.dumps((queue.get("result") or {}).get("queueSummary") or {}, ensure_ascii=False)[:1000],
        )
        _append_json_section("Parallel Stress Payload", result, max_chars=30000)
        print_json({"ok": False, "report": str(REPORT), "findings": _finding_count(), "reason": "active work present"})
        return result

    project_ids = _project_ids()
    result["resolvedSandboxProjectIds"] = project_ids
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    specs = [
        ("TestProject1-A", SANDBOXES[0], "stress-agent-a", ["testproject1:area-a"]),
        ("TestProject1-B", SANDBOXES[0], "stress-agent-a", ["testproject1:area-b"]),
        ("TestProject1-C", SANDBOXES[0], "stress-agent-b", ["testproject1:area-c"]),
        ("TestProject2-D", SANDBOXES[1], "stress-agent-c", ["testproject2:area-a"]),
        ("TestProject3-E", SANDBOXES[2], "stress-agent-d", ["testproject3:area-a"]),
    ]
    operations: dict[str, str] = {}
    for name, path, agent_id, resource_keys in specs:
        project_id = project_ids.get(path.name)
        if not project_id:
            index = finding(
                index,
                severity="S2",
                area="codex_list_projects",
                scenario=f"resolve {path.name} for parallel stress",
                expected="project id present",
                actual="missing project id",
                evidence=path.name,
            )
            continue
        label = f"parallel-{name.lower()}-{stamp}"
        submit = _submit_start_chat(
            project_id=project_id,
            path=path,
            label=label,
            message=_long_running_prompt(label, sleep_seconds=args.stress_sleep_seconds, sleep_count=args.stress_sleep_count),
            agent_id=agent_id,
            resource_keys=resource_keys,
            cost_class="heavy",
        )
        result[f"submit:{name}"] = submit
        index = _public_response_findings(index, area="codex_submit_task", scenario=f"parallel stress submit {name}", response=submit.get("result"))
        structured = submit.get("result") or {}
        if structured.get("_mcpIsError") or not structured.get("operationId"):
            error = structured.get("error") or {}
            index = finding(
                index,
                severity="S2",
                area="codex_submit_task",
                scenario=f"parallel stress submit {name}",
                expected="long-running operation accepted into durable queue",
                actual=f"{error.get('code') or 'missing_operation'}: {error.get('message') or 'no operationId'}",
                evidence=json.dumps(error or structured, ensure_ascii=False)[:1000],
            )
            continue
        operations[name] = structured["operationId"]
    result["operations"] = operations

    deadline = time.monotonic() + args.stress_timeout_seconds
    last_statuses: dict[str, dict[str, Any]] = {}
    samples: list[dict[str, Any]] = []
    while operations and time.monotonic() < deadline:
        last_statuses = _poll_operation_statuses(operations, progress_events=10)
        queue = _daemon_tool("codex_get_queue_status", {}, 10)
        concurrency = _daemon_tool("codex_get_concurrency_status", {}, 10)
        app_server = _daemon_tool("codex_get_app_server_status", {}, 10)
        health = _daemon_tool("codex_health_summary", {}, 10)
        active_slots = _active_slot_count(queue)
        queued_count = _queued_count(queue)
        samples.append(
            {
                "at": utc_now(),
                "activeTurnSlots": active_slots,
                "queuedCount": queued_count,
                "operations": {name: _status_compact(status) for name, status in last_statuses.items()},
                "concurrency": (concurrency.get("result") or {}).get("summary") or concurrency.get("result"),
                "appServerStatus": (app_server.get("result") or {}).get("status"),
                "health": (health.get("result") or {}).get("overallStatus"),
            }
        )
        if active_slots > 4:
            index = finding(
                index,
                severity="S1",
                area="codex_get_queue_status",
                scenario="parallel stress global slot limit",
                expected="at most 4 active turn slots",
                actual=f"activeTurnSlots={active_slots}",
                evidence=json.dumps((queue.get("result") or {}).get("queueSummary") or {}, ensure_ascii=False)[:1000],
            )
        all_terminal = all((status.get("result") or {}).get("status") in TERMINAL_OPERATION_STATUSES for status in last_statuses.values())
        if all_terminal:
            break
        time.sleep(args.poll_interval_seconds)
    result["samples"] = samples[-20:]
    result["lastStatuses"] = last_statuses
    if operations:
        nonterminal = {
            name: operation_id
            for name, operation_id in operations.items()
            if (last_statuses.get(name, {}).get("result") or {}).get("status") not in TERMINAL_OPERATION_STATUSES
        }
        if nonterminal:
            index = finding(
                index,
                severity="S1",
                area="codex_get_operation_status",
                scenario="parallel stress terminal completion",
                expected="all stress operations reach terminal state before scenario timeout",
                actual=f"nonterminal={sorted(nonterminal)}",
                evidence=json.dumps({name: _status_compact(last_statuses.get(name, {})) for name in nonterminal}, ensure_ascii=False)[:1000],
            )
            result["cleanupInterrupts"] = _stop_test_operations(nonterminal, last_statuses)
    result["finalQueue"] = _daemon_tool("codex_get_queue_status", {}, 10)
    result["finalConcurrency"] = _daemon_tool("codex_get_concurrency_status", {}, 10)
    _append_json_section("Parallel Stress Payload", result, max_chars=70000)
    print_json({"ok": True, "report": str(REPORT), "findings": _finding_count(), "operations": operations, "sampleCount": len(samples)})
    return result


def run_steer_interrupt_scenario(args: argparse.Namespace, *, initialize: bool = True) -> dict[str, Any]:
    if initialize:
        initialize_report(scenario=args.scenario, archive=args.archive_report)
    append_report(f"- `{utc_now()}` Starting external-client steer/interrupt scenario.\n")
    index = _next_finding_index()
    result: dict[str, Any] = {}
    project_ids = _project_ids()
    project_id = project_ids.get("TestProject1")
    if not project_id:
        index = finding(index, severity="S2", area="codex_list_projects", scenario="steer/interrupt setup", expected="TestProject1 project id", actual="missing", evidence=json.dumps(project_ids, ensure_ascii=False))
        print_json({"ok": False, "report": str(REPORT), "findings": _finding_count(), "reason": "missing project"})
        return result
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    operations: dict[str, str] = {}
    for name in ("target", "interrupt"):
        label = f"steer-{name}-{stamp}"
        submit = _submit_start_chat(
            project_id=project_id,
            path=SANDBOXES[0],
            label=label,
            message=_long_running_prompt(label, sleep_seconds=args.stress_sleep_seconds, sleep_count=1),
            agent_id="external-steer-test",
            resource_keys=[f"testproject1:steer:{name}:{stamp}"],
            cost_class="normal",
        )
        result[f"submit:{name}"] = submit
        structured = submit.get("result") or {}
        if structured.get("operationId"):
            operations[name] = structured["operationId"]
        else:
            index = finding(index, severity="S2", area="codex_submit_task", scenario=f"steer/interrupt submit {name}", expected="operation accepted", actual=json.dumps(structured.get("error") or structured, ensure_ascii=False)[:500], evidence=name)
    result["operations"] = operations
    target_status = _wait_for_thread_turn(operations.get("target", ""), timeout_seconds=180) if operations.get("target") else {}
    interrupt_status = _wait_for_thread_turn(operations.get("interrupt", ""), timeout_seconds=180) if operations.get("interrupt") else {}
    result["targetReadyStatus"] = target_status
    result["interruptReadyStatus"] = interrupt_status
    target = target_status.get("result") or {}
    if target.get("threadId") and target.get("turnId"):
        steer = _daemon_tool(
            "codex_submit_task",
            {
                "operation_type": "steer_turn",
                "thread_id": target.get("threadId"),
                "expected_turn_id": target.get("turnId"),
                "message": (
                    "MCP LIVE TEST STEER\n\n"
                    "Не останавливай текущую работу. Добавь файл steer_received.txt в свой scenario-каталог "
                    "и учти это в final_report.json."
                ),
                "client_request_id": f"external-live:steer-turn-{stamp}",
                "agent_id": "external-steer-test",
                "sandbox": "danger-full-access",
                "approval_policy": "never",
                "timeout_seconds": 60,
            },
            30,
        )
        result["steerSubmit"] = steer
        structured = steer.get("result") or {}
        if structured.get("operationId"):
            operations["steer"] = structured["operationId"]
        else:
            index = finding(index, severity="S2", area="codex_submit_task", scenario="steer submit", expected="steer operation accepted", actual=json.dumps(structured.get("error") or structured, ensure_ascii=False)[:500], evidence=json.dumps({"threadId": target.get("threadId"), "turnId": target.get("turnId")}, ensure_ascii=False))
    else:
        index = finding(index, severity="S1", area="codex_get_operation_status", scenario="steer target readiness", expected="target operation exposes threadId and turnId", actual=json.dumps(_status_compact(target_status), ensure_ascii=False)[:500], evidence=operations.get("target", ""))
    if operations.get("interrupt"):
        result["interruptCommand"] = _interrupt_operation(operations["interrupt"])
    if operations.get("target"):
        result["targetInterruptCommand"] = _interrupt_operation(operations["target"])
    deadline = time.monotonic() + 180
    last_statuses: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        last_statuses = _poll_operation_statuses(operations, progress_events=10)
        if all((status.get("result") or {}).get("status") in TERMINAL_OPERATION_STATUSES for status in last_statuses.values()):
            break
        time.sleep(10)
    result["lastStatuses"] = last_statuses
    for name, status in last_statuses.items():
        structured = status.get("result") or {}
        if structured.get("status") not in TERMINAL_OPERATION_STATUSES:
            index = finding(index, severity="S1", area="codex_get_operation_status", scenario=f"steer/interrupt terminal {name}", expected="operation reaches terminal after interrupt", actual=f"status={structured.get('status')}", evidence=json.dumps(_status_compact(status), ensure_ascii=False)[:1000])
    result["finalQueue"] = _daemon_tool("codex_get_queue_status", {}, 10)
    _append_json_section("Steer Interrupt Payload", result, max_chars=50000)
    print_json({"ok": True, "report": str(REPORT), "findings": _finding_count(), "operations": operations})
    return result


def run_workflow_review_scenario(args: argparse.Namespace, *, initialize: bool = True) -> dict[str, Any]:
    if initialize:
        initialize_report(scenario=args.scenario, archive=args.archive_report)
    append_report(f"- `{utc_now()}` Starting external-client workflow/review scenario.\n")
    index = _next_finding_index()
    result: dict[str, Any] = {}
    project_ids = _project_ids()
    project_id = project_ids.get("TestProject3")
    if not project_id:
        index = finding(index, severity="S2", area="codex_list_projects", scenario="workflow/review setup", expected="TestProject3 project id", actual="missing", evidence=json.dumps(project_ids, ensure_ascii=False))
        print_json({"ok": False, "report": str(REPORT), "findings": _finding_count(), "reason": "missing project"})
        return result
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan = _daemon_tool(
        "codex_start_plan_workflow",
        {
            "project_id": project_id,
            "cwd": str(SANDBOXES[2]),
            "message": (
                "MCP LIVE TEST / SANDBOX PROJECT / OK TO MODIFY FILES\n\n"
                f"Подготовь короткий план для создания live_parallel_test/workflow-{stamp}/workflow_result.json. "
                "План должен быть конкретным и выполнимым без долгих ожиданий. Не запрашивай подтверждений."
            ),
            "title": f"MCP LIVE TEST workflow-{stamp}",
            "client_request_id": f"external-live:workflow-plan-{stamp}",
            "sandbox": "danger-full-access",
            "approval_policy": "never",
            "timeout_seconds": 60,
        },
        30,
    )
    result["planStart"] = plan
    workflow_id = (plan.get("result") or {}).get("workflowId")
    if workflow_id:
        statuses = []
        deadline = time.monotonic() + 360
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = _daemon_tool("codex_get_workflow_status", {"workflow_id": workflow_id, "refresh_live": False, "last_messages": 3, "message_max_chars": 3000, "include_events": True}, 15)
            statuses.append(_compact_workflow_status(latest))
            phase = (latest.get("result") or {}).get("phase") or (latest.get("result") or {}).get("status")
            if phase in {"plan_ready", "ready_for_approval", "failed", "blocked", "completed"} or (latest.get("result") or {}).get("latestPlan"):
                break
            time.sleep(15)
        result["planStatuses"] = statuses
        latest_result = latest.get("result") or {}
        if latest_result.get("latestPlan"):
            approve = _daemon_tool(
                "codex_approve_plan",
                {
                    "workflow_id": workflow_id,
                    "client_request_id": f"external-live:workflow-approve-{stamp}",
                    "sandbox": "danger-full-access",
                    "approval_policy": "never",
                    "message": (
                        "Implement the approved test plan. Work only inside the current sandbox project. "
                        f"Create live_parallel_test/workflow-{stamp}/workflow_result.json with completed=true."
                    ),
                    "output_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"completed": {"type": "boolean"}, "scenarioId": {"type": "string"}},
                        "required": ["completed", "scenarioId"],
                    },
                },
                30,
            )
            result["approve"] = approve
            index = _record_tool_error(
                index,
                response=approve,
                area="codex_approve_plan",
                scenario="plan workflow approval",
                expected="approval queues execution operation or returns idempotent durable ack",
            )
            if _tool_error_summary(approve):
                result["finalQueue"] = _daemon_tool("codex_get_queue_status", {}, 10)
                _append_json_section("Workflow Review Payload", result, max_chars=70000)
                print_json({"ok": True, "report": str(REPORT), "findings": _finding_count(), "workflowId": workflow_id, "reviewWorkflowId": None})
                return result
            exec_deadline = time.monotonic() + 420
            execution_statuses = []
            while time.monotonic() < exec_deadline:
                status = _daemon_tool("codex_get_workflow_status", {"workflow_id": workflow_id, "refresh_live": False, "last_messages": 3, "message_max_chars": 3000, "include_events": True}, 15)
                execution_statuses.append(_compact_workflow_status(status))
                if (status.get("result") or {}).get("status") in {"completed", "failed", "blocked", "interrupted"}:
                    break
                time.sleep(20)
            result["executionStatuses"] = execution_statuses
            if not execution_statuses or execution_statuses[-1].get("status") not in {"completed", "failed", "blocked", "interrupted"}:
                index = finding(
                    index,
                    severity="S2",
                    area="codex_get_workflow_status",
                    scenario="plan workflow execution terminal",
                    expected="approved workflow execution reaches terminal state within scenario timeout",
                    actual=json.dumps(execution_statuses[-1] if execution_statuses else {}, ensure_ascii=False)[:700],
                    evidence=f"workflowId={workflow_id}",
                )
        else:
            index = finding(index, severity="S2", area="codex_get_workflow_status", scenario="plan workflow readiness", expected="latestPlan appears or terminal blocker is explained", actual=json.dumps(_compact_workflow_status(latest), ensure_ascii=False)[:700], evidence=f"workflowId={workflow_id}")
    else:
        index = finding(index, severity="S2", area="codex_start_plan_workflow", scenario="plan workflow start", expected="workflowId returned", actual=json.dumps(plan.get("result") or {}, ensure_ascii=False)[:700], evidence="workflow start response")

    review = _daemon_tool(
        "codex_start_review_workflow",
        {
            "target_type": "custom",
            "project_id": project_id,
            "cwd": str(SANDBOXES[2]),
            "instructions": (
                "MCP LIVE TEST REVIEW. Review only the sandbox test files. "
                "Return a compact report; do not run long commands or modify files."
            ),
            "client_request_id": f"external-live:review-{stamp}",
            "sandbox": "danger-full-access",
            "approval_policy": "never",
            "timeout_seconds": 60,
        },
        30,
    )
    result["reviewStart"] = review
    index = _record_tool_error(
        index,
        response=review,
        area="codex_start_review_workflow",
        scenario="review workflow start",
        expected="review workflow starts and returns workflowId",
    )
    review_workflow_id = (review.get("result") or {}).get("workflowId")
    if review_workflow_id:
        review_statuses = []
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            status = _daemon_tool("codex_get_workflow_status", {"workflow_id": review_workflow_id, "refresh_live": False, "last_messages": 3, "message_max_chars": 3000, "include_events": True}, 15)
            review_statuses.append(_compact_workflow_status(status))
            if (status.get("result") or {}).get("status") in {"completed", "failed", "blocked", "interrupted"}:
                break
            time.sleep(20)
        result["reviewStatuses"] = review_statuses
        if not review_statuses or review_statuses[-1].get("status") not in {"completed", "failed", "blocked", "interrupted"}:
            index = finding(
                index,
                severity="S2",
                area="codex_get_workflow_status",
                scenario="review workflow terminal",
                expected="review workflow reaches terminal state within scenario timeout",
                actual=json.dumps(review_statuses[-1] if review_statuses else {}, ensure_ascii=False)[:700],
                evidence=f"workflowId={review_workflow_id}",
            )
    else:
        index = finding(index, severity="S2", area="codex_start_review_workflow", scenario="review workflow start", expected="workflowId returned", actual=json.dumps(review.get("result") or {}, ensure_ascii=False)[:700], evidence="review start response")
    result["finalQueue"] = _daemon_tool("codex_get_queue_status", {}, 10)
    _append_json_section("Workflow Review Payload", result, max_chars=70000)
    print_json({"ok": True, "report": str(REPORT), "findings": _finding_count(), "workflowId": workflow_id, "reviewWorkflowId": review_workflow_id})
    return result


def _compact_workflow_status(status_call: dict[str, Any]) -> dict[str, Any]:
    status = status_call.get("result") or {}
    return {
        "workflowId": status.get("workflowId"),
        "kind": status.get("kind") or status.get("workflowKind"),
        "phase": status.get("phase"),
        "status": status.get("status"),
        "nextRecommendedAction": status.get("nextRecommendedAction"),
        "officialTurnId": status.get("officialTurnId"),
        "canonicalTurnId": status.get("canonicalTurnId"),
        "latestThreadTurnId": status.get("latestThreadTurnId"),
        "hasLatestPlan": bool(status.get("latestPlan")),
        "hasFinalReport": bool(status.get("finalReport")),
        "workflowOperationQueueState": status.get("workflowOperationQueueState"),
        "error": status.get("error"),
    }


def run_lifecycle_scenario(args: argparse.Namespace, *, initialize: bool = True) -> dict[str, Any]:
    if initialize:
        initialize_report(scenario=args.scenario, archive=args.archive_report)
    append_report(f"- `{utc_now()}` Starting external-client lifecycle scenario.\n")
    index = _next_finding_index()
    result: dict[str, Any] = {}
    project_ids = _project_ids()
    project_id = project_ids.get("TestProject2")
    if not project_id:
        index = finding(index, severity="S2", area="codex_list_projects", scenario="lifecycle setup", expected="TestProject2 project id", actual="missing", evidence=json.dumps(project_ids, ensure_ascii=False))
        print_json({"ok": False, "report": str(REPORT), "findings": _finding_count(), "reason": "missing project"})
        return result
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    submit = _submit_start_chat(
        project_id=project_id,
        path=SANDBOXES[1],
        label=f"lifecycle-{stamp}",
        message=_quick_write_prompt(f"lifecycle-{stamp}"),
        agent_id="external-lifecycle-test",
        resource_keys=[f"testproject2:lifecycle:{stamp}"],
        cost_class="light",
    )
    result["submit"] = submit
    operation_id = (submit.get("result") or {}).get("operationId")
    thread_id = None
    if operation_id:
        deadline = time.monotonic() + 240
        latest = {}
        while time.monotonic() < deadline:
            latest = _daemon_tool("codex_get_operation_status", {"operation_id": operation_id, "last_messages": 2, "message_max_chars": 1000, "progress_events": 5}, 15)
            if (latest.get("result") or {}).get("status") in TERMINAL_OPERATION_STATUSES:
                break
            time.sleep(10)
        result["operationStatus"] = latest
        thread_id = (latest.get("result") or {}).get("threadId")
    if not thread_id:
        index = finding(index, severity="S2", area="codex_get_operation_status", scenario="lifecycle test thread setup", expected="completed quick operation exposes threadId", actual=json.dumps(_status_compact(result.get("operationStatus") or {}), ensure_ascii=False)[:700], evidence=operation_id or "no operation")
        _append_json_section("Lifecycle Payload", result, max_chars=40000)
        print_json({"ok": False, "report": str(REPORT), "findings": _finding_count(), "reason": "no thread"})
        return result
    result["archive"] = _daemon_tool("codex_archive_thread", {"thread_id": thread_id, "project_id": project_id, "refresh_catalog": False, "timeout_seconds": 30}, 40)
    result["archiveCommandStatuses"] = _poll_worker_command((result["archive"].get("result") or {}).get("commandId"), timeout_seconds=120) if (result["archive"].get("result") or {}).get("commandId") else []
    index = _record_lifecycle_command_result(index, area="codex_archive_thread", scenario="archive completed test thread", response=result["archive"], command_statuses=result["archiveCommandStatuses"])
    result["unarchive"] = _daemon_tool("codex_unarchive_thread", {"thread_id": thread_id, "project_id": project_id, "refresh_catalog": False, "timeout_seconds": 30}, 40)
    result["unarchiveCommandStatuses"] = _poll_worker_command((result["unarchive"].get("result") or {}).get("commandId"), timeout_seconds=120) if (result["unarchive"].get("result") or {}).get("commandId") else []
    index = _record_lifecycle_command_result(index, area="codex_unarchive_thread", scenario="unarchive completed test thread", response=result["unarchive"], command_statuses=result["unarchiveCommandStatuses"])
    compaction = _daemon_tool("codex_start_thread_compaction", {"thread_id": thread_id, "project_id": project_id, "timeout_seconds": 30}, 40)
    result["compactionStart"] = compaction
    result["compactionCommandStatuses"] = _poll_worker_command((compaction.get("result") or {}).get("commandId"), timeout_seconds=180) if (compaction.get("result") or {}).get("commandId") else []
    index = _record_lifecycle_command_result(index, area="codex_start_thread_compaction", scenario="compact completed test thread", response=compaction, command_statuses=result["compactionCommandStatuses"])
    action_id = (
        (compaction.get("result") or {}).get("actionId")
        or (compaction.get("result") or {}).get("action_id")
        or _extract_action_id_from_command_statuses(result["compactionCommandStatuses"])
    )
    if action_id:
        compaction_statuses = []
        for _ in range(18):
            status = _daemon_tool("codex_get_thread_compaction_status", {"action_id": action_id, "include_events": False}, 15)
            compaction_statuses.append(status.get("result") or {})
            if (status.get("result") or {}).get("status") in {"completed", "failed", "partial_success", "timed_out", "cancelled", "canceled", "ambiguous_after_timeout", "unknown_after_app_server_exit"}:
                break
            time.sleep(10)
        result["compactionStatuses"] = compaction_statuses
    result["pendingInteractions"] = _daemon_tool("codex_list_pending_interactions", {}, 15)
    _append_json_section("Lifecycle Payload", result, max_chars=60000)
    print_json({"ok": True, "report": str(REPORT), "findings": _finding_count(), "threadId": thread_id, "actionId": action_id})
    return result


def run_diagnostics_scenario(args: argparse.Namespace, *, initialize: bool = True) -> dict[str, Any]:
    if initialize:
        initialize_report(scenario=args.scenario, archive=args.archive_report)
    append_report(f"- `{utc_now()}` Starting external-client diagnostics scenario.\n")
    index = _next_finding_index()
    result: dict[str, Any] = {}
    diagnostics = _daemon_tool("codex_collect_diagnostics", {"include_logs": False, "include_timeline": True, "event_limit": 50, "timeline_limit": 50}, 20)
    analyze = _daemon_tool("codex_analyze_issue", {"include_evidence": True, "problem_text": "External MCP client routine diagnostics live test.", "since_minutes": 120}, 20)
    repair = _daemon_tool("codex_repair_issue", {"action": "validate_paths_and_config", "dry_run": True, "reason": "external-client diagnostics scenario", "timeout_seconds": 30}, 40)
    result["diagnostics"] = diagnostics
    result["analyze"] = analyze
    result["repairDryRun"] = repair
    for area, response in (("codex_collect_diagnostics", diagnostics), ("codex_analyze_issue", analyze), ("codex_repair_issue", repair)):
        index = _public_response_findings(index, area=area, scenario="diagnostics public response safety", response=response.get("result"))
        structured = response.get("result") or {}
        if structured.get("_mcpIsError"):
            error = structured.get("error") or {}
            index = finding(index, severity="S2", area=area, scenario="diagnostics scenario tool success", expected="tool succeeds or returns structured recoverable state", actual=f"{error.get('code')}: {error.get('message')}", evidence=json.dumps(error, ensure_ascii=False)[:1000])
    _append_json_section("Diagnostics Payload", result, max_chars=50000)
    print_json({"ok": True, "report": str(REPORT), "findings": _finding_count()})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="External MCP client for live testing without restarting Codex Desktop.")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--execution-mode", default="client", choices=["inline", "client", "worker", "observe"])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("smoke", help="Run one-shot protocol/runtime smoke.")

    start = sub.add_parser("daemon-start", help="Start the external MCP client daemon.")
    start.add_argument("--port", type=int, default=DEFAULT_PORT)
    start.add_argument("--force", action="store_true")
    start.add_argument("--allowed-root", action="append", default=None)

    sub.add_parser("daemon-status", help="Read daemon status.")
    sub.add_parser("daemon-stop", help="Stop daemon.")

    restart = sub.add_parser("daemon-restart-mcp", help="Restart only the MCP subprocess inside daemon.")
    restart.add_argument("--reason", default="manual_restart")

    tools = sub.add_parser("tools-list", help="Call tools/list through daemon.")
    tools.add_argument("--compact", action="store_true")

    call = sub.add_parser("call", help="Call an MCP tool.")
    call.add_argument("tool_name")
    call.add_argument("--json", default="{}")
    call.add_argument("--daemon", action="store_true", help="Route through external daemon instead of one-shot stdio.")

    live = sub.add_parser("run-live-test", help="Run a live-test scenario through the daemon.")
    live.add_argument(
        "--scenario",
        default="baseline",
        choices=[
            "baseline",
            "read",
            "durable-matrix",
            "parallel-stress",
            "steer-interrupt",
            "workflow-review",
            "lifecycle",
            "diagnostics",
            "full",
        ],
    )
    live.add_argument("--archive-report", action="store_true")
    live.add_argument("--stress-sleep-seconds", type=int, default=300)
    live.add_argument("--stress-sleep-count", type=int, default=2)
    live.add_argument("--stress-timeout-seconds", type=int, default=1800)
    live.add_argument("--poll-interval-seconds", type=int, default=60)
    return parser


def _effective_allowed_roots(explicit: list[str] | None) -> list[str] | None:
    if explicit:
        return explicit
    if os.environ.get("CODEX_ALLOWED_ROOTS"):
        return None
    configured = local_mcp_entry_env().get("CODEX_ALLOWED_ROOTS")
    if configured:
        return [item for item in configured.split(";") if item.strip()]
    existing_parents = sorted({str(path.parent) for path in SANDBOXES if path.exists()})
    return existing_parents or None


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "smoke":
        print_json(run_oneshot_smoke(args))
    elif args.command == "daemon-start":
        print_json(start_daemon(args))
    elif args.command == "daemon-status":
        print_json(daemon_status(args))
    elif args.command == "daemon-stop":
        print_json(stop_daemon(args))
    elif args.command == "daemon-restart-mcp":
        print_json(daemon_restart_mcp(args))
    elif args.command == "tools-list":
        print_json(daemon_tools_list(args))
    elif args.command == "call":
        print_json(call_tool(args))
    elif args.command == "run-live-test":
        if args.scenario == "baseline":
            run_baseline_scenario(args)
        elif args.scenario == "read":
            run_read_scenario(args)
        elif args.scenario == "durable-matrix":
            run_durable_matrix_scenario(args)
        elif args.scenario == "parallel-stress":
            run_parallel_stress_scenario(args)
        elif args.scenario == "steer-interrupt":
            run_steer_interrupt_scenario(args)
        elif args.scenario == "workflow-review":
            run_workflow_review_scenario(args)
        elif args.scenario == "lifecycle":
            run_lifecycle_scenario(args)
        elif args.scenario == "diagnostics":
            run_diagnostics_scenario(args)
        elif args.scenario == "full":
            run_baseline_scenario(args, initialize=True)
            run_read_scenario(args, initialize=False)
            run_durable_matrix_scenario(args, initialize=False)
            run_parallel_stress_scenario(args, initialize=False)
            run_steer_interrupt_scenario(args, initialize=False)
            run_workflow_review_scenario(args, initialize=False)
            run_lifecycle_scenario(args, initialize=False)
            run_diagnostics_scenario(args, initialize=False)
    else:
        parser.error(f"unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
