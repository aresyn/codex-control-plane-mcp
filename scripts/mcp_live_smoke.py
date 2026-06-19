from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
TERMINAL_WORKFLOW_PHASES = {"completed", "failed", "orphaned_after_app_server_exit"}


class McpStdioClient:
    def __init__(self, *, cwd: Path, timeout_seconds: int) -> None:
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self.timeout_seconds = timeout_seconds
        self.next_id = 1
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "codex_control_plane_mcp.server"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._stdout_reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stdout_reader.start()

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            self._stdout_lines.put(None)
            return
        try:
            for line in self.process.stdout:
                self._stdout_lines.put(line)
        finally:
            self._stdout_lines.put(None)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("MCP subprocess stdio pipes are not available.")
        request_id = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                line = self._stdout_lines.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self.process.poll() is not None:
                    stderr = self.process.stderr.read() if self.process.stderr is not None else ""
                    raise RuntimeError(f"MCP subprocess exited early with code {self.process.returncode}: {stderr}")
                continue
            if line is None:
                stderr = self.process.stderr.read() if self.process.stderr is not None else ""
                raise RuntimeError(f"MCP subprocess stdout closed with code {self.process.poll()}: {stderr}")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"MCP stdout contained non-JSON data: {line!r}") from exc
            if response.get("id") == request_id:
                return response
        raise TimeoutError(f"MCP request timed out: {method}")

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in response:
            raise RuntimeError(f"JSON-RPC error for tool {name}: {response['error']}")
        result = response.get("result") or {}
        structured = result.get("structuredContent") or {}
        structured["_mcpIsError"] = bool(result.get("isError"))
        return structured

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


def choose_project(projects: list[dict[str, Any]], cwd: Path, project_id: str | None) -> tuple[dict[str, Any], int]:
    if project_id:
        for project in projects:
            if project.get("projectId") == project_id or project.get("project_id") == project_id:
                return project, 0
        raise RuntimeError(f"Requested project_id was not found: {project_id}")
    cwd_key = str(cwd).replace("\\", "/").casefold()
    matches = [
        project
        for project in projects
        if str(project.get("path") or "").replace("\\", "/").casefold() == cwd_key
    ]
    if matches:
        return matches[0], len(matches)
    if not projects:
        raise RuntimeError("No Codex projects were returned by codex_list_projects.")
    return projects[0], 0


def compact_repair(result: dict[str, Any]) -> dict[str, Any]:
    repair_result = result.get("result") if isinstance(result.get("result"), dict) else {}
    return {
        "ok": result.get("ok"),
        "isError": result.get("_mcpIsError"),
        "repairRunId": result.get("repairRunId"),
        "action": result.get("action"),
        "dryRun": result.get("dryRun"),
        "force": result.get("force"),
        "changed": result.get("changed"),
        "restarted": repair_result.get("restarted"),
        "started": repair_result.get("started"),
        "beforePid": repair_result.get("beforePid"),
        "afterPid": repair_result.get("afterPid"),
        "processGeneration": repair_result.get("processGeneration"),
    }


def run_protocol(client: McpStdioClient) -> dict[str, Any]:
    initialized = client.request("initialize", {"protocolVersion": "2025-01-10"})
    listed = client.request("tools/list", {})
    tool_error = client.request("tools/call", {"name": "missing_tool", "arguments": {}})
    rpc_error = client.request("missing/method", {})
    tools = (listed.get("result") or {}).get("tools") or []
    return {
        "initialize": {
            "protocolVersion": (initialized.get("result") or {}).get("protocolVersion"),
            "serverInfo": (initialized.get("result") or {}).get("serverInfo"),
        },
        "toolsList": {
            "toolCount": len(tools),
            "hasOutputSchema": all("outputSchema" in tool for tool in tools),
            "hasHealthSummary": any(tool.get("name") == "codex_health_summary" for tool in tools),
        },
        "toolError": {
            "isError": ((tool_error.get("result") or {}).get("isError")),
            "code": (((tool_error.get("result") or {}).get("structuredContent") or {}).get("error") or {}).get("code"),
        },
        "rpcError": rpc_error.get("error"),
    }


