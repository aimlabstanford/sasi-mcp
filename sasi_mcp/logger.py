"""Structured logging — JSON to a rotating log file + plain to stdout.

Vendored from ledger_bridge.logger; kept identical so behavior matches what
we already operate against with the AppleScript reader.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import cast

import structlog


def configure_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level_num = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level_num)
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "sasi-mcp.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level_num)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level_num)

    fmt = logging.Formatter("%(message)s")
    file_handler.setFormatter(fmt)
    stream_handler.setFormatter(fmt)

    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_num),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
