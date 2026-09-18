# GridWise — LLM-Assisted Smart Campus Energy Optimization

BUP CSE Fest 2026 Hackathon (Online Preliminary) — one HTTP API service that interprets
natural-language campus operator notes with an LLM, validates the interpretation with
deterministic guardrails, compiles the resulting directives into a linear program, and
returns a provably valid, minimum-cost 24-hour energy schedule.

## Endpoints

| Endpoint              | Purpose                                           | Success |
|-----------------------|---------------------------------------------------|---------|
| `GET /`               | Demo playground UI (self-contained `index.html`)  | `200` HTML |
| `GET /health`         | Readiness probe for the judging harness           | `200 {"status":"ok"}` |
| `POST /optimize-energy` | Interpret operator notes + return a 24-hour plan | `200` with the response schema below |

## Architecture

```
Client JSON ─▶ [Pydantic schema guard]        malformed/invalid ─▶ 400
            ─▶ [LLM Interpreter]              one batched structured-output call
            ─▶ [Guardrail Validator]          deterministic; salvage + safe no_op demotion
            ─▶ [Directive Compiler]           effective solar, reserve floor, blocked
                                              windows, grid caps (per-hour bounds)
            ─▶ [LP Optimizer (HiGHS)]         min Σ grid·tariff subject to Section-09 rules
            ─▶ [Replay Validator]             independent judge-mirror re-simulation
            ─▶ [Response Assembler]           totals recomputed from hourly_plan
```

- **LLM role (mandatory):** the language model converts each operator note into one
  machine-checkable directive entry (type, hours, numeric adjustment). Its structured
  output is the *only* source of directives and is always treated as untrusted data.
- **Guardrails:** pure-Python checks — allowed types, one entry per note in `note_index`
  order, hours unique/ascending/0–23, factor ∈ [0,1], non-negative finite numerics,
  `applies` semantics, exact adjustment shapes. Near-misses are conservatively salvaged
  (numeric strings coerced, unsorted hours re-sorted, index forced positional); anything
  else is demoted to a safe `no_op`. Malformed output never crashes the service and never
  invents a directive.
- **Optimizer:** continuous LP (5 variables × 24 hours) solved by HiGHS via
  `scipy.optimize.linprog` — energy balance, battery transition/bounds/rate limits,
  directive constraints, end-of-day neutrality, objective `Σ grid·tariff`. A deterministic
  netting pass removes the degenerate simultaneous charge/discharge artifact.
- **Replay validator:** independently re-simulates the returned plan (0.01 tolerance) and
  blocks the response if any rule fails; totals are always derived from `hourly_plan`.

## Documentation

Full documentation lives in [`docs/`](docs/index.md):

| Document | Contents |
|---|---|
| [Architecture & Pipeline](docs/architecture.md) | Components, request lifecycle, failure modes, design rationale |
| [API Reference](docs/api.md) | Endpoints, exact schemas, status codes, invariants, curl examples |
| [Interpretation & Guardrails](docs/interpretation.md) | Directive taxonomy, LLM prompting & adapters, validation & salvage policy |
| [Optimization & Replay](docs/optimization.md) | LP formulation, netting pass, replay validator, performance |
| [Deployment & Operations](docs/deployment.md) | Configuration, local run, Docker, Fly.io/Render, verification, troubleshooting |
| [Testing & Quality](docs/testing.md) | Test-suite map, judge-side acceptance gates, performance results |
| [Video Script](docs/video-script.md) | 3-minute architecture video script with live demo cues |

## Local quickstart (clean environment)

```bash
git clone <your-repo-url> gridwise && cd gridwise
python -m venv .venv
# Windows: .venv\Scripts\activate    |    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

Run all 10 public sample cases against the service:

```bash
python scripts/send_sample.py
# or a single case:
python scripts/send_sample.py SAMPLE-03
```

One-off sample request (first public case, solar-cleaning directive):

```bash
python - <<'PY'
import json, urllib.request
pack = json.load(open("BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json", encoding="utf-8"))
case = pack["cases"][0]
req = urllib.request.Request(
    "http://127.0.0.1:8000/optimize-energy",
    data=json.dumps(case["input"]).encode(),
    headers={"Content-Type": "application/json"},
)
resp = json.load(urllib.request.urlopen(req))
print(json.dumps(resp["directive_interpretation"], indent=2))
print("total_cost_bdt:", resp["total_cost_bdt"], "(reference:", case["expected_output"]["total_cost_bdt"], ")")
PY
```

Expected result: cost matches the reference optimum (38365 BDT for SAMPLE-01) within 0.01.

## Configuration (environment variables — names only, no secrets committed)

| Variable              | Default | Purpose |
|-----------------------|---------|---------|
| `LLM_PROVIDER`        | `mock`  | `mock` \| `anthropic` \| `openai` |
| `LLM_MODEL`           | provider default | Model identifier (e.g. `claude-sonnet-4-5`, `gpt-4o`) |
| `LLM_API_KEY`         | —       | Key for the hosted provider (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` also honored) |
| `LLM_TIMEOUT_SECONDS` | `8`     | Per-attempt LLM call timeout (1–20 s) |
| `LLM_TRANSPORT_RETRIES` | `1`   | Bounded retries on transient provider errors |
| `PORT`                | `8000`  | HTTP port (Docker binds `0.0.0.0:$PORT`) |

