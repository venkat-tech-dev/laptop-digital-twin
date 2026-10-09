"""Structured JSON logging with rotation and secret redaction.

Every line carries: timestamp (UTC ISO-8601), level, component (logger name), event, and the bound
device/agent identifiers. Values under keys that look like credentials are replaced with
``[REDACTED]``; collectors never log telemetry values or process/user details.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog

_SENSITIVE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|ingest[_-]?key|credential)", re.I
)


def redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if _SENSITIVE.search(key):
            event_dict[key] = "[REDACTED]"
    return event_dict


def _rename_logger(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    name = event_dict.pop("logger", None)
    if name:
        event_dict["component"] = name
    return event_dict


def configure_logging(
    level: str, log_file: Path | None, *, max_mb: int = 5, backups: int = 5, console: bool = True
) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handlers: list[logging.Handler] = []
    if console and sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_mb * 1024 * 1024, backupCount=backups, encoding="utf-8"
            )
        )
    for h in handlers:
        h.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(h)
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            _rename_logger,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=False,
    )


def bind_identity(device_id: str, agent_id: str | None) -> None:
    structlog.contextvars.bind_contextvars(
        device_id=device_id, **({"agent_id": agent_id} if agent_id else {})
    )