def run_safe_operation(client: McpStdioClient, *, cwd: Path, project_id: str | None, timeout_seconds: int, safe_restart: bool) -> dict[str, Any]:
    projects = client.tool("codex_list_projects")
    project, path_matches = choose_project(projects.get("projects") or [], cwd, project_id)
    selected_project_id = project.get("projectId") or project.get("project_id")
    marker = "MCP LIVE TEST / DO NOT MODIFY FILES / ITERATION 5 / " + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    health_before = client.tool("codex_health_summary", {"max_recent_errors": 5})
    submit_args = {
        "operation_type": "start_chat",
        "project_id": selected_project_id,
        "cwd": str(cwd),
        "title": marker,
        "client_request_id": "live-iter5-" + marker.rsplit(" / ", 1)[-1],
        "message": marker + "\nSafe production hardening smoke. Do not modify files. Reply with one short sentence confirming the marker.",
        "approval_policy": "never",
        "sandbox": "read-only",
        "collaboration_mode": "default",
        "timeout_seconds": min(timeout_seconds, 120),
    }
    submitted = client.tool("codex_submit_task", submit_args)
    duplicate = client.tool("codex_submit_task", {**submit_args, "client_request_id": None})
    operation_id = submitted.get("operationId") or submitted.get("operation_id")
    status = submitted
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = client.tool("codex_get_operation_status", {"operation_id": operation_id, "last_messages": 3, "message_max_chars": 4000})
        if status.get("status") in TERMINAL_OPERATION_STATUSES and status.get("threadId") and status.get("turnId"):
            break
        time.sleep(3)
    diagnostics = client.tool("codex_collect_diagnostics", {"operation_id": operation_id, "include_timeline": True, "timeline_limit": 50})
    analysis = client.tool("codex_analyze_issue", {"operation_id": operation_id, "problem_text": "live regression smoke", "include_evidence": False})
    dry_repair = client.tool("codex_repair_issue", {"action": "recover_stale_operations", "operation_id": operation_id})
    health_after = client.tool("codex_health_summary", {"operation_id": operation_id, "max_recent_errors": 5})
    active = health_after.get("activeWork") or {}
    idle = not active.get("activeTurnCount") and not active.get("pendingInteractionCount") and not active.get("pendingRequests")
    restart = {"skipped": True, "reason": "safe_restart disabled"}
    if safe_restart:
        if idle:
            restart = compact_repair(
                client.tool("codex_repair_issue", {"action": "restart_app_server_idle", "dry_run": False, "timeout_seconds": 30})
            )
        else:
            restart = {"skipped": True, "reason": "active work present"}
    return {
        "projectId": selected_project_id,
        "pathCasingMatches": path_matches,
        "healthBefore": health_before.get("overallStatus"),
        "version": health_before.get("version"),
        "operationId": operation_id,
        "operationStatus": status.get("status"),
        "threadId": status.get("threadId"),
        "turnId": status.get("turnId"),
        "duplicate": {
            "isError": duplicate.get("_mcpIsError"),
            "code": ((duplicate.get("error") or {}).get("code")),
            "operationId": duplicate.get("operationId"),
        },
        "diagnostics": {
            "overallStatus": diagnostics.get("overallStatus"),
            "diagnosisConfidence": diagnostics.get("diagnosisConfidence"),
            "timelineCount": len(diagnostics.get("timeline") or []),
        },
        "analysisRoot": ((analysis.get("likelyRootCause") or {}).get("category")),
        "dryRepair": {"action": dry_repair.get("action"), "dryRun": dry_repair.get("dryRun"), "changed": dry_repair.get("changed")},
        "safeRestart": restart,
    }


