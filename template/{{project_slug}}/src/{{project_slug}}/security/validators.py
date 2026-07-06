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
    """Check if a hostname or its resolved IP is private/loopback/link-local."""
    import socket

    # First check if the hostname itself is a literal IP
    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass

    # Resolve the hostname and check ALL resolved IPs (DNS rebinding defense)
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, socktype, proto, canonname, sockaddr in results:
            ip_str = sockaddr[0]
            try:
                addr = ipaddress.ip_address(ip_str)
                if addr.is_private or addr.is_loopback or addr.is_link_local:
                    logger.warning(
                        f"[Validators] DNS rebinding blocked: {hostname} resolved to {ip_str}"
                    )
                    return True
            except ValueError:
                continue
    except socket.gaierror:
        pass

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

    Limitation (TOCTOU):
        Hostname resolution occurs at validation time. A hostname could resolve
        to a public IP during validation but to a private IP at connection time
        (DNS rebinding). revalidate_url_at_connect() narrows (but cannot
        close) that window; for highest assurance use IP pinning or
        allowlists at the transport.
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


def revalidate_url_at_connect(url: str, field_name: str = "url") -> None:
    """
    Connect-time DNS re-validation (anti-rebinding).

    Re-resolves the URL's hostname immediately before an outbound request
    and raises ValidationError if it now resolves to a private, loopback,
    or link-local address -- catching hostnames that passed validate_url
    earlier but were re-pointed at internal targets since (DNS rebinding).

    Literal IPs and "localhost" are deliberately NOT re-checked here:
    they cannot rebind, and whether they are allowed at all was decided
    when the URL was admitted (validate_url at registration blocks them;
    directly constructed dev clients may point at localhost on purpose).

    Unlike registration-time validation, this path fails CLOSED on a
    resolver error: if the hostname cannot be re-resolved, the request
    is refused (the connect would fail anyway if DNS were truly down,
    and passing an unverifiable hostname would defeat the re-check).

    Residual TOCTOU (honest limitation): this check and the subsequent
    connect are still two separate resolutions, so a rebind in the gap
    between them wins. Callers should re-check before every connect
    attempt (agents/remote.py does), which narrows the exposure to the
    re-check-to-connect gap of each attempt; eliminating it entirely
    needs IP pinning at the transport layer.
    """
    import socket

    parsed = urlparse(url.strip())
    hostname = parsed.hostname
    if not hostname:
        raise ValidationError(f"{field_name} must include a hostname")
    try:
        ipaddress.ip_address(hostname)
        return  # literal IP: cannot rebind; admission policy already ruled
    except ValueError:
        pass
    if hostname.lower() == "localhost":
        return  # cannot rebind; admission policy already ruled
    try:
        results = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        logger.warning(
            f"[Validators] Connect-time re-resolution of {hostname} failed "
            f"({e}); failing closed"
        )
        raise ValidationError(
            f"{field_name} could not be re-resolved at connect time; "
            "refusing to connect (fail closed)"
        ) from e
    for _family, _socktype, _proto, _canonname, sockaddr in results:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            logger.warning(
                f"[Validators] DNS rebinding blocked at connect time: "
                f"{hostname} resolved to {sockaddr[0]}"
            )
            raise ValidationError(
                f"{field_name} now resolves to a private/internal address "
                "(possible DNS rebinding); refusing to connect"
            )


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
