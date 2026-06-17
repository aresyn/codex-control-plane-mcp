from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


_CONFIGURED = False


def configure_logging(base_dir: Path | None = None) -> Path:
    """Configure file-only diagnostics without writing anything to MCP stdout."""
    global _CONFIGURED
    base_dir = base_dir or Path.cwd()
    log_path = Path(os.environ.get("CODEX_CONTROL_PLANE_MCP_LOG") or os.environ.get("OPENCLAW_CODEX_MCP_LOG") or (base_dir / "logs" / "server.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("openclaw_codex_mcp")
    root.setLevel(logging.INFO)
    root.propagate = False

    if not _CONFIGURED:
        handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s pid=%(process)d %(name)s: %(message)s")
        )
        root.addHandler(handler)
        _CONFIGURED = True

    root.info("logging configured path=%s python=%s argv=%s cwd=%s", log_path, sys.version.split()[0], sys.argv, os.getcwd())
    return log_path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"openclaw_codex_mcp.{name}")
