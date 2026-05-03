"""Telegram webhook hardening — IP allowlist + replay protection.

Provides a middleware layer for incoming Telegram webhook requests that:
1. Validates the source IP against known Telegram datacenter ranges
2. Detects and rejects replayed requests via nonce/timestamp deduplication
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

# Known Telegram datacenter IP ranges (CIDR notation)
TELEGRAM_IP_RANGES: Final[list[str]] = [
    "149.154.160.0/20",
    "91.108.4.0/22",
    "91.108.8.0/22",
    "91.108.12.0/22",
    "5.255.192.0/21",
    "5.255.196.0/23",
    "5.255.200.0/24",
    "5.255.201.0/24",
    "5.255.202.0/24",
    "5.255.203.0/24",
    "5.255.204.0/23",
    "5.255.206.0/24",
    "5.255.207.0/24",
]


@dataclass(frozen=True)
class TelegramWebhookResult:
    """Result of webhook validation."""

    allowed: bool
    reason: str


class TelegramWebhookGuard:
    """Validates incoming Telegram webhook requests."""

    def __init__(
        self,
        ip_ranges: list[str] | None = None,
        replay_window_seconds: float = 300.0,
        max_seen_nonces: int = 4096,
    ) -> None:
        self._networks: list[ipaddress.IPv4Network] = []
        ranges = ip_ranges or TELEGRAM_IP_RANGES
        for cidr in ranges:
            try:
                self._networks.append(ipaddress.IPv4Network(cidr, strict=False))
            except ValueError:
                logger.warning("Invalid CIDR range skipped: %s", cidr)

        self._replay_window = replay_window_seconds
        self._max_nonces = max_seen_nonces
        self._seen_nonces: OrderedDict[str, float] = OrderedDict()

    def validate_request(
        self,
        source_ip: str,
        request_id: str | None = None,
        timestamp: float | None = None,
    ) -> TelegramWebhookResult:
        """Validate an incoming webhook request.

        Args:
            source_ip: The remote IP address of the request.
            request_id: A unique identifier (nonce) for the request.
            timestamp: Unix timestamp of the request (defaults to now).

        Returns:
            TelegramWebhookResult with allowed status and reason.
        """
        # 1. IP allowlist check
        if not self._is_ip_allowed(source_ip):
            return TelegramWebhookResult(
                allowed=False,
                reason=f"Source IP {source_ip} not in Telegram allowlist",
            )

        # 2. Replay protection (only if request_id provided)
        if request_id:
            ts = timestamp or time.time()
            if self._is_replay(request_id, ts):
                return TelegramWebhookResult(
                    allowed=False,
                    reason=f"Replay detected: request_id={request_id}",
                )
            self._record_nonce(request_id, ts)

        return TelegramWebhookResult(allowed=True, reason="ok")

    def _is_ip_allowed(self, ip_str: str) -> bool:
        """Check if an IP address is within allowed Telegram ranges."""
        try:
            addr = ipaddress.IPv4Address(ip_str)
        except (ipaddress.AddressValueError, ValueError):
            return False
        return any(addr in net for net in self._networks)

    def _is_replay(self, request_id: str, timestamp: float) -> bool:
        """Check if a request_id has been seen within the replay window."""
        if request_id in self._seen_nonces:
            seen_at = self._seen_nonces[request_id]
            if timestamp - seen_at < self._replay_window:
                return True
        return False

    def _record_nonce(self, request_id: str, timestamp: float) -> None:
        """Record a nonce with its timestamp for replay detection."""
        # Evict stale entries first
        self._evict_stale(timestamp)
        # Insert new nonce
        self._seen_nonces[request_id] = timestamp
        # Trim if over capacity
        while len(self._seen_nonces) > self._max_nonces:
            self._seen_nonces.popitem(last=False)

    def _evict_stale(self, now: float) -> None:
        """Remove nonces older than the replay window."""
        cutoff = now - self._replay_window
        while self._seen_nonces:
            _oldest_id, oldest_ts = next(iter(self._seen_nonces.items()))
            if oldest_ts < cutoff:
                self._seen_nonces.popitem(last=False)
            else:
                break

    def purge_stale(self) -> int:
        """Manually purge stale nonces. Returns count of purged entries."""
        before = len(self._seen_nonces)
        self._evict_stale(time.time())
        return before - len(self._seen_nonces)

    @property
    def active_nonces(self) -> int:
        """Number of currently tracked nonces."""
        return len(self._seen_nonces)
