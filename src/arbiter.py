import json
import logging
import os
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("cerebellum.arbiter")


@dataclass
class ActionDecision:
    hypothesis_id: str
    decision: str  # "auto_execute" | "stage_notify" | "discard"
    reason: str
    timestamp: str

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "decision": self.decision,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


class RateLimiter:
    """Simple thread-safe sliding window rate limiter."""

    def __init__(self, max_count: int, window_seconds: int):
        self.max_count = max_count
        self.window_seconds = window_seconds
        self.events: list[datetime] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = datetime.now()
            self.events = [
                event
                for event in self.events
                if now - event < timedelta(seconds=self.window_seconds)
            ]
            if len(self.events) >= self.max_count:
                return False
            self.events.append(now)
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = datetime.now()
            self.events = [
                event
                for event in self.events
                if now - event < timedelta(seconds=self.window_seconds)
            ]
            return {
                "max_count": self.max_count,
                "window_seconds": self.window_seconds,
                "used": len(self.events),
                "remaining": max(self.max_count - len(self.events), 0),
            }


class DailyCostTracker:
    """Tracks daily LLM budget usage."""

    def __init__(self, max_cost: float):
        self.max_cost = max_cost
        self._lock = threading.Lock()
        self._day = datetime.now().date()
        self._spent = 0.0

    def allow(self, additional_cost: float) -> bool:
        with self._lock:
            today = datetime.now().date()
            if today != self._day:
                self._day = today
                self._spent = 0.0
            if self._spent + max(additional_cost, 0.0) > self.max_cost:
                return False
            self._spent += max(additional_cost, 0.0)
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            today = datetime.now().date()
            if today != self._day:
                self._day = today
                self._spent = 0.0
            return {
                "date": self._day.isoformat(),
                "max_cost": self.max_cost,
                "spent": round(self._spent, 4),
                "remaining": round(max(self.max_cost - self._spent, 0.0), 4),
            }


