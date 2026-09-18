"""Optimizer unit tests with hand-computed known optima."""

from __future__ import annotations

import pytest

from app.optimizer import (
    OptimizationError,
    compile_directives_from_base,
    solve,
)
from app.schemas import BatteryInput, HourInput


def make_battery(**overrides):
    fields = dict(
        capacity_kwh=100.0,
        initial_energy_kwh=50.0,
        minimum_energy_kwh=10.0,
        max_charge_kwh_per_hour=25.0,
        max_discharge_kwh_per_hour=25.0,
    )
    fields.update(overrides)
    return BatteryInput(**fields)


def make_hours(demands, tariffs, solar=None):
    solar = solar or [0.0] * 24
    return [
        HourInput(hour=h, demand_kwh=float(demands[h]), solar_kwh=float(solar[h]),
                  tariff_bdt_per_kwh=float(tariffs[h]))
        for h in range(24)
    ]


def test_no_battery_flexibility_cost_is_flat():
    # Battery pinned at its initial level (min == initial == capacity/2) and no
    # room to move: cost must equal demand x tariff with no arbitrage.
    battery = make_battery(initial_energy_kwh=50.0, minimum_energy_kwh=50.0,
                           capacity_kwh=100.0, max_charge_kwh_per_hour=0.0,
                           max_discharge_kwh_per_hour=0.0)
    demands = [100.0] * 24
    tariffs = [10.0 if h in (14, 15) else 1.0 for h in range(24)]
    hours = make_hours(demands, tariffs)

    directives = compile_directives_from_base(battery, [0.0] * 24, [])
    result = solve(hours, battery, directives)

    expected = 22 * 100 * 1.0 + 2 * 100 * 10.0
    assert result.total_cost_bdt == pytest.approx(expected, abs=1e-6)
    for p in result.plan:
        assert p.battery_action == "idle"
        assert p.grid_kwh == pytest.approx(100.0)


def test_perfect_arbitrage_shifts_energy_to_cheap_hours():
    # Charge 25 in each of the 20 cheap hours (tariff 1), discharge 25 in each
    # of the 4 expensive hours (tariff 10), returning to the initial 50 kWh.
    battery = make_battery()
    demands = [100.0] * 24
    expensive = {14, 15, 16, 17}
    tariffs = [10.0 if h in expensive else 1.0 for h in range(24)]
    hours = make_hours(demands, tariffs)

    directives = compile_directives_from_base(battery, [0.0] * 24, [])
    result = solve(hours, battery, directives)

    cheap_hours = 24 - len(expensive)
    # Verified LP optimum: SOC can rise to at most 100 before the expensive
    # block, so at most 90 kWh can be discharged there (100 -> 10); the 90 kWh
    # is recharged in cheap hours afterwards. Cost = 2090 (cheap) + 3100 (exp).
    expected = 5190.0
    assert result.total_cost_bdt == pytest.approx(expected, abs=1e-4)

    # Neutrality holds and the round trip is energy-conserving.
    assert result.plan[-1].battery_energy_after_kwh == pytest.approx(
        battery.initial_energy_kwh, abs=1e-6
    )
    total_charge = sum(p.battery_kwh for p in result.plan if p.battery_action == "charge")
    total_discharge = sum(p.battery_kwh for p in result.plan if p.battery_action == "discharge")
    assert total_charge == pytest.approx(total_discharge, abs=1e-6)
    # Discharge-at-cheap-hour + recharge-later is cost-neutral, so the solver
    # may return degenerate alternative optima; only the verified invariants
    # (cost, neutrality, charge==discharge) are asserted here.


def test_solar_used_before_grid_when_free():
    battery = make_battery()
    demands = [50.0] * 24
    tariffs = [5.0] * 24
    solar = [60.0 if h == 12 else 0.0 for h in range(24)]
    hours = make_hours(demands, tariffs, solar)

    directives = compile_directives_from_base(battery, solar, [])
    result = solve(hours, battery, directives)

    noon = next(p for p in result.plan if p.hour == 12)
    # Solar is free, so all 60 kWh are used: 50 serves demand directly, 10
    # charges the battery. Under end-of-day neutrality (charge == discharge)
    # the grid supplies exactly demand minus solar: (1200 - 60) * 5.
    assert noon.solar_used_kwh == pytest.approx(60.0)
    assert noon.grid_kwh == pytest.approx(0.0)
    assert noon.battery_action == "charge"
    assert result.total_cost_bdt == pytest.approx((24 * 50 - 60) * 5.0, abs=1e-6)


def test_solar_reduction_directive_curtails_usable_solar():
    battery = make_battery()
    demands = [50.0] * 24
    tariffs = [5.0] * 24
    solar = [100.0 if h == 12 else 0.0 for h in range(24)]
    hours = make_hours(demands, tariffs, solar)

    interp = [{
        "note_index": 0, "applies": True, "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": [12], "factor": 0.25},
        "explanation": "",
    }]
    directives = compile_directives_from_base(battery, solar, interp)
    assert directives.effective_solar[12] == pytest.approx(25.0)

    result = solve(hours, battery, directives)
    noon = next(p for p in result.plan if p.hour == 12)
    assert noon.solar_used_kwh <= 25.0 + 1e-6


