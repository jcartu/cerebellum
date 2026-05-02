"""Tests for cron_instrument.py — CronInstrumenter."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def mock_emitter():
    emitter = MagicMock()
    emitter.emit = MagicMock()
    return emitter


class TestCronInstrumenterSuccess:
    def test_run_success_emits_start_and_end(self, mock_emitter):
        from cerebellum.instruments.cron_instrument import CronInstrumenter
        inst = CronInstrumenter(mock_emitter)

        def my_func(x: int, y: str) -> str:
            return f"{x}-{y}"

        result = inst.run("test_job", my_func, 42, "hello")
        assert result == "42-hello"
        assert mock_emitter.emit.call_count == 2
        calls = mock_emitter.emit.call_args_list
        assert calls[0][0][0] == "cron.start"
        assert calls[0][0][1] == {"job_name": "test_job"}
        assert calls[0][1]["actor"] == "cron"
        assert calls[1][0][0] == "cron.end"
        assert calls[1][0][1]["job_name"] == "test_job"
        assert "duration_ms" in calls[1][0][1]
        assert calls[1][1]["actor"] == "cron"

    def test_run_success_with_kwargs(self, mock_emitter):
        from cerebellum.instruments.cron_instrument import CronInstrumenter
        inst = CronInstrumenter(mock_emitter)

        def my_func(**kwargs):
            return kwargs.get("key", "default")

        result = inst.run("kwarg_job", my_func, key="value")
        assert result == "value"
        assert mock_emitter.emit.call_count == 2

    def test_run_success_no_args(self, mock_emitter):
        from cerebellum.instruments.cron_instrument import CronInstrumenter
        inst = CronInstrumenter(mock_emitter)

        def simple():
            return 42

        result = inst.run("simple_job", simple)
        assert result == 42
        assert mock_emitter.emit.call_count == 2


class TestCronInstrumenterFailure:
    def test_run_failure_emits_error_and_raises(self, mock_emitter):
        from cerebellum.instruments.cron_instrument import CronInstrumenter
        inst = CronInstrumenter(mock_emitter)

        def failing_func():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            inst.run("fail_job", failing_func)

        assert mock_emitter.emit.call_count == 2
        calls = mock_emitter.emit.call_args_list
        assert calls[0][0][0] == "cron.start"
        assert calls[1][0][0] == "cron.error"
        error_payload = calls[1][0][1]
        assert error_payload["job_name"] == "fail_job"
        assert error_payload["error"] == "boom"
        assert error_payload["error_type"] == "ValueError"
        assert "duration_ms" in error_payload
        assert calls[1][1]["actor"] == "cron"

    def test_run_failure_with_different_exception(self, mock_emitter):
        from cerebellum.instruments.cron_instrument import CronInstrumenter
        inst = CronInstrumenter(mock_emitter)

        def failing_func():
            raise RuntimeError("disk full")

        with pytest.raises(RuntimeError, match="disk full"):
            inst.run("disk_job", failing_func)

        error_payload = mock_emitter.emit.call_args_list[1][0][1]
        assert error_payload["error_type"] == "RuntimeError"
        assert error_payload["error"] == "disk full"


class TestCronInstrumenterInit:
    def test_init_sets_emitter(self, mock_emitter):
        from cerebellum.instruments.cron_instrument import CronInstrumenter
        inst = CronInstrumenter(mock_emitter)
        assert inst.emitter is mock_emitter
