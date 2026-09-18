# Interpretation & Guardrails

How natural-language operator notes become machine-checkable directives — and
why nothing the LLM says is ever trusted without verification.

## The directive taxonomy

The Problem Statement defines a closed set of six interpretation outcomes. The
service's prompt, guardrails, compiler, and replay validator all enforce this
same closed set — there is no code path that can accept a seventh type.

| # | Type | Adjustment shape | Operational effect |
|---|---|---|---|
| 1 | `solar_reduction` | `{hours, factor}` | Usable solar = forecast × `factor` (factor = **fraction remaining**) |
| 2 | `minimum_battery_reserve` | `{hours, minimum_energy_kwh}` | Stored energy floor in those hours |
| 3 | `no_charge_window` | `{hours}` | Charging forbidden |
| 4 | `no_discharge_window` | `{hours}` | Discharging forbidden |
| 5 | `max_grid_window` | `{hours, max_grid_kwh}` | Grid import cap |
| 6 | `no_op` | `null` | No schedule effect (`applies: false`) |

Time convention throughout: whole hours, **start-inclusive / end-exclusive** —
"1 PM to 3 PM" → `[13, 14]`, "6 PM until 9 PM" → `[18, 19, 20]`.

## The interpretation stage (`app/llm.py`)

### One batched call per request

All notes (1–3) are interpreted in a **single** LLM call with the battery
capacity included in the user prompt:

```
Battery capacity for %-of-capacity conversions: 200 kWh.
Operator notes (2):
[0] Solar output will drop to about 20% from 1 PM to 3 PM ...
[1] Keep at least 50% of the battery capacity from 6 PM until 9 PM ...

Return one directive entry per note, in note_index order.
```

Batching halves worst-case latency versus per-note calls and gives the model
cross-note context (the sample pack contains deliberate distractor notes that
only make sense next to their neighbors).

### The system prompt encodes the contract

`SYSTEM_PROMPT` pins, in order: the closed taxonomy with exact JSON shapes;
the start-inclusive/end-exclusive window convention with worked examples; the
factor-is-fraction-remaining rule ("drop to 20%" **and** "80% reduction" both
→ 0.2); %-of-capacity → kWh conversion using the scenario's battery capacity;
the per-hour cap rule for `max_grid_window`; the no_op policy (administrative,
ambiguous, or off-schedule notes); and hard prohibitions — never invent
numbers, types, demand, tariffs, or battery parameters.

### Structured output is enforced provider-side

Both hosted adapters force the model into a JSON contract at the API level, so
malformed prose cannot be the failure mode:

| Provider | Mechanism | Notes |
|---|---|---|
| `anthropic` | Claude Messages API, **forced tool use** (`tool_choice`), `temperature: 0`, shared `ENTRY_SCHEMA` as the tool's `input_schema` | Response is read from the `tool_use` block |
| `openai` | Chat Completions, `response_format: json_schema`, **strict mode**, `temperature: 0` | Strict mode requires all properties in `required`; schema is adapted per provider quirk |
| `mock` | Deterministic pattern interpreter (see below) | Dev/tests only — **not** a language model and not the judging interpreter |

The JSON contract (`ENTRY_SCHEMA`) allows only the six enum types, hours as
1–24 unique integers in 0–23, `factor` in [0, 1], non-negative kWh values,
`structured_adjustment` as one of the four adjustment shapes or `null`, and no
additional properties anywhere.

### Reliability path

