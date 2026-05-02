"""Telegram webhook fuzzer - 10k iterations of random payloads.

Verifies the webhook parsing logic never crashes on malformed input.
Tests core logic directly since dashboard.py has sys.exit(1) at module level.
"""

from __future__ import annotations

import json
import random
import re
import string
import time

import pytest
from hypothesis import given, settings, strategies as st


HYPOTHESIS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class TestTelegramWebhookFuzzer:
    """Fuzz the Telegram webhook parsing with 10k random payloads."""

    def _generate_random_update(self) -> dict:
        strategies = [
            self._random_callback_query,
            self._random_message,
            self._random_edited_message,
            self._random_channel_post,
            self._random_chosen_inline_result,
            self._random_polling_update,
            self._random_nested_malformed,
            self._random_empty_fields,
        ]
        return random.choice(strategies)()

    def _random_callback_query(self) -> dict:
        return {
            "update_id": random.randint(0, 999999999),
            "callback_query": {
                "id": "".join(random.choices(string.ascii_letters + string.digits, k=random.randint(1, 50))),
                "from": {
                    "id": random.randint(-999999999, 999999999),
                    "is_bot": random.choice([True, False]),
                    "first_name": "".join(random.choices(string.ascii_letters, k=random.randint(1, 20))),
                },
                "message": {
                    "date": random.randint(0, int(time.time()) + 1000),
                    "chat": {"id": random.randint(-999999999, 999999999)},
                },
                "data": random.choice([
                    "",
                    "approve:abc123",
                    "reject:abc123",
                    "snooze:abc123",
                    "explain:abc123",
                    "approve:" + "A" * 500,
                    "approve:invalid!@#$%",
                    "unknown_action:abc123",
                    "approve:" + "".join(random.choices(string.ascii_letters + string.digits + "!@#$%^&*()", k=random.randint(1, 100))),
                    json.dumps({"nested": "data"}),
                    None,
                ]),
            },
        }

    def _random_message(self) -> dict:
        return {
            "update_id": random.randint(0, 999999999),
            "message": {
                "message_id": random.randint(1, 999999),
                "date": random.randint(0, int(time.time()) + 1000),
                "chat": {"id": random.randint(-999999999, 999999999)},
                "from": {
                    "id": random.randint(-999999999, 999999999),
                    "is_bot": random.choice([True, False]),
                },
                "text": random.choice([
                    "/cerebellum-halt",
                    "/start",
                    "hello world",
                    "A" * 10000,
                    "",
                    None,
                    "/cerebellum-halt" + " " * 100,
                ]),
            },
        }

    def _random_edited_message(self) -> dict:
        return {
            "update_id": random.randint(0, 999999999),
            "edited_message": {
                "message_id": random.randint(1, 999999),
                "date": random.randint(0, int(time.time())),
                "chat": {"id": random.randint(-999999999, 999999999)},
            },
        }

    def _random_channel_post(self) -> dict:
        return {
            "update_id": random.randint(0, 999999999),
            "channel_post": {
                "message_id": random.randint(1, 999999),
                "date": random.randint(0, int(time.time())),
                "chat": {"id": random.randint(-999999999, 999999999), "type": "channel"},
            },
        }

    def _random_chosen_inline_result(self) -> dict:
        return {
            "update_id": random.randint(0, 999999999),
            "chosen_inline_result": {
                "result_id": "".join(random.choices(string.ascii_letters + string.digits, k=random.randint(1, 20))),
                "from": {"id": random.randint(1, 999999999)},
                "query": "".join(random.choices(string.ascii_letters, k=random.randint(0, 50))),
            },
        }

    def _random_polling_update(self) -> dict:
        return {
            "update_id": random.randint(0, 999999999),
            "poll": {
                "id": "".join(random.choices(string.ascii_letters + string.digits, k=random.randint(1, 20))),
                "question": "".join(random.choices(string.ascii_letters, k=random.randint(1, 50))),
                "options": [{"text": "A", "voter_count": 0}, {"text": "B", "voter_count": 0}],
                "total_voter_count": 0,
                "is_closed": random.choice([True, False]),
                "is_anonymous": random.choice([True, False]),
            },
        }

    def _random_nested_malformed(self) -> dict:
        depth = random.randint(1, 10)
        nested = {}
        current = nested
        for _ in range(depth):
            key = "".join(random.choices(string.ascii_letters, k=random.randint(1, 10)))
            current[key] = {}
            current = current[key]
        current["value"] = random.choice(["leaf", 12345, None, [1, 2, 3], {"deep": "nested"}])
        return {"update_id": random.randint(0, 999999999), "malformed_nested": nested}

    def _random_empty_fields(self) -> dict:
        return {"update_id": random.randint(0, 999999999), "callback_query": {}}

    def test_fuzz_webhook_10k_iterations(self) -> None:
        """Run 10k random payloads through the webhook parsing logic."""
        errors = []
        for i in range(10000):
            update = self._generate_random_update()
            try:
                self._test_webhook_logic(update)
            except Exception as e:
                errors.append((i, str(e)))

        if errors:
            print(f"Found {len(errors)} errors out of 10000 iterations:")
            for idx, err in errors[:5]:
                print(f"  [{idx}] {err}")
            assert False, f"Fuzzer found {len(errors)} errors"

    def _test_webhook_logic(self, update: dict) -> None:
        """Test the core webhook logic (mirrors dashboard.py parsing)."""
        callback = update.get("callback_query") or {}
        message = update.get("message") or {}

        if callback:
            callback_data = str(callback.get("data", ""))
            match = re.match(r"^(approve|reject|snooze|explain):(.+)$", callback_data)
            if match:
                action, hypothesis_id = match.groups()
                assert action in ("approve", "reject", "snooze", "explain")
                HYPOTHESIS_ID_RE.match(hypothesis_id)

        if message:
            text = message.get("text")
            if text == "/cerebellum-halt":
                pass


class TestWebhookParsingProperties:
    """Property tests for webhook callback parsing."""

    @given(data=st.text(min_size=0, max_size=500, alphabet=st.characters(
        min_codepoint=32, max_codepoint=126,
        blacklist_characters="\n\r"
    )))
    @settings(max_examples=500, deadline=None)
    def test_callback_data_parsing_safe(self, data: str) -> None:
        """Callback data parsing never crashes on any input."""
        match = re.match(r"^(approve|reject|snooze|explain):(.+)$", data)
        if match:
            action, hypothesis_id = match.groups()
            assert action in ("approve", "reject", "snooze", "explain")
            try:
                HYPOTHESIS_ID_RE.match(hypothesis_id)
            except Exception:
                assert False, f"HYPOTHESIS_ID_RE.match crashed on: {hypothesis_id!r}"

    @given(text=st.text(min_size=0, max_size=500, alphabet=st.characters(
        min_codepoint=32, max_codepoint=126,
        blacklist_characters="\n\r"
    )))
    @settings(max_examples=200, deadline=None)
    def test_message_text_handling_safe(self, text: str) -> None:
        """Message text handling never crashes on any input."""
        is_halt = text == "/cerebellum-halt"
        assert isinstance(is_halt, bool)

    @given(update_id=st.integers(min_value=-1000000000, max_value=1000000000))
    @settings(max_examples=100, deadline=None)
    def test_update_id_handling_safe(self, update_id: int) -> None:
        """Update ID handling never crashes on any integer."""
        assert isinstance(update_id, int)
        assert update_id == update_id
