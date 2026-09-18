"""Deterministic guardrail validator (Problem Statement Section 08).

The LLM's output is treated as untrusted structured data. Nothing reaches the
optimizer unless every check below passes. Pure, dependency-free Python —
independently unit-testable.

On malformed output we never crash and never invent directives: we salvage what
is salvageable per note, demote unsupported/malformed notes to a safe no_op, and
log (redacted) the reason.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("gridwise.guardrails")

ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

# Per-type structured_adjustment requirements: (required keys, extra allowed?)
ADJUSTMENT_REQUIREMENTS: dict[str, tuple[tuple[str, ...], bool]] = {
    "solar_reduction": (("hours", "factor"), False),
    "minimum_battery_reserve": (("hours", "minimum_energy_kwh"), False),
    "no_charge_window": (("hours",), False),
    "no_discharge_window": (("hours",), False),
    "max_grid_window": (("hours", "max_grid_kwh"), False),
    "no_op": ((), False),
}


def _valid_hours(hours: Any) -> bool:
    """Unique integers 0..23, non-empty, strictly ascending."""
    if not isinstance(hours, list) or len(hours) == 0:
        return False
    for h in hours:
        if isinstance(h, bool) or not isinstance(h, int):
            return False
        if not (0 <= h <= 23):
            return False
    return len(set(hours)) == len(hours) and all(
        hours[i] < hours[i + 1] for i in range(len(hours) - 1)
    )


def _finite_non_negative(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def _finite_fraction(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0.0 <= value <= 1.0


def check_entry(entry: Any, note_count: int) -> tuple[bool, str]:
    """Validate one candidate interpretation entry. Returns (ok, reason)."""
    if not isinstance(entry, dict):
        return False, "entry is not an object"
    if "note_index" not in entry or "applies" not in entry or "directive_type" not in entry:
        return False, "entry missing required fields"

    note_index = entry["note_index"]
    if isinstance(note_index, bool) or not isinstance(note_index, int) or not (
        0 <= note_index < note_count
    ):
        return False, "note_index out of range"

    applies = entry["applies"]
    if not isinstance(applies, bool):
        return False, "applies is not a boolean"

    directive_type = entry["directive_type"]
    if directive_type not in ALLOWED_DIRECTIVE_TYPES:
        return False, "unsupported directive_type"

    if directive_type == "no_op":
        if applies is not False:
            return False, "no_op must have applies=false"
        if entry.get("structured_adjustment") is not None:
            return False, "no_op must have structured_adjustment=null"
        return True, ""

    if applies is not True:
        return False, "non-no_op directive must have applies=true"

    adjustment = entry.get("structured_adjustment")
    required, extra_allowed = ADJUSTMENT_REQUIREMENTS[directive_type]
    if not isinstance(adjustment, dict):
        return False, "structured_adjustment must be an object"
    if not extra_allowed and set(adjustment.keys()) != set(required):
        return False, "structured_adjustment has unexpected or missing keys"

    if not _valid_hours(adjustment.get("hours")):
        return False, "hours must be unique integers 0-23 in ascending order"

    if directive_type == "solar_reduction":
        if not _finite_fraction(adjustment.get("factor")):
            return False, "factor must be a finite fraction between 0 and 1"
    elif directive_type == "minimum_battery_reserve":
        if not _finite_non_negative(adjustment.get("minimum_energy_kwh")):
            return False, "minimum_energy_kwh must be finite and non-negative"
    elif directive_type == "max_grid_window":
        if not _finite_non_negative(adjustment.get("max_grid_kwh")):
            return False, "max_grid_kwh must be finite and non-negative"

    return True, ""


def normalize_valid_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Coerce a guardrail-passing entry into the exact response shape."""
    directive_type = entry["directive_type"]
    normalized: dict[str, Any] = {
        "note_index": entry["note_index"],
        "applies": entry["applies"],
        "directive_type": directive_type,
        "structured_adjustment": None,
        "explanation": str(entry.get("explanation") or "").strip() or
        "Interpreted operator note.",
    }
    if directive_type != "no_op":
        adj = entry["structured_adjustment"]
        clean: dict[str, Any] = {"hours": list(adj["hours"])}
        if directive_type == "solar_reduction":
            clean["factor"] = float(adj["factor"])
        elif directive_type == "minimum_battery_reserve":
            clean["minimum_energy_kwh"] = float(adj["minimum_energy_kwh"])
        elif directive_type == "max_grid_window":
            clean["max_grid_kwh"] = float(adj["max_grid_kwh"])
        normalized["structured_adjustment"] = clean
    return normalized


