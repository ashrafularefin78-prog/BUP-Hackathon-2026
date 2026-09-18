# Testing & Quality

The suite is the correctness story of the project: 162 tests, all runnable
offline in about three seconds, covering every layer from regex guardrails to
end-to-end HTTP behavior against the organizer's own sample pack.

```bash
pytest            # 162 tests, ~3 s
```

## Test-suite map

| File | Layer | What it proves |
|---|---|---|
| `tests/test_guardrails.py` | `app/guardrails.py` | Every rejection path, salvage rules, hybrid index mapping, no_op demotion |
| `tests/test_optimizer.py` | `app/optimizer.py` | LP unit tests with hand-verified optima; directive compilation; netting; degenerate-optimum handling |
| `tests/test_api_public_samples.py` | Full pipeline via HTTP | All 10 public sample cases: interpretation ground truth, schedule validity, cost == reference optimum (0.01) |
| `tests/test_robustness.py` | HTTP boundary + pipeline | Malformed JSON → 400; invalid structures → 400; simulated provider outage → valid all-no_op 200 |
| `tests/test_paraphrase.py` | Interpretation layer | Same directive reworded (units, synonyms, time formats) → same structured result; auto-skips under hosted providers |
| `tests/test_judge_acceptance.py` | Whole response, judge's view | Strict JSON-Schema conformance + independent replay against organizer ground truth + schema negative controls |
| `tests/test_request_contract.py` | Request contract, ingress equivalence | Strict request-schema conformance of every sample input, Pydantic↔schema equivalence proofs, request-side negative controls |
| `tests/test_adversarial_notes.py` | Interpretation, adversarial | 50 hidden-style paraphrases across all six directive types scored against exact ground truth (type, hours, value) |
| `tests/conftest.py` | — | Session-scoped sample-pack loader and API client (pinned to the mock provider) |

## The three layers of proof

### 1. Unit level — deterministic components

- **Guardrails:** parametrized rejections (unknown type, factor > 1, out-of-range
  or non-ascending hours, extra adjustment keys, `no_op` with an adjustment,
  non-boolean `applies`, out-of-range indices) plus salvage cases (numeric
  strings, unsorted hours) and the index-mapping policy (labels trusted only as
  a complete permutation; otherwise forced positional).
- **Optimizer:** hand-computed optima for scenario families — flat tariffs,
  peak-shaving with a midday solar valley, reserve floors, blocked windows,
  grid caps — asserting exact totals; plus infeasible-scenario behavior
  (cap below achievable supply raises `OptimizationError`, surfacing as 500).

### 2. Integration level — the public sample pack

Every one of the 10 organizer-provided cases is sent through the real HTTP
app (ASGI in-process) and checked three ways:

1. **Interpretation ground truth** — `directive_interpretation` matches the
   pack's expected types, hours, and values (including the factor-is-remaining
   and %-of-capacity conventions).
2. **Schedule validity** — the plan passes the replay validator.
3. **Optimality** — `total_cost_bdt` equals `expected_output.total_cost_bdt`
   within 0.01.

### 2b. Request-side contract (`tests/test_request_contract.py`)

The request direction gets the same treatment as the response:

- **Strict schema gate** — every public sample input validates against
  [`schemas/request.schema.json`](../schemas/request.schema.json) (Draft
  2020-12): closed objects everywhere, notes 1–3 non-empty strings, exactly
  24 hour entries with **pigeonhole `contains` constraints** forcing every
  hour 0–23 to appear (⇒ exactly once), positive capacity, non-negative
  numerics.
- **Ingress equivalence** — the schema and the Pydantic ingress
  (`app.schemas.OptimizeRequest`) are proven to accept/reject the same
  shapes: a canonical request passes both; 15 parametrized mutations (bad
  hours, negative values, empty/oversized notes, zero capacity) fail **both**
  gates; extra fields fail both; unsorted hours pass both.
- **Non-finite layering** — `-inf` violates the schema's numeric bounds;
  `nan`/`+inf` are comparison-blind spots that the ingress finite-check
  rejects — over real HTTP as raw `NaN`/`Infinity` literals (which the
  schema, bound to valid JSON, could never see) → 400.
