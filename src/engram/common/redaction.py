"""Common redaction helpers for sensitive strings."""

from __future__ import annotations

import re
from typing import Any, Pattern

_REDACTION_PATTERNS: tuple[tuple[Pattern[str], str], ...] = (
    # GitLab tokens (glpat-xxx, glptt-xxx, etc)
    (re.compile(r"\b(glp[a-z]{1,2}-[A-Za-z0-9_-]{10,})\b"), "[GITLAB_TOKEN]"),
    # Bearer tokens
    (re.compile(r"\bBearer\s+[A-Za-z0-9_.\-=]+", re.IGNORECASE), "[REDACTED]"),
    # PostgreSQL DSN
    (re.compile(r"postgres(ql)?://[^\s)]+", re.IGNORECASE), "[REDACTED]"),
    # Authorization header values
    (re.compile(r"(Authorization[:\s]+)(\S+\s+)?(\S+)", re.IGNORECASE), r"\1[REDACTED]"),
    # PRIVATE-TOKEN header values
    (re.compile(r"(PRIVATE-TOKEN[:\s]+)[^\s,;]+", re.IGNORECASE), r"\1[REDACTED]"),
    # token/password parameters
    (re.compile(r"(password[=:\s]+)[^\s&;,]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(token[=:\s]+)[^\s&;,]+", re.IGNORECASE), r"\1[REDACTED]"),
    # URL credentials (user:pass@host)
    (re.compile(r"(://[^:]+:)[^@]+(@)"), r"\1[REDACTED]\2"),
)

_SENSITIVE_KEYS: set[str] = {
    "authorization",
    "proxy-authorization",
    "private-token",
    "x-private-token",
    "x-gitlab-token",
    "cookie",
    "set-cookie",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "api_key",
    "apikey",
    "client_secret",
    "secret",
}


def _normalize_key(key: Any) -> str:
    if isinstance(key, str):
        return key.lower()
    return str(key).lower()


def redact_sensitive_text(text: str | None) -> str:
    """Redact common sensitive patterns from text."""
    if not text:
        return ""
    result = str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_sensitive_data(
    value: Any,
    *,
    sensitive_keys: set[str] | None = None,
    max_depth: int = 6,
) -> Any:
    """
    Redact sensitive content from nested data structures.

    - Strings: apply redact_sensitive_text
    - Dicts: redact values for sensitive keys; recurse for other keys
    - Lists/Tuples: recurse for each item
    """
    if max_depth < 0:
        return value
    if value is None:
        return None
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, Exception):
        return redact_sensitive_text(str(value))

    if isinstance(value, dict):
        all_sensitive_keys = _SENSITIVE_KEYS | {
            _normalize_key(key) for key in (sensitive_keys or set())
        }
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _normalize_key(key) in all_sensitive_keys:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_sensitive_data(
                    item,
                    sensitive_keys=sensitive_keys,
                    max_depth=max_depth - 1,
                )
        return redacted

    if isinstance(value, list):
        return [
            redact_sensitive_data(
                item,
                sensitive_keys=sensitive_keys,
                max_depth=max_depth - 1,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_sensitive_data(
                item,
                sensitive_keys=sensitive_keys,
                max_depth=max_depth - 1,
            )
            for item in value
        )

    return value
