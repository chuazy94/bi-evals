"""Tests for `bi-evals doctor`.

Uses a real localhost HTTP server (matching tests/test_api_endpoint.py)
rather than mocking urlopen — gives us a higher-fidelity check that the
POST + parse path works. Snowflake and Anthropic checks are mocked,
since they can't be exercised in unit tests.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bi_evals.cli import cli
from bi_evals.config import (
    AgentConfig,
    ApiEndpointConfig,
    BiEvalsConfig,
    DatabaseConfig,
    ProjectConfig,
    ReportingConfig,
    ToolConfig,
)
from bi_evals.db.client import QueryResult
from bi_evals.doctor import (
    CheckResult,
    check_builtin_setup,
    check_byo_endpoint,
    format_report,
    is_failing,
)


# ──────────────────────────────────────────────────────────────────────────────
# Mock HTTP server
# ──────────────────────────────────────────────────────────────────────────────


class _MockEndpointHandler(BaseHTTPRequestHandler):
    response_data: Any = {}
    response_status: int = 200
    response_body_override: bytes | None = None

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(
            content_length
        )  # consume but ignore — doctor sends a synthetic question
        self.send_response(self.__class__.response_status)
        if self.__class__.response_body_override is not None:
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(self.__class__.response_body_override)
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.__class__.response_data).encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture()
def mock_endpoint():
    server = HTTPServer(("127.0.0.1", 0), _MockEndpointHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Reset between tests
    _MockEndpointHandler.response_data = {}
    _MockEndpointHandler.response_status = 200
    _MockEndpointHandler.response_body_override = None
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ──────────────────────────────────────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────────────────────────────────────


def _byo_config(url: str, tmp_path: Path) -> BiEvalsConfig:
    config = BiEvalsConfig(
        project=ProjectConfig(name="t"),
        agent=AgentConfig(
            type="api_endpoint",
            endpoint=ApiEndpointConfig(url=url),
        ),
        database=DatabaseConfig(type="snowflake"),
        reporting=ReportingConfig(results_dir="results/"),
    )
    config._base_dir = tmp_path
    return config


def _builtin_config(
    tmp_path: Path, *, with_system_prompt: bool = True, with_tools: bool = True
) -> BiEvalsConfig:
    system_prompt_path = ""
    if with_system_prompt:
        prompt_file = tmp_path / "system-prompt.md"
        prompt_file.write_text("you are an agent")
        system_prompt_path = "system-prompt.md"

    tools = []
    if with_tools:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        tools = [
            ToolConfig(
                name="read_skill_file",
                type="file_reader",
                config={"base_dir": "skills/"},
            )
        ]

    config = BiEvalsConfig(
        project=ProjectConfig(name="t"),
        agent=AgentConfig(
            type="anthropic_tool_loop",
            model="claude-sonnet-4-6",
            system_prompt=system_prompt_path,
            tools=tools,
        ),
        database=DatabaseConfig(type="snowflake"),
        reporting=ReportingConfig(results_dir="results/"),
    )
    config._base_dir = tmp_path
    return config


def _mock_snowflake_ok():
    """Returns a patch context manager that stubs create_db_client to return
    a client whose execute() returns a one-row QueryResult."""

    class _StubClient:
        def execute(self, sql: str) -> QueryResult:
            return QueryResult(columns=["1"], rows=[{"1": 1}], row_count=1)

    return patch("bi_evals.doctor.create_db_client", return_value=_StubClient())


def _mock_snowflake_fail(exc: Exception | None = None, error_text: str | None = None):
    """Stub create_db_client so the doctor sees either a connection error
    (exc raised) or a QueryResult with .error populated."""

    if exc is not None:
        return patch("bi_evals.doctor.create_db_client", side_effect=exc)

    class _StubClient:
        def execute(self, sql: str) -> QueryResult:
            return QueryResult(
                columns=[], rows=[], row_count=0, error=error_text or "boom"
            )

    return patch("bi_evals.doctor.create_db_client", return_value=_StubClient())


def _mock_promptfoo_ok():
    return patch("bi_evals.doctor.shutil.which", return_value="/usr/local/bin/npx")


def _mock_promptfoo_missing():
    return patch("bi_evals.doctor.shutil.which", return_value=None)


# ──────────────────────────────────────────────────────────────────────────────
# BYO mode tests
# ──────────────────────────────────────────────────────────────────────────────


class TestCheckByoEndpoint:
    def test_minimum_valid_response(self, mock_endpoint, tmp_path: Path) -> None:
        _MockEndpointHandler.response_data = {"sql": "SELECT 1"}
        config = _byo_config(mock_endpoint, tmp_path)
        with _mock_snowflake_ok(), _mock_promptfoo_ok():
            results = check_byo_endpoint(config)

        assert not is_failing(results), format_report(results, mode="BYO")
        names = {r.name for r in results if r.severity == "ok"}
        assert "Endpoint reachable" in names
        assert "Response is valid JSON" in names
        assert "Schema validation" in names
        # 'text', 'files_read', 'trace' all absent → warnings
        assert any(r.severity == "warn" for r in results)

    def test_full_response(self, mock_endpoint, tmp_path: Path) -> None:
        _MockEndpointHandler.response_data = {
            "sql": "SELECT 1",
            "text": "hello",
            "files_read": ["SKILL.md"],
            "trace": [
                {
                    "type": "tool_use",
                    "tool_name": "read_skill_file",
                    "tool_input": {"path": "SKILL.md"},
                }
            ],
        }
        config = _byo_config(mock_endpoint, tmp_path)
        with _mock_snowflake_ok(), _mock_promptfoo_ok():
            results = check_byo_endpoint(config)

        assert not is_failing(results), format_report(results, mode="BYO")
        # No warnings — every optional field present
        warns = [r for r in results if r.severity == "warn"]
        assert warns == [], [(r.name, r.detail) for r in warns]

    def test_missing_sql_fails(self, mock_endpoint, tmp_path: Path) -> None:
        _MockEndpointHandler.response_data = {"text": "hello no fence here"}
        config = _byo_config(mock_endpoint, tmp_path)
        with _mock_snowflake_ok(), _mock_promptfoo_ok():
            results = check_byo_endpoint(config)
        assert is_failing(results)
        fails = [r for r in results if r.severity == "fail"]
        assert any("SQL retrievable" in r.name for r in fails)

    def test_sql_in_text_fence_passes(self, mock_endpoint, tmp_path: Path) -> None:
        _MockEndpointHandler.response_data = {
            "text": "Here you go:\n```sql\nSELECT 1\n```",
        }
        config = _byo_config(mock_endpoint, tmp_path)
        with _mock_snowflake_ok(), _mock_promptfoo_ok():
            results = check_byo_endpoint(config)
        assert not is_failing(results), format_report(results, mode="BYO")

    def test_html_response_fails_json_parse(
        self, mock_endpoint, tmp_path: Path
    ) -> None:
        _MockEndpointHandler.response_body_override = b"<html>500 error</html>"
        config = _byo_config(mock_endpoint, tmp_path)
        results = check_byo_endpoint(config)
        # Should fail at "Response is valid JSON"
        fails = [r for r in results if r.severity == "fail"]
        assert any("valid JSON" in r.name for r in fails)

    def test_http_500_fails(self, mock_endpoint, tmp_path: Path) -> None:
        _MockEndpointHandler.response_status = 500
        _MockEndpointHandler.response_data = {"error": "boom"}
        config = _byo_config(mock_endpoint, tmp_path)
        results = check_byo_endpoint(config)
        fails = [r for r in results if r.severity == "fail"]
        assert any(r.name == "Endpoint reachable" for r in fails)

    def test_connection_refused(self, tmp_path: Path) -> None:
        config = _byo_config("http://127.0.0.1:1", tmp_path)  # nothing listens here
        results = check_byo_endpoint(config)
        fails = [r for r in results if r.severity == "fail"]
        assert any(r.name == "Endpoint reachable" for r in fails)

    def test_wrong_mode_fails_fast(self, tmp_path: Path) -> None:
        config = _builtin_config(tmp_path)  # anthropic_tool_loop
        results = check_byo_endpoint(config)
        assert is_failing(results)
        assert any("BYO mode" in r.name for r in results)

    def test_files_read_derivable_from_trace(
        self, mock_endpoint, tmp_path: Path
    ) -> None:
        # No top-level files_read, but trace has it
        _MockEndpointHandler.response_data = {
            "sql": "SELECT 1",
            "trace": [
                {
                    "type": "tool_use",
                    "tool_name": "read_skill_file",
                    "tool_input": {"path": "SKILL.md"},
                }
            ],
        }
        config = _byo_config(mock_endpoint, tmp_path)
        with _mock_snowflake_ok(), _mock_promptfoo_ok():
            results = check_byo_endpoint(config)
        # Should be an "ok" check noting derivation
        derivable = [r for r in results if "derivable from trace" in r.name]
        assert len(derivable) == 1
        assert derivable[0].severity == "ok"

    def test_snowflake_failure_surfaced(self, mock_endpoint, tmp_path: Path) -> None:
        _MockEndpointHandler.response_data = {"sql": "SELECT 1"}
        config = _byo_config(mock_endpoint, tmp_path)
        with _mock_snowflake_fail(error_text="auth failed"), _mock_promptfoo_ok():
            results = check_byo_endpoint(config)
        assert is_failing(results)
        assert any(
            r.name == "Snowflake reachability" and r.severity == "fail" for r in results
        )


# ──────────────────────────────────────────────────────────────────────────────
# Built-in mode tests
# ──────────────────────────────────────────────────────────────────────────────


def _mock_anthropic_ok():
    """Stub the Anthropic client so models.list() doesn't actually hit the API."""

    class _StubModels:
        def list(self, **_kwargs):
            return object()  # truthy, no exception

    class _StubClient:
        def __init__(self):
            self.models = _StubModels()

    return patch("anthropic.Anthropic", return_value=_StubClient())


