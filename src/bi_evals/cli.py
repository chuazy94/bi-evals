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


@cli.group(invoke_without_command=True)
@click.pass_context
def init(ctx: click.Context) -> None:
    """Scaffold a new bi-evals project.

    Default on-ramp is `api_endpoint` (bi-evals calls your existing agent over
    HTTP and scores what it returns). `dev` scaffolds the dev-only driving
    adapter for authoring goldens before a real agent exists.
    """
    if ctx.invoked_subcommand is None:
        raise click.UsageError(
            "must specify a scaffold: 'api_endpoint' (default on-ramp) or 'dev'. "
            "Run 'bi-evals init --help' for details."
        )


@init.command("api_endpoint")
@click.option(
    "--dir",
    "-d",
    "target_dir",
    default=".",
    help="Directory to scaffold the project in.",
)
def init_api_endpoint(target_dir: str) -> None:
    """Scaffold an api_endpoint project (bi-evals calls your agent over HTTP)."""
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    _scaffold_project(target, mode="api_endpoint")
    click.echo(f"Scaffolded api_endpoint bi-evals project in {target}")
    click.echo()
    click.echo("Next steps:")
    click.echo(
        "  1. Edit bi-evals.yaml — set agent.api_endpoint.url (and headers if needed) to point at your agent"
    )
    click.echo("  2. Edit bi-evals.yaml — configure your database connection")
    click.echo("  3. Create golden tests in golden/")
    click.echo(
        "  4. Edit .env with BI_AGENT_URL, BI_AGENT_TOKEN (if applicable), and Snowflake credentials"
    )
    click.echo(
        "  5. See adapter_example.py for a reference FastAPI shim if your agent isn't HTTP-reachable yet"
    )
    click.echo("  6. Run: bi-evals run")


