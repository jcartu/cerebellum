from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

try:
    from ..events import CerebellumEventEmitter
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.events import CerebellumEventEmitter


CONFIG_PATH = Path("/home/josh/.openclaw/cerebellum/config.json")
emitter = CerebellumEventEmitter(CONFIG_PATH)
app = FastAPI(title="Cerebellum Observatory")


def _stats_payload() -> dict[str, Any]:
    since = datetime.now().astimezone() - timedelta(hours=24)
    events = emitter.query(since=since, limit=10000)
    counts = dict(Counter(event["type"] for event in events))
    return {"window": "24h", "counts": counts, "total": len(events)}


def _parse_since(raw_since: str | None) -> datetime | None:
    if not raw_since:
        return None
    return datetime.fromisoformat(raw_since)


def _render_events(events: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for event in events:
        payload = json.dumps(event["payload"], indent=2)
        context = json.dumps(event["context"], indent=2)
        cards.append(
            f"""
            <article class=\"event-card\">
              <div class=\"event-meta\">
                <span class=\"event-type\">{event['type']}</span>
                <span>{event['timestamp']}</span>
                <span>{event['actor']}</span>
              </div>
              <div class=\"event-id\">{event['id']}</div>
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


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    events = emitter.query(limit=50)
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
          <div class=\"subtle\">Phase 1 event stream spine · live timeline · SQLite WAL + NATS JetStream</div>
          <section class=\"layout\">
            <aside class=\"panel\" id=\"stats\" hx-get=\"/api/stats/html\" hx-trigger=\"load, every 3s\" hx-swap=\"innerHTML\"></aside>
            <section class=\"panel\">
              <div id=\"events\" hx-get=\"/timeline\" hx-trigger=\"load, every 3s\" hx-swap=\"innerHTML\">{_render_events(events)}</div>
            </section>
          </section>
        </main>
        <script>
          const feed = document.getElementById("events");
          const source = new EventSource("/api/events/stream");
          source.onmessage = () => htmx.ajax("GET", "/timeline", {{target: "#events", swap: "innerHTML"}});
        </script>
      </body>
    </html>
    """


@app.get("/timeline", response_class=HTMLResponse)
async def timeline(limit: int = 50) -> str:
    return _render_events(emitter.query(limit=limit))


@app.get("/api/events")
async def api_events(since: str | None = None, limit: int = 50) -> JSONResponse:
    events = emitter.query(since=_parse_since(since), limit=limit)
    return JSONResponse(events)


@app.get("/api/events/stream")
async def api_events_stream(request: Request) -> StreamingResponse:
    async def event_generator() -> Any:
        last_seen = datetime.now().astimezone() - timedelta(seconds=5)
        while True:
            if await request.is_disconnected():
                break

            for event in reversed(emitter.query(since=last_seen, limit=100)):
                event_time = datetime.fromisoformat(event["timestamp"])
                if event_time > last_seen:
                    last_seen = event_time
                yield f"data: {json.dumps(event)}\n\n"

            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    return JSONResponse(_stats_payload())


@app.get("/api/stats/html", response_class=HTMLResponse)
async def api_stats_html() -> str:
    payload = _stats_payload()
    count_rows = "".join(
        f"<li><strong>{event_type}</strong><span>{count}</span></li>"
        for event_type, count in sorted(payload["counts"].items())
    ) or "<li><strong>No events</strong><span>0</span></li>"
    return f"""
    <h2 style=\"margin-top:0\">24h Stats</h2>
    <div style=\"color:#87a1c3;margin-bottom:14px\">Total events: {payload['total']}</div>
    <ul style=\"list-style:none;padding:0;margin:0;display:grid;gap:10px\">{count_rows}</ul>
    <style>li{{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #16263f;padding-bottom:8px}}li:last-child{{border-bottom:none}}</style>
    """


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text())
    port = config.get("dashboard", {}).get("port", 18790)
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
