"""Load golden tests from YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml

from bi_evals.config import BiEvalsConfig
from bi_evals.golden.model import GoldenTest


def load_golden_test(path: Path) -> GoldenTest:
    """Load a single golden test from a YAML file."""
    data = yaml.safe_load(path.read_text())
    return GoldenTest(**data)


def load_golden_tests(config: BiEvalsConfig) -> list[GoldenTest]:
    """Load all golden tests from the configured directory.

    Returns tests sorted by file path for deterministic ordering.
    """
    golden_dir = config.resolve_path(config.golden_tests.dir)
    if not golden_dir.exists():
        return []

    tests = []
    for yaml_file in sorted(golden_dir.glob("**/*.yaml")):
        tests.append(load_golden_test(yaml_file))
    for yml_file in sorted(golden_dir.glob("**/*.yml")):
        tests.append(load_golden_test(yml_file))
    return tests


def load_golden_tests_with_paths(config: BiEvalsConfig) -> list[tuple[GoldenTest, str]]:
    """Load all golden tests, returning (test, relative_path) pairs.

    The relative path is relative to the config file's base directory,
    matching how the scorer resolves golden_file paths.
    """
    golden_dir = config.resolve_path(config.golden_tests.dir)
    if not golden_dir.exists():
        return []

    results: list[tuple[GoldenTest, str]] = []
    for pattern in ("**/*.yaml", "**/*.yml"):
        for yaml_file in sorted(golden_dir.glob(pattern)):
            test = load_golden_test(yaml_file)
            rel = str(yaml_file.relative_to(config._base_dir))
            results.append((test, rel))
    return results
