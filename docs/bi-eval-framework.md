# The three pillars of BI agent evaluation

When someone asks a BI agent a question — "What's our enterprise revenue trend?" — three things happen in sequence. The agent finds data, interprets that data, and presents an answer. Each step can fail independently. A correct SQL query can return wrong data if it hits the wrong table. Correct data can be misrepresented if the agent says "179 million" when the number is 179 thousand. And even a perfectly accurate answer is useless if nobody trusts it enough to act on it.

This gives us three pillars:

- **Accuracy** — Did the agent get the right data?
- **Faithfulness** — Did it tell the truth about that data?
- **Confidence** — How reliably can we expect this to be right?

Each pillar builds on the one before it. Faithfulness is meaningless without accuracy. Confidence is meaningless without faithfulness. Together, they form a complete evaluation framework that lets a team go from "I think this AI might be useful" to "I trust this answer enough to put it in a board deck."

---

## Pillar 1: Accuracy

*Did the agent find the right data?*

This is the foundation. Before we care about anything else, we need to know: did the SQL query hit the correct tables, use the right columns, apply the right filters, and return the right rows?

Today, the data warehouse evals test two things: **does the SQL contain the right structural elements** (L1/L2 checks for table names, column names, ID patterns) and **does the SQL execute without errors** (L3 Snowflake execution). These are necessary but not sufficient. A query can use the right table, execute cleanly, and still return the wrong data.

### What accuracy evals should actually test

**Result correctness, not just executability.** The gold standard in text-to-SQL evaluation is execution accuracy: run the AI's SQL and a known-good reference SQL against the same database, compare the result sets. If they match, the query is correct — regardless of whether the SQL syntax looks different. This is already partially built: `setup/index.js` captures stable facts from November 2025 immutable data, and `snowflake-sdk-runner.js` supports an `expectedValues` config. But no L3 test actually uses `expectedValues` yet. Activating this is the single highest-impact improvement available.

**Multi-dimensional binary scoring.** A single pass/fail verdict for an entire query is too coarse — it treats a query that hits the right table but applies the wrong filter identically to one that returns complete garbage. But subjective continuous rubrics (0.8 = "mostly right") introduce calibration problems and don't generalise across organisations.

The solution: evaluate accuracy across multiple independent binary dimensions, each answering a clear yes/no question. The overall accuracy score is the fraction of dimensions that passed — objective, transparent, and diagnostic.

The core accuracy dimensions:

- **Execution** — Did the SQL execute without errors?
- **Table alignment** — Did the AI query the correct source table(s)?
- **Column alignment** — Did the AI select the required columns? (handles aliasing)
- **Filter correctness** — Do all returned rows satisfy the constraints defined in the golden test?
- **Row completeness (recall)** — Did the AI return ≥95% of the expected rows?
- **Row precision** — Is ≥95% of what the AI returned actually correct?
- **Value accuracy** — For matching rows, are numeric values correct within tolerance? (configurable epsilon, e.g. 0.01% for financial data)
- **No hallucinated columns** — Did the AI avoid returning columns that don't exist in the reference or underlying schema?
- **Skill path correctness** — Did the agent invoke the expected skills/tools in the expected sequence? (see "Skill sequence validation" below)

Each dimension is independently pass/fail. A test scoring 7/9 tells you more than a test scoring "0.78" — you can see *which* dimensions failed, which gives you a diagnostic fingerprint: table alignment failure = routing problem, filter correctness failure = knowledge file problem, value accuracy failure = precision/rounding issue. Failure categorisation falls out of the dimension vector for free.

For productisation, the dimensions become configurable: organisations choose which to include, set their own thresholds for the ones that need them (row completeness %, value tolerance), and optionally weight them. The defaults work out of the box for most BI use cases.

**Edge cases that reflect real usage.** The current 40 tests cover the critical happy paths well. What's missing: ambiguous queries ("show me revenue" — gross? net? which account?), temporal reasoning ("same month last year", "trailing 12 months"), NULL handling and empty result sets, multi-step queries requiring CTEs or subqueries, and graceful failure when data doesn't exist. These aren't exotic — they're the questions people actually ask.

