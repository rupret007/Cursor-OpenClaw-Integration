#!/usr/bin/env python3
"""Send one First Officer span to a loopback Phoenix collector.

OpenLLMetry initializes the tracer. Its OpenInference processor maps the span
into the conventions Phoenix stores. Phoenix's HTTP exporter delivers it to
``/v1/traces``. The endpoint has to be loopback HTTP. Ambient collector URLs,
API keys and proxies are ignored so a trace cannot leave the machine or carry
a credential that happened to be in the environment.
"""

from contextlib import contextmanager
import os
from urllib.parse import urlsplit
import re


SPAN = "first-officer.lane"
LOOPBACK = ("localhost", "127.0.0.1", "::1")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA = re.compile(r"[0-9a-f]{40}")
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}|gh[pousr]_|Bearer |api[_-]?key", re.IGNORECASE)
FIELDS = {
    "fo.lane": NAME,
    "fo.head": SHA,
    "fo.backend": re.compile(r"hatchet|restate|none"),
    "fo.outcome": re.compile(r"queued|refused|unknown"),
}
REQUIRED = ("fo.lane", "fo.head")
AMBIENT = (
    "TRACELOOP_BASE_URL", "TRACELOOP_API_KEY", "TRACELOOP_HEADERS",
    "PHOENIX_COLLECTOR_ENDPOINT", "PHOENIX_API_KEY", "PHOENIX_CLIENT_HEADERS",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_PROTOCOL",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
)
_collector = None
_result = None


def collector_url(base_url):
    url = urlsplit(str(base_url))
    if (url.scheme != "http" or url.hostname not in LOOPBACK or url.username is not None or
            url.password is not None or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Phoenix must be a loopback HTTP collector")
    return str(base_url).rstrip("/")


def clean_attributes(attributes):
    if not isinstance(attributes, dict) or not attributes or not set(REQUIRED) <= set(attributes):
        raise ValueError("A trace needs its lane and head")
    if not set(attributes) <= set(FIELDS):
        raise ValueError("A trace attribute is not on the allowlist")
    cleaned = {}
    for key in sorted(attributes):
        value = attributes[key]
        if not isinstance(value, str) or SECRET.search(value) or not FIELDS[key].fullmatch(value):
            raise ValueError("A trace attribute is not usable")
        cleaned[key] = value
    return cleaned


@contextmanager
def _ignore_ambient_routes():
    saved = {key: os.environ.get(key) for key in AMBIENT}
    for key in AMBIENT:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _wire(url):
    global _result
    import requests
    from openinference.instrumentation.openllmetry import OpenInferenceSpanProcessor
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from phoenix.otel import HTTPSpanExporter, SimpleSpanProcessor
    from traceloop.sdk import Traceloop

    class Accepted(SpanExporter):
        def export(self, spans):
            global _result
            _result = self.inner.export(spans)
            return _result

        def shutdown(self):
            return self.inner.shutdown()

        def force_flush(self, timeout_millis=30000):
            return self.inner.force_flush(timeout_millis)

    session = requests.Session()
    session.trust_env = False
    exporter = HTTPSpanExporter(endpoint=url + "/v1/traces", headers={"user-agent": "first-officer"},
                                timeout=5, session=session)
    accepted = Accepted()
    accepted.inner = exporter
    Traceloop.init(
        app_name="first-officer",
        api_endpoint=url,
        disable_batch=True,
        telemetry_enabled=False,
        trace_content=False,
        instruments=set(),
        processor=[OpenInferenceSpanProcessor(), SimpleSpanProcessor(accepted)],
    )
    _result = SpanExportResult.FAILURE


def deliver(base_url, attributes):
    """Export one span. Raise unless Phoenix's collector accepts it."""
    global _collector, _result
    url = collector_url(base_url)
    cleaned = clean_attributes(attributes)
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SpanExportResult

    with _ignore_ambient_routes():
        if _collector is None:
            _wire(url)
            _collector = url
        elif _collector != url:
            raise ValueError("Phoenix is already collecting at a different loopback address")
        _result = None
        tracer = trace.get_tracer("first-officer")
        with tracer.start_as_current_span(SPAN) as span:
            for key, value in cleaned.items():
                span.set_attribute(key, value)
    if _result is not SpanExportResult.SUCCESS:
        raise ValueError("Phoenix did not accept the trace")
    return {"span": SPAN, "attributes": cleaned}


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collector", help="Loopback Phoenix base URL")
    parser.add_argument("attributes", type=Path, help="JSON object of allowlisted span attributes")
    args = parser.parse_args(argv)
    try:
        result = deliver(args.collector, json.loads(args.attributes.read_text(encoding="utf-8")))
        print(f"Phoenix accepted span {result['span']} for lane {result['attributes']['fo.lane']}.")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        message = str(error) if type(error) is ValueError else "The trace was not delivered"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
