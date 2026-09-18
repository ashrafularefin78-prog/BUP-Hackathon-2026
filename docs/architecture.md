# Architecture & Pipeline

## Overview

GridWise is a single FastAPI service with two endpoints. Everything between the
HTTP boundary and the response is a fixed, staged pipeline. Each stage has one
job, is independently testable, and has a defined safe-failure behavior.

```
                        ┌─────────────────────────────────────────────┐
                        │                  FastAPI app                │
                        │               (app/main.py)                 │
                        └─────────────────────────────────────────────┘
POST /optimize-energy
         │
         ▼
┌──────────────────┐   malformed JSON or invalid shape ──▶ 400 (no LLM call)
│  Schema guard    │
│ (app/schemas.py) │  Pydantic v2, extra="forbid", finite-number checks,
└──────────────────┘  24 unique hours, initial ≤ capacity, notes 1–3
         │ valid OptimizeRequest
         ▼
┌──────────────────┐   provider outage after retries ──▶ safe all-no_op
│ LLM interpreter  │
│   (app/llm.py)   │  one batched structured-output call for ALL notes
└──────────────────┘  (mock | anthropic | openai — env-configured)
         │ raw candidate entries (untrusted data)
         ▼
┌──────────────────┐   unusable structure ──▶ one corrective retry
│ Guardrail        │
│ validator        │  deterministic checks + conservative salvage;
│(app/guardrails.py)  anything else demotes to safe no_op
└──────────────────┘
         │ validated interpretations
         ▼
┌──────────────────┐
│ Directive        │  effective solar, reserve floor, blocked windows,
│ compiler         │  grid caps — compiled to per-hour bound arrays
│(app/optimizer.py)│
└──────────────────┘
         │ CompiledDirectives
         ▼
┌──────────────────┐   infeasible scenario ──▶ controlled 500
│ LP optimizer     │
│ (HiGHS via scipy)│  120 variables, 49 equations, millisecond solve
└──────────────────┘
         │ OptimizationResult (plan + totals)
         ▼
┌──────────────────┐   any rule violated ──▶ controlled 500 (plan withheld)
│ Replay validator │
│  (app/replay.py) │  independent re-simulation, 0.01 tolerance
└──────────────────┘
         │ verified plan
         ▼
┌──────────────────┐
│ Response         │  totals recomputed from hourly_plan (never from solver)
│ assembler        │  + one-sentence plan_summary
└──────────────────┘
         ▼
   200 application/json
```

## Component responsibilities

| Component | File | Responsibility | On failure |
|---|---|---|---|
| HTTP boundary | `app/main.py` | Routing, error mapping (400/500), generic redacted error bodies | Always returns controlled JSON |
| Schema guard | `app/schemas.py` | Reject structurally invalid requests before any cost is incurred | 400 before LLM call |
| Interpreter | `app/llm.py` | Notes → candidate directive entries (LLM or mock), batched single call | Retries bounded, then `LLMTransportError` |
| Guardrails | `app/guardrails.py` | Validate/salvage/demote candidate entries deterministically | Never raises; worst case all-no_op |
| Compiler + optimizer | `app/optimizer.py` | Directives → LP bounds; solve min-cost schedule | `OptimizationError` → controlled 500 |
| Replay validator | `app/replay.py` | Re-simulate the final plan against all rules | Blocks the response (controlled 500) |
| Orchestrator | `app/pipeline.py` | Stage sequencing, timings, totals derivation, summary text | Degrades safely at each stage |

## The request lifecycle in detail

1. **Schema guard (`app/schemas.py`).** Pydantic models transcribed from the
   Problem Statement. `model_config = ConfigDict(extra="forbid")` closes every
   object; field validators enforce finite non-negative numbers; a model
   validator requires exactly 24 unique hours 0–23 and `initial ≤ capacity`;
   notes are 1–3 non-empty strings. Invalid input never reaches the LLM — this
   is both a correctness and a cost control.

2. **Interpretation (`app/llm.py`).** One call covers all notes (1–3), with the
   battery capacity in the prompt for %-of-capacity conversion. The provider is
   selected by `LLM_PROVIDER`:
   - `anthropic` — Claude Messages API with **forced tool use**; the tool's
     `input_schema` pins the entry shape, `temperature = 0`.
   - `openai` — Chat Completions with **strict `json_schema` response format**.
   - `mock` — a deterministic offline pattern interpreter used for development
     and the test suite. It is documented everywhere as a dev double, not the
     judging interpreter.

   Transport failures raise `LLMTransportError` after a bounded retry budget.

