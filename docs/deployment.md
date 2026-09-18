# Deployment & Operations

## Configuration

All configuration is 12-factor: environment variables, read at request time,
no secrets in the repo or the image (`.env.example` documents names only).

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` \| `anthropic` \| `openai` |
| `LLM_MODEL` | provider default | `claude-sonnet-4-5`, `gpt-4o`, … |
| `LLM_API_KEY` | — | Key for the hosted provider (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` also honored) |
| `LLM_TIMEOUT_SECONDS` | `8` | Per-attempt LLM timeout (clamped 1–20) |
| `LLM_TRANSPORT_RETRIES` | `1` | Bounded retries on transient provider errors (clamped 0–3) |
| `PORT` | `8000` | HTTP port (Docker binds `0.0.0.0:$PORT`) |

Provider notes:

- `mock` is a deterministic offline interpreter for development and the test
  suite. It is **not** a language model and does not satisfy the judging
  requirement by itself.
- Judging configuration: `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-4-5`,
  `LLM_API_KEY=<key>` (or the OpenAI equivalents). Switching providers is an
  environment change only — the adapter interface and reliability path are shared.
- An unavailable provider never takes the API down: requests degrade to safe
  `no_op` interpretations with valid schedules (HTTP 200).

## Local development

```bash
git clone <your-repo-url> gridwise && cd gridwise
python -m venv .venv
# Windows: .venv\Scripts\activate    |    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Smoke tests:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

python scripts/send_sample.py          # all 10 public sample cases
python scripts/send_sample.py SAMPLE-03  # one case
```

Every sample should report a cost equal to the pack's reference optimum within
0.01 BDT.

## Docker

```bash
docker build -t gridwise:latest .
docker run --rm -p 8000:8000 \
  -e LLM_PROVIDER=anthropic -e LLM_MODEL=claude-sonnet-4-5 -e LLM_API_KEY=<key> \
  gridwise:latest
```

Image properties: multi-stage build (wheels compiled once, slim runtime),
non-root user, `HEALTHCHECK` hitting `/health` inside the container, binds
`0.0.0.0:$PORT`, no secrets baked in. This doubles as the submission's
**Docker fallback** deliverable — push the verified image with an exact tag
(not `:latest`).

## Cloud deployment

Two ready configs are included; either platform works for judging. The service
must stay reachable for the whole evaluation window, so avoid sleeping/free
tiers.

### Option A — Fly.io (Machines)

```bash
flyctl launch --no-deploy        # creates the app; keeps fly.toml
flyctl secrets set LLM_PROVIDER=anthropic LLM_MODEL=claude-sonnet-4-5 LLM_API_KEY=<key>
flyctl deploy
flyctl status                    # public URL: https://<app-name>.fly.dev
```

`fly.toml` pins the HTTP service to the container's `$PORT`, runs a TCP check
against `/health`, and sets `min_machines_running = 1` so the API never sleeps.

### Option B — Render (Blueprint)

```bash
# Render Dashboard → New + → Blueprint → pick this repo
# (or: render blueprint launch), then set the LLM_* env vars in the dashboard.
```

`render.yaml` defines the Docker web service with `healthCheckPath: /health`
and `sync: false` secret slots; use an always-on plan (`starter`) — the free
tier sleeps and would fail latency/failure-rate checks.

## Post-deploy verification (required before submission)

Run from **outside** the dev network (e.g., laptop on mobile data):

```bash
python scripts/verify_deployment.py --base-url https://<your-app>.fly.dev
```

The gate checks, in order:

1. `/health` → 200 `{"status":"ok"}` within the readiness window;
2. all 10 public sample cases over real HTTP → 200;
3. recomputed cost vs. the pack's reference optimum (0.01 tolerance);
4. every response validates against `schemas/response.schema.json`;
5. p95 latency mapped to the rubric bands.

Expected final line: `RESULT: PASS — service is judge-ready at this URL`.
After it passes, record the live URL and the Docker fallback reference in
README.md's **Live URL** section.

## Operations reference

### Logging

Single-line structured-ish logs on stdout (uvicorn access logs plus
`gridwise.*` loggers): per-request stage timings, guardrail salvages (info),
demotions with reasons (warning), controlled failures (error). No secrets,
keys, or full request payloads are logged. On Docker/Fly/Render, stdout is
captured by the platform's log stream.

### Health & monitoring

- `/health` is dependency-free by design — it reports process readiness, never
  provider health, so platform checks do not flap when the LLM provider does.
- The Docker `HEALTHCHECK`, Fly TCP check, and Render health check all target it.

### Request-level observability

`pipeline.run_pipeline` measures each stage (`interpret`, `compile`,
`optimize`, `assemble_and_replay`, `total`) in milliseconds and logs them per
scenario — the same numbers `verify_deployment.py` reports at p95.

### Failure playbook

| Symptom | Likely cause | Action |
|---|---|---|
| All notes come back `no_op` under a hosted provider | Bad/missing API key, wrong model name | Check platform logs for the transport warning; re-set secrets; verify model id |
| Intermittent `no_op` demotions in logs | Provider schema drift or truncation | Inspect logged reason strings; tighten `ENTRY_SCHEMA` / prompt; confirm adapter strict mode |
| 500 "could not be optimized" on valid-looking input | Genuinely infeasible directive set (e.g., grid cap < achievable supply) | Expected behavior; verify the scenario constraints |
| 500 with "Internal service error" | Unexpected bug | Server-side log has the traceback (bodies never do) |
| p95 latency creeping up | Provider latency | Check stage timings in logs; the interpret stage dominates; consider a faster model |
| Health check fails on platform | Wrong `$PORT` binding | Confirm the platform injects `PORT` and the container binds `0.0.0.0:$PORT` (Dockerfile default 8000) |

### Resource footprint

The service is a single uvicorn process; HiGHS solves in single-digit
milliseconds and memory usage is dominated by the Python runtime + SciPy.
One small always-on instance is sufficient for the judging load.
