from fastapi import FastAPI

from mybot.infrastructure import telemetry
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