- **Negative controls** — missing top-level/nested fields, wrong types,
  structurally broken hour entries all fail the schema.

### 2c. Adversarial note suite (`tests/test_adversarial_notes.py`)

50 hidden-style operator notes — 8 per schedulable directive type plus 10
no-op distractors (`tests/fixtures/adversarial_notes.json`) — rehearse the
rubric's hidden paraphrase evaluation: word-number clocks, word percentages
and kWh values, `o'clock`/day-part phrasing, possessive capacity ("the
battery's capacity"), midnight-crossing windows, and administrative traps
with temporal decoys. Each note asserts **exact** type, hours, and value
(midnight-crossing windows compared rotation-equivalently, since guardrails
force ascending hours). The same fixture is scored over live HTTP by
`scripts/run_adversarial_suite.py --base-url <url>` (per-type scorecard,
`--json` mode, non-zero exit on any miss). Design note: the suite's canonical
scenario is deliberately built so every directive is *feasible* — an
infeasible directive (e.g., an evening grid cap below achievable supply, or a
reserve floor above the initial energy under neutrality) correctly yields an
HTTP 500, which would mask the interpretation being scored.

### 3. Judge-side acceptance (`tests/test_judge_acceptance.py`)

The same checks a judge performs, run against our own responses:

- **Strict schema gate** — the raw response JSON validates against
  [`schemas/response.schema.json`](../schemas/response.schema.json) (Draft
  2020-12): closed objects at every level, per-type `structured_adjustment`
  shapes via `if/then`, hours arrays 1–24 unique ints in 0–23, enumerated
  `directive_type`/`battery_action`, non-negative numerics, exactly 24
  `hourly_plan` entries. A meta-test cross-checks the schema file against the
  sample pack's own `_meta.schema_notes` required-field lists, so the contract
  cannot silently drift from the spec.
- **Independent replay gate** — each returned `hourly_plan` is replayed by a
  fresh validator instance against the **organizer's ground-truth directives**
  parsed from the pack (not our service's reported interpretations), mirroring
  the guide's "the judge replays using the true directive" rule — including
  per-hour `solar_used ≤ base × factor` checks for `solar_reduction`.
- **Negative controls** — seven deliberately corrupted responses (extra
  top-level field, wrong adjustment shape, `no_op` with an adjustment,
  `hour: 24`, negative `grid_kwh`, unknown `battery_action`, deleted required
  field) all raise `ValidationError`, proving the schema is not vacuous.

## Scripts

### `scripts/send_sample.py`

Runs the public sample pack against any live base URL (default
`http://127.0.0.1:8000`, override with `--base-url`):

```bash
python scripts/send_sample.py                  # all 10 cases
python scripts/send_sample.py SAMPLE-03        # one case
```

Reports per-case cost vs. the reference optimum and fails non-zero on any
mismatch.

### `scripts/verify_deployment.py`

The post-deploy gate ([Deployment & Operations](deployment.md#post-deploy-verification-required-before-submission)):
health, all 10 samples over real HTTP, cost deltas, schema conformance, and
p95 latency mapped to the rubric bands.

## Performance results

| Measurement | Result | Rubric band |
|---|---|---|
| End-to-end per request (mock provider, in-process) | 4–8 ms | top band (p95 ≤ 5 s) |
| Live HTTP over all 10 samples | p95 ≈ 32 ms | top band |
| LP solve alone | single-digit ms | — |
| Full pytest suite | ~1 s offline | — |

With a hosted LLM, the interpretation call dominates latency but remains
orders of magnitude inside the 5 s p95 requirement.

## Quality guarantees (what the tests collectively prove)

1. **No invalid plan can be returned** — replay validation blocks the response,
   and the negative controls prove the schema would reject one anyway.
2. **Optimality** — every public case hits the organizer's reference optimum.
3. **Determinism of the safety net** — guardrail behavior is fully specified
   and unit-tested; the LLM cannot crash or corrupt the schedule.
4. **Provider independence** — the API contract, guardrails, optimizer, and
   replay are identical across `mock` / `anthropic` / `openai`; only the
   interpretation call differs.
5. **Spec conformance is machine-checked twice** — by Pydantic at ingress and
   by the JSON Schema at egress.
