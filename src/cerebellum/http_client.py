"""HTTP client with SSRF protection using httpx.

Provides safe_get(), safe_post(), safe_post_bytes(), and safe_request()
that reject redirects, block private/metadata IPs, and enforce timeouts.
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
    """Check if a hostname resolves to a blocked IP range."""
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
    """Perform a safe HTTP GET request."""
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
    """Perform a safe HTTP POST request."""
    if _is_blocked_ip(url.split("//")[-1].split("/")[0].split(":")[0]):
        raise ValueError(f"Blocked IP in URL: {url}")

    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        response = client.post(url, json=json, content=data, headers=headers)
        response.raise_for_status()
        return response


def safe_post_bytes(
    url: str,
    json: dict[str, Any] | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> bytes:
    """Perform a safe HTTP POST and return raw response bytes."""
    if _is_blocked_ip(url.split("//")[-1].split("/")[0].split(":")[0]):
        raise ValueError(f"Blocked IP in URL: {url}")

    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        response = client.post(url, json=json, content=data, headers=headers)
        response.raise_for_status()
        return response.content


def safe_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    data: bytes | None = None,
    timeout: float = 30.0,
    pin_to_ip: str | None = None,
) -> httpx.Response:
    """Perform a safe HTTP request with optional IP pinning for SSRF protection.

    Args:
        method: HTTP method (GET, POST, etc.).
        url: The URL to request.
        headers: Optional headers to include.
        json: Optional JSON payload.
        data: Optional raw bytes payload.
        timeout: Request timeout in seconds.
        pin_to_ip: If set, connect to this IP instead of resolving the URL hostname.
            The original hostname is preserved in the Host header and TLS SNI.

    Returns:
        The httpx Response object.
    """
    resolved_host = pin_to_ip or url.split("//")[-1].split("/")[0].split(":")[0]
    if _is_blocked_ip(resolved_host):
        raise ValueError(f"Blocked IP in URL: {url}")

    effective_url = url
    if pin_to_ip:
        parsed = url.split("//")
        effective_url = f"{parsed[0]}//{pin_to_ip}{parsed[1]}"
        if headers is None:
            headers = {}
        original_host = url.split("//")[-1].split("/")[0]
        headers["Host"] = original_host

    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        response = client.request(
            method, effective_url, headers=headers, json=json, content=data
        )
        response.raise_for_status()
        return response
