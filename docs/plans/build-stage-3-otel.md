# Plan: OTel — SDK trace correlation + batch-ingest adapter

> **Status:** Part 1 implemented (`Runner.traced_call()` — see `STATUS.md`'s
> "Build Stage 3, Part 1" entry under Completed). Part 2 (file-based OTLP
> batch-ingest adapter) is design-only, gated behind a real customer who fits
> all three clauses in its "Who this is for" section below.

## Why (and why not more than this)

`STATUS.md`'s Build Stage 3 was originally scoped as "OTel adapter — ingest
spans the real agent emits," framed as the lowest-effort, highest-fidelity
on-ramp because OpenTelemetry is the vendor-neutral substrate the observability
market has converged on (CNCF-graduated 2026; OpenLLMetry/OpenInference are
semantic-convention layers *on top of* OTLP, not competing wire formats — an
OTel-based adapter is not bespoke to one vendor's instrumentation).

That value is real but **conditional**: it only pays off for customers who are
already OTel-instrumented, and even then it is not zero-cost — the customer
must still tag spans with a bi-evals-known correlation key, because bi-evals
never calls their agent (see "Two designs" below). Framing corrected during
design review from "zero-touch adapter" to "two `set_attribute` calls at one
call site, reusing an existing tracer."

**Explicitly out of scope for this stage** (per `CLAUDE.md`'s MVP discipline —
none of this makes first-run setup faster or clearer, so it stays deferred
until a real customer needs it):
- A live OTLP receiver bi-evals stands up during a run (rejected — see below).
- Pulling spans from a backend's query API (Langfuse/Phoenix/Datadog) — a
  distinct, larger integration per backend.
- Writing bi-evals' own scores back as OTel spans (`gen_ai.evaluation.result`)
  so they show up in the customer's dashboard — a real idea from the original
  research, but a separate, later addition once ingestion exists at all.

## Two designs considered, one rejected

**A. Live receiver** — bi-evals stands up an OTLP receiver during `bi-evals
run`, mints a `traceparent` per golden the way Promptfoo's own tracing feature
does, and the agent must be invoked *as part of the eval run* to continue that
trace context. **Rejected**: this requires bi-evals to be in the request path
at call time, which collapses into "real-time orchestration wearing an OTel
costume" — a different `api_endpoint`, not a new capability. It also breaks
the load-bearing "bi-evals is an offline eval tool, not live traffic" decision
already recorded in `STATUS.md`.

**B. Batch ingestion** — the agent already ran (in CI, staging, a customer-run
replay pass) and already emitted spans; bi-evals shows up afterward and asks
"what happened for these test-case IDs?" Same shape as `push`: the golden
suite is still the source of truth for what's being asked; bi-evals just gets
the trace data through a different pipe. **Chosen** — consistent with the
offline-eval decision, and the correlation-tagging cost (below) is honest
about what B actually requires instead of promising something A alone could
deliver.

## The correlation problem (why the customer must tag anything at all)

Because bi-evals never calls the agent in design B, it can't attach a
correlation ID to a request after the fact — a span can't be labeled by
something that wasn't there when it was created, and OTLP exports are
effectively immutable by the time bi-evals reads them. Whoever drives the
agent call is the only party that can tag it. Two required attributes — but
**their acquisition costs are very different, and the gap is the crux of this
whole stage**:

1. **`bi_evals.golden_id`** — which golden this trace answers. Must be set on
   the trace's root span (or any span in it) at call time. Trivial: a
   correlation tag with no semantic judgment.
2. **`bi_evals.generated_sql`** (config-nameable, default shown — mirrors
   `api_endpoint.response_sql_key`'s existing precedent of "customer names the
   field, config points at it" rather than sniffing) — there is no `gen_ai.*`
   or OpenLLMetry/OpenInference convention for "this tool call's result was
   this specific SQL string," so this one has to be explicit, not inferred.
   **This is the real work, and it's on the exact artifact under evaluation.**
   The final SQL is the load-bearing field the scorer *executes against the
   warehouse* (`contract.py`: "`generated_sql` and `trace` are the load-bearing
   fields"). Unlike the trace shape below, it can't be fuzzy-matched out of
   existing spans — the customer must deliberately reach into their agent, find
   the moment it has settled on its *final* SQL (not an intermediate draft, not
   a raw tool-call argument, not one of several candidates), and emit that
   specific string as a span attribute. In many agent architectures the final
   SQL isn't a clean variable in one place, so this is genuine instrumentation
   work — see Part 2's "Who this is for" for what that means for the audience.

**These two are not equal, and the difference drives the sequencing.** The
trace *shape* — which tools ran, in what order, reading which files, scored by
`skill_path_correctness` — is genuinely nearly free *if* the spans already
exist: Promptfoo's `trajectory:*` assertions already prove OTLP span attributes
can be read generically via fuzzy key matching
(`/tool.?name|function.?name/i`, `/(^|[._])(arguments|args|input)($|[._])/i`)
rather than a hardcoded per-vendor allowlist, and this stage reuses that same
approach rather than re-inventing it. So the trace path is a read-off-existing-
instrumentation problem; the `generated_sql` attribute is a
customer-adds-new-instrumentation problem. **Part 1 sidesteps the second
problem entirely** (it carries the SQL as a plain Python value through
`submit(generated_sql=...)`, never as a span attribute), which is a large part
of why it's the primary path and Part 2 is gated behind real demand.

---

## Part 1 — `Runner.traced_call()` (SDK correlation helper)

### What it is

A thin context manager on `bi_evals.Runner` that opens one span tagged
`bi_evals.golden_id`, for customers who already run OTel in their own stack
and want the request bi-evals triggers to show up correctly labeled in their
own trace dashboard (Datadog/Langfuse/whatever they already use). It changes
**nothing** about how bi-evals gets scored — `submit()` already carries
`trace=` today (`sdk.py:228`, accepts `Any`, serialized straight into the push
JSONL row) and stays the only path data reaches the scorer through. This is
pure production-observability courtesy: jump from a failing bi-evals report
row into the customer's own full trace for that exact request.

### Usage

```python
import bi_evals
from opentelemetry import trace

runner = bi_evals.Runner("bi-evals.yaml", verbose=True)
tracer = trace.get_tracer("my-agent")          # customer's own OTel setup, untouched

for case in runner.golden_cases():
    with runner.traced_call(case, tracer):
        answer = my_agent.ask(case.question)    # unchanged — emits its own child spans as usual
        runner.submit(case, generated_sql=answer.sql, trace=answer.trace)
```

### API

```python
@contextmanager
def traced_call(self, case: Case, tracer: "opentelemetry.trace.Tracer") -> Iterator["opentelemetry.trace.Span"]:
    """Open a span tagged `bi_evals.golden_id` so this request correlates in
    the caller's own OTel backend. Purely a courtesy to the caller's tracing
    setup — has no effect on how bi-evals scores the submission; use
    `submit(trace=...)` for that, same as always."""
    with tracer.start_as_current_span(f"bi_evals.golden:{case.id}") as span:
        span.set_attribute("bi_evals.golden_id", case.id)
        yield span
```

### Design notes

- **bi-evals does not own tracer setup, exporters, or endpoints.** The
  customer passes in their own `Tracer`; if they don't have `opentelemetry-api`
  installed, `traced_call` isn't importable/usable and nothing else in the SDK
  is affected — this stays an optional accessory, not a new dependency of
  `bi_evals.Runner` itself.
- `opentelemetry-api` becomes an **optional extra**
  (`pip install "bi-evals[otel]"` / `uv add "bi-evals[otel]"`), not a base
  dependency — matches the MVP discipline of not growing install weight for a
  feature most first-time users won't touch.
- No config surface, no new adapter, no scorer change. Purely additive to the
  SDK's public surface.
- **Part 1 never touches the `generated_sql`-attribute problem.** It tags only
  `bi_evals.golden_id` (a benign correlation tag) and gets the actual SQL to the
  scorer through `submit(generated_sql=...)` — a clean Python value, no span-
  attribute round-trip, no missing-convention extraction. The entire
  correlation-attribute cost described above is *exclusively* a Part 2 concern.
  This is the core reason Part 1 is the primary path.

### Testing

- Unit: `traced_call` opens exactly one span, sets `bi_evals.golden_id`
  correctly, re-raises exceptions from the `with` body unchanged (doesn't
  swallow agent errors), works with an in-memory `TracerProvider` +
  `InMemorySpanExporter` (no real OTLP endpoint needed for tests).
- No live-agent/API-spend test needed — this is pure SDK plumbing.

---

## Part 2 — File-based OTLP batch-ingest adapter (`agent.adapter: otel`)

### Who this is for

A **much** narrower audience than Part 1 — narrower than "anyone with existing
OTel exports," which is the tempting overstatement to avoid. The honest audience
is: a customer who has *already committed to emitting `bi_evals.generated_sql`
in their own instrumentation*, runs a batch pass that produces OTLP export
files, and cannot or will not write a `Runner` loop to call `submit()`.

That third clause is what makes it real (non-Python shops; teams whose eval pass
is a separate CI job with nothing that could `import bi_evals`) — but the first
clause is the catch. Because there is no convention for the SQL attribute (see
"The correlation problem"), nobody's *existing* production spans carry it; a
customer only has scoreable exports if they went back, added a bi-evals-shaped
attribute to their agent, and re-ran to produce fresh exports. At that point
they've done strictly *more* work than Part 1 + `submit()` — for a file-parsing
pipeline instead of a function call. So "the agent already ran independently"
does **not** by itself make a customer a Part 2 user; the pre-existing
`generated_sql` instrumentation does. Ship Part 2 only once real usage shows a
customer who genuinely fits all three clauses — and if the customer is willing
to write a `Runner` loop at all, Part 1 + `submit()` already covers them with
far less new code (no export files, no parser, no adapter).

### Shape (mirrors `PushReplayAdapter` exactly)

1. **Input**: a directory of one-or-more OTLP/JSON trace export files (see
   Decision 2 below for why a directory, not a single file — reuses the wire
   shape Promptfoo's own OTLP receiver already decodes, no new format
   invented).
   ```bash
   bi-evals score --otel-input otel-exports/
   ```
2. **Ingestion** (`provider/otel.py`, new): parse every export file in the
   directory, merge and group spans by `traceId` **across all files** (a
   trace can span multiple export flushes — see Decision 2), read
   `bi_evals.golden_id` off each merged trace, map spans onto the existing
   `TraceStep` list (`round`, `type`, `tool_name`, `tool_input`,
   `tool_result_preview`, `text`) using the same fuzzy attribute-key matching
   `trajectory:*` uses, and pull `generated_sql` from the configured attribute
   key, passed through `extract_sql()` (see Decision 3). Produces the same
   `{golden_id: AgentResult}` map `PushReplayAdapter` builds from JSONL — same
   canonical contract, different source format.
3. **Adapter**: `OtelReplayAdapter` — structurally identical to
   `PushReplayAdapter` (`registry.py:247`): pre-load the map once, `produce()`
   looks up by golden ID and returns it. No new `Adapter` protocol shape;
   `produce()` stays synchronous, it just resolves instantly from memory.
4. **Config** (new `OtelConfig` block, same nesting pattern as `PushConfig`):
   ```yaml
   agent:
     adapter: otel
     otel:
       input_dir: "otel-exports/"
       golden_id_attribute: "bi_evals.golden_id"       # override if customer uses a different key
       generated_sql_attribute: "bi_evals.generated_sql"
   ```
5. **Registry**: one new branch in `build_adapter()` (`registry.py:312`),
   `adapter: Literal[..., "otel"]` added to `AgentConfig` (`config.py:209`).

### What's reused unchanged

`TraceStep`, `AgentResult`, the entire scorer (incl. `skill_path_correctness`
via `scorer/capability.py`'s existing trace-usability classifier — a trace
with no usable tool-call spans is honestly `not_evaluated`, not a failure,
same as today), ingest, report, compare. Only the ingestion/adapter layer is
new — same principle as every prior adapter addition (`push`, `api_endpoint`):
new front door, same pipeline.

### Partial traces are the common case, not the exception (design point)

Because there is no convention for the SQL attribute, the single most likely
real-world OTel export is one that is **partially populated** — good tool-call
spans (existing instrumentation) but a missing or malformed `generated_sql`
attribute (new instrumentation the customer forgot, mis-keyed, or hasn't rolled
out to every code path). Part 2 must treat this as an expected input, not an
error. Two behaviors, both reusing machinery that already exists:

- **Missing `generated_sql` attribute → `not_evaluated`, never a false
  failure.** When a merged trace has a usable tool-path but no value at the
  configured `generated_sql_attribute`, the ingestion produces an `AgentResult`
  with `extracted_sql=None`. This is *exactly* the shape Build Stage 2 already
  handles: `skill_path_correctness` still scores honestly off the tool spans,
  and the SQL-dependent dimensions report `not_evaluated` with an unlock hint
  ("no SQL found at attribute `bi_evals.generated_sql` on the trace for golden
  X — set `agent.otel.generated_sql_attribute` or emit the attribute at your
  call site"), rather than an empty-SQL failure or a hard ingest crash. This is
  the strongest fit between this stage and Stage 2: the capability check was
  built for precisely this "we have *some* signal but not all of it" situation,
  and the OTel adapter is the case most likely to hit it. A missing
  `golden_id` attribute is different — that trace can't be mapped to any golden
  at all, so it's skipped with a warning (Decision below), not scored as
  `not_evaluated`.

- **Malformed / prose-wrapped attribute value → `extract_sql()` forgives it.**
  Since no convention governs the attribute, customers will populate it
  inconsistently: some clean SQL, some the agent's whole fenced prose response,
  some with a trailing `;`. Routing the attribute value through the existing
  `extract_sql()` (Decision 3) means the adapter gets the same prose-tolerant
  extraction every other adapter gets — ```sql fences, generic fences, and
  sqlglot-validated bare statements all resolve; genuinely-broken values are
  rejected rather than sent to the warehouse mangled. **This should be stated
  plainly in the customer-facing docs**, because it turns the missing-convention
  problem into a forgiving, documented behavior: "put your final SQL in the
  attribute; if it's wrapped in prose or fences, we'll extract it — the same
  tolerance `push` and `api_endpoint` already give you." That framing is
  squarely on the MVP north star (forgiving first-run, clear errors) where a
  strict "the attribute must be exactly one clean SQL statement" rule would not
  be.

### Testing

- Unit: OTLP/JSON parsing (valid + malformed export), span→`TraceStep`
  mapping across a few realistic attribute-name shapes (raw OTel, OpenLLMetry-
  style, OpenInference-style — to prove the fuzzy matching genuinely isn't
  single-vendor), missing `golden_id_attribute` handling (skip + warn, don't
  crash the whole ingest), missing `generated_sql_attribute` → `not_evaluated`
  with the unlock hint (not a silent empty SQL), and a prose/fenced attribute
  value round-tripping through `extract_sql()` — the two behaviors from
  "Partial traces are the common case" above.
- `tmp/my-evals/` demo: one small OTLP export fixture exercising the adapter
  end-to-end, per `CLAUDE.md`'s live-project-sync rule (new adapter is
  user-visible config surface).

---

## Decisions (resolved during design review)

1. **`Runner.traced_call()`'s span name / attribute key: fixed, not
   configurable.** `bi_evals.golden_id` is bi-evals' own internal correlation
   key — the customer's code reads it back only to see "this was golden test
   X," it never has to match it against an external standard or another
   team's naming policy. Per `CLAUDE.md`'s bias against building for
   hypothetical needs: no known customer constraint requires this to be
   renameable today. If a real naming collision shows up later (e.g. an org
   that lints all custom span attributes into a `team.*` namespace), that's a
   small, well-scoped follow-up — not a reason to add a config knob now.

2. **Part 2 ingestion accepts a directory of OTLP export payloads, not a
   single file.** Checked the standard OTel SDK's `BatchSpanProcessor`
   directly (`opentelemetry-sdk`, `trace/export/__init__.py`): it flushes on a
   **timer + batch-size trigger**, not once per run — so a file-based OTLP
   exporter realistically produces multiple export payloads over a run's
   lifetime, and spans belonging to the same trace can legitimately land in
   different flushes. A single-file assumption would silently drop spans that
   arrived in a later flush. Config becomes `otel.input_dir` (a directory of
   one-or-more OTLP JSON export files); ingestion groups spans by `traceId`
   **across all files in the directory** before resolving `bi_evals.golden_id`,
   the same merge-then-group step `otlpReceiver.groupTraces()` does internally
   in Promptfoo's own receiver.

3. **`generated_sql_attribute`'s value is passed through the existing
   `extract_sql()`** (the sqlglot-validated extraction in `provider/contract.py`
   already used by `push`/`api_endpoint`), not required clean by convention.
   One extraction path across all adapters — an attribute that's already
   clean SQL round-trips through unchanged (confirmed behavior after the PR
   #48 rewrite), and prose/fenced values some agents might stuff into the
   attribute get the same tolerant handling every other adapter gets, instead
   of a fourth bespoke SQL-shape assumption.
