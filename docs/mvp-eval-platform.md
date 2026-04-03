# Design: BI Agent Eval Platform MVP

**Date**: March 2026 | **Status**: Draft

---

## Problem

We have a data warehouse skill with 15+ knowledge files covering 150+ Snowflake objects, MCP integrations with HubSpot, Gong, Slack, and Jira, and a growing user base across the company. But we can't answer the question that matters: **"How much should I trust this answer?"**

The existing eval suite (40 Promptfoo test cases across 3 levels) validates that SQL contains the right structural elements and executes without errors. This is necessary but not sufficient. A query can use the right table, execute cleanly, and still return the wrong data. And even correct data can be misinterpreted when the agent summarises it in natural language.

We need an eval platform that provides **provable, measurable, communicable reliability** — evidence that non-technical stakeholders can act on.

---

## Vision: Three Pillars of BI Agent Evaluation

The platform is organised around three pillars that mirror how a BI agent actually works:

- **Accuracy** — Did the agent get the right data?
- **Faithfulness** — Did it tell the truth about that data?
- **Confidence** — How reliably can we expect this to be right?

Each pillar builds on the one before it. Faithfulness is meaningless without accuracy. Confidence is meaningless without faithfulness. The MVP focuses on **Pillar 1 (Accuracy)** with explainability woven through, laying the foundation for Pillars 2 and 3.

For full detail on the three pillars framework, see: [The Three Pillars of BI Agent Evaluation](./THREE_PILLARS.md)

---

## Context: How the industry builds AI BI

Every major data platform (Snowflake Cortex Analyst, Databricks Genie, Looker + Gemini, Microsoft Fabric Copilot) has converged on the same architecture: **semantic model → multi-agent LLM pipeline → warehouse-executed SQL → governance guardrails**. The critical finding across all of them: raw text-to-SQL achieves ~17% accuracy on enterprise schemas; adding a semantic layer pushes this to 83–100%.

Our knowledge files are functionally equivalent to a lightweight semantic layer encoded in natural language. This trades deterministic SQL compilation for flexibility and faster iteration — a defensible tradeoff for a focused internal use case, but one that makes rigorous evaluation even more important. MCP is now the standard integration protocol (adopted by OpenAI, Google, Databricks, and donated to the Linux Foundation), so our multi-source architecture is well-aligned with industry direction.

The key gap relative to industry practice: Snowflake and Databricks both center their accuracy strategy on **verified query repositories** — curated question-SQL-result triples that grow from production usage. Our golden dataset serves the same purpose but doesn't yet validate result correctness. Closing this gap is the highest-impact improvement available.

---

## Architecture decision: Promptfoo as engine, platform on top

The existing Promptfoo-based eval suite was selected in February 2026 (see [SKILL_EVALS_PROPOSAL.md](./SKILL_EVALS_PROPOSAL.md)) for its YAML-first simplicity, comprehensive assertion types, and zero vendor lock-in. That choice remains sound. What's changed is that we now need capabilities Promptfoo doesn't provide: result persistence, regression comparison, explainability traces, custom reporting, and confidence scoring.

**The recommendation: keep Promptfoo as the test runner, build the platform layer on top.**

```
┌─────────────────────────────────────────────────┐
│         Eval Platform (what we build)            │
│                                                  │
│  • Golden dataset management                     │
│  • Accuracy scorer (result set comparison)       │
│  • Explainability trace capture                  │
│  • Result persistence + regression tracking      │
│  • Report generation (HTML, shareable)           │
│  • Cost tracking + budget visibility             │
│  • [Future] Faithfulness scoring (Pillar 2)      │
│  • [Future] Confidence scoring (Pillar 3)        │
└──────────────────────┬──────────────────────────┘
                       │ reads JSON output
┌──────────────────────┴──────────────────────────┐
│         Promptfoo (test runner engine)            │
│                                                  │
│  • Sends questions to models via custom provider │
│  • Runs assertions (contains, JS, llm-rubric)    │
│  • Manages tool-calling loops                    │
│  • Handles caching, retries, parallelisation     │
│  • Outputs structured JSON results               │
└─────────────────────────────────────────────────┘
```

