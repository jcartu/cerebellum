"""Conftest — set required env vars before any module imports."""

import json
import os
from pathlib import Path

# Create a minimal config.json for tests (removed from repo as build artifact)
repo_root = Path(__file__).resolve().parent.parent
test_config = repo_root / "config.json"
if not test_config.exists():
    test_config.write_text(
        json.dumps(
            {
                "nats": {"host": "localhost", "port": 4222, "jetstream_domain": ""},
                "sqlite": {"events_db": "events.db"},
                "dashboard": {"port": 18790},
                "arbiter_loop": {"sleep_seconds": 300, "sleep_jitter_fraction": 0.1},
                "hippocampus": {
                    "openrouter_url": "https://openrouter.ai/api/v1/chat/completions",
                    "openrouter_model": "openai/gpt-4o",
                },
                "models": ["openai/gpt-4o", "anthropic/claude-opus-4-7"],
                "openrouter_base_url": "https://openrouter.ai/api/v1",
                "generation_interval_minutes": 5,
                "app_name": "CEREBELLUM",
                "site_url": "https://localhost/cerebellum",
            }
        )
    )

# Force env vars for dashboard module (override shell/.env values)
os.environ["CEREBELLUM_TESTING"] = "1"
os.environ["DASHBOARD_TOKEN"] = "test-token"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "12345678"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:ABC-TEST-BOT-TOKEN"
