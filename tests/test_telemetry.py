from uuid import UUID

from fastapi import FastAPI
from opentelemetry import trace

from mybot.infrastructure import telemetry
from mybot.operator.api import render_prometheus
from mybot.settings import Settings


def test_blank_otel_endpoint_does_not_construct_exporter(monkeypatch) -> None:
    exporter_constructed = False

    def fail_if_constructed(*_args, **_kwargs):
        nonlocal exporter_constructed
        exporter_constructed = True
        raise AssertionError("blank OTLP endpoint must not construct an exporter")

    monkeypatch.setenv("MYBOT_OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", fail_if_constructed)
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", lambda _provider: None)
    monkeypatch.setattr(telemetry.FastAPIInstrumentor, "instrument_app", lambda _app: None)

    telemetry.configure_telemetry(FastAPI(), Settings(), service_name="test-api")

    assert exporter_constructed is False


def test_uuid_trace_id_is_preserved_for_external_and_database_alignment() -> None:
    trace_id = "12345678-1234-5678-1234-567812345678"

    parent = telemetry._trace_parent(trace_id)

    assert parent is not None
    assert trace.get_current_span(parent).get_span_context().trace_id == UUID(trace_id).int


def test_prometheus_renderer_flattens_shared_operator_metrics() -> None:
    rendered = render_prometheus(
        {
            "queues": {"ingest": 3, "ingest_dead_letter": 1},
            "turns": {"avg_latency_ms": 42.5},
        }
    )

    assert "# TYPE mybot_queues_ingest gauge" in rendered
    assert "mybot_queues_ingest 3" in rendered
    assert "mybot_turns_avg_latency_ms 42.5" in rendered
