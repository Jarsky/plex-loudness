import datetime as dt

import pytest

from plex_loudness.schedule import Schedule, describe_days, load_schedule, parse_days, parse_time, save_schedule

MON = dt.datetime(2026, 11, 9)          # a Monday


def at(day: int, hhmm: str) -> dt.datetime:
    hour, minute = map(int, hhmm.split(":"))
    return MON + dt.timedelta(days=day, hours=hour, minutes=minute)


def test_parsing_days_and_times():
    assert parse_days("mon-thu") == [0, 1, 2, 3]
    assert parse_days("Mon, wed,FRI") == [0, 2, 4]
    assert parse_days("fri-mon") == [0, 4, 5, 6]          # wraps over the weekend
    assert parse_days("weekdays") == [0, 1, 2, 3, 4] and parse_days("all") == list(range(7))
    with pytest.raises(ValueError):
        parse_days("funday")
    assert parse_time("2") == "02:00" and parse_time("14:30") == "14:30" and parse_time("24:00") == "00:00"
    with pytest.raises(ValueError):
        parse_time("25:00")
    assert describe_days([0, 1, 2, 3, 5]) == "Mon-Thu, Sat"


def test_off_means_24x7():
    s = Schedule(enabled=False, days=[0], start="02:00", end="03:00")
    assert s.is_open(at(4, "12:00")) and s.next_change(at(4, "12:00")) is None


def test_daytime_window_on_chosen_days():
    s = Schedule(enabled=True, days=[0, 1, 2, 3], start="02:00", end="10:00")
    assert not s.is_open(at(0, "01:59"))
    assert s.is_open(at(0, "02:00")) and s.is_open(at(3, "09:59"))
    assert not s.is_open(at(3, "10:00"))
    assert not s.is_open(at(4, "03:00"))                  # Friday isn't picked
    assert s.next_change(at(0, "05:00")) == at(0, "10:00")
    assert s.next_change(at(3, "11:00")) == at(7, "02:00")  # Thursday after hours -> next Monday
    assert s.status(at(3, "11:00")).endswith("closed, opens Mon 02:00")


def test_window_across_midnight_belongs_to_the_day_it_starts():
    s = Schedule(enabled=True, days=[4], start="22:00", end="06:00")   # Friday night
    assert s.is_open(at(4, "23:00")) and s.is_open(at(5, "05:59"))     # ...into Saturday morning
    assert not s.is_open(at(5, "06:00")) and not s.is_open(at(5, "23:00"))
    assert not s.is_open(at(4, "05:00"))                               # Thursday night wasn't picked
    assert s.next_change(at(4, "23:00")) == at(5, "06:00")


def test_same_start_and_end_is_all_day():
    s = Schedule(enabled=True, days=[5, 6], start="00:00", end="00:00")
    assert s.is_open(at(5, "00:00")) and s.is_open(at(6, "23:59")) and not s.is_open(at(0, "12:00"))
    assert s.next_change(at(5, "10:00")) == at(7, "00:00")


def test_saved_and_loaded_and_a_broken_file_runs_24x7(tmp_path):
    assert not load_schedule(tmp_path).enabled
    save_schedule(tmp_path, Schedule(enabled=True, days=[0, 1], start="01:00", end="07:30"))
    assert load_schedule(tmp_path) == Schedule(enabled=True, days=[0, 1], start="01:00", end="07:30")
    (tmp_path / "schedule.json").write_text('{"enabled": true, "start": "nonsense"}')
    assert not load_schedule(tmp_path).enabled