**Why not switch to Braintrust?** It would give us regression detection and PR comments, but we'd lose the custom tool-calling provider, the YAML test format, and take on vendor dependency ($249/mo after free tier). The actual gaps we're filling (explainability, BI-specific accuracy scoring, confidence scoring) aren't things Braintrust provides out of the box either.

**Why not build from scratch?** We'd be reimplementing the test runner loop that Promptfoo already does well — sending prompts, collecting responses, running assertions, caching, parallelisation. That's commodity infrastructure. Our differentiation is the BI-specific evaluation layer on top.

---

## MVP scope: Accuracy + Explainability

The MVP answers: **"When the genie generates SQL for this type of question, is the answer correct — and can we prove why?"**

Every eval result produces three things:
1. A **verdict** (correct / partially correct / wrong) with a continuous 0.0–1.0 score
2. An **explanation** (which knowledge files were read, how the agent reasoned, what it decided)
3. A **comparison** (AI result set vs expected result set)

### What we're building

#### 1. Enhanced Provider (capture the reasoning trace)

The existing `anthropic-with-tools.js` already performs the full reasoning trace — Claude reads SKILL.md, routes to a knowledge file, reads it, generates SQL. It just throws this trace away and returns only the final text. The enhanced provider captures everything:

```javascript
// Current return:
{ output: "Here's the SQL...", tokenUsage: { prompt: 1200, completion: 800 } }

// Enhanced return:
{
  output: "Here's the SQL...",
  tokenUsage: { prompt: 1200, completion: 800, total: 2000 },
  cost: 0.042,
  metadata: {
    trace: [
      { round: 1, tool: "read_skill_file", input: "skill/SKILL.md" },
      { round: 2, tool: "read_skill_file", input: "skill/knowledge/REVENUE.md" },
      { round: 3, type: "text", summary: "Generated SQL using V_UNIFIED_REVENUE" }
    ],
    files_read: ["SKILL.md", "knowledge/REVENUE.md"],
    model: "claude-sonnet-4-5-20250929",
    rounds: 3
  }
}
```

The cost field uses a simple pricing map:

```javascript
const PRICING = {
  'claude-sonnet-4-5-20250929': { input: 3.0 / 1e6, output: 15.0 / 1e6 },
  'claude-opus-4-6':            { input: 15.0 / 1e6, output: 75.0 / 1e6 },
};
```

This is ~30 lines of changes to the existing provider. No new infrastructure. The `metadata` field is standard Promptfoo — it gets persisted alongside results and is queryable.

#### 2. Golden Dataset (the source of truth)

A curated set of question → expected skill path → reference SQL → expected results tuples, organised by category. This is the ground truth against which everything is measured. Each golden test captures not just the expected output but the expected reasoning path — which skills/tools the agent should invoke and in what order.

```yaml
# golden-tests/revenue/enterprise-revenue-trend.yaml
id: revenue-001
category: revenue
difficulty: medium
question: "Show me the last 6 months of revenue for account 48821"
expected_skill_path:
  required_skills:
    - skill: read_skill_file
      input_contains: "SKILL.md"
      purpose: "Route to correct knowledge domain"
    - skill: read_skill_file
      input_contains: "REVENUE.md"
      purpose: "Load revenue domain knowledge including V_UNIFIED_REVENUE gotcha"
  sequence_matters: true  # skills must be invoked in this order
  allow_extra_skills: true  # agent may read additional files without penalty
reference_sql: |
  SELECT ABLY_ACCOUNT_ID, DT_INVOICE_MONTH, INVOICE_VALUE, INVOICE_SOURCE
  FROM ABLY_ANALYTICS_DEV_MATTO.DEEP_DIVE_POC.V_UNIFIED_REVENUE
  WHERE ABLY_ACCOUNT_ID = 48821
    AND DT_INVOICE_MONTH >= DATEADD(MONTH, -6, DATE_TRUNC('MONTH', CURRENT_DATE()))
  ORDER BY DT_INVOICE_MONTH DESC
expected:
  min_rows: 1
  required_columns: [ABLY_ACCOUNT_ID, DT_INVOICE_MONTH, INVOICE_VALUE]
  checks:
    - column: ABLY_ACCOUNT_ID
      every_row: 48821
    - column: INVOICE_VALUE
      type: positive_number
tags: [v_unified_revenue, deep_dive_poc, enterprise]
notes: "Must use V_UNIFIED_REVENUE, not ACCOUNT_INVOICES (broken)"
```