@init.command("dev")
@click.option(
    "--dir",
    "-d",
    "target_dir",
    default=".",
    help="Directory to scaffold the project in.",
)
def init_dev(target_dir: str) -> None:
    """Scaffold the dev-only driving adapter (bi-evals runs Claude + skill files).

    Not a production-fidelity setup — it evaluates a local rebuild of an agent.
    Useful for authoring goldens before a real agent exists.
    """
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    _scaffold_project(target, mode="dev")
    click.echo(f"Scaffolded dev (anthropic_tool_loop) bi-evals project in {target}")
    click.echo()
    click.echo("Next steps:")
    click.echo(
        "  1. Create your system-prompt.md and skill files (e.g. skills/SKILL.md, skills/knowledge/*.md)"
    )
    click.echo(
        "  2. Edit bi-evals.yaml — point agent.anthropic_tool_loop.tools[].config.base_dir to your skill/knowledge files"
    )
    click.echo("  3. Edit bi-evals.yaml — configure your database connection")
    click.echo("  4. Create golden tests in golden/")
    click.echo(
        "  5. Edit .env with ANTHROPIC_API_KEY and Snowflake credentials (next to bi-evals.yaml; loaded automatically)"
    )
    click.echo("  6. Run: bi-evals run")


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Validate the project's runtime setup before running an eval.

    For BYO mode: POSTs a synthetic question to the configured endpoint,
    validates the response against the bundled JSON Schema, and reports
    which optional fields are present (and which scoring dimensions they
    unlock).

    For Built-in mode: checks the Anthropic API key, system prompt,
    file_reader base_dirs, Snowflake reachability (real SELECT 1), and
    Promptfoo (npx) availability.

    Exits 0 only when no required checks fail. Warnings indicate degraded
    scoring coverage but do not block.
    """
    from bi_evals.doctor import (
        check_builtin_setup,
        check_byo_endpoint,
        format_report,
        is_failing,
    )

    config_path = ctx.obj["config_path"]
    config = BiEvalsConfig.load(config_path)

    if config.agent.adapter == "api_endpoint":
        results = check_byo_endpoint(config)
        mode = "api_endpoint"
    elif config.agent.adapter == "anthropic_tool_loop":
        results = check_builtin_setup(config)
        mode = "anthropic_tool_loop (dev-only)"
    else:
        raise click.ClickException(
            f"Unknown agent.adapter {config.agent.adapter!r}. "
            "Expected 'api_endpoint' or 'anthropic_tool_loop'."
        )

    click.echo(format_report(results, mode=mode))
    if is_failing(results):
        ctx.exit(1)


@cli.command()
@click.option(
    "--filter", "-f", "filter_pattern", help="Run only tests matching pattern."
)
@click.option(
    "--dry-run", is_flag=True, help="Generate promptfoo config without running."
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose Promptfoo output.")
@click.option(
    "--no-cache",
    is_flag=True,
    help="Disable Promptfoo provider cache (force fresh API calls).",
)
@click.option(
    "--repeats",
    type=int,
    default=None,
    help="Override scoring.repeats: run each golden N times.",
)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip cost-estimate confirmation prompt."
)
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

    # Multi-model fan-out is a property of the driving adapter only; other
    # adapters run one trial per (test, repeat). Be explicit rather than relying
    # on the back-compat .models accessor coincidentally returning [] elsewhere.
    models = (
        list(config.agent.anthropic_tool_loop.models)
        if config.agent.adapter == "anthropic_tool_loop"
        else []
    )
    total_trials = test_count * max(1, len(models)) * max(1, config.scoring.repeats)

    click.echo(f"Project: {config.project.name}")
    click.echo(f"Tests:   {test_count}")
    if len(models) > 1:
        click.echo(f"Models:  {', '.join(models)}")
    if config.scoring.repeats > 1:
        click.echo(f"Repeats: {config.scoring.repeats}")
    if total_trials > test_count:
        click.echo(
            f"Trials:  {total_trials} (cost multiplier: {total_trials / test_count:.1f}x)"
        )
        if not yes and not dry_run:
            if not click.confirm("Proceed?", default=True):
                raise click.Abort()

    if dry_run:
        click.echo("\n--- Generated promptfooconfig.yaml ---")
        click.echo(yaml.dump(pf_config, default_flow_style=False, sort_keys=False))
        return

    _execute_eval(config, pf_config, verbose=verbose, no_cache=no_cache)


def _execute_eval(
    config: BiEvalsConfig,
    pf_config: dict,
    *,
    verbose: bool,
    no_cache: bool,
) -> None:
    """Write the promptfoo config, run it, and auto-ingest the result.

    Shared by `run` (live adapters) and `score` (push replay) — everything after
    config generation is identical: both produce the same eval JSON + traces and
    flow through the same ingest path.
    """
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

    # Ingest whenever the results JSON exists, even if Promptfoo exited non-zero
    # (it returns a non-zero code when any test fails — but a failing run is
    # exactly when the user wants to inspect the report).
    if config.storage.auto_ingest and results_output.exists():
        try:
            db_path = config.resolve_path(config.storage.db_path)
            with store_connect(db_path) as conn:
                run_id = ingest_run(conn, results_output, config)
                alert = store_queries.cost_alerts(
                    conn,
                    run_id,
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

    if exit_code != 0:
        raise click.ClickException(f"Promptfoo exited with code {exit_code}")

    click.echo(f"\nDone. View results: npx promptfoo view")


@cli.command()
@click.option(
    "--input",
    "-i",
    "input_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSONL of submissions: one {golden_file, generated_sql, trace?} per line.",
)
@click.option(
    "--filter", "-f", "filter_pattern", help="Score only tests matching pattern."
)
@click.option(
    "--dry-run", is_flag=True, help="Generate promptfoo config without running."
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose Promptfoo output.")
@click.pass_context
def score(
    ctx: click.Context,
    input_file: str,
    filter_pattern: str | None,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Score a pre-run submission file (push adapter).

    You run your own agent over the goldens, write one JSONL line per result
    ({golden_file, generated_sql, trace?}), and bi-evals scores it — no live
    agent, no API spend. This forces the push adapter regardless of the
    configured `agent.adapter`.
    """
    config_path = ctx.obj["config_path"]
    config = BiEvalsConfig.load(config_path)

    # Force push and point it at the submission file. score is the push entry
    # point; the configured adapter (often api_endpoint) is intentionally
    # overridden so a user can score without editing bi-evals.yaml.
    config.agent.adapter = "push"
    config.agent.push.input_file = str(Path(input_file).resolve())

    pf_config = generate_promptfoo_config(config, config_path, filter_pattern)
    test_count = len(pf_config.get("tests", []))
    if test_count == 0:
        if filter_pattern:
            raise click.ClickException(f"No tests match filter '{filter_pattern}'.")
        raise click.ClickException(
            "No golden tests found. Add tests to the golden/ directory."
        )

    _validate_push_submissions(config, pf_config, input_file)

    click.echo(f"Project: {config.project.name}")
    click.echo(f"Tests:   {test_count}")
    click.echo(f"Input:   {input_file}")
    click.echo()

    if dry_run:
        click.echo("--- Generated promptfooconfig.yaml ---")
        click.echo(yaml.dump(pf_config, default_flow_style=False, sort_keys=False))
        return

    _execute_eval(config, pf_config, verbose=verbose, no_cache=True)


