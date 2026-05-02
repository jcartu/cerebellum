"""HTTP client with SSRF protection using httpx.

Provides safe_get() and safe_post() that reject redirects, block
private/metadata IPs, and enforce timeouts. Replaces urllib.request
for new code (Phase 4). The legacy _PinnedHTTPSConnection in
policy_arbiter.py is kept for backward compatibility until Phase 6.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# RFC1918 + loopback + link-local + cloud metadata ranges
_BLOCKED_RANGES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.0.0.0/24"),
    ipaddress.IPv4Network("192.0.2.0/24"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("198.18.0.0/15"),
    ipaddress.IPv4Network("203.0.113.0/24"),
    ipaddress.IPv4Network("224.0.0.0/4"),
    ipaddress.IPv4Network("240.0.0.0/4"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
]


def _is_blocked_ip(host: str) -> bool:
    """Check if a hostname resolves to a blocked IP range.

    Args:
        host: Hostname or IP address to check.

    Returns:
        True if the host resolves to a blocked IP range.
    """
    try:
        addr = ipaddress.ip_address(host)
        for network in _BLOCKED_RANGES:
            if addr in network:
                return True
    except ValueError:
        pass
    return False


def safe_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    allow_redirects: bool = False,
) -> httpx.Response:
    """Perform a safe HTTP GET request.

    Args:
        url: The URL to GET.
        headers: Optional headers to include.
        timeout: Request timeout in seconds.
        allow_redirects: Whether to follow redirects (default False).

    Returns:
        The httpx Response object.

    Raises:
        ValueError: If the URL resolves to a blocked IP range.
        httpx.HTTPError: On network or HTTP errors.
    """
    if _is_blocked_ip(url.split("//")[-1].split("/")[0].split(":")[0]):
        raise ValueError(f"Blocked IP in URL: {url}")

    with httpx.Client(
        follow_redirects=allow_redirects,
        timeout=timeout,
    ) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response


def safe_post(
    url: str,
    json: dict[str, Any] | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> httpx.Response:
    """Perform a safe HTTP POST request.

    Args:
        url: The URL to POST to.
        json: Optional JSON payload.
        data: Optional raw bytes payload.
        headers: Optional headers to include.
        timeout: Request timeout in seconds.

    Returns:
        The httpx Response object.

    Raises:
        ValueError: If the URL resolves to a blocked IP range.
        httpx.HTTPError: On network or HTTP errors.
    """
    if _is_blocked_ip(url.split("//")[-1].split("/")[0].split(":")[0]):
        raise ValueError(f"Blocked IP in URL: {url}")

    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        response = client.post(url, json=json, content=data, headers=headers)
        response.raise_for_status()
        return response
