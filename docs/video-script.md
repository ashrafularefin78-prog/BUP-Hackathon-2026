# 3-Minute Architecture Video — Script & Demo Cues

**Runtime:** 3:00 · **Format:** screen recording + voiceover · **Word budget:** ~450 words of VO (≈150 wpm)
Every demo cue below was captured from the running service; re-run the commands
in the cue column immediately before recording so the numbers match live.

> Pre-flight: `pip install -r requirements.txt`, then
> `uvicorn app.main:app --host 127.0.0.1 --port 8000` in a second terminal.
> Recording checklist: terminal font ≥ 16pt, dark theme, browser at /docs for
> one shot, capture at 1080p. Do one dry read of Segment 2 — it is the densest.

---

## Segment 1 — Hook (0:00–0:20)

**On screen:** repo README, then the pipeline diagram from `README.md`/`docs/architecture.md` fading in section by section.

**VO:**
> Campus operators write plain-English notes — "solar is down 80% this
> afternoon." GridWise turns those notes into machine-checkable directives,
> then computes a provably minimum-cost, fully valid 24-hour energy schedule.
> This is our entry for the BUP CSE Fest 2026 preliminary.

---

## Segment 2 — Architecture (0:20–0:55)

**On screen:** the architecture diagram (`docs/architecture.md`); highlight each box as it's named.

**VO:**
> One FastAPI service, six stages. A Pydantic schema guard rejects malformed
> requests with a 400 — before we spend a single token. One batched LLM call
> interprets all operator notes into structured directives. Because LLM output
> is untrusted, deterministic guardrails validate every entry — salvaging
> near-misses and demoting anything doubtful to a safe no-op. Compiled
> constraints feed a linear program solved by HiGHS: energy balance, battery
> limits, directive rules, end-of-day neutrality. An independent replay
> validator re-simulates the plan at the judge's 0.01 tolerance — an invalid
> plan physically cannot leave this service.

---

## Segment 3 — Live demo: interpretation (0:55–1:30)

**On screen / demo cue:** terminal 1:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
python scripts/send_sample.py SAMPLE-01
# SAMPLE-01 (Solar cleaning + distractor): cost 38365.00 BDT
#   (reference 38365.00, OK) grid 2692.5 kWh, peak 188 kWh [9 ms]
```

Then show the SAMPLE-01 response (use the `/docs` "Try it out" or the inline
script from the README) — zoom on the two interpretation entries:

```json
{ "note_index": 0, "applies": true,  "directive_type": "solar_reduction",
  "structured_adjustment": { "hours": [12, 13], "factor": 0.25 }, ... },
{ "note_index": 1, "applies": false, "directive_type": "no_op",
  "structured_adjustment": null, ... }
```

**VO:**
> Here's the live service. Health is dependency-free and instant. Sample-1
> has two notes: a cleaning window stated as "drop to a quarter of output",
> and a deliberate distractor. Note the interpretation: the model resolved
> the window to hours 12 and 13 — start-inclusive, end-exclusive — the factor
> to 0.25 — the fraction *remaining*, not the reduction — and the distractor
> correctly became a no-op.

---

## Segment 4 — Live demo: optimality & robustness (1:30–1:55)

**On screen / demo cue:** terminal 1, then a quick cut:

```bash
python scripts/send_sample.py            # all 10 cases → all "OK" vs reference
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST .../optimize-energy \
  -H "Content-Type: application/json" -d '{"scenario_id":"BAD"}'
# HTTP 400
```

**VO:**
> All ten public cases come back at exactly the organizer's reference optimum —
> that's a real LP, not a heuristic, at five to fourteen milliseconds a call.
> Malformed input? A clean 400 — no crash, no leaked internals. And if the LLM
> provider ever goes down mid-judging, the pipeline degrades to safe no-ops
> and still returns a valid schedule.

---

## Segment 5 — Judge-acceptance evidence (1:55–2:30)

**On screen / demo cue:** terminal 2:

```bash
pytest tests/test_judge_acceptance.py -q
# 14 passed
pytest -q
# 77 passed
python scripts/verify_deployment.py --base-url http://127.0.0.1:8000
# [PASS] SAMPLE-01..10: cost == reference, delta 0.0000 | schema OK
# latency: mean 8 ms, max 28 ms, p95 28 ms -> 3/3 latency credit
# RESULT: PASS — service is judge-ready at this URL
```

**VO:**
> We test from the judge's chair. Fourteen acceptance tests validate every
> response against a strict JSON-Schema contract — closed objects, per-type
> adjustment shapes — and replay every plan against the *organizer's* ground-
> truth directives, not our own interpretation. Negative controls prove the
> schema rejects corrupted responses. Seventy-seven tests total, all passing.
> The deployment verifier re-checks all of it over live HTTP and scores
> latency: p95 of twenty-eight milliseconds, against a five-second budget.

---

## Segment 6 — Deployment & wrap-up (2:30–3:00)

**On screen:** `Dockerfile` + `fly.toml` quick flash, then the scorecard overlay.

**VO:**
> Deployment is one command on Fly or Render — multi-stage Docker, non-root,
> secrets injected at runtime — and the verifier gate must pass before we
> submit. Full docs are in the repo. GridWise: notes to directives, directives
> to a provably optimal plan — verified the way it will be judged. Thank you.

**Scorecard overlay (optional end card):**

| Rubric line | Evidence shown |
|---|---|
| LLM interpretation (25) | Segment 3: factor/window/no_op correctness |
| Directive application (25) | Segments 3–5: guardrails + ground-truth replay |
| Optimization quality (10) | Segment 4: 10/10 reference optima |
| API & schema (10) | Segments 3–5: 400 path + JSON-Schema gate |
| Performance (10) | Segment 5: p95 28 ms → 3/3 band |
| Docker (10) | Segment 6: multi-stage non-root image |
| Docs (10) | Segment 6: `docs/` set + README |

---

## Production notes

- **Re-record cues fresh:** the millisecond timings vary run to run; the costs,
  deltas, pass counts, and p95 band are stable.
- If a hosted LLM key is configured at recording time, Segment 3 doubles as
  the "real model" proof — same expected values, and say so in the VO.
- Keep terminal scrollback cleared; run `clear` between segments so cuts are
  seamless.
- Export captions from this script (the VO text above is caption-ready).
- Source docs for overlays: `docs/architecture.md` (diagram),
  `docs/testing.md` (proof layers), `docs/deployment.md` (deploy commands).
