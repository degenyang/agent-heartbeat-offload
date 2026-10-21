import datetime as dt

import pytest

from aho.cronexpr import CronError, is_cron, parse


def test_basic_fields():
    c = parse("*/15 9-17 * * 1-5")
    assert c.minute == {0, 15, 30, 45}
    assert c.hour == set(range(9, 18))
    assert c.dow == {1, 2, 3, 4, 5}


def test_lists_ranges_steps_names():
    c = parse("5,10-20/5 */6 1,15 jan-mar mon,fri")
    assert c.minute == {5, 10, 15, 20}
    assert c.hour == {0, 6, 12, 18}
    assert c.dom == {1, 15}
    assert c.month == {1, 2, 3}
    assert c.dow == {1, 5}


def test_start_step_and_sunday_7():
    assert parse("5/20 * * * *").minute == {5, 25, 45}
    assert parse("0 0 * * 7").dow == {0}


def test_macros():
    assert parse("@daily").expr == "@daily"
    assert parse("@hourly").minute == {0}
    assert parse("@weekly").dow == {0}


@pytest.mark.parametrize("bad", ["", "* * * *", "61 * * * *", "0 9 L * *", "*/0 * * * *", "5-1 * * * *",
                                 "on_session_start"])
def test_invalid(bad):
    assert not is_cron(bad)
    with pytest.raises((CronError, AttributeError)):
        parse(bad)


def test_dom_dow_or_semantics():
    c = parse("0 7 1,15 * 1")          # 1st/15th OR Mondays
    assert c.day_matches(dt.date(2026, 1, 1))      # Thursday the 1st
    assert c.day_matches(dt.date(2026, 1, 5))      # Monday
    assert not c.day_matches(dt.date(2026, 1, 6))  # Tuesday the 6th


def test_dom_star_step_is_and():
    # '*'-prefixed DOM -> AND semantics (Vixie): odd days that are also Tuesdays
    c2 = parse("0 7 */2 * 2")
    assert not c2.day_matches(dt.date(2026, 1, 15))      # Thursday the 15th (odd, not Tuesday)
    assert c2.day_matches(dt.date(2026, 1, 13))          # Tuesday the 13th (odd)
    assert not c2.day_matches(dt.date(2026, 1, 6))       # Tuesday the 6th (even)


def test_prev_next_fire():
    c = parse("50 */2 * * *")
    now = dt.datetime(2026, 3, 10, 13, 5)
    assert c.prev_fire(now) == dt.datetime(2026, 3, 10, 12, 50)
    assert c.next_fire(now) == dt.datetime(2026, 3, 10, 14, 50)
    w = parse("3 9 * * 1")
    assert w.prev_fire(dt.datetime(2026, 3, 11, 0, 0)) == dt.datetime(2026, 3, 9, 9, 3)


def test_typical_interval():
    assert parse("*/10 * * * *").typical_interval() == dt.timedelta(minutes=10)
    assert parse("0 8 * * *").typical_interval() == dt.timedelta(days=1)
