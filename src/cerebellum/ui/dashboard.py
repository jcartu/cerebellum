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
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from cerebellum.event_bus import EventBus
from cerebellum.feedback_loop import FeedbackStore
from cerebellum.http_safe import _safe_opener
from cerebellum.policy_arbiter import PolicyArbiter

logger = logging.getLogger(__name__)


def _base_dir() -> Path:
    return Path(os.environ.get("CEREBELLUM_BASE_DIR", str(Path(__file__).resolve().parents[3]))).expanduser()


def _config_path() -> Path:
    configured = os.environ.get("CEREBELLUM_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return _base_dir() / "config.json"


CONFIG_PATH = _config_path()


# Lazy singleton — avoids module-level side effects
_emitter: EventBus | None = None
_arbiter: PolicyArbiter | None = None


def get_emitter() -> EventBus:
    global _emitter
    if _emitter is None:
        try:
            _emitter = EventBus(CONFIG_PATH)
        except Exception:
            logger.exception("Failed to initialize emitter")
            raise RuntimeError("Dashboard cannot start: emitter unavailable")
    return _emitter


def get_arbiter() -> PolicyArbiter | None:
    global _arbiter
    if _arbiter is None:
        policy_path = _base_dir() / "policy.yaml"
        if not policy_path.exists():
            return None
        try:
            _arbiter = PolicyArbiter(str(policy_path), emitter=get_emitter())
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
_auth_rate_lock = threading.RLock()
_auth_rate_windows: dict[str, tuple[int, int]] = {}
_telegram_webhook_rate_lock = threading.RLock()
_telegram_webhook_rate_windows: dict[str, tuple[int, int]] = {}


_feedback_store: FeedbackStore | None = None


def get_feedback_store() -> FeedbackStore:
    global _feedback_store
    if _feedback_store is None:
        db_path = _base_dir() / "feedback.db"
        _feedback_store = FeedbackStore(db_path)
    return _feedback_store


def _dashboard_db_path() -> Path:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    db_path = Path(config["sqlite"]["events_db"]).expanduser()
    if not db_path.is_absolute():
        db_path = _base_dir() / db_path
    return db_path


def _get_dashboard_db() -> sqlite3.Connection:
    global _dashboard_db
    with _dashboard_db_lock:
        if _dashboard_db is None:
            db_path = _dashboard_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _dashboard_db = sqlite3.connect(db_path, check_same_thread=False)
            _ensure_telegram_seen_updates_schema(_dashboard_db)
            _dashboard_db.commit()
        return _dashboard_db


def _ensure_telegram_seen_updates_schema(db: sqlite3.Connection) -> None:
    schema_rows = db.execute("PRAGMA table_info(telegram_seen_updates)").fetchall()
    needs_rebuild = False
    if schema_rows:
        update_id_row = next((row for row in schema_rows if row[1] == "update_id"), None)
        update_id_type = str(update_id_row[2]).upper() if update_id_row else ""
        needs_rebuild = update_id_type != "TEXT"
    if needs_rebuild:
        db.execute("ALTER TABLE telegram_seen_updates RENAME TO telegram_seen_updates_legacy")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_seen_updates (
            update_id TEXT PRIMARY KEY,
            seen_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_seen_updates_update_id ON telegram_seen_updates(update_id)"
    )
    if needs_rebuild:
        db.execute(
            """
            INSERT OR IGNORE INTO telegram_seen_updates (update_id, seen_at)
            SELECT CAST(update_id AS TEXT), seen_at
            FROM telegram_seen_updates_legacy
            WHERE update_id IS NOT NULL
            """
        )
        db.execute("DROP TABLE telegram_seen_updates_legacy")


@app.on_event("startup")
async def _startup_init_dashboard_db() -> None:
    if TELEGRAM_WEBHOOK_SECRET and len(TELEGRAM_WEBHOOK_SECRET.encode("utf-8")) < 32:
        logger.error("TELEGRAM_WEBHOOK_SECRET must be at least 32 bytes")
        raise RuntimeError("Dashboard cannot start: weak Telegram webhook secret")
    _get_dashboard_db()


@app.middleware("http")
async def auth_middleware(request: Request, call_next: Any) -> Any:
    if request.url.path in ("/healthz", "/telegram/webhook"):
        return await call_next(request)
    if DASHBOARD_TOKEN:
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {DASHBOARD_TOKEN}"):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        source_ip = _request_source_ip(request)
        if not _allow_authenticated_request(source_ip):
            return JSONResponse(status_code=429, content={"error": "rate_limit_exceeded"})
    return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stats_payload() -> dict[str, Any]:
    emitter = get_emitter()
    since = datetime.now(UTC) - timedelta(hours=24)
    events = emitter.query(since=since, limit=10000)
    counts = dict(Counter(event["type"] for event in events))
    return {"window": "24h", "counts": counts, "total": len(events)}


def _parse_since(raw_since: str | None) -> datetime | None:
    if not raw_since:
        return None
    try:
        return datetime.fromisoformat(raw_since).astimezone(UTC)
    except ValueError:
        return None


def _request_source_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    client = request.client
    return client.host if client and client.host else "unknown"


def _allow_telegram_webhook_ip(source_ip: str) -> bool:
    now_window = int(time.time() // 60)
    with _telegram_webhook_rate_lock:
        window_start, count = _telegram_webhook_rate_windows.get(source_ip, (now_window, 0))
        if window_start != now_window:
            window_start, count = now_window, 0
        if count >= 60:
            _telegram_webhook_rate_windows[source_ip] = (window_start, count)
            stale_windows = [ip for ip, (window, _) in _telegram_webhook_rate_windows.items() if window != now_window]
            for stale_ip in stale_windows:
                _telegram_webhook_rate_windows.pop(stale_ip, None)
            return False
        _telegram_webhook_rate_windows[source_ip] = (window_start, count + 1)
        stale_windows = [ip for ip, (window, _) in _telegram_webhook_rate_windows.items() if window != now_window]
        for stale_ip in stale_windows:
            _telegram_webhook_rate_windows.pop(stale_ip, None)
        return True


def _allow_authenticated_request(source_ip: str) -> bool:
    now_window = int(time.time() // 60)
    with _auth_rate_lock:
        window_start, count = _auth_rate_windows.get(source_ip, (now_window, 0))
        if window_start != now_window:
            window_start, count = now_window, 0
        if count >= 60:
            _auth_rate_windows[source_ip] = (window_start, count)
            stale_windows = [ip for ip, (window, _) in _auth_rate_windows.items() if window != now_window]
            for stale_ip in stale_windows:
                _auth_rate_windows.pop(stale_ip, None)
            return False
        _auth_rate_windows[source_ip] = (window_start, count + 1)
        stale_windows = [ip for ip, (window, _) in _auth_rate_windows.items() if window != now_window]
        for stale_ip in stale_windows:
            _auth_rate_windows.pop(stale_ip, None)
        return True


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
        last_seen = datetime.now(UTC) - timedelta(seconds=5)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    for event in reversed(emitter.query(since=last_seen, limit=100)):
                        if await request.is_disconnected():
                            break
                        event_time = datetime.fromisoformat(event["timestamp"]).astimezone(UTC)
                        if event_time > last_seen:
                            last_seen = event_time
                        yield f"data: {json.dumps(event, default=str)}\n\n"
                        if await request.is_disconnected():
                            break
                except Exception:
                    logger.exception("SSE generator failed")
                if await request.is_disconnected():
                    break
                await asyncio.sleep(2)
        finally:
            # Log explicit cleanup so disconnected SSE clients do not silently leak generators.
            logger.debug("Closing dashboard SSE generator after client disconnect")

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


@app.get("/metrics", response_class=HTMLResponse)
async def metrics_page() -> str:
    store = get_feedback_store()
    metrics = store.compute_calibration(window_days=7)
    outcomes = store.query_outcomes(limit=100)

    outcome_rows = ""
    for row in outcomes[:20]:
        outcome_rows += f"""
        <tr>
          <td>{html.escape(row['hypothesis_id'][:32])}</td>
          <td>{html.escape(row['model'])}</td>
          <td>{row['confidence']:.3f}</td>
          <td>{html.escape(row['outcome'])}</td>
          <td>{html.escape(row['outcome_at'][:16])}</td>
        </tr>"""

    return f"""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>CEREBELLUM · Metrics</title>
        <style>
          :root {{ color-scheme: dark; }}
          body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #070b12; color: #d9e7ff; }}
          main {{ max-width: 1080px; margin: 0 auto; padding: 24px; }}
          h1 {{ margin-bottom: 4px; font-size: 28px; }}
          .subtle {{ color: #87a1c3; margin-bottom: 24px; }}
          .panel {{ background: rgba(10, 18, 31, 0.9); border: 1px solid #1b2c48; border-radius: 16px; padding: 16px; margin-bottom: 16px; }}
          .metric-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }}
          .metric-card {{ background: #0b1322; border: 1px solid #1b2c48; border-radius: 12px; padding: 12px; }}
          .metric-label {{ color: #87a1c3; font-size: 13px; margin-bottom: 4px; }}
          .metric-value {{ font-size: 24px; font-weight: 700; color: #7cd4ff; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
          th {{ text-align: left; color: #87a1c3; padding: 8px; border-bottom: 1px solid #1b2c48; }}
          td {{ padding: 8px; border-bottom: 1px solid #0d1a2d; }}
          .calibrated {{ color: #4ade80; }}
          .uncalibrated {{ color: #f87171; }}
          a {{ color: #7cd4ff; }}
        </style>
      </head>
      <body>
        <main>
          <h1>CEREBELLUM Metrics</h1>
          <div class="subtle">Feedback loop · calibration · <a href="/">← Back to dashboard</a></div>
          <div class="panel">
            <h2 style="margin-top:0">Calibration ({metrics.window_days}d window)</h2>
            <div class="metric-grid">
              <div class="metric-card">
                <div class="metric-label">Model</div>
                <div class="metric-value">{html.escape(metrics.model)}</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">Total Outcomes</div>
                <div class="metric-value">{metrics.total_outcomes}</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">Approval Rate</div>
                <div class="metric-value">{metrics.approval_rate:.1%}</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">Mean Confidence (Approved)</div>
                <div class="metric-value">{metrics.mean_confidence_approved:.3f}</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">Mean Confidence (Rejected)</div>
                <div class="metric-value">{metrics.mean_confidence_rejected:.3f}</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">Expected Calibration Error</div>
                <div class="metric-value">{metrics.expected_calibration_error:.4f}</div>
              </div>
              <div class="metric-card">
                <div class="metric-label">Status</div>
                <div class="metric-value {'calibrated' if metrics.is_calibrated else 'uncalibrated'}">{'Calibrated' if metrics.is_calibrated else 'Uncalibrated'}</div>
              </div>
            </div>
          </div>
          <div class="panel">
            <h2 style="margin-top:0">Recent Outcomes</h2>
            <table>
              <thead><tr><th>Hypothesis ID</th><th>Model</th><th>Confidence</th><th>Outcome</th><th>Time</th></tr></thead>
              <tbody>{outcome_rows}</tbody>
            </table>
          </div>
        </main>
      </body>
    </html>
    """


@app.get("/api/metrics")
async def api_metrics() -> JSONResponse:
    store = get_feedback_store()
    metrics = store.compute_calibration(window_days=7)
    return JSONResponse({
        "model": metrics.model,
        "window_days": metrics.window_days,
        "total_outcomes": metrics.total_outcomes,
        "approval_rate": metrics.approval_rate,
        "mean_confidence_approved": metrics.mean_confidence_approved,
        "mean_confidence_rejected": metrics.mean_confidence_rejected,
        "expected_calibration_error": metrics.expected_calibration_error,
        "is_calibrated": metrics.is_calibrated,
        "platt_a": metrics.platt_a,
        "platt_b": metrics.platt_b,
    })


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

    source_ip = _request_source_ip(request)
    logger.info("Telegram webhook request from %s", source_ip)

    if not _allow_telegram_webhook_ip(source_ip):
        logger.warning("Rejected Telegram webhook from %s: rate limit exceeded", source_ip)
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(
        provided_secret.encode("utf-8"),
        TELEGRAM_WEBHOOK_SECRET.encode("utf-8"),
    ):
        logger.warning("Rejected Telegram webhook from %s: bad secret token", source_ip)
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    if not TELEGRAM_ALLOWED_USER_IDS:
        logger.error("TELEGRAM_ALLOWED_USER_IDS not set; refusing webhook")
        raise HTTPException(status_code=503, detail="No allowed user ids configured")

    try:
        update = await request.json()
    except Exception:
        # Invalid JSON is expected from untrusted webhook callers; keep this low-noise while preserving traceability.
        logger.debug("Rejected Telegram webhook with invalid JSON body", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    now_ts = int(time.time())
    update_id_raw = update.get("update_id", update.get("id"))
    update_id = str(update_id_raw).strip() if update_id_raw is not None else ""

    if update_id:
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
                return JSONResponse({"ok": True, "message": "update already processed"})
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
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            data=json.dumps({"callback_query_id": callback_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _safe_opener.open(req, timeout=10):
            pass
    except Exception:
        logger.exception("Failed to answer callback %s", callback_id)


def _send_telegram_text(chat_id: str | int, text: str) -> None:
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _safe_opener.open(req, timeout=10):
            pass
    except Exception:
        logger.exception("Failed to send Telegram message to %s", chat_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    port = config.get("dashboard", {}).get("port", 18790)
    host = os.environ.get("CEREBELLUM_DASHBOARD_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