def test_no_charge_window_blocks_charging():
    battery = make_battery()
    demands = [100.0] * 24
    tariffs = [1.0] * 24
    hours = make_hours(demands, tariffs)

    interp = [{
        "note_index": 0, "applies": True, "directive_type": "no_charge_window",
        "structured_adjustment": {"hours": [0, 1, 2]},
        "explanation": "",
    }]
    directives = compile_directives_from_base(battery, [0.0] * 24, interp)
    result = solve(hours, battery, directives)

    for p in result.plan:
        if p.hour in (0, 1, 2):
            assert p.battery_action != "charge"


def test_no_discharge_window_blocks_discharging():
    battery = make_battery()
    demands = [100.0] * 24
    tariffs = [10.0 if h in (5, 6) else 1.0 for h in range(24)]
    hours = make_hours(demands, tariffs)

    interp = [{
        "note_index": 0, "applies": True, "directive_type": "no_discharge_window",
        "structured_adjustment": {"hours": [5, 6]},
        "explanation": "",
    }]
    directives = compile_directives_from_base(battery, [0.0] * 24, interp)
    result = solve(hours, battery, directives)

    for p in result.plan:
        if p.hour in (5, 6):
            assert p.battery_action != "discharge"


def test_reserve_floor_respected_and_uses_max_with_base():
    battery = make_battery(minimum_energy_kwh=40.0)
    demands = [100.0] * 24
    tariffs = [1.0] * 24
    hours = make_hours(demands, tariffs)

    interp = [{
        "note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
        "structured_adjustment": {"hours": [10, 11], "minimum_energy_kwh": 70.0},
        "explanation": "",
    }]
    directives = compile_directives_from_base(battery, [0.0] * 24, interp)
    assert directives.active_min_reserve[10] == pytest.approx(70.0)
    assert directives.active_min_reserve[9] == pytest.approx(40.0)

    result = solve(hours, battery, directives)
    for p in result.plan:
        if p.hour in (10, 11):
            assert p.battery_energy_after_kwh >= 70.0 - 1e-6


def test_grid_cap_directive_limits_hourly_import():
    battery = make_battery()
    demands = [200.0] * 24
    tariffs = [1.0] * 24
    hours = make_hours(demands, tariffs)

    interp = [{
        "note_index": 0, "applies": True, "directive_type": "max_grid_window",
        "structured_adjustment": {"hours": [10], "max_grid_kwh": 180.0},
        "explanation": "",
    }]
    directives = compile_directives_from_base(battery, [0.0] * 24, interp)
    result = solve(hours, battery, directives)

    capped = next(p for p in result.plan if p.hour == 10)
    assert capped.grid_kwh <= 180.0 + 1e-6
    # The 20 kWh shortfall must be covered by the battery.
    assert capped.battery_action == "discharge"
    assert capped.battery_kwh == pytest.approx(20.0, abs=1e-6)


def test_end_of_day_neutrality_always_holds():
    battery = make_battery()
    demands = [90.0 + (h % 5) * 10 for h in range(24)]
    tariffs = [2.0 + (h % 7) for h in range(24)]
    hours = make_hours(demands, tariffs)

    directives = compile_directives_from_base(battery, [0.0] * 24, [])
    result = solve(hours, battery, directives)
    assert result.plan[-1].battery_energy_after_kwh == pytest.approx(
        battery.initial_energy_kwh, abs=1e-6
    )


def test_no_simultaneous_charge_and_discharge_after_netting():
    battery = make_battery()
    demands = [100.0 + (h % 3) * 20 for h in range(24)]
    tariffs = [3.0 + (h % 11) for h in range(24)]
    hours = make_hours(demands, tariffs)

    directives = compile_directives_from_base(battery, [0.0] * 24, [])
    result = solve(hours, battery, directives)
    for p in result.plan:
        assert p.battery_action == "idle" or p.battery_kwh > 0.0


def test_infeasible_scenario_raises_controlled_error():
    # Demand exceeds what grid + battery + zero solar can supply in every hour
    # (grid uncapped here, so instead force infeasibility via a zero grid cap).
    battery = make_battery()
    demands = [1000.0] * 24
    tariffs = [1.0] * 24
    hours = make_hours(demands, tariffs)

    interp = [{
        "note_index": 0, "applies": True, "directive_type": "max_grid_window",
        "structured_adjustment": {"hours": list(range(24)), "max_grid_kwh": 0.0},
        "explanation": "",
    }]
    directives = compile_directives_from_base(battery, [0.0] * 24, interp)
    with pytest.raises(OptimizationError):
        solve(hours, battery, directives)
