"""Tonight's sky, computed locally with ephem. Zero network."""
from __future__ import annotations

import math
from datetime import date as date_cls
from datetime import datetime, timezone

import ephem

from . import config

SYNODIC = 29.530588  # synodic month, days

PHASE_NAMES = ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
               "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"]
_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _phase_name(age_days: float) -> str:
    idx = int((age_days / SYNODIC) * 8 + 0.5) % 8
    return PHASE_NAMES[idx]


def _fmt_local(edate, ref_day: date_cls | None = None) -> str:
    """ephem.Date (UTC) → 'HH:MM' local. Events that fall outside ref_day get
    a day marker — a rise computed from midnight can land on the next day, and
    a bare HH:MM would mislead."""
    tz = config.local_tz()
    dt = edate.datetime().replace(tzinfo=timezone.utc).astimezone(tz)
    out = dt.strftime("%H:%M")
    if ref_day is not None and dt.date() != ref_day:
        delta = (dt.date() - ref_day).days
        out += f" (+{delta}d)" if delta > 0 else f" ({delta}d)"
    return out


def compute(day: date_cls) -> dict:
    """Pure computation — tests hit this directly. `day` is a local date."""
    tz = config.local_tz()
    obs = ephem.Observer()
    obs.lat, obs.lon = str(config.LAT), str(config.LON)

    # Moon phase anchored at local noon (a representative instant for the day).
    noon_local = datetime(day.year, day.month, day.day, 12, 0, tzinfo=tz)
    obs.date = ephem.Date(noon_local.astimezone(timezone.utc))
    moon = ephem.Moon(obs)
    illum = round(moon.phase, 1)  # % illuminated
    age = obs.date - ephem.previous_new_moon(obs.date)  # days

    # Rise/set anchored at local *midnight* — anchoring at noon would skip a
    # morning moonrise. Events landing on another day carry a (+Nd) marker.
    obs.date = ephem.Date(
        datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
        .astimezone(timezone.utc))

    def _try(fn, body):
        try:
            return _fmt_local(fn(body), ref_day=day)
        except Exception:  # noqa: BLE001 — polar edge cases: never rises/sets
            return "none"

    out = {
        "date": day.isoformat(),
        "moon_phase": _phase_name(age),
        "moon_illumination": f"{illum}%",
        "moonrise": _try(obs.next_rising, ephem.Moon()),
        "moonset": _try(obs.next_setting, ephem.Moon()),
        "sunset": _try(obs.next_setting, ephem.Sun()),
        "next_full_moon": ephem.next_full_moon(obs.date).datetime()
        .replace(tzinfo=timezone.utc).astimezone(tz).strftime("%m-%d"),
        "next_new_moon": ephem.next_new_moon(obs.date).datetime()
        .replace(tzinfo=timezone.utc).astimezone(tz).strftime("%m-%d"),
    }

    # Which naked-eye planets sit above 10° at 21:00 local tonight.
    obs.date = ephem.Date(
        datetime(day.year, day.month, day.day, 21, 0, tzinfo=tz)
        .astimezone(timezone.utc))
    visible = []
    for name, body in (("Venus", ephem.Venus()), ("Mars", ephem.Mars()),
                       ("Jupiter", ephem.Jupiter()), ("Saturn", ephem.Saturn())):
        body.compute(obs)
        alt_deg = math.degrees(float(body.alt))
        if alt_deg > 10.0:
            az_deg = math.degrees(float(body.az))
            visible.append(f"{name} ({_DIRS[int((az_deg + 22.5) // 45) % 8]}, alt {alt_deg:.0f}°)")
    out["planets_2100"] = visible or ["no naked-eye planets above 10° at 21:00"]
    return out
