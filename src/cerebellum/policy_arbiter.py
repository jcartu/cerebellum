"""Policy-driven action arbiter for evaluating and executing hypotheses."""

import fnmatch
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.client import HTTPSConnection
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from cerebellum.http_client import safe_post
from cerebellum.http_safe import NoRedirectHandler, _safe_opener

logger = logging.getLogger(__name__)

RECENT_DECISIONS_MAX = 50
RECENT_DECISIONS_STATUS_LIMIT = 10
DEFAULT_HTTP_REFERER = "https://openclaw.local/cerebellum"
OPENCLAW_FALLBACK_BINARY_PATHS = (
    Path.home() / ".npm-global" / "bin" / "openclaw",
    Path("/usr/local/bin/openclaw"),
    Path("/usr/bin/openclaw"),
)
COSTLY_AUTO_EXECUTE_TOOLS = {"model.call", "web.search"}

# Execution cost estimates per tool (USD)
TOOL_COST_ESTIMATES: dict[str, float] = {
    "http.get": 0.0,
    "web.search": 0.005,
    "file.read": 0.0,
    "memory.query": 0.001,
    "model.call": 0.01,
    "notification.send": 0.0,
    "notification.summarize": 0.001,
    "proposal.snooze": 0.0,
    "rasputin.search": 0.001,
    "rasputin.recent_facts": 0.001,
    "rasputin.entity_lookup": 0.001,
    "rasputin.episode_summary": 0.001,
    "rasputin.commit_fact": 0.001,
    "rasputin.reflect": 0.01,
}
SENSITIVE_HYPOTHESIS_FIELD_TOKENS = (
    "api_key",
    "api_keys",
    "token",
    "tokens",
    "password",
    "passwords",
    "secret",
    "secrets",
    "authorization",
    "credential",
    "credentials",
)


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
        self.events: deque[float] = deque(maxlen=max_count + 10)
        self._lock = threading.RLock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.events and self.events[0] <= cutoff:
            self.events.popleft()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if len(self.events) >= self.max_count:
                return False
            self.events.append(now)
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
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
        self._lock = threading.RLock()
        self._day = datetime.now(UTC).date()
        self._spent = 0.0

    def allow(self, additional_cost: float) -> bool:
        with self._lock:
            additional = max(additional_cost, 0.0)
            today = datetime.now(UTC).date()
            if today != self._day:
                self._day = today
                self._spent = 0.0
            if self._spent + additional > self.max_cost:
                return False
            self._spent += additional
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            today = datetime.now(UTC).date()
            if today != self._day:
                self._day = today
                self._spent = 0.0
            return {
                "date": self._day.isoformat(),
                "max_cost": self.max_cost,
                "spent": round(self._spent, 4),
                "remaining": round(max(self.max_cost - self._spent, 0.0), 4),
            }


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, *args: Any, connect_host: str, server_hostname: str | None = None, **kwargs: Any):
        self._connect_host = connect_host
        self._server_hostname = server_hostname
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        self.host = self._connect_host
        self.sock = self._create_connection((self._connect_host, self.port), self.timeout, self.source_address)  # type: ignore[attr-defined]
        peer_ip = self._normalize_ip_address(self.sock.getpeername()[0])
        expected_ip = self._normalize_ip_address(self._connect_host)
        if peer_ip != expected_ip:
            self.sock.close()
            raise ValueError(f"Pinned HTTPS peer mismatch: expected {expected_ip}, got {peer_ip}")
        if self._tunnel_host:  # type: ignore[attr-defined]
            self._tunnel()  # type: ignore[attr-defined]
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname or self.host)  # type: ignore[attr-defined]

    @staticmethod
    def _normalize_ip_address(value: str) -> str:
        return str(ipaddress.ip_address(value))


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, connect_host: str, server_hostname: str):
        super().__init__()
        self._connect_host = connect_host
        self._server_hostname = server_hostname

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(
                host,
                connect_host=self._connect_host,
                server_hostname=self._server_hostname,
                **kwargs,
            ),
            req,
        )


