"""Adversarial note-generation suite: score the live interpretation endpoint.

Posts each of the 50 hidden-style operator notes (tests/fixtures/adversarial_notes.json)
as a single-note request to a running /optimize-energy endpoint, then scores the
returned directive_interpretation against the fixture's ground truth:

  - directive_type must match exactly,
  - hours must match exactly (whole hours, start-inclusive/end-exclusive),
  - the type's numeric value must match within 1e-3.

No-op ground truth means the note must come back as applies=false / no_op.

Usage:
  python scripts/run_adversarial_suite.py                       # http://127.0.0.1:8000
  python scripts/run_adversarial_suite.py --base-url https://gridwise.example.dev
  python scripts/run_adversarial_suite.py --json                # machine-readable
  LLM_PROVIDER=anthropic LLM_API_KEY=... python scripts/run_adversarial_suite.py

Exit code 0 only when every note passes — suitable as a release gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "adversarial_notes.json"

VALUE_KEY = {
    "solar_reduction": "factor",
    "minimum_battery_reserve": "minimum_energy_kwh",
    "max_grid_window": "max_grid_kwh",
}

# Canonical scenario designed so EVERY directive in the suite is feasible:
#   - evening demand stays below the tightest cap (90 kWh) plus discharge (60)
#     plus evening solar, so max_grid_window notes never force infeasibility;
#   - initial energy (150) exceeds every reserve floor, so reserve windows that
#     include hour 23 never conflict with end-of-day neutrality.
# (A genuinely infeasible scenario makes the service return a correct HTTP 500
# — which would mask the interpretation being scored.)
HOURS = [
    {"hour": 0, "demand_kwh": 120, "solar_kwh": 0, "tariff_bdt_per_kwh": 6.5},
    {"hour": 1, "demand_kwh": 112, "solar_kwh": 0, "tariff_bdt_per_kwh": 6.2},
    {"hour": 2, "demand_kwh": 105, "solar_kwh": 0, "tariff_bdt_per_kwh": 6.0},
    {"hour": 3, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 5.8},
    {"hour": 4, "demand_kwh": 102, "solar_kwh": 0, "tariff_bdt_per_kwh": 5.9},
    {"hour": 5, "demand_kwh": 110, "solar_kwh": 0, "tariff_bdt_per_kwh": 6.4},
    {"hour": 6, "demand_kwh": 130, "solar_kwh": 10, "tariff_bdt_per_kwh": 8.0},
    {"hour": 7, "demand_kwh": 140, "solar_kwh": 40, "tariff_bdt_per_kwh": 10.0},
    {"hour": 8, "demand_kwh": 150, "solar_kwh": 90, "tariff_bdt_per_kwh": 12.0},
    {"hour": 9, "demand_kwh": 155, "solar_kwh": 150, "tariff_bdt_per_kwh": 14.0},
    {"hour": 10, "demand_kwh": 158, "solar_kwh": 200, "tariff_bdt_per_kwh": 15.0},
    {"hour": 11, "demand_kwh": 156, "solar_kwh": 230, "tariff_bdt_per_kwh": 15.5},
    {"hour": 12, "demand_kwh": 152, "solar_kwh": 240, "tariff_bdt_per_kwh": 15.0},
    {"hour": 13, "demand_kwh": 155, "solar_kwh": 235, "tariff_bdt_per_kwh": 14.5},
    {"hour": 14, "demand_kwh": 157, "solar_kwh": 210, "tariff_bdt_per_kwh": 14.0},
    {"hour": 15, "demand_kwh": 150, "solar_kwh": 170, "tariff_bdt_per_kwh": 15.5},
    {"hour": 16, "demand_kwh": 145, "solar_kwh": 110, "tariff_bdt_per_kwh": 17.0},
    {"hour": 17, "demand_kwh": 150, "solar_kwh": 50, "tariff_bdt_per_kwh": 20.0},
    {"hour": 18, "demand_kwh": 158, "solar_kwh": 10, "tariff_bdt_per_kwh": 26.0},
    {"hour": 19, "demand_kwh": 160, "solar_kwh": 0, "tariff_bdt_per_kwh": 30.0},
    {"hour": 20, "demand_kwh": 156, "solar_kwh": 0, "tariff_bdt_per_kwh": 27.0},
    {"hour": 21, "demand_kwh": 148, "solar_kwh": 0, "tariff_bdt_per_kwh": 19.0},
    {"hour": 22, "demand_kwh": 130, "solar_kwh": 0, "tariff_bdt_per_kwh": 11.0},
    {"hour": 23, "demand_kwh": 115, "solar_kwh": 0, "tariff_bdt_per_kwh": 7.5},
]
BATTERY = {
    "capacity_kwh": 200.0,
    "initial_energy_kwh": 150.0,
    "minimum_energy_kwh": 20.0,
    "max_charge_kwh_per_hour": 50.0,
    "max_discharge_kwh_per_hour": 60.0,
}


def build_request(note: str) -> dict:
    return {
        "scenario_id": "ADVERSARIAL",
        "operator_notes": [note],
        "hours": [dict(h) for h in HOURS],
        "battery": dict(BATTERY),
    }


def _normalize_wrap(hours: list[int]) -> list[int]:
    """Rotate a midnight-crossing window so its minimum comes first."""
    i = hours.index(min(hours))
    return hours[i:] + hours[:i]


def score_note(expected: dict | None, entries: list[dict]) -> tuple[bool, str]:
    """Score one note's interpretation entries against ground truth."""
    entry = next((e for e in entries if e.get("note_index") == 0), None)
    if entry is None:
        return False, "no entry for note_index 0"

    if expected is None:
        if entry.get("directive_type") == "no_op" and entry.get("applies") is False:
            return True, ""
        return False, f"expected no_op, got {entry.get('directive_type')} (applies={entry.get('applies')})"

    if entry.get("directive_type") != expected["directive_type"]:
        return False, f"expected {expected['directive_type']}, got {entry.get('directive_type')}"
    if entry.get("applies") is not True:
        return False, "expected applies=true"

    adj = entry.get("structured_adjustment") or {}
    got_hours, want_hours = adj.get("hours"), _normalize_wrap(expected["hours"])
    if got_hours != want_hours:
        return False, f"hours {got_hours} != expected {want_hours}"

    key = VALUE_KEY.get(expected["directive_type"])
    if key is None:
        return True, ""
    got, want = adj.get(key), expected[key]
    if not isinstance(got, (int, float)) or abs(float(got) - float(want)) > 1e-3:
        return False, f"{key} {got} != expected {want}"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    notes = fixture["notes"]

    results: list[dict] = []
    by_type: dict[str, list[bool]] = defaultdict(list)

    with httpx.Client(timeout=35.0) as client:
        for item in notes:
            started = __import__("time").perf_counter()
            try:
                resp = client.post(f"{args.base_url}/optimize-energy", json=build_request(item["note"]))
            except httpx.HTTPError as exc:
                ok, detail, ms = False, f"request error: {exc}", 0.0
                body = None
            else:
                ms = (__import__("time").perf_counter() - started) * 1000
                if resp.status_code != 200:
                    ok, detail, body = False, f"HTTP {resp.status_code}: {resp.text[:120]}", None
                else:
                    body = resp.json()
                    ok, detail = score_note(item["expected"], body.get("directive_interpretation", []))

            etype = "no_op" if item["expected"] is None else item["expected"]["directive_type"]
            by_type[etype].append(ok)
            results.append({
                "id": item["id"], "type": etype, "pass": ok,
                "detail": detail, "latency_ms": round(ms, 1), "response": body,
            })

    if args.json:
        total = sum(r["pass"] for r in results)
        print(json.dumps({
            "total": len(results), "passed": total,
            "score_by_type": {t: f"{sum(v)}/{len(v)}" for t, v in by_type.items()},
            "results": results,
        }, indent=2))
        return 0 if total == len(results) else 1

    print(f"GridWise adversarial suite — {args.base_url}\n")
    for item_type, outcomes in by_type.items():
        print(f"  {item_type:<26} {sum(outcomes)}/{len(outcomes)}")
    total = sum(r["pass"] for r in results)
    print(f"\n  {'TOTAL':<26} {total}/{len(results)}\n")

    misses = [r for r in results if not r["pass"]]
    if misses:
        print("Misses:")
        for r in misses:
            print(f"  [{r['id']}] {r['detail']}")
    print(f"\nRESULT: {'PASS' if not misses else 'FAIL'}")
    return 0 if not misses else 1


if __name__ == "__main__":
    sys.exit(main())
