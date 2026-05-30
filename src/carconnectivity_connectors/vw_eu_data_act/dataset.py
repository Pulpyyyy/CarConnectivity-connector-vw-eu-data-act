"""Pure-Python parsing of EU Data Act portal datasets.

Ported from the homeassistant-vw-eu-data-act integration's ``data.py`` (the
value-typing and dataset-model parts). The Home-Assistant-specific curated
registry and the 1000-field data dictionary are intentionally dropped: this
connector maps only the well-known fields onto native CarConnectivity
attributes, so no dictionary lookup is needed.

A dataset JSON looks like::

    {"vin": "...", "user_id": "...", "Data": [
        {"key": "uuid", "dataFieldName": "battery_state_report.soc", "value": "69"},
        ...
    ]}
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Value typing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*s$", re.I)
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def parse_duration_seconds(raw: str) -> Optional[float]:
    """Parse values like "0s" / "1800s" into seconds."""
    m = _DURATION_RE.match(raw.strip())
    return float(m.group(1)) if m else None


def parse_value(raw: Optional[str], type_hint: Optional[str] = None):
    """Coerce a raw string value into a typed Python value.

    Falls back to structural detection so it works without a type hint.
    Enums, ISO timestamps and free text stay as strings.
    """
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None

    hint = (type_hint or "").lower()

    if hint == "boolean" or s.lower() in ("true", "false"):
        return s.lower() == "true"

    if hint in ("int", "integer") and _INT_RE.match(s):
        return int(s)
    if hint == "float":
        try:
            return float(s)
        except ValueError:
            return s

    # duration shorthand ("0s")
    dur = parse_duration_seconds(s)
    if dur is not None:
        return dur

    # structural fallbacks
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)

    return s


# Distance unit enums (e.g. ``mileage.unit``) -> canonical short unit. The
# portal reports mileage/range in either miles or kilometres depending on the
# vehicle, so the unit must not be hardcoded; it is read from a companion
# ``*.unit`` field when present.
DISTANCE_UNIT_BY_ENUM: Dict[str, str] = {
    "MILES": "mi",
    "MILE": "mi",
    "KM": "km",
    "KILOMETER": "km",
    "KILOMETERS": "km",
    "KILOMETRE": "km",
    "KILOMETRES": "km",
}


def resolve_distance_unit(enum_value, default: Optional[str] = None) -> Optional[str]:
    """Map a distance-unit enum value (e.g. "MILES") to a short unit ("mi")."""
    if isinstance(enum_value, str):
        return DISTANCE_UNIT_BY_ENUM.get(enum_value.strip().upper(), default)
    return default


def parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    """Parse the various timestamp encodings seen in datasets."""
    s = (raw or "").strip()
    if not s:
        return None
    # epoch millis
    if _INT_RE.match(s) and len(s) >= 12:
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Dataset model
# ---------------------------------------------------------------------------


@dataclass
class DataPoint:
    """A single data point from a dataset."""

    key: str
    field_name: str
    raw_value: str

    @property
    def value(self):
        """The typed value (int/float/bool/str/None)."""
        return parse_value(self.raw_value)


@dataclass
class Dataset:
    """A parsed dataset JSON."""

    vin: str
    user_id: Optional[str] = None
    points: Dict[str, DataPoint] = field(default_factory=dict)  # by key
    captured_at: Optional[datetime] = None

    @classmethod
    def from_json(cls, payload: dict) -> "Dataset":
        """Parse a dataset JSON body into a :class:`Dataset`."""
        points: Dict[str, DataPoint] = {}
        captured = []
        for item in payload.get("Data", []):
            key = item.get("key")
            if not key:
                continue
            field_name = item.get("dataFieldName") or key
            dp = DataPoint(key=key, field_name=field_name, raw_value=item.get("value", ""))
            points[key] = dp
            if field_name == "car_captured_time":
                ts = parse_timestamp(dp.raw_value)
                if ts:
                    captured.append(ts)
        return cls(
            vin=payload.get("vin", ""),
            user_id=payload.get("user_id"),
            points=points,
            captured_at=max(captured) if captured else None,
        )

    def by_field(self, field_name: str) -> Optional[DataPoint]:
        """Return a single data point for a (possibly duplicated) field name.

        The portal merges several report snapshots into one flat array with no
        ordering guarantee and no way to tell which value is "live", so a field
        like ``charging_state_report.current_charge_state`` can appear several
        times under different UUIDs with conflicting values. We pick the entry
        with the smallest ``key`` (UUID): an arbitrary but *stable* choice, so a
        mapped attribute consistently tracks the same data point across refreshes
        instead of flip-flopping when the portal reshuffles the array.
        """
        matches = [dp for dp in self.points.values() if dp.field_name == field_name]
        return min(matches, key=lambda dp: dp.key) if matches else None

    def value_of(self, field_name: str):
        """Return the typed value of ``field_name`` or ``None`` if absent."""
        dp = self.by_field(field_name)
        return dp.value if dp is not None else None
