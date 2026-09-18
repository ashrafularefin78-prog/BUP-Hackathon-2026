# API Reference

Base URL (local): `http://127.0.0.1:8000` · Interactive docs: `/docs` (Swagger UI, served by FastAPI)

All request and response bodies are `application/json`. Every object in every
body is **closed**: fields other than those documented below are rejected
(requests) or never emitted (responses). Machine-checkable contracts (JSON
Schema Draft 2020-12):

- Request: [`schemas/request.schema.json`](../schemas/request.schema.json)
- Response: [`schemas/response.schema.json`](../schemas/response.schema.json)

---

## GET /health

Readiness probe. Never gated on external dependencies (the LLM provider being
down must not flip health), so orchestrator health checks stay truthful.

```bash
curl http://127.0.0.1:8000/health
```

```json
{ "status": "ok" }
```

| Code | Meaning |
|---|---|
| 200 | Service is up and ready |

---

## POST /optimize-energy

Interprets 1–3 operator notes and returns a minimum-cost 24-hour schedule.

### Request

```json
{
  "scenario_id": "SAMPLE-01",
  "operator_notes": [
    "Solar output will drop to about 20% from 1 PM to 3 PM due to panel cleaning."
  ],
  "hours": [
    { "hour": 0, "demand_kwh": 210, "solar_kwh": 0, "tariff_bdt_per_kwh": 6.5 },
    { "hour": 1, "demand_kwh": 198, "solar_kwh": 0, "tariff_bdt_per_kwh": 6.2 }
  ],
  "battery": {
    "capacity_kwh": 200,
    "initial_energy_kwh": 100,
    "minimum_energy_kwh": 20,
    "max_charge_kwh_per_hour": 50,
    "max_discharge_kwh_per_hour": 60
  }
}
```

> The `hours` array above is truncated for readability — a valid request has
> **exactly 24 entries covering hours 0–23**.

#### Field reference

| Field | Type | Constraints |
|---|---|---|
| `scenario_id` | string | 1 ≤ length, non-empty |
| `operator_notes` | string[] | **1 to 3** notes, each non-empty (whitespace-only rejected) |
| `hours` | HourInput[] | **exactly 24** unique entries covering hours 0–23 (enforced in the schema itself via pigeonhole `contains` constraints; any order accepted) |
| `battery.capacity_kwh` | number | finite, > 0 |
| `battery.initial_energy_kwh` | number | finite, ≥ 0, ≤ `capacity_kwh` |
| `battery.minimum_energy_kwh` | number | finite, ≥ 0, ≤ `capacity_kwh` |
| `battery.max_charge_kwh_per_hour` | number | finite, ≥ 0 |
| `battery.max_discharge_kwh_per_hour` | number | finite, ≥ 0 |
| `hours[].hour` | integer | 0–23 |
| `hours[].demand_kwh` | number | finite, ≥ 0 |
| `hours[].solar_kwh` | number | finite, ≥ 0 |
| `hours[].tariff_bdt_per_kwh` | number | finite, ≥ 0 |

Additional rules enforced by the schema guard (HTTP 400 before any LLM call):

- Unknown top-level or nested fields are rejected (`extra="forbid"`).
- `NaN` / infinite numbers are rejected.
- Hours may arrive in any order; they are normalized internally.

