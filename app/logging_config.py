"""
Structured logging. Call setup_logging() once at process start (API and
worker both do this). Everywhere else, just use logging.getLogger(name)
and pass identifiers via `extra={...}` — this formatter renders them into
the JSON line. Never pass customer secrets/credentials/PII into `extra`.
"""
from __future__ import annotations

import json
import logging
import sys

# Fields the spec explicitly asks for in structured logs. Anything passed
# via `extra` that isn't one of these still gets included (structured
# logs should be able to carry ad-hoc context), but these are the ones
# every booking/search-related log line should try to include when known.
_TRACKED_FIELDS = (
    "request_id",
    "workflow_id",
    "booking_id",
    "supplier",
    "supplier_booking_ref",
)

_RESERVED = set(logging.LogRecord(
    "", 0, "", 0, "", (), None
).__dict__.keys()) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        return json.dumps(payload, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)