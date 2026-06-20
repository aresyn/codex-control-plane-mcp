from __future__ import annotations

from . import tools as _tools
from .active_work import worker_active_turns_snapshot

globals().update(_tools.__dict__)


class RuntimeServiceMixin:
    async def codex_restart_app_server(self, args: dict[str, Any]) -> dict[str, Any]:
        start_after_restart = bool(args.get("start_after_restart", True))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", 30), 1, 120)
        force = bool(args.get("force", False))
        from_repair = bool(args.get("_from_repair", False))
        if self._app_server is None:
            if not start_after_restart:
                return {
                    "ok": True,
                    "restarted": False,
                    "started": False,
                    "before_pid": None,
                    "after_pid": None,
                    "active_work": {"pending_requests": 0, "active_turns": []},
                    "activeWork": {"pendingRequests": 0, "activeTurns": []},
                }
            self._app_server = CodexAppServerClient(self.config, self.storage)
        result = await self._app_server.restart(start_after_restart=start_after_restart, timeout_seconds=timeout_seconds, force=force)
        if not from_repair:
            action = "force_restart_app_server" if force else "restart_app_server_idle"
            loop_guard = self._record_guidance_attempt(
                action=action,
                args=args,
                result=result,
                status="succeeded" if result.get("ok", True) else "failed",
                count_attempt=True,
                force=force,
            )
            result["loopGuard"] = loop_guard
            post_guidance = build_post_repair_guidance(
                {"changed": bool(result.get("restarted") or result.get("started")), **result},
                action=action,
                scope_type=loop_guard["scopeType"],
                scope_id=loop_guard["scopeId"],
                loop_guard=loop_guard,
            )
            result["postRepairGuidance"] = post_guidance
            result["postRepairGuidanceText"] = guidance_text(post_guidance)
        return result

    def codex_get_app_server_status(self, args: dict[str, Any]) -> dict[str, Any]:
        include_recent_events = bool(args.get("include_recent_events", False))
        if self.config.execution_mode in {"client", "observe"}:
            active_snapshot = worker_active_turns_snapshot(self.storage)
            worker_active_turns = active_snapshot["activeTurns"]
            worker = _runtime_live_worker_snapshot(self.storage)
            latest_worker = worker or _runtime_latest_worker_snapshot(self.storage)
            heartbeat_age = _worker_heartbeat_age_seconds(latest_worker)
            heartbeat_stale = bool(latest_worker and heartbeat_age is not None and heartbeat_age >= 120)
            if worker is not None:
                local_status = (
                    self._app_server.status_snapshot(include_recent_events=False)
                    if self._app_server is not None
                    else None
                )
                result = {
                    "ok": True,
                    "running": True,
                    "started": False,
                    "scope": "worker_managed",
                    "workerManaged": True,
                    "pid": worker.get("pid"),
                    "workerId": worker.get("worker_id"),
                    "processGeneration": worker.get("app_server_generation") or 0,
                    "pendingRequests": 0,
                    "activeTurns": worker_active_turns,
                    "activeTurnCount": len(worker_active_turns),
                    "workerDerivedActiveTurns": worker_active_turns,
                    "appServerReportedActiveTurns": [],
                    "appServerLiveState": "worker_live",
                    "workerState": {
                        "workerId": worker.get("worker_id"),
                        "status": worker.get("status"),
                        "heartbeatAgeSeconds": heartbeat_age,
                        "heartbeatStale": False,
                    },
                    "workerHeartbeatStale": False,
                    "staleActiveRecordsExcluded": active_snapshot["staleActiveRecordsExcluded"],
                    "codexBinaryPath": str(self.config.codex_binary_path),
                    "codexBinaryExists": self.config.codex_binary_path.exists(),
                }
                if local_status is not None:
                    result["ignoredLocalProcess"] = {
                        "running": local_status.get("running"),
                        "pid": local_status.get("pid"),
                        "processGeneration": local_status.get("processGeneration"),
                    }
                return result
            local_status = (
                self._app_server.status_snapshot(include_recent_events=False)
                if self._app_server is not None
                else None
            )
            result = {
                "ok": True,
                "running": False,
                "started": False,
                "scope": "worker_managed",
                "workerManaged": True,
                "workerId": (latest_worker or {}).get("worker_id"),
                "pid": (latest_worker or {}).get("pid"),
                "processGeneration": (latest_worker or {}).get("app_server_generation") or 0,
                "pendingRequests": 0,
                "activeTurns": worker_active_turns,
                "activeTurnCount": len(worker_active_turns),
                "workerDerivedActiveTurns": worker_active_turns,
                "appServerReportedActiveTurns": [],
                "appServerLiveState": "unknown_stale_worker" if latest_worker else "unknown_no_worker",
                "workerState": {
                    "workerId": (latest_worker or {}).get("worker_id"),
                    "status": (latest_worker or {}).get("status"),
                    "heartbeatAgeSeconds": heartbeat_age,
                    "heartbeatStale": heartbeat_stale,
                },
                "workerHeartbeatStale": heartbeat_stale,
                "staleActiveRecordsExcluded": active_snapshot["staleActiveRecordsExcluded"],
                "codexBinaryPath": str(self.config.codex_binary_path),
                "codexBinaryExists": self.config.codex_binary_path.exists(),
                "warning": "No live worker is registered for this client-mode MCP process.",
            }
            if local_status is not None:
                result["ignoredLocalProcess"] = {
                    "running": local_status.get("running"),
                    "pid": local_status.get("pid"),
                    "processGeneration": local_status.get("processGeneration"),
                }
            return result
        if self._app_server is None:
            return {
                "ok": True,
                "running": False,
                "started": False,
                "scope": "local_process",
                "workerManaged": False,
                "pid": None,
                "processGeneration": 0,
                "pendingRequests": 0,
                "activeTurns": [],
                "codexBinaryPath": str(self.config.codex_binary_path),
                "codexBinaryExists": self.config.codex_binary_path.exists(),
            }
        return self._app_server.status_snapshot(include_recent_events=include_recent_events)

    async def codex_get_runtime_capabilities(self, args: dict[str, Any]) -> dict[str, Any]:
        refresh = bool(args.get("refresh", False))
        include_models = bool(args.get("include_models", True))
        include_hooks = bool(args.get("include_hooks", True))
        include_skills = bool(args.get("include_skills", True))
        include_account = bool(args.get("include_account", True))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", 2), 1, 30)
        cwd = _optional_string(args.get("cwd")) or str(self.config.projects_root)
        if not is_allowed_path(cwd, self.config.allowed_roots):
            raise invalid_argument("cwd is outside configured allowed roots.", cwd=redact_text(cwd, max_chars=300))
        cache_key = json.dumps(
            {
                "cwd": path_key(cwd),
                "includeModels": include_models,
                "includeHooks": include_hooks,
                "includeSkills": include_skills,
                "includeAccount": include_account,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now_monotonic = time.monotonic()
        if (
            not refresh
            and self._runtime_capabilities_cache is not None
            and self._runtime_capabilities_cache_key == cache_key
            and self._runtime_capabilities_cache_at is not None
            and now_monotonic - self._runtime_capabilities_cache_at < RUNTIME_CAPABILITIES_CACHE_TTL_SECONDS
        ):
            cached = copy.deepcopy(self._runtime_capabilities_cache)
            cached["cacheState"] = {
                "hit": True,
                "ageSeconds": int(now_monotonic - self._runtime_capabilities_cache_at),
                "ttlSeconds": RUNTIME_CAPABILITIES_CACHE_TTL_SECONDS,
                "cacheKey": hashlib.sha256(cache_key.encode("utf-8")).hexdigest(),
            }
            return cached

        generated_at = runtime_now_iso()
        method_results: dict[str, dict[str, Any]] = {}
        warnings: list[dict[str, Any]] = []
        app_status_before = self.codex_get_app_server_status({"include_recent_events": False})
        if self.config.execution_mode in {"client", "observe"} and self._app_server is None:
            reason = f"execution_mode={self.config.execution_mode}; live inventory is owned by the worker process"
            worker_snapshot = self._worker_runtime_snapshot_for_client()
            cached_worker_snapshot = self._latest_runtime_capabilities_snapshot()
            if cached_worker_snapshot is not None:
                cached_result = copy.deepcopy(cached_worker_snapshot["payload"])
                runtime_capabilities = cached_result.get("runtimeCapabilities")
                if isinstance(runtime_capabilities, dict):
                    runtime_capabilities["cacheSource"] = "worker_status_snapshot"
                    runtime_capabilities.setdefault("workerRuntimeSnapshot", worker_snapshot)
                cached_result["cacheState"] = {
                    "hit": True,
                    "source": "worker_status_snapshot",
                    "ageSeconds": cached_worker_snapshot["ageSeconds"],
                    "ttlSeconds": RUNTIME_CAPABILITIES_CACHE_TTL_SECONDS,
                    "cacheKey": hashlib.sha256(cache_key.encode("utf-8")).hexdigest(),
                }
                cached_result.setdefault("recommendedPollAfterSeconds", 0)
                cached_result.setdefault("pollRecommended", False)
                self._runtime_capabilities_cache = copy.deepcopy(cached_result)
                self._runtime_capabilities_cache_key = cache_key
                self._runtime_capabilities_cache_at = time.monotonic()
                return cached_result

            def skip_live_inventory(method: str) -> None:
                method_results[method] = {"status": "skipped", "reason": reason, "elapsedMs": 0}

            for method in (
                "model/list",
                "permissionProfile/list",
                "windowsSandbox/readiness",
                "hooks/list",
                "skills/list",
                "modelProvider/capabilities/read",
                "account/read",
                "account/usage/read",
                "account/rateLimits/read",
            ):
                skip_live_inventory(method)
            warnings.append(
                {
                    "method": "runtime/inventory",
                    "code": "WORKER_MANAGED_RUNTIME",
                    "status": "skipped",
                    "message": "Runtime inventory was not collected in client/observe mode because the worker owns codex-app-server.",
                    "retryable": False,
                }
            )
            runtime_capabilities = {
                "status": "passive",
                "cacheSource": "worker_registry" if worker_snapshot else "none",
                "workerRuntimeSnapshot": worker_snapshot,
                "generatedAt": generated_at,
                "appServer": {
                    "running": app_status_before.get("running"),
                    "started": app_status_before.get("started"),
                    "workerManaged": app_status_before.get("workerManaged"),
                    "workerId": app_status_before.get("workerId"),
                    "processGeneration": app_status_before.get("processGeneration"),
                    "initialize": None,
                },
                "schemaMethods": schema_methods_block(),
                "models": None,
                "permissionProfiles": None,
                "sandboxReadiness": None,
                "hooks": None,
                "skills": None,
                "modelProviderCapabilities": None,
                "accountStatus": None,
                "accountUsage": None,
                "rateLimits": None,
            }
            result = {
                "ok": True,
                "runtimeCapabilities": runtime_capabilities,
                "cacheState": {
                    "hit": False,
                    "source": "worker_registry" if worker_snapshot else "none",
                    "ageSeconds": 0,
                    "ttlSeconds": RUNTIME_CAPABILITIES_CACHE_TTL_SECONDS,
                    "cacheKey": hashlib.sha256(cache_key.encode("utf-8")).hexdigest(),
                },
                "methodResults": method_results,
                "warnings": warnings,
                "recommendedPollAfterSeconds": 0,
                "pollRecommended": False,
            }
            self._runtime_capabilities_cache = copy.deepcopy(result)
            self._runtime_capabilities_cache_key = cache_key
            self._runtime_capabilities_cache_at = time.monotonic()
            return result
        try:
            client = await self._app()
        except CodexMcpError as exc:
            warnings.append(
                {
                    "method": "app-server/start",
                    "code": exc.code,
                    "message": redact_text(exc.message, max_chars=500),
                    "retryable": exc.retryable,
                }
            )
            method_results["app-server/start"] = {
                "status": "error",
                "elapsedMs": 0,
                "errorCode": exc.code,
                "retryable": exc.retryable,
            }
            runtime_capabilities = {
                "status": "unavailable",
                "generatedAt": generated_at,
                "appServer": {
                    "running": False,
                    "started": app_status_before.get("started"),
                    "processGeneration": app_status_before.get("processGeneration"),
                    "initialize": None,
                },
                "schemaMethods": schema_methods_block(),
                "models": None,
                "permissionProfiles": None,
                "sandboxReadiness": None,
                "hooks": None,
                "skills": None,
                "modelProviderCapabilities": None,
                "accountStatus": None,
                "accountUsage": None,
                "rateLimits": None,
            }
            return {
                "ok": True,
                "runtimeCapabilities": runtime_capabilities,
                "cacheState": {
                    "hit": False,
                    "ageSeconds": 0,
                    "ttlSeconds": RUNTIME_CAPABILITIES_CACHE_TTL_SECONDS,
                    "cacheKey": hashlib.sha256(cache_key.encode("utf-8")).hexdigest(),
                },
                "methodResults": method_results,
                "warnings": warnings,
                "recommendedPollAfterSeconds": 0,
                "pollRecommended": False,
            }

        async def run_method(method: str, call: Any, compact: Any) -> Any:
            started = time.monotonic()
            try:
                raw = await call()
                method_results[method] = {"status": "ok", "elapsedMs": int((time.monotonic() - started) * 1000)}
                return compact(raw)
            except CodexMcpError as exc:
                status = "timeout" if exc.code == "CODEX_TIMEOUT" else "error"
                method_results[method] = {
                    "status": status,
                    "elapsedMs": int((time.monotonic() - started) * 1000),
                    "errorCode": exc.code,
                    "retryable": exc.retryable,
                }
                warnings.append(
                    {
                        "method": method,
                        "code": exc.code,
                        "status": status,
                        "message": redact_text(exc.message, max_chars=500),
                        "retryable": exc.retryable,
                    }
                )
                return None
            except Exception as exc:  # noqa: BLE001 - inventory must be best-effort.
                method_results[method] = {
                    "status": "error",
                    "elapsedMs": int((time.monotonic() - started) * 1000),
                    "errorCode": type(exc).__name__,
                    "retryable": False,
                }
                warnings.append(
                    {
                        "method": method,
                        "code": type(exc).__name__,
                        "status": "error",
                        "message": redact_text(str(exc), max_chars=500),
                        "retryable": False,
                    }
                )
                return None

        def skip_method(method: str, reason: str) -> None:
            method_results[method] = {"status": "skipped", "reason": reason, "elapsedMs": 0}

        models = None
        if include_models:
            models = await run_method(
                "model/list",
                lambda: client.model_list(limit=100, include_hidden=True, timeout_seconds=timeout_seconds),
                compact_models,
            )
        else:
            skip_method("model/list", "include_models=false")

        permission_profiles = await run_method(
            "permissionProfile/list",
            lambda: client.permission_profile_list(cwd=cwd, limit=100, timeout_seconds=timeout_seconds),
            compact_permission_profiles,
        )
        sandbox_readiness = await run_method(
            "windowsSandbox/readiness",
            lambda: client.windows_sandbox_readiness(timeout_seconds=timeout_seconds),
            compact_sandbox_readiness,
        )
        hooks = None
        if include_hooks:
            hooks = await run_method(
                "hooks/list",
                lambda: client.hooks_list(cwds=[cwd], timeout_seconds=timeout_seconds),
                compact_hooks,
            )
        else:
            skip_method("hooks/list", "include_hooks=false")

        skills = None
        if include_skills:
            skills = await run_method(
                "skills/list",
                lambda: client.skills_list(cwds=[cwd], force_reload=refresh, timeout_seconds=timeout_seconds),
                compact_skills,
            )
        else:
            skip_method("skills/list", "include_skills=false")

        provider_capabilities = await run_method(
            "modelProvider/capabilities/read",
            lambda: client.model_provider_capabilities_read(timeout_seconds=timeout_seconds),
            compact_provider_capabilities,
        )
        account_status = None
        account_usage = None
        rate_limits = None
        if include_account:
            account_status = await run_method(
                "account/read",
                lambda: client.account_read(refresh_token=False, timeout_seconds=timeout_seconds),
                compact_account_status,
            )
            if account_status and account_status.get("authenticated"):
                account_usage = await run_method(
                    "account/usage/read",
                    lambda: client.account_usage_read(timeout_seconds=timeout_seconds),
                    compact_account_usage,
                )
                rate_limits = await run_method(
                    "account/rateLimits/read",
                    lambda: client.account_rate_limits_read(timeout_seconds=timeout_seconds),
                    compact_rate_limits,
                )
            elif account_status is not None:
                skip_method("account/usage/read", "unauthenticated")
                skip_method("account/rateLimits/read", "unauthenticated")
            else:
                skip_method("account/usage/read", "account_status_unavailable")
                skip_method("account/rateLimits/read", "account_status_unavailable")
        else:
            skip_method("account/read", "include_account=false")
            skip_method("account/usage/read", "include_account=false")
            skip_method("account/rateLimits/read", "include_account=false")

        app_status_after = self.codex_get_app_server_status({"include_recent_events": False})
        initialize = compact_initialize_result(getattr(client, "initialize_result", None))
        runtime_capabilities = {
            "status": "partial" if warnings else "ok",
            "generatedAt": generated_at,
            "appServer": {
                "running": app_status_after.get("running"),
                "started": app_status_after.get("started"),
                "processGeneration": app_status_after.get("processGeneration"),
                "platform": initialize.get("platform"),
                "userAgent": initialize.get("userAgent"),
                "initialize": initialize,
            },
            "schemaMethods": schema_methods_block(),
            "models": models,
            "permissionProfiles": permission_profiles,
            "sandboxReadiness": sandbox_readiness,
            "hooks": hooks,
            "skills": skills,
            "modelProviderCapabilities": provider_capabilities,
            "accountStatus": account_status,
            "accountUsage": account_usage,
            "rateLimits": rate_limits,
        }
        result = {
            "ok": True,
            "runtimeCapabilities": runtime_capabilities,
            "cacheState": {
                "hit": False,
                "ageSeconds": 0,
                "ttlSeconds": RUNTIME_CAPABILITIES_CACHE_TTL_SECONDS,
                "cacheKey": hashlib.sha256(cache_key.encode("utf-8")).hexdigest(),
            },
            "methodResults": method_results,
            "warnings": warnings,
            "recommendedPollAfterSeconds": 0,
            "pollRecommended": False,
        }
        result = redact_payload(result)
        self._runtime_capabilities_cache = copy.deepcopy(result)
        self._runtime_capabilities_cache_key = cache_key
        self._runtime_capabilities_cache_at = time.monotonic()
        return result

    async def codex_preflight_project_run(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = _optional_string(args.get("project_id"))
        cwd = _optional_string(args.get("cwd"))
        if project_id and not cwd:
            project = self.catalog.get_project(project_id)
            if project is None:
                raise project_not_found(project_id)
            cwd = project.path
        if not cwd:
            cwd = str(self.config.projects_root)
        if not project_id:
            project_id = project_id_for_path(cwd)
        if not is_allowed_path(cwd, self.config.allowed_roots):
            raise invalid_argument("cwd is outside configured allowed roots.", cwd=redact_text(cwd, max_chars=300))

        timeout_seconds = _bounded_int(args.get("timeout_seconds", 30), 1, 300)
        sandbox = _optional_string(args.get("sandbox")) or _sandbox_value_from_policy(self.config.default_sandbox_policy)
        approval_policy = _optional_string(args.get("approval_policy")) or self.config.default_approval_policy
        model = _optional_string(args.get("model")) or self.config.default_model
        checks: list[dict[str, Any]] = []

        def add_check(name: str, status: str, message: str, **details: Any) -> None:
            checks.append({"name": name, "status": status, "message": message, "details": redact_payload(details)})

        cwd_path = Path(cwd)
        add_check("cwd_exists", "ok" if cwd_path.exists() else "error", "Project cwd exists." if cwd_path.exists() else "Project cwd does not exist.", cwd=str(cwd_path))
        add_check("allowed_root", "ok", "Project cwd is inside CODEX_ALLOWED_ROOTS.", cwd=str(cwd_path))
        add_check(
            "codex_home",
            "ok" if self.config.codex_home.exists() else "warning",
            "Configured CODEX_HOME exists." if self.config.codex_home.exists() else "Configured CODEX_HOME does not exist yet.",
            codexHome=str(self.config.codex_home),
        )
        add_check(
            "codex_binary",
            "ok" if self.config.codex_binary_path.exists() else "error",
            "Codex binary exists." if self.config.codex_binary_path.exists() else "Codex binary was not found.",
            codexBinaryPath=str(self.config.codex_binary_path),
        )
        add_check(
            "auth_file",
            "ok" if (self.config.codex_home / "auth.json").exists() else "warning",
            "Codex auth.json is present." if (self.config.codex_home / "auth.json").exists() else "Codex auth.json was not found.",
        )
        hook_status = self._hook_history_snapshot()
        add_check(
            "hooks",
            "ok" if hook_status.get("installed") and hook_status.get("dbWritable") else "warning",
            "Codex MCP hooks are installed." if hook_status.get("installed") else "Codex MCP hooks are not installed.",
            hookStatus=hook_status,
        )

        capabilities = await self.codex_get_runtime_capabilities(
            {
                "refresh": bool(args.get("refresh", False)),
                "cwd": cwd,
                "timeout_seconds": min(timeout_seconds, 10),
                "include_models": True,
                "include_hooks": True,
                "include_skills": False,
                "include_account": True,
            }
        )
        runtime = capabilities.get("runtimeCapabilities") or {}
        account = runtime.get("accountStatus") if isinstance(runtime.get("accountStatus"), dict) else {}
        auth_file_present = (self.config.codex_home / "auth.json").exists()
        worker_managed_runtime = self.config.execution_mode in {"client", "observe"}
        worker_managed_passive = worker_managed_runtime and runtime.get("status") in {
            "passive",
            "not_collected",
            "unknown",
            "stale",
            "cached",
            "ok",
        }
        if account.get("authenticated"):
            account_status = "ok"
            account_message = "Codex account is authenticated."
        elif worker_managed_passive and auth_file_present:
            account_status = "skipped"
            account_message = "Codex account inventory is worker-managed; auth.json is present, so authentication is unknown from this client process."
            account = {"authenticated": None, "status": "unknown_worker_managed", "skippedReason": "worker_managed_passive_inventory"}
        elif worker_managed_runtime and auth_file_present:
            account_status = "skipped"
            account_message = "Codex account inventory is worker-managed; auth.json is present, so this client does not block on stale account state."
            account = {"authenticated": None, "status": "skipped_worker_managed", "skippedReason": "worker_managed_account_inventory"}
        else:
            account_status = "error"
            account_message = "Codex account is not authenticated."
        add_check(
            "account",
            account_status,
            account_message,
            accountStatus=account,
        )

        live_probe = bool(args.get("live_probe", False))
        probe_operation = None
        if live_probe:
            probe_operation = self.codex_submit_task(
                {
                    "operation_type": "start_chat",
                    "project_id": project_id,
                    "cwd": cwd,
                    "message": "MCP PREFLIGHT / DO NOT MODIFY FILES\nConfirm briefly that this Codex turn can read the current workspace. Do not modify files.",
                    "title": "MCP preflight probe",
                    "model": model,
                    "sandbox": sandbox,
                    "approval_policy": approval_policy,
                    "client_request_id": f"preflight:{path_key(cwd)}:{int(time.time())}",
                    "timeout_seconds": timeout_seconds,
                    "first_message_max_chars": 2000,
                }
            )
            add_check(
                "live_probe",
                "ok" if probe_operation.get("ok") else "error",
                "Live probe operation was submitted." if probe_operation.get("ok") else "Live probe operation failed to submit.",
                operationId=probe_operation.get("operationId"),
                error=probe_operation.get("error"),
            )
        else:
            add_check("live_probe", "skipped", "live_probe=false; no Codex turn was started.")

        status = "ok"
        if any(item["status"] == "error" for item in checks):
            status = "error"
        elif any(item["status"] == "warning" for item in checks):
            status = "warning"
        result = {
            "ok": status != "error",
            "status": status,
            "cwd": str(cwd_path),
            "projectId": project_id,
            "workflowKind": _optional_string(args.get("workflow_kind")) or "plan",
            "model": model,
            "sandbox": sandbox,
            "approvalPolicy": approval_policy,
            "checks": checks,
            "runtimeCapabilities": runtime_health_subset(
                capabilities,
                cache_age_seconds=(capabilities.get("cacheState") or {}).get("ageSeconds")
                if isinstance(capabilities.get("cacheState"), dict)
                else None,
            ),
            "probeOperation": probe_operation,
            "nextRecommendedAction": "start_workflow" if status == "ok" else "inspect_diagnostics",
            "recommendedPollAfterSeconds": 0,
            "pollRecommended": False,
        }
        return self._attach_agent_guidance(result, surface="preflight_project_run")

    def _worker_runtime_snapshot_for_client(self) -> dict[str, Any] | None:
        worker = _runtime_live_worker_snapshot(self.storage)
        if worker is None:
            return None
        heartbeat_age: int | None = None
        try:
            heartbeat = datetime.fromisoformat(str(worker.get("last_heartbeat_at") or "").replace("Z", "+00:00"))
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            heartbeat_age = int((datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            heartbeat_age = None
        return {
            "workerId": worker.get("worker_id"),
            "role": worker.get("role"),
            "status": worker.get("status"),
            "pid": worker.get("pid"),
            "hostname": worker.get("hostname"),
            "lastHeartbeatAt": worker.get("last_heartbeat_at"),
            "heartbeatAgeSeconds": heartbeat_age,
            "activeOperationCount": worker.get("active_operation_count"),
            "activeTurnCount": worker.get("active_turn_count"),
            "appServerGeneration": worker.get("app_server_generation"),
        }

    def _latest_runtime_capabilities_snapshot(self) -> dict[str, Any] | None:
        row = self.storage.get_latest_status_snapshot("runtime_capabilities")
        if row is None:
            return None
        expires_at = _optional_string(row.get("expires_at"))
        if expires_at:
            try:
                expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                    return None
            except ValueError:
                return None
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        age_seconds = _staleness_seconds(str(row.get("created_at") or "")) or 0
        return {"payload": payload, "ageSeconds": age_seconds, "createdAt": row.get("created_at")}


def _runtime_live_worker_snapshot(storage: Any) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    for row in storage.list_workers(limit=20):
        if row.get("role") != "worker" or row.get("status") != "running":
            continue
        try:
            heartbeat = datetime.fromisoformat(str(row.get("last_heartbeat_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if int((now - heartbeat.astimezone(timezone.utc)).total_seconds()) < 120:
            return row
    return None


def _runtime_latest_worker_snapshot(storage: Any) -> dict[str, Any] | None:
    for row in storage.list_workers(limit=20):
        if row.get("role") == "worker":
            return row
    return None


def _worker_heartbeat_age_seconds(row: dict[str, Any] | None) -> int | None:
    if row is None:
        return None
    try:
        heartbeat = datetime.fromisoformat(str(row.get("last_heartbeat_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds())
