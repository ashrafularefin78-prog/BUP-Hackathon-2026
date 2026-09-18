"""LLM interpretation layer with provider adapters.

The language model is a mandatory part of the operator-note interpretation path
(Problem Statement Section 02). Provider choice, model, key, timeouts, and retry
budget are environment-configured (app.config). The adapter returns a candidate
list; deterministic guardrails validate it before the optimizer sees it.

Providers:
  - mock:      deterministic offline interpreter for development and the test
               suite (documented as a dev double, NOT the judging interpreter).
  - anthropic: Claude Messages API with forced tool use (strict input schema).
  - openai:    Chat Completions with strict JSON-schema response_format.

Transport failures raise LLMTransportError after the bounded retry budget so the
caller can apply its circuit-breaker fallback.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol

import httpx

from .config import Settings

logger = logging.getLogger("gridwise.llm")


class LLMTransportError(RuntimeError):
    """Raised when the LLM provider cannot be reached or keeps failing."""


# ---------------------------------------------------------------------------
# System prompt: closed taxonomy, exact shapes, time convention, no invention.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the directive-interpretation module of an energy-scheduling \
service for a smart campus (24-hour horizon, hours 0-23 = midnight to 11 PM).

You read short natural-language operator notes and convert EACH note into exactly one \
machine-checkable directive. You output ONLY structured data via the provided tool/JSON \
schema — never prose.

Supported directive types and their exact structured_adjustment shapes:
1. solar_reduction           -> {"hours": [ints], "factor": number}
2. minimum_battery_reserve   -> {"hours": [ints], "minimum_energy_kwh": number}
3. no_charge_window          -> {"hours": [ints]}
4. no_discharge_window       -> {"hours": [ints]}
5. max_grid_window           -> {"hours": [ints], "max_grid_kwh": number}
6. no_op                     -> structured_adjustment must be null, applies false

Rules:
- Return exactly one entry per note, with note_index = the note's zero-based position,
  in note order. Do not merge, split, or skip notes.
- applies is true for every supported directive type; only no_op uses applies=false.
- Time windows use whole hours, start-INCLUSIVE and end-EXCLUSIVE:
  "from 1 PM to 3 PM" / "1-3 PM" / "13:00-15:00" all map to hours [13, 14].
  "from 6 PM until 9 PM" maps to [18, 19, 20]. "from 6 PM until 10 PM" maps to
  [18, 19, 20, 21]. Midnight-based ranges like "2 AM until 5 AM" map to [2, 3, 4].
- solar_reduction factor is the usable FRACTION REMAINING, not the reduction:
  "output will drop to about 20%" -> factor 0.2; "an 80% reduction" -> factor 0.2;
  "about half of the forecast output" -> factor 0.5; "roughly 25% of the forecast"
  -> factor 0.25.
- minimum_battery_reserve: convert relative language into kWh using the battery
  capacity given in the scenario context (e.g. "keep at least 50% of the battery
  capacity" with a 200 kWh battery -> minimum_energy_kwh 100). Absolute kWh values
  are used as stated.
- max_grid_window: the stated per-hour grid import limit, e.g. "must not exceed
  155 kWh in any hour" -> max_grid_kwh 155.
- Charge/discharge unavailability, protection testing on the battery, charger
  maintenance/isolation -> no_charge_window or no_discharge_window depending on
  which action is blocked. Grid/feeder/transformer import limits -> max_grid_window.
- If a note does not concern today's 24-hour energy schedule, or is ambiguous,
  administrative, or about something other than solar availability, battery
  charge/discharge, battery reserve, or grid import limits, return no_op.
- NEVER invent numbers the note does not state, never invent a new directive type,
  never change demand, tariffs, or battery parameters, and never guess missing
  hours or values.
- Keep each explanation to one short sentence describing the operational effect.

Scenario context you receive includes the battery capacity (for %-of-capacity
conversion) and the 24-hour demand/solar/tariff profile (so "evening", "peak
afternoon", etc. can be resolved to concrete hours when a note uses them)."""


