"""Tracing and structured logs, shared by the API, the agent and the MCP server.

Traces export over OTLP/HTTP only when OTEL_EXPORTER_OTLP_ENDPOINT is set, so tests and
local runs pay nothing. Every log line is one JSON object carrying the current run id.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

_configured = False


def setup_tracing(service_name: str) -> None:
    """Install an OTLP exporter once per process, if an endpoint is configured."""
    global _configured
    if _configured or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": run_id_var.get(),
        }
        if span.is_valid:
            payload["trace_id"] = format(span.trace_id, "032x")
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))


def log(logger: logging.Logger, msg: str, **fields: Any) -> None:
    logger.info(msg, extra={"fields": fields})
