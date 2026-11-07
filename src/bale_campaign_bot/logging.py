"""JSON or console structlog setup with defensive secret-field redaction."""

from __future__ import annotations

import sys
from typing import Any

import structlog


def redact_secret_fields(
    _: Any,
    __: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    for key in tuple(event_dict):
        normalized_key = key.casefold()
        if any(
            fragment in normalized_key
            for fragment in ("token", "secret", "password", "authorization")
        ):
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging(*, json_logs: bool, log_level: str = "INFO") -> None:
    processors: list[Any] = [
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_log_level,
        redact_secret_fields,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        wrapper_class=structlog.make_filtering_bound_logger(log_level.upper()),
        cache_logger_on_first_use=True,
    )