### Response — 200

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": { "hours": [13, 14], "factor": 0.2 },
      "explanation": "Usable solar is reduced to 0.2 of the forecast during the stated window."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 210.0,
      "solar_used_kwh": 0.0,
      "battery_action": "idle",
      "battery_kwh": 0.0,
      "battery_energy_after_kwh": 100.0
    }
  ],
  "total_grid_kwh": 738.0,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 210.0,
  "plan_summary": "Applies: solar reduced to 0.2 of forecast in hours 13-14. 0 of 1 notes ignored as unrelated. Grid purchase 738 kWh at cost 38365 BDT (peak 210 kWh), battery shifted toward cheaper hours and restored to its initial level by hour 23."
}
```

> `hourly_plan` is truncated — a valid response always has **exactly 24
> entries**, hours 0–23 in order.

#### Field reference

| Field | Type | Notes |
|---|---|---|
| `scenario_id` | string | Echoed from the request |
| `directive_interpretation` | Interpretation[] | Exactly one entry per note, same order |
| `hourly_plan` | PlanEntry[] | Exactly 24 entries, hours 0–23 |
| `total_grid_kwh` | number | Recomputed from `hourly_plan` (never trusted from the solver) |
| `total_cost_bdt` | number | Σ `grid_kwh × tariff`, recomputed from `hourly_plan` |
| `peak_grid_kwh` | number | max over hours, recomputed from `hourly_plan` |
| `plan_summary` | string | One sentence: applied directives, ignored notes, strategy outcome |

**`directive_interpretation[]`**

| Field | Type | Notes |
|---|---|---|
| `note_index` | integer | 0-based position of the source note |
| `applies` | boolean | `true` for the five directive types; `false` only for `no_op` |
| `directive_type` | string | One of the six taxonomy values (below) |
| `structured_adjustment` | object \| null | Shape depends on `directive_type`; `null` iff `no_op` |
| `explanation` | string | One short sentence describing the operational effect |

**Directive taxonomy and adjustment shapes**

| `directive_type` | `structured_adjustment` | Meaning |
|---|---|---|
| `solar_reduction` | `{ "hours": [int], "factor": number }` | Usable solar = forecast × `factor` in those hours; `factor` ∈ [0, 1] is the **fraction remaining** |
| `minimum_battery_reserve` | `{ "hours": [int], "minimum_energy_kwh": number }` | Stored energy may not drop below the floor in those hours |
| `no_charge_window` | `{ "hours": [int] }` | Charging forbidden in those hours |
| `no_discharge_window` | `{ "hours": [int] }` | Discharging forbidden in those hours |
| `max_grid_window` | `{ "hours": [int], "max_grid_kwh": number }` | Grid import ≤ cap in those hours |
| `no_op` | `null` | Note does not affect today's schedule (`applies: false`) |

Time-window convention (applies to every `hours` array): whole hours,
**start-inclusive, end-exclusive** — "1 PM to 3 PM" → `[13, 14]`;
"6 PM until 9 PM" → `[18, 19, 20]`.

**`hourly_plan[]`**

| Field | Type | Notes |
|---|---|---|
| `hour` | integer | 0–23 |
| `grid_kwh` | number | Grid import for the hour, ≥ 0 |
| `solar_used_kwh` | number | Solar actually used, ≤ effective solar for the hour |
| `battery_action` | string | `"charge"` \| `"discharge"` \| `"idle"` |
| `battery_kwh` | number | Energy moved that hour (0 when idle) |
| `battery_energy_after_kwh` | number | Stored energy at the end of the hour |

Invariants the service guarantees on every 200 response (and self-verifies via
the replay validator before sending):

1. Balance: `grid + solar_used + discharge = demand + charge` per hour.
2. Battery transition: `after = before + charge − discharge`, chained from
   `initial_energy_kwh`.
3. Bounds: `minimum_energy_kwh` (and any directive reserve) ≤ `after` ≤
   `capacity_kwh`, every hour.
4. Rates: `battery_kwh` ≤ the respective max charge/discharge rate.
5. Directive compliance: solar caps, reserves, blocked windows, grid caps.
6. Neutrality: `battery_energy_after_kwh` at hour 23 equals
   `initial_energy_kwh`.
7. Totals match recomputation from `hourly_plan` (0.01 tolerance).

### Errors

| Code | Body | Cause |
|---|---|---|
| 400 | `{"detail": "Malformed JSON or structurally invalid request."}` | Schema guard rejection — bad JSON, unknown fields, wrong shapes, invalid values. No LLM call is made. |
| 500 | `{"detail": "The scenario could not be optimized to a valid schedule."}` | LP infeasible or replay validation failed (plan withheld) |
| 500 | `{"detail": "Internal service error."}` | Unexpected error — details logged server-side only, never in the body |

Error bodies are deliberately generic: no stack traces, no provider errors, no
configuration leakage.

### Provider-failure semantics

If the LLM provider is unreachable after the bounded retry budget, the request
does **not** fail: interpretations short-circuit to safe `no_op` entries and a
valid unconstrained-optimal schedule is still returned with HTTP 200.

---

## JSON Schemas (machine-checkable, both directions)

**Request** — `schemas/request.schema.json` is a strict Draft 2020-12 schema
for the request body: closed objects at every level, `operator_notes` 1–3
non-empty strings, `hours` exactly 24 entries with **pigeonhole `contains`
constraints requiring every hour 0–23 at least once (hence exactly once)**,
and positive/ non-negative numeric domains. Test coverage:
`tests/test_request_contract.py` validates every public sample input against
it, proves equivalence with the Pydantic ingress (both gates accept/reject the
same mutations), and runs negative controls (missing fields, wrong types,
duplicate/missing hours, extra fields) plus the non-finite-number layering
(bounds reject `-inf`; the ingress finite-check rejects `nan`/`±inf` → 400).

**Response** — `schemas/response.schema.json` is a strict Draft 2020-12 schema
for the 200
response: closed objects (`additionalProperties: false` at every level),
per-type `structured_adjustment` shapes via `if/then`, hours arrays of 1–24
unique integers in 0–23, `hourly_plan` of exactly 24 entries, enumerated
`directive_type` and `battery_action`, and non-negative numerics. The test
suite validates every sample response against it (and proves it is not vacuous
with negative controls) — see [Testing & Quality](testing.md#judge-side-acceptance).

```bash
curl -s -X POST http://127.0.0.1:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @sample_request.json | python -m json.tool
```