### Model / provider notes (important)

- The **`mock` provider is a deterministic offline interpreter used for development and
  the test suite only**. It pattern-matches the public sample semantics and is NOT a
  language model; it does not satisfy the judging requirement by itself.
- For judging, set e.g. `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-4-5`,
  `LLM_API_KEY=<key>`. The Anthropic adapter uses forced tool use (strict JSON schema,
  temperature 0); the OpenAI adapter uses strict `json_schema` response format. The LLM
  is invoked once per request with all notes batched.
- Reliability path: bounded retries with backoff → on sustained provider failure the
  request short-circuits to safe `no_op` interpretations and still returns a valid
  schedule (the service never crashes on provider errors).

## Docker fallback

```bash
docker build -t gridwise:latest .
docker run --rm -p 8000:8000 -e LLM_PROVIDER=anthropic -e LLM_MODEL=claude-sonnet-4-5 -e LLM_API_KEY=<key> gridwise:latest
```

- Multi-stage build, non-root user, binds `0.0.0.0:$PORT`, `HEALTHCHECK` on `/health`.
- No secrets are baked into the image; all configuration is injected at run time.

## Cloud deployment & live URL

Two ready-to-use deploy configs are included — either platform works for judging
(choose one; the service must stay reachable for the whole evaluation window):

### Option A — Fly.io (Machines)

```bash
flyctl launch --no-deploy        # create the app; keeps fly.toml
flyctl secrets set LLM_PROVIDER=anthropic LLM_MODEL=claude-sonnet-4-5 LLM_API_KEY=<key>
flyctl deploy
flyctl status                    # shows the public URL: https://<app-name>.fly.dev
```

`fly.toml` pins the HTTP service to the container's `$PORT`, runs a TCP check
against `/health`, and keeps `min_machines_running = 1` so the API never sleeps
during judging.

### Option B — Render (Blueprint)

```bash
# Render Dashboard -> New + -> Blueprint -> pick this repo (or: render blueprint launch)
# Then set LLM_PROVIDER / LLM_MODEL / LLM_API_KEY in the dashboard (or: render env set ...)
```

`render.yaml` defines the Docker web service with `healthCheckPath: /health` and
`sync: false` secrets. Use an always-on plan (`starter`) — the free tier sleeps
and would fail the p95/failure-rate checks.

### Post-deploy verification (required before submission)

Run the reachability verifier from OUTSIDE your dev environment (e.g. laptop on
mobile data) against the public URL:

```bash
python scripts/verify_deployment.py --base-url https://<your-app>.fly.dev
```

It checks: `/health` readiness, all 10 public samples over real HTTP (each input
pre-validated against `schemas/request.schema.json`), recomputed
cost vs. the reference optimum, strict JSON-Schema conformance of every response,
and p95 latency against the rubric bands. Expected output ends with
`RESULT: PASS — service is judge-ready at this URL`.

### Live URL

> **Live URL:** _pending first deploy — record it here after `flyctl deploy` /_
> _Render Blueprint creation, once `verify_deployment.py` passes from an external network._
>
> **Docker fallback:** _push the verified image to Docker Hub / GHCR with an exact
> tag (not `:latest`) and record the pullable reference here._

## Tests

```bash
pytest            # 162 tests
```

Coverage: every guardrail rejection path, LP unit tests with hand-verified optima,
**all 10 public sample cases end-to-end** (interpretation ground truth + schedule
validity + cost equal to the reference optimum within 0.01), robustness (malformed
JSON, invalid structures, simulated provider outage), paraphrase variants of every
directive type (skipped automatically when a hosted provider is configured), and
**judge-side acceptance tests**: every response is validated against a strict
Draft 2020-12 JSON Schema (`schemas/response.schema.json`, closed objects, per-type
adjustment shapes) and its `hourly_plan` independently replayed against the organizer
ground-truth directives — with negative controls proving the schema rejects broken
responses (extra fields, wrong adjustment shapes, out-of-range hours, negative values,
unknown battery actions, missing required fields). The request side mirrors this:
every sample input validates against `schemas/request.schema.json` (closed objects,
pigeonhole-enforced 24 unique hours 0–23), with equivalence tests against the
Pydantic ingress and its own negative controls (`tests/test_request_contract.py`).
An adversarial note suite — 50 hidden-style paraphrases across all six directive
types (`tests/fixtures/adversarial_notes.json`) — is scored through the full
interpretation path by `tests/test_adversarial_notes.py` and over live HTTP by
`scripts/run_adversarial_suite.py` (exit code 0 = 50/50, usable as a release gate).

## Known limitations

- The mock provider only recognizes the semantic patterns exercised by the public pack;
  hidden paraphrases require a hosted LLM (see above) — that is the intended production path.
- Solar is curtailed (no export), per the Problem Statement; the LP is continuous
  (kWh are divisible), matching the spec's tolerance-based checks.
- Battery efficiency is 1.0 (no round-trip loss) — the spec's balance equations define
  lossless accounting.
- Negative-price tariffs are not modeled (tariffs are non-negative by schema).

## Credits

- FastAPI, Pydantic, SciPy (HiGHS), NumPy, httpx, uvicorn, pytest — open-source libraries.
- Built with AI coding assistance (Codebuff), credited per the Participant Guide; core
  architecture and logic are the team's own work.