class TestCheckBuiltinSetup:
    def test_all_pass(self, tmp_path: Path) -> None:
        config = _builtin_config(tmp_path)
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False),
            _mock_anthropic_ok(),
            _mock_snowflake_ok(),
            _mock_promptfoo_ok(),
        ):
            results = check_builtin_setup(config)
        assert not is_failing(results), format_report(results, mode="Built-in")

    def test_missing_api_key_fails(self, tmp_path: Path) -> None:
        config = _builtin_config(tmp_path)
        with (
            patch.dict("os.environ", {}, clear=True),
            _mock_snowflake_ok(),
            _mock_promptfoo_ok(),
        ):
            results = check_builtin_setup(config)
        fails = [r for r in results if r.severity == "fail"]
        assert any("Anthropic API key" in r.name for r in fails)

    def test_missing_system_prompt_file_fails(self, tmp_path: Path) -> None:
        config = _builtin_config(tmp_path, with_system_prompt=False)
        config.agent.system_prompt = "missing.md"  # claim a file that doesn't exist
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False),
            _mock_anthropic_ok(),
            _mock_snowflake_ok(),
            _mock_promptfoo_ok(),
        ):
            results = check_builtin_setup(config)
        fails = [r for r in results if r.severity == "fail"]
        assert any("System prompt file exists" in r.name for r in fails)

    def test_missing_tool_base_dir_fails(self, tmp_path: Path) -> None:
        config = _builtin_config(tmp_path, with_tools=False)
        config.agent.tools = [
            ToolConfig(
                name="read_skill_file",
                type="file_reader",
                config={"base_dir": "nonexistent-skills/"},
            )
        ]
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False),
            _mock_anthropic_ok(),
            _mock_snowflake_ok(),
            _mock_promptfoo_ok(),
        ):
            results = check_builtin_setup(config)
        fails = [r for r in results if r.severity == "fail"]
        assert any("base_dir exists" in r.name for r in fails)

    def test_promptfoo_missing_fails(self, tmp_path: Path) -> None:
        config = _builtin_config(tmp_path)
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False),
            _mock_anthropic_ok(),
            _mock_snowflake_ok(),
            _mock_promptfoo_missing(),
        ):
            results = check_builtin_setup(config)
        fails = [r for r in results if r.severity == "fail"]
        assert any("Promptfoo" in r.name for r in fails)

    def test_snowflake_connection_error_fails(self, tmp_path: Path) -> None:
        config = _builtin_config(tmp_path)
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False),
            _mock_anthropic_ok(),
            _mock_snowflake_fail(exc=RuntimeError("network down")),
            _mock_promptfoo_ok(),
        ):
            results = check_builtin_setup(config)
        fails = [r for r in results if r.severity == "fail"]
        assert any(r.name == "Snowflake reachability" for r in fails)


