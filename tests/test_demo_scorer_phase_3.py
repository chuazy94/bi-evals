"""Phase 3 end-to-end demo: Provider → Scorer pipeline.

=======================================================================
WHAT THIS TESTS
=======================================================================

This demo exercises the full evaluation pipeline built in Phases 1-3:

  1. **Provider** (Phase 2): Sends a question to Claude via the Anthropic
     tool-calling loop. Claude reads skill/knowledge files and generates
     SQL. The provider captures a full trace (tool calls, files read,
     generated SQL, cost).

  2. **Scorer** (Phase 3): Takes the provider's output and scores it
     across the 9-dimension evaluation framework:

     - execution:              Did the SQL run without error?
     - table_alignment:        Did it query the right tables?
     - column_alignment:       Are required columns present?
     - filter_correctness:     Does the WHERE clause match?
     - row_completeness:       Are all expected rows returned?
     - row_precision:          Are there spurious extra rows?
     - value_accuracy:         Are numeric values correct?
     - no_hallucinated_columns: No fabricated columns?
     - skill_path_correctness: Did the agent follow the expected
                                reasoning path (SKILL.md → knowledge file)?

HOW IT WORKS
-----------------------------------------------------------------------
- Uses a REAL Anthropic API call (consumes credits)
- Uses a MOCK database client (no Snowflake credentials needed)
- The mock DB returns pre-defined "expected" results for both the
  reference SQL and the generated SQL, simulating what Snowflake
  would return. This lets us test the full scoring pipeline without
  a live database.

WHAT TO LOOK FOR IN THE OUTPUT
-----------------------------------------------------------------------
- The agent should read SKILL.md first, then ORDERS.md (routing)
- The generated SQL should reference SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS
- The SQL should have a COUNT or SUM aggregation
- Skill path should pass (correct routing order)
- Table alignment should pass (correct table)
- Most dimensions should pass if the agent generates reasonable SQL

REQUIREMENTS
-----------------------------------------------------------------------
- ANTHROPIC_API_KEY environment variable (or in .env file)

USAGE
-----------------------------------------------------------------------
    uv run python -m pytest tests/test_demo_scorer_phase_3.py -v -s
    uv run python -m pytest tests/test_demo_scorer_phase_3.py -v -s -m integration

The -s flag shows print output so you can see the dimension results.

=======================================================================
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bi_evals.db.client import QueryResult
from bi_evals.golden.model import (
    ExpectedResults,
    ExpectedSkillPath,
    GoldenTest,
    RowComparison,
    SkillStep,
)
from bi_evals.provider.agent_loop import AgentResult, run_agent_loop
from bi_evals.scorer.dimensions import (
    check_column_alignment,
    check_execution,
    check_filter_correctness,
    check_no_hallucinated_columns,
    check_row_completeness,
    check_row_precision,
    check_skill_path_correctness,
    check_table_alignment,
    check_value_accuracy,
)
from bi_evals.config import ScoringConfig
from bi_evals.tools.file_reader import FileReaderTool


def _load_env() -> None:
    """Load .env file if present."""
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------------------
# Golden test definition (what we expect the agent to produce)
# ---------------------------------------------------------------------------

GOLDEN_TEST = GoldenTest(
    id="demo-orders-count",
    category="orders",
    difficulty="easy",
    question="How many orders are there in total?",
    expected_skill_path=ExpectedSkillPath(
        required_skills=[
            SkillStep(tool="read_skill_file", input_contains="SKILL.md"),
            SkillStep(tool="read_skill_file", input_contains="ORDERS.md"),
        ],
        sequence_matters=True,
        allow_extra_skills=True,
    ),
    reference_sql=(
        "SELECT COUNT(*) AS ORDER_COUNT "
        "FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS"
    ),
    expected=ExpectedResults(
        min_rows=1,
        required_columns=["ORDER_COUNT"],
        row_comparison=RowComparison(
            enabled=True,
            key_columns=["ORDER_COUNT"],
            value_columns=["ORDER_COUNT"],
            value_tolerance=0.0,
        ),
    ),
    tags=["demo", "orders"],
    notes="Simple count query to verify end-to-end pipeline.",
)

# Mock DB results — what Snowflake would return
MOCK_REFERENCE_RESULT = QueryResult(
    columns=["ORDER_COUNT"],
    rows=[{"ORDER_COUNT": 1500000}],
    row_count=1,
)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestDemoScorerPhase3:
    """End-to-end demo: real Claude API call → score across 9 dimensions."""

    @pytest.fixture(autouse=True)
    def setup_skill_files(self, tmp_path: Path) -> None:
        """Create minimal skill/knowledge files for the agent."""
        self.skill_dir = tmp_path / "skill"
        self.skill_dir.mkdir()

        (self.skill_dir / "SKILL.md").write_text(
            "# Data Warehouse Skill\n\n"
            "## Routing Table\n"
            "| Topic | Knowledge File |\n"
            "|-------|---------------|\n"
            "| Orders, purchases, sales | knowledge/ORDERS.md |\n\n"
            "Always start by reading the relevant knowledge file "
            "before generating SQL.\n"
        )

        knowledge_dir = self.skill_dir / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "ORDERS.md").write_text(
            "# Orders\n\n"
            "## Table\n"
            "`SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS`\n\n"
            "## Columns\n"
            "- O_ORDERKEY (NUMBER) — primary key\n"
            "- O_CUSTKEY (NUMBER) — customer foreign key\n"
            "- O_ORDERSTATUS (VARCHAR) — F, O, or P\n"
            "- O_TOTALPRICE (NUMBER) — order total\n"
            "- O_ORDERDATE (DATE) — when the order was placed\n"
            "- O_ORDERPRIORITY (VARCHAR)\n"
            "- O_CLERK (VARCHAR)\n"
            "- O_SHIPPRIORITY (NUMBER)\n"
            "- O_COMMENT (VARCHAR)\n\n"
            "## Notes\n"
            "- ~1.5M rows in SF1 scale factor\n"
            "- Always use fully qualified table name\n"
        )

    def test_full_pipeline(self) -> None:
        """Run the agent, then score its output across all 9 dimensions."""
        _load_env()

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            pytest.skip("ANTHROPIC_API_KEY not set")

        # ----- Step 1: Run the provider (real API call) -----
        tool = FileReaderTool(
            tool_name="read_skill_file", base_dir=self.skill_dir,
        )

        print("\n" + "=" * 70)
        print("STEP 1: Running agent loop (real Anthropic API call)")
        print("=" * 70)

        result: AgentResult = run_agent_loop(
            question=GOLDEN_TEST.question,
            system_prompt=(
                "You are a BI agent. Answer data questions by generating SQL.\n\n"
                "IMPORTANT: Always start by reading SKILL.md using the "
                "read_skill_file tool. SKILL.md contains a routing table — "
                "follow it to read the relevant knowledge file, then generate "
                "SQL based on the schema.\n\n"
                "Present your final SQL in a ```sql code block."
            ),
            model="claude-sonnet-4-5-20250929",
            tools=[tool],
            tool_definitions=[tool.definition()],
            max_rounds=5,
            api_key=api_key,
        )

        print(f"\nAgent result:")
        print(f"  SQL:        {result.extracted_sql}")
        print(f"  Files read: {result.files_read}")
        print(f"  Rounds:     {result.rounds}")
        print(f"  Cost:       ${result.cost:.4f}")
        print(f"  Trace steps:")
        for step in result.trace:
            if step.type == "tool_use":
                print(f"    [{step.round}] tool: {step.tool_name}({step.tool_input})")
            else:
                print(f"    [{step.round}] text: {(step.text or '')[:80]}...")

        assert result.extracted_sql is not None, "Agent failed to generate SQL"

        # ----- Step 2: Build mock DB results -----
        # Simulate what Snowflake would return for the generated SQL.
        # For a COUNT(*) query, both reference and generated should
        # return the same single row.
        mock_generated_result = QueryResult(
            columns=["ORDER_COUNT"],
            rows=[{"ORDER_COUNT": 1500000}],
            row_count=1,
        )

        # ----- Step 3: Run all 9 dimensions -----
        print("\n" + "=" * 70)
        print("STEP 2: Scoring across 9 dimensions")
        print("=" * 70)

        scoring = ScoringConfig()
        trace_dicts = result.trace_as_dicts()

        dimensions = [
            check_execution(mock_generated_result),
            check_table_alignment(result.extracted_sql, GOLDEN_TEST.reference_sql),
            check_column_alignment(mock_generated_result, GOLDEN_TEST),
            check_filter_correctness(result.extracted_sql, GOLDEN_TEST.reference_sql),
            check_row_completeness(
                mock_generated_result, MOCK_REFERENCE_RESULT, GOLDEN_TEST, scoring,
            ),
            check_row_precision(
                mock_generated_result, MOCK_REFERENCE_RESULT, GOLDEN_TEST, scoring,
            ),
            check_value_accuracy(
                mock_generated_result, MOCK_REFERENCE_RESULT, GOLDEN_TEST, scoring,
            ),
            check_no_hallucinated_columns(
                mock_generated_result, MOCK_REFERENCE_RESULT,
            ),
            check_skill_path_correctness(trace_dicts, GOLDEN_TEST),
        ]

        # ----- Step 4: Print results -----
        print(f"\n{'Dimension':<30} {'Pass':>6}  Reason")
        print("-" * 80)

        passed_count = 0
        for d in dimensions:
            icon = "PASS" if d.passed else "FAIL"
            print(f"  {d.name:<28} {icon:>6}  {d.reason}")
            if d.passed:
                passed_count += 1

        total = len(dimensions)
        print("-" * 80)
        print(f"  {'TOTAL':<28} {passed_count}/{total}")
        print(f"\n  Overall score: {passed_count/total:.0%}")
        print("=" * 70)

        # ----- Assertions -----
        # The agent should at minimum:
        # 1. Generate valid SQL (execution passes because we mock success)
        assert dimensions[0].passed, "execution dimension should pass (mocked)"

        # 2. Reference the correct table
        assert dimensions[1].passed, (
            f"table_alignment should pass — agent SQL: {result.extracted_sql}"
        )

        # 3. Follow the correct skill path (SKILL.md → ORDERS.md)
        assert dimensions[8].passed, (
            f"skill_path should pass — trace: {result.files_read}"
        )

        # Report overall — don't fail hard on dimensions that depend on
        # exact SQL structure (column names, filters) since the agent
        # has some freedom in how it writes the query
        print(f"\n  {passed_count}/{total} dimensions passed")
