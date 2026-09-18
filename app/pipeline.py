"""Pipeline orchestrator: schema guard -> LLM interpreter -> guardrails ->
directive compiler -> LP optimizer -> replay validator -> response assembly.

Totals are always recomputed from hourly_plan (never trusted from the solver),
mirroring the judge. Failures degrade safely: malformed LLM output demotes to
no_op; transport outages short-circuit to safe no_ops so a valid schedule is
still returned; only a genuinely unsolvable scenario surfaces a controlled 500.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import guardrails, llm, optimizer, replay
from .config import get_settings
from .optimizer import OptimizationError, OptimizationResult, compile_directives_from_base
from .schemas import OptimizeRequest

logger = logging.getLogger("gridwise.pipeline")


class OptimizationFailedError(RuntimeError):
    """Optimizer could not produce a valid schedule (controlled 500)."""


@dataclass
class PipelineOutcome:
    response: dict
    stage_timings_ms: dict[str, float]


def _build_plan_summary(
    interpretations: list[dict], result: OptimizationResult, total_notes: int
) -> str:
    applied: list[str] = []
    for entry in interpretations:
        dtype = entry["directive_type"]
        adj = entry.get("structured_adjustment") or {}
        if not entry.get("applies"):
            continue
        if dtype == "solar_reduction":
            applied.append(
                f"solar reduced to {adj['factor']:g} of forecast in hours "
                f"{_join_hours(adj['hours'])}"
            )
        elif dtype == "minimum_battery_reserve":
            applied.append(
                f"battery reserve of {adj['minimum_energy_kwh']:g} kWh in hours "
                f"{_join_hours(adj['hours'])}"
            )
        elif dtype == "no_charge_window":
            applied.append(f"no charging in hours {_join_hours(adj['hours'])}")
        elif dtype == "no_discharge_window":
            applied.append(f"no discharging in hours {_join_hours(adj['hours'])}")
        elif dtype == "max_grid_window":
            applied.append(
                f"grid import capped at {adj['max_grid_kwh']:g} kWh in hours "
                f"{_join_hours(adj['hours'])}"
            )

    directive_part = (
        "Applies: " + "; ".join(applied) + ". "
        if applied
        else "No schedule-affecting directives were interpreted. "
    )
    ignored = total_notes - len(applied)
    ignored_part = f"{ignored} of {total_notes} notes ignored as unrelated. " if ignored else ""
    strategy_part = (
        f"Grid purchase {result.total_grid_kwh:g} kWh at cost {result.total_cost_bdt:g} BDT "
        f"(peak {result.peak_grid_kwh:g} kWh), battery shifted toward cheaper hours and "
        "restored to its initial level by hour 23."
    )
    return directive_part + ignored_part + strategy_part


def _join_hours(hours: list) -> str:
    if not hours:
        return "none"
    runs: list[tuple[int, int]] = []
    start = prev = hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            runs.append((start, prev))
            start = prev = h
    runs.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def run_pipeline(request: OptimizeRequest) -> PipelineOutcome:
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    # 1. LLM interpretation (batched, one call for all notes).
    settings = get_settings()
    notes = [n.strip() for n in request.operator_notes]
    battery_capacity = float(request.battery.capacity_kwh)

    candidates: list[dict] | None = None
    interpreter = llm.build_interpreter(settings)

    try:
        raw = interpreter.interpret(notes, battery_capacity)
        candidates, _ = guardrails.validate_and_repair(raw, len(notes))
        if candidates is None:
            # Structural mismatch: one bounded corrective retry (same prompt;
            # hosted adapters already enforce schema provider-side, so a retry
            # here targets transient truncation/parser faults).
            raw = interpreter.interpret(notes, battery_capacity)
            candidates, _ = guardrails.validate_and_repair(raw, len(notes))
    except llm.LLMTransportError as exc:
        logger.warning("LLM provider unavailable (%s); short-circuiting to safe no_ops", exc)
        candidates = None

    if candidates is None:
        # Circuit-breaker / safe failure: valid schedule with all notes demoted.
        candidates = [guardrails.make_no_op(i) for i in range(len(notes))]

    interpretations = candidates
    timings["interpret"] = (time.perf_counter() - t0) * 1000

    # 2. Deterministic directive compilation.
    t1 = time.perf_counter()
    base_solar = [float(h.solar_kwh) for h in request.hours]
    directives = compile_directives_from_base(request.battery, base_solar, interpretations)
    timings["compile"] = (time.perf_counter() - t1) * 1000

    # 3. LP optimization.
    t2 = time.perf_counter()
    try:
        result = optimizer.solve(request.hours, request.battery, directives)
    except OptimizationError as exc:
        logger.error("optimization failed for scenario %s: %s", request.scenario_id, exc)
        raise OptimizationFailedError(str(exc)) from exc
    timings["optimize"] = (time.perf_counter() - t2) * 1000

    # 4. Response assembly: totals derived from the plan, never the solver.
    t3 = time.perf_counter()
    plan_dicts = [
        {
            "hour": p.hour,
            "grid_kwh": p.grid_kwh,
            "solar_used_kwh": p.solar_used_kwh,
            "battery_action": p.battery_action,
            "battery_kwh": p.battery_kwh,
            "battery_energy_after_kwh": p.battery_energy_after_kwh,
        }
        for p in result.plan
    ]

    total_grid = round(sum(p["grid_kwh"] for p in plan_dicts), 6)
    total_cost = round(
        sum(
            p["grid_kwh"] * float(request.hours[i].tariff_bdt_per_kwh)
            for i, p in enumerate(plan_dicts)
        ),
        6,
    )
    peak_grid = round(max(p["grid_kwh"] for p in plan_dicts), 6)

    # 5. Final replay validator (judge-mirror self-check before responding).
    valid, violations = replay.replay_plan(
        request.hours,
        request.battery,
        interpretations,
        plan_dicts,
        total_grid,
        total_cost,
        peak_grid,
    )
    if not valid:
        logger.error(
            "replay validation failed for scenario %s: %s", request.scenario_id, violations
        )
        raise OptimizationFailedError("final replay validation failed; refusing to return invalid plan")

    timings["assemble_and_replay"] = (time.perf_counter() - t3) * 1000
    timings["total"] = (time.perf_counter() - t0) * 1000
    logger.info(
        "scenario %s processed in %.1f ms (interpret %.1f ms, optimize %.1f ms)",
        request.scenario_id,
        timings["total"],
        timings["interpret"],
        timings["optimize"],
    )

    response = {
        "scenario_id": request.scenario_id,
        "directive_interpretation": interpretations,
        "hourly_plan": plan_dicts,
        "total_grid_kwh": total_grid,
        "total_cost_bdt": total_cost,
        "peak_grid_kwh": peak_grid,
        "plan_summary": _build_plan_summary(interpretations, result, len(notes)),
    }
    return PipelineOutcome(response=response, stage_timings_ms=timings)
