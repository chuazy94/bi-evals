# Changelog

All notable changes to bi-evals are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is pre-1.0, minor versions (0.x.0) may include breaking changes;
these are called out under a **Breaking** heading.

## [Unreleased]

### Added
- **Capability check** (Build Stage 2, per
  `docs/plans/build-stage-2-capability-check.md`) — when bi-evals cannot score
  a dimension it now says "I can't know", never "I know it failed":
  - Dimension results carry a first-class status: `pass` | `fail` | `skipped`
    (golden declares nothing to check) | `not_evaluated` (submission lacks the
    data). `skill_path_correctness` with no usable trace is **not evaluated**
    (with an unlock hint naming the exact shape to submit) instead of failing.
  - Pre-flight warning before any warehouse spend (`score` CLI, SDK, and
    `doctor`): "0 of N submissions contain a usable trace — X will not be
    evaluated this run."
  - Report gains a **Capability panel** (rendered only when something wasn't
    evaluable): per-dimension evaluated/not-evaluated counts + the unlock hint.
  - Compare/gating treats `not_evaluated` as absent, not zero — adding a trace
    later doesn't read as a "fix", dropping one doesn't read as a regression.
  - A **critical** dimension that cannot be evaluated fails the test with a
    distinct reason ("must be verifiable to pass") — never silently.
  - `dimension_results` gains a nullable `status` column (auto-migrated on
    connect; historical rows keep boolean-only semantics, no backfill).

### Changed
- **Vacuous skips no longer pad the weighted score.** A dimension the golden
  declares nothing for (e.g. `anti_pattern_compliance` with no `anti_patterns`)
  used to contribute a free `1.0 × weight`; it is now excluded from the score
  entirely (numerator and denominator). Weighted scores can shift slightly on
  unchanged submissions — re-baseline before comparing across this version;
  pass/fail flips are possible for tests sitting near `pass_threshold`.
- Upstream-cascade reasons are honest: row dimensions blocked by an execution
  failure now say "failed upstream: the generated SQL did not execute" instead
  of the misleading "skipped: SQL execution failed". (Still counted as
  failures — the agent caused them.)
- **CI regression gating** (Build Stage 1, per
  `docs/plans/build-stage-1-regression-gating.md`) — one shared gate engine
  (`compare/gate.py: evaluate_gate`) behind both surfaces:
  - `bi-evals compare BASELINE CANDIDATE --fail-on [red|amber|never]` exits 1
    when the gate fails (flag overrides the new `compare.fail_on` config; with
    neither set, compare stays informational and always exits 0).
  - SDK: `report.passed_gate` (absolute floor, no baseline needed) and
    `report.compare_to("prev")` → `GateResult` (baseline regression gate,
    assertable in CI). `GateResult` is exported from `bi_evals`.
  - New `compare:` config knobs, all opt-in: `min_pass_rate` (absolute floor),
    `max_regressions_allowed` (flaky-suite budget), `fail_on` (gate level;
    unset = no gating). Defaults preserve pre-gate behavior exactly.
  - `compare` CLI output now prints the verdict + reasons; the CLI recipe is
    `bi-evals compare prev latest --fail-on red` (baseline first, candidate
    second).
  - **Gate strip on the compare page** — when gating is enabled, the compare
    HTML (both the CLI artifact and the `bi-evals ui` compare view) shows the
    gate outcome under the verdict banner: passed/FAILED, the policy level,
    the reasons (floor/budget arithmetic), and the regressed tests by name.
    Non-gating compares render unchanged.

## [0.1.0] - 2026-06-16

Baseline tag for the framework as it stands after the response-evaluation pivot.

### Added
- **SDK on-ramp** — `bi_evals.Runner` (`golden_cases()` / `submit()` / `score()`)
  with progress logging, returning an assertable `RunReport`.
- **Push adapter** — `bi-evals score --input`; accepts `generated_sql` or raw
  `response_text` from an existing agent.
- **Scoring engine** — 10 binary pass/fail dimensions, weighted score with
  critical-dimension gating.
- **Storage** — embedded DuckDB store with idempotent ingest; per-run prompt
  snapshot for drift detection; multi-trial aggregation in storage
  (`pass_rate` / `score_mean` / `score_stddev`) behind `scoring.repeats`.
- **Reporting & compare** — single-run HTML report and a tiered (🔴/🟡/🟢)
  rate-based run-vs-run comparison; `cost` and `flakiness` views.
- **Two run modes** — built-in (`anthropic_tool_loop`) and bring-your-own
  (`api_endpoint`), sharing one scoring engine.

### Notes
- Versioning starts here. `0.1.x` is pre-1.0: the API and config schema may shift
  between minor versions.

[Unreleased]: https://github.com/chuazy94/bi-evals/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/chuazy94/bi-evals/releases/tag/v0.1.0
