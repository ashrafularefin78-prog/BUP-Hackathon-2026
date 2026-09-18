"""Pydantic v2 models transcribed from the Problem Statement (Sections 07 & 10).

The request models form the Ingress / Schema Guard: structurally invalid requests are
rejected with HTTP 400 before any LLM call. The response models mirror Section 10
exactly.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Directive domain (closed taxonomy from Section 04 of the Problem Statement)
# ---------------------------------------------------------------------------

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

BatteryAction = Literal["charge", "discharge", "idle"]


class SolarReductionAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: list[int]
    factor: float


class MinimumBatteryReserveAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: list[int]
    minimum_energy_kwh: float


class HoursOnlyAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: list[int]


class MaxGridWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: list[int]
    max_grid_kwh: float


DirectiveAdjustment = (
    SolarReductionAdjustment
    | MinimumBatteryReserveAdjustment
    | HoursOnlyAdjustment
    | MaxGridWindowAdjustment
)


def _hours_are_valid(hours: list[int]) -> bool:
    """Unique integers 0..23, non-empty, in ascending order."""
    return (
        len(hours) > 0
        and all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in hours)
        and len(set(hours)) == len(hours)
        and all(hours[i] < hours[i + 1] for i in range(len(hours) - 1))
    )


# ---------------------------------------------------------------------------
# Request (Section 07)
# ---------------------------------------------------------------------------


class HourInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)

    @field_validator("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class BatteryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capacity_kwh: float = Field(ge=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    @field_validator(
        "capacity_kwh",
        "initial_energy_kwh",
        "minimum_energy_kwh",
        "max_charge_kwh_per_hour",
        "max_discharge_kwh_per_hour",
    )
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v

    @model_validator(mode="after")
    def _initial_within_capacity(self) -> "BatteryInput":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        return self


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourInput] = Field(min_length=24, max_length=24)
    battery: BatteryInput

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, notes: list[str]) -> list[str]:
        for i, note in enumerate(notes):
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"operator_notes[{i}] must be a non-empty string")
        return notes

    @model_validator(mode="after")
    def _hours_unique_and_complete(self) -> "OptimizeRequest":
        hours = [h.hour for h in self.hours]
        if len(set(hours)) != 24 or sorted(hours) != list(range(24)):
            raise ValueError("hours must contain exactly 24 unique entries for hours 0 through 23")
        self.hours = sorted(self.hours, key=lambda h: h.hour)
        return self


# ---------------------------------------------------------------------------
# Response (Section 10)
# ---------------------------------------------------------------------------


class DirectiveInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None
    explanation: str


class HourlyPlanEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)


class OptimizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanEntry] = Field(min_length=24, max_length=24)
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