def _validate_push_submissions(
    config: BiEvalsConfig, pf_config: dict, input_file: str
) -> None:
    """Fail fast with a clear message before launching Promptfoo.

    Checks every selected golden has a submission row, and warns about
    submissions that don't match any selected golden (typo / stale row).
    """
    import json

    submitted: dict[str, dict] = {}
    for lineno, line in enumerate(Path(input_file).read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise click.ClickException(f"{input_file}:{lineno}: invalid JSON ({e})")
        gf = row.get("golden_file")
        if not gf:
            raise click.ClickException(
                f"{input_file}:{lineno}: row is missing required 'golden_file'."
            )
        if not row.get("generated_sql"):
            raise click.ClickException(
                f"{input_file}:{lineno}: row for '{gf}' is missing 'generated_sql'."
            )
        submitted[gf] = row

    selected = {t["vars"]["golden_file"] for t in pf_config.get("tests", [])}
    missing = sorted(selected - set(submitted))
    if missing:
        raise click.ClickException(
            "No submission for these goldens:\n  "
            + "\n  ".join(missing)
            + f"\nAdd a line per golden to {input_file}."
        )
    extra = sorted(set(submitted) - selected)
    if extra:
        click.echo(
            f"Note: {len(extra)} submission(s) don't match any selected golden "
            f"(ignored): {', '.join(extra[:5])}" + (" ..." if len(extra) > 5 else ""),
            err=True,
        )


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
@click.option("--port", "-p", default=8765, help="Port to serve the UI on.")
@click.option("--host", default="127.0.0.1", help="Host interface to bind.")
@click.option("--no-open", is_flag=True, help="Do not auto-open the browser.")
@click.pass_context
def ui(ctx: click.Context, port: int, host: str, no_open: bool) -> None:
    """Launch the local web viewer (runs list, single-run, compare)."""
    import threading
    import webbrowser

    import uvicorn

    from bi_evals.ui import create_app

    config = BiEvalsConfig.load(ctx.obj["config_path"])
    app = create_app(config)

    url = f"http://{host}:{port}"
    click.echo(f"bi-evals viewer → {url}")
    click.echo("Ctrl+C to stop.")

    if not no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")


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
@click.option(
    "--out", "out_path", type=click.Path(dir_okay=False), help="Output HTML path."
)
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
                conn,
                rid,
                stale_after_days=config.scoring.stale_after_days,
                cost_alert_multiplier=config.storage.cost_alert_multiplier,
                cost_alert_window=config.storage.cost_alert_window,
                knowledge_stale_after_days=config.scoring.knowledge_stale_after_days,
                base_dir=config._base_dir,
                pass_threshold=config.scoring.pass_threshold,
                critical_dimensions=list(config.scoring.critical_dimensions),
            )
        except KeyError:
            raise click.ClickException(
                f"Run '{rid}' not found in DB. Ingest it with `bi-evals ingest <eval_json>`."
            )

    out = _resolve_report_output(
        config, out_path, f"report_{sanitize_for_filename(rid)}.html"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    click.echo(f"Report: {out}")


@cli.command()
@click.argument("run_a")
@click.argument("run_b")
@click.option(
    "--out", "out_path", type=click.Path(dir_okay=False), help="Output HTML path."
)
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
                conn,
                a_id,
                b_id,
                regression_threshold=config.compare.regression_threshold,
            )
        except KeyError as e:
            raise click.ClickException(str(e))

    filename = (
        f"compare_{sanitize_for_filename(a_id)}__vs__{sanitize_for_filename(b_id)}.html"
    )
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


def _resolve_report_output(
    config: BiEvalsConfig, out_path: str | None, default_name: str
) -> Path:
    if out_path:
        return Path(out_path).resolve()
    return config.resolve_path(config.reporting.output_dir) / default_name


