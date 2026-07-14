"""OpenTelemetry bootstrap kept separate from application construction."""

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import (  # pyright: ignore[reportMissingTypeStubs]
    FastAPIInstrumentor,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from mybot.settings import Settings

_configured = False


def configure_telemetry(app: FastAPI, settings: Settings, *, service_name: str) -> None:
    global _configured
    if not _configured:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if settings.otel_exporter_otlp_endpoint is not None:
            exporter = OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint.get_secret_value()
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _configured = True
    FastAPIInstrumentor.instrument_app(app)
