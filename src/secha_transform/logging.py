"""Structured logging setup (JSON), matching the rest of the SECHA system."""

from __future__ import annotations

import logging

import structlog


def configure(level: int = logging.INFO) -> None:
    """Configure structlog for JSON output. Safe to call from the CLI entrypoint."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        cache_logger_on_first_use=True,
    )