@cli.command()
@click.option(
    "--last-n", "last_n", default=10, help="Number of recent runs to consider."
)
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
            f"{s.current_streak} pass"
            if s.current_streak > 0
            else f"{-s.current_streak} fail"
            if s.current_streak < 0
            else "—"
        )
        click.echo(
            f"{s.test_id[:60]:<60}  {s.runs_observed:>4}  "
            f"{s.flip_count:>5}  {int(s.pass_rate_overall * 100):>4}%  {streak}"
        )


@cli.command()
@click.option(
    "--last-n", "last_n", default=20, help="Number of recent runs to inspect."
)
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
        cost_str = (
            f"${run.total_cost_usd:.4f}" if run.total_cost_usd is not None else "—"
        )
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
        click.echo(
            f"\n⚠  {len(stale)} golden(s) stale (last verified > {threshold} days ago):"
        )
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
                conn,
                latest,
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
        click.echo(
            f"   - {f.path}  modified {f.mtime} ({f.days_since_modified} days ago)"
        )
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
            click.echo(
                f"     - {tid}: ${actual:.4f} vs median ${median:.4f} ({mult:.1f}×)"
            )


def _scaffold_project(target: Path, *, mode: str) -> None:
    """Create eval infrastructure files. Mode-aware: 'dev' or 'api_endpoint'.

    The 'dev' mode (dev-only driving adapter) writes a Claude-harness config
    and .env keyed on ANTHROPIC_API_KEY. The 'api_endpoint' mode (default
    on-ramp) writes an api_endpoint config, .env keyed on
    BI_AGENT_URL/BI_AGENT_TOKEN, and an adapter_example.py FastAPI shim
    demonstrating the response shape bi-evals expects.

    Neither mode scaffolds skill/knowledge files — in 'dev' mode the user
    provides those; in 'api_endpoint' mode they live with the user's agent.
    """
    if mode == "dev":
        config_template = _TEMPLATE_CONFIG_BUILTIN
        env_template = _TEMPLATE_ENV_BUILTIN
        sample_env = _SAMPLE_DOT_ENV_BUILTIN
    elif mode == "api_endpoint":
        config_template = _TEMPLATE_CONFIG_BYO
        env_template = _TEMPLATE_ENV_BYO
        sample_env = _SAMPLE_DOT_ENV_BYO
    else:
        raise ValueError(f"unknown mode: {mode!r}")

    # bi-evals.yaml
    config_file = target / "bi-evals.yaml"
    if not config_file.exists():
        config_file.write_text(config_template)

    # .env.example (reference; safe to commit if you version this folder)
    env_example = target / ".env.example"
    if not env_example.exists():
        env_example.write_text(env_template)

    # .env (sample placeholders; fill with real values — do not commit secrets)
    dot_env = target / ".env"
    if not dot_env.exists():
        dot_env.write_text(sample_env)

    # Directory structure — eval infrastructure only
    for d in ["golden", "results", "reports"]:
        (target / d).mkdir(parents=True, exist_ok=True)

    # Example golden test (mode-agnostic — schema is identical)
    golden_file = target / "golden" / "example-query.yaml"
    if not golden_file.exists():
        golden_file.write_text(_TEMPLATE_GOLDEN)

    # api_endpoint-only: reference FastAPI shim showing the response contract
    if mode == "api_endpoint":
        adapter_file = target / "adapter_example.py"
        if not adapter_file.exists():
            adapter_file.write_text(_TEMPLATE_BYO_ADAPTER)

    # .gitkeep files
    for d in ["results", "reports"]:
        gitkeep = target / d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("")


# ────────────────────────────────────────────────────────────────────────────
# Built-in mode templates — bi-evals runs Claude with your skill files
# ────────────────────────────────────────────────────────────────────────────