**Adversarial robustness.** The agent runs SQL against production data. It must reject prompt injection attempts, never generate DDL/DML, and enforce read-only access patterns. Promptfoo's built-in `sql-injection` and `rbac` red team plugins can generate these tests automatically.

### What accuracy looks like in practice

A mature accuracy eval suite has three tiers:

1. **Golden dataset** (50–100 curated question → skill path → SQL → expected result tuples, covering every knowledge domain). These are the ground truth. They grow from production failures — every time a user reports a wrong answer, it becomes a new test case. Each golden test specifies not just the expected SQL and results, but the expected skill/tool invocation sequence — which knowledge files should be read, in what order, and what routing decisions should be made. This validates the agent's reasoning path, not just its output, catching "right answer for the wrong reasons" failures that result-only evaluation misses.

2. **Structural checks** (the existing L1/L2 tests). These are fast, cheap, and catch the most common mistakes — wrong table, wrong ID column, broken table references.

3. **Execution checks with result validation** (enhanced L3). The SQL runs against Snowflake and results are compared against expected values. Non-deterministic queries (e.g., "top 10 accounts" with ties) use set comparison rather than ordered comparison.

---

## Pillar 2: Faithfulness

*Did the agent tell the truth about the data?*

This is the gap between "correct data" and "correct answer." The agent doesn't just return SQL results — it interprets them. It writes sentences like "Revenue grew 15% quarter-over-quarter, driven primarily by enterprise expansion." Every word in that sentence is a claim that can be wrong, even when the underlying data is right.

Today, the eval framework stops at SQL generation. It doesn't test what the agent *says about* the data. This is where the most dangerous failures live, because they're invisible — the SQL is correct, the query runs, the numbers are there, but the natural language summary misrepresents them.

### What faithfulness evals should actually test

**Claim decomposition and verification.** Break the agent's response into atomic claims, then check each against the query results. "Revenue grew 15%" — is the growth rate actually 15%? "Driven by enterprise expansion" — does the data support a causal claim, or is this fabricated reasoning? The technique is well-established (RAGAS Faithfulness, DeepEval's G-Eval): an LLM extracts claims, then a verifier checks each claim against the source data.

**Magnitude and unit accuracy.** The single most dangerous interpretation error is getting the scale wrong: millions vs. thousands, daily vs. monthly, per-account vs. total. A dedicated scorer that extracts all numbers from the response and cross-references them against the SQL result set catches these before a user does.

**Appropriate hedging.** When data is incomplete (e.g., HubSpot coverage is only ~60%), the agent should say so. When a trend is based on 2 data points, it shouldn't speak with the confidence of 24 months of data. Faithfulness includes knowing what you *don't* know.

**Hallucination detection.** Does the response contain claims not supported by the query results? Does it reference data that doesn't exist in the result set? This is a binary check — any unsupported claim in a BI context is unacceptable, because business decisions may depend on it.

### What faithfulness looks like in practice

A faithfulness eval adds a new layer to the existing framework:

1. **For each L3 test**, after validating SQL correctness, capture both the query results and the agent's natural language response.

2. **Run a faithfulness scorer** that extracts numerical claims from the response and verifies them against the actual data. This is an LLM-as-judge step, but a targeted one — it's grading "does the text match the numbers?" not "is this a good answer?"

3. **Score on three dimensions**: factual consistency (all claims supported by data), completeness (key findings mentioned), and appropriate uncertainty (caveats where data is limited).

The genie's existing response protocol already requires auto-validation and confidence tagging (High/Medium/Low). Faithfulness evals make that protocol testable.

---

## Pillar 3: Confidence

*How reliably can we expect this to be right — and does it stay that way?*

Accuracy and faithfulness evaluate individual answers. Confidence evaluates the *system*. It answers two meta-questions: "When this agent tells me something, how often is it actually correct?" and, critically, "**Did the last change make things better or worse?**"

