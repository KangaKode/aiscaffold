"""
Input Validators - Generic validation for user input at system boundaries.

Parse at the boundary: validate and type-check all external input
before it enters the system. Never pass raw dicts or unvalidated
strings through multiple layers.

Reference: docs/REFERENCES.md
"""

import ipaddress
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_URL_SCHEMES = {"http", "https"}
BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}


class ValidationError(ValueError):
    """Raised when input validation fails. Contains a user-friendly message."""

    pass


def validate_not_empty(value: str, field_name: str = "input") -> str:
    """Validate that a string is not empty or whitespace-only."""
    if not value or not value.strip():
        raise ValidationError(f"{field_name} cannot be empty")
    return value.strip()


def validate_length(
    value: str,
    field_name: str = "input",
    min_length: int = 0,
    max_length: int = 100_000,
) -> str:
    """Validate string length is within bounds."""
    if len(value) < min_length:
        raise ValidationError(f"{field_name} must be at least {min_length} characters")
    if len(value) > max_length:
        raise ValidationError(f"{field_name} must be at most {max_length} characters")
    return value


def validate_identifier(value: str, field_name: str = "identifier") -> str:
    """Validate that a string is a safe identifier (alphanumeric + underscore)."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", value):
        raise ValidationError(
            f"{field_name} must start with a letter and contain only "
            f"letters, numbers, underscores, and hyphens"
        )
    return value


def validate_in_choices(value: str, choices: list[str], field_name: str = "value") -> str:
    """Validate that a value is one of the allowed choices."""
    if value not in choices:
        raise ValidationError(f"{field_name} must be one of: {', '.join(choices)}")
    return value


def validate_positive_number(value: float | int, field_name: str = "number") -> float:
    """Validate that a number is positive."""
    if value <= 0:
        raise ValidationError(f"{field_name} must be positive (got {value})")
    return float(value)


def _is_private_ip(hostname: str) -> bool:
    """Check if a hostname resolves to a private/loopback IP address."""
    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


def validate_url(
    url: str,
    field_name: str = "url",
    allow_private: bool = False,
) -> str:
    """
    Validate a URL for safe outbound requests (anti-SSRF).

    Blocks:
      - Non-http/https schemes (file://, gopher://, ftp://, etc.)
      - Private IPs (10.x, 172.16.x, 192.168.x, 127.x, ::1)
      - Link-local addresses (169.254.x -- cloud metadata endpoints)
      - Known dangerous hostnames (localhost, metadata.google.internal)
      - Empty or malformed URLs

    Args:
        url: The URL to validate.
        field_name: Field name for error messages.
        allow_private: If True, skip private IP checks (for local dev only).

    Returns:
        The validated URL string.

    Raises:
        ValidationError: If the URL is unsafe.
    """
    if not url or not url.strip():
        raise ValidationError(f"{field_name} cannot be empty")

    parsed = urlparse(url.strip())

    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise ValidationError(
            f"{field_name} must use http or https (got '{parsed.scheme}')"
        )

    hostname = parsed.hostname
    if not hostname:
        raise ValidationError(f"{field_name} must include a hostname")

    hostname_lower = hostname.lower()
    if hostname_lower in BLOCKED_HOSTNAMES:
        raise ValidationError(
            f"{field_name} cannot point to {hostname_lower}"
        )

    if not allow_private and _is_private_ip(hostname):
        raise ValidationError(
            f"{field_name} cannot point to private/internal addresses"
        )

    if not allow_private and hostname_lower.endswith(".internal"):
        raise ValidationError(
            f"{field_name} cannot point to internal hostnames"
        )

    logger.debug(f"[Validators] URL validated: {parsed.scheme}://{hostname}")
    return url.strip()


def validate_list_size(
    items: list,
    field_name: str = "list",
    max_items: int = 100,
) -> list:
    """Validate that a list does not exceed a maximum number of items."""
    if len(items) > max_items:
        raise ValidationError(
            f"{field_name} cannot have more than {max_items} items (got {len(items)})"
        )
    return items


def validate_dict_size(
    data: dict,
    field_name: str = "data",
    max_size_bytes: int = 1_000_000,
) -> dict:
    """Validate that a serialized dict does not exceed a maximum byte size."""
    import json

    serialized = json.dumps(data, default=str)
    if len(serialized) > max_size_bytes:
        raise ValidationError(
            f"{field_name} exceeds maximum size of {max_size_bytes} bytes"
        )
    return data
