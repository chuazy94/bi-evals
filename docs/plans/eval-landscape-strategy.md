# Where bi-evals sits in the eval landscape (and the direction that follows)

This doc records a strategic discussion (June 2026): how production AI eval is done
generally, where bi-evals fits, and the direction that falls out of it. It complements
`docs/bi-eval-integration-analysis.md` (the adapter thesis) — that doc is about *how the
agent's output reaches us*; this one is about *which kind of eval we are*.

## Offline vs online eval

Production AI eval happens at two points in the lifecycle:

- **Offline** — before ship, in dev and CI, against a *curated golden dataset* with known
  expected answers. Reproducible, gating, comparable run-to-run. **This is bi-evals.**
- **Online** — after ship, continuously, on a *sampled slice of live production traffic*.
  No ground truth (you didn't write those questions), so it's scored reference-free —
  LLM-as-judge, anomaly checks. Surfaces drift and failure modes the golden set missed.

Every mature stack (Datadog, Braintrust, LangSmith, Langfuse) runs **both as one loop**:
offline gates the build; online watches production; failing production traces feed back to
*grow* the offline set.

## Why bi-evals is — and should stay — an offline tool

1. **It's what we already are.** Golden YAMLs, `reference_sql`, execution-based scoring,
   regression compare, CI gating — all offline machinery.
2. **Our moat only exists offline.** Execution accuracy (run the generated SQL, compare its
   result set to a reference query's) *requires a reference*. Live user questions have none.
   Online bi-evals would be forced onto a different, reference-free scorer — Pillar 2
   Faithfulness — which we have deliberately not built. Online is a different product, not
   an extension of this one.
3. **Offline is the buying wedge.** Teams adopt a CI gate (block bad SQL before ship) before
   they adopt production monitoring.
4. **It matches the North Star.** "Author your first golden tests, quickly" *is* offline
   eval; online has no "author golden tests" step.

This is recorded as a Key Design Decision in `STATUS.md` ("bi-evals is an offline eval tool").

## The crucial BI-specific fact: goldens are hand-authored, and must be

In the chatbot world, the online→offline loop can auto-grow the test set: a failing
production trace becomes a new test case. **In BI this is not true**, because the part that
makes a golden trustworthy — the `reference_sql` answer key — must be written and verified
by a human SME. You cannot harvest a verified-correct answer from production: production is
the *agent's output*, i.e. the thing under suspicion. You can't grade the agent against
itself.

So the most production can ever do for a BI golden is suggest **which questions** are worth
testing (and pre-fill the agent's answer for review). The **expected-answer side stays
hand-authored, always.** The hand-authoring tax is *inherent to trustworthy BI eval* — not
an ergonomics gap to engineer away.

**Consequence:** the friction to attack is **fast hand-authoring**, not auto-generation.
Make the human's job quick (scaffolders, reference-SQL validation, clear errors); don't try
to remove the human. "Production-trace harvesting" (scrape candidate *questions* from logs)
is a genuine but *future* convenience — it shrinks the blank-page problem, it never removes
the SME labeling step.

## Direction (now)

**bi-evals is an offline BI-agent eval tool. The single goal right now is reducing the
friction between "install" and "first trustworthy green run."**

Friction lives in two places, in priority order:

1. **Day-0 setup** — getting to a first run at all. Snowflake key-pair ceremony, 7 env
   vars, no zero-cred path. (`docs/mvp-critique.md` #2.) A bundled no-creds example +
   `doctor` (shipped) attack this. *This gates every adapter equally.*
2. **Authoring the first golden** — once set up, writing `reference_sql` by hand is the long
   tail. (`docs/mvp-critique.md` #7.) A `golden new` scaffolder + reference-SQL validation
   attack this — and per the BI fact above, this *is* the right thing to attack, because the
   human author is here to stay.

## Adapters graded by adoption friction

The adapters all feed the same scorer (`docs/bi-eval-integration-analysis.md`); they differ
only in how much work the customer does to reach a first green run. Our prior docs ranked
them by *invasiveness* and labelled raw-file **push** the "default on-ramp" — but sorted by
*adoption friction*, raw-file push is actually the **highest-effort build-it path**, not the
front door. Re-sorted by "how fast does a new customer get to a green run":

| Option | Customer effort | Fidelity | Fits whom | Verdict |
|---|---|---|---|---|
| **`api_endpoint`** (shipped) | **Lowest** — give a URL; runner owns the loop | response-eval | agent already *is* an HTTP service | **Front door.** Re-message as such |
| **`submit()` SDK / Runner** | **Lowest build-it** — write one `ask()` call; framework owns loop + collection + scoring + file I/O | response-eval | anyone who can call their agent from Python | **Build now.** Biggest friction cut for least work; already in "Onboarding polish" |
| **OTel** (Pivot Phase 6) | **Near-zero *if already instrumented*; large if not** | **highest** — clean structured spans from the real run | shops already emitting OTel GenAI spans | **Right strategic future.** Build after SDK + setup friction land |
| **push (raw `results.jsonl`)** (shipped) | **High** — hand-build the loop + reshape to schema + file | response-eval | "I can only give you a log dump" | **Fallback**, not default. The SDK is the ergonomic front-end to this same path |
| **MCP server** (Adapter B) | **Moderate–high** — modify the *production loop* to call a `submit_for_eval` tool | high (real stack) | MCP-native shops wanting tight glue | **Lowest priority.** A fidelity/integration play, not a friction play; niche + invasive |

**Key reframe:** the `submit()` SDK is the thing that should wear the "default on-ramp"
label — it turns push's painful part (hand-building the loop + JSONL) into the framework's
job, matching how Braintrust (`task`) and DeepEval (black-box `assert_test`) structure
black-box eval. Raw-file push stays as the "I can only give you logs" escape hatch.

**OTel is strategic but not the *adoption* lever today:** its near-zero friction is
conditional on the customer already being instrumented; for everyone else "add OTel" is the
largest lift of any option. It *eliminates* friction for the observable minority — a "win
those accounts for free" play — rather than *reducing* it for the median adopter. Right
direction, later week.

## What is parked (real, later)

Online eval, production-trace harvesting, OTel and MCP adapters, new scoring dimensions.
None of these are wrong — OTel in particular is the right high-fidelity future — they are
simply *after* "shrink install → first green run."