class PolicyArbiter:
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
        self.kill_switch_file = self.state_dir / "kill_switch.flag"
        self.runtime_config_path = self.base_dir / "config.json"
        self.emitter = emitter
        self.cortex = cortex
        self._lock = threading.RLock()

        try:
            self.policy = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.exception("Failed to read policy file: %s", self.config_path)
            raise RuntimeError(f"Unable to load arbiter policy from {self.config_path}") from exc

        global_cfg = self.policy.get("global", {})
        self.runtime_config = self._load_runtime_config()
        self.kill_switch = not bool(global_cfg.get("enabled", True))
        self.kill_switch_command = str(global_cfg.get("kill_switch_command", "/cerebellum-halt"))
        self.action_limiter = RateLimiter(int(global_cfg.get("max_actions_per_hour", 10)), 3600)
        self.cost_limiter = DailyCostTracker(float(global_cfg.get("max_llm_cost_per_day_usd", 5.0)))
        self.recent_decisions: list[ActionDecision] = []
        self._load_state()

    def evaluate(self, hypothesis: dict) -> ActionDecision:
        hypothesis_id = str(hypothesis.get("id") or hypothesis.get("hypothesis_id") or uuid.uuid4())
        daily_budget = float(self.cost_limiter.max_cost)
        raw_confidence = hypothesis.get("confidence")
        raw_generation_cost = hypothesis.get("generation_cost_usd", hypothesis.get("cost"))
        raw_estimated_execution_cost = hypothesis.get("estimated_execution_cost_usd")
        try:
            confidence = float(raw_confidence or 0.0)
            generation_cost_usd = float(raw_generation_cost or 0.0)
        except (TypeError, ValueError):
            logger.warning(
                "Rejected hypothesis %s: invalid numeric generation cost/confidence (confidence=%r, generation_cost_usd=%r)",
                hypothesis_id,
                raw_confidence,
                raw_generation_cost,
            )
            return self._record_decision(hypothesis_id, "discard", "invalid generation cost/confidence")
        if (
            not math.isfinite(confidence)
            or not math.isfinite(generation_cost_usd)
            or confidence < 0.0
            or generation_cost_usd < 0.0
        ):
            logger.warning(
                "Rejected hypothesis %s: non-finite or negative generation cost/confidence (confidence=%r, generation_cost_usd=%r)",
                hypothesis_id,
                raw_confidence,
                raw_generation_cost,
            )
            return self._record_decision(hypothesis_id, "discard", "invalid generation cost/confidence")
        if confidence > 1.0:
            logger.warning("Clamped hypothesis %s confidence from %s to 1.0", hypothesis_id, confidence)
            confidence = 1.0
        if generation_cost_usd > daily_budget:
            logger.warning(
                "Clamped hypothesis %s generation cost from %s to daily budget %s",
                hypothesis_id,
                generation_cost_usd,
                daily_budget,
            )
            generation_cost_usd = daily_budget
        estimated_execution_cost_usd = self._coerce_optional_float(raw_estimated_execution_cost)
        reversibility = str(hypothesis.get("reversibility") or "unknown")
        tools = self._extract_tools(hypothesis)
        missing_execution_cost_for_costly_tools = (
            estimated_execution_cost_usd is None and bool(COSTLY_AUTO_EXECUTE_TOOLS.intersection(tools))
        )

        try:
            self._refresh_kill_switch_from_disk()
            if self.kill_switch:
                return self._record_decision(hypothesis_id, "discard", "kill switch enabled")

            if not self.action_limiter.allow():
                return self._record_decision(hypothesis_id, "discard", "action rate limit reached")

            if not self.cost_limiter.allow(generation_cost_usd):
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
                and generation_cost_usd <= float(auto_cfg.get("max_cost", 0.3))
                and reversibility in set(auto_cfg.get("required_reversibility", []))
                and all(tool in allowed_tools for tool in tools)
            ):
                if missing_execution_cost_for_costly_tools:
                    # TODO(Phase 5): allow auto-execution once real execution-cost
                    # estimates are populated for costly tools like model/web calls.
                    return self._record_decision(
                        hypothesis_id,
                        "stage_notify",
                        "missing execution cost estimate for costly auto-execute tools",
                    )
                return self._record_decision(hypothesis_id, "auto_execute", "meets auto-execute policy")

            stage_cfg = self.policy.get("stage_notify", {})
            if (
                confidence >= float(stage_cfg.get("min_confidence", 0.6))
                and generation_cost_usd <= float(stage_cfg.get("max_cost", 0.8))
            ):
                return self._record_decision(hypothesis_id, "stage_notify", "requires approval")

            discard_cfg = self.policy.get("discard", {})
            if (
                confidence <= float(discard_cfg.get("max_confidence", 0.5))
                or generation_cost_usd >= float(discard_cfg.get("min_cost", 0.9))
            ):
                return self._record_decision(hypothesis_id, "discard", "below policy thresholds")

            return self._record_decision(hypothesis_id, "discard", "did not match any executable policy")
        except Exception as exc:
            logger.exception("Failed to evaluate hypothesis %s", hypothesis_id)
            return self._record_decision(hypothesis_id, "discard", f"evaluation error: {exc}")

    def auto_execute(self, hypothesis: dict) -> dict:
        hypothesis_id = str(hypothesis.get("id") or hypothesis.get("hypothesis_id") or uuid.uuid4())
        self._refresh_kill_switch_from_disk()
        if self.kill_switch:
            logger.warning("auto_execute blocked by kill switch: %s", hypothesis_id)
            return {
                "hypothesis_id": hypothesis_id,
                "status": "blocked",
                "executed_at": datetime.now(UTC).isoformat(),
                "results": [],
                "reason": "kill switch enabled",
            }
        daily_budget = float(self.policy.get("global", {}).get("max_llm_cost_per_day_usd", 5.0))
        auto_cfg = self.policy.get("auto_execute", {})
        execution_cost_limit = float(auto_cfg.get("execution_cost_limit_usd", daily_budget * 0.5))
        results: list[dict[str, Any]] = []
        success = True
        execution_cost = 0.0
        halted_for_budget = False
        for step in self._extract_plan(hypothesis):
            tool_name = str(step.get("tool") or step.get("action") or "")
            try:
                result = self._execute_step(tool_name, step)
                step_cost = self._extract_execution_cost(step, result)
                execution_cost += step_cost
                results.append({"tool": tool_name, "ok": True, "cost": step_cost, "result": result})
                if execution_cost > execution_cost_limit:
                    halted_for_budget = True
                    success = False
                    logger.warning(
                        "Auto-execution halted for %s: execution cost %.4f exceeded limit %.4f",
                        hypothesis_id,
                        execution_cost,
                        execution_cost_limit,
                    )
                    break
            except Exception as exc:
                logger.exception("Auto-execution failed for %s step %s", hypothesis_id, tool_name)
                success = False
                results.append({"tool": tool_name, "ok": False, "error": str(exc)})

        payload = {
            "hypothesis_id": hypothesis_id,
            "status": "completed" if success else "partial_failure",
            "executed_at": datetime.now(UTC).isoformat(),
            "execution_cost": round(execution_cost, 6),
            "execution_cost_limit": round(execution_cost_limit, 6),
            "results": results,
        }
        if halted_for_budget:
            payload["reason"] = "execution cost budget exceeded"
        self._update_hypothesis_state(hypothesis_id, payload["status"], payload)  # type: ignore[arg-type]
        self._emit_event("cerebellum.execution", payload)
        return payload

    def stage_for_approval(self, hypothesis: dict) -> str:
        hypothesis_id = str(hypothesis.get("id") or hypothesis.get("hypothesis_id") or uuid.uuid4())
        sanitized_hypothesis = self._sanitize_hypothesis(hypothesis)
        timeout_minutes = int(
            self.policy.get("stage_notify", {}).get("telegram", {}).get("timeout_minutes", 60)
        )
        message_text = self._format_telegram_card(sanitized_hypothesis)
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
            "staged_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=timeout_minutes)).isoformat(),
            "hypothesis": sanitized_hypothesis,
            "telegram_result": telegram_result,
        }
        import fcntl

        lock_path = self.pending_file.with_suffix(".json.lock")
        with self._lock, lock_path.open("w", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                pending = self._load_json(self.pending_file, default={})
                pending[hypothesis_id] = record
                self._save_json(self.pending_file, pending)
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        self._update_hypothesis_state(hypothesis_id, "pending_approval", record)
        self._emit_event("cerebellum.approval.staged", record)
        return message_id

    def handle_approval(self, hypothesis_id: str, decision: str, user_id: str = "") -> dict:
        import fcntl

        lock_path = self.pending_file.with_suffix(".json.lock")
        response = {
            "hypothesis_id": hypothesis_id,
            "decision": decision,
            "user_id": user_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        try:
            if decision == "approve":
                self._refresh_kill_switch_from_disk()
                if self.kill_switch:
                    logger.warning("handle_approval blocked by kill switch: %s", hypothesis_id)
                    response["status"] = "blocked"
                    response["error"] = "kill switch enabled"
                    return response

            with self._lock, lock_path.open("w", encoding="utf-8") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                try:
                    pending = self._load_json(self.pending_file, default={})
                    record = pending.get(hypothesis_id, {})
                    if decision == "snooze":
                        record["expires_at"] = (
                            datetime.now(UTC) + timedelta(hours=1)
                        ).isoformat()
                        pending[hypothesis_id] = record
                        self._save_json(self.pending_file, pending)
                    elif decision in {"approve", "reject"}:
                        pending.pop(hypothesis_id, None)
                        self._save_json(self.pending_file, pending)
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

            if decision == "approve":
                hypothesis = record.get("hypothesis", {})
                response["execution"] = self.auto_execute(hypothesis)  # type: ignore[assignment]
                response["status"] = "approved"
            elif decision == "reject":
                response["status"] = "rejected"
                self._append_jsonl(self.feedback_file, response)
                self._update_hypothesis_state(hypothesis_id, "rejected", response)
            elif decision == "snooze":
                response["status"] = "snoozed"
                self._update_hypothesis_state(hypothesis_id, "snoozed", response)
            elif decision == "explain":
                response["status"] = "explained"
                response["explanation"] = record.get("hypothesis", {}).get("reasoning") or record.get(
                    "hypothesis", {}
                ).get("summary", "No explanation available.")
            else:
                response["status"] = "unknown_decision"

            self._emit_event("cerebellum.approval", response)
            return response
        except Exception as exc:
            logger.exception("Failed to handle approval for %s", hypothesis_id)
            response["status"] = "error"
            response["error"] = str(exc)
            return response

    def toggle_kill_switch(self, enabled: bool) -> dict:
        self.kill_switch = enabled
        self._write_kill_switch_file(enabled)
        self._persist_state()
        payload = {
            "kill_switch": self.kill_switch,
            "updated_at": datetime.now(UTC).isoformat(),
            "command": self.kill_switch_command,
        }
        self._emit_event("cerebellum.kill_switch", payload)
        return payload

    def _refresh_kill_switch_from_disk(self) -> None:
        """Re-read kill-switch state from the shared kill_switch.flag file.

        Enables cross-process halt: when the dashboard toggles the switch,
        the arbiter_loop process observes it on the next evaluate() cycle.
        Uses advisory fcntl locking + atomic replace for race safety.
        """
        try:
            disk_value = self._read_kill_switch_file()
            if isinstance(disk_value, bool):
                self.kill_switch = disk_value
        except Exception:
            logger.exception("Failed to refresh kill switch from %s", self.kill_switch_file)

    def _read_kill_switch_file(self) -> bool | None:
        import fcntl

        path = self.kill_switch_file
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    raw = fh.read().strip()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            if not raw:
                return None
            data = json.loads(raw)
            value = data.get("kill_switch")
            return bool(value) if isinstance(value, bool) else None
        except Exception:
            logger.exception("Failed to read kill switch file %s", path)
            return None

    def _load_runtime_config(self) -> dict[str, Any]:
        try:
            if self.runtime_config_path.exists():
                return json.loads(self.runtime_config_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to load runtime config from %s", self.runtime_config_path)
        return {}

    def _site_url(self) -> str:
        env_value = (
            os.environ.get("CEREBELLUM_HTTP_REFERER")
            or os.environ.get("CEREBELLUM_SITE_URL")
            or os.environ.get("OPENROUTER_HTTP_REFERER")
            or ""
        ).strip()
        if env_value:
            return env_value
        config_value = str(self.runtime_config.get("http_referer") or "").strip()
        if config_value:
            return config_value
        openrouter_cfg = self.runtime_config.get("openrouter")
        if isinstance(openrouter_cfg, dict):
            config_value = str(openrouter_cfg.get("http_referer") or "").strip()
            if config_value:
                return config_value
        config_value = str(self.runtime_config.get("site_url") or "").strip()
        if config_value:
            return config_value
        return DEFAULT_HTTP_REFERER

    def _telegram_fallback_binary(self) -> str | None:
        telegram_cfg = self.runtime_config.get("telegram") if isinstance(self.runtime_config.get("telegram"), dict) else {}
        configured_binary = str(telegram_cfg.get("fallback_binary") or "").strip()
        if configured_binary:
            return configured_binary

        discovered_binary = shutil.which("openclaw")
        if discovered_binary:
            return discovered_binary

        legacy_binary = Path.home() / ".npm-global" / "bin" / "openclaw"
        if legacy_binary.exists():
            return str(legacy_binary)
        return None

    def _write_kill_switch_file(self, enabled: bool) -> None:
        """Atomic write: write to temp file under flock, then os.replace."""
        import fcntl

        path = self.kill_switch_file
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "kill_switch": bool(enabled),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        data = json.dumps(payload, ensure_ascii=False)
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            os.replace(tmp_path, path)
            parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except Exception:
            logger.exception("Failed to write kill switch file %s", path)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                # Best-effort cleanup only; keep the original write failure as the signal.
                logger.debug("Ignoring temporary kill-switch cleanup failure for %s", tmp_path, exc_info=True)

    def _validate_url(self, url: str) -> tuple[str, str]:
        """SSRF guard: only allow http/https to public hosts.

        Returns (safe_ip, host_header) so the caller can connect directly
        to the resolved IP (closing the DNS-rebinding TOCTOU window).
        """
        import ipaddress
        import socket

        if not isinstance(url, str) or not url.strip():
            raise ValueError("URL must be a non-empty string")

        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"URL scheme must be http or https, got: {parsed.scheme}")

        hostname = parsed.hostname
        if not hostname:
            raise ValueError("URL is missing a hostname")

        forbidden_hosts = {"localhost", "metadata.google.internal", "metadata"}
        if hostname.lower() in forbidden_hosts:
            raise ValueError(f"Forbidden hostname: {hostname}")

        # If the host is already a literal IP, validate it directly.
        try:
            literal_ip = ipaddress.ip_address(hostname)
            self._assert_public_ip(literal_ip, hostname)
            host_header = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
            return str(literal_ip), host_header
        except ValueError:
            pass

        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            raise ValueError(f"Cannot resolve hostname {hostname}: {exc}") from exc

        # Validate every returned address; pick the first IPv4 (then IPv6) as safe_ip.
        safe_ip: str | None = None
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            self._assert_public_ip(ip, hostname)
            if safe_ip is None:
                safe_ip = str(ip)

        if safe_ip is None:
            raise ValueError(f"No usable address for hostname {hostname}")

        host_header = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
        return safe_ip, host_header

    def _assert_public_ip(self, ip: Any, hostname: str) -> None:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"URL resolves to forbidden address {ip} (host={hostname})")

    def _validate_file_path(self, path: Path) -> None:
        """Restrict file.read to allowlisted roots; reject traversal & secrets."""
        if not isinstance(path, Path):
            raise ValueError("file path must be a Path instance")

        try:
            resolved = Path(os.path.realpath(path.expanduser(), strict=True))
        except Exception as exc:
            raise ValueError(f"Unable to resolve real path {path}: {exc}") from exc

        resolved_str = str(resolved)

        # Blanket-reject any access under sensitive system roots.
        forbidden_prefixes = (
            "/etc/",
            "/root/",
            "/proc/",
            "/sys/",
            "/boot/",
            "/var/log/",
        )
        for prefix in forbidden_prefixes:
            if resolved_str == prefix.rstrip("/") or resolved_str.startswith(prefix):
                raise ValueError(f"Forbidden path prefix {prefix!r} in {resolved_str}")

        forbidden_patterns = (
            "/.ssh/",
            "/.aws/",
            "/.gnupg/",
            "/.config/gcloud/",
            "/.kube/",
            "/.docker/",
        )
        for pattern in forbidden_patterns:
            if pattern in resolved_str:
                raise ValueError(f"Forbidden path pattern {pattern!r} in {resolved_str}")

        basename = resolved.name
        lower_basename = basename.lower()
        allowed_suffixes = {
            ".py",
            ".md",
            ".txt",
            ".log",
            ".yaml",
            ".toml",
            ".cfg",
            ".ini",
            ".sh",
            ".html",
            ".css",
            ".js",
            ".ts",
        }
        sensitive_patterns = (
            ".env*",
            "*.db",
            "*.json",
            "policy.yaml",
            "*.flag",
            "*.key",
            "*.pem",
            "*.crt",
        )
        if lower_basename != "config.json":
            for pattern in sensitive_patterns:
                if fnmatch.fnmatch(lower_basename, pattern):
                    raise ValueError(f"Refusing to read sensitive file: {resolved_str}")
        if resolved.suffix.lower() not in allowed_suffixes and lower_basename != "config.json":
            raise ValueError(f"Refusing to read disallowed file type: {resolved_str}")

        allowed_roots = [
            self.base_dir.resolve(),
        ]
        for root in allowed_roots:
            try:
                resolved.relative_to(root)
                return
            except ValueError:
                continue

        raise ValueError(f"Path {resolved_str} is outside allowed roots")

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
            "recent_decisions": [decision.to_dict() for decision in self.recent_decisions[-RECENT_DECISIONS_STATUS_LIMIT:]],
        }

    def _record_decision(self, hypothesis_id: str, decision: str, reason: str) -> ActionDecision:
        action_decision = ActionDecision(
            hypothesis_id=hypothesis_id,
            decision=decision,
            reason=reason,
            timestamp=datetime.now(UTC).isoformat(),
        )
        payload = action_decision.to_dict()
        self.recent_decisions.append(action_decision)
        self.recent_decisions = self.recent_decisions[-RECENT_DECISIONS_MAX:]
        self._append_jsonl(self.decisions_file, payload)
        logger.info("Arbiter decision %s for %s: %s", decision, hypothesis_id, reason)
        self._emit_event("cerebellum.action", payload)
        self._persist_state()
        return action_decision

    def _sanitize_hypothesis(self, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key).lower()
                if any(token in normalized_key for token in SENSITIVE_HYPOTHESIS_FIELD_TOKENS):
                    sanitized[str(key)] = "[REDACTED]"
                    continue
                sanitized[str(key)] = self._sanitize_hypothesis(item)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize_hypothesis(item) for item in value]
        return value

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
            "http.get": self._handle_http_get,
            "web.search": self._handle_web_search,
            "file.read": self._handle_file_read,
            "memory.query": self._handle_memory_query,
            "model.call": self._handle_model_call,
            "notification.send": self._handle_notification_send,
            "notification.summarize": self._handle_notification_summarize,
            "proposal.snooze": self._handle_proposal_snooze,
            "rasputin.search": self._handle_rasputin_search,
            "rasputin.recent_facts": self._handle_rasputin_recent_facts,
            "rasputin.entity_lookup": self._handle_rasputin_entity_lookup,
            "rasputin.episode_summary": self._handle_rasputin_episode_summary,
            "rasputin.commit_fact": self._handle_rasputin_commit_fact,
            "rasputin.reflect": self._handle_rasputin_reflect,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return {
                "status": "error",
                "tool": tool_name,
                "error": f"No handler for tool: {tool_name}",
                "estimated_execution_cost_usd": 0.0,
            }
        cost = TOOL_COST_ESTIMATES.get(tool_name, 0.0)
        try:
            result = handler(step)
            return {
                "status": "ok",
                "tool": tool_name,
                "result": result,
                "estimated_execution_cost_usd": cost,
            }
        except Exception as e:
            return {
                "status": "error",
                "tool": tool_name,
                "error": str(e),
                "estimated_execution_cost_usd": cost,
            }
    def _handle_http_get(self, step: dict[str, Any]) -> dict[str, Any]:
        url = str(step.get("url") or "")
        if not url:
            raise ValueError("http.get requires a url")
        safe_ip, host_header = self._validate_url(url)

        parsed = urllib.parse.urlparse(url)
        # Rebuild netloc using the validated IP; preserve port if present.
        if parsed.port is not None:
            new_netloc = f"{safe_ip}:{parsed.port}"
        else:
            new_netloc = safe_ip
        rebuilt = parsed._replace(netloc=new_netloc).geturl()

        request = urllib.request.Request(
            rebuilt,
            headers={
                "User-Agent": "Cerebellum/1.0",
                "Host": host_header,
            },
        )
        if parsed.scheme == "https":
            opener = urllib.request.build_opener(
                NoRedirectHandler(),
                _PinnedHTTPSHandler(connect_host=safe_ip, server_hostname=parsed.hostname or ""),
            )
        else:
            opener = urllib.request.build_opener(NoRedirectHandler())
        with opener.open(request, timeout=20) as response:
            if parsed.scheme == "http":
                response_host = response.headers.get("Host")
                if response_host is not None and response_host != host_header:
                    raise ValueError(
                        f"HTTP response Host header mismatch: expected {host_header}, got {response_host}"
                    )
            try:
                http_status = getattr(response, "status", None)
                content_type = response.headers.get("Content-Type")
            except TypeError:
                http_status = None
                content_type = None
            return {
                "status": "ok",
                "tool": "http.get",
                "url": url,
                "resolved_ip": safe_ip,
                "http_status": http_status,
                "content_type": content_type,
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
        MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB
        with _safe_opener.open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("web.search response exceeded 2 MiB cap")
        payload = json.loads(raw.decode("utf-8"))
        return {"status": "ok", "tool": "web.search", "query": query, "results": payload}

    def _handle_file_read(self, step: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(step.get("path") or step.get("file") or ""))
        if not str(path):
            raise ValueError("file.read requires a path")
        self._validate_file_path(path)
        resolved_path = path.expanduser().resolve(strict=True)
        self._validate_file_path(resolved_path)
        content = resolved_path.read_text(encoding="utf-8")
        return {
            "status": "ok",
            "tool": "file.read",
            "path": str(resolved_path),
            "content": content[:10000],
            "truncated": len(content) > 10000,
        }

    def _handle_memory_query(self, step: dict[str, Any]) -> dict[str, Any]:
        qdrant_url = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
        parsed = urllib.parse.urlparse(qdrant_url)
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"Qdrant URL must be localhost, got {parsed.hostname}")
        collection = str(step.get("collection") or os.environ.get("QDRANT_COLLECTION", "memory"))
        if not re.fullmatch(r"^[A-Za-z0-9_\-]{1,64}$", collection):
            raise ValueError(f"Invalid Qdrant collection name: {collection}")
        vector = step.get("vector")
        if vector is None:
            raise ValueError("memory.query requires a vector payload")
        payload = json.dumps({"vector": vector, "limit": int(step.get("limit", 5))}).encode("utf-8")
        request = urllib.request.Request(
            f"{qdrant_url.rstrip('/')}/collections/{urllib.parse.quote(collection, safe='')}/points/search",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        MAX_RESPONSE_BYTES = 4 * 1024 * 1024
        with _safe_opener.open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("memory.query response exceeded 4 MiB cap")
        result = json.loads(raw.decode("utf-8"))
        return {"status": "ok", "tool": "memory.query", "result": result}

    def _handle_model_call(self, step: dict[str, Any]) -> dict[str, Any]:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        model_candidates = self.policy.get("model_candidates", ["openai/gpt-4o"])
        if step.get("model") not in model_candidates:
            raise ValueError(f"Model {step.get('model')} not in allowlist {model_candidates}")
        payload = {
            "model": step.get("model") or "openai/gpt-4o",
            "messages": step.get("messages")
            or [{"role": "user", "content": str(step.get("prompt") or "")}],
        }
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": self._site_url(),
                "X-Title": "CEREBELLUM",
            },
        )
        MAX_RESPONSE_BYTES = 8 * 1024 * 1024
        with _safe_opener.open(request, timeout=60) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("model.call response exceeded 8 MiB cap")
        result = json.loads(raw.decode("utf-8"))
        return {"status": "ok", "tool": "model.call", "result": result}

    def _handle_notification_send(self, step: dict[str, Any]) -> dict[str, Any]:
        text = str(step.get("text") or step.get("message") or "")
        if not text:
            raise ValueError("notification.send requires text")
        result = self._send_telegram_message(text)
        return {"status": "ok", "tool": "notification.send", "result": result}

    # -----------------------------------------------------------------------
    # New handlers (Phase 4)
    # -----------------------------------------------------------------------

    def _handle_notification_summarize(self, step: dict[str, Any]) -> dict[str, Any]:
        """Summarize recent events and post to Telegram.

        Args:
            step: Step payload with optional 'hours' (default 1) and 'channel'.

        Returns:
            Execution result dict.
        """
        hours = float(step.get("hours", 1))
        since = datetime.now(UTC) - timedelta(hours=hours)

        events = self._emitter.query(since=since, limit=50)  # type: ignore[attr-defined]
        if not events:
            return {"status": "ok", "tool": "notification.summarize", "result": {"message": "No recent events"}}

        # Build summary text
        type_counts: Counter[str] = Counter()
        for event in events:
            type_counts[event.get("type", "unknown")] += 1

        summary_lines = [f"📊 Events in last {hours}h: {len(events)} total"]
        for etype, count in type_counts.most_common(5):
            summary_lines.append(f"  • {etype}: {count}")
        summary_text = "\n".join(summary_lines)

        result = self._send_telegram_message(summary_text)
        return {"status": "ok", "tool": "notification.summarize", "result": result}

    def _handle_proposal_snooze(self, step: dict[str, Any]) -> dict[str, Any]:
        """Snooze a proposal for later review.

        Args:
            step: Step payload with 'proposal_id' and optional 'duration_minutes'.

        Returns:
            Execution result dict.
        """
        proposal_id = str(step.get("proposal_id", ""))
        if not proposal_id:
            raise ValueError("proposal.snooze requires proposal_id")
        duration = int(step.get("duration_minutes", 60))
        snooze_until = datetime.now(UTC) + timedelta(minutes=duration)

        return {
            "status": "ok",
            "tool": "proposal.snooze",
            "result": {
                "proposal_id": proposal_id,
                "snoozed_until": snooze_until.isoformat(),
                "duration_minutes": duration,
            },
        }

    # RASPUTIN MCP handlers (port 8808)
    RASPUTIN_MCP_URL = "http://localhost:8808"

    def _handle_rasputin_search(self, step: dict[str, Any]) -> dict[str, Any]:
        """Query RASPUTIN memory by string.

        Args:
            step: Step payload with 'query' string.

        Returns:
            Execution result dict.
        """
        query = str(step.get("query", ""))
        if not query:
            raise ValueError("rasputin.search requires query")
        return self._call_rasputin_mcp("search", {"query": query})

    def _handle_rasputin_recent_facts(self, step: dict[str, Any]) -> dict[str, Any]:
        """List recent RASPUTIN facts.

        Args:
            step: Step payload with optional 'limit' (default 10).

        Returns:
            Execution result dict.
        """
        limit = int(step.get("limit", 10))
        return self._call_rasputin_mcp("recent_facts", {"limit": limit})

    def _handle_rasputin_entity_lookup(self, step: dict[str, Any]) -> dict[str, Any]:
        """Resolve an entity in RASPUTIN.

        Args:
            step: Step payload with 'entity' name.

        Returns:
            Execution result dict.
        """
        entity = str(step.get("entity", ""))
        if not entity:
            raise ValueError("rasputin.entity_lookup requires entity")
        return self._call_rasputin_mcp("entity_lookup", {"entity": entity})

    def _handle_rasputin_episode_summary(self, step: dict[str, Any]) -> dict[str, Any]:
        """Fetch RASPUTIN's view of recent activity.

        Args:
            step: Step payload with optional 'hours' (default 24).

        Returns:
            Execution result dict.
        """
        hours = int(step.get("hours", 24))
        return self._call_rasputin_mcp("episode_summary", {"hours": hours})

    def _handle_rasputin_commit_fact(self, step: dict[str, Any]) -> dict[str, Any]:
        """Write a fact to RASPUTIN (requires approval, never auto-execute).

        Args:
            step: Step payload with 'fact' string.

        Returns:
            Execution result dict.
        """
        fact = str(step.get("fact", ""))
        if not fact:
            raise ValueError("rasputin.commit_fact requires fact")
        return self._call_rasputin_mcp("commit_fact", {"fact": fact})

    def _handle_rasputin_reflect(self, step: dict[str, Any]) -> dict[str, Any]:
        """Trigger a reflection cycle in RASPUTIN.

        Args:
            step: Step payload with optional 'prompt'.

        Returns:
            Execution result dict.
        """
        prompt = str(step.get("prompt", ""))
        return self._call_rasputin_mcp("reflect", {"prompt": prompt})

    def _call_rasputin_mcp(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call a RASPUTIN MCP tool via HTTP.

        Args:
            tool: MCP tool name.
            params: Tool parameters.

        Returns:
            Execution result dict.
        """
        payload = {"tool": tool, "params": params}
        try:
            response = safe_post(
                f"{self.RASPUTIN_MCP_URL}/tools/call",
                json=payload,
                timeout=30.0,
            )
            return {
                "status": "ok",
                "tool": f"rasputin.{tool}",
                "result": response.json(),
            }
        except Exception as exc:
            logger.error("RASPUTIN MCP call failed for %s: %s", tool, exc)
            return {
                "status": "error",
                "tool": f"rasputin.{tool}",
                "error": str(exc),
            }

    def _send_telegram_message(
        self, text: str, keyboard: list[list[dict[str, str]]] | None = None
    ) -> dict[str, Any]:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("OPENCLAW_TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            openclaw_bin = self._resolve_openclaw_binary()
            if openclaw_bin is not None:
                command = [str(openclaw_bin), "agent", "--message", text, "--channel", "telegram", "--json"]
                completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
                if completed.returncode != 0:
                    raise RuntimeError(
                        f"Telegram bot credentials missing and OpenClaw fallback failed: {completed.stderr.strip()}"
                    )
                stdout = completed.stdout.strip() or "{}"
                return {"ok": True, "source": "openclaw", "stdout": stdout}
            logger.warning("Telegram bot credentials missing and no approved OpenClaw fallback is available")
            return {
                "ok": False,
                "source": "openclaw",
                "skipped": True,
                "reason": "Telegram bot credentials are not configured",
            }

        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}

        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        MAX_TELEGRAM_RESPONSE_BYTES = 256 * 1024  # 256 KiB
        try:
            with _safe_opener.open(request, timeout=30) as response:
                raw = response.read(MAX_TELEGRAM_RESPONSE_BYTES + 1)
            if len(raw) > MAX_TELEGRAM_RESPONSE_BYTES:
                raise RuntimeError("Telegram response exceeded 256 KiB cap")
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read(MAX_TELEGRAM_RESPONSE_BYTES + 1)
            if len(body_bytes) > MAX_TELEGRAM_RESPONSE_BYTES:
                body = "<response truncated at 256 KiB>"
            else:
                body = body_bytes.decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API error {exc.code}: {body}") from exc

    def _extract_execution_cost(self, step: dict[str, Any], result: dict[str, Any]) -> float:
        for source in (result, step):
            for key in ("cost", "execution_cost", "estimated_cost"):
                value = source.get(key)
                try:
                    if value is not None:
                        return max(0.0, float(value))
                except (TypeError, ValueError):
                    logger.warning("Ignoring non-numeric execution cost %r for key %s", value, key)
        return 0.0

    def _coerce_optional_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed < 0.0:
            return None
        return parsed

    def _resolve_openclaw_binary(self) -> Path | None:
        telegram_cfg = self.runtime_config.get("telegram") if isinstance(self.runtime_config.get("telegram"), dict) else {}
        configured_binary = str(telegram_cfg.get("fallback_binary") or "").strip()
        allowed_paths = {path.expanduser().resolve(strict=False) for path in OPENCLAW_FALLBACK_BINARY_PATHS}
        if configured_binary:
            candidates = [Path(configured_binary).expanduser()]
        else:
            which_binary = shutil.which("openclaw")
            candidates = []
            if which_binary:
                candidates.append(Path(which_binary))
            candidates.extend(OPENCLAW_FALLBACK_BINARY_PATHS)
        for candidate in candidates:
            if not candidate.exists():
                logger.warning("Skipping OpenClaw fallback at %s: binary not found", candidate)
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except Exception as exc:
                logger.warning("Skipping OpenClaw fallback at %s: %s", candidate, exc)
                continue
            if resolved not in allowed_paths:
                logger.warning(
                    "Skipping OpenClaw fallback at %s: resolved path %s is not in allowlist",
                    candidate,
                    resolved,
                )
                continue
            if candidate.is_symlink() and self._is_world_writable_path(resolved.parent):
                logger.warning(
                    "Skipping OpenClaw fallback at %s: symlink target parent %s is world-writable",
                    candidate,
                    resolved.parent,
                )
                continue
            if not os.access(resolved, os.X_OK):
                logger.warning("Skipping OpenClaw fallback at %s: binary is not executable", candidate)
                continue
            return candidate
        logger.warning("No approved OpenClaw fallback binary found")
        return None

    def _is_world_writable_path(self, path: Path) -> bool:
        try:
            mode = path.stat().st_mode
        except OSError:
            return True
        return bool(mode & stat.S_IWOTH)

    def _telegram_keyboard(self, hypothesis_id: str) -> list[list[dict[str, str]]]:
        return [
            [
                {"text": "Approve", "callback_data": f"approve:{hypothesis_id}"},
                {"text": "Reject", "callback_data": f"reject:{hypothesis_id}"},
            ],
            [
                {"text": "Snooze 1h", "callback_data": f"snooze:{hypothesis_id}"},
                {"text": "Explain", "callback_data": f"explain:{hypothesis_id}"},
            ],
        ]

    def _format_telegram_card(self, hypothesis: dict) -> str:
        hypothesis_id = str(hypothesis.get("id") or hypothesis.get("hypothesis_id") or "unknown")
        summary = str(hypothesis.get("summary") or hypothesis.get("title") or "No summary provided")
        confidence = hypothesis.get("confidence", "n/a")
        generation_cost = hypothesis.get("generation_cost_usd", hypothesis.get("cost", "n/a"))
        execution_cost = hypothesis.get("estimated_execution_cost_usd")
        tools = ", ".join(self._extract_tools(hypothesis)) or "none"
        return (
            "CEREBELLUM Approval Required\n\n"
            f"ID: {hypothesis_id}\n"
            f"Summary: {summary}\n"
            f"Confidence: {confidence}\n"
            f"Generation Cost (USD): {generation_cost}\n"
            f"Estimated Execution Cost (USD): {execution_cost if execution_cost is not None else 'unknown'}\n"
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
                    except TypeError as exc:
                        logger.debug(
                            "Proposer state update via %s(hypothesis_id, state, payload) failed; trying fallback signature: %s",
                            method_name,
                            exc,
                        )
                        try:
                            method(hypothesis_id, state)
                            return
                        except TypeError as fallback_exc:
                            logger.debug(
                                "Proposer state update via %s(hypothesis_id, state) failed after fallback attempt: %s",
                                method_name,
                                fallback_exc,
                            )
                        except Exception:
                            logger.exception("Proposer state update failed via %s(hypothesis_id, state)", method_name)
                    except Exception:
                        logger.exception("Proposer state update failed via %s", method_name)
            logger.warning(
                "No compatible proposer state update signature succeeded for %s; falling back to local persistence",
                hypothesis_id,
            )
            logger.warning("Falling back to local hypothesis state persistence for %s", hypothesis_id)

        state_data = self._load_json(self.state_dir / "hypothesis_states.json", default={})
        state_data[hypothesis_id] = {
            "state": state,
            "updated_at": datetime.now(UTC).isoformat(),
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
        """Fallback event persistence: append to JSONL in state_dir.

        The canonical event store is events.db via EventBus.
        This fallback is used only when no emitter is wired.
        """
        fallback_path = self.state_dir / "arbiter_fallback_events.jsonl"
        record = {
            "topic": topic,
            "payload": payload,
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            self._append_jsonl(fallback_path, record)
        except Exception:
            logger.exception("Failed to persist fallback event %s", topic)

    def _load_state(self) -> None:
        state = self._load_json(self.state_file, default={})
        self.kill_switch = bool(state.get("kill_switch", self.kill_switch))
        decisions: list[ActionDecision] = []
        for item in state.get("recent_decisions", [])[-RECENT_DECISIONS_MAX:]:
            if not isinstance(item, dict):
                continue
            try:
                decisions.append(ActionDecision(**item))
            except TypeError:
                logger.debug("Skipping malformed persisted decision record: %s", item)
        self.recent_decisions = decisions

    def _persist_state(self) -> None:
        self._save_json(
            self.state_file,
            {
                "kill_switch": self.kill_switch,
                "recent_decisions": [decision.to_dict() for decision in self.recent_decisions[-RECENT_DECISIONS_MAX:]],
            },
        )

    def _load_json(self, path: Path, default: Any) -> Any:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to load JSON from %s", path)
        return default

    def _save_json(self, path: Path, payload: Any) -> None:
        """Atomically write JSON: temp file in same dir, fsync, os.replace."""
        with self._lock:
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            data = json.dumps(payload, indent=2, ensure_ascii=False)
            try:
                with tmp_path.open("w", encoding="utf-8") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, path)
            except Exception:
                logger.exception("Atomic save failed for %s", path)
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    # Best-effort cleanup only; do not mask the original JSON write failure.
                    logger.debug("Ignoring temporary JSON cleanup failure for %s", tmp_path, exc_info=True)
                raise

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


BasalGanglia = PolicyArbiter
