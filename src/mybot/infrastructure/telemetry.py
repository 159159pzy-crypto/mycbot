"""OpenTelemetry bootstrap and manual spans shared by every process role."""

import secrets
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import (  # pyright: ignore[reportMissingTypeStubs]
    FastAPIInstrumentor,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from mybot.settings import Settings

_configured = False


def configure_telemetry(app: FastAPI, settings: Settings, *, service_name: str) -> None:
    configure_process_telemetry(settings, service_name=service_name)
    FastAPIInstrumentor.instrument_app(app)


def configure_process_telemetry(settings: Settings, *, service_name: str) -> None:
    global _configured
    if not _configured:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if (
            settings.otel_exporter_otlp_endpoint is not None
            and settings.otel_exporter_otlp_endpoint.get_secret_value().strip()
        ):
            exporter = OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint.get_secret_value()
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _configured = True


@contextmanager
def start_span(
    name: str,
    *,
    trace_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Generator[trace.Span]:
    with trace.get_tracer("mybot").start_as_current_span(
        name,
        context=_trace_parent(trace_id),
        attributes=dict(attributes or {}),
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


def _trace_parent(trace_id: str | None) -> context.Context | None:
    if trace_id is None:
        return None
    try:
        value = UUID(trace_id).int
    except ValueError:
        return None
    if value == 0:
        return None
    span_context = SpanContext(
        trace_id=value,
        span_id=secrets.randbits(64) or 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context))


__all__ = ["configure_process_telemetry", "configure_telemetry", "start_span"]
