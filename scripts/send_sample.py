#!/usr/bin/env python
"""Send the public sample cases (or a single scenario id) to a running GridWise service.

Usage:
    python scripts/send_sample.py                      # all 10 cases -> http://127.0.0.1:8000
    python scripts/send_sample.py SAMPLE-03            # one case
    python scripts/send_sample.py --base-url https://my-app.example.com SAMPLE-07
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

PACK_PATH = Path(__file__).resolve().parents[1] / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", help="scenario id to send (default: all)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="service base URL")
    args = parser.parse_args()

    with open(PACK_PATH, "r", encoding="utf-8") as f:
        pack = json.load(f)

    cases = pack["cases"]
    if args.scenario:
        cases = [c for c in cases if c["id"].upper() == args.scenario.upper()]
        if not cases:
            print(f"scenario {args.scenario!r} not found in the public pack")
            return 1

    failures = 0
    with httpx.Client(timeout=35.0) as client:
        health = client.get(f"{args.base_url}/health")
        print(f"GET /health -> {health.status_code} {health.text}")
        if health.status_code != 200:
            return 1

        for case in cases:
            started = time.perf_counter()
            resp = client.post(f"{args.base_url}/optimize-energy", json=case["input"])
            elapsed_ms = (time.perf_counter() - started) * 1000
            label = f"{case['id']} ({case['label']})"

            if resp.status_code != 200:
                failures += 1
                print(f"{label}: HTTP {resp.status_code} -- {resp.text[:200]}")
                continue

            body = resp.json()
            ref = case["expected_output"]
            cost_delta = abs(body["total_cost_bdt"] - ref["total_cost_bdt"])
            cost_ok = "OK" if cost_delta <= 0.01 else "MISMATCH"
            print(
                f"{label}: cost {body['total_cost_bdt']:.2f} BDT "
                f"(reference {ref['total_cost_bdt']:.2f}, {cost_ok}) "
                f"grid {body['total_grid_kwh']:.1f} kWh, peak {body['peak_grid_kwh']:.0f} kWh "
                f"[{elapsed_ms:.0f} ms]"
            )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
