"""Tests for bi_evals.golden — GoldenTest model and loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from bi_evals.golden.loader import load_golden_test, load_golden_tests
from bi_evals.golden.model import GoldenTest


class TestGoldenTest:
    def test_minimal(self) -> None:
        gt = GoldenTest(id="t-001", question="What is total revenue?")
        assert gt.id == "t-001"
        assert gt.category == ""
        assert gt.difficulty == ""
        assert gt.reference_sql == ""
        assert gt.expected.min_rows == 0
        assert gt.expected.required_columns == []
        assert gt.expected.checks == []
        assert gt.expected.row_comparison.enabled is False
        assert gt.expected_skill_path.required_skills == []
        assert gt.tags == []

    def test_full(self) -> None:
        data = {
            "id": "rev-001",
            "category": "revenue",
            "difficulty": "medium",
            "question": "Show revenue for account 123",
            "expected_skill_path": {
                "required_skills": [
                    {"tool": "read_skill_file", "input_contains": "SKILL.md"},
                    {"tool": "read_skill_file", "input_contains": "REVENUE.md"},
                ],
                "sequence_matters": True,
                "allow_extra_skills": True,
            },
            "reference_sql": "SELECT * FROM revenue WHERE id = 123",
            "expected": {
                "min_rows": 1,
                "required_columns": ["ID", "AMOUNT"],
                "checks": [
                    {"column": "AMOUNT", "condition": "type", "value": "positive_number"},
                    {"column": "ID", "condition": "equals", "value": 123},
                ],
                "row_comparison": {
                    "enabled": True,
                    "completeness_threshold": 0.90,
                    "precision_threshold": 0.90,
                    "value_tolerance": 0.01,
                    "key_columns": ["ID"],
                    "value_columns": ["AMOUNT"],
                    "ignore_order": True,
                },
            },
            "tags": ["revenue", "enterprise"],
            "notes": "Test note",
        }
        gt = GoldenTest(**data)
        assert gt.category == "revenue"
        assert len(gt.expected_skill_path.required_skills) == 2
        assert gt.expected_skill_path.required_skills[0].tool == "read_skill_file"
        assert gt.expected.row_comparison.enabled is True
        assert gt.expected.row_comparison.completeness_threshold == 0.90
        assert len(gt.expected.checks) == 2
        assert gt.expected.checks[0].condition == "type"

    def test_no_skill_path(self) -> None:
        gt = GoldenTest(id="t-002", question="Q")
        assert gt.expected_skill_path.required_skills == []


class TestLoader:
    def test_load_single_file(self, tmp_path: Path) -> None:
        yaml_content = dedent("""\
            id: test-001
            question: "What is the total?"
            reference_sql: "SELECT SUM(val) FROM t"
            expected:
              min_rows: 1
              required_columns:
                - TOTAL
        """)
        f = tmp_path / "test.yaml"
        f.write_text(yaml_content)

        gt = load_golden_test(f)
        assert gt.id == "test-001"
        assert gt.expected.required_columns == ["TOTAL"]

    def test_load_directory(self, tmp_path: Path) -> None:
        for i in range(3):
            (tmp_path / f"test-{i:03d}.yaml").write_text(
                f'id: "t-{i:03d}"\nquestion: "Q{i}"'
            )

        from bi_evals.config import BiEvalsConfig

        config = self._make_config(tmp_path, ".")
        tests = load_golden_tests(config)
        assert len(tests) == 3
        assert tests[0].id == "t-000"

    def test_load_yml_extension(self, tmp_path: Path) -> None:
        (tmp_path / "test.yml").write_text('id: "yml-001"\nquestion: "Q"')

        config = self._make_config(tmp_path, ".")
        tests = load_golden_tests(config)
        assert len(tests) == 1
        assert tests[0].id == "yml-001"

    def test_empty_directory(self, tmp_path: Path) -> None:
        golden_dir = tmp_path / "golden"
        golden_dir.mkdir()

        config = self._make_config(tmp_path, "golden")
        tests = load_golden_tests(config)
        assert tests == []

    def test_missing_directory(self, tmp_path: Path) -> None:
        config = self._make_config(tmp_path, "nonexistent")
        tests = load_golden_tests(config)
        assert tests == []

    @staticmethod
    def _make_config(base_dir: Path, golden_dir: str) -> BiEvalsConfig:
        """Create a minimal config pointing at a golden test directory."""
        from bi_evals.config import (
            AgentConfig,
            BiEvalsConfig,
            DatabaseConfig,
            GoldenTestsConfig,
            ProjectConfig,
        )

        config = BiEvalsConfig(
            project=ProjectConfig(name="test"),
            agent=AgentConfig(model="test"),
            database=DatabaseConfig(type="snowflake"),
            golden_tests=GoldenTestsConfig(dir=golden_dir),
        )
        config._base_dir = base_dir
        return config
