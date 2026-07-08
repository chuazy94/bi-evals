"""CI gate tests: pure evaluate_gate logic, plus the SDK gate surface
(RunReport.passed_gate / compare_to) against a seeded store."""

from __future__ import annotations

import dataclasses

import pytest

from bi_evals.compare.gate import GateResult, classify_runs, evaluate_gate
from bi_evals.config import BiEvalsConfig
from bi_evals.runner_core import PushScoreError
from bi_evals.sdk import RunReport
from bi_evals.store import connect as store_connect
from bi_evals.store.ingest import ingest_run
from bi_evals.store.queries import RunTestPair

from tests.conftest import RUN_A_ID, RUN_A_JSON, RUN_B_ID, RUN_B_JSON

CRITICAL = {"execution", "row_completeness", "value_accuracy"}


def _pair(
    test_id: str,
    *,
    a_pass: bool | None,
    b_pass: bool | None,
    a_score: float | None = None,
    b_score: float | None = None,
) -> RunTestPair:
    def _rate(p: bool | None) -> float | None:
        return None if p is None else (1.0 if p else 0.0)

    return RunTestPair(
        test_id=test_id,
        category="cat",
        model=None,
        a_passed=a_pass,
        b_passed=b_pass,
        a_score=a_score,
        a_pass_rate=_rate(a_pass),
        b_score=b_score,
        b_pass_rate=_rate(b_pass),
        a_dims={},
        b_dims={},
    )


def _classify(pairs):
    from bi_evals.compare.diff import classify_pairs

    return classify_pairs(pairs, CRITICAL)


STEADY = _classify([_pair("t1", a_pass=True, b_pass=True)])
ONE_REGRESSION = _classify(
    [
        _pair("t1", a_pass=True, b_pass=False, a_score=1.0, b_score=0.4),
        _pair("t2", a_pass=True, b_pass=True),
    ]
)
TWO_REGRESSIONS = _classify(
    [
        _pair("t1", a_pass=True, b_pass=False, a_score=1.0, b_score=0.4),
        _pair("t2", a_pass=True, b_pass=False, a_score=1.0, b_score=0.2),
    ]
)
ONE_FIXED = _classify(
    [
        _pair("t1", a_pass=False, b_pass=True, a_score=0.3, b_score=1.0),
        _pair("t2", a_pass=True, b_pass=True),
    ]
)


class TestEvaluateGate:
    def test_clean_diff_passes(self) -> None:
        gate = evaluate_gate(STEADY, suite_pass_rate=1.0)
        assert gate.passed
        assert gate.verdict.value == "green"
        assert gate.regression_count == 0
        assert "no regressions" in gate.reasons

    def test_empty_classified_evaluates_floor_only(self) -> None:
        # First-ever run: no baseline at all.
        gate = evaluate_gate([], suite_pass_rate=0.5, min_pass_rate=0.8)
        assert not gate.passed
        assert any("floor" in r for r in gate.reasons)

    def test_regression_fails_with_zero_budget(self) -> None:
        gate = evaluate_gate(ONE_REGRESSION, suite_pass_rate=0.5)
        assert not gate.passed
        assert gate.regression_count == 1
        assert any("budget 0" in r for r in gate.reasons)

    def test_budget_absorbs_regressions(self) -> None:
        gate = evaluate_gate(
            ONE_REGRESSION, suite_pass_rate=0.5, max_regressions_allowed=1
        )
        assert gate.passed
        assert any("absorbed" in r for r in gate.reasons)

    def test_budget_exceeded_fails(self) -> None:
        gate = evaluate_gate(
            TWO_REGRESSIONS, suite_pass_rate=0.0, max_regressions_allowed=1
        )
        assert not gate.passed

    def test_amber_passes_on_fail_on_red(self) -> None:
        gate = evaluate_gate(ONE_FIXED, suite_pass_rate=1.0, fail_on="red")
        assert gate.verdict.value == "amber"
        assert gate.passed

    def test_amber_fails_on_fail_on_amber(self) -> None:
        gate = evaluate_gate(ONE_FIXED, suite_pass_rate=1.0, fail_on="amber")
        assert not gate.passed
        assert any("amber" in r for r in gate.reasons)

    def test_fail_on_amber_ignores_budget(self) -> None:
        # Strictest mode: even a budget-absorbed regression is a delta.
        gate = evaluate_gate(
            ONE_REGRESSION,
            suite_pass_rate=0.5,
            max_regressions_allowed=5,
            fail_on="amber",
        )
        assert not gate.passed

    def test_floor_breach_fails_even_when_diff_is_clean(self) -> None:
        gate = evaluate_gate(STEADY, suite_pass_rate=0.7, min_pass_rate=0.8)
        assert not gate.passed
        assert any("< floor" in r for r in gate.reasons)

    def test_floor_met_passes(self) -> None:
        gate = evaluate_gate(STEADY, suite_pass_rate=0.9, min_pass_rate=0.8)
        assert gate.passed
        assert any(">= floor" in r for r in gate.reasons)

    def test_floor_unevaluated_when_no_pass_rate(self) -> None:
        gate = evaluate_gate(STEADY, suite_pass_rate=None, min_pass_rate=0.8)
        assert gate.passed
        assert any("not evaluated" in r for r in gate.reasons)

    def test_fail_on_never_is_report_only(self) -> None:
        gate = evaluate_gate(
            TWO_REGRESSIONS, suite_pass_rate=0.0, min_pass_rate=0.9, fail_on="never"
        )
        assert gate.passed
        assert any("report-only" in r for r in gate.reasons)
        # The breaches are still visible in the reasons.
        assert any("regressed" in r for r in gate.reasons)
        assert any("floor" in r for r in gate.reasons)

    def test_gate_result_is_truthy_iff_passed(self) -> None:
        assert evaluate_gate(STEADY, suite_pass_rate=1.0)
        assert not evaluate_gate(ONE_REGRESSION, suite_pass_rate=0.5)

    def test_verdict_unaffected_by_budget(self) -> None:
        # The budget is a gate concept; the report verdict stays red.
        gate = evaluate_gate(
            ONE_REGRESSION, suite_pass_rate=0.5, max_regressions_allowed=1
        )
        assert gate.verdict.value == "red"
        assert gate.passed