The `expected_skill_path` field captures the reasoning path, not just the output. `sequence_matters` controls whether ordering is evaluated. `allow_extra_skills` prevents penalising the agent for reading additional context (e.g., checking a related knowledge file for cross-references). When `expected_skill_path` is omitted, the skill path dimension is simply skipped for that test — not every golden test needs to validate routing.

**How it grows:** start with 20–30 cases across 6 categories (revenue, usage, adoption, accounts, signals, cross-domain). Source from: the existing 40 tests (upgrade the best ones), real questions people have asked the genie, and known failure cases. Every production failure becomes a new golden test — this is the Braintrust "flywheel" without the Braintrust dependency.

#### 3. Accuracy Scorer (compare results, not just SQL)

A new `scorers/accuracy-scorer.js` that runs both the AI's SQL and the golden reference SQL against Snowflake, then compares result sets.

**Multi-dimensional binary scoring:**

Each test is evaluated across independent binary dimensions. Each dimension answers a yes/no question. The overall accuracy score is the fraction of applicable dimensions that passed — objective, transparent, and diagnostic.

| Dimension | Question | Binary check |
|-----------|----------|--------------|
| **Execution** | Did the SQL run? | No Snowflake errors |
| **Table alignment** | Correct source tables? | AI tables ⊇ reference tables |
| **Column alignment** | Required columns present? | AI columns ⊇ required columns (handles aliasing) |
| **Filter correctness** | Right rows only? | All returned rows satisfy golden test constraints |
| **Row completeness** | All expected rows returned? | ≥95% of reference rows present in AI results |
| **Row precision** | No junk rows? | ≥95% of AI rows match reference rows |
| **Value accuracy** | Numbers correct? | All matched values within tolerance (configurable, default 0.01%) |
| **No hallucinated columns** | No fabricated columns? | AI doesn't return columns absent from reference/schema |
| **Skill path correctness** | Right reasoning path? | Agent invoked expected skills in expected sequence (when `expected_skill_path` is defined) |

A test scoring 7/9 is immediately diagnostic: the two failed dimensions tell you *what kind* of failure occurred. Table alignment failure = routing problem. Filter correctness failure = knowledge file problem. Skill path failure = the agent got the right answer for the wrong reasons (fragile, will break on schema changes).

For productisation, dimensions are configurable: organisations choose which to include, set their own thresholds (row completeness %, value tolerance), and optionally weight them.

**Comparison method (per dimension):**
1. SQL parsing — extract tables and columns from AI SQL for structural dimensions (table/column alignment)
2. Result set execution — run both AI and reference SQL against Snowflake
3. Row matching — set-based comparison with configurable tolerance; "top N" queries with ties use set semantics, ignoring row order
4. Constraint validation — check golden test constraints (every_row, type, range) against AI results
5. Trace matching — compare captured skill invocation trace against `expected_skill_path` when defined

#### 4. The Eval Report (the artifact people actually look at)

A generated single-file HTML report. Open it in a browser, share it on Slack, attach it to a PR. Two views:

**Per-test explainability card** — shows the reasoning trace, result comparison, and diagnosis:

```
┌─────────────────────────────────────────────────────────┐
│ revenue-001: Enterprise revenue trend          ✅ 9/9   │
├─────────────────────────────────────────────────────────┤
│ Question: "Show me the last 6 months of revenue         │
│            for account 48821"                           │
│                                                         │
│ Skill path (expected → actual):                         │
│   ✅ SKILL.md → REVENUE.md → generate SQL               │
│   Key decision: Used V_UNIFIED_REVENUE (not broken      │
│   ACCOUNT_INVOICES) — correctly following REVENUE.md    │
│   gotcha #0.                                            │
│                                                         │
│ Dimensions:                                             │
│   ✅ Execution    ✅ Table align   ✅ Column align       │
│   ✅ Filters      ✅ Completeness  ✅ Precision          │
│   ✅ Values       ✅ No halluc.    ✅ Skill path         │
│                                                         │
│ Result comparison:                                      │
│   AI returned 6 rows, reference returned 6 rows         │
│                                                         │
│ Cost: $0.042 (Sonnet 4.5) | Tokens: 2,000              │
└─────────────────────────────────────────────────────────┘
```

For failures, the failed dimensions narrow diagnosis immediately:
- **Skill path** failed → agent skipped a knowledge file or took wrong route → fix SKILL.md routing table
- **Table alignment** failed → wrong source table → fix routing or knowledge file
- **Filter correctness** failed → right table, wrong WHERE clause → fix knowledge file
- **Value accuracy** failed → right rows, wrong numbers → check aggregation logic or data types
- **Row completeness** failed → missing rows → check filters are not too restrictive

**Category summary dashboard** — the view a PM can read:

```
┌──────────────────────────────────────────────────────────────┐
│ Eval Run: 2026-03-27 | Models: Sonnet 4.5, Opus 4.6         │
│ Total: 30 tests | 9 dimensions per test | Cost: $4.23       │
├────────────────┬──────────┬──────────┬───────────────────────┤
│ Category       │ Pass Rate│ Avg Dims │ Status                │
├────────────────┼──────────┼──────────┼───────────────────────┤
│ Revenue        │ 6/6 100% │ 8.8/9    │ 🟢 High confidence    │
│ Usage          │ 5/6  83% │ 7.3/9    │ 🟡 Review needed      │
│ Adoption       │ 4/6  67% │ 6.5/9    │ 🟡 Review needed      │
│ Accounts       │ 5/5 100% │ 8.6/9    │ 🟢 High confidence    │
│ Signals        │ 2/4  50% │ 5.0/9    │ 🔴 Low confidence     │
│ Cross-domain   │ 2/3  67% │ 6.2/9    │ 🟡 Review needed      │
├────────────────┼──────────┼──────────┼───────────────────────┤
│ Overall        │ 24/30 80%│ 7.2/9    │                       │
└────────────────┴──────────┴──────────┴───────────────────────┘

Weakest dimensions across all tests:
  Skill path correctness: 73% (8 failures — agents skipping knowledge files)
  Filter correctness:     80% (6 failures — mostly temporal filter issues)
  Row completeness:       87% (4 failures — edge cases with NULL handling)

Cost breakdown:
  Inference (Sonnet): $1.82 | Inference (Opus): $2.11
  Snowflake queries:  34 executed, 2 failed
  Grading (llm-rubric): $0.30
```

#### 5. Result Persistence + Regression Detection

Results are persisted as timestamped JSON files (committed to the repo, not gitignored). A comparison script loads two result files and shows what changed: new passes, new failures, score changes by category.

This is the foundation for Pillar 3 (Confidence). Once results are persisted, you can compute:
- Accuracy trends over time per category
- Regression detection on PR (did this change make anything worse?)
- Graduated test tracking (which tests have reached "regression gate" status)

**PR merge policy for regression:**
- **No regressions** = green, merge freely
- **Regressions with improvements** = yellow, reviewer decides
- **Net regressions** = red, must be fixed before merge

---

## How explainability works

Explainability isn't a separate feature — it falls out naturally from capturing the agent's reasoning trace. The enhanced provider records three things at each round of the tool-calling loop:

1. **What file was read** — the `path` argument to `read_skill_file`
2. **What the model said** — text blocks between tool calls (where reasoning lives)
3. **What SQL was generated** — extracted from the final response

From these signals, the report reconstructs the decision chain:

- **Routing decision:** "Read SKILL.md, decided this is a revenue question, routed to REVENUE.md" → validates the routing table
- **Knowledge application:** "Read REVENUE.md, found gotcha about ACCOUNT_INVOICES, used V_UNIFIED_REVENUE" → validates knowledge files are being applied
- **SQL generation:** "Generated SQL using DEEP_DIVE_POC schema with ABLY_ACCOUNT_ID filter" → validates SQL follows knowledge file patterns

This works identically whether the business logic comes from knowledge files (our current approach) or a formal semantic model (Snowflake Semantic Views, dbt Semantic Layer). The explainability layer is source-agnostic — it captures *what the agent decided and why*, regardless of how the context was provided.

---

## Implementation plan

### Phase 1: Foundation (1–2 days)

**Enhance the provider.** Add to `anthropic-with-tools.js`:
- Capture the trace array (files read, model reasoning at each step)
- Add cost calculation (pricing map + multiplication)
- Return both in standard Promptfoo `metadata` and `cost` fields
- ~30 lines of changes to an existing file

**Persist results.** Update `run.sh` to output JSON with `--output results/run-$(date +%Y%m%d-%H%M%S).json`. Remove the gitignore on results.

**Wire up expectedValues.** `setup/index.js` already captures stable facts. `snowflake-sdk-runner.js` already supports `expectedValues`. Pick 3–5 L3 tests and connect them. Zero new code — just configuration.

### Phase 2: Golden Dataset + Accuracy Scorer (3–5 days)

**Create golden dataset format.** Design the YAML schema. Write 20–30 golden tests across 6 categories, sourced from existing tests, real user questions, and known failures.

**Build the accuracy scorer.** New `scorers/accuracy-scorer.js` that extracts AI SQL, runs both AI and reference SQL against Snowflake, compares result sets, returns continuous 0.0–1.0 score with breakdown.

**Integrate with Promptfoo.** Golden tests become new entries in the config with the accuracy scorer as an assertion. They coexist with existing L1/L2/L3 tests.

### Phase 3: Eval Report (2–3 days)

**Build HTML report generator.** A Node.js script that reads Promptfoo's JSON output and generates a single-file HTML report with: summary dashboard, per-test explainability cards, sortable/filterable table, cost breakdown. The trace data from Phase 1 and accuracy scores from Phase 2 provide all content.

### Phase 4: CI/CD + Regression (1–2 days)

**GitHub Action for PR gating.** L1/L2 tests on every PR touching skill or knowledge files. Uses Promptfoo's GitHub Action. ~$2–3 per PR, 2–3 minutes.

**Nightly full suite.** Scheduled GitHub Action running L3 + golden dataset. Posts summary to Slack.

**Regression comparison script.** Loads two result JSON files, shows what changed. This is the before/after evidence for knowledge file changes.

### Total estimated effort: ~2 weeks

---

## What this gives us

| Capability | What it means |
|-----------|---------------|
| **Multi-dimensional accuracy scoring** | Not just "does the SQL run" but 9 binary dimensions telling you exactly what went right and wrong |
| **Skill path validation** | For every test, verify the agent followed the expected reasoning path — not just that it got the right answer |
| **Explainability traces** | For every test, see which files were read and why |
| **Diagnostic fingerprinting** | Failed dimensions tell you the failure type: routing, knowledge, filter, precision — no manual triage needed |
| **Cost visibility** | What each eval run costs, broken down by model and level |
| **Category confidence** | "Revenue questions: 8.8/9 avg dimensions. Signals questions: 5.0/9." |
| **Regression detection** | Know immediately when a change makes something worse |
| **Shareable evidence** | An HTML report anyone can read, not just developers |

