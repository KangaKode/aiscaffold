"""Observability -- optional Prometheus metrics (leaf module, no internal imports)."""
from .metrics import (  # noqa: F401
    PROMETHEUS_AVAILABLE,
    PROMETHEUS_CONTENT_TYPE,
    record_correction_lifecycle,
    record_deliberation,
    record_llm_call,
    record_phase,
    render_prometheus,
)

__all__ = [
    "PROMETHEUS_AVAILABLE",
    "PROMETHEUS_CONTENT_TYPE",
    "record_correction_lifecycle",
    "record_deliberation",
    "record_llm_call",
    "record_phase",
    "render_prometheus",
]
