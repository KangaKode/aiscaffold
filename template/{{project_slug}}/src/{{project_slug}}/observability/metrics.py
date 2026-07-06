"""
Prometheus metrics -- optional, no-op without the [metrics] extra.

This is a LEAF module: it imports nothing from the rest of the package,
so any layer may call it without creating dependency cycles. Install the
optional extra to activate it:

    pip install '<project>[metrics]'

Without prometheus_client installed, every public function here is a
safe no-op (the instruments below are inert stand-ins), and the
GET /metrics/prometheus route returns 501 with an install hint. The
existing JSON GET /metrics endpoint is unrelated and always works.

LABEL POLICY (cardinality): label values MUST be bounded enums --
outcome, phase, provider, model, direction, action. Unbounded values
(tenant_id, user_id, session_id, correlation_id, task ids, free text)
are BANNED as label values: each distinct value creates a new
timeseries forever, which degrades and eventually breaks the metrics
backend. Per-tenant spend belongs in the budget_spend table (see
GET /api/v1/budgets/{tenant_id}), not in metric labels.

MULTI-WORKER CAVEAT: prometheus_client keeps its registry per process.
Under multiple uvicorn workers (e.g. `make serve-prod` with --workers 4)
each worker exposes only its own counts, and scrapes hit an arbitrary
worker. For aggregate numbers either run a single worker, or wire up
prometheus_client's multiprocess mode (PROMETHEUS_MULTIPROC_DIR + a
shared CollectorRegistry) -- that is a "you add" extension, see
docs/OPERATIONS.md.

Keep this file under 200 lines.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
    PROMETHEUS_CONTENT_TYPE = CONTENT_TYPE_LATEST
except ImportError:
    PROMETHEUS_AVAILABLE = False
    PROMETHEUS_CONTENT_TYPE = "text/plain; charset=utf-8"


class _NoopInstrument:
    """Inert stand-in so record functions never branch on availability."""

    def labels(self, **_labels) -> "_NoopInstrument":
        return self

    def inc(self, _amount: float = 1.0) -> None:
        return None

    def observe(self, _value: float) -> None:
        return None


# Deliberation phases run 1-100+ seconds, so buckets stretch far beyond
# the prometheus_client defaults (which top out at 10s).
DURATION_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

if PROMETHEUS_AVAILABLE:
    _deliberations_total = Counter(
        "deliberations_total",
        "Round table deliberations by outcome",
        ["outcome"],
    )
    _deliberation_duration = Histogram(
        "deliberation_duration_seconds",
        "End-to-end round table deliberation duration",
        buckets=DURATION_BUCKETS,
    )
    _phase_duration = Histogram(
        "phase_duration_seconds",
        "Per-phase deliberation duration",
        ["phase"],
        buckets=DURATION_BUCKETS,
    )
    _llm_calls_total = Counter(
        "llm_calls_total",
        "LLM calls by provider and model",
        ["provider", "model"],
    )
    _llm_tokens_total = Counter(
        "llm_tokens_total",
        "LLM tokens by direction (input/output)",
        ["direction"],
    )
    _llm_cost_dollars_total = Counter(
        "llm_cost_dollars_total",
        "Estimated LLM spend in USD (all tenants combined)",
    )
    _corrections_lifecycle_total = Counter(
        "corrections_lifecycle_total",
        "Correction lifecycle transitions by action",
        ["action"],
    )
else:
    _noop = _NoopInstrument()
    _deliberations_total = _noop
    _deliberation_duration = _noop
    _phase_duration = _noop
    _llm_calls_total = _noop
    _llm_tokens_total = _noop
    _llm_cost_dollars_total = _noop
    _corrections_lifecycle_total = _noop


def record_deliberation(outcome: str, duration_seconds: float) -> None:
    """One finished round table run. outcome is a bounded enum
    ("completed", "refused", "failed"). Never raises."""
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        _deliberations_total.labels(outcome=outcome or "unknown").inc()
        if duration_seconds > 0:
            _deliberation_duration.observe(float(duration_seconds))
    except Exception as exc:
        logger.warning(f"[Metrics] record_deliberation failed: {exc}")


def record_phase(phase: str, duration_seconds: float) -> None:
    """One deliberation phase (bounded enum: strategy, analysis, ...).
    Never raises."""
    if not PROMETHEUS_AVAILABLE or not phase:
        return
    try:
        _phase_duration.labels(phase=phase).observe(
            max(0.0, float(duration_seconds))
        )
    except Exception as exc:
        logger.warning(f"[Metrics] record_phase failed: {exc}")


def record_llm_call(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """One LLM call: count, tokens by direction, estimated spend.
    provider/model are bounded (a deployment uses a handful). Never raises."""
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        _llm_calls_total.labels(
            provider=provider or "unknown", model=model or "unknown"
        ).inc()
        if input_tokens > 0:
            _llm_tokens_total.labels(direction="input").inc(input_tokens)
        if output_tokens > 0:
            _llm_tokens_total.labels(direction="output").inc(output_tokens)
        if cost_usd > 0:
            _llm_cost_dollars_total.inc(cost_usd)
    except Exception as exc:
        logger.warning(f"[Metrics] record_llm_call failed: {exc}")


def record_correction_lifecycle(action: str) -> None:
    """One correction lifecycle transition (bounded enum: propose,
    approve, reject, retire, revalidate). Never raises."""
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        _corrections_lifecycle_total.labels(action=action or "unknown").inc()
    except Exception as exc:
        logger.warning(f"[Metrics] record_correction_lifecycle failed: {exc}")


def render_prometheus() -> bytes:
    """Serialized metrics in Prometheus text format (b"" when the
    [metrics] extra is not installed)."""
    if not PROMETHEUS_AVAILABLE:
        return b""
    return generate_latest()
