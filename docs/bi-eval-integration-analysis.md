# Integration Surfaces for `bi-evals`: What It Should Expose, and How Systems Should Connect to It

## TL;DR
- **The contract is the product; transports are adapters.** `bi-evals` already has a thin, well-chosen contract: the agent hands over **generated SQL + an execution trace** (`files_read` / tool calls), and the scorer *executes the SQL itself* to compute `value_accuracy`, `row_completeness`, etc. Because the scorer runs the SQL, the customer never has to surface result sets — only the query and what it touched. That contract is universal and topology-free. Everything else is just *how those two fields arrive*.
- **`bi-evals` should evaluate the response, not reconstruct the agent.** The decisive design principle (your own, arrived at in discussion): `bi-evals` must not own the LLM loop. Reconstruction (`bi-evals` as MCP *client* running its own model/orchestration — the `McpAdapter` from the earlier draft) is *per-customer and low-fidelity*; response-evaluation is *universal and faithful*. As a product aimed at "plug into any setup," only the response-evaluation stance survives contact with arbitrary topologies.
- **This reframes issue #27.** The fix is **not** "`bi-evals` as MCP host/client." It's a set of *delivery adapters* that all normalise into the one contract: (A) customer **pushes** SQL+trace to `bi-evals` (CLI/SDK/collector), (B) customer's production host **calls `bi-evals` as an MCP server** at the end of its loop, (C) `bi-evals` **reads OTel traces** the customer already emits. Rank them by how much production change each demands; the least-invasive one that a given customer can satisfy is their on-ramp.
- **`bi-evals`'s defensible value is the scorer, not the transport.** The 9-dimension gating-vs-diagnostic design (verified from the repo) maps cleanly onto established text-to-SQL evaluation methodology (Execution Accuracy + partial-credit set comparison + schema checks). That layer — not the pipe — is the product.

> **Sourcing note:** The repo README and issue #27 were retrieved and verified this session, so claims about the current modes (`anthropic_tool_loop`, `api_endpoint`), the 9 dimensions, the tiering, and the scorer-executes-SQL design are grounded in the actual repo. The `src/bi_evals/provider/` source itself could not be fetched (GitHub directory/blob listing blocked), so internal provider-dispatch details are inferred from the README's stated behaviour and your confirmation. External claims (MCP transports, Promptfoo's `mcp` provider, Langfuse/Braintrust/DeepEval integration surfaces, text-to-SQL metrics) are independently sourced.

---

## Part 1 — What `bi-evals` actually is today (verified from the repo)

