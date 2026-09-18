# GridWise Documentation

LLM-assisted smart-campus energy optimization for the BUP CSE Fest 2026
(Online Preliminary). One HTTP service that interprets natural-language
operator notes with a language model, validates the interpretation with
deterministic guardrails, compiles the directives into a linear program, and
returns a provably valid, minimum-cost 24-hour energy schedule.

## Documentation map

| Document | Contents |
|---|---|
| [Architecture](architecture.md) | System components, pipeline walkthrough, data flow, failure modes, design decisions |
| [API Reference](api.md) | Endpoints, exact request/response schemas, status codes, curl examples, machine-checkable JSON Schema |
| [Interpretation & Guardrails](interpretation.md) | Directive taxonomy, LLM prompting, provider adapters, deterministic validation & salvage, safe-failure policy |
| [Optimization & Replay](optimization.md) | LP formulation, directive compilation, netting pass, replay validator, optimality & complexity notes |
| [Deployment & Operations](deployment.md) | Configuration, local run, Docker, Fly.io / Render deploys, verification, monitoring, troubleshooting |
| [Testing & Quality](testing.md) | Test-suite map, judge-side acceptance gates, adversarial suite, performance results, quality guarantees |
| [Video Script](video-script.md) | 3-minute architecture video: timestamped segments, live demo cues, judge-acceptance evidence |

## Quick links

- **Quickstart:** `pip install -r requirements.txt && uvicorn app.main:app --port 8000` — full instructions in [Deployment](deployment.md#local-development).
- **Verify against the public sample pack:** `python scripts/send_sample.py` ([Testing](testing.md#sample-pack-scripts)).
- **Project layout:** the repository root holds `app/` (service code), `tests/`
  (pytest suite), `scripts/` (operational helpers), `schemas/` (strict request
  and response JSON-Schema contracts), and the platform configs `Dockerfile`,
  `fly.toml`, `render.yaml`. Details in [Architecture](architecture.md#project-layout).

## Source documents

The service implements the hackathon spec: the *Preliminary Problem Statement*,
the *Participant Guide & Evaluation Rubric*, and the *Public Sample Cases* pack
(all included in the repository root). Documentation here describes the
implementation of that spec.