def run_workflow(client: McpStdioClient, *, cwd: Path, project_id: str | None, timeout_seconds: int) -> dict[str, Any]:
    projects = client.tool("codex_list_projects")
    project, path_matches = choose_project(projects.get("projects") or [], cwd, project_id)
    selected_project_id = project.get("projectId") or project.get("project_id")
    marker = "MCP LIVE TEST / DO NOT MODIFY FILES / ITERATION 5 WORKFLOW / " + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started = client.tool(
        "codex_start_plan_workflow",
        {
            "project_id": selected_project_id,
            "cwd": str(cwd),
            "title": marker,
            "client_request_id": "live-iter5-workflow-" + marker.rsplit(" / ", 1)[-1],
            "message": marker + "\nPrepare a tiny plan that confirms this smoke test should not modify files.",
            "approval_policy": "never",
            "sandbox": "read-only",
            "timeout_seconds": min(timeout_seconds, 120),
        },
    )
    workflow_id = started.get("workflowId")
    status = started
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = client.tool("codex_get_workflow_status", {"workflow_id": workflow_id, "last_messages": 3, "message_max_chars": 4000})
        if status.get("phase") in {"plan_ready", "completed", "failed", "orphaned_after_app_server_exit"}:
            break
        time.sleep(3)
    approved: dict[str, Any] = {"skipped": True, "reason": "plan not ready"}
    final_status = status
    duplicate_approve: dict[str, Any] = {"skipped": True}
    if status.get("phase") == "plan_ready":
        approved = client.tool(
            "codex_approve_plan",
            {
                "workflow_id": workflow_id,
                "client_request_id": "live-iter5-workflow-approve-" + str(workflow_id),
                "message": "Execute the smoke plan without modifying files. Reply with a short final report.",
                "approval_policy": "never",
                "sandbox": "read-only",
                "timeout_seconds": min(timeout_seconds, 120),
            },
        )
        duplicate_approve = client.tool(
            "codex_approve_plan",
            {
                "workflow_id": workflow_id,
                "client_request_id": "live-iter5-workflow-approve-" + str(workflow_id),
                "message": "Execute the smoke plan without modifying files. Reply with a short final report.",
                "approval_policy": "never",
                "sandbox": "read-only",
            },
        )
        execution_deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < execution_deadline:
            final_status = client.tool("codex_get_workflow_status", {"workflow_id": workflow_id, "last_messages": 3, "message_max_chars": 4000})
            if final_status.get("phase") in TERMINAL_WORKFLOW_PHASES:
                break
            time.sleep(5)
    return {
        "projectId": selected_project_id,
        "pathCasingMatches": path_matches,
        "workflowId": workflow_id,
        "planOperationId": started.get("planOperationId"),
        "planTurnId": final_status.get("planTurnId") or status.get("planTurnId"),
        "executionOperationId": final_status.get("executionOperationId"),
        "executionTurnId": final_status.get("executionTurnId"),
        "phase": final_status.get("phase") or status.get("phase"),
        "status": final_status.get("status") or status.get("status"),
        "latestPlanStatus": ((final_status.get("latestPlan") or status.get("latestPlan") or {}).get("status")),
        "finalReportPresent": bool(final_status.get("finalReport")),
        "approved": {"isError": approved.get("_mcpIsError"), "executionOperationId": approved.get("executionOperationId")},
        "duplicateApprove": {
            "isError": duplicate_approve.get("_mcpIsError"),
            "idempotent": duplicate_approve.get("idempotent"),
            "executionOperationId": duplicate_approve.get("executionOperationId"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Codex Control Plane MCP live smoke through real MCP stdio.")
    parser.add_argument("--scenario", choices=["protocol", "safe-operation", "full"], default="protocol")
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--safe-restart", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    cwd = Path(args.cwd).resolve()
    started_at = datetime.now(timezone.utc).isoformat()
    client = McpStdioClient(cwd=repo_root, timeout_seconds=max(10, min(args.timeout_seconds, 600)))
    try:
        protocol = run_protocol(client)
        result: dict[str, Any] = {
            "ok": True,
            "scenario": args.scenario,
            "startedAt": started_at,
            "protocol": protocol,
        }
        if args.scenario in {"safe-operation", "full"}:
            result["safeOperation"] = run_safe_operation(
                client,
                cwd=cwd,
                project_id=args.project_id,
                timeout_seconds=max(30, min(args.timeout_seconds, 600)),
                safe_restart=bool(args.safe_restart or args.scenario == "full"),
            )
        if args.scenario == "full":
            result["workflow"] = run_workflow(
                client,
                cwd=cwd,
                project_id=args.project_id,
                timeout_seconds=max(60, min(args.timeout_seconds, 900)),
            )
    except Exception as exc:
        result = {
            "ok": False,
            "scenario": args.scenario,
            "startedAt": started_at,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    finally:
        client.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