_TEMPLATE_CONFIG_BUILTIN = """\
# Dev-only adapter: bi-evals runs Claude with your skill/knowledge files.
# This evaluates a LOCAL REBUILD of an agent — not a production-fidelity setup.
# It's for authoring goldens before a real agent exists. The default on-ramp is
# api_endpoint (bi-evals init api_endpoint).

project:
  name: "My BI Agent Evals"

agent:
  adapter: "anthropic_tool_loop"
  anthropic_tool_loop:
    model: "claude-sonnet-4-6"                          # example; any valid Anthropic model ID works
    system_prompt: "path/to/your/system-prompt.md"      # Path to your system prompt
    tools:
      - name: read_skill_file                           # Tool name the agent uses
        type: file_reader
        config:
          base_dir: "path/to/your/skill/"               # Path to your existing skill/knowledge files
    max_rounds: 10

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

_TEMPLATE_ENV_BUILTIN = """\
ANTHROPIC_API_KEY=sk-ant-...
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=
SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=
SNOWFLAKE_WAREHOUSE=
SNOWFLAKE_DATABASE=
SNOWFLAKE_SCHEMA=
"""

_SAMPLE_DOT_ENV_BUILTIN = """\
# Local credentials for this Built-in mode eval project (gitignored).
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

# ────────────────────────────────────────────────────────────────────────────
# BYO mode templates — bi-evals calls your existing agent over HTTP
# ────────────────────────────────────────────────────────────────────────────

_TEMPLATE_CONFIG_BYO = """\
# api_endpoint adapter (default on-ramp): bi-evals POSTs each question to your
# existing agent endpoint and scores what it returns. Your agent owns its own
# skills/prompt/routing. For authoring goldens without a live agent, see
# `bi-evals init dev`.

project:
  name: "My BI Agent Evals"

agent:
  adapter: "api_endpoint"
  api_endpoint:
    url: "${BI_AGENT_URL}"                              # e.g. http://localhost:8000/ask
    method: "POST"                                      # default; omit if unchanged
    timeout: 60                                         # seconds; default
    headers:
      Authorization: "Bearer ${BI_AGENT_TOKEN}"         # optional; omit if your endpoint doesn't need auth
    # response_text_key defaults to "text", response_sql_key defaults to "sql".
    # Set them explicitly if your endpoint uses different field names or nests its response
    # (dot-notation supported, e.g. "response.sql").

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
    # skill_path_correctness only works if your endpoint returns `files_read`
    # or `trace` data. Re-enable once your endpoint emits it.
    # - skill_path_correctness
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

_TEMPLATE_ENV_BYO = """\
BI_AGENT_URL=http://localhost:8000/ask
BI_AGENT_TOKEN=
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=
SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=
SNOWFLAKE_WAREHOUSE=
SNOWFLAKE_DATABASE=
SNOWFLAKE_SCHEMA=
"""

_SAMPLE_DOT_ENV_BYO = """\
# Local credentials for this BYO mode eval project (gitignored).
# Replace placeholder values before running bi-evals run.
# BI_AGENT_URL is where bi-evals will POST each test question.

BI_AGENT_URL=http://localhost:8000/ask
BI_AGENT_TOKEN=
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=
SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=
SNOWFLAKE_WAREHOUSE=
SNOWFLAKE_DATABASE=
SNOWFLAKE_SCHEMA=
"""

_TEMPLATE_BYO_ADAPTER = '''\
"""Reference FastAPI adapter for BYO mode.

bi-evals expects to POST {"question": "..."} to your endpoint and receive
JSON back. This file shows the minimum shape that lets the scorer work,
plus the optional fields that unlock the full set of scoring dimensions.

You do not have to run this exact file. Treat it as a template for wrapping
your existing production agent in a thin HTTP layer for evaluation:
import your agent here, call it in /ask, format the response.

Run locally:
    pip install fastapi uvicorn
    uvicorn adapter_example:app --port 8000

Then point bi-evals at it:
    BI_AGENT_URL=http://localhost:8000/ask
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    # Minimum useful response — scores SQL+results dimensions:
    text: str  # Natural-language answer
    sql: str   # SQL the agent generated

    # Optional — unlocks knowledge-file scoring (skill_path_correctness):
    # files_read: list of knowledge files your agent's retrieval surfaced.
    # trace: per-step record of tool calls / reasoning steps (shape mirrors
    # bi_evals.provider.agent_loop.TraceStep — see api_endpoint.py for the
    # exact fields bi-evals reads).
    files_read: list[str] | None = None
    trace: list[dict[str, Any]] | None = None


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    # Replace this stub with a call to your real agent.
    # Example:
    #     result = my_agent.run(req.question)
    #     return AskResponse(
    #         text=result.answer,
    #         sql=result.sql,
    #         files_read=result.retrieved_files,
    #     )
    return AskResponse(
        text=f"(stub) You asked: {req.question}",
        sql="SELECT 1",
        files_read=[],
    )
'''

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
