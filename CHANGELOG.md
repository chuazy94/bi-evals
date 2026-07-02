# Changelog

All notable changes to bi-evals are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is pre-1.0, minor versions (0.x.0) may include breaking changes;
these are called out under a **Breaking** heading.

## [Unreleased]

### Added
- Regression-gating plan for CI (`docs/plans/build-stage-1-regression-gating.md`): shared gate
  engine, absolute floor + baseline regression, CLI + SDK surfaces, multi-trial
  aggregation. _Design only — not yet implemented._

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
