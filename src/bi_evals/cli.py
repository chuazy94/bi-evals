"""CLI entry point for bi-evals."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import click
import yaml

from bi_evals.config import BiEvalsConfig
from bi_evals.golden.loader import load_golden_tests_with_paths
from bi_evals.promptfoo.bridge import (
    filter_tests as bridge_filter_tests,
    generate_promptfoo_config,
    run_promptfoo,
    write_promptfoo_config,
)
from bi_evals.report import build_compare_html, build_report_html
from bi_evals.report.builder import sanitize_for_filename
from bi_evals.store import connect as store_connect
from bi_evals.store import queries as store_queries
from bi_evals.store.ingest import ingest_run


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
    click.echo("  4. Edit .env with your API keys and Snowflake credentials (next to bi-evals.yaml; loaded automatically)")
    click.echo("  5. Run: bi-evals run")


@cli.command()
@click.option("--filter", "-f", "filter_pattern", help="Run only tests matching pattern.")
@click.option("--dry-run", is_flag=True, help="Generate promptfoo config without running.")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose Promptfoo output.")
@click.option("--no-cache", is_flag=True, help="Disable Promptfoo provider cache (force fresh API calls).")
@click.option("--repeats", type=int, default=None, help="Override scoring.repeats: run each golden N times.")
@click.option("--yes", "-y", is_flag=True, help="Skip cost-estimate confirmation prompt.")
@click.pass_context
def run(
    ctx: click.Context,
    filter_pattern: str | None,
    dry_run: bool,
    verbose: bool,
    no_cache: bool,
    repeats: int | None,
    yes: bool,
) -> None:
    """Run the eval suite via Promptfoo."""
    config_path = ctx.obj["config_path"]
    config = BiEvalsConfig.load(config_path)

    if repeats is not None:
        if repeats < 1:
            raise click.ClickException("--repeats must be >= 1")
        config.scoring.repeats = repeats

    pf_config = generate_promptfoo_config(config, config_path, filter_pattern)
    test_count = len(pf_config.get("tests", []))

    if test_count == 0:
        if filter_pattern:
            raise click.ClickException(f"No tests match filter '{filter_pattern}'.")
        raise click.ClickException(
            "No golden tests found. Add tests to the golden/ directory."
        )

    _warn_stale_goldens(config, filter_pattern)
    _warn_stale_knowledge(config)

    models = list(config.agent.models) if config.agent.models else ([config.agent.model] if config.agent.model else [])
    total_trials = test_count * max(1, len(models)) * max(1, config.scoring.repeats)

    click.echo(f"Project: {config.project.name}")
    click.echo(f"Tests:   {test_count}")
    if len(models) > 1:
        click.echo(f"Models:  {', '.join(models)}")
    if config.scoring.repeats > 1:
        click.echo(f"Repeats: {config.scoring.repeats}")
    if total_trials > test_count:
        click.echo(f"Trials:  {total_trials} (cost multiplier: {total_trials / test_count:.1f}x)")
        if not yes and not dry_run:
            if not click.confirm("Proceed?", default=True):
                raise click.Abort()

    if dry_run:
        click.echo("\n--- Generated promptfooconfig.yaml ---")
        click.echo(yaml.dump(pf_config, default_flow_style=False, sort_keys=False))
        return

    results_dir = config.resolve_path(config.reporting.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pf_config_path = results_dir / f"promptfooconfig_{timestamp}.yaml"
    results_output = results_dir / f"eval_{timestamp}.json"

    write_promptfoo_config(pf_config, pf_config_path)
    click.echo(f"Config:  {pf_config_path}")
    click.echo(f"Results: {results_output}")
    click.echo()

    exit_code = run_promptfoo(
        pf_config_path,
        results_output,
        verbose=verbose,
        no_cache=no_cache,
        repeat=max(1, config.scoring.repeats),
    )

    if exit_code != 0:
        raise click.ClickException(f"Promptfoo exited with code {exit_code}")

    if config.storage.auto_ingest:
        try:
            db_path = config.resolve_path(config.storage.db_path)
            with store_connect(db_path) as conn:
                run_id = ingest_run(conn, results_output, config)
                alert = store_queries.cost_alerts(
                    conn, run_id,
                    multiplier=config.storage.cost_alert_multiplier,
                    window=config.storage.cost_alert_window,
                )
            click.echo(f"Ingested: {run_id}")
            click.echo(f"Report:   bi-evals report --run-id {run_id}")
            if alert is not None:
                _echo_cost_alert(alert)
        except Exception as e:
            click.echo(
                f"Warning: ingest failed ({e}). Raw JSON still at {results_output}",
                err=True,
            )

    click.echo(f"\nDone. View results: npx promptfoo view")


@cli.command()
@click.option("--port", "-p", default=15500, help="Port for the web UI.")
def view(port: int) -> None:
    """Open the Promptfoo web UI to browse eval results."""
    import shutil
    import subprocess
    import sys

    if shutil.which("npx") is None:
        raise click.ClickException(
            "Promptfoo not found. Install it: npm install -g promptfoo"
        )

    click.echo(f"Opening Promptfoo UI on http://localhost:{port} ...")
    subprocess.run(
        ["npx", "promptfoo", "view", "--port", str(port)],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


@cli.command()
@click.argument("eval_json_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--force", is_flag=True, help="No-op; ingest is already idempotent.")
@click.pass_context
def ingest(ctx: click.Context, eval_json_path: str, force: bool) -> None:
    """Ingest a Promptfoo eval_*.json into the local DuckDB store."""
    config = BiEvalsConfig.load(ctx.obj["config_path"])
    db_path = config.resolve_path(config.storage.db_path)
    with store_connect(db_path) as conn:
        run_id = ingest_run(conn, eval_json_path, config)
    click.echo(f"Ingested: {run_id}")
    click.echo(f"DB:       {db_path}")


@cli.command()
@click.option("--run-id", help="Specific run (default: latest in DB).")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), help="Output HTML path.")
@click.pass_context
def report(ctx: click.Context, run_id: str | None, out_path: str | None) -> None:
    """Generate an HTML summary report from the ingested run."""
    config = BiEvalsConfig.load(ctx.obj["config_path"])
    db_path = config.resolve_path(config.storage.db_path)

    if not db_path.exists():
        raise click.ClickException(
            "No runs in the DuckDB store. Run `bi-evals run` or `bi-evals ingest <path>` first."
        )

    with store_connect(db_path, read_only=True) as conn:
        rid = run_id or store_queries.latest_run_id(conn)
        if rid is None:
            raise click.ClickException(
                "No runs in the DuckDB store. Run `bi-evals run` or `bi-evals ingest <path>` first."
            )
        try:
            html = build_report_html(
                conn, rid,
                stale_after_days=config.scoring.stale_after_days,
                cost_alert_multiplier=config.storage.cost_alert_multiplier,
                cost_alert_window=config.storage.cost_alert_window,
                knowledge_stale_after_days=config.scoring.knowledge_stale_after_days,
                base_dir=config._base_dir,
            )
        except KeyError:
            raise click.ClickException(
                f"Run '{rid}' not found in DB. Ingest it with `bi-evals ingest <eval_json>`."
            )

    out = _resolve_report_output(config, out_path, f"report_{sanitize_for_filename(rid)}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    click.echo(f"Report: {out}")


@cli.command()
@click.argument("run_a")
@click.argument("run_b")
@click.option("--out", "out_path", type=click.Path(dir_okay=False), help="Output HTML path.")
@click.pass_context
def compare(ctx: click.Context, run_a: str, run_b: str, out_path: str | None) -> None:
    """Compare two runs (by run-id, or shortcuts 'latest' / 'prev')."""
    config = BiEvalsConfig.load(ctx.obj["config_path"])
    db_path = config.resolve_path(config.storage.db_path)

    if not db_path.exists():
        raise click.ClickException(
            "No runs in the DuckDB store. Run `bi-evals run` or `bi-evals ingest <path>` first."
        )

    with store_connect(db_path, read_only=True) as conn:
        a_id = _resolve_run_ref(conn, run_a)
        b_id = _resolve_run_ref(conn, run_b)
        try:
            html = build_compare_html(
                conn, a_id, b_id,
                regression_threshold=config.compare.regression_threshold,
            )
        except KeyError as e:
            raise click.ClickException(str(e))

    filename = f"compare_{sanitize_for_filename(a_id)}__vs__{sanitize_for_filename(b_id)}.html"
    out = _resolve_report_output(config, out_path, filename)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    click.echo(f"Compare: {out}")


def _resolve_run_ref(conn, ref: str) -> str:
    """Translate 'latest' / 'prev' to actual run_ids; otherwise return as-is."""
    if ref == "latest":
        rid = store_queries.latest_run_id(conn)
        if rid is None:
            raise click.ClickException("No runs in DB for 'latest'.")
        return rid
    if ref in ("prev", "previous"):
        rid = store_queries.previous_run_id(conn)
        if rid is None:
            raise click.ClickException("No previous run in DB.")
        return rid
    return ref


def _resolve_report_output(config: BiEvalsConfig, out_path: str | None, default_name: str) -> Path:
    if out_path:
        return Path(out_path).resolve()
    return config.resolve_path(config.reporting.output_dir) / default_name


@cli.command()
@click.option("--last-n", "last_n", default=10, help="Number of recent runs to consider.")
@click.option("--limit", default=20, help="Maximum tests to list.")
@click.pass_context
def flakiness(ctx: click.Context, last_n: int, limit: int) -> None:
    """List tests ranked by cross-run flip count (unstable outcomes)."""
    config = BiEvalsConfig.load(ctx.obj["config_path"])
    db_path = config.resolve_path(config.storage.db_path)

    if not db_path.exists():
        raise click.ClickException(
            "No runs in the DuckDB store. Run `bi-evals run` first."
        )

    with store_connect(db_path, read_only=True) as conn:
        results = store_queries.flakiest_tests(conn, last_n_runs=last_n, limit=limit)

    if not results:
        click.echo("No tests with >1 run in history. Accumulate more runs first.")
        return

    click.echo(f"{'TEST':<60}  {'RUNS':>4}  {'FLIPS':>5}  {'PASS%':>5}  STREAK")
    click.echo("-" * 90)
    for s in results:
        streak = (
            f"{s.current_streak} pass" if s.current_streak > 0
            else f"{-s.current_streak} fail" if s.current_streak < 0
            else "—"
        )
        click.echo(
            f"{s.test_id[:60]:<60}  {s.runs_observed:>4}  "
            f"{s.flip_count:>5}  {int(s.pass_rate_overall * 100):>4}%  {streak}"
        )


@cli.command()
@click.option("--last-n", "last_n", default=20, help="Number of recent runs to inspect.")
@click.pass_context
def cost(ctx: click.Context, last_n: int) -> None:
    """List recent runs with their cost multiplier vs. prior median."""
    config = BiEvalsConfig.load(ctx.obj["config_path"])
    db_path = config.resolve_path(config.storage.db_path)
    if not db_path.exists():
        raise click.ClickException(
            "No runs in the DuckDB store. Run `bi-evals run` first."
        )

    with store_connect(db_path, read_only=True) as conn:
        rows = store_queries.cost_history(conn, last_n=last_n)

    if not rows:
        click.echo("No runs in DB.")
        return

    threshold = config.storage.cost_alert_multiplier
    click.echo(f"{'RUN':<40}  {'COST':>8}  {'MULT':>5}  STATUS")
    click.echo("-" * 72)
    for run, mult in rows:
        cost_str = f"${run.total_cost_usd:.4f}" if run.total_cost_usd is not None else "—"
        if mult <= 0:
            status = "(insufficient history)"
        elif mult >= threshold:
            status = f"⚠ flagged (>= {threshold:.1f}×)"
        else:
            status = "ok"
        click.echo(f"{run.run_id[:40]:<40}  {cost_str:>8}  {mult:>4.1f}×  {status}")


@cli.command()
@click.pass_context
def curate(ctx: click.Context) -> None:
    """Interactive helper to create golden tests from SQL."""
    click.echo("Not yet implemented. Coming in Phase 7.")


def _warn_stale_goldens(config: BiEvalsConfig, filter_pattern: str | None) -> None:
    """Print a warning header listing stale and unverified goldens.

    Loads golden YAMLs directly (not through the DB) since this fires before
    the run, when the run hasn't been ingested yet. ``stale_after_days = 0``
    disables the warning entirely.
    """
    threshold = config.scoring.stale_after_days
    if threshold <= 0:
        return
    pairs = load_golden_tests_with_paths(config)
    if filter_pattern:
        pairs = bridge_filter_tests(pairs, filter_pattern)
    if not pairs:
        return

    today = date.today()
    stale: list[tuple[str, date, int]] = []
    unverified: list[str] = []
    for golden, rel_path in pairs:
        last = golden.last_verified_at
        if last is None:
            unverified.append(rel_path)
            continue
        days = (today - last).days
        if days > threshold:
            stale.append((rel_path, last, days))
    stale.sort(key=lambda x: -x[2])

    if stale:
        click.echo(f"\n⚠  {len(stale)} golden(s) stale (last verified > {threshold} days ago):")
        for path, last, days in stale[:10]:
            click.echo(f"   - {path}  verified {last} ({days} days ago)")
    if unverified:
        click.echo(f"\n⚠  {len(unverified)} golden(s) have no last_verified_at set:")
        for path in unverified[:10]:
            click.echo(f"   - {path}")
    if stale or unverified:
        click.echo("\nProceeding with eval (goldens still run; warning only).\n")


def _warn_stale_knowledge(config: BiEvalsConfig) -> None:
    """Phase 6d: warn about knowledge files that are mtime-stale AND were
    actually read in the most recent ingested run.

    Silent when there's no run history (nothing to intersect with) or the DB
    doesn't exist yet. ``knowledge_stale_after_days = 0`` disables.
    """
    threshold = config.scoring.knowledge_stale_after_days
    if threshold <= 0:
        return
    db_path = config.resolve_path(config.storage.db_path)
    if not db_path.exists():
        return
    try:
        with store_connect(db_path, read_only=True) as conn:
            latest = store_queries.latest_run_id(conn)
            if latest is None:
                return
            stale = store_queries.stale_knowledge_files(
                conn, latest,
                base_dir=config._base_dir,
                stale_after_days=threshold,
            )
    except Exception:
        return
    if not stale:
        return
    click.echo(
        f"\n⚠  {len(stale)} knowledge file(s) stale "
        f"(mtime > {threshold} days ago, read in last run):"
    )
    for f in stale[:10]:
        click.echo(f"   - {f.path}  modified {f.mtime} ({f.days_since_modified} days ago)")
    click.echo("\nProceeding with eval (warning only).\n")


def _echo_cost_alert(alert: store_queries.CostAlert) -> None:
    click.echo(
        f"\n⚠  This run cost ${alert.actual_cost:.4f}, "
        f"{alert.multiplier:.1f}× the median (${alert.median_cost:.4f}) "
        f"of the last {alert.sample_size} runs."
    )
    if alert.anomalous_tests:
        click.echo("   Anomalous tests:")
        for tid, actual, median in alert.anomalous_tests[:5]:
            mult = (actual / median) if median else 0.0
            click.echo(f"     - {tid}: ${actual:.4f} vs median ${median:.4f} ({mult:.1f}×)")


def _scaffold_project(target: Path) -> None:
    """Create eval infrastructure files only. No skill/knowledge files."""
    # bi-evals.yaml
    config_file = target / "bi-evals.yaml"
    if not config_file.exists():
        config_file.write_text(_TEMPLATE_CONFIG)

    # .env.example (reference; safe to commit if you version this folder)
    env_example = target / ".env.example"
    if not env_example.exists():
        env_example.write_text(_TEMPLATE_ENV)

    # .env (sample placeholders; fill with real values — do not commit secrets)
    dot_env = target / ".env"
    if not dot_env.exists():
        dot_env.write_text(_SAMPLE_DOT_ENV)

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
  # Option 1: "anthropic_tool_loop" — eval runs Claude with your skill files
  # Option 2: "api_endpoint" — eval calls your existing agent API
  type: "anthropic_tool_loop"

  # --- Settings for anthropic_tool_loop ---
  model: "claude-sonnet-4-5-20250929"
  system_prompt: "path/to/your/system-prompt.md"     # Path to your system prompt
  tools:
    - name: read_skill_file                           # Tool name the agent uses
      type: file_reader
      config:
        base_dir: "path/to/your/skill/"               # Path to your existing skill/knowledge files
  max_rounds: 10

  # --- Settings for api_endpoint ---
  # endpoint:
  #   url: "https://your-agent-api.com/ask"
  #   method: POST
  #   headers:
  #     Authorization: "Bearer ${API_TOKEN}"
  #   response_sql_key: "sql"           # JSONPath to SQL in the response
  #   response_text_key: "text"         # JSONPath to text answer in the response
  #   timeout: 60

database:
  type: snowflake
  connection:
    account: "${SNOWFLAKE_ACCOUNT}"
    user: "${SNOWFLAKE_USER}"
    private_key_path: "${SNOWFLAKE_PRIVATE_KEY_PATH}"
    private_key_passphrase: "${SNOWFLAKE_PRIVATE_KEY_PASSPHRASE}"  # optional, if key is encrypted
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

storage:
  db_path: "results/bi-evals.duckdb"
  auto_ingest: true
"""

_TEMPLATE_ENV = """\
ANTHROPIC_API_KEY=sk-ant-...
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=
SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=
SNOWFLAKE_WAREHOUSE=
SNOWFLAKE_DATABASE=
SNOWFLAKE_SCHEMA=
"""

_SAMPLE_DOT_ENV = """\
# Local credentials for this eval project (gitignored in bi-evals repo root).
# Replace placeholder values before running bi-evals run.

ANTHROPIC_API_KEY=sk-ant-...
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=
SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=
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