def salvage_entry(entry: Any, expected_index: int, note_count: int) -> dict[str, Any] | None:
    """Attempt a conservative repair of a near-miss entry.

    Salvageable (conservative) cases:
      - numeric values given as numeric strings ("0.25") for the type's key fields
      - unsorted hours lists with unique in-range integers (re-sorted)
    note_index is ALWAYS forced to the positional expected_index — the LLM's own
    index label is never trusted. Anything else is rejected (caller demotes to no_op).
    """
    if not isinstance(entry, dict):
        return None
    directive_type = entry.get("directive_type")
    if directive_type not in ALLOWED_DIRECTIVE_TYPES or directive_type == "no_op":
        return None
    if entry.get("applies") is not True:
        return None

    adjustment = entry.get("structured_adjustment")
    if not isinstance(adjustment, dict):
        return None

    repaired = dict(adjustment)

    # Re-sort hours when they are a set-like unique collection of valid ints.
    hours = repaired.get("hours")
    if isinstance(hours, list) and len(hours) > 0:
        ints_ok = all(
            not isinstance(h, bool) and isinstance(h, int) and 0 <= h <= 23 for h in hours
        )
        if ints_ok and len(set(hours)) == len(hours):
            repaired["hours"] = sorted(hours)

    # Coerce numeric strings for the type's numeric key.
    numeric_key = {
        "solar_reduction": "factor",
        "minimum_battery_reserve": "minimum_energy_kwh",
        "max_grid_window": "max_grid_kwh",
    }.get(directive_type)
    if numeric_key is not None:
        raw = repaired.get(numeric_key)
        if isinstance(raw, str):
            try:
                repaired[numeric_key] = float(raw)
            except ValueError:
                return None

    candidate = {
        "note_index": expected_index,
        "applies": True,
        "directive_type": directive_type,
        "structured_adjustment": repaired,
        "explanation": entry.get("explanation"),
    }
    ok, _ = check_entry(candidate, note_count)
    return candidate if ok else None


def make_no_op(note_index: int, reason: str = "") -> dict[str, Any]:
    """Safe-failure interpretation for one note."""
    explanation = (
        "This note could not be mapped to a supported directive with confidence, "
        "so it is treated as not affecting today's 24-hour energy schedule."
    )
    if reason:
        explanation += f" ({reason})"
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": explanation,
    }


def validate_and_repair(
    candidates: Any, note_count: int
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Validate the full candidate list; salvage near-misses; demote the rest.

    Returns (entries_or_None, reasons). entries is None only if the candidate
    structure itself is unusable (not a list) — the caller then retries the LLM
    once before falling back to all-no_op.

    Index mapping is hybrid: LLM-provided note_index labels are used only when
    they form a complete, unique 0..N-1 permutation (trusting a provider that
    demonstrated a coherent mapping); otherwise entries are mapped positionally
    and each note_index is forced to its positional index. A candidate list is
    never discarded for wrong arity — missing entries simply demote to no_op.
    """
    reasons: list[str] = []

    if not isinstance(candidates, list):
        return None, ["LLM output was not a list"]

    # Prefer label-based mapping only for a complete, unique 0..N-1 permutation.
    labeled = [
        e.get("note_index") for e in candidates
        if isinstance(e, dict)
        and isinstance(e.get("note_index"), int)
        and not isinstance(e.get("note_index"), bool)
    ]
    labels_form_permutation = (
        len(labeled) == len(candidates) == note_count
        and sorted(labeled) == list(range(note_count))
    )

    entries: list[dict[str, Any]] = []
    for expected_index in range(note_count):
        entry: Any = None
        if labels_form_permutation:
            for e in candidates:
                if isinstance(e, dict) and e.get("note_index") == expected_index:
                    entry = e
                    break
        elif expected_index < len(candidates):
            entry = candidates[expected_index]

        ok, reason = check_entry(entry, note_count)
        if ok:
            normalized = normalize_valid_entry(entry)
            if not labels_form_permutation:
                normalized["note_index"] = expected_index
            entries.append(normalized)
            continue

        salvaged = salvage_entry(entry, expected_index, note_count)
        if salvaged is not None:
            logger.info(
                "guardrail salvaged note %d: %s", expected_index, reason
            )
            entries.append(normalize_valid_entry(salvaged))
            reasons.append(f"note {expected_index}: salvaged ({reason})")
            continue

        logger.warning(
            "guardrails demoted note %d to no_op: %s", expected_index, reason
        )
        entries.append(make_no_op(expected_index, reason))
        reasons.append(f"note {expected_index}: demoted to no_op ({reason})")

    # Mapping is already exactly one entry per position 0..N-1 in order.
    return entries, reasons