# ──────────────────────────────────────────────────────────────────────────────
# format_report + is_failing
# ──────────────────────────────────────────────────────────────────────────────


class TestFormatReport:
    def test_all_ok_shows_ready_message(self) -> None:
        out = format_report(
            [CheckResult("foo", "ok"), CheckResult("bar", "ok")], mode="BYO"
        )
        assert "Ready to run: bi-evals run" in out
        assert "Summary: 2 ok, 0 warn, 0 fail" in out

    def test_warnings_show_degraded_note(self) -> None:
        out = format_report([CheckResult("opt", "warn", "missing")], mode="BYO")
        assert "degraded scoring coverage" in out

    def test_fail_shows_required_message(self) -> None:
        out = format_report([CheckResult("hard", "fail", "broken")], mode="BYO")
        assert "Required checks failed" in out

    def test_is_failing_only_on_fail(self) -> None:
        assert is_failing([CheckResult("x", "fail")])
        assert not is_failing([CheckResult("x", "warn"), CheckResult("y", "ok")])
        assert not is_failing([])


# ──────────────────────────────────────────────────────────────────────────────
# CLI integration (`bi-evals doctor`)
# ──────────────────────────────────────────────────────────────────────────────


def _write_byo_config_file(tmp_path: Path, url: str) -> Path:
    config_file = tmp_path / "bi-evals.yaml"
    config_file.write_text(
        f"""project:
  name: "doctor test"
agent:
  type: "api_endpoint"
  endpoint:
    url: "{url}"
database:
  type: snowflake
golden_tests:
  dir: "golden/"
reporting:
  results_dir: "results/"
"""
    )
    return config_file


