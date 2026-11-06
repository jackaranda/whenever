"""Repeat rules: converting between RRULEs and the web calendar's repeat form.

The form covers the patterns people commonly use: every N days, weeks
(on chosen weekdays), months (on a day of the month, or on e.g. the second
Tuesday) or years, ending never, on a date or after a number of occurrences.
Any other RRULE, e.g. one made in another calendar app, is "custom": it is
shown as text and kept unchanged unless the user replaces it.
"""

from __future__ import annotations

import calendar as pycalendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}
_BYDAY_RE = re.compile(r"^([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)$")

Freq = Literal["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]
Weekday = Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


class RecurrenceError(ValueError):
    pass


class Repeat(BaseModel):
    freq: Freq
    interval: int = Field(1, ge=1, le=999)
    byday: list[Weekday] = []  # WEEKLY only
    monthly_mode: Literal["day", "weekday"] = "day"  # MONTHLY only
    nth: int | None = None  # MONTHLY weekday mode: 1-4, or -1 for "last"
    ends: Literal["never", "until", "count"] = "never"
    until: date | None = None  # last day on which an occurrence may start
    count: int | None = Field(None, ge=1, le=1000)


@dataclass
class RepeatInfo:
    kind: Literal["none", "simple", "custom"]
    repeat: Repeat | None = None
    text: str = ""


def monthly_nths(day: date) -> list[int]:
    """The ordinals that describe `day` within its month, e.g. [2] for the 2nd Tuesday, [4, -1] for the 4th (and last)."""
    nths = [(day.day - 1) // 7 + 1]
    if day.day + 7 > pycalendar.monthrange(day.year, day.month)[1]:
        nths.append(-1)
    return [n for n in nths if n in ORDINALS]


def _wall_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def build_rrule(rep: Repeat, start: date | datetime) -> dict:
    """Build the RRULE for a series starting at `start` (a date for all-day events, else an aware datetime)."""
    first = _wall_date(start)
    rule: dict = {"FREQ": rep.freq}
    if rep.interval > 1:
        rule["INTERVAL"] = rep.interval

    if rep.freq == "WEEKLY":
        # Always include the start's weekday, so the first occurrence matches the rule.
        days = set(rep.byday) | {WEEKDAYS[first.weekday()]}
        rule["BYDAY"] = [d for d in WEEKDAYS if d in days]
    elif rep.freq == "MONTHLY" and rep.monthly_mode == "weekday":
        allowed = monthly_nths(first)
        nth = rep.nth if rep.nth is not None else allowed[0]
        if nth not in allowed:
            raise RecurrenceError("The chosen week of the month doesn't match the start date.")
        rule["BYDAY"] = [f"{nth}{WEEKDAYS[first.weekday()]}"]

    if rep.ends == "until":
        if rep.until is None:
            raise RecurrenceError("Choose the date the series ends.")
        if rep.until < first:
            raise RecurrenceError("The series can't end before it starts.")
        if isinstance(start, datetime):
            # UNTIL must be UTC when DTSTART has a timezone (RFC 5545 §3.3.10).
            rule["UNTIL"] = datetime.combine(rep.until, time(23, 59, 59), start.tzinfo).astimezone(UTC)
        else:
            rule["UNTIL"] = rep.until
    elif rep.ends == "count":
        if rep.count is None:
            raise RecurrenceError("Choose how many times the event repeats.")
        rule["COUNT"] = rep.count
    return rule


def repeat_info(vevent) -> RepeatInfo:
    """Describe the master VEVENT's recurrence for the edit form."""
    rrule = vevent.get("RRULE")
    if rrule is None:
        if "RDATE" in vevent:
            return RepeatInfo("custom", text="Repeats on specific dates")
        return RepeatInfo("none")
    if isinstance(rrule, list):
        return RepeatInfo("custom", text="Custom repeat rule")
    custom = RepeatInfo("custom", text="Custom repeat rule: " + rrule.to_ical().decode())
    if "RDATE" in vevent:
        return custom

    start = vevent.decoded("DTSTART")
    first = _wall_date(start)
    rule = {key.upper(): list(value) if isinstance(value, (list, tuple)) else [value] for key, value in rrule.items()}
    rule.pop("WKST", None)
    freq = str(rule.pop("FREQ", [""])[0]).upper()
    if freq not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        return custom

    rep = Repeat(freq=freq)
    if "INTERVAL" in rule:
        rep.interval = int(rule.pop("INTERVAL")[0])
    if "COUNT" in rule and "UNTIL" in rule:
        return custom
    if "COUNT" in rule:
        rep.ends, rep.count = "count", int(rule.pop("COUNT")[0])
    if "UNTIL" in rule:
        until = rule.pop("UNTIL")[0]
        if isinstance(until, datetime) and isinstance(start, datetime) and start.tzinfo and until.tzinfo:
            until = until.astimezone(start.tzinfo)
        rep.ends, rep.until = "until", _wall_date(until)

    byday = [str(d).upper() for d in rule.pop("BYDAY", [])]
    bymonthday = rule.pop("BYMONTHDAY", None)
    bymonth = rule.pop("BYMONTH", None)
    if rule:  # BYSETPOS, BYHOUR, ... aren't representable in the form
        return custom
    if bymonthday is not None and (freq not in {"MONTHLY", "YEARLY"} or [int(x) for x in bymonthday] != [first.day]):
        return custom
    if bymonth is not None and (freq != "YEARLY" or [int(x) for x in bymonth] != [first.month]):
        return custom

    if freq == "WEEKLY":
        if any(d not in WEEKDAYS for d in byday):
            return custom
        rep.byday = byday or [WEEKDAYS[first.weekday()]]
    elif freq == "MONTHLY" and byday:
        match = _BYDAY_RE.match(byday[0]) if len(byday) == 1 else None
        if not match or not match.group(1) or bymonthday is not None:
            return custom
        nth, weekday = int(match.group(1)), match.group(2)
        if weekday != WEEKDAYS[first.weekday()] or nth not in monthly_nths(first):
            return custom
        rep.monthly_mode, rep.nth = "weekday", nth
    elif byday:
        return custom

    return RepeatInfo("simple", rep, describe(rep, first))


def describe(rep: Repeat, first: date) -> str:
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}[rep.freq]
    if rep.interval == 1:
        text = {"DAILY": "Daily", "WEEKLY": "Weekly", "MONTHLY": "Monthly", "YEARLY": "Yearly"}[rep.freq]
    else:
        text = f"Every {rep.interval} {unit}s"

    if rep.freq == "WEEKLY":
        days = rep.byday or [WEEKDAYS[first.weekday()]]
        if set(days) == set(WEEKDAYS[:5]) and len(days) == 5:
            text += " on weekdays"
        else:
            text += " on " + ", ".join(WEEKDAY_NAMES[WEEKDAYS.index(d)][:3] for d in WEEKDAYS if d in days)
    elif rep.freq == "MONTHLY":
        if rep.monthly_mode == "weekday":
            nth = rep.nth if rep.nth is not None else monthly_nths(first)[0]
            text += f" on the {ORDINALS[nth]} {WEEKDAY_NAMES[first.weekday()]}"
        else:
            text += f" on day {first.day}"
    elif rep.freq == "YEARLY":
        text += f" on {first.day} {pycalendar.month_name[first.month]}"

    if rep.ends == "until" and rep.until:
        text += f", until {rep.until.day} {pycalendar.month_abbr[rep.until.month]} {rep.until.year}"
    elif rep.ends == "count" and rep.count:
        text += ", once" if rep.count == 1 else f", {rep.count} times"
    return text


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError) as exc:  # ZoneInfoNotFoundError is a KeyError
        raise RecurrenceError(f"Unknown timezone '{name}'.") from exc
