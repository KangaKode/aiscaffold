"""Data models for the evidence enforcement pipeline."""

from dataclasses import dataclass, field


@dataclass
class Violation:
    """A single enforcement violation found in an agent response.

    Attributes:
        rule: Category of violation (e.g. "banned_pattern", "missing_citation").
        severity: "critical" triggers rejection; "warning" attaches a flag.
        message: Human-readable explanation of what's wrong.
        location: The text fragment that triggered the violation.
        suggestion: How to fix it.
    """

    rule: str
    severity: str  # "critical" or "warning"
    message: str
    location: str = ""
    suggestion: str = ""


@dataclass
class ValidationResult:
    """Result of running the enforcement pipeline on an agent response.

    Attributes:
        outcome: "accepted" (no issues), "challenged" (warnings attached),
                 or "rejected" (critical issues found).
        violations: All violations found.
        corrected_content: Corrected text when optional LLM correction is configured.
    """

    outcome: str = "accepted"  # "accepted", "challenged", "rejected"
    violations: list[Violation] = field(default_factory=list)
    corrected_content: str | None = None
