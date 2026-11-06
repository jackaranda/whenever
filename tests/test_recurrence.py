from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

from whenever.web.recurrence import RecurrenceError, Repeat, build_rrule, describe, monthly_nths, repeat_info

LONDON = ZoneInfo("Europe/London")


def master(rrule: str, dtstart: str = "DTSTART;TZID=Europe/London:20261110T090000", extra: str = ""):
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:u\r\n"
        f"{dtstart}\r\n" + (f"RRULE:{rrule}\r\n" if rrule else "") + extra + "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return Calendar.from_ical(text).walk("VEVENT")[0]


def test_monthly_nths():
    assert monthly_nths(date(2026, 11, 10)) == [2]   # 2nd Tuesday
    assert monthly_nths(date(2026, 11, 24)) == [4, -1]  # 4th and last Tuesday
    assert monthly_nths(date(2026, 12, 29)) == [-1]  # 5th Tuesday: only "last"


def test_build_weekly_adds_start_weekday_and_utc_until():
    start = datetime(2026, 11, 10, 9, tzinfo=LONDON)  # a Tuesday
    rule = build_rrule(Repeat(freq="WEEKLY", byday=["TH"], ends="until", until=date(2026, 12, 31)), start)
    assert rule["BYDAY"] == ["TU", "TH"]
    assert rule["UNTIL"] == datetime(2026, 12, 31, 23, 59, 59, tzinfo=LONDON).astimezone(ZoneInfo("UTC"))


def test_build_all_day_until_is_a_date():
    rule = build_rrule(Repeat(freq="DAILY", ends="until", until=date(2026, 11, 20)), date(2026, 11, 10))
    assert rule["UNTIL"] == date(2026, 11, 20)


def test_build_monthly_weekday_validates_nth():
    start = datetime(2026, 11, 24, 9, tzinfo=LONDON)
    assert build_rrule(Repeat(freq="MONTHLY", monthly_mode="weekday", nth=-1), start)["BYDAY"] == ["-1TU"]
    with pytest.raises(RecurrenceError):
        build_rrule(Repeat(freq="MONTHLY", monthly_mode="weekday", nth=2), start)


def test_build_rejects_until_before_start():
    with pytest.raises(RecurrenceError):
        build_rrule(Repeat(freq="DAILY", ends="until", until=date(2026, 1, 1)), date(2026, 11, 10))


@pytest.mark.parametrize("rrule, text", [
    ("FREQ=WEEKLY;BYDAY=TU,TH", "Weekly on Tue, Thu"),
    ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TU,WE,TH,FR", "Every 2 weeks on weekdays"),
    ("FREQ=MONTHLY;BYDAY=2TU;COUNT=6", "Monthly on the second Tuesday, 6 times"),
    ("FREQ=MONTHLY", "Monthly on day 10"),
    ("FREQ=YEARLY", "Yearly on 10 November"),
    ("FREQ=DAILY;UNTIL=20261130T235959Z", "Daily, until 30 Nov 2026"),
])
def test_repeat_info_simple(rrule, text):
    info = repeat_info(master(rrule))
    assert info.kind == "simple"
    assert info.text == text
    # and building it again gives an equivalent rule
    rebuilt = build_rrule(info.repeat, datetime(2026, 11, 10, 9, tzinfo=LONDON))
    assert rebuilt["FREQ"] == info.repeat.freq


@pytest.mark.parametrize("rrule, extra", [
    ("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", ""),
    ("FREQ=DAILY;COUNT=3;UNTIL=20261130T000000Z", ""),
    ("FREQ=MONTHLY;BYMONTHDAY=1,15", ""),
    ("FREQ=WEEKLY", "RDATE;TZID=Europe/London:20261201T090000\r\n"),
])
def test_repeat_info_custom(rrule, extra):
    assert repeat_info(master(rrule, extra=extra)).kind == "custom"


def test_repeat_info_none():
    assert repeat_info(master("")).kind == "none"


def test_describe_count_once():
    assert describe(Repeat(freq="DAILY", ends="count", count=1), date(2026, 11, 10)) == "Daily, once"
