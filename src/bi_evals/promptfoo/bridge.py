"""Promptfoo config generation and runner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from bi_evals.config import BiEvalsConfig
from bi_evals.golden.loader import load_golden_tests_with_paths
from bi_evals.golden.model import GoldenTest


def filter_tests(
    tests: list[tuple[GoldenTest, str]],
    pattern: str,
) -> list[tuple[GoldenTest, str]]:
    """Filter golden tests by substring match on id, category, or tags."""
    p = pattern.lower()
    return [
        (t, path)
        for t, path in tests
        if p in t.id.lower()
        or p in t.category.lower()
        or any(p in tag.lower() for tag in t.tags)
    ]


def _get_package_root() -> Path:
    """Return the root of the bi_evals package source tree."""
    return Path(__file__).resolve().parent.parent.parent.parent


def generate_promptfoo_config(
    config: BiEvalsConfig,
    config_path: str,
    filter_pattern: str | None = None,
) -> dict[str, Any]:
    """Generate a Promptfoo config dict from bi-evals config and golden tests.

    Args:
        config: Loaded BiEvalsConfig.
        config_path: Path to bi-evals.yaml (passed to provider/scorer).
        filter_pattern: Optional substring filter for test selection.

    Returns:
        Dict ready to be written as promptfooconfig.yaml.
    """
    tests_with_paths = load_golden_tests_with_paths(config)

    if filter_pattern:
        tests_with_paths = filter_tests(tests_with_paths, filter_pattern)

    pkg_root = _get_package_root()
    provider_path = pkg_root / "src" / "bi_evals" / "provider" / "entry.py"
    scorer_path = pkg_root / "src" / "bi_evals" / "scorer" / "entry.py"

    abs_config_path = str(Path(config_path).resolve())

    promptfoo_tests = []
    for golden_test, rel_path in tests_with_paths:
        desc = f"{golden_test.id}: {golden_test.question[:60]}"
        promptfoo_tests.append(
            {
                "description": desc,
                "vars": {
                    "question": golden_test.question,
                    "golden_file": rel_path,
                },
                "assert": [
                    {
                        "type": "python",
                        "value": f"file://{scorer_path}:get_assert",
                    }
                ],
            }
        )

    return {
        "prompts": ["{{question}}"],
        "providers": [
            {
                "id": f"file://{provider_path}:call_api",
                "config": {
                    "config_path": abs_config_path,
                },
            }
        ],
        "tests": promptfoo_tests,
    }


def write_promptfoo_config(config_dict: dict[str, Any], output_path: Path) -> Path:
    """Write a Promptfoo config dict as YAML."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.dump(config_dict, default_flow_style=False, sort_keys=False)
    )
    return output_path


def run_promptfoo(
    config_path: Path,
    results_path: Path,
    *,
    verbose: bool = False,
    no_cache: bool = False,
) -> int:
    """Run Promptfoo eval via npx, streaming output to the terminal.

    Returns:
        exit_code from the Promptfoo process.

    Raises:
        click.ClickException: If npx/promptfoo is not installed.
    """
    import sys

    import click

    if shutil.which("npx") is None:
        raise click.ClickException(
            "Promptfoo not found. Install it: npm install -g promptfoo"
        )

    cmd = [
        "npx", "promptfoo", "eval",
        "--config", str(config_path),
        "--output", str(results_path),
    ]
    if verbose:
        cmd.append("--verbose")
    if no_cache:
        cmd.append("--no-cache")

    process = subprocess.Popen(
        cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    process.wait()
    return process.returncode
