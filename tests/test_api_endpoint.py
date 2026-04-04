"""Tests for the API endpoint provider."""

from __future__ import annotations

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Any

import pytest

from bi_evals.config import ApiEndpointConfig
from bi_evals.provider.api_endpoint import _get_nested, call_api_endpoint


class TestGetNested:
    def test_simple_key(self) -> None:
        assert _get_nested({"sql": "SELECT 1"}, "sql") == "SELECT 1"

    def test_nested_key(self) -> None:
        data = {"response": {"data": {"sql": "SELECT 1"}}}
        assert _get_nested(data, "response.data.sql") == "SELECT 1"

    def test_missing_key(self) -> None:
        assert _get_nested({"a": 1}, "b") is None

    def test_missing_nested_key(self) -> None:
        assert _get_nested({"a": {"b": 1}}, "a.c") is None

    def test_non_dict_intermediate(self) -> None:
        assert _get_nested({"a": "string"}, "a.b") is None


class _MockAgentHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler that returns a BI agent-like response."""

    response_data: dict[str, Any] = {}

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.__class__.response_data).encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress logs


@pytest.fixture()
def mock_server():
    """Start a local HTTP server that returns mock agent responses."""
    server = HTTPServer(("127.0.0.1", 0), _MockAgentHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{port}"
    server.shutdown()


class TestCallApiEndpoint:
    def test_basic_response(self, mock_server) -> None:
        server, url = mock_server
        _MockAgentHandler.response_data = {
            "text": "The total revenue is $1.2M",
            "sql": "SELECT SUM(revenue) FROM sales",
        }

        config = ApiEndpointConfig(url=url)
        result = call_api_endpoint("What is total revenue?", config)

        assert result.extracted_sql == "SELECT SUM(revenue) FROM sales"
        assert "1.2M" in result.final_text
        assert result.latency_ms >= 0

    def test_sql_extracted_from_text_when_no_sql_key(self, mock_server) -> None:
        server, url = mock_server
        _MockAgentHandler.response_data = {
            "text": "Here's the query:\n```sql\nSELECT * FROM users\n```",
        }

        config = ApiEndpointConfig(url=url, response_sql_key="sql")
        result = call_api_endpoint("Show me users", config)

        # sql key returns None, so it falls back to extracting from text
        assert result.extracted_sql == "SELECT * FROM users"

    def test_custom_response_keys(self, mock_server) -> None:
        server, url = mock_server
        _MockAgentHandler.response_data = {
            "result": {
                "answer": "42 accounts",
                "query": "SELECT COUNT(*) FROM accounts",
            }
        }

        config = ApiEndpointConfig(
            url=url,
            response_text_key="result.answer",
            response_sql_key="result.query",
        )
        result = call_api_endpoint("How many accounts?", config)

        assert result.extracted_sql == "SELECT COUNT(*) FROM accounts"
        assert "42 accounts" in result.final_text

    def test_api_returns_trace(self, mock_server) -> None:
        server, url = mock_server
        _MockAgentHandler.response_data = {
            "text": "Revenue is growing",
            "sql": "SELECT * FROM revenue",
            "files_read": ["SKILL.md", "REVENUE.md"],
            "trace": [
                {"type": "tool_use", "tool_name": "read_skill_file",
                 "tool_input": {"path": "SKILL.md"}},
            ],
        }

        config = ApiEndpointConfig(url=url)
        result = call_api_endpoint("Revenue trend", config)

        assert result.files_read == ["SKILL.md", "REVENUE.md"]
        # Trace includes the 2 default steps + 1 from API
        tool_steps = [s for s in result.trace if s.tool_name == "read_skill_file"]
        assert len(tool_steps) == 1

    def test_connection_error(self) -> None:
        config = ApiEndpointConfig(url="http://127.0.0.1:1", timeout=1)
        result = call_api_endpoint("test", config)

        assert "Connection error" in result.final_text
        assert result.extracted_sql is None

    def test_no_tokens_or_cost(self, mock_server) -> None:
        server, url = mock_server
        _MockAgentHandler.response_data = {"text": "ok", "sql": "SELECT 1"}

        config = ApiEndpointConfig(url=url)
        result = call_api_endpoint("test", config)

        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0
        assert result.cost == 0.0
