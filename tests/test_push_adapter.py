"""Tests for the push adapter and `bi-evals score` (Pivot Phase 3)."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bi_evals.cli import cli
from bi_evals.config import (
    AgentConfig,
    BiEvalsConfig,
    DatabaseConfig,
    ProjectConfig,
)
from bi_evals.provider.contract import AgentResult
from bi_evals.provider.registry import (
    PushReplayAdapter,
    _load_submissions,
    resolve_sql,
    _trace_from_row,
    build_adapter,
)


def _push_config(tmp_path: Path, input_file: str) -> BiEvalsConfig:
    config = BiEvalsConfig(
        project=ProjectConfig(name="t"),
        agent=AgentConfig(adapter="push", push={"input_file": input_file}),
        database=DatabaseConfig(type="snowflake"),
    )
    config._base_dir = tmp_path
    return config


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


class TestBuildAdapter:
    def test_push_resolves(self, tmp_path: Path) -> None:
        adapter = build_adapter(_push_config(tmp_path, "r.jsonl"))
        assert isinstance(adapter, PushReplayAdapter)


class TestLoadSubmissions:
    def test_keys_by_golden_file(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_jsonl(
            p,
            [
                {"golden_file": "golden/a.yaml", "generated_sql": "SELECT 1"},
                {"golden_file": "golden/b.yaml", "generated_sql": "SELECT 2"},
            ],
        )
        _load_submissions.cache_clear()
        subs = _load_submissions(str(p))
        assert set(subs) == {"golden/a.yaml", "golden/b.yaml"}

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        p.write_text(
            '{"golden_file": "golden/a.yaml", "generated_sql": "SELECT 1"}\n\n'
        )
        _load_submissions.cache_clear()
        assert set(_load_submissions(str(p))) == {"golden/a.yaml"}

    def test_malformed_json_raises_with_lineno(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        p.write_text("not json\n")
        _load_submissions.cache_clear()
        with pytest.raises(ValueError, match=r":1: invalid JSON"):
            _load_submissions(str(p))

    def test_missing_golden_file_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        p.write_text('{"generated_sql": "SELECT 1"}\n')
        _load_submissions.cache_clear()
        with pytest.raises(ValueError, match="missing required 'golden_file'"):
            _load_submissions(str(p))

    def test_duplicate_golden_file_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_jsonl(
            p,
            [
                {"golden_file": "golden/a.yaml", "generated_sql": "SELECT 1"},
                {"golden_file": "golden/a.yaml", "generated_sql": "SELECT 2"},
            ],
        )
        _load_submissions.cache_clear()
        with pytest.raises(ValueError, match="duplicate golden_file 'golden/a.yaml'"):
            _load_submissions(str(p))


class TestTraceFromRow:
    def test_dict_envelope_with_tool_calls(self) -> None:
        row = {
            "trace": {
                "tool_calls": [
                    {
                        "type": "tool_use",
                        "tool_name": "read_skill_file",
                        "tool_input": {"path": "SKILL.md"},
                    }
                ]
            }
        }
        steps, files = _trace_from_row(row)
        assert len(steps) == 1
        assert steps[0].tool_name == "read_skill_file"
        assert files == ["SKILL.md"]

    def test_list_envelope(self) -> None:
        row = {"trace": [{"type": "tool_use", "tool_name": "x", "tool_input": {}}]}
        steps, files = _trace_from_row(row)
        assert len(steps) == 1 and steps[0].tool_name == "x"

    def test_explicit_files_read_wins(self) -> None:
        row = {"files_read": ["A.md", "B.md"], "trace": []}
        _, files = _trace_from_row(row)
        assert files == ["A.md", "B.md"]

    def test_captures_all_tool_paths_not_just_first(self) -> None:
        # Regression: previously only the first step's path was kept.
        row = {
            "trace": {
                "tool_calls": [
                    {"tool_name": "read", "tool_input": {"path": "SKILL.md"}},
                    {"tool_name": "read", "tool_input": {"path": "KNOWLEDGE.md"}},
                ]
            }
        }
        steps, files = _trace_from_row(row)
        assert len(steps) == 2
        assert files == ["SKILL.md", "KNOWLEDGE.md"]

    def test_no_trace_is_empty(self) -> None:
        steps, files = _trace_from_row({"generated_sql": "SELECT 1"})
        assert steps == [] and files == []


class TestResolveSql:
    def test_generated_sql_wins_over_response_text(self) -> None:
        sql, _, err = resolve_sql(
            {"generated_sql": "SELECT 1", "response_text": "```sql\nSELECT 2\n```"},
            "g",
        )
        assert err is None
        assert sql == "SELECT 1"

    def test_generated_sql_fenced_is_unwrapped(self) -> None:
        sql, _, err = resolve_sql({"generated_sql": "```sql\nSELECT 5\n```"}, "g")
        assert err is None and sql == "SELECT 5"

    def test_extracts_from_response_text(self) -> None:
        sql, final, err = resolve_sql(
            {"response_text": "Here:\n```sql\nSELECT 3\n```\nDone."}, "g"
        )
        assert err is None
        assert sql == "SELECT 3"
        assert final == "Here:\n```sql\nSELECT 3\n```\nDone."  # raw answer kept

    def test_final_text_prefers_response_text(self) -> None:
        _, final, _ = resolve_sql(
            {"generated_sql": "SELECT 1", "response_text": "the answer"}, "g"
        )
        assert final == "the answer"

    def test_response_text_without_sql_errors(self) -> None:
        sql, _, err = resolve_sql({"response_text": "I couldn't answer that."}, "g")
        assert sql == ""
        assert err is not None and "no SQL could be extracted" in err

    def test_neither_field_errors(self) -> None:
        _, _, err = resolve_sql({"trace": {}}, "g")
        assert err is not None and "missing both" in err


class TestPushReplayAdapter:
    def test_replays_matching_row(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_jsonl(
            p,
            [
                {
                    "golden_file": "golden/a.yaml",
                    "generated_sql": "SELECT SUM(rev) FROM t",
                    "trace": {"files_read": ["SKILL.md"]},
                }
            ],
        )
        _load_submissions.cache_clear()
        config = _push_config(tmp_path, "r.jsonl")
        result = PushReplayAdapter().produce(
            "Q", {"golden_file": "golden/a.yaml"}, config, None
        )
        assert isinstance(result, AgentResult)
        assert result.extracted_sql == "SELECT SUM(rev) FROM t"
        assert result.files_read == ["SKILL.md"]

    def test_replays_response_text_row(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_jsonl(
            p,
            [
                {
                    "golden_file": "golden/a.yaml",
                    "response_text": "Sure:\n```sql\nSELECT 1\n```\nThat's it.",
                }
            ],
        )
        _load_submissions.cache_clear()
        config = _push_config(tmp_path, "r.jsonl")
        result = PushReplayAdapter().produce(
            "Q", {"golden_file": "golden/a.yaml"}, config, None
        )
        assert isinstance(result, AgentResult)
        assert result.extracted_sql == "SELECT 1"
        assert "That's it." in result.final_text  # raw answer preserved

    def test_response_text_without_sql_errors(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_jsonl(
            p,
            [{"golden_file": "golden/a.yaml", "response_text": "no query here"}],
        )
        _load_submissions.cache_clear()
        config = _push_config(tmp_path, "r.jsonl")
        result = PushReplayAdapter().produce(
            "Q", {"golden_file": "golden/a.yaml"}, config, None
        )
        assert isinstance(result, str)
        assert "no SQL could be extracted" in result

    def test_error_row_produces_agent_error(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_jsonl(
            p,
            [{"golden_file": "golden/a.yaml", "error": "agent timed out after 60s"}],
        )
        _load_submissions.cache_clear()
        config = _push_config(tmp_path, "r.jsonl")
        result = PushReplayAdapter().produce(
            "Q", {"golden_file": "golden/a.yaml"}, config, None
        )
        assert isinstance(result, AgentResult)
        assert result.agent_error == "agent timed out after 60s"
        assert result.extracted_sql is None
        # an error row is valid in validation (not "missing both")
        from bi_evals.provider.registry import resolve_sql

        assert resolve_sql({"error": "x"}, "g")[2] is None

    def test_missing_row_returns_error(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_jsonl(p, [{"golden_file": "golden/a.yaml", "generated_sql": "X"}])
        _load_submissions.cache_clear()
        config = _push_config(tmp_path, "r.jsonl")
        result = PushReplayAdapter().produce(
            "Q", {"golden_file": "golden/missing.yaml"}, config, None
        )
        assert isinstance(result, str)
        assert "No push submission found" in result

    def test_missing_both_sql_fields_returns_error(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_jsonl(p, [{"golden_file": "golden/a.yaml", "generated_sql": ""}])
        _load_submissions.cache_clear()
        config = _push_config(tmp_path, "r.jsonl")
        result = PushReplayAdapter().produce(
            "Q", {"golden_file": "golden/a.yaml"}, config, None
        )
        assert isinstance(result, str)
        assert "missing both" in result

    def test_unset_input_file_returns_error(self, tmp_path: Path) -> None:
        config = _push_config(tmp_path, "")
        result = PushReplayAdapter().produce(
            "Q", {"golden_file": "g.yaml"}, config, None
        )
        assert isinstance(result, str)
        assert "input_file is not set" in result


def _write_project(tmp_path: Path) -> Path:
    (tmp_path / "golden" / "cases").mkdir(parents=True)
    (tmp_path / "bi-evals.yaml").write_text(
        dedent("""\
        project:
          name: "Push CLI"
        agent:
          adapter: api_endpoint
          api_endpoint:
            url: "http://unused"
        database:
          type: snowflake
        golden_tests:
          dir: "golden/cases/"
        reporting:
          results_dir: "results/"
        """)
    )
    (tmp_path / "golden" / "cases" / "q1.yaml").write_text(
        "id: q1\nquestion: 'total revenue?'\nreference_sql: 'SELECT 1'\n"
    )
    return tmp_path / "bi-evals.yaml"


class TestScoreCommand:
    def test_dry_run_emits_push_provider(self, tmp_path: Path) -> None:
        config_file = _write_project(tmp_path)
        results = tmp_path / "r.jsonl"
        _write_jsonl(
            results,
            [{"golden_file": "golden/cases/q1.yaml", "generated_sql": "SELECT 1"}],
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(config_file), "score", "--input", str(results), "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "adapter: push" in result.output
        assert "push_input_file" in result.output

    def test_missing_submission_fails(self, tmp_path: Path) -> None:
        config_file = _write_project(tmp_path)
        results = tmp_path / "r.jsonl"
        _write_jsonl(
            results,
            [{"golden_file": "golden/cases/other.yaml", "generated_sql": "SELECT 1"}],
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(config_file), "score", "--input", str(results), "--dry-run"],
        )
        assert result.exit_code != 0
        assert "No submission for these goldens" in result.output
        assert "golden/cases/q1.yaml" in result.output

    def test_malformed_jsonl_fails(self, tmp_path: Path) -> None:
        config_file = _write_project(tmp_path)
        results = tmp_path / "r.jsonl"
        results.write_text("not json\n")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(config_file), "score", "--input", str(results), "--dry-run"],
        )
        assert result.exit_code != 0
        assert "invalid JSON" in result.output

    def test_missing_input_file_fails(self, tmp_path: Path) -> None:
        config_file = _write_project(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(config_file), "score", "--input", str(tmp_path / "nope.jsonl")],
        )
        assert result.exit_code != 0  # click validates --input exists

    def test_duplicate_golden_fails(self, tmp_path: Path) -> None:
        config_file = _write_project(tmp_path)
        results = tmp_path / "r.jsonl"
        _write_jsonl(
            results,
            [
                {"golden_file": "golden/cases/q1.yaml", "generated_sql": "SELECT 1"},
                {"golden_file": "golden/cases/q1.yaml", "generated_sql": "SELECT 2"},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(config_file), "score", "--input", str(results), "--dry-run"],
        )
        assert result.exit_code != 0
        assert "duplicate golden_file" in result.output

    def test_response_text_row_accepted(self, tmp_path: Path) -> None:
        config_file = _write_project(tmp_path)
        results = tmp_path / "r.jsonl"
        _write_jsonl(
            results,
            [
                {
                    "golden_file": "golden/cases/q1.yaml",
                    "response_text": "Answer:\n```sql\nSELECT 1\n```",
                }
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(config_file), "score", "--input", str(results), "--dry-run"],
        )
        assert result.exit_code == 0, result.output

    def test_response_text_without_sql_fails(self, tmp_path: Path) -> None:
        config_file = _write_project(tmp_path)
        results = tmp_path / "r.jsonl"
        _write_jsonl(
            results,
            [{"golden_file": "golden/cases/q1.yaml", "response_text": "no sql here"}],
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["-c", str(config_file), "score", "--input", str(results), "--dry-run"],
        )
        assert result.exit_code != 0
        assert "no SQL could be extracted" in result.output


class TestDoctorPush:
    def test_doctor_push_does_not_crash(self, tmp_path: Path) -> None:
        from bi_evals.doctor import check_push_setup

        results = tmp_path / "r.jsonl"
        _write_jsonl(
            results,
            [{"golden_file": "golden/a.yaml", "generated_sql": "SELECT 1"}],
        )
        config = _push_config(tmp_path, "r.jsonl")
        with (
            patch("bi_evals.doctor.create_db_client") as mock_db,
            patch("bi_evals.doctor.shutil.which", return_value="/usr/bin/npx"),
        ):
            mock_db.return_value.execute.return_value = type(
                "R", (), {"error": None, "row_count": 1}
            )()
            checks = check_push_setup(config)
        names = {c.name for c in checks}
        assert "push adapter" in names
        assert "Submission file" in names
        # the valid submission file should be an "ok" check
        sub = next(c for c in checks if c.name == "Submission file")
        assert sub.severity == "ok"