@pytest.fixture
def seeded_config(eval_sample_config: BiEvalsConfig) -> BiEvalsConfig:
    """The fixture project's two runs (B has one regression vs A) ingested
    into the tmp store."""
    db_path = eval_sample_config.resolve_path(eval_sample_config.storage.db_path)
    with store_connect(db_path) as conn:
        ingest_run(conn, RUN_A_JSON, eval_sample_config)
        ingest_run(conn, RUN_B_JSON, eval_sample_config)
    return eval_sample_config


def _report(config: BiEvalsConfig, *, pass_rate: float = 0.5) -> RunReport:
    return RunReport(
        run_id=RUN_B_ID,
        total=2,
        passed=1,
        failed=1,
        pass_rate=pass_rate,
        report_path="",
        _config=config,
    )


class TestSdkGate:
    def test_classify_runs_finds_fixture_regression(
        self, seeded_config: BiEvalsConfig
    ) -> None:
        db_path = seeded_config.resolve_path(seeded_config.storage.db_path)
        with store_connect(db_path, read_only=True) as conn:
            compared = classify_runs(conn, RUN_A_ID, RUN_B_ID)
        assert any(c.bucket == "regressed" for c in compared.classified)
        assert compared.diff.run_b.run_id == RUN_B_ID

    def test_passed_gate_true_with_no_floor_configured(
        self, eval_sample_config: BiEvalsConfig
    ) -> None:
        assert _report(eval_sample_config, pass_rate=0.0).passed_gate

    def test_passed_gate_enforces_floor(
        self, eval_sample_config: BiEvalsConfig
    ) -> None:
        eval_sample_config.compare.min_pass_rate = 0.9
        assert not _report(eval_sample_config, pass_rate=0.5).passed_gate
        assert _report(eval_sample_config, pass_rate=0.95).passed_gate

    def test_passed_gate_respects_fail_on_never(
        self, eval_sample_config: BiEvalsConfig
    ) -> None:
        eval_sample_config.compare.min_pass_rate = 0.9
        eval_sample_config.compare.fail_on = "never"
        assert _report(eval_sample_config, pass_rate=0.5).passed_gate

    def test_compare_to_detects_fixture_regression(
        self, seeded_config: BiEvalsConfig
    ) -> None:
        gate = _report(seeded_config).compare_to("prev")
        assert isinstance(gate, GateResult)
        assert not gate.passed
        assert gate.regression_count >= 1

    def test_compare_to_budget_absorbs(self, seeded_config: BiEvalsConfig) -> None:
        seeded_config.compare.max_regressions_allowed = 5
        gate = _report(seeded_config).compare_to("prev")
        assert gate.passed

    def test_compare_to_rejects_self_baseline(
        self, seeded_config: BiEvalsConfig
    ) -> None:
        with pytest.raises(PushScoreError, match="resolves to this run itself"):
            _report(seeded_config).compare_to("latest")

    def test_compare_to_missing_baseline_errors(
        self, eval_sample_config: BiEvalsConfig
    ) -> None:
        # Empty store: nothing to resolve 'prev' against.
        db_path = eval_sample_config.resolve_path(eval_sample_config.storage.db_path)
        with store_connect(db_path):  # create the empty schema
            pass
        with pytest.raises(PushScoreError, match="No baseline run"):
            _report(eval_sample_config).compare_to("prev")

    def test_gate_methods_need_runner_built_report(self) -> None:
        bare = RunReport(
            run_id="x",
            total=1,
            passed=1,
            failed=0,
            pass_rate=1.0,
            report_path="",
        )
        with pytest.raises(PushScoreError, match="without config"):
            bare.passed_gate  # noqa: B018

    def test_config_field_hidden_from_repr(
        self, eval_sample_config: BiEvalsConfig
    ) -> None:
        rep = _report(eval_sample_config)
        assert "_config" not in repr(rep)
        assert any(f.name == "_config" and not f.repr for f in dataclasses.fields(rep))