3. **Guardrails (`app/guardrails.py`).** The LLM output is untrusted structured
   data. Validation is pure Python: allowed types, exact adjustment key sets,
   hours unique/ascending/0–23, factor ∈ [0,1], finite non-negative numerics,
   `applies` semantics (`true` for every real directive; `no_op` must have
   `applies=false` and `structured_adjustment=null`). Two conservative repairs
   exist (numeric strings coerced; unsorted-but-valid hours re-sorted);
   `note_index` is always forced to the positional index. Everything else is
   demoted to a safe `no_op`. See
   [Interpretation & Guardrails](interpretation.md).

4. **Directive compilation (`optimizer.compile_directives_from_base`).**
   Interpretations become per-hour arrays: `effective_solar` (base solar ×
   factor), `active_min_reserve` (max of battery floor and directive reserves,
   clamped to capacity), `charge_blocked` / `discharge_blocked` sets, and
   `grid_caps` (the minimum cap wins if two notes cap the same hour).

5. **LP optimization (`optimizer.solve`).** 24 × 5 variables, 49 equality
   constraints, solved by HiGHS through `scipy.optimize.linprog`. Formulation
   and netting pass detailed in [Optimization & Replay](optimization.md).

6. **Replay validation (`replay.replay_plan`).** An independent re-simulation
   of the response's own `hourly_plan` — separate code from the optimizer —
   checks balance, battery transitions and bounds, rate limits, directive
   compliance, end-of-day neutrality, and that all reported totals match
   recomputation, all at the judge's 0.01 tolerance. A failing replay refuses
   the response rather than emit an invalid plan.

7. **Assembly (`pipeline.run_pipeline`).** Totals (`total_grid_kwh`,
   `total_cost_bdt`, `peak_grid_kwh`) are recomputed from `hourly_plan`, never
   taken from the solver object, mirroring how a judge will recompute them.
   `plan_summary` is one sentence: what applies, what was ignored, and the
   strategy outcome.

## Failure modes and degradation policy

| Failure | Detection | Behavior | HTTP |
|---|---|---|---|
| Malformed JSON / invalid shape | Pydantic guard | Generic redacted message | 400 |
| LLM structurally unusable | Guardrails return `None` | One corrective retry, then all-no_op | 200 |
| Individual bad entries | Guardrails | Salvage near-misses; demote the rest to no_op | 200 |
| Provider outage / timeout | `LLMTransportError` | Circuit-breaker: all notes → safe no_op, schedule still valid | 200 |
| Infeasible scenario | `linprog` status | Controlled 500, generic message | 500 |
| Post-solve rule violation | Replay validator | Controlled 500, plan withheld | 500 |
| Unexpected exception | Catch-all handler | Logged server-side, generic body | 500 |

Design rule: **only the optimizer's own infeasibility or a replay failure may
produce a 500.** Every upstream failure degrades to a *valid* schedule.

## Project layout

```
app/
  main.py        FastAPI app: /health, /optimize-energy, 400/500 mapping
  schemas.py     Pydantic ingress guard + response models
  config.py      12-factor settings from environment variables
  llm.py         Interpreter: system prompt, JSON contract, provider adapters, mock
  guardrails.py  Deterministic validator: checks, salvage, no_op demotion
  optimizer.py   Directive compiler + LP formulation + netting
  replay.py      Independent replay validator (0.01 tolerance)
  pipeline.py    Orchestrator: staging, timings, totals, plan summary
schemas/
  request.schema.json   Strict Draft 2020-12 request contract (pigeonhole hours)
  response.schema.json  Strict Draft 2020-12 response contract (judge-mirror)
tests/           77-test pytest suite (see Testing & Quality)
scripts/
  send_sample.py         Run public sample cases against any base URL
  verify_deployment.py   Post-deploy reachability + correctness gate
docs/            This documentation set
Dockerfile       Multi-stage, non-root, HEALTHCHECK, 0.0.0.0:$PORT
fly.toml         Fly.io Machines config
render.yaml      Render Blueprint config
```

## Design decisions and rationale

- **LLM output is never trusted.** The guardrail layer is pure Python with no
  LLM dependency, so its behavior is fully deterministic and unit-testable.
  This is the load-bearing decision: it converts "the LLM might hallucinate"
  from a correctness risk into a bounded, testable degradation path.
- **A real LP, not heuristics.** Battery scheduling over 24 hours with caps,
  floors, and blocked windows is exactly a linear program; `linprog`/HiGHS
  returns a *provably* minimal-cost schedule in milliseconds, which the sample
  pack's reference optima confirm.
- **Independent replay validator.** Re-simulating the emitted plan with
  separate code (not solver outputs) structurally prevents invalid plans from
  leaving the service — the same check the judge will perform.
- **Batched interpretation.** One LLM call per request rather than one per
  note halves worst-case latency and keeps cross-note context available.
- **Provider-agnostic adapter.** `mock` / `anthropic` / `openai` share one
  interface (`build_interpreter`), so switching providers is an environment
  change, not a code change.
- **12-factor configuration.** All settings are environment variables; no
  secrets in the image or the repo (`.env.example` documents names only).