# ---------------------------------------------------------------------------
# JSON-schema contract shared by both hosted providers.
# ---------------------------------------------------------------------------

ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "note_index": {"type": "integer", "minimum": 0, "maximum": 2},
        "applies": {"type": "boolean"},
        "directive_type": {
            "type": "string",
            "enum": [
                "solar_reduction",
                "minimum_battery_reserve",
                "no_charge_window",
                "no_discharge_window",
                "max_grid_window",
                "no_op",
            ],
        },
        "structured_adjustment": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 23},
                            "minItems": 1,
                            "maxItems": 24,
                        },
                        "factor": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["hours", "factor"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 23},
                            "minItems": 1,
                            "maxItems": 24,
                        },
                        "minimum_energy_kwh": {"type": "number", "minimum": 0},
                    },
                    "required": ["hours", "minimum_energy_kwh"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 23},
                            "minItems": 1,
                            "maxItems": 24,
                        }
                    },
                    "required": ["hours"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 23},
                            "minItems": 1,
                            "maxItems": 24,
                        },
                        "max_grid_kwh": {"type": "number", "minimum": 0},
                    },
                    "required": ["hours", "max_grid_kwh"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "explanation": {"type": "string"},
    },
    "required": [
        "note_index",
        "applies",
        "directive_type",
        "structured_adjustment",
        "explanation",
    ],
    "additionalProperties": False,
}


def _entry_schema_without_addl() -> dict[str, Any]:
    """OpenAI strict mode disallows some constructs; relaxed copy for it."""
    schema = json.loads(json.dumps(ENTRY_SCHEMA))
    schema["additionalProperties"] = False
    return schema


def build_user_prompt(notes: list[str], battery_capacity_kwh: float) -> str:
    return (
        f"Battery capacity for %-of-capacity conversions: {battery_capacity_kwh} kWh.\n"
        f"Operator notes ({len(notes)}):\n"
        + "\n".join(f"[{i}] {note}" for i, note in enumerate(notes))
        + "\n\nReturn one directive entry per note, in note_index order."
    )


# ---------------------------------------------------------------------------
# Deterministic mock interpreter (development / tests only — NOT the judging
# interpreter; README documents this explicitly).
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(
    r"\b(?:(?P<wnum>one(?!\s+(?:third|quarter|fifth|half))|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"|(?P<num>\d{1,2}))(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\b",
    re.IGNORECASE,
)

_MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.IGNORECASE)

_WORD_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_CONNECTOR_RE = re.compile(r"\b(?:to|until|till|through|and)\b|[-\u2013\u2014]")


def _normalize_time_words(note: str) -> str:
    note = re.sub(r"\bnoon\b", "12 pm", note, flags=re.IGNORECASE)
    note = re.sub(r"\bmidnight\b", "12 am", note, flags=re.IGNORECASE)
    return note


def _token_hour(match: re.Match) -> tuple[int | None, str]:
    """Return (raw_hour, ampm) for a time token; raw_hour None if invalid."""
    wnum = match.group("wnum")
    ampm = (match.group("ampm") or "").lower()
    if wnum:
        hour = _WORD_HOURS[wnum.lower()]
    else:
        minute = match.group("minute")
        hour = int(match.group("num"))
        if minute is not None and minute != "00":
            return None, ampm  # non-whole-hour boundary
    if not (0 <= hour <= 23):
        return None, ampm
    return hour, ampm


def _apply_ampm(hour: int, ampm: str) -> int:
    if ampm == "am" and hour == 12:
        return 0
    if ampm == "pm" and hour != 12:
        return hour + 12
    return hour


def _maybe_pm_shift(hours: list[int], note: str) -> list[int]:
    """Solar notes with unpinned word windows ("from one until three") are
    daytime events; interpret bare 1-5 as PM when the result lands in daylight."""
    if re.search(r"\b(?:am|pm)\b", note.lower()):
        return hours
    shifted = [h + 12 if 1 <= h <= 5 else h for h in hours]
    if shifted != hours and all(6 <= h <= 19 for h in shifted):
        return sorted(set(shifted))
    return hours


def _parse_windows(note: str) -> list[int]:
    """Extract whole-hour start-inclusive/end-exclusive windows from the note."""
    text = _normalize_time_words(note)
    parsed: list[tuple[re.Match, int, str]] = []
    for m in _TIME_RE.finditer(text):
        raw_hour, ampm = _token_hour(m)
        if raw_hour is not None:
            parsed.append((m, raw_hour, ampm))

    hours: list[int] = []
    i = 0
    while i + 1 < len(parsed):
        ma, raw_start, a_ampm = parsed[i]
        mb, raw_end, b_ampm = parsed[i + 1]
        i += 1
        span = text[ma.end() : mb.start()].lower()
        if not _CONNECTOR_RE.search(span):
            continue
        # "1-3 PM": the first boundary inherits the second's am/pm qualifier.
        start = _apply_ampm(raw_start, a_ampm or b_ampm)
        end = _apply_ampm(raw_end, b_ampm or a_ampm)
        if start == end:
            continue
        cursor = start
        steps = 0
        while cursor != end and steps < 24:
            hours.append(cursor)
            cursor = (cursor + 1) % 24
            steps += 1

    return sorted(set(hours))


