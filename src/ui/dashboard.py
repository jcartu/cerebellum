from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

try:
    from ..events import CerebellumEventEmitter
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.events import CerebellumEventEmitter  # type: ignore[no-redef]

try:
    from ..arbiter import BasalGanglia  # type: ignore[import-not-found]
except ImportError:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.arbiter import BasalGanglia  # type: ignore[no-redef]

logger = logging.getLogger("cerebellum.dashboard")
CONFIG_PATH = Path("/home/josh/.openclaw/cerebellum/config.json")

# Lazy singleton — avoids module-level side effects
_emitter: CerebellumEventEmitter | None = None
_arbiter: BasalGanglia | None = None


def get_emitter() -> CerebellumEventEmitter:
    global _emitter
    if _emitter is None:
        try:
            _emitter = CerebellumEventEmitter(CONFIG_PATH)
        except Exception:
            logger.exception("Failed to initialize emitter")
            raise RuntimeError("Dashboard cannot start: emitter unavailable")
    return _emitter


def get_arbiter() -> BasalGanglia | None:
    global _arbiter
    if _arbiter is None:
        policy_path = CONFIG_PATH.parent / "policy.yaml"
        if not policy_path.exists():
            return None
        try:
            _arbiter = BasalGanglia(str(policy_path), emitter=get_emitter())
        except Exception:
            logger.exception("Failed to initialize arbiter")
            return None
    return _arbiter


app = FastAPI(title="Cerebellum Observatory")
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
if not DASHBOARD_TOKEN:
    logger.error("DASHBOARD_TOKEN environment variable is required")
    sys.exit(1)
_dashboard_db: sqlite3.Connection | None = None
_dashboard_db_lock = threading.RLock()


def _dashboard_db_path() -> Path:
    config = json.loads(CONFIG_PATH.read_text())
    return Path(config["sqlite"]["events_db"]).expanduser()


