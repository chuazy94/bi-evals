"""Tests for bi_evals.config."""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from bi_evals.config import BiEvalsConfig, _resolve_env_vars


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Create a minimal config file in a temp directory."""
    config_content = dedent("""\
        project:
          name: "Test Project"

        agent:
          model: "claude-sonnet-4-5-20250929"
          system_prompt: "prompts/system.md"
          tools:
            - name: read_skill_file
              type: file_reader
              config:
                base_dir: "skill/"
          max_rounds: 5

        database:
          type: snowflake
          connection:
            account: "test-account"
            user: "test-user"
            password: "test-pass"
            warehouse: "test-wh"
            database: "test-db"
            schema: "test-schema"
          query_timeout: 15

        golden_tests:
          dir: "golden/"

        scoring:
          dimensions:
            - execution
            - table_alignment
          thresholds:
            completeness: 0.90
            precision: 0.90
            value_tolerance: 0.001

        reporting:
          output_dir: "reports/"
          results_dir: "results/"
    """)
    config_file = tmp_path / "bi-evals.yaml"
    config_file.write_text(config_content)
    return tmp_path


class TestResolveEnvVars:
    def test_resolves_known_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert _resolve_env_vars("prefix_${MY_VAR}_suffix") == "prefix_hello_suffix"

    def test_missing_var_resolves_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        assert _resolve_env_vars("${NONEXISTENT_VAR}") == ""

    def test_multiple_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _resolve_env_vars("${A}-${B}") == "1-2"

    def test_no_vars_unchanged(self) -> None:
        assert _resolve_env_vars("no vars here") == "no vars here"


class TestBiEvalsConfig:
    def test_load_basic(self, config_dir: Path) -> None:
        config = BiEvalsConfig.load(config_dir / "bi-evals.yaml")
        assert config.project.name == "Test Project"
        assert config.agent.model == "claude-sonnet-4-5-20250929"
        assert config.agent.max_rounds == 5
        assert config.database.type == "snowflake"
        assert config.database.connection.account == "test-account"
        assert config.database.connection.schema_ == "test-schema"
        assert config.database.query_timeout == 15

    def test_scoring_config(self, config_dir: Path) -> None:
        config = BiEvalsConfig.load(config_dir / "bi-evals.yaml")
        assert config.scoring.dimensions == ["execution", "table_alignment"]
        assert config.scoring.thresholds.completeness == 0.90
        assert config.scoring.thresholds.value_tolerance == 0.001

    def test_tools_config(self, config_dir: Path) -> None:
        config = BiEvalsConfig.load(config_dir / "bi-evals.yaml")
        assert len(config.agent.tools) == 1
        assert config.agent.tools[0].name == "read_skill_file"
        assert config.agent.tools[0].type == "file_reader"
        assert config.agent.tools[0].config == {"base_dir": "skill/"}

    def test_resolve_path(self, config_dir: Path) -> None:
        config = BiEvalsConfig.load(config_dir / "bi-evals.yaml")
        resolved = config.resolve_path("prompts/system.md")
        assert resolved == (config_dir / "prompts" / "system.md").resolve()

    def test_env_var_resolution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ACCOUNT", "my-snowflake-account")
        config_content = dedent("""\
            project:
              name: "Env Test"
            agent:
              model: "claude-sonnet-4-5-20250929"
              system_prompt: "prompts/system.md"
            database:
              type: snowflake
              connection:
                account: "${TEST_ACCOUNT}"
        """)
        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(config_content)

        config = BiEvalsConfig.load(config_file)
        assert config.database.connection.account == "my-snowflake-account"

    def test_defaults(self, tmp_path: Path) -> None:
        """Minimal config should work with defaults."""
        config_content = dedent("""\
            project:
              name: "Minimal"
            agent:
              model: "claude-sonnet-4-5-20250929"
              system_prompt: "prompts/system.md"
            database:
              type: snowflake
        """)
        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(config_content)

        config = BiEvalsConfig.load(config_file)
        assert config.agent.max_rounds == 10
        assert config.agent.api_key_env == "ANTHROPIC_API_KEY"
        assert config.scoring.thresholds.completeness == 0.95
        assert config.golden_tests.dir == "golden/"
        assert len(config.scoring.dimensions) == 9

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            BiEvalsConfig.load(tmp_path / "nonexistent.yaml")