This second question — regression — is what separates a demo from a production system. Any change to the agent can have unintended consequences: a knowledge file edit that fixes revenue queries might silently break adoption queries. A model upgrade that improves SQL syntax might degrade interpretation quality. Without regression testing, you're flying blind. With it, you have a ratchet that prevents quality from sliding backward.

### Regression: the backbone of confidence

Regression testing is the practice of continuously re-running a stable suite of tests to ensure that past behaviours are retained correctly — and repeatedly. It deserves special emphasis because it's the mechanism that turns point-in-time accuracy into durable reliability.

**The graduation model.** Not every test starts as a regression test. New tests begin as *capability evals* — they have low pass rates and represent aspirational behaviours ("can the agent handle temporal queries?"). As a capability eval climbs to sustained high pass rates, it *graduates* into a regression gate. The test doesn't change — its purpose does. It shifts from "can we do this?" to "can we still do this?" A regression failure on a graduated test is treated as a blocker, not a nice-to-have.

**What regression catches.** The most dangerous failures are the ones nobody notices:

- A knowledge file change that fixes one domain but breaks another. The revenue knowledge improves, but the cross-schema join pattern it relied on was also used by adoption queries — which now silently fail.
- A model upgrade that changes tool-calling behaviour. Opus 4.6 reads files in a different order than Sonnet 4.5, and the new order skips a knowledge file that contained a critical gotcha.
- A Snowflake schema change upstream. A column rename in production data doesn't break the eval suite (which uses test accounts) but breaks real queries — unless the regression suite includes realistic queries against live data.

**How regression testing works in practice.** Every PR that touches skill files, knowledge files, or the eval framework itself triggers the regression suite. The suite is the full set of graduated tests (currently the L1/L2 suite, growing over time). The PR can only merge if pass rates don't drop. Specifically:

- **No regressions** = green, merge freely
- **Regressions with improvements** = yellow, reviewer decides (e.g., 2 new tests pass but 1 old test fails — is the trade-off worth it?)
- **Net regressions** = red, must be fixed before merge

This is cheap to run (~$2 for L1/L2, 3 minutes) and transforms the eval suite from something you run manually into a safety net that's always on.

**Regression tracking over time.** When you change a knowledge file, does accuracy go up or down? Today, you can't answer this because results are gitignored. Persisting eval results — even just as timestamped JSON — creates a time series. A chart showing "revenue accuracy: 85% → 90% → 88% → 94%" over four weeks tells a story. A sudden drop from 94% to 75% after a model change screams for attention.

### Reliability scoring: the static dimension

Alongside regression (the temporal dimension), confidence needs a point-in-time reliability score that fuses multiple signals:

**Multiple trials, not single runs.** The current evals run each test once. But LLM outputs are non-deterministic — a test that passes 7 out of 10 times is meaningfully different from one that passes 10 out of 10. Running 3–5 trials per test and reporting both pass@k (at least one passes) and pass^k (all pass) reveals the reliability gap. pass^k is what matters in production: users expect correct answers every time, not most of the time.

**A composite reliability score.** No single signal is trustworthy alone. A practical composite combines:

- **Eval pass rate** for this question category (from the golden dataset)
- **Self-consistency** — when you ask the same question 5 times, do you get the same SQL? Agreement = high confidence. Divergence = flag for review.
- **Category coverage** — how well-tested is this domain? Revenue questions backed by 30 golden tests deserve more confidence than a question type with 3 tests.
- **Regression trend** — is accuracy for this category stable, improving, or degrading? A category with 90% pass rate but a downward trend is less trustworthy than one at 85% with a stable or improving trend.

Each signal contributes to a 0–100 score. The score is **per question category**, not per individual query. "Revenue questions: 92/100" tells a stakeholder something actionable. "This specific query: 73/100" tells them to double-check.

### The trust signal in the response

Confidence shouldn't live only in a dashboard. It should be visible in the answer itself:

