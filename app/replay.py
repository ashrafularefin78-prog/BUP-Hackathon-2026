"""Independent replay validator — a judge-mirror self-check.

Before a response leaves the service, the final hourly_plan is re-simulated
hour by hour using the request data and the interpreted directives (with 0.01
tolerance, per Section 11.5). This structurally prevents invalid plans from
being returned.
"""

from __future__ import annotations

import math
from typing import Any

from .schemas import BatteryInput, HourInput

TOLERANCE = 0.01


def _close(a: float, b: float, tol: float = TOLERANCE) -> bool:
    return abs(a - b) <= tol


def replay_plan(
    hours: list[HourInput],
    battery: BatteryInput,
    interpretations: list[dict[str, Any]],
    hourly_plan: list[dict[str, Any]],
    total_grid_kwh: float,
    total_cost_bdt: float,
    peak_grid_kwh: float,
) -> tuple[bool, list[str]]:
    """Replay the plan; returns (valid, violations)."""
    violations: list[str] = []
    if len(hourly_plan) != 24:
        return False, ["hourly_plan must contain exactly 24 entries"]

    by_hour = {entry.get("hour"): entry for entry in hourly_plan}
    if len(by_hour) != 24 or sorted(by_hour.keys()) != list(range(24)):
        return False, ["hourly_plan hours must be exactly 0..23"]

    # Effective solar from base solar + solar_reduction directives.
    effective_solar = [float(h.solar_kwh) for h in hours]
    active_min = [float(battery.minimum_energy_kwh)] * 24
    charge_blocked: set[int] = set()
    discharge_blocked: set[int] = set()
    grid_caps: dict[int, float] = {}

    for entry in interpretations:
        if not entry.get("applies"):
            continue
        dtype = entry["directive_type"]
        adj = entry.get("structured_adjustment") or {}
        for h in adj.get("hours", []):
            if dtype == "solar_reduction":
                effective_solar[h] = min(
                    effective_solar[h],
                    float(hours[h].solar_kwh) * float(adj["factor"]),
                )
            elif dtype == "minimum_battery_reserve":
                active_min[h] = max(active_min[h], float(adj["minimum_energy_kwh"]))
            elif dtype == "no_charge_window":
                charge_blocked.add(h)
            elif dtype == "no_discharge_window":
                discharge_blocked.add(h)
            elif dtype == "max_grid_window":
                grid_caps[h] = min(grid_caps.get(h, float("inf")), float(adj["max_grid_kwh"]))

    energy = float(battery.initial_energy_kwh)
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    for h, hour_row in enumerate(hours):
        entry = by_hour[h]
        try:
            grid = float(entry["grid_kwh"])
            solar_used = float(entry["solar_used_kwh"])
            action = entry["battery_action"]
            battery_kwh = float(entry["battery_kwh"])
            after = float(entry["battery_energy_after_kwh"])
        except (KeyError, TypeError, ValueError):
            return False, [f"hour {h}: malformed plan entry"]

        if any(not math.isfinite(v) for v in (grid, solar_used, battery_kwh, after)):
            violations.append(f"hour {h}: non-finite values")

        if grid < -TOLERANCE or solar_used < -TOLERANCE or battery_kwh < -TOLERANCE:
            violations.append(f"hour {h}: negative values")

        # Solar cap (effective solar).
        if solar_used > effective_solar[h] + TOLERANCE:
            violations.append(f"hour {h}: solar_used_kwh exceeds effective solar")

        # Grid cap directive.
        if h in grid_caps and grid > grid_caps[h] + TOLERANCE:
            violations.append(f"hour {h}: grid_kwh exceeds max_grid_window cap")

        # Battery action consistency.
        if action == "idle" and battery_kwh > TOLERANCE:
            violations.append(f"hour {h}: idle with non-zero battery_kwh")
        if action == "charge" and battery_kwh > float(battery.max_charge_kwh_per_hour) + TOLERANCE:
            violations.append(f"hour {h}: charge rate exceeded")
        if action == "discharge" and battery_kwh > float(battery.max_discharge_kwh_per_hour) + TOLERANCE:
            violations.append(f"hour {h}: discharge rate exceeded")
        if h in charge_blocked and action == "charge" and battery_kwh > TOLERANCE:
            violations.append(f"hour {h}: charge in no_charge_window")
        if h in discharge_blocked and action == "discharge" and battery_kwh > TOLERANCE:
            violations.append(f"hour {h}: discharge in no_discharge_window")

        # Battery transition + bounds.
        expected_after = energy + (
            battery_kwh if action == "charge" else 0.0
        ) - (battery_kwh if action == "discharge" else 0.0)
        if not _close(after, expected_after):
            violations.append(f"hour {h}: battery transition mismatch")
        lower = active_min[h]
        if after < lower - TOLERANCE or after > float(battery.capacity_kwh) + TOLERANCE:
            violations.append(f"hour {h}: battery energy out of bounds")

        # Energy balance.
        supply = grid + solar_used + (battery_kwh if action == "discharge" else 0.0)
        use = float(hour_row.demand_kwh) + (battery_kwh if action == "charge" else 0.0)
        if not _close(supply, use):
            violations.append(f"hour {h}: energy balance mismatch")

        energy = after
        total_grid += grid
        total_cost += grid * float(hour_row.tariff_bdt_per_kwh)
        peak_grid = max(peak_grid, grid)

    if not _close(energy, float(battery.initial_energy_kwh)):
        violations.append("end-of-day battery energy does not equal initial_energy_kwh")

    if not _close(total_grid, float(total_grid_kwh)):
        violations.append("total_grid_kwh does not match recalculated value")
    if not _close(total_cost, float(total_cost_bdt)):
        violations.append("total_cost_bdt does not match recalculated value")
    if not _close(peak_grid, float(peak_grid_kwh)):
        violations.append("peak_grid_kwh does not match recalculated value")

    return (len(violations) == 0), violations
