"""Tests for Telegram webhook hardening — IP allowlist + replay protection."""

from __future__ import annotations

from cerebellum.telegram_hardening import (
    TELEGRAM_IP_RANGES,
    TelegramWebhookGuard,
)


class TestIPAllowlist:
    """Validate IP allowlist checks."""

    def test_telegram_ip_allowed(self) -> None:
        guard = TelegramWebhookGuard()
        # 149.154.160.0/20 range
        assert guard.validate_request("149.154.160.1").allowed is True
        assert guard.validate_request("149.154.165.100").allowed is True

    def test_another_telegram_range_allowed(self) -> None:
        guard = TelegramWebhookGuard()
        # 91.108.4.0/22 range
        assert guard.validate_request("91.108.4.1").allowed is True
        assert guard.validate_request("91.108.7.254").allowed is True

    def test_non_telegram_ip_rejected(self) -> None:
        guard = TelegramWebhookGuard()
        result = guard.validate_request("8.8.8.8")
        assert result.allowed is False
        assert "not in Telegram allowlist" in result.reason

    def test_localhost_rejected(self) -> None:
        guard = TelegramWebhookGuard()
        assert guard.validate_request("127.0.0.1").allowed is False

    def test_invalid_ip_rejected(self) -> None:
        guard = TelegramWebhookGuard()
        assert guard.validate_request("not_an_ip").allowed is False
        assert guard.validate_request("999.999.999.999").allowed is False

    def test_custom_ip_ranges(self) -> None:
        guard = TelegramWebhookGuard(ip_ranges=["10.0.0.0/8"])
        assert guard.validate_request("10.0.0.1").allowed is True
        assert guard.validate_request("8.8.8.8").allowed is False

    def test_cidr_boundary(self) -> None:
        guard = TelegramWebhookGuard()
        # 149.154.160.0/20 → 149.154.160.0 to 149.154.175.255
        assert guard.validate_request("149.154.175.255").allowed is True
        assert guard.validate_request("149.154.176.0").allowed is False


class TestReplayProtection:
    """Validate replay detection via nonce tracking."""

    def test_first_request_allowed(self) -> None:
        guard = TelegramWebhookGuard()
        result = guard.validate_request(
            source_ip="149.154.160.1",
            request_id="unique-req-001",
        )
        assert result.allowed is True

    def test_replayed_request_rejected(self) -> None:
        guard = TelegramWebhookGuard()
        # First request
        guard.validate_request(
            source_ip="149.154.160.1",
            request_id="replay-req-001",
            timestamp=1000.0,
        )
        # Replay
        result = guard.validate_request(
            source_ip="149.154.160.1",
            request_id="replay-req-001",
            timestamp=1001.0,
        )
        assert result.allowed is False
        assert "Replay detected" in result.reason

    def test_replay_after_window_allowed(self) -> None:
        guard = TelegramWebhookGuard(replay_window_seconds=60.0)
        # First request
        guard.validate_request(
            source_ip="149.154.160.1",
            request_id="window-req-001",
            timestamp=1000.0,
        )
        # Same nonce, but outside window
        result = guard.validate_request(
            source_ip="149.154.160.1",
            request_id="window-req-001",
            timestamp=1061.0,
        )
        assert result.allowed is True

    def test_no_request_id_skips_replay_check(self) -> None:
        guard = TelegramWebhookGuard()
        # Multiple requests without request_id should all pass
        assert guard.validate_request("149.154.160.1").allowed is True
        assert guard.validate_request("149.154.160.1").allowed is True

    def test_nonce_capacity_trim(self) -> None:
        guard = TelegramWebhookGuard(max_seen_nonces=5)
        for i in range(10):
            guard.validate_request(
                source_ip="149.154.160.1",
                request_id=f"req-{i}",
                timestamp=float(1000 + i),
            )
        assert guard.active_nonces <= 5

    def test_purge_stale(self) -> None:
        guard = TelegramWebhookGuard(replay_window_seconds=60.0)
        for i in range(5):
            guard.validate_request(
                source_ip="149.154.160.1",
                request_id=f"stale-{i}",
                timestamp=float(1000 + i),
            )
        # All nonces are stale relative to now
        purged = guard.purge_stale()
        assert purged == 5
        assert guard.active_nonces == 0


class TestCombinedValidation:
    """Test IP + replay checks together."""

    def test_ip_fail_short_circuits_replay(self) -> None:
        guard = TelegramWebhookGuard()
        result = guard.validate_request(
            source_ip="8.8.8.8",
            request_id="should-not-be-recorded",
        )
        assert result.allowed is False
        assert "not in Telegram allowlist" in result.reason
        # Nonce should NOT be recorded for rejected IPs
        assert guard.active_nonces == 0

    def test_valid_ip_and_unique_nonce_passes(self) -> None:
        guard = TelegramWebhookGuard()
        result = guard.validate_request(
            source_ip="149.154.160.1",
            request_id="valid-req-001",
        )
        assert result.allowed is True
        assert result.reason == "ok"
        assert guard.active_nonces == 1


class TestConstants:
    """Verify Telegram IP ranges are populated."""

    def test_ip_ranges_not_empty(self) -> None:
        assert len(TELEGRAM_IP_RANGES) > 0

    def test_ip_ranges_are_valid_cidrs(self) -> None:
        import ipaddress
        for cidr in TELEGRAM_IP_RANGES:
            ipaddress.IPv4Network(cidr, strict=False)  # should not raise
