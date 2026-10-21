"""Minimal 5-field cron expression parser (Vixie-cron semantics).

Supports: ``*``, numbers, lists (``1,5``), ranges (``1-5``), steps (``*/15``,
``1-30/5``, ``5/10`` = ``5-max/10``), month/weekday names, weekday 7 = Sunday,
and the macros ``@hourly @daily @midnight @weekly @monthly @yearly @annually``.

Day-of-month / day-of-week rule (same as Vixie cron): if BOTH fields are
restricted (neither starts with ``*``), a day matches when EITHER matches.
Otherwise both must match (the unrestricted one matches everything anyway).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

MONTH_NAMES = {n: i for i, n in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
DOW_NAMES = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}

# (name, min, max, names)
FIELDS = [
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("dom", 1, 31, {}),
    ("month", 1, 12, MONTH_NAMES),
    ("dow", 0, 7, DOW_NAMES),
]


class CronError(ValueError):
    pass


def _num(tok: str, names: dict, lo: int, hi: int) -> int:
    t = tok.strip().lower()
    if t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(f"bad value {tok!r}")
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"value {v} out of range {lo}-{hi}")
    return v


def _parse_field(text: str, lo: int, hi: int, names: dict) -> tuple[frozenset, bool]:
    """Return (set of values, is_wildcard)."""
    values: set[int] = set()
    wildcard = text.startswith("*")
    for part in text.split(","):
        if not part:
            raise CronError(f"empty list item in {text!r}")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) == 0:
                raise CronError(f"bad step in {text!r}")
            step = int(s)
            if part != "*" and "-" not in part:      # '5/10' == '5-max/10'
                part = f"{part}-{hi}"
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, names, lo, hi), _num(y, names, lo, hi)
            if a > b:
                raise CronError(f"descending range {part!r}")
        else:
            a = b = _num(part, names, lo, hi)
        values.update(range(a, b + 1, step))
    return frozenset(values), wildcard


@dataclass(frozen=True)
class Cron:
    expr: str
    minute: frozenset
    hour: frozenset
    dom: frozenset
    month: frozenset
    dow: frozenset          # normalised: 0..6, Sunday = 0
    dom_star: bool
    dow_star: bool

    # ------------------------------------------------------------------ match
    def day_matches(self, d: _dt.date) -> bool:
        if d.month not in self.month:
            return False
        dom_ok = d.day in self.dom
        dow_ok = (d.isoweekday() % 7) in self.dow
        if not self.dom_star and not self.dow_star:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, t: _dt.datetime) -> bool:
        return (t.minute in self.minute and t.hour in self.hour
                and self.day_matches(t.date()))

    # -------------------------------------------------------------- iterate
    def times_of_day(self) -> list[tuple[int, int]]:
        return sorted((h, m) for h in self.hour for m in self.minute)

    def prev_fire(self, now: _dt.datetime, max_days: int = 400) -> _dt.datetime | None:
        """Latest fire time <= now (minute resolution)."""
        now = now.replace(second=0, microsecond=0)
        tods = self.times_of_day()
        d = now.date()
        for i in range(max_days + 1):
            day = d - _dt.timedelta(days=i)
            if not self.day_matches(day):
                continue
            for h, m in reversed(tods):
                cand = now.replace(year=day.year, month=day.month, day=day.day, hour=h, minute=m)
                if cand <= now:
                    return cand
        return None

    def next_fire(self, now: _dt.datetime, max_days: int = 400) -> _dt.datetime | None:
        """Earliest fire time > now."""
        now = now.replace(second=0, microsecond=0)
        tods = self.times_of_day()
        d = now.date()
        for i in range(max_days + 1):
            day = d + _dt.timedelta(days=i)
            if not self.day_matches(day):
                continue
            for h, m in tods:
                cand = now.replace(year=day.year, month=day.month, day=day.day, hour=h, minute=m)
                if cand > now:
                    return cand
        return None

    def typical_interval(self, now: _dt.datetime | None = None) -> _dt.timedelta:
        """Largest gap between consecutive fires over the next ~2 cycles (for reporting)."""
        now = now or _dt.datetime(2026, 1, 1)
        t = self.next_fire(now)
        if t is None:
            return _dt.timedelta(days=366)
        gaps = []
        for _ in range(64):
            n = self.next_fire(t)
            if n is None:
                break
            gaps.append(n - t)
            t = n
        return max(gaps) if gaps else _dt.timedelta(days=366)


def parse(expr: str) -> Cron:
    raw = expr.strip()
    text = MACROS.get(raw.lower(), raw)
    parts = text.split()
    if len(parts) != 5:
        raise CronError(f"expected 5 fields, got {len(parts)}: {expr!r}")
    vals = []
    stars = []
    for (name, lo, hi, names), p in zip(FIELDS, parts):
        v, star = _parse_field(p, lo, hi, names)
        vals.append(v)
        stars.append(star)
    dow = frozenset(x % 7 for x in vals[4])
    return Cron(expr=raw, minute=vals[0], hour=vals[1], dom=vals[2], month=vals[3],
                dow=dow, dom_star=stars[2], dow_star=stars[4])


def is_cron(expr: str) -> bool:
    try:
        parse(expr)
        return True
    except (CronError, AttributeError):
        return False
