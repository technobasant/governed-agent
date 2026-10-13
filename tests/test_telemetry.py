from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ga_agent.__main__ import main as agent_main
from ga_core import telemetry
from ga_core.telemetry import JsonFormatter, log, run_id_var, setup_logging, setup_tracing


def test_log_lines_are_json_with_the_run_id(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO")
    token = run_id_var.set("run_abc")
    try:
        log(logging.getLogger("t"), "hello", rows=3)
    finally:
        run_id_var.reset(token)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["msg"] == "hello" and line["run_id"] == "run_abc" and line["rows"] == 3


def test_exceptions_are_logged_as_a_field() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("t").makeRecord(
            "t", logging.ERROR, __file__, 1, "failed", (), exc_info=__import__("sys").exc_info()
        )
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exc"]


def test_tracing_is_off_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    setup_tracing("test")
    assert telemetry._configured is False


def test_tracing_installs_an_exporter_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    monkeypatch.setattr(telemetry, "_configured", False)
    installed: list[object] = []
    monkeypatch.setattr("ga_core.telemetry.trace.set_tracer_provider", installed.append)
    setup_tracing("test")
    setup_tracing("test")
    assert len(installed) == 1


def test_agent_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], warehouse_path: Path
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "ga_agent",
            "How many orders were returned?",
            "--model",
            "demo",
            "--warehouse",
            str(warehouse_path),
        ],
    )
    monkeypatch.delenv("GA_MCP_URL", raising=False)
    agent_main()
    result = json.loads(capsys.readouterr().out)
    assert result["disposition"] == "published"
