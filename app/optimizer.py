"""Math optimizer: 24-hour linear program solved with scipy's HiGHS (linprog).

Formulation (per hour h = 0..23), exactly matching Problem Statement Section 09:

  variables   grid[h], solar_used[h], charge[h], discharge[h], battery_after[h]
  objective   minimize sum(grid[h] * tariff[h])
  balance     grid[h] + solar_used[h] + discharge[h] - charge[h] = demand[h]
  battery     battery_after[h] = battery_after[h-1] + charge[h] - discharge[h]
              battery_after[0] = initial + charge[0] - discharge[0]
  neutrality  battery_after[23] = initial
  bounds      0 <= solar_used <= effective_solar[h]
              active_min[h] <= battery_after[h] <= capacity
              0 <= charge <= max_charge (0 in no-charge hours)
              0 <= discharge <= max_discharge (0 in no-discharge hours)
              0 <= grid <= max_grid_kwh (in capped hours)

All constraints are equations or variable bounds, so this is a pure LP (HiGHS
solves it in milliseconds). Simultaneous charge+discharge is a degenerate LP
artifact that is never cost-reducing; a deterministic netting pass removes it
without changing battery state, balance, rate-limit compliance, or cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linprog

from .schemas import BatteryInput, HourInput


class OptimizationError(RuntimeError):
    """Raised when the LP cannot be solved to optimality."""


@dataclass
class CompiledDirectives:
    """Directive constraints compiled into optimizer-ready per-hour arrays."""

    effective_solar: list[float]
    active_min_reserve: list[float]
    charge_blocked: set[int] = field(default_factory=set)
    discharge_blocked: set[int] = field(default_factory=set)
    grid_caps: dict[int, float] = field(default_factory=dict)


def compile_directives_from_base(
    battery: BatteryInput,
    base_solar: list[float],
    interpretations: list[dict[str, Any]],
) -> CompiledDirectives:
    """Authoritative directive compiler: base solar is always the input source."""
    effective_solar = list(base_solar)
    active_min = [float(battery.minimum_energy_kwh)] * 24
    charge_blocked: set[int] = set()
    discharge_blocked: set[int] = set()
    grid_caps: dict[int, float] = {}

    for entry in interpretations:
        if not entry.get("applies"):
            continue
        dtype = entry["directive_type"]
        adj = entry.get("structured_adjustment") or {}
        hours = adj.get("hours", [])

        if dtype == "solar_reduction":
            factor = float(adj["factor"])
            for h in hours:
                effective_solar[h] = base_solar[h] * factor
        elif dtype == "minimum_battery_reserve":
            floor = float(adj["minimum_energy_kwh"])
            for h in hours:
                active_min[h] = max(active_min[h], min(floor, float(battery.capacity_kwh)))
        elif dtype == "no_charge_window":
            charge_blocked.update(hours)
        elif dtype == "no_discharge_window":
            discharge_blocked.update(hours)
        elif dtype == "max_grid_window":
            cap = float(adj["max_grid_kwh"])
            for h in hours:
                grid_caps[h] = min(grid_caps.get(h, cap), cap)

    return CompiledDirectives(
        effective_solar=effective_solar,
        active_min_reserve=active_min,
        charge_blocked=charge_blocked,
        discharge_blocked=discharge_blocked,
        grid_caps=grid_caps,
    )


# ---------------------------------------------------------------------------
# LP solve
# ---------------------------------------------------------------------------


@dataclass
class HourPlan:
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: str
    battery_kwh: float
    battery_energy_after_kwh: float


@dataclass
class OptimizationResult:
    plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float


def _snap(value: float, tolerance: float = 1e-7) -> float:
    """Remove floating-point noise from solver output."""
    nearest = round(value)
    if abs(value - nearest) < tolerance:
        return float(nearest)
    return round(value, 9)


def solve(
    hours: list[HourInput],
    battery: BatteryInput,
    directives: CompiledDirectives,
) -> OptimizationResult:
    n = 24
    # Variable order per hour: [grid, solar_used, charge, discharge, battery_after]
    cost_vector = np.zeros(5 * n)
    bounds: list[tuple[float, float]] = []

    for h, hour_row in enumerate(hours):
        g, s, c, d, e = 5 * h, 5 * h + 1, 5 * h + 2, 5 * h + 3, 5 * h + 4
        cost_vector[g] = float(hour_row.tariff_bdt_per_kwh)

        grid_upper = directives.grid_caps.get(h, np.inf)
        bounds.append((0.0, grid_upper))                                   # grid
        bounds.append((0.0, max(0.0, float(directives.effective_solar[h]))))  # solar_used
        charge_upper = 0.0 if h in directives.charge_blocked else float(battery.max_charge_kwh_per_hour)
        bounds.append((0.0, charge_upper))                                 # charge
        discharge_upper = (
            0.0 if h in directives.discharge_blocked else float(battery.max_discharge_kwh_per_hour)
        )
        bounds.append((0.0, discharge_upper))                              # discharge
        bounds.append((float(directives.active_min_reserve[h]), float(battery.capacity_kwh)))  # after

    a_eq: list[list[float]] = []
    b_eq: list[float] = []

    for h, hour_row in enumerate(hours):
        g, s, c, d, e = 5 * h, 5 * h + 1, 5 * h + 2, 5 * h + 3, 5 * h + 4
        # grid + solar_used + discharge - charge = demand
        row = [0.0] * (5 * n)
        row[g] = 1.0
        row[s] = 1.0
        row[d] = 1.0
        row[c] = -1.0
        a_eq.append(row)
        b_eq.append(float(hour_row.demand_kwh))

        # battery_after[h] - battery_after[h-1] - charge + discharge = 0
        row = [0.0] * (5 * n)
        row[e] = 1.0
        row[c] = -1.0
        row[d] = 1.0
        if h > 0:
            row[5 * (h - 1) + 4] = -1.0
            b_eq.append(0.0)
        else:
            b_eq.append(float(battery.initial_energy_kwh))
        a_eq.append(row)

    # End-of-day neutrality: battery_after[23] = initial_energy_kwh
    row = [0.0] * (5 * n)
    row[5 * 23 + 4] = 1.0
    a_eq.append(row)
    b_eq.append(float(battery.initial_energy_kwh))

    result = linprog(
        c=cost_vector,
        A_eq=np.array(a_eq),
        b_eq=np.array(b_eq),
        bounds=bounds,
        method="highs",
    )

    if not result.success or result.x is None:
        raise OptimizationError(
            f"optimizer could not find a feasible minimum-cost schedule (status: {result.status})"
        )

    return _extract_and_net(hours, battery, result.x)


def _extract_and_net(
    hours: list[HourInput],
    battery: BatteryInput,
    x: np.ndarray,
) -> OptimizationResult:
    """Extract solver output, net simultaneous charge/discharge, snap FP noise."""
    n = 24
    raw_grid = [float(x[5 * h]) for h in range(n)]
    raw_solar = [float(x[5 * h + 1]) for h in range(n)]
    raw_charge = [float(x[5 * h + 2]) for h in range(n)]
    raw_discharge = [float(x[5 * h + 3]) for h in range(n)]
    raw_after = [float(x[5 * h + 4]) for h in range(n)]

    plan: list[HourPlan] = []
    energy = float(battery.initial_energy_kwh)
    for h, hour_row in enumerate(hours):
        net = raw_charge[h] - raw_discharge[h]
        net = _snap(net)
        # Clamp tiny bound violations introduced by solver tolerance.
        if net > 0:
            net = min(net, float(battery.max_charge_kwh_per_hour))
        elif net < 0:
            net = max(net, -float(battery.max_discharge_kwh_per_hour))

        if net > 1e-9:
            action, battery_kwh = "charge", net
        elif net < -1e-9:
            action, battery_kwh = "discharge", -net
        else:
            action, battery_kwh = "idle", 0.0

        energy = energy + (battery_kwh if action == "charge" else 0.0) - (
            battery_kwh if action == "discharge" else 0.0
        )
        after = _snap(energy)
        energy = after

        solar_used = max(0.0, _snap(raw_solar[h]))
        grid = max(0.0, _snap(raw_grid[h]))

        plan.append(
            HourPlan(
                hour=hour_row.hour,
                grid_kwh=grid,
                solar_used_kwh=solar_used,
                battery_action=action,
                battery_kwh=battery_kwh,
                battery_energy_after_kwh=after,
            )
        )

    total_grid = round(sum(p.grid_kwh for p in plan), 6)
    total_cost = round(
        sum(p.grid_kwh * float(hours[i].tariff_bdt_per_kwh) for i, p in enumerate(plan)), 6
    )
    peak_grid = round(max(p.grid_kwh for p in plan), 6)

    return OptimizationResult(
        plan=plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
    )
