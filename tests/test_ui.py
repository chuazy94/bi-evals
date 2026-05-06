"""Smoke tests for the FastAPI viewer in ``bi_evals.ui``."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bi_evals.config import BiEvalsConfig
from bi_evals.store import connect as store_connect
from bi_evals.store.ingest import ingest_run
from bi_evals.ui import create_app

from tests.conftest import RUN_A_ID, RUN_A_JSON, RUN_B_ID, RUN_B_JSON


def _seed(config: BiEvalsConfig) -> None:
    db_path = config.resolve_path(config.storage.db_path)
    with store_connect(db_path) as conn:
        ingest_run(conn, RUN_A_JSON, config)
        ingest_run(conn, RUN_B_JSON, config)


@pytest.fixture
def client(eval_sample_config: BiEvalsConfig) -> TestClient:
    _seed(eval_sample_config)
    app = create_app(eval_sample_config)
    return TestClient(app)


@pytest.fixture
def empty_client(eval_sample_config: BiEvalsConfig) -> TestClient:
    """No DB yet — exercises the empty state."""
    app = create_app(eval_sample_config)
    return TestClient(app)


def test_runs_list_shows_seeded_runs(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    body = res.text
    assert RUN_A_ID in body
    assert RUN_B_ID in body
    assert 'http-equiv="refresh"' in body
    assert "Compare selected" in body


def test_runs_list_empty_state(empty_client: TestClient) -> None:
    res = empty_client.get("/")
    assert res.status_code == 200
    assert "No runs yet" in res.text
    assert "bi-evals run" in res.text


def test_single_run_view_renders(client: TestClient) -> None:
    res = client.get(f"/runs/{RUN_B_ID}")
    assert res.status_code == 200
    body = res.text
    assert "<html" in body
    assert RUN_B_ID in body


def test_single_run_unknown_id_returns_404(client: TestClient) -> None:
    res = client.get("/runs/does-not-exist")
    assert res.status_code == 404
    assert "Not found" in res.text
    assert 'href="/"' in res.text


def test_compare_view_renders(client: TestClient) -> None:
    res = client.get(f"/compare?a={RUN_A_ID}&b={RUN_B_ID}")
    assert res.status_code == 200
    # Compare template emits a verdict block; the fixture pair is a known regression.
    assert "verdict" in res.text


def test_compare_unknown_run_redirects_with_error(client: TestClient) -> None:
    # follow_redirects=False so we can assert the 303 + Location.
    res = client.get(
        f"/compare?a=missing&b={RUN_B_ID}",
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/?error=")


def test_compare_selected_two_runs_redirects(client: TestClient) -> None:
    res = client.post(
        "/compare-selected",
        data={"run_ids": [RUN_A_ID, RUN_B_ID]},
        follow_redirects=False,
    )
    assert res.status_code == 303
    loc = res.headers["location"]
    assert loc == f"/compare?a={RUN_A_ID}&b={RUN_B_ID}"


def test_compare_selected_wrong_count_redirects_with_error(client: TestClient) -> None:
    res = client.post(
        "/compare-selected",
        data={"run_ids": [RUN_A_ID]},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "error=" in res.headers["location"]
    assert "exactly%202" in res.headers["location"]


# --- Phase 7.5 tests --------------------------------------------------------


FAILING_TEST_ID = "golden/cases/daily-cases-filtered.yaml"
PASSING_TEST_ID = "golden/cases/total-cases-by-country.yaml"


def test_run_view_shows_failures_section(client: TestClient) -> None:
    res = client.get(f"/runs/{RUN_B_ID}")
    assert res.status_code == 200
    body = res.text
    assert "Failures" in body
    assert "daily-cases-filtered" in body


def test_run_view_filter_by_category(client: TestClient) -> None:
    res = client.get(f"/runs/{RUN_B_ID}?category=cases")
    assert res.status_code == 200
    body = res.text
    # Active filter is reflected in the dropdown
    assert 'value="cases" selected' in body or "cases\" selected" in body
    # Drilldown links only refer to /cases/ tests in the body (filtered table)
    import re
    test_id_rows = re.findall(r'href="/runs/[^"]+/tests/([^"?]+)', body)
    assert test_id_rows
    for tid in test_id_rows:
        assert "/cases/" in tid


def test_test_drilldown_renders(client: TestClient) -> None:
    from urllib.parse import quote
    res = client.get(f"/runs/{RUN_B_ID}/tests/{quote(PASSING_TEST_ID, safe='')}")
    assert res.status_code == 200
    body = res.text
    assert PASSING_TEST_ID in body
    assert "Generated SQL" in body
    assert "Dimensions" in body


def test_test_drilldown_shows_fail_reason(client: TestClient) -> None:
    from urllib.parse import quote
    res = client.get(f"/runs/{RUN_B_ID}/tests/{quote(FAILING_TEST_ID, safe='')}")
    assert res.status_code == 200
    body = res.text
    assert "Failure summary" in body or "fail" in body.lower()
    # Some dimension reason text should be present (not just "—")
    assert "row_completeness" in body or "value_accuracy" in body


def test_test_drilldown_unknown_returns_404(client: TestClient) -> None:
    res = client.get(f"/runs/{RUN_B_ID}/tests/does-not-exist")
    assert res.status_code == 404


def test_runs_list_project_filter(client: TestClient, eval_sample_config: BiEvalsConfig) -> None:
    res = client.get(f"/?project={eval_sample_config.project.name}")
    assert res.status_code == 200
    assert RUN_B_ID in res.text

    miss = client.get("/?project=does-not-exist")
    assert miss.status_code == 200
    assert RUN_B_ID not in miss.text


def test_run_view_renders_scoring_rule_callout(client: TestClient) -> None:
    res = client.get(f"/runs/{RUN_B_ID}")
    assert res.status_code == 200
    body = res.text
    assert "Scoring rule" in body
    # Default pass_threshold from config is 0.75; it should appear in the
    # callout AND in the weighted-score column headers.
    assert "0.75" in body
    assert "Weighted score" in body
    # Critical dims listed in the callout
    for dim in ("execution", "row_completeness", "value_accuracy"):
        assert dim in body


def test_test_drilldown_pass_verdict(client: TestClient) -> None:
    from urllib.parse import quote
    res = client.get(f"/runs/{RUN_B_ID}/tests/{quote(PASSING_TEST_ID, safe='')}")
    assert res.status_code == 200
    body = res.text
    assert "Passed:" in body
    assert "all critical dimensions green" in body
    # Threshold appears in the verdict sentence and in the score stat label.
    assert "0.75" in body
    assert "Weighted score" in body


def test_test_drilldown_fail_verdict(client: TestClient) -> None:
    from urllib.parse import quote
    res = client.get(f"/runs/{RUN_B_ID}/tests/{quote(FAILING_TEST_ID, safe='')}")
    assert res.status_code == 200
    body = res.text
    assert "Failed:" in body
    # The fail path should mention either a specific failed critical dim or
    # the weighted-score-below-threshold case. Either is acceptable.
    assert ("critical dimension" in body) or ("below threshold" in body)


def test_runs_list_refresh_preserves_project_filter(client: TestClient) -> None:
    res = client.get("/?project=Foo")
    assert res.status_code == 200
    # The meta-refresh tag should encode the project so the 10s reload doesn't drop it.
    # Format: content="10;url=/?project=Foo"
    assert ";url=/?project=" in res.text or "?project=Foo" in res.text
