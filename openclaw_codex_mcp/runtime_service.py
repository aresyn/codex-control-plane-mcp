from __future__ import annotations

from . import tools as _tools

globals().update(_tools.__dict__)


class RuntimeServiceMixin:
    async def codex_restart_app_server(self, args: dict[str, Any]) -> dict[str, Any]:
        start_after_restart = bool(args.get("start_after_restart", True))
        timeout_seconds = _bounded_int(args.get("timeout_seconds", 30), 1, 120)
        force = bool(args.get("force", False))
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
        return await self._app_server.restart(start_after_restart=start_after_restart, timeout_seconds=timeout_seconds, force=force)

    def codex_get_app_server_status(self, args: dict[str, Any]) -> dict[str, Any]:
        include_recent_events = bool(args.get("include_recent_events", False))
        if self._app_server is None:
            return {
                "ok": True,
                "running": False,
                "started": False,
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

