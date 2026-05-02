"""Conftest — set required env vars before any module imports."""

import os

# Force env vars for dashboard module (override shell/.env values)
os.environ["CEREBELLUM_TESTING"] = "1"
os.environ["DASHBOARD_TOKEN"] = "test-token"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "12345678"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:ABC-TEST-BOT-TOKEN"
