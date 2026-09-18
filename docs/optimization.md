# Optimization & Replay

The deterministic half of the service: validated directives become a linear
program, and the answer is re-verified by an independent validator before it
may leave the service.

## Directive compilation (`optimizer.compile_directives_from_base`)

Interpretations are compiled into per-hour constraint arrays. Base solar (from
the request) is always the source of truth — a solar reduction is applied as
`base_solar[h] × factor`, never compounded:

| Directive | Compiled effect |
|---|---|
| `solar_reduction` | `effective_solar[h] = base_solar[h] × factor` for each hour |
| `minimum_battery_reserve` | `active_min[h] = max(battery.minimum_energy_kwh, floor)` clamped to `capacity_kwh` |
| `no_charge_window` | hour added to `charge_blocked` |
| `no_discharge_window` | hour added to `discharge_blocked` |
| `max_grid_window` | `grid_caps[h] = min(existing, cap)` — the **stricter** cap wins |

`CompiledDirectives` is the only interface between the interpretation world
and the mathematical world.

## LP formulation (`optimizer.solve`)

For each hour `h ∈ 0..23` there are five variables:

```
grid[h], solar_used[h], charge[h], discharge[h], battery_after[h]
```

— 120 variables total. The objective and constraints mirror the Problem
Statement's Section 09 rules exactly:

```
minimize   Σ_h  grid[h] · tariff[h]

subject to (balance)      grid[h] + solar_used[h] + discharge[h] − charge[h] = demand[h]
           (battery)      battery_after[h] = battery_after[h−1] + charge[h] − discharge[h]
                          battery_after[0] = initial + charge[0] − discharge[0]
           (neutrality)   battery_after[23] = initial_energy_kwh
           (bounds)       0 ≤ solar_used[h] ≤ effective_solar[h]
                          active_min[h] ≤ battery_after[h] ≤ capacity_kwh
                          0 ≤ charge[h] ≤ max_charge_kwh_per_hour      (0 in blocked hours)
                          0 ≤ discharge[h] ≤ max_discharge_kwh_per_hour (0 in blocked hours)
                          0 ≤ grid[h] ≤ grid_caps[h]                   (capped hours only)
```

Every constraint is an equality or a variable bound, so the model is a **pure
linear program** — no integers, no quadratics — and `scipy.optimize.linprog`
solves it with the HiGHS backend. A full 24-hour solve takes single-digit
milliseconds; the LLM call, not the math, dominates request latency.

Properties worth noting:

- **Curtailment is native.** `solar_used ≤ effective_solar` (not `=`) lets the
  optimizer curtail surplus solar, matching the spec (no solar export).
- **Neutrality is a hard equality.** The battery must end where it started, so
  arbitrage plans always "pay back" the battery within the day.
- **Infeasibility is meaningful.** If demand cannot be met under the compiled
  constraints (e.g., a grid cap tighter than the achievable discharge ceiling
  during a deficit), HiGHS reports infeasible and the pipeline surfaces a
  controlled 500 — an honest signal, not a silent best-effort schedule.

### The netting pass (`optimizer._extract_and_net`)

LP solutions may contain a degenerate artifact: charging and discharging in
the same hour simultaneously (e.g., charge 30 + discharge 30). This is
cost-neutral in the objective but violates the "one action per hour" response
shape. The netting pass replaces both flows with their difference
(`net = charge − discharge`) and re-derives the plan:

- `net > ε` → `charge`, `net < −ε` → `discharge`, else `idle`;
- battery state transitions are re-walked from `initial_energy_kwh`;
- rates are re-clamped to the charge/discharge maxima;
- floating-point noise is snapped (`_snap`) so values like `49.999999` become
  `50.0`.

Netting provably preserves balance, battery state, rate-limit compliance, and
cost — it only removes the artifact.

### Output totals

`OptimizationResult` carries the 24 `HourPlan` entries plus totals. The
pipeline recomputes all totals **from the plan itself** (not from the solver
object) before responding — mirroring how a judge recomputes them.

## Replay validation (`app/replay.py`)

The replay validator is a **separate, independent implementation** of the
rulebook (it does not import the optimizer) used as a self-check before any
response is emitted — the same check the judge will run, at the judge's
0.01 tolerance.

Given the request, the final interpretations, and the response's own
`hourly_plan`, it re-simulates hour by hour and flags violations:

| Rule | Check |
|---|---|
| Plan shape | Exactly 24 entries covering hours 0–23; numeric fields parse and are finite |
| Non-negativity | `grid_kwh`, `solar_used_kwh`, `battery_kwh` ≥ 0 |
| Effective solar | `solar_used ≤ base_solar × factor` in reduction hours |
| Grid cap | `grid_kwh ≤ cap` in `max_grid_window` hours |
| Action consistency | `idle` ⇒ `battery_kwh = 0`; rate limits respected |
| Blocked windows | No charging in `no_charge_window`; no discharging in `no_discharge_window` |
| Battery transition | `after = before + charge − discharge`, chained from `initial` |
| Battery bounds | Reserve floor ≤ `after` ≤ capacity (0.01 tolerance) |
| Energy balance | `grid + solar + discharge = demand + charge` (0.01 tolerance) |
| Neutrality | Hour-23 `after` equals `initial_energy_kwh` |
| Totals | `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh` match recomputation |

Result: `(valid, violations)`. The pipeline refuses the response on any
violation (controlled 500) — an invalid plan can never leave the service.

## Why optimality is trustworthy

- The objective and every constraint are linear; HiGHS returns a certified
  optimum, so "minimum cost" is a solver guarantee, not a heuristic claim.
- The test suite pins the full pipeline to the organizer's reference optima:
  all 10 public sample cases match `expected_output.total_cost_bdt` within
  0.01 over real HTTP ([Testing & Quality](testing.md)).
- The same `replay_plan` is used twice: once inside the service (pre-response)
  and once in judge-side acceptance tests against **organizer ground-truth
  directives** — proving the plan is valid even under an independent reading
  of the notes.

## Complexity and performance

| Stage | Complexity | Measured |
|---|---|---|
| Compile directives | O(notes × hours) | negligible |
| LP solve | HiGHS, 120 vars / 49 eq | single-digit ms |
| Netting + totals | O(24) | negligible |
| Replay validation | O(24 × notes) | negligible |
| End-to-end (mock provider) | — | 4–8 ms per request; p95 32 ms over live HTTP |

With a hosted LLM the interpretation call dominates (sub-second to a few
seconds depending on provider/model), still far inside the judge's 5 s p95
band.