_WORD_PERCENT = {
    "zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "seventy five": 75, "twenty five": 25, "thirty five": 35, "forty five": 45,
    "fifty five": 55, "sixty five": 65, "eighty five": 85, "ninety five": 95,
}

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}


def _words_to_number(text: str) -> float | None:
    """Parse English number words up to 199 ("one hundred forty" -> 140)."""
    parts = text.lower().replace("-", " ").split()
    if not parts or any(p not in _WORD_NUMBERS for p in parts):
        return None
    total, hundreds = 0, 0
    for p in parts:
        v = _WORD_NUMBERS[p]
        if v == 100:
            hundreds += max(total, 1) * 100
            total = 0
        else:
            total += v
    result = hundreds + total
    return float(result) if 0 < result < 1000 else None


_WORDNUM = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty(?:[- ]five)?|thirty(?:[- ]five)?|forty(?:[- ]five)?|fifty(?:[- ]five)?|sixty(?:[- ]five)?|seventy(?:[- ]five)?|eighty(?:[- ]five)?|ninety(?:[- ]five)?|one hundred(?: (?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety))?)"


def _word_percent_value(note: str) -> float | None:
    m = re.search(rf"\b({_WORDNUM})\s*percent", note.lower())
    if not m:
        return None
    return _WORD_PERCENT.get(m.group(1).replace("-", " "))


def _parse_factor(note: str) -> float | None:
    lowered = note.lower()

    # "80% reduction" style: reduction percentage -> factor = 1 - pct/100
    reduction = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:reduction|drop|less|decrease)", lowered)
    if reduction:
        return round(1.0 - float(reduction.group(1)) / 100.0, 6)

    # "reduced by fifty percent" / "cut by seventy percent" (word form)
    by_word = re.search(
        r"\b(?:reduced?|dropped?|cut|down)\s+by\s+((?:" + _WORDNUM + r"))\s+percent",
        lowered,
    )
    if by_word:
        w = _WORD_PERCENT.get(by_word.group(1).replace("-", " "))
        if w is not None:
            return round(1.0 - w / 100.0, 6)

    # "thirty percent of the usual forecast" (word form)
    of_word = re.search(
        rf"\b({_WORDNUM})\s*percent\s+of\s+(?:the\s+)?(?:forecast|normal|usual|expected)",
        lowered,
    )
    if of_word:
        w = _WORD_PERCENT.get(of_word.group(1).replace("-", " "))
        if w is not None:
            return round(w / 100.0, 6)

    # "drop to about 20%" / "roughly 25% of the forecast" / "about half"
    to_pct = re.search(r"(?:drop|drops|fall|falls|goes down|down)\s+to\s+(?:about |roughly |approximately )?(\d{1,3}(?:\.\d+)?)\s*%", lowered)
    if to_pct:
        return round(float(to_pct.group(1)) / 100.0, 6)

    of_pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?(?:forecast|normal|usual|expected)", lowered)
    if of_pct:
        return round(float(of_pct.group(1)) / 100.0, 6)

    if re.search(r"\bhalf\b", lowered):
        return 0.5
    if re.search(r"\bone[- ]fifth\b|\ba fifth\b", lowered):
        return 0.2
    if re.search(r"\bone[- ]third\b|\ba third\b", lowered):
        return round(1.0 / 3.0, 6)
    if re.search(r"\bone[- ]quarter\b|\ba quarter\b", lowered):
        return 0.25
    if re.search(r"\btwo[- ]thirds\b", lowered):
        return round(2.0 / 3.0, 6)
    return None


