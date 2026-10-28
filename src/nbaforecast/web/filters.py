"""Jinja filters and small display helpers (percentages, signed numbers, Central-time dates)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from nbaforecast.ingest.espn import EASTERN  # noqa: F401  (slate dates stay US Eastern)

CENTRAL = ZoneInfo("America/Chicago")  # every time shown on the site is Central


def _pct(p: float | None) -> str:
    return "–" if p is None else f"{p:.0%}"


def _num(x: float | None, digits: int = 3) -> str:
    return "–" if x is None else f"{x:.{digits}f}"


def _nice_date(iso: str | None) -> str:
    return "–" if not iso else datetime.fromisoformat(iso).strftime("%a, %b %-d")


def _long_date(iso: str | None) -> str:
    return "–" if not iso else datetime.fromisoformat(iso).strftime("%A, %B %-d")


def _metric(x: float | None, fmt: str) -> str:
    if x is None:
        return "–"
    return {"pct1": f"{x * 100:.1f}%", "num1": f"{x:.1f}", "num3": f"{x:.3f}"}.get(fmt, str(x))


def _tip_day(tipoff_utc: str) -> str:
    return (
        datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00"))
        .astimezone(CENTRAL)
        .strftime("%a %b %-d")
    )


def _signed(x: float | None) -> str:
    return "–" if x is None else f"{x:+.1f}"


def _odds(p: float | None) -> str:
    """Simulation odds: never print a flat 0% or 100% for something that can still happen."""
    if p is None:
        return "–"
    if p < 0.005:  # including 0 in 10,000 simulations: unlikely, not impossible
        return "<1%"
    if p > 0.995 and p < 1:
        return ">99%"
    return f"{p:.0%}"


WIN_AXIS = (10, 72)  # the range-of-wins track on the standings page


def _win_pos(wins: float) -> float:
    lo, hi = WIN_AXIS
    return round(max(0.0, min(100.0, (wins - lo) / (hi - lo) * 100)), 2)


def _tip_time(tipoff_utc: str) -> str:
    t = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00")).astimezone(CENTRAL)
    return t.strftime("%-I:%M %p")


def _tip_et(tipoff_utc: str) -> str:
    t = datetime.fromisoformat(tipoff_utc).astimezone(CENTRAL)
    return t.strftime("%-I:%M %p CT")