- 🟢 **High confidence** (85+): "Enterprise revenue grew 15% QoQ to $2.3M." Answer presented directly.
- 🟡 **Medium confidence** (60–84): "Revenue appears to have grown ~15%, but note that AWS Marketplace invoices have a sync lag — you may want to verify." Answer with caveats.
- 🔴 **Low confidence** (<60): "I found some revenue data but this query type hasn't been well-validated. Here's the SQL — I'd recommend having the data team verify." Transparent about limitations.

This is where the genie's existing personality (🔮 Validating, ⚠️ Data warning) becomes machine-testable rather than just stylistic.

### What confidence looks like in practice

**Cost tracking is part of confidence.** You can't run multi-trial evals, nightly regression suites, or CI/CD quality gates without understanding what they cost. The custom provider already tracks tokens — adding a `cost` field (~10 lines of code) and persisting results (~5 min config change) are prerequisites for everything else.

**The trust dashboard.** A weekly view that shows:

- Overall accuracy trend (line chart, 12 weeks)
- Per-category pass rates (heatmap: revenue, usage, adoption, signals, cross-domain)
- Regression alerts (categories where pass rate dropped >5% since last run)
- Graduated test count (how many tests have reached "regression gate" status)
- Coverage gaps (question categories with <10 golden tests)
- Cost per run (broken down by inference, grading, and Snowflake)

This is the artifact that turns evals from "developer testing" into "organizational evidence." A PM can look at this dashboard and say "the genie handles revenue questions reliably but we should be cautious with customer signals queries." That's the goal.

---

## How the three pillars connect

The pillars form a pipeline that mirrors how the agent works:

```
Question → [Skill Routing] → [SQL Generation] → [Data Retrieval] → [Interpretation] → Answer
              Pillar 1              Pillar 1           Pillar 1           Pillar 2
            (skill path)          (accuracy)          (accuracy)        Faithfulness
                                                                             ↓
                                                                      Pillar 3: Confidence
                                                                   (wraps everything above)
```

And they form a maturity model for the eval framework itself:

| Stage | What you can say | What it requires |
|-------|------------------|------------------|
| **Accuracy only** | "The SQL is correct and the agent reasoned correctly" | Golden dataset + multi-dimensional scoring + skill path validation |
| **+ Faithfulness** | "The answer is correct" | Claim verification + hallucination detection |
| **+ Confidence** | "The answer is reliable" | Multi-trial scoring + regression tracking + trust dashboard |

You don't need all three on day one. Start with accuracy (enhance what exists), add faithfulness (new scorer layer), then build confidence (the system that wraps it all together). Each stage is independently valuable, each builds on the last, and the end state is something no other internal BI tool has: **provable, measurable, communicable reliability.**

---

## What to build first

The framework is ambitious, but the first steps are small:

1. **Activate expectedValues in L3 tests** (accuracy). The infrastructure exists — `setup/index.js` captures stable facts, `snowflake-sdk-runner.js` supports the config. Wire them together. This upgrades L3 from "does it run?" to "does it return correct data?"

2. **Build the multi-dimensional accuracy scorer** (accuracy). Implement the 9 binary dimensions: execution, table alignment, column alignment, filter correctness, row completeness, row precision, value accuracy, no hallucinated columns, and skill path correctness. Each dimension is independently pass/fail. The overall score is the fraction that passed. This replaces subjective continuous rubrics with objective, diagnostic scoring.

3. **Add skill path expectations to golden tests** (accuracy). Extend the golden dataset format to include `expected_skill_path` — the sequence of skills/knowledge files the agent should invoke. The enhanced provider already captures the reasoning trace; this makes it evaluable. Start with the revenue domain where routing paths are well-understood.

4. **Add cost tracking to the provider** (confidence prerequisite). Ten lines of code: pricing map + `cost` field in the return. Persist results with `--output`. Now you can budget for everything else.

5. **CI/CD gating** on PR changes to knowledge files. L1+L2 on every PR (~$2, 3 minutes). L3 nightly. Regression alerts on Slack.

Five changes. Each one independently valuable. Together, they transform the eval framework from a development tool into a trust infrastructure.