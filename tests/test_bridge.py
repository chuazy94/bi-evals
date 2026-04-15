"""Tests for bi_evals.promptfoo — config generation, filtering, runner, CLI."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from bi_evals.config import (
    AgentConfig,
    BiEvalsConfig,
    DatabaseConfig,
    GoldenTestsConfig,
    ProjectConfig,
    ReportingConfig,
)
from bi_evals.golden.model import GoldenTest
from bi_evals.promptfoo.bridge import (
    filter_tests,
    generate_promptfoo_config,
    run_promptfoo,
    write_promptfoo_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(base_dir: Path, golden_dir: str = "golden") -> BiEvalsConfig:
    config = BiEvalsConfig(
        project=ProjectConfig(name="Test Project"),
        agent=AgentConfig(model="claude-sonnet-4-5-20250929"),
        database=DatabaseConfig(type="snowflake"),
        golden_tests=GoldenTestsConfig(dir=golden_dir),
        reporting=ReportingConfig(results_dir="results/"),
    )
    config._base_dir = base_dir
    return config


def _write_golden(path: Path, id: str, question: str, category: str = "", tags: list[str] | None = None) -> None:
    data = {"id": id, "question": question, "reference_sql": "SELECT 1"}
    if category:
        data["category"] = category
    if tags:
        data["tags"] = tags
    path.write_text(yaml.dump(data))


def _setup_golden_dir(tmp_path: Path) -> Path:
    golden = tmp_path / "golden"
    golden.mkdir()
    return golden


# ---------------------------------------------------------------------------
# Config Generation
# ---------------------------------------------------------------------------

class TestGeneratePromptfooConfig:
    def test_basic_structure(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        _write_golden(golden / "t1.yaml", "t-001", "What is revenue?")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml")

        assert result["prompts"] == ["{{question}}"]
        assert len(result["providers"]) == 1
        assert "file://" in result["providers"][0]["id"]
        assert len(result["tests"]) == 1

    def test_multiple_tests(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        for i in range(3):
            _write_golden(golden / f"t{i}.yaml", f"t-{i:03d}", f"Question {i}")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml")

        assert len(result["tests"]) == 3

    def test_config_path_in_provider(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        _write_golden(golden / "t1.yaml", "t-001", "Q")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "/path/to/bi-evals.yaml")

        assert result["providers"][0]["config"]["config_path"] == "/path/to/bi-evals.yaml"

    def test_golden_file_paths_relative(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        _write_golden(golden / "t1.yaml", "t-001", "Q")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml")

        golden_file = result["tests"][0]["vars"]["golden_file"]
        assert golden_file == "golden/t1.yaml"

    def test_provider_file_reference(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        _write_golden(golden / "t1.yaml", "t-001", "Q")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml")

        provider_id = result["providers"][0]["id"]
        assert provider_id.startswith("file://")
        assert provider_id.endswith("/src/bi_evals/provider/entry.py:call_api")
        assert "/src/bi_evals/provider/entry.py" in provider_id

    def test_scorer_file_reference(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        _write_golden(golden / "t1.yaml", "t-001", "Q")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml")

        assertion = result["tests"][0]["assert"][0]
        assert assertion["type"] == "python"
        assert assertion["value"].startswith("file://")
        assert assertion["value"].endswith("/src/bi_evals/scorer/entry.py:get_assert")

    def test_empty_golden_dir(self, tmp_path: Path) -> None:
        _setup_golden_dir(tmp_path)
        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml")

        assert result["tests"] == []

    def test_question_in_vars(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        _write_golden(golden / "t1.yaml", "t-001", "What is the total revenue?")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml")

        assert result["tests"][0]["vars"]["question"] == "What is the total revenue?"

    def test_description_includes_id(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        _write_golden(golden / "t1.yaml", "rev-001", "Show revenue")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml")

        assert "rev-001" in result["tests"][0]["description"]


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

class TestFilterTests:
    def _make_tests(self) -> list[tuple[GoldenTest, str]]:
        return [
            (GoldenTest(id="rev-001", question="Q", category="revenue", tags=["enterprise"]), "golden/rev-001.yaml"),
            (GoldenTest(id="ord-001", question="Q", category="orders", tags=["basic"]), "golden/ord-001.yaml"),
            (GoldenTest(id="rev-002", question="Q", category="revenue", tags=["smb"]), "golden/rev-002.yaml"),
        ]

    def test_filter_by_id(self) -> None:
        result = filter_tests(self._make_tests(), "rev-001")
        assert len(result) == 1
        assert result[0][0].id == "rev-001"

    def test_filter_by_category(self) -> None:
        result = filter_tests(self._make_tests(), "revenue")
        assert len(result) == 2

    def test_filter_by_tag(self) -> None:
        result = filter_tests(self._make_tests(), "enterprise")
        assert len(result) == 1
        assert result[0][0].id == "rev-001"

    def test_filter_case_insensitive(self) -> None:
        result = filter_tests(self._make_tests(), "REVENUE")
        assert len(result) == 2

    def test_filter_no_match(self) -> None:
        result = filter_tests(self._make_tests(), "nonexistent")
        assert result == []

    def test_filter_integrated_with_generate(self, tmp_path: Path) -> None:
        golden = _setup_golden_dir(tmp_path)
        _write_golden(golden / "rev.yaml", "rev-001", "Revenue Q", category="revenue")
        _write_golden(golden / "ord.yaml", "ord-001", "Orders Q", category="orders")

        config = _make_config(tmp_path)
        result = generate_promptfoo_config(config, "bi-evals.yaml", filter_pattern="revenue")

        assert len(result["tests"]) == 1
        assert "rev-001" in result["tests"][0]["description"]


# ---------------------------------------------------------------------------
# Write Config
# ---------------------------------------------------------------------------

class TestWriteConfig:
    def test_creates_yaml_file(self, tmp_path: Path) -> None:
        config_dict = {"prompts": ["{{question}}"], "tests": []}
        path = tmp_path / "config.yaml"

        write_promptfoo_config(config_dict, path)

        assert path.exists()
        loaded = yaml.safe_load(path.read_text())
        assert loaded["prompts"] == ["{{question}}"]

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deep" / "config.yaml"
        write_promptfoo_config({"tests": []}, path)
        assert path.exists()


# ---------------------------------------------------------------------------
# Promptfoo Runner
# ---------------------------------------------------------------------------

class TestRunPromptfoo:
    @patch("bi_evals.promptfoo.bridge.subprocess.Popen")
    @patch("bi_evals.promptfoo.bridge.shutil.which", return_value="/usr/bin/npx")
    def test_success(self, mock_which: MagicMock, mock_popen: MagicMock) -> None:
        mock_process = MagicMock()
        mock_process.wait.return_value = 0
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        code = run_promptfoo(Path("config.yaml"), Path("results.json"))

        assert code == 0
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "npx" in cmd
        assert "promptfoo" in cmd
        assert "config.yaml" in cmd
        assert "results.json" in cmd

    @patch("bi_evals.promptfoo.bridge.subprocess.Popen")
    @patch("bi_evals.promptfoo.bridge.shutil.which", return_value="/usr/bin/npx")
    def test_failure(self, mock_which: MagicMock, mock_popen: MagicMock) -> None:
        mock_process = MagicMock()
        mock_process.wait.return_value = 1
        mock_process.returncode = 1
        mock_popen.return_value = mock_process

        code = run_promptfoo(Path("config.yaml"), Path("results.json"))

        assert code == 1

    @patch("bi_evals.promptfoo.bridge.subprocess.Popen")
    @patch("bi_evals.promptfoo.bridge.shutil.which", return_value="/usr/bin/npx")
    def test_verbose_flag(self, mock_which: MagicMock, mock_popen: MagicMock) -> None:
        mock_process = MagicMock()
        mock_process.wait.return_value = 0
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        run_promptfoo(Path("config.yaml"), Path("results.json"), verbose=True)

        cmd = mock_popen.call_args[0][0]
        assert "--verbose" in cmd

    @patch("bi_evals.promptfoo.bridge.subprocess.Popen")
    @patch("bi_evals.promptfoo.bridge.shutil.which", return_value="/usr/bin/npx")
    def test_no_verbose_by_default(self, mock_which: MagicMock, mock_popen: MagicMock) -> None:
        mock_process = MagicMock()
        mock_process.wait.return_value = 0
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        run_promptfoo(Path("config.yaml"), Path("results.json"))

        cmd = mock_popen.call_args[0][0]
        assert "--verbose" not in cmd

    @patch("bi_evals.promptfoo.bridge.shutil.which", return_value=None)
    def test_not_installed(self, mock_which: MagicMock) -> None:
        import click

        with pytest.raises(click.ClickException, match="Promptfoo not found"):
            run_promptfoo(Path("config.yaml"), Path("results.json"))


# ---------------------------------------------------------------------------
# CLI Integration
# ---------------------------------------------------------------------------

class TestCLIRun:
    def _write_config_and_golden(self, tmp_path: Path) -> Path:
        config_content = dedent("""\
            project:
              name: "CLI Test"
            agent:
              model: "claude-sonnet-4-5-20250929"
              system_prompt: "p.md"
            database:
              type: snowflake
            golden_tests:
              dir: "golden/"
            reporting:
              results_dir: "results/"
        """)
        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(config_content)

        golden = tmp_path / "golden"
        golden.mkdir()
        _write_golden(golden / "t1.yaml", "t-001", "What is revenue?", category="revenue")

        return config_file

    def test_dry_run(self, tmp_path: Path) -> None:
        config_file = self._write_config_and_golden(tmp_path)

        from bi_evals.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(config_file), "run", "--dry-run"])

        assert result.exit_code == 0
        assert "prompts:" in result.output or "{{question}}" in result.output
        assert "t-001" in result.output

    def test_no_golden_tests(self, tmp_path: Path) -> None:
        config_content = dedent("""\
            project:
              name: "Empty"
            agent:
              model: "test"
              system_prompt: "p.md"
            database:
              type: snowflake
            golden_tests:
              dir: "golden/"
        """)
        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(config_content)
        (tmp_path / "golden").mkdir()

        from bi_evals.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(config_file), "run"])

        assert result.exit_code != 0
        assert "No golden tests found" in result.output

    def test_filter_no_match(self, tmp_path: Path) -> None:
        config_file = self._write_config_and_golden(tmp_path)

        from bi_evals.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(config_file), "run", "-f", "nonexistent"])

        assert result.exit_code != 0
        assert "No tests match filter" in result.output

    @patch("bi_evals.promptfoo.bridge.subprocess.Popen")
    @patch("bi_evals.promptfoo.bridge.shutil.which", return_value="/usr/bin/npx")
    def test_success_flow(self, mock_which: MagicMock, mock_popen: MagicMock, tmp_path: Path) -> None:
        mock_process = MagicMock()
        mock_process.wait.return_value = 0
        mock_process.returncode = 0
        mock_popen.return_value = mock_process
        config_file = self._write_config_and_golden(tmp_path)

        from bi_evals.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(config_file), "run"])

        assert result.exit_code == 0
        assert "CLI Test" in result.output
        assert "Tests:" in result.output
        mock_popen.assert_called_once()

    @patch("bi_evals.promptfoo.bridge.shutil.which", return_value=None)
    def test_promptfoo_missing(self, mock_which: MagicMock, tmp_path: Path) -> None:
        config_file = self._write_config_and_golden(tmp_path)

        from bi_evals.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["-c", str(config_file), "run"])

        assert result.exit_code != 0
        assert "Promptfoo not found" in result.output


# ---------------------------------------------------------------------------
# Golden loader with paths
# ---------------------------------------------------------------------------

class TestLoadGoldenTestsWithPaths:
    def test_returns_relative_paths(self, tmp_path: Path) -> None:
        golden = tmp_path / "golden"
        golden.mkdir()
        _write_golden(golden / "t1.yaml", "t-001", "Q1")
        _write_golden(golden / "t2.yaml", "t-002", "Q2")

        config = _make_config(tmp_path)
        from bi_evals.golden.loader import load_golden_tests_with_paths
        results = load_golden_tests_with_paths(config)

        assert len(results) == 2
        test, path = results[0]
        assert test.id == "t-001"
        assert path == "golden/t1.yaml"

    def test_nested_subdirectories(self, tmp_path: Path) -> None:
        golden = tmp_path / "golden" / "revenue"
        golden.mkdir(parents=True)
        _write_golden(golden / "rev.yaml", "rev-001", "Revenue Q")

        config = _make_config(tmp_path)
        from bi_evals.golden.loader import load_golden_tests_with_paths
        results = load_golden_tests_with_paths(config)

        assert len(results) == 1
        assert results[0][1] == "golden/revenue/rev.yaml"

    def test_empty_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "golden").mkdir()
        config = _make_config(tmp_path)
        from bi_evals.golden.loader import load_golden_tests_with_paths
        assert load_golden_tests_with_paths(config) == []