_RESERVE_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?battery(?:'s)?\s+capacity")

_RESERVE_FRACTION_RE = re.compile(
    r"\b(half|a quarter|one quarter|a third|one third|two thirds|three quarters)\s+of\s+(?:the\s+)?battery(?:'s)?\s+capacity"
)


def _parse_reserve(note: str, capacity_kwh: float) -> float | None:
    pct = _RESERVE_PCT_RE.search(note.lower())
    if pct:
        return round(float(pct.group(1)) / 100.0 * capacity_kwh, 6)
    frac = _RESERVE_FRACTION_RE.search(note.lower())
    if frac:
        fraction = {
            "half": 0.5, "a quarter": 0.25, "one quarter": 0.25, "a third": 1.0 / 3.0,
            "one third": 1.0 / 3.0, "two thirds": 2.0 / 3.0, "three quarters": 0.75,
        }[frac.group(1)]
        return round(fraction * capacity_kwh, 6)
    abs_kwh = re.search(r"(\d+(?:\.\d+)?)\s*kwh", note.lower())
    if abs_kwh:
        return float(abs_kwh.group(1))
    word_kwh = re.search(rf"\b({_WORDNUM})\s*kwh", note.lower())
    if word_kwh:
        value = _words_to_number(word_kwh.group(1))
        if value is not None:
            return value
    return None


_CAP_RE = re.compile(
    r"(?:not exceed|at or below|no more than|below|under|maximum of|max of|"
    r"cap(?:ped)?\s+(?:at|to|is|of)|limit(?:ed)?\s+(?:at|to|is|of))\s+"
    r"(\d+(?:\.\d+)?)\s*kwh",
    re.IGNORECASE,
)


def _parse_grid_cap(note: str) -> float | None:
    m = _CAP_RE.search(note)
    if m:
        return float(m.group(1))
    m = re.search(
        rf"(?:not exceed|capped at|maximum of|max of|limit is|limited to|below|under)\s+({_WORDNUM})\s*kwh",
        note.lower(),
    )
    if m:
        return _words_to_number(m.group(1))
    return None


def mock_interpret(notes: list[str], battery_capacity_kwh: float) -> list[dict[str, Any]]:
    """Deterministic, dependency-free interpretation used for local dev and tests.

    Covers the semantic patterns exercised by the public sample pack. It is NOT a
    substitute for the hosted LLM at judging time (see README).
    """
    entries: list[dict[str, Any]] = []

    for idx, note in enumerate(notes):
        lowered = note.lower()

        if _MONTH_RE.search(lowered) and "next" in lowered:
            entries.append(_mock_entry(idx, "no_op", None,
                                       "This note does not affect today's energy schedule."))
            continue

        if "tomorrow" in lowered or "next week" in lowered or "next month" in lowered:
            entries.append(_mock_entry(idx, "no_op", None,
                                       "This note does not affect today's energy schedule."))
            continue

        hours = _parse_windows(note)

        mentions_solar = any(
            w in lowered
            for w in ("solar", "pv", "panel", "rooftop", "inverter", "photovoltaic")
        )
        mentions_grid = any(
            w in lowered
            for w in ("grid", "feeder", "transformer", "import", "substation", "intake")
        )
        mentions_battery = (
            "battery" in lowered or "charger" in lowered or "charging" in lowered
            or "discharge" in lowered
        )
        keep_reserve = any(
            w in lowered
            for w in ("keep", "reserve", "at least", "remain", "stored", "hold",
                      "minimum battery level", "must hold", "maintain", "preserve")
        )

        if mentions_solar and hours:
            factor = _parse_factor(note)
            if factor is not None:
                hours = _maybe_pm_shift(hours, note)
                entries.append(_mock_entry(
                    idx, "solar_reduction",
                    {"hours": hours, "factor": factor},
                    f"Usable solar is reduced to {factor:g} of the forecast during the stated window.",
                ))
                continue

        if mentions_grid and hours:
            cap = _parse_grid_cap(note)
            if cap is not None:
                entries.append(_mock_entry(
                    idx, "max_grid_window",
                    {"hours": hours, "max_grid_kwh": cap},
                    f"Grid import is capped at {cap:g} kWh in each listed hour.",
                ))
                continue

        if (mentions_battery or keep_reserve) and hours:
            reserve = _parse_reserve(note, battery_capacity_kwh)
            no_charge = any(
                w in lowered
                for w in ("not charge", "no charge", "charging is disabled",
                          "charging is unavailable", "charging will be", "charger",
                          "charging circuit", "isolated", "avoid charging",
                          "refrain from", "charging must stay off", "charging is off")
            )
            no_discharge = any(
                w in lowered
                for w in ("not discharge", "no discharge", "discharging", "discharge",
                          "draw from", "protection test", "relay test")
            )
            keep = any(
                w in lowered
                for w in ("keep", "reserve", "at least", "remain", "stored", "hold",
                          "maintain", "preserve", "minimum battery level")
            )

            if keep and reserve is not None:
                entries.append(_mock_entry(
                    idx, "minimum_battery_reserve",
                    {"hours": hours, "minimum_energy_kwh": reserve},
                    f"A {reserve:g} kWh minimum battery reserve applies during the stated window.",
                ))
                continue
            if no_charge and not no_discharge:
                entries.append(_mock_entry(
                    idx, "no_charge_window",
                    {"hours": hours},
                    "Battery charging is unavailable during the stated window.",
                ))
                continue
            if no_discharge:
                entries.append(_mock_entry(
                    idx, "no_discharge_window",
                    {"hours": hours},
                    "Battery discharge is unavailable during the stated window.",
                ))
                continue

        entries.append(_mock_entry(
            idx, "no_op", None,
            "This note does not map to a supported energy directive for today's schedule.",
        ))

    return entries


def _mock_entry(
    note_index: int,
    directive_type: str,
    adjustment: dict[str, Any] | None,
    explanation: str,
) -> dict[str, Any]:
    return {
        "note_index": note_index,
        "applies": directive_type != "no_op",
        "directive_type": directive_type,
        "structured_adjustment": adjustment,
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------


class Interpreter(Protocol):
    def interpret(self, notes: list[str], battery_capacity_kwh: float) -> list[dict[str, Any]]:
        ...


class MockInterpreter:
    provider = "mock"

    def interpret(self, notes: list[str], battery_capacity_kwh: float) -> list[dict[str, Any]]:
        return mock_interpret(notes, battery_capacity_kwh)


class AnthropicInterpreter:
    """Claude Messages API with forced tool use — shape enforced provider-side."""

    provider = "anthropic"
    TOOL_NAME = "emit_directive_interpretation"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)

    def interpret(self, notes: list[str], battery_capacity_kwh: float) -> list[dict[str, Any]]:
        tool = {
            "name": self.TOOL_NAME,
            "description": "Emit exactly one directive interpretation entry per operator note.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "interpretations": {
                        "type": "array",
                        "minItems": len(notes),
                        "maxItems": len(notes),
                        "items": ENTRY_SCHEMA,
                    }
                },
                "required": ["interpretations"],
                "additionalProperties": False,
            },
        }
        payload = {
            "model": self._settings.model,
            "max_tokens": 1500,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": self.TOOL_NAME},
            "messages": [
                {
                    "role": "user",
                    "content": build_user_prompt(notes, battery_capacity_kwh),
                }
            ],
        }
        data = self._post("https://api.anthropic.com/v1/messages", payload, {
            "x-api-key": self._settings.api_key or "",
            "anthropic-version": "2023-06-01",
        })
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == self.TOOL_NAME:
                return list(block["input"].get("interpretations", []))
        raise LLMTransportError("provider returned no tool_use block")

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        retries = self._settings.transport_retries
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.post(url, json=payload, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504, 529):
                    last_error = LLMTransportError(f"provider status {resp.status_code}")
                else:
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
        raise LLMTransportError(str(last_error or "provider unavailable"))