---

## What this does NOT include (and why)

**No web application.** The MVP is CLI runner + generated HTML report. A live dashboard is Pillar 3 territory.

**No faithfulness scoring (Pillar 2).** Testing whether the agent's natural language interpretation matches the data requires a separate LLM-as-judge layer. The infrastructure for this (trace capture, Snowflake results) is built in Phases 1–2, but the scoring logic is a separate effort.

**No multi-trial runs.** Running each test 3–5 times for pass@k/pass^k reliability scoring is important but expensive. Add after we have cost visibility and can budget for it.

**No composite confidence scoring (Pillar 3).** The regression tracking, multi-trial metrics, self-consistency checks, and trust dashboard are the full Confidence pillar. The MVP builds the data foundation (persisted results, cost tracking) that makes Pillar 3 possible.

**No semantic layer integration.** Knowledge files are the semantic layer for now. If schema complexity grows significantly, Snowflake Semantic Views become the formal layer with knowledge files providing context enrichment on top.

---

## Future: Pillars 2 and 3

The MVP builds the foundation for the complete framework:

**Pillar 2 (Faithfulness)** adds a scoring layer that captures the agent's natural language response alongside SQL results, decomposes it into atomic claims ("revenue grew 15%", "driven by enterprise expansion"), and verifies each claim against the actual data. Infrastructure needed: the trace capture and Snowflake result sets from Phase 1–2 are prerequisites. New work: a faithfulness scorer using LLM-as-judge (RAGAS Faithfulness or DeepEval G-Eval).

**Pillar 3 (Confidence)** wraps everything in a reliability system. It adds multi-trial runs (pass@k / pass^k), a composite reliability score per question category (combining eval pass rate, self-consistency, coverage depth, and regression trend), the graduation model (capability evals that reach sustained high pass rates become regression gates), and a trust dashboard for non-technical stakeholders. Infrastructure needed: persisted results and cost tracking from Phase 1 are prerequisites.

The three pillars form a maturity model:

| Stage | What you can say | What it requires |
|-------|------------------|------------------|
| **Accuracy only** (MVP) | "The SQL is correct and the agent reasoned correctly" | Golden dataset + multi-dimensional scoring + skill path validation |
| **+ Faithfulness** | "The answer is correct" | Claim verification + hallucination detection |
| **+ Confidence** | "The answer is reliable" | Multi-trial scoring + regression tracking + trust dashboard |

Each stage is independently valuable and each builds on the last. The end state is something no other internal BI tool has: provable, measurable, communicable reliability.

---

## Success criteria

| Metric | Target | Notes |
|--------|--------|-------|
| Golden dataset size | 30+ tests across 6 categories | Growing from production failures |
| L1 pass rate (routing) | >95% | Agent references correct tables |
| L2 pass rate (SQL correctness) | >85% | SQL uses correct columns/syntax |
| Avg dimensions passed (golden tests) | >7/9 | Multi-dimensional binary scoring |
| Skill path pass rate | >80% | Agent follows expected reasoning path |
| Regression detection | <24h to detect | Via nightly run + Slack alerts |
| Cost per full run | <$10 | Across both models + Snowflake |
| Report generation | Single HTML file, <5s | Shareable without infrastructure |

---

## References

- [Three Pillars of BI Agent Evaluation](./THREE_PILLARS.md) — full framework description
- [SKILL_EVALS_PROPOSAL.md](./SKILL_EVALS_PROPOSAL.md) — original Promptfoo selection rationale
- [Anthropic: Demystifying Evals for AI Agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [Braintrust: Three Pillars of AI Observability](https://www.braintrust.dev/blog/three-pillars-ai-observability)
- [Snowflake: Cortex Analyst Behind the Scenes](https://www.snowflake.com/en/engineering-blog/snowflake-cortex-analyst-behind-the-scenes/)
- [Spider 2.0: Enterprise Text-to-SQL Evaluation](https://arxiv.org/abs/2411.07763)