**Two current agent modes** (from README + issue #27):
- `anthropic_tool_loop` ("built-in") — `bi-evals` rebuilds the agent locally from skill/knowledge *files* plus a Claude tool-calling loop (`file_reader`, `describe_table`).
- `api_endpoint` ("BYO") — `bi-evals` POSTs `{"question": ...}` to a customer URL and expects JSON back.

**The scoring contract** (verified): a test yields 9 independent dimension results, aggregated by a tiered/weighted rule.

| Dimension | Tier | Weight | What it needs from the agent |
|---|---|---|---|
| `execution` | critical | 3.0 | the generated SQL (scorer runs it) |
| `row_completeness` | critical | 3.0 | the generated SQL (scorer runs both, compares row keys) |
| `value_accuracy` | critical | 3.0 | the generated SQL (scorer runs both, compares values within tolerance) |
| `row_precision` | important | 2.0 | the generated SQL |
| `column_alignment` | important | 2.0 | the generated SQL (static parse vs `required_columns`) |
| `table_alignment` | diagnostic | 1.0 | the generated SQL |
| `filter_correctness` | diagnostic | 1.0 | the generated SQL |
| `no_hallucinated_columns` | diagnostic | 1.0 | the generated SQL |
| `skill_path_correctness` | diagnostic | 1.0 | the trace: files read + tools invoked |

**Pass rule:** all `critical_dimensions` pass **and** weighted score ≥ `pass_threshold` (0.75). Critical dimensions gate; diagnostics shape the score but can't fail a test alone.

**The crucial architectural fact:** eight of nine dimensions are computed *from the generated SQL string*, and the scorer *executes that SQL itself* against the warehouse to get values. The ninth (`skill_path_correctness`) needs the trace. So:

> **The minimum viable contract is `{ generated_sql, trace }` per question.** Not result sets. The scorer is the oracle; it executes both the candidate SQL and the reference SQL on its *own* connection.

This is a genuinely strong design choice, and it's the foundation everything below rests on.

---

## Part 2 — The stance that resolves everything: evaluate the response, don't reconstruct the agent

A production BI agent (e.g. Ably's Genie) is a stack: **model → orchestration loop → MCP tools → warehouse.** The question for any eval tool is: *which layers does it own, and which does it observe?*

- **Reconstruction** (`bi-evals` owns model + loop, observes only tools) — this is what `anthropic_tool_loop` does locally, and what an `McpAdapter`/MCP-client would do remotely. It evaluates *a rebuild* of the agent. Fidelity is bounded by how perfectly you replicate the prompt/model/loop, and it requires per-customer knowledge of their stack. **Fails "any setup."**
- **Response-evaluation** (`bi-evals` owns nothing, observes the produced SQL + trace) — the real stack runs; `bi-evals` scores its output. Faithful by construction, and makes *no assumption* about how the SQL was produced. **Survives "any setup."**

The product goal ("plug into any setup") and the fidelity goal ("evaluate the real Genie") turn out to be the *same* constraint, and both point at response-evaluation. This is why the `McpAdapter`-as-client recommendation in the earlier draft was wrong *for this product*: it's a reconstruction pattern wearing a transport costume.

**Consequence for issue #27:** the proposal in the issue ("`bi-evals` as MCP host running the agent loop") is itself a reconstruction pattern — it has `bi-evals` supply the model and orchestration. It should be superseded. The MCP idea is right, but the *direction* is backwards: not `bi-evals`→client→their server, but their host→client→**`bi-evals` as server**, or simpler push/trace adapters.

---

## Part 3 — How comparable tools structure their integration surface (and what to copy)

The pattern every successful tool converges on: **one target abstraction, many adapters.** Not "modes."

- **Promptfoo — `providers`.** One provider interface; implementations include a model string, an `http` endpoint, a custom JS/Python file, and a first-class `mcp` provider. The `mcp` provider treats "the MCP server itself as the system under test," supports remote servers (`url` + `headers`) with a full auth matrix (bearer / basic / api_key / OAuth client-credentials, with token-endpoint discovery and auto-refresh), takes prompts as JSON tool calls, and uses `transformResponse` to reshape tool output into a scorable response. *This is the closest existing precedent for MCP-aware evaluation, and the auth/transform machinery is worth modelling on.*
- **Braintrust — `Eval(name, { data, task, scores })`.** `task` is "the unit of work being evaluated — usually one or more LLM calls," and can wrap *anything* (an HTTP call, a crew, an agent). `data` is the dataset, `scores` are scorers. Clean separation of *how you reach the system* (`task`) from *how you judge it* (`scores`).
- **DeepEval — instrumented vs un-instrumented.** Either decorate the app with `@observe` so it auto-builds the test case, **or** the black-box path: "you build the `LLMTestCase` yourself and hand it to `assert_test()` … use this when you can't or don't want to instrument the app — e.g. evaluating a deployed black-box system." *That black-box path is exactly `bi-evals`'s response-evaluation stance.*
- **OpenAI Evals — `CompletionFn`/`Solver`.** Deliberately "solver-agnostic": the eval doesn't bake in how the system-under-test is reached.
- **Langfuse / Phoenix — OTel normalisation.** Observability tools don't re-architect per backend; they normalise everything to OpenTelemetry GenAI spans and let any backend consume them. "Instrument once, switch backends."

**Lesson for `bi-evals`:** replace "`anthropic_tool_loop` mode vs `api_endpoint` mode" with **one `AgentSource` contract** (`-> { generated_sql, trace }`) and several adapters. The modes become adapters; the scorer never changes.

---

## Part 4 — The three delivery adapters (with architectural sketches)

All three deliver the *same* `{ generated_sql, trace }` into the *same* scorer. They differ only in who initiates and how invasive they are to the customer's production stack.

### Adapter A — Customer pushes to `bi-evals` (sink model) — *lowest friction, recommended default*

The customer runs their real agent however they like, and emits the contract for each golden question. `bi-evals` receives and scores. This is the purest response-evaluation shape, and it's essentially your `api_endpoint` contract **inverted** (they push artifacts of a produced answer, instead of you pulling an answer synchronously).

```yaml
# bi-evals.yaml
agent:
  type: push                      # bi-evals exposes a sink; customer submits results
golden: ./golden/
scoring:
  critical_dimensions: [execution, row_completeness, value_accuracy]
  pass_threshold: 0.75
```

```bash
# Customer's CI, after running their production agent over the golden questions:
bi-evals score --input results.jsonl
```

```jsonl
# results.jsonl — one line per golden question; the entire contract
{"question_id": "q_017", "generated_sql": "SELECT region, SUM(rev) ... GROUP BY 1", "trace": {"files_read": ["skills/revenue.md"], "tool_calls": [{"tool": "describe_table", "args": {"name": "fct_revenue"}}, {"tool": "run_query", "args": {"sql": "SELECT ..."}}]}}
```

Equivalent thin SDK form (for customers who'd rather call a function than write a file):

```python
import bievals

runner = bievals.Runner(config="bi-evals.yaml")
for case in runner.golden_cases():
    answer = my_production_agent.ask(case.question)   # their real stack, unchanged
    runner.submit(                                    # "emit the contract" = this call
        case.id,
        generated_sql=answer.sql,
        trace=answer.raw_trace,                       # dump whatever the agent exposes
    )
report = runner.score()
```

**What "emit the contract for each golden question" actually means.** `bi-evals` owns the golden questions, so it hands the customer the list. The customer's "integration" is a *for-loop* around the agent they already have: ask each golden question, call `runner.submit(...)` with what comes back. There is no service to stand up, no inbound connection, nothing to keep running. This is the key difference from the old `api_endpoint` mode: that asked the customer to **build and host an interface** (infrastructure their platform team owns and maintains); Adapter A asks them to **run a script once** (an afternoon for one engineer, no new infra). Both require *some* customer work — the claim was never "zero effort," it's "work a single engineer can do without involving infra."

**The real cost is not the push — it's what the agent surfaces.** The for-loop is trivial. The load-bearing question is whether `answer.sql` and `answer.raw_trace` even exist: does the customer's agent expose its generated SQL and its tool/file trace *to its own caller*? Many text-to-SQL agents do (they show the SQL in the UI), in which case Adapter A is genuinely an-afternoon's work. If the agent buries the SQL and only returns a natural-language summary, the customer must crack it open to surface it — and that cost is **identical for every adapter (A, B, and C alike)**. This is the true integration boundary of `bi-evals`: not the transport, but *what the agent reveals about its own work*. No adapter can score `value_accuracy` from a SQL string the agent never emitted.

#### The contract is an open envelope, not a rigid schema

A naive fixed schema ("submit exactly these fields") is brittle: the moment you add a scoring dimension that needs a new field, every customer's submission script breaks. Avoid this by making `trace` an **open envelope** — the customer dumps *whatever their agent emits* (all tool calls, file reads, timings, intermediate reasoning), and `bi-evals` reads the keys it understands *today* while storing the rest. A customer who over-captures on day one has already satisfied dimensions you haven't invented yet: when you later ship, say, a `tool_efficiency` dimension, their existing rich submissions light it up **retroactively, with zero work on their side.**

> **Design rule:** tell customers to *over-capture*, not to match a schema. The contract is "send me the richest trace your agent can produce," not "send me these five fields." (This is exactly why OTel spans are bags-of-attributes rather than fixed structs — producer and consumer evolve independently.)

#### The bridge: normalise to one canonical trace shape

Open envelopes mean every customer's trace is shaped differently (Ably's Genie ≠ a LangChain agent ≠ Cortex). So `bi-evals` provides a thin **bridge / normalisation layer** per customer-shape that captures the agent's native trace and reshapes it into **one canonical internal format** that the scorer consumes. The scorer only ever sees the canonical shape; the messiness is isolated in small, named, independently-testable bridges.

```
   Genie native trace  ─┐
   LangChain trace      ─┤──►  [ bridge / normaliser ]  ──►  canonical trace  ──►  scorer
   Cortex trace         ─┘        (one per source-shape)        (one shape)        (never changes)
```

This is the N+M-not-N×M argument (the reason OpenTelemetry exists): many producers, one canonical shape, so each side evolves independently. **Crucially, `bi-evals` does not change the agent's behaviour** — the Genie runs exactly as it always does; the bridge only captures and reshapes what already comes out.

#### The hard boundary: the bridge reshapes *format*, it cannot invent *information*

This is the one place the idea is harder than it looks, and it must be stated explicitly in the design:

- **Format mismatch → free.** Agent emits `{start, end}` timestamps; scorer wants `duration_ms`. The bridge subtracts two numbers. Pure reshaping. Works always.
- **Information mismatch → impossible.** A dimension needs tool timings but the agent never recorded timestamps. No bridge can conjure data that was never emitted. The only fixes are: the customer instruments their agent to start emitting it (real work, back on their side), or that dimension scores **`unknown`** for that customer.

So the bridge handles everything on the "already surfaced" side of the integration boundary, and nothing crosses it.

#### Turn the boundary into a feature: a capability check

Because some customers will surface *some* but not all of what the dimensions want, `bi-evals` should run a **capability check** at connect-time that maps "what your trace contains" → "which dimensions light up," using the existing critical/important/diagnostic tiering for graceful degradation:

- Dimensions whose required fields are present → **scored**.
- Dimensions whose fields are absent → **`unknown` / skipped**, surfaced explicitly (never silently failed).
- The report tells the customer, on day one: *"Given what your agent currently emits, I can score these 6 dimensions now. Surface tool timestamps and retrieved-chunk content and you'd unlock these 3 more."*

This converts the hard boundary from a silent wall into an explicit **graceful-degradation story + upgrade path** — and doubles as the product's adoption/value ladder (thin trace = core scoring; richer trace = more dimensions).

**Why Adapter A is the recommended default on-ramp:** it asks the customer only to run a script that emits whatever their agent already exposes — no hosting, no protocol, no behaviour change — and the open-envelope + bridge design means your scorer can grow without forcing every customer to re-integrate. It works regardless of whether their agent is MCP, LangChain, Cortex, or a shell script.

### Adapter B — `bi-evals` as an MCP server the production host calls — *highest fidelity for MCP-native shops*

This is the topology inversion you proposed. `bi-evals` exposes an MCP server; the customer's **real** production host (their model, their loop) connects to it and calls a `submit_for_eval` tool as the final step of answering each golden question. The production stack does all the reasoning (full fidelity, no reconstruction), and hands `bi-evals` the contract *in the act of calling the tool*.

```yaml
agent:
  type: mcp_server                # bi-evals listens; the customer's host connects in
  bind: 0.0.0.0:8900
  transport: streamable_http      # current MCP remote standard
  auth: { mode: bearer }          # token the customer's host presents
```

```jsonc
// The tool bi-evals advertises (tools/list). The production agent calls this last.
{
  "name": "submit_for_eval",
  "description": "Submit generated SQL and execution trace for scoring.",
  "inputSchema": {
    "type": "object",
    "required": ["question_id", "generated_sql"],
    "properties": {
      "question_id":  { "type": "string" },
      "generated_sql":{ "type": "string" },
      "trace": {
        "type": "object",
        "properties": {
          "files_read": { "type": "array", "items": { "type": "string" } },
          "tool_calls": { "type": "array", "items": { "type": "object" } }
        }
      }
    }
  }
}
```

Conceptually, this is the mirror image of how you already wire Snowflake-MCP or dbt-MCP *into* a host: there, those servers expose data tools and your host consumes them. Here, `bi-evals` exposes an *eval-sink* tool and the customer's host consumes it. Same MCP plumbing, opposite role.

**Cost:** the customer must modify their production agent to call your tool (more invasive than Adapter A's "emit a file"). **Benefit:** MCP-native, real stack, and if you later want richer signals you extend the tool schema rather than the transport.

> **A subtlety from your `skill_path_correctness` dimension:** for that dimension to score, the trace the host submits must reflect *which skill files / tools the production agent actually used*. The host has to populate `trace` honestly. If the production loop won't expose that, `skill_path_correctness` degrades to "unknown" — non-fatal (it's diagnostic, weight 1.0), but document it.

### Adapter C — `bi-evals` reads OTel traces the customer already emits — *zero new production code, if they're instrumented*

If the customer instruments their agent with OpenTelemetry GenAI spans (increasingly common), the generated SQL and tool calls are *already* leaving the agent as trace data. `bi-evals` subscribes as a trace consumer and scores asynchronously. This is exactly how Langfuse/Phoenix attach — they don't drive the agent, they read its spans.

```yaml
agent:
  type: otel                      # bi-evals consumes spans; never touches the agent
  source:
    endpoint: otlp://collector.internal:4317
    filter: { service: "genie-prod" }
  map:                            # which span fields carry the contract
    generated_sql: "span.attributes['gen_ai.tool.call.arguments'].sql"
    files_read:    "span.events[?name=='skill.read'].attributes.path"
```

**Cost:** requires the customer to already emit (or add) OTel GenAI spans carrying the SQL and tool calls. **Benefit:** for the instrumented subset, this is the most plug-and-play option of all — no inbound tool call, no result file, just point `bi-evals` at the collector.

### Ranking by production-change demanded (least → most)

1. **Adapter C** *if already instrumented* → near-zero (point at the collector).
2. **Adapter A (push)** → small (emit a JSONL line / call `submit()` after answering).
3. **Adapter B (MCP server)** → moderate (modify the production loop to call your tool).
4. (**Adapter C if not yet instrumented** collapses into "add OTel," which can be the largest lift.)

A product should ship A as the default on-ramp (works for everyone), offer B for MCP-native shops that want maximal fidelity and minimal glue, and support C to win the already-observable accounts for free.

---

## Part 5 — Where this leaves the two existing modes

- **`anthropic_tool_loop` (built-in)** — keep it, but reframe it as the **"reconstruction" adapter for development/capability testing**, *not* a production-fidelity mode. It's genuinely useful: deterministic, fully observable, great for iterating on golden tests before a real agent exists. Just stop positioning it as "evaluating the agent" — it evaluates a rebuild.
- **`api_endpoint` (BYO)** — **still earns its place**, but only for customers whose agent genuinely *is* a clean question→SQL HTTP service. For those, synchronous pull is the simplest thing in the world. For MCP-fronted agents (the issue #27 class), it's the wrong shape — Adapter A/B/C replace it there. So `api_endpoint` isn't redundant; it's *one adapter among several*, no longer the BYO catch-all it was overloaded to be.

The reframe in one line: **`anthropic_tool_loop` and `api_endpoint` stop being the two top-level "modes" and become two adapters in a set of five (tool-loop, http-pull, push, mcp-server, otel), all feeding one contract and one scorer.**

---

## Part 6 — The scorer is the product (and the independence caveat)

Everything above is plumbing. The defensible value is the 9-dimension scorer, and it's well-grounded:

- **Execution Accuracy** (your `execution` + result comparison) is the field-standard text-to-SQL metric precisely because string-matching SQL fails on semantically-equivalent-but-different queries.
- **Partial-credit result comparison.** Your `row_completeness`/`row_precision`/`value_accuracy` with ratio thresholds is the right antidote to binary EX's brittleness on large result sets. Consider adding a **Soft-F1**-style aggregate (the literature's "informational overlap between generated and ground-truth result sets") so "99 of 100 rows correct" scores ~0.99 rather than 0.
- **Schema-level diagnostics** (`table_alignment`, `column_alignment`, `filter_correctness`, `no_hallucinated_columns`) are exactly what generic LLM-eval tools *don't* provide and what makes this BI-native.

**One independence caveat that the delivery choice creates.** Because the scorer *executes both* the candidate and reference SQL, *where it executes them matters.* Keep the **reference** SQL on a connection `bi-evals` controls directly — do **not** route reference execution through the customer's agent tools (e.g. an MCP `run_query` tool in Adapter B). If the agent's execution tool silently transforms queries (a default `LIMIT`, a role/warehouse swap, a type cast), routing your ground truth through it contaminates both sides identically and hides the bug. The candidate's results may come via whatever path is faithful to production; the *yardstick* must stay independent. (Be aware of the flip side: two different execution paths can differ for legitimate reasons — role, session settings — so document which connection scores the reference.)

---

## Recommendations (staged)

**Stage 1 — Collapse modes into one contract + adapter set.** Define `AgentSource -> { generated_sql, trace }`. Re-slot `anthropic_tool_loop` and `api_endpoint` as two adapters. Keep the scorer and golden format untouched. *Done when* the same golden suite scores identically through two different adapters.

**Stage 2 — Ship Adapter A (push) as the default plug-and-play on-ramp.** CLI `bi-evals score --input results.jsonl` + a thin `submit()` SDK. Make `trace` an **open envelope** (customer over-captures; scorer reads what it understands), build a **bridge/normalisation layer** that maps each customer's native trace into one canonical scorer-facing shape, and add a **capability check** that reports which dimensions light up given what the agent emits. This resolves the issue #27 friction for *any* topology, because it asks the customer only to run a script emitting whatever their agent already exposes — and lets the scorer grow new dimensions without forcing customers to re-integrate. *Done when* a non-`bi-evals` engineer scores their MCP-fronted agent without writing an adapter service, and a later scorer upgrade lights up new dimensions on their *existing* submissions.

**Stage 3 — Ship Adapter B (`bi-evals` as MCP server).** For MCP-native customers who want full-fidelity with minimal glue: expose `submit_for_eval`, Streamable HTTP, bearer/OAuth, map tool input → contract. Supersede issue #27's host-loop proposal with this inversion. *Done when* a customer's real production host scores by calling your tool, with no model/loop supplied by `bi-evals`.

**Stage 4 — Adapter C (OTel ingest) + Soft-F1 + CI reporting.** Consume `gen_ai.*` spans for already-instrumented customers; emit results as OTel `gen_ai.evaluation.result` so scores flow to Langfuse/Phoenix/Datadog; add a GitHub Action that posts per-dimension diffs on PRs (Braintrust/Promptfoo ergonomics). *Done when* a failing eval row links back to the agent's own trace.

**What would change the ranking:** if most target customers are *not* MCP-fronted, A stays default and B is niche; if most are *already* OTel-instrumented, C jumps to the front; if a customer's agent never surfaces SQL (only a final NL answer), the contract degrades — you'd lose the structural dimensions and need either agent instrumentation or a fallback that re-derives SQL, which is a product boundary worth stating explicitly.

---

## Caveats
- **Provider source unverified.** README + issue #27 are verified; `src/bi_evals/provider/` could not be fetched. The "scorer executes both queries" design is confirmed by you and the README; provider-dispatch specifics are inferred.
- **`skill_path_correctness` depends on trace honesty.** In Adapters B and C, the dimension only scores if the production agent faithfully reports files/tools. It's diagnostic (weight 1.0), so degradation is non-fatal but should be surfaced.
- **MCP is moving fast.** Streamable HTTP is current (MCP spec 2025-03-26, refined 2025-11-25, replacing HTTP+SSE) but auth and client support are still churning.
- **Execution Accuracy has false positives** (semantically different queries coinciding on a data instance) and benchmark numbers don't transfer to enterprise schemas (Spider 1.0 ~91% vs Spider 2.0 ~10–21% for frontier models). Pair execution scoring with the schema diagnostics; don't over-trust one aggregate.
- **Vendor flux.** Langfuse was acquired by ClickHouse (Jan 16 2026); Promptfoo agreed to be acquired by OpenAI (Mar 9 2026). Both remain open-source today; re-validate licensing before deep coupling.
- Some mechanism descriptions for comparison tools draw on vendor docs/blogs; wire-level facts were cross-checked against official Promptfoo/Langfuse/OpenTelemetry/MCP docs where possible.