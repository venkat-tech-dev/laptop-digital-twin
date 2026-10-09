"""Optional OpenTelemetry tracing.

Enabled only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set *and* the ``otel`` extra is installed
(``pip install -e ".[otel]"``). Otherwise this is a no-op, keeping the default install lightweight.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI

from app.core.config import Settings

log = structlog.get_logger("tracing")


def setup_tracing(app: FastAPI, settings: Settings) -> bool:
    if not settings.otel_exporter_otlp_endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("otel_not_installed", hint='pip install -e ".[otel]"')
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": "ldt-backend"}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,metrics")
    log.info("otel_enabled", endpoint=settings.otel_exporter_otlp_endpoint)
    return True
