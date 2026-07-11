"""
OpenTelemetry phase spans -- optional, no-op without the [otel] extra.

This is a LEAF module mirroring the metrics degradation pattern
(observability/metrics.py): it imports nothing from the rest of the
package, so any layer may call it without dependency cycles. Install
the optional extra to activate it:

    pip install '<project>[otel]'

Without opentelemetry-api installed, phase_span() returns a plain
contextlib.nullcontext -- zero overhead, nothing to configure. With
the API installed but NO tracer provider configured (the default),
spans are non-recording no-ops; the scaffold never configures a
provider or exporter for you. Configure one yourself (opentelemetry-sdk
plus the standard OTEL_* environment variables or explicit
set_tracer_provider) and per-phase spans appear with no further wiring.

Span naming follows the spirit of the OTel GenAI semantic conventions
(dot-separated component.phase names like "deliberation.phase.independent");
no convention compliance is claimed.

Keep this file under 150 lines.
"""

import logging
from contextlib import contextmanager, nullcontext

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


@contextmanager
def _span(span_name: str, attrs: dict):
    """Start a span, attach attributes when it records. Never raises
    around the wrapped block: span bookkeeping errors are logged."""
    try:
        tracer = trace.get_tracer(__name__)
        cm = tracer.start_as_current_span(span_name)
        span = cm.__enter__()
    except Exception as exc:
        logger.warning(f"[Tracing] span start failed (ignored): {exc}")
        yield None
        return
    try:
        if span.is_recording():
            for key, value in attrs.items():
                span.set_attribute(key, value)
    except Exception as exc:
        logger.warning(f"[Tracing] span attributes failed (ignored): {exc}")
    try:
        yield span
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception as exc:
            logger.warning(f"[Tracing] span end failed (ignored): {exc}")


def phase_span(phase_name: str, **attrs):
    """Context manager for one orchestration phase.

    phase_name is the full span name (e.g. "deliberation.phase.independent",
    "chat.phase.synthesize"); keyword arguments become span attributes.

    Degradation contract: without the [otel] extra this returns a plain
    nullcontext (inert); with the extra but no tracer provider
    configured, the span is non-recording (the OTel API default) and
    attributes are skipped. Either way the wrapped code runs unchanged
    and this function never raises.
    """
    if not OTEL_AVAILABLE:
        return nullcontext()
    return _span(phase_name, attrs)