def _get_dashboard_db() -> sqlite3.Connection:
    global _dashboard_db
    with _dashboard_db_lock:
        if _dashboard_db is None:
            db_path = _dashboard_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _dashboard_db = sqlite3.connect(db_path, check_same_thread=False)
            _dashboard_db.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_seen_updates (
                    update_id INTEGER PRIMARY KEY,
                    seen_at INTEGER NOT NULL
                )
                """
            )
            _dashboard_db.commit()
        return _dashboard_db


@app.on_event("startup")
async def _startup_init_dashboard_db() -> None:
    _get_dashboard_db()


@app.middleware("http")
async def auth_middleware(request: Request, call_next: Any) -> Any:
    if request.url.path in ("/healthz", "/telegram/webhook"):
        return await call_next(request)
    if DASHBOARD_TOKEN:
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {DASHBOARD_TOKEN}"):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stats_payload() -> dict[str, Any]:
    emitter = get_emitter()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    events = emitter.query(since=since, limit=10000)
    counts = dict(Counter(event["type"] for event in events))
    return {"window": "24h", "counts": counts, "total": len(events)}


def _parse_since(raw_since: str | None) -> datetime | None:
    if not raw_since:
        return None
    try:
        return datetime.fromisoformat(raw_since).astimezone(timezone.utc)
    except ValueError:
        return None


def _render_events(events: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for event in events:
        payload = html.escape(json.dumps(event["payload"], indent=2, default=str))
        context = html.escape(json.dumps(event["context"], indent=2, default=str))
        cards.append(
            f"""
            <article class=\"event-card\">
              <div class=\"event-meta\">
                <span class=\"event-type\">{html.escape(str(event['type']))}</span>
                <span>{html.escape(str(event['timestamp']))}</span>
                <span>{html.escape(str(event['actor']))}</span>
              </div>
              <div class=\"event-id\">{html.escape(str(event['id']))}</div>
              <details>
                <summary>payload</summary>
                <pre>{payload}</pre>
              </details>
              <details>
                <summary>context</summary>
                <pre>{context}</pre>
              </details>
            </article>
            """
        )
    return "".join(cards) or "<div class=\"empty-state\">No events yet.</div>"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    events = get_emitter().query(limit=50)
    return f"""
    <!DOCTYPE html>
    <html lang=\"en\">
      <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>CEREBELLUM · Observatory</title>
        <script src=\"https://unpkg.com/htmx.org@1.9.12\"></script>
        <style>
          :root {{ color-scheme: dark; }}
          body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #070b12; color: #d9e7ff; }}
          main {{ max-width: 1080px; margin: 0 auto; padding: 24px; }}
          h1 {{ margin-bottom: 4px; font-size: 28px; }}
          .subtle {{ color: #87a1c3; margin-bottom: 24px; }}
          .layout {{ display: grid; grid-template-columns: 280px 1fr; gap: 20px; align-items: start; }}
          .panel {{ background: rgba(10, 18, 31, 0.9); border: 1px solid #1b2c48; border-radius: 16px; padding: 16px; box-shadow: 0 0 0 1px rgba(89, 132, 213, 0.06), 0 24px 60px rgba(0, 0, 0, 0.25); }}
          .event-card {{ border: 1px solid #1b2c48; border-radius: 12px; padding: 12px; margin-bottom: 12px; background: #0b1322; }}
          .event-meta {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; color: #94b2da; font-size: 13px; }}
          .event-type {{ color: #7cd4ff; font-weight: 700; }}
          .event-id {{ font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; color: #6f89ab; margin-bottom: 10px; }}
          pre {{ white-space: pre-wrap; word-break: break-word; background: #040913; color: #b9d6ff; padding: 10px; border-radius: 10px; overflow-x: auto; }}
          summary {{ cursor: pointer; color: #b6cbeb; margin-bottom: 8px; }}
          .empty-state {{ color: #87a1c3; padding: 20px 0; }}
          @media (max-width: 840px) {{ .layout {{ grid-template-columns: 1fr; }} }}
        </style>
      </head>
      <body>
        <main>
          <h1>CEREBELLUM Observatory</h1>
          <div class=\"subtle\">Event stream · live timeline · SQLite WAL + NATS JetStream</div>
          <section class=\"layout\">
            <aside class=\"panel\" id=\"stats\" hx-get=\"/api/stats/html\" hx-trigger=\"load, every 5s\" hx-swap=\"innerHTML\"></aside>
            <section class=\"panel\">
              <div id=\"events\" hx-get=\"/timeline\" hx-trigger=\"load, every 5s\" hx-swap=\"innerHTML\">{_render_events(events)}</div>
            </section>
          </section>
        </main>
      </body>
    </html>
    """


@app.get("/timeline", response_class=HTMLResponse)
async def timeline(limit: int = Query(default=50, ge=1, le=500)) -> str:
    return _render_events(get_emitter().query(limit=limit))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "uptime": "running"})


@app.get("/api/events")
async def api_events(since: str | None = None, limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    events = get_emitter().query(since=_parse_since(since), limit=limit)
    return JSONResponse(events)


@app.get("/api/events/stream")
async def api_events_stream(request: Request) -> StreamingResponse:
    emitter = get_emitter()

    async def event_generator() -> Any:
        last_seen = datetime.now(timezone.utc) - timedelta(seconds=5)
        while True:
            if await request.is_disconnected():
                break
            try:
                for event in reversed(emitter.query(since=last_seen, limit=100)):
                    event_time = datetime.fromisoformat(event["timestamp"]).astimezone(timezone.utc)
                    if event_time > last_seen:
                        last_seen = event_time
                    yield f"data: {json.dumps(event, default=str)}\n\n"
            except Exception:
                logger.exception("SSE generator failed")
            await asyncio.sleep(2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    return JSONResponse(_stats_payload())


@app.get("/api/stats/html", response_class=HTMLResponse)
async def api_stats_html() -> str:
    payload = _stats_payload()
    count_rows = "".join(
        f"<li><strong>{html.escape(event_type)}</strong><span>{count}</span></li>"
        for event_type, count in sorted(payload["counts"].items())
    ) or "<li><strong>No events</strong><span>0</span></li>"
    return f"""
    <h2 style=\"margin-top:0\">24h Stats</h2>
    <div style=\"color:#87a1c3;margin-bottom:14px\">Total events: {payload['total']}</div>
    <ul style=\"list-style:none;padding:0;margin:0;display:grid;gap:10px\">{count_rows}</ul>
    <style>li{{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #16263f;padding-bottom:8px}}li:last-child{{border-bottom:none}}</style>
    """


# ---------------------------------------------------------------------------
# Telegram Webhook (C6 fix)
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
TELEGRAM_ALLOWED_USER_IDS = {
    uid.strip()
    for uid in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}
HYPOTHESIS_ID_RE = re.compile(r"^[A-Za-z0-9_\-:]{1,128}$")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    """Receive Telegram callback queries and commands.

    Security:
      - Requires TELEGRAM_WEBHOOK_SECRET to be set and match the
        X-Telegram-Bot-Api-Secret-Token header (Telegram's built-in secret).
      - User IDs must appear in TELEGRAM_ALLOWED_USER_IDS (comma-separated).
      - hypothesis_id must match a strict allowlist regex.
    """
    if not TELEGRAM_BOT_TOKEN:
        return JSONResponse({"ok": False, "error": "Telegram bot token not configured"}, status_code=503)

    if not TELEGRAM_WEBHOOK_SECRET:
        logger.error("TELEGRAM_WEBHOOK_SECRET not set; refusing webhook")
        raise HTTPException(status_code=503, detail="Webhook secret not configured")

    provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(
        provided_secret.encode("utf-8"),
        TELEGRAM_WEBHOOK_SECRET.encode("utf-8"),
    ):
        logger.warning("Rejected Telegram webhook: bad secret token")
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    if not TELEGRAM_ALLOWED_USER_IDS:
        logger.error("TELEGRAM_ALLOWED_USER_IDS not set; refusing webhook")
        raise HTTPException(status_code=503, detail="No allowed user ids configured")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    now_ts = int(time.time())
    update_id_raw = update.get("update_id", update.get("id"))
    try:
        update_id = int(update_id_raw) if update_id_raw is not None else None
    except (TypeError, ValueError):
        update_id = None

    if update_id is not None:
        with _dashboard_db_lock:
            db = _get_dashboard_db()
            cursor = db.cursor()
            cursor.execute(
                "SELECT seen_at FROM telegram_seen_updates WHERE update_id = ?",
                (update_id,),
            )
            row = cursor.fetchone()
            if row:
                return JSONResponse({"ok": True, "message": "update already processed"})
            cursor.execute(
                "INSERT OR IGNORE INTO telegram_seen_updates (update_id, seen_at) VALUES (?, ?)",
                (update_id, now_ts),
            )
            if cursor.rowcount == 0:
                db.commit()
                return JSONResponse({"ok": True, "message": "update already processed"})
            db.commit()
            cursor.execute(
                "DELETE FROM telegram_seen_updates WHERE seen_at < ?",
                (now_ts - 86400,),
            )
            db.commit()

    callback = update.get("callback_query") or {}
    message = update.get("message") or {}

    msg_date_raw = callback.get("message", {}).get("date", 0)
    try:
        msg_date = int(msg_date_raw)
    except (TypeError, ValueError):
        msg_date = 0
    if msg_date and (time.time() - msg_date) > 300:
        return JSONResponse({"ok": True, "message": "callback expired"})

    def _actor_id(obj: dict) -> str:
        user = obj.get("from") or {}
        return str(user.get("id") or "")

    if callback:
        actor = _actor_id(callback)
        if actor not in TELEGRAM_ALLOWED_USER_IDS:
            logger.warning("Rejected callback from unauthorized user %s", actor)
            raise HTTPException(status_code=403, detail="User not allowed")

        callback_data = str(callback.get("data", ""))
        callback_id = str(callback.get("id", ""))

        match = re.match(r"^(approve|reject|snooze|explain):(.+)$", callback_data)
        if match:
            action, hypothesis_id = match.groups()
            if not HYPOTHESIS_ID_RE.match(hypothesis_id):
                _answer_callback(callback_id, "Invalid hypothesis id")
                raise HTTPException(status_code=400, detail="Invalid hypothesis id")
            arbiter = get_arbiter()
            if arbiter:
                try:
                    result = arbiter.handle_approval(hypothesis_id, action, user_id=actor)
                    _answer_callback(callback_id, "Processed")
                    return JSONResponse(result)
                except Exception:
                    logger.exception("Failed to handle approval for %s", hypothesis_id)
                    _answer_callback(callback_id, "Error processing request")
                    return JSONResponse({"ok": False, "error": "handler failed"}, status_code=500)
            _answer_callback(callback_id, "Arbiter unavailable")
            return JSONResponse({"ok": False, "error": "Arbiter unavailable"}, status_code=503)

        _answer_callback(callback_id, "Unknown action")
        return JSONResponse({"ok": False, "error": "Unknown action"}, status_code=400)

    if message and message.get("text") == "/cerebellum-halt":
        actor = _actor_id(message)
        if actor not in TELEGRAM_ALLOWED_USER_IDS:
            logger.warning("Rejected /cerebellum-halt from unauthorized user %s", actor)
            raise HTTPException(status_code=403, detail="User not allowed")
        arbiter = get_arbiter()
        if arbiter:
            result = arbiter.toggle_kill_switch(enabled=True)
            chat_id = message.get("chat", {}).get("id")
            if chat_id:
                _send_telegram_text(chat_id, f"🛑 Kill switch ENABLED: {result}")
            return JSONResponse(result)
        return JSONResponse({"ok": False, "error": "Arbiter unavailable"}, status_code=503)

    return JSONResponse({"ok": True})


def _answer_callback(callback_id: str, text: str) -> None:
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            data=json.dumps({"callback_query_id": callback_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        logger.exception("Failed to answer callback %s", callback_id)


def _send_telegram_text(chat_id: str | int, text: str) -> None:
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        logger.exception("Failed to send Telegram message to %s", chat_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = json.loads(CONFIG_PATH.read_text())
    port = config.get("dashboard", {}).get("port", 18790)
    host = os.environ.get("CEREBELLUM_DASHBOARD_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