class BasalGanglia:
    """Action arbiter - decides what to do with hypotheses."""

    def __init__(self, config_path: str, emitter: Any = None, cortex: Any = None):
        self.config_path = Path(config_path)
        self.base_dir = self.config_path.parent
        self.state_dir = self.base_dir / "graph"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pending_file = self.state_dir / "pending_approvals.json"
        self.feedback_file = self.state_dir / "arbiter_feedback.jsonl"
        self.state_file = self.state_dir / "arbiter_state.json"
        self.decisions_file = self.state_dir / "arbiter_decisions.jsonl"
        self.events_db_path = self.state_dir / "observatory.sqlite3"
        self.emitter = emitter
        self.cortex = cortex
        self._lock = threading.Lock()

        try:
            self.policy = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.exception("Failed to read policy file: %s", self.config_path)
            raise RuntimeError(f"Unable to load arbiter policy from {self.config_path}") from exc

        global_cfg = self.policy.get("global", {})
        self.kill_switch = not bool(global_cfg.get("enabled", True))
        self.kill_switch_command = str(global_cfg.get("kill_switch_command", "/cerebellum-halt"))
        self.action_limiter = RateLimiter(int(global_cfg.get("max_actions_per_hour", 10)), 3600)
        self.cost_limiter = DailyCostTracker(float(global_cfg.get("max_llm_cost_per_day_usd", 5.0)))
        self.recent_decisions: list[dict[str, Any]] = []
        self._load_state()

    def evaluate(self, hypothesis: dict) -> ActionDecision:
        hypothesis_id = str(hypothesis.get("id") or hypothesis.get("hypothesis_id") or uuid.uuid4())
        confidence = float(hypothesis.get("confidence") or 0.0)
        cost = float(hypothesis.get("estimated_cost") or hypothesis.get("cost") or 0.0)
        reversibility = str(hypothesis.get("reversibility") or "unknown")
        tools = self._extract_tools(hypothesis)

        try:
            if self.kill_switch:
                return self._record_decision(hypothesis_id, "discard", "kill switch enabled")

            if not self.action_limiter.allow():
                return self._record_decision(hypothesis_id, "discard", "action rate limit reached")

            if not self.cost_limiter.allow(cost):
                return self._record_decision(hypothesis_id, "discard", "daily llm budget exceeded")

            forbidden = set(self.policy.get("forbidden_tools", []))
            blocked_tools = [tool for tool in tools if tool in forbidden]
            if blocked_tools:
                return self._record_decision(
                    hypothesis_id,
                    "discard",
                    f"forbidden tools requested: {', '.join(blocked_tools)}",
                )

            auto_cfg = self.policy.get("auto_execute", {})
            allowed_tools = set(auto_cfg.get("allowed_tools", []))
            if (
                confidence >= float(auto_cfg.get("min_confidence", 0.85))
                and cost <= float(auto_cfg.get("max_cost", 0.3))
                and reversibility in set(auto_cfg.get("required_reversibility", []))
                and all(tool in allowed_tools for tool in tools)
            ):
                return self._record_decision(hypothesis_id, "auto_execute", "meets auto-execute policy")

            stage_cfg = self.policy.get("stage_notify", {})
            if (
                confidence >= float(stage_cfg.get("min_confidence", 0.6))
                and cost <= float(stage_cfg.get("max_cost", 0.8))
            ):
                return self._record_decision(hypothesis_id, "stage_notify", "requires approval")

            discard_cfg = self.policy.get("discard", {})
            if (
                confidence <= float(discard_cfg.get("max_confidence", 0.5))
                or cost >= float(discard_cfg.get("min_cost", 0.9))
            ):
                return self._record_decision(hypothesis_id, "discard", "below policy thresholds")

            return self._record_decision(hypothesis_id, "discard", "did not match any executable policy")
        except Exception as exc:
            logger.exception("Failed to evaluate hypothesis %s", hypothesis_id)
            return self._record_decision(hypothesis_id, "discard", f"evaluation error: {exc}")

    def auto_execute(self, hypothesis: dict) -> dict:
        hypothesis_id = str(hypothesis.get("id") or hypothesis.get("hypothesis_id") or uuid.uuid4())
        results: list[dict[str, Any]] = []
        success = True
        for step in self._extract_plan(hypothesis):
            tool_name = str(step.get("tool") or step.get("action") or "")
            try:
                result = self._execute_step(tool_name, step)
                results.append({"tool": tool_name, "ok": True, "result": result})
            except Exception as exc:
                logger.exception("Auto-execution failed for %s step %s", hypothesis_id, tool_name)
                success = False
                results.append({"tool": tool_name, "ok": False, "error": str(exc)})

        payload = {
            "hypothesis_id": hypothesis_id,
            "status": "completed" if success else "partial_failure",
            "executed_at": datetime.utcnow().isoformat(),
            "results": results,
        }
        self._update_hypothesis_state(hypothesis_id, payload["status"], payload)
        self._emit_event("cerebellum.execution", payload)
        return payload

    def stage_for_approval(self, hypothesis: dict) -> str:
        hypothesis_id = str(hypothesis.get("id") or hypothesis.get("hypothesis_id") or uuid.uuid4())
        timeout_minutes = int(
            self.policy.get("stage_notify", {}).get("telegram", {}).get("timeout_minutes", 60)
        )
        message_text = self._format_telegram_card(hypothesis)
        keyboard = self._telegram_keyboard(hypothesis_id)
        message_id = f"local-{uuid.uuid4()}"
        telegram_result: dict[str, Any] | None = None

        try:
            telegram_result = self._send_telegram_message(message_text, keyboard)
            message_id = str(telegram_result.get("result", {}).get("message_id") or message_id)
        except Exception as exc:
            logger.warning("Telegram approval staging failed for %s: %s", hypothesis_id, exc)

        record = {
            "hypothesis_id": hypothesis_id,
            "message_id": message_id,
            "status": "pending",
            "staged_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=timeout_minutes)).isoformat(),
            "hypothesis": hypothesis,
            "telegram_result": telegram_result,
        }
        pending = self._load_json(self.pending_file, default={})
        pending[hypothesis_id] = record
        self._save_json(self.pending_file, pending)
        self._update_hypothesis_state(hypothesis_id, "pending_approval", record)
        self._emit_event("cerebellum.approval.staged", record)
        return message_id

    def handle_approval(self, hypothesis_id: str, decision: str, user_id: str = "") -> dict:
        pending = self._load_json(self.pending_file, default={})
        record = pending.get(hypothesis_id, {})
        response = {
            "hypothesis_id": hypothesis_id,
            "decision": decision,
            "user_id": user_id,
            "timestamp": datetime.utcnow().isoformat(),
        }
        try:
            if decision == "approve":
                hypothesis = record.get("hypothesis", {})
                response["execution"] = self.auto_execute(hypothesis)
                response["status"] = "approved"
            elif decision == "reject":
                response["status"] = "rejected"
                self._append_jsonl(self.feedback_file, response)
                self._update_hypothesis_state(hypothesis_id, "rejected", response)
            elif decision == "snooze":
                response["status"] = "snoozed"
                record["expires_at"] = (datetime.utcnow() + timedelta(hours=1)).isoformat()
                pending[hypothesis_id] = record
                self._save_json(self.pending_file, pending)
                self._update_hypothesis_state(hypothesis_id, "snoozed", response)
            elif decision == "explain":
                response["status"] = "explained"
                response["explanation"] = record.get("hypothesis", {}).get("reasoning") or record.get(
                    "hypothesis", {}
                ).get("summary", "No explanation available.")
            else:
                response["status"] = "unknown_decision"

            if decision in {"approve", "reject"}:
                pending.pop(hypothesis_id, None)
                self._save_json(self.pending_file, pending)

            self._emit_event("cerebellum.approval", response)
            return response
        except Exception as exc:
            logger.exception("Failed to handle approval for %s", hypothesis_id)
            response["status"] = "error"
            response["error"] = str(exc)
            return response

    def toggle_kill_switch(self, enabled: bool) -> dict:
        self.kill_switch = enabled
        self._persist_state()
        payload = {
            "kill_switch": self.kill_switch,
            "updated_at": datetime.utcnow().isoformat(),
            "command": self.kill_switch_command,
        }
        self._emit_event("cerebellum.kill_switch", payload)
        return payload

    def get_status(self) -> dict:
        pending = self._load_json(self.pending_file, default={})
        return {
            "kill_switch": self.kill_switch,
            "kill_switch_command": self.kill_switch_command,
            "rate_limits": {
                "actions": self.action_limiter.snapshot(),
                "cost": self.cost_limiter.snapshot(),
            },
            "pending_approvals": len(pending),
            "recent_decisions": self.recent_decisions[-10:],
        }

    def _record_decision(self, hypothesis_id: str, decision: str, reason: str) -> ActionDecision:
        action_decision = ActionDecision(
            hypothesis_id=hypothesis_id,
            decision=decision,
            reason=reason,
            timestamp=datetime.utcnow().isoformat(),
        )
        payload = action_decision.to_dict()
        self.recent_decisions.append(payload)
        self.recent_decisions = self.recent_decisions[-50:]
        self._append_jsonl(self.decisions_file, payload)
        logger.info("Arbiter decision %s for %s: %s", decision, hypothesis_id, reason)
        self._emit_event("cerebellum.action", payload)
        self._persist_state()
        return action_decision

    def _extract_plan(self, hypothesis: dict) -> list[dict[str, Any]]:
        plan = hypothesis.get("plan", [])
        if isinstance(plan, dict):
            plan = plan.get("steps", [])
        if isinstance(plan, list):
            return [step for step in plan if isinstance(step, dict)]
        return []

    def _extract_tools(self, hypothesis: dict) -> list[str]:
        tools = []
        for step in self._extract_plan(hypothesis):
            tool_name = step.get("tool") or step.get("action")
            if tool_name:
                tools.append(str(tool_name))
        return tools

    def _execute_step(self, tool_name: str, step: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "browser.screenshot": self._handle_browser_screenshot,
            "browser.navigate": self._handle_browser_navigate,
            "web.search": self._handle_web_search,
            "file.read": self._handle_file_read,
            "memory.query": self._handle_memory_query,
            "model.call": self._handle_model_call,
            "notification.send": self._handle_notification_send,
        }
        if tool_name not in handlers:
            raise ValueError(f"Unsupported auto-execute tool: {tool_name}")
        return handlers[tool_name](step)

    def _handle_browser_screenshot(self, step: dict[str, Any]) -> dict[str, Any]:
        url = str(step.get("url") or "")
        output_path = str(step.get("output_path") or self.state_dir / f"{uuid.uuid4()}.png")
        return {
            "status": "deferred",
            "tool": "browser.screenshot",
            "url": url,
            "output_path": output_path,
            "note": "CDP integration not configured in Phase 4 runtime; step recorded for external executor.",
        }

    def _handle_browser_navigate(self, step: dict[str, Any]) -> dict[str, Any]:
        url = str(step.get("url") or "")
        if not url:
            raise ValueError("browser.navigate requires a url")
        request = urllib.request.Request(url, headers={"User-Agent": "Cerebellum/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return {
                "status": "ok",
                "tool": "browser.navigate",
                "url": url,
                "http_status": getattr(response, "status", None),
                "content_type": response.headers.get("Content-Type"),
            }

    def _handle_web_search(self, step: dict[str, Any]) -> dict[str, Any]:
        query = str(step.get("query") or "")
        if not query:
            raise ValueError("web.search requires a query")
        brave_api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
        if not brave_api_key:
            raise RuntimeError("BRAVE_SEARCH_API_KEY is not configured")
        request = urllib.request.Request(
            f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": brave_api_key,
                "User-Agent": "Cerebellum/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"status": "ok", "tool": "web.search", "query": query, "results": payload}

    def _handle_file_read(self, step: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(step.get("path") or step.get("file") or ""))
        if not str(path):
            raise ValueError("file.read requires a path")
        content = path.read_text(encoding="utf-8")
        return {
            "status": "ok",
            "tool": "file.read",
            "path": str(path),
            "content": content[:10000],
            "truncated": len(content) > 10000,
        }

    def _handle_memory_query(self, step: dict[str, Any]) -> dict[str, Any]:
        endpoint = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
        collection = str(step.get("collection") or os.environ.get("QDRANT_COLLECTION", "memory"))
        vector = step.get("vector")
        if vector is None:
            raise ValueError("memory.query requires a vector payload")
        payload = json.dumps({"vector": vector, "limit": int(step.get("limit", 5))}).encode("utf-8")
        request = urllib.request.Request(
            f"{endpoint.rstrip('/')}/collections/{collection}/points/search",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        return {"status": "ok", "tool": "memory.query", "result": result}

    def _handle_model_call(self, step: dict[str, Any]) -> dict[str, Any]:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        payload = {
            "model": step.get("model") or "openrouter/openai/gpt-5.5",
            "messages": step.get("messages")
            or [{"role": "user", "content": str(step.get("prompt") or "")}],
        }
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://openclaw.local/cerebellum",
                "X-Title": "CEREBELLUM",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        return {"status": "ok", "tool": "model.call", "result": result}

    def _handle_notification_send(self, step: dict[str, Any]) -> dict[str, Any]:
        text = str(step.get("text") or step.get("message") or "")
        if not text:
            raise ValueError("notification.send requires text")
        result = self._send_telegram_message(text)
        return {"status": "ok", "tool": "notification.send", "result": result}

    def _send_telegram_message(
        self, text: str, keyboard: Optional[list[list[dict[str, str]]]] = None
    ) -> dict[str, Any]:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("OPENCLAW_TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            openclaw_bin = Path.home() / ".npm-global" / "bin" / "openclaw"
            if openclaw_bin.exists():
                command = [str(openclaw_bin), "agent", "--message", text, "--channel", "telegram", "--json"]
                completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
                if completed.returncode != 0:
                    raise RuntimeError(
                        f"Telegram bot credentials missing and OpenClaw fallback failed: {completed.stderr.strip()}"
                    )
                stdout = completed.stdout.strip() or "{}"
                return {"ok": True, "source": "openclaw", "stdout": stdout}
            raise RuntimeError("Telegram bot credentials are not configured")

        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}

        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API error {exc.code}: {body}") from exc

    def _telegram_keyboard(self, hypothesis_id: str) -> list[list[dict[str, str]]]:
        return [
            [
                {"text": "✅ Approve", "callback_data": f"approve:{hypothesis_id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{hypothesis_id}"},
            ],
            [
                {"text": "⏰ Snooze 1h", "callback_data": f"snooze:{hypothesis_id}"},
                {"text": "❓ Explain", "callback_data": f"explain:{hypothesis_id}"},
            ],
        ]

    def _format_telegram_card(self, hypothesis: dict) -> str:
        hypothesis_id = str(hypothesis.get("id") or hypothesis.get("hypothesis_id") or "unknown")
        summary = str(hypothesis.get("summary") or hypothesis.get("title") or "No summary provided")
        confidence = hypothesis.get("confidence", "n/a")
        cost = hypothesis.get("estimated_cost") or hypothesis.get("cost") or "n/a"
        tools = ", ".join(self._extract_tools(hypothesis)) or "none"
        return (
            "🧠 CEREBELLUM Approval Required\n\n"
            f"ID: {hypothesis_id}\n"
            f"Summary: {summary}\n"
            f"Confidence: {confidence}\n"
            f"Estimated Cost: {cost}\n"
            f"Tools: {tools}\n"
            f"Reasoning: {hypothesis.get('reasoning') or 'n/a'}"
        )

    def _update_hypothesis_state(self, hypothesis_id: str, state: str, payload: dict[str, Any]) -> None:
        if self.cortex is not None:
            for method_name in ("update_hypothesis_state", "set_hypothesis_state"):
                method = getattr(self.cortex, method_name, None)
                if callable(method):
                    try:
                        method(hypothesis_id, state, payload)
                        return
                    except TypeError:
                        method(hypothesis_id, state)
                        return
                    except Exception:
                        logger.exception("Cortex state update failed via %s", method_name)

        state_data = self._load_json(self.state_dir / "hypothesis_states.json", default={})
        state_data[hypothesis_id] = {
            "state": state,
            "updated_at": datetime.utcnow().isoformat(),
            "payload": payload,
        }
        self._save_json(self.state_dir / "hypothesis_states.json", state_data)

    def _emit_event(self, topic: str, payload: dict[str, Any]) -> None:
        if self.emitter is not None:
            for method_name in ("emit", "publish", "send"):
                method = getattr(self.emitter, method_name, None)
                if callable(method):
                    try:
                        method(topic, payload)
                        return
                    except TypeError:
                        method(payload)
                        return
                    except Exception:
                        logger.exception("Emitter failed via %s", method_name)
                        break
        self._persist_event(topic, payload)

    def _persist_event(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            with sqlite3.connect(self.events_db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        topic TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO events(topic, payload, created_at) VALUES (?, ?, ?)",
                    (topic, json.dumps(payload), datetime.utcnow().isoformat()),
                )
                connection.commit()
        except Exception:
            logger.exception("Failed to persist fallback event %s", topic)

    def _load_state(self) -> None:
        state = self._load_json(self.state_file, default={})
        self.kill_switch = bool(state.get("kill_switch", self.kill_switch))
        self.recent_decisions = state.get("recent_decisions", [])[-50:]

    def _persist_state(self) -> None:
        self._save_json(
            self.state_file,
            {"kill_switch": self.kill_switch, "recent_decisions": self.recent_decisions[-50:]},
        )

    def _load_json(self, path: Path, default: Any) -> Any:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to load JSON from %s", path)
        return default

    def _save_json(self, path: Path, payload: Any) -> None:
        with self._lock:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
