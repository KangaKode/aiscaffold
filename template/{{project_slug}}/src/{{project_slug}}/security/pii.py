"""
PII redaction -- pattern-based detection and masking of personal data.

Redacts emails, phone numbers, SSNs, credit card numbers (Luhn-verified),
IP addresses, and names-in-context before text is persisted or logged.
Matches are replaced with `[REDACTED:<CATEGORY>]` tokens.

Design notes:
  - Text is Unicode-normalized (homoglyph mapping + invisible-char
    stripping) BEFORE matching, so obfuscated PII (e.g. a Cyrillic 'a'
    inside an email) is still caught. The returned text is the
    normalized form.
  - Idempotent: redact_pii(redact_pii(x)[0]) == redact_pii(x)[0].
    Replacement tokens contain no digits, no '@', and no lowercase
    letters, so no category pattern can re-match them.
  - Credit card candidates must pass a Luhn checksum, which filters out
    most random 13-19 digit sequences (order IDs, timestamps).
  - names_in_context is a deliberately modest heuristic: it only
    matches a capitalized First Last pair immediately following an
    honorific (Mr./Ms./Mrs./Dr./Prof.) or the phrase "name is". It will
    miss most names (single names, lowercase, other languages, no
    trigger phrase) and can rarely over-match capitalized non-names
    after a trigger. Do not rely on it as the only defense.

Layer 0 file: only imports from security/ and stdlib.
Keep this file under 250 lines.
"""

import re

from .injection_defense import normalize_unicode, strip_invisible_chars

# -- Category patterns ------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# US SSN: 3-2-4 digits, separators required to avoid eating arbitrary
# 9-digit numbers (zip+4, order ids).
_SSN_RE = re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")

# 13-19 digits with optional single space/dash separators; each
# candidate must also pass the Luhn check before being redacted.
_CREDIT_CARD_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")

_IP_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"
_IP_ADDRESS_RE = re.compile(rf"\b{_IP_OCTET}(?:\.{_IP_OCTET}){{3}}\b")

# US-style: optional +1 country code, 3-3-4 digits with ()/-/./space
# separators. International-ish: '+' followed by 8-15 digits with
# optional single separators (E.164-inspired, intentionally loose).
# Lookarounds keep matches out of longer digit runs and decimals while
# still allowing a sentence-ending period after the number. IPs are
# redacted before phones, so dotted quads never reach these patterns.
_PHONE_US_RE = re.compile(
    r"(?<![\d.])(?:\+?1[-. ]?)?(?:\(\d{3}\)|\d{3})[-. ]?\d{3}[-. ]?\d{4}(?!\d|\.\d)"
)
_PHONE_INTL_RE = re.compile(r"(?<![\d.])\+\d(?:[-. ]?\d){7,14}(?!\d|\.\d)")

# Capitalized First Last following an honorific or "name is". The
# trigger (group 1) is preserved; only the name (group 2) is redacted.
_NAME_HONORIFIC_RE = re.compile(
    r"\b((?:Mr|Ms|Mrs|Dr|Prof)\.?\s+)([A-Z][a-z]+\s+[A-Z][a-z]+)\b"
)
_NAME_PHRASE_RE = re.compile(
    r"\b([Nn]ame\s+is\s+)([A-Z][a-z]+\s+[A-Z][a-z]+)\b"
)

CATEGORIES = (
    "email",
    "ssn",
    "credit_card",
    "ip_address",
    "phone",
    "names_in_context",
)


def _token(label: str) -> str:
    return f"[REDACTED:{label}]"


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum -- filters non-card digit runs from credit_card hits."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_credit_cards(text: str) -> tuple[str, int]:
    count = 0

    def _replace(match: re.Match) -> str:
        nonlocal count
        digits = re.sub(r"[ -]", "", match.group())
        if _luhn_valid(digits):
            count += 1
            return _token("CREDIT_CARD")
        return match.group()

    return _CREDIT_CARD_RE.sub(_replace, text), count


def _redact_names(text: str) -> tuple[str, int]:
    count = 0

    def _replace(match: re.Match) -> str:
        nonlocal count
        count += 1
        return match.group(1) + _token("NAME")

    for pattern in (_NAME_HONORIFIC_RE, _NAME_PHRASE_RE):
        text = pattern.sub(_replace, text)
    return text, count


def _redact_simple(text: str, pattern: re.Pattern, label: str) -> tuple[str, int]:
    return pattern.subn(_token(label), text)


def _redact_phone(text: str) -> tuple[str, int]:
    text, us = _redact_simple(text, _PHONE_US_RE, "PHONE")
    text, intl = _redact_simple(text, _PHONE_INTL_RE, "PHONE")
    return text, us + intl


# Order matters: cards before phones so a 16-digit card is not partially
# eaten by the phone pattern; IPs before phones for the same reason.
_REDACTORS = {
    "email": lambda t: _redact_simple(t, _EMAIL_RE, "EMAIL"),
    "ssn": lambda t: _redact_simple(t, _SSN_RE, "SSN"),
    "credit_card": _redact_credit_cards,
    "ip_address": lambda t: _redact_simple(t, _IP_ADDRESS_RE, "IP_ADDRESS"),
    "phone": _redact_phone,
    "names_in_context": _redact_names,
}


def redact_pii(
    text: str, categories: list[str] | None = None
) -> tuple[str, dict[str, int]]:
    """
    Redact PII from text.

    Returns (redacted_text, counts) where counts maps each category to
    the number of redactions made (categories with zero hits are
    omitted). categories limits which detectors run (default: all of
    CATEGORIES); unknown names raise ValueError.

    The input is Unicode-normalized first, so the returned text is the
    normalized form even when nothing was redacted. Idempotent:
    re-running on already-redacted text changes nothing.
    """
    if categories is None:
        categories = list(CATEGORIES)
    unknown = set(categories) - set(CATEGORIES)
    if unknown:
        raise ValueError(
            f"Unknown PII categories: {sorted(unknown)}; allowed: {list(CATEGORIES)}"
        )

    if not text:
        return "", {}

    redacted = normalize_unicode(strip_invisible_chars(text))
    counts: dict[str, int] = {}
    for category in CATEGORIES:
        if category not in categories:
            continue
        redacted, n = _REDACTORS[category](redacted)
        if n:
            counts[category] = n
    return redacted, counts


def contains_pii(text: str, categories: list[str] | None = None) -> bool:
    """True when redact_pii would redact anything in text."""
    _, counts = redact_pii(text, categories)
    return bool(counts)
