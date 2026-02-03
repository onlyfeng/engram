"""
Redaction helpers for gateway logging and error responses.

- sanitize_header_list: logging only
- sanitize_error_message/sanitize_error_details: outbound responses
"""

from __future__ import annotations

from typing import Any

from engram.common.redaction import redact_sensitive_data, redact_sensitive_text

_DEFAULT_MAX_HEADER_LENGTH = 200
DEFAULT_PUBLIC_ERROR_MESSAGE = "内部错误"


def _truncate_text(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[:max_length]}..."


def sanitize_header_list(
    raw_headers: str | None, max_length: int = _DEFAULT_MAX_HEADER_LENGTH
) -> str:
    """
    Sanitize CORS header lists for logging.

    - Strip any accidental header values (split on ':')
    - Redact common token patterns
    - Truncate to avoid overly long log lines
    """
    if not raw_headers:
        return ""

    header_names: list[str] = []
    for item in str(raw_headers).split(","):
        item = item.strip()
        if not item:
            continue
        header_name = item.split(":", 1)[0].strip()
        if header_name:
            header_name = header_name.split()[0]
        if header_name:
            header_names.append(header_name)

    joined = ", ".join(header_names)
    redacted = redact_sensitive_text(joined)
    return _truncate_text(redacted, max_length)


def sanitize_error_message(message: str | None) -> str:
    """Sanitize error messages for outbound responses."""
    return redact_sensitive_text(message)


def sanitize_error_details(details: Any) -> Any:
    """Sanitize error details for outbound responses."""
    return redact_sensitive_data(details)