class OpenAIInterpreter:
    """Chat Completions with strict JSON-schema response_format."""

    provider = "openai"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)

    def interpret(self, notes: list[str], battery_capacity_kwh: float) -> list[dict[str, Any]]:
        item_schema = _entry_schema_without_addl()
        # OpenAI strict mode requires all properties in "required"
        item_schema["required"] = [
            "note_index", "applies", "directive_type",
            "structured_adjustment", "explanation",
        ]
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "directive_interpretations",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "interpretations": {
                            "type": "array",
                            "items": item_schema,
                        }
                    },
                    "required": ["interpretations"],
                    "additionalProperties": False,
                },
            },
        }
        payload = {
            "model": self._settings.model,
            "temperature": 0,
            "response_format": response_format,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_user_prompt(notes, battery_capacity_kwh)
                    + f" Exactly {len(notes)} entries.",
                },
            ],
        }
        data = self._post("https://api.openai.com/v1/chat/completions", payload, {
            "Authorization": f"Bearer {self._settings.api_key or ''}",
        })
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return list(parsed.get("interpretations", []))

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        retries = self._settings.transport_retries
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.post(url, json=payload, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = LLMTransportError(f"provider status {resp.status_code}")
                else:
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
        raise LLMTransportError(str(last_error or "provider unavailable"))


def build_interpreter(settings: Settings) -> Interpreter:
    if settings.provider == "anthropic":
        return AnthropicInterpreter(settings)
    if settings.provider == "openai":
        return OpenAIInterpreter(settings)
    return MockInterpreter()