Per attempt: `LLM_TIMEOUT_SECONDS` (default 8, clamped 1–20). Transient
failures (HTTP 429/5xx/provider disconnect) are retried up to
`LLM_TRANSPORT_RETRIES` (default 1, clamped 0–3) with linear backoff. Sustained
failure raises `LLMTransportError`, which the pipeline catches and converts to
the safe all-no_op path (see [Architecture](architecture.md#failure-modes-and-degradation-policy)).

### The mock interpreter

`MockInterpreter` is a deterministic regex/pattern parser covering the
semantic patterns exercised by the public sample pack: word-number clocks
("one until three"), `noon`/`midnight`, am/pm inheritance ("1-3 PM"),
connector detection ("to/until/till/through/and", hyphens), fraction words
("one third", "half"), %-of-capacity reserve conversion, and cap phrasings
("must not exceed", "capped at", "limit is"). It is clearly labeled in code,
README, and docs as a development double — hidden paraphrase evaluation
requires a hosted provider.

## The guardrail stage (`app/guardrails.py`)

**Threat model:** the LLM output is untrusted structured data. It may be the
wrong shape, reference impossible hours, carry a factor of 4, claim an
invented directive type, or disagree about note indices. None of that may
reach the optimizer, crash the service, or silently distort the schedule.

### Properties guaranteed

1. **Deterministic** — pure Python, no LLM involvement, fully unit-testable.
2. **Never crashes** — malformed output demotes; it never raises.
3. **Never invents** — demoted notes become `no_op`; nothing is guessed.
4. **Positional mapping** — `note_index` is forced to the entry's position; the
   LLM's own labels are used only when they form a complete, unique 0..N−1
   permutation (i.e., a provider that demonstrated a coherent mapping).

### Validation checks (per entry)

| Check | Rule |
|---|---|
| Object shape | Entry must be a dict with `note_index`, `applies`, `directive_type` |
| `note_index` | Integer in `[0, note_count)` |
| `applies` | Boolean; `true` for the five real directives, `false` **only** for `no_op` |
| `directive_type` | Must be one of the six taxonomy values |
| `no_op` hygiene | `applies=false` and `structured_adjustment=null` required |
| Adjustment object | Must be a dict with **exactly** the type's required keys — no extras, none missing |
| `hours` | Non-empty list of unique integers, each 0–23, strictly ascending |
| `factor` | Finite, 0.0 ≤ factor ≤ 1.0 |
| `minimum_energy_kwh`, `max_grid_kwh` | Finite, ≥ 0 |

### Conservative salvage

Two near-miss classes are repaired rather than discarded (each repair is
re-validated through the full check pipeline before acceptance):

- **Numeric strings** — `"0.25"`, `"120"` for the type's numeric key are
  coerced to floats.
- **Unsorted hours** — unique valid integers in the wrong order are sorted.

Everything else — wrong types, out-of-range values, unknown keys, missing
fields, negative kWh, factor > 1 — is **demoted to a safe `no_op`** with an
explanation stating why the note does not affect today's schedule. The demotion
reason is logged server-side.

### Index-mapping policy (hybrid)

If the candidate list carries `note_index` labels forming a complete unique
permutation of 0..N−1, label-based mapping is used (trusting a provider that
demonstrated coherence). Otherwise entries map **positionally** and every
`note_index` is overwritten with its position — a wrong/missing/garbage label
can never mis-route a directive. A short candidate list simply demotes the
missing positions; it is never discarded for wrong arity.

## End-to-end example

Note: *"Panel washing from noon until 2 PM will leave roughly one fifth of
normal solar output."*

1. Interpreter (hosted or mock) emits:
   `{note_index: 0, applies: true, directive_type: "solar_reduction",
     structured_adjustment: {hours: [12, 13], factor: 0.2}, explanation: "..."}`
2. Guardrails: type allowed ✓, adjustment keys exactly `{hours, factor}` ✓,
   hours unique/ascending/0–23 ✓, factor ∈ [0,1] ✓ → passes unchanged.
3. Compiler: `effective_solar[12] = effective_solar[13] = base_solar × 0.2`.
4. Optimizer: schedules around the reduced solar.
5. Replay: re-checks `solar_used ≤ effective_solar` per hour before responding.

Note: *"The staff canteen will serve a special lunch menu tomorrow."* →

1. Interpreter emits `no_op` (off-schedule administrative note).
2. Guardrails: `applies=false`, `structured_adjustment=null` ✓.
3. Compiler: skips it; the LP optimizes the unconstrained scenario.
