"""CLI entry point for bi-evals."""

from __future__ import annotations

from pathlib import Path

import click

from bi_evals.config import BiEvalsConfig


@click.group()
@click.option(
    "--config",
    "-c",
    "config_path",
    default="bi-evals.yaml",
    help="Path to bi-evals.yaml config file.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """bi-evals: Evaluation framework for SQL-generating BI agents."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command()
@click.option(
    "--dir",
    "-d",
    "target_dir",
    default=".",
    help="Directory to scaffold the project in.",
)
def init(target_dir: str) -> None:
    """Scaffold a new bi-evals project."""
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    _scaffold_project(target)
    click.echo(f"Scaffolded bi-evals project in {target}")
    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Edit bi-evals.yaml — point agent.tools[].config.base_dir to your skill/knowledge files")
    click.echo("  2. Edit bi-evals.yaml — configure your database connection")
    click.echo("  3. Create golden tests in golden/")
    click.echo("  4. Set environment variables (see .env.example)")
    click.echo("  5. Run: bi-evals run")


@cli.command()
@click.option("--filter", "-f", "filter_pattern", help="Run only tests matching pattern.")
@click.option("--dry-run", is_flag=True, help="Generate promptfoo config without running.")
@click.pass_context
def run(ctx: click.Context, filter_pattern: str | None, dry_run: bool) -> None:
    """Run the eval suite via Promptfoo."""
    config = BiEvalsConfig.load(ctx.obj["config_path"])
    click.echo(f"Running evals for: {config.project.name}")
    # TODO: implement in Phase 4
    click.echo("Not yet implemented. Coming in Phase 4.")


@cli.command()
@click.option("--run-id", help="Specific run to report on (default: latest).")
@click.pass_context
def report(ctx: click.Context, run_id: str | None) -> None:
    """Generate HTML report from eval results."""
    click.echo("Not yet implemented. Coming in Phase 5.")


@cli.command()
@click.argument("run1")
@click.argument("run2")
@click.pass_context
def compare(ctx: click.Context, run1: str, run2: str) -> None:
    """Compare two eval runs for regressions."""
    click.echo("Not yet implemented. Coming in Phase 5.")


@cli.command()
@click.pass_context
def curate(ctx: click.Context) -> None:
    """Interactive helper to create golden tests from SQL."""
    click.echo("Not yet implemented. Coming in Phase 7.")


def _scaffold_project(target: Path) -> None:
    """Create eval infrastructure files only. No skill/knowledge files."""
    # bi-evals.yaml
    config_file = target / "bi-evals.yaml"
    if not config_file.exists():
        config_file.write_text(_TEMPLATE_CONFIG)

    # .env.example
    env_file = target / ".env.example"
    if not env_file.exists():
        env_file.write_text(_TEMPLATE_ENV)

    # Directory structure — eval infrastructure only
    for d in ["golden", "results", "reports"]:
        (target / d).mkdir(parents=True, exist_ok=True)

    # Example golden test
    golden_file = target / "golden" / "example-query.yaml"
    if not golden_file.exists():
        golden_file.write_text(_TEMPLATE_GOLDEN)

    # .gitkeep files
    for d in ["results", "reports"]:
        gitkeep = target / d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("")


_TEMPLATE_CONFIG = """\
project:
  name: "My BI Agent Evals"

agent:
  model: "claude-sonnet-4-5-20250929"
  # Path to your system prompt file
  system_prompt: "path/to/your/system-prompt.md"
  tools:
    - name: read_skill_file          # Tool name the agent uses
      type: file_reader
      config:
        base_dir: "path/to/your/skill/"  # Path to your existing skill/knowledge files
  max_rounds: 10

database:
  type: snowflake
  connection:
    account: "${SNOWFLAKE_ACCOUNT}"
    user: "${SNOWFLAKE_USER}"
    password: "${SNOWFLAKE_PASSWORD}"
    warehouse: "${SNOWFLAKE_WAREHOUSE}"
    database: "${SNOWFLAKE_DATABASE}"
    schema: "${SNOWFLAKE_SCHEMA}"
  query_timeout: 30

golden_tests:
  dir: "golden/"

scoring:
  dimensions:
    - execution
    - table_alignment
    - column_alignment
    - filter_correctness
    - row_completeness
    - row_precision
    - value_accuracy
    - no_hallucinated_columns
    - skill_path_correctness
  thresholds:
    completeness: 0.95
    precision: 0.95
    value_tolerance: 0.0001

reporting:
  output_dir: "reports/"
  results_dir: "results/"
"""

_TEMPLATE_ENV = """\
ANTHROPIC_API_KEY=sk-ant-...
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=
SNOWFLAKE_PASSWORD=
SNOWFLAKE_WAREHOUSE=
SNOWFLAKE_DATABASE=
SNOWFLAKE_SCHEMA=
"""

_TEMPLATE_GOLDEN = """\
id: example-001
category: example
difficulty: easy
question: "What is the total value for each name?"

# expected_skill_path:
#   required_skills:
#     - tool: read_skill_file
#       input_contains: "SKILL.md"
#     - tool: read_skill_file
#       input_contains: "YOUR_KNOWLEDGE_FILE.md"
#   sequence_matters: true
#   allow_extra_skills: true

reference_sql: |
  SELECT NAME, SUM(VALUE) AS TOTAL_VALUE
  FROM MY_DATABASE.MY_SCHEMA.MY_TABLE
  GROUP BY NAME
  ORDER BY TOTAL_VALUE DESC

expected:
  min_rows: 1
  required_columns:
    - NAME
    - TOTAL_VALUE
  checks:
    - column: TOTAL_VALUE
      condition: type
      value: positive_number
  # row_comparison:
  #   enabled: true
  #   completeness_threshold: 0.95
  #   precision_threshold: 0.95
  #   value_tolerance: 0.0001
  #   key_columns: [NAME]
  #   value_columns: [TOTAL_VALUE]
  #   ignore_order: true

tags: [example]
notes: "Example golden test — replace with your actual queries."
"""