class TestCliDoctor:
    def test_byo_minimum_exits_zero(self, mock_endpoint, tmp_path: Path) -> None:
        # Minimum response has no optional fields, so we expect warnings but
        # no failures — exit 0, and the report should say required checks passed.
        _MockEndpointHandler.response_data = {"sql": "SELECT 1"}
        config_file = _write_byo_config_file(tmp_path, mock_endpoint)
        runner = CliRunner()
        with _mock_snowflake_ok(), _mock_promptfoo_ok():
            result = runner.invoke(cli, ["-c", str(config_file), "doctor"])
        assert result.exit_code == 0, result.output
        assert "BYO" in result.output
        assert "0 fail" in result.output
        assert "All required checks passed" in result.output

    def test_byo_broken_exits_one(self, mock_endpoint, tmp_path: Path) -> None:
        _MockEndpointHandler.response_data = {"text": "no SQL anywhere"}
        config_file = _write_byo_config_file(tmp_path, mock_endpoint)
        runner = CliRunner()
        with _mock_snowflake_ok(), _mock_promptfoo_ok():
            result = runner.invoke(cli, ["-c", str(config_file), "doctor"])
        assert result.exit_code == 1
        assert "fail" in result.output.lower()

    def test_unknown_agent_type_errors(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(
            """project:
  name: "x"
agent:
  type: "weird_thing"
database:
  type: snowflake
golden_tests:
  dir: "golden/"
reporting:
  results_dir: "results/"
"""
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(config_file), "doctor"])
        assert result.exit_code != 0
        assert "Unknown agent.type" in result.output
