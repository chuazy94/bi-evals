"""Tests for `bi-evals init` subcommand split — api_endpoint (default) vs dev.

Covers:
- bare `init` errors with the scaffold-required message
- `init dev` writes the right files, no adapter shim, valid config
- `init api_endpoint` writes the right files including adapter_example.py, valid config
- generated YAML loads cleanly under BiEvalsConfig (adapter-nested schema) in both
- adapter_example.py is syntactically valid Python
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import pytest
from click.testing import CliRunner

from bi_evals.cli import cli
from bi_evals.config import BiEvalsConfig


class TestInitBareErrors:
    def test_bare_init_exits_nonzero(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init"])
        assert result.exit_code != 0
        assert "must specify a scaffold" in result.output
        assert "api_endpoint" in result.output and "dev" in result.output


class TestInitDev:
    def _scaffold(self, tmp_path: Path) -> Path:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "dev", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        return tmp_path

    def test_writes_expected_files(self, tmp_path: Path) -> None:
        target = self._scaffold(tmp_path)
        assert (target / "bi-evals.yaml").exists()
        assert (target / ".env").exists()
        assert (target / ".env.example").exists()
        assert (target / "golden" / "example-query.yaml").exists()
        assert (target / "results").is_dir()
        assert (target / "reports").is_dir()

    def test_does_not_write_api_endpoint_adapter(self, tmp_path: Path) -> None:
        target = self._scaffold(tmp_path)
        assert not (target / "adapter_example.py").exists()

    def test_env_has_anthropic_key(self, tmp_path: Path) -> None:
        target = self._scaffold(tmp_path)
        env_text = (target / ".env").read_text()
        assert "ANTHROPIC_API_KEY" in env_text
        assert "BI_AGENT_URL" not in env_text

    def test_generated_config_loads(self, tmp_path: Path) -> None:
        target = self._scaffold(tmp_path)
        config = BiEvalsConfig.load(target / "bi-evals.yaml")
        assert config.agent.adapter == "anthropic_tool_loop"
        assert config.agent.model.startswith("claude-")
        assert len(config.agent.tools) >= 1

    def test_next_steps_mention_skills(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "dev", "--dir", str(tmp_path)])
        assert "system-prompt.md" in result.output
        assert "skill" in result.output.lower()


class TestInitApiEndpoint:
    def _scaffold(self, tmp_path: Path) -> Path:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "api_endpoint", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        return tmp_path

    def test_writes_expected_files(self, tmp_path: Path) -> None:
        target = self._scaffold(tmp_path)
        assert (target / "bi-evals.yaml").exists()
        assert (target / ".env").exists()
        assert (target / ".env.example").exists()
        assert (target / "golden" / "example-query.yaml").exists()
        assert (target / "adapter_example.py").exists()
        assert (target / "results").is_dir()
        assert (target / "reports").is_dir()

    def test_env_has_bi_agent_vars(self, tmp_path: Path) -> None:
        target = self._scaffold(tmp_path)
        env_text = (target / ".env").read_text()
        assert "BI_AGENT_URL" in env_text
        assert "BI_AGENT_TOKEN" in env_text
        assert "ANTHROPIC_API_KEY" not in env_text

    def test_generated_config_loads(self, tmp_path: Path) -> None:
        target = self._scaffold(tmp_path)
        config = BiEvalsConfig.load(target / "bi-evals.yaml")
        assert config.agent.adapter == "api_endpoint"
        assert config.agent.endpoint is not None
        assert config.agent.endpoint.url  # substituted from .env

    def test_adapter_is_valid_python(self, tmp_path: Path) -> None:
        target = self._scaffold(tmp_path)
        # py_compile raises if the file has a SyntaxError
        py_compile.compile(str(target / "adapter_example.py"), doraise=True)

    def test_next_steps_mention_endpoint_and_adapter(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "api_endpoint", "--dir", str(tmp_path)])
        assert "endpoint" in result.output.lower()
        assert "adapter_example.py" in result.output


class TestInitIdempotent:
    """Re-running init should not clobber existing files."""

    @pytest.mark.parametrize("scaffold", ["dev", "api_endpoint"])
    def test_existing_config_preserved(self, tmp_path: Path, scaffold: str) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", scaffold, "--dir", str(tmp_path)])
        config_path = tmp_path / "bi-evals.yaml"
        marker = "# USER EDIT MARKER — DO NOT OVERWRITE\n"
        config_path.write_text(marker + config_path.read_text())
        # Re-run init — existing file should be left alone
        runner.invoke(cli, ["init", scaffold, "--dir", str(tmp_path)])
        assert config_path.read_text().startswith(marker)
