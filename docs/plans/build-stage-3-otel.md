# Plan: OTel — SDK trace correlation + batch-ingest adapter

> **Status:** design, not yet built. Two independently-shippable pieces; ship
> Part 1 first (small, reuses the entire existing push pipeline unchanged),
> Part 2 second (larger, new adapter) once Part 1's real-world usage confirms
> the ingestion path is actually needed.

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
agent call is the only party that can tag it. Two required attributes:

1. **`bi_evals.golden_id`** — which golden this trace answers. Must be set on
   the trace's root span (or any span in it) at call time.
2. **`bi_evals.generated_sql`** (config-nameable, default shown — mirrors
   `api_endpoint.response_sql_key`'s existing precedent of "customer names the
   field, config points at it" rather than sniffing) — there is no `gen_ai.*`
   or OpenLLMetry/OpenInference convention for "this tool call's result was
   this specific SQL string," so this one has to be explicit, not inferred.

Tool-call spans for `skill_path_correctness` are the one part that's genuinely
free *if* they already exist — Promptfoo's `trajectory:*` assertions already
prove OTLP span attributes can be read generically via fuzzy key matching
(`/tool.?name|function.?name/i`, `/(^|[._])(arguments|args|input)($|[._])/i`)
rather than a hardcoded per-vendor allowlist, and this stage reuses that same
approach rather than re-inventing it.

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

### Testing

- Unit: `traced_call` opens exactly one span, sets `bi_evals.golden_id`
  correctly, re-raises exceptions from the `with` body unchanged (doesn't
  swallow agent errors), works with an in-memory `TracerProvider` +
  `InMemorySpanExporter` (no real OTLP endpoint needed for tests).
- No live-agent/API-spend test needed — this is pure SDK plumbing.

---

## Part 2 — File-based OTLP batch-ingest adapter (`agent.adapter: otel`)

### Who this is for

A narrower audience than Part 1: a customer whose agent **already ran
independently of any bi-evals-authored loop** — production traffic replayed
against goldens, or a staging pass run by a harness bi-evals never touched —
and the only thing available afterward is a directory of exported OTLP trace
files. If the customer is willing to write a `Runner` loop at all, Part 1 +
`submit()` already covers them with far less new code (no export files, no
parser, no adapter). Ship Part 2 only once real usage shows this narrower
case matters.

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
   `PushReplayAdapter` (`registry.py:246`): pre-load the map once, `produce()`
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
5. **Registry**: one new branch in `build_adapter()` (`registry.py:311`),
   `adapter: Literal[..., "otel"]` added to `AgentConfig` (`config.py:209`).

### What's reused unchanged

`TraceStep`, `AgentResult`, the entire scorer (incl. `skill_path_correctness`
via `scorer/capability.py`'s existing trace-usability classifier — a trace
with no usable tool-call spans is honestly `not_evaluated`, not a failure,
same as today), ingest, report, compare. Only the ingestion/adapter layer is
new — same principle as every prior adapter addition (`push`, `api_endpoint`):
new front door, same pipeline.

### Testing

- Unit: OTLP/JSON parsing (valid + malformed export), span→`TraceStep`
  mapping across a few realistic attribute-name shapes (raw OTel, OpenLLMetry-
  style, OpenInference-style — to prove the fuzzy matching genuinely isn't
  single-vendor), missing `golden_id_attribute` handling (skip + warn, don't
  crash the whole ingest), missing `generated_sql_attribute` (same `not
  evaluated`-style honesty as Stage 2, not a silent empty SQL).
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
