"""Token-based auth for SSE transport."""

from __future__ import annotations

import hmac
import logging
import os
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

# Simple in-memory rate limiter per IP
_rate_windows: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_REQUESTS = 60


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def check_rate_limit(client_ip: str, max_requests: int = _RATE_MAX_REQUESTS) -> bool:
    """Check if client IP is within rate limit. Returns True if allowed."""
    now = time.time()
    window_start = now - _RATE_WINDOW_SECONDS

    # Clean old entries
    _rate_windows[client_ip] = [t for t in _rate_windows[client_ip] if t > window_start]

    if len(_rate_windows[client_ip]) >= max_requests:
        logger.warning("Rate limit exceeded for IP %s", client_ip)
        return False

    _rate_windows[client_ip].append(now)
    return True


def get_mcp_token() -> str | None:
    """Get MCP auth token from environment."""
    env_var = os.environ.get("CEREBELLUM_MCP_TOKEN_ENV", "CEREBELLUM_MCP_TOKEN")
    return os.environ.get(env_var)


def validate_token(provided: str, expected: str) -> bool:
    """Validate provided token against expected in constant time."""
    if not provided or not expected:
        return False
    return constant_time_compare(provided, expected)
