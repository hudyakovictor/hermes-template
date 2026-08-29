"""Small structured logger, with no dependency on python-json-logger."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "asctime": self.formatTime(record, self.datefmt),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for key in ("event", "region_id", "hypothesis_id", "run_id", "error"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", log_format: str = "json") -> logging.Logger:
    """Configure the package logger once and return it."""

    logger = logging.getLogger("researchagen.bottom_detection")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        # Keep stdout machine-readable for every ``--json`` CLI command.
        handler = logging.StreamHandler(sys.stderr)
        if log_format.lower() == "json":
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
        logger.addHandler(handler)
    return logger


def log_extra(**values: Any) -> dict[str, Any]:
    """Return a typed ``extra`` mapping for the stdlib logging API."""

    return values
