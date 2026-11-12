"""When workers may measure: days of the week and a time window, shared by every worker.

    <work_dir>/schedule.json   {"enabled": true, "days": [0, 1, 2, 3], "start": "02:00", "end": "10:00"}

Outside the window workers behave as if paused: running tracks are frozen (progress kept) and no
new ones start; they carry on when the window opens again. A window that crosses midnight
(22:00-06:00) belongs to the day it starts on. Start == end means the whole day. Times are each
worker's local time (its TZ). No file, or enabled false, means 24x7.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_time(text: str) -> str:
    """'2', '02', '2:30', '02:30', '14:00' -> 'HH:MM'."""
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*", text or "")
    if not match:
        raise ValueError(f"time {text!r}: use HH:MM, e.g. 02:00")
    hour, minute = int(match[1]), int(match[2] or 0)
    if hour == 24 and minute == 0:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"time {text!r}: use HH:MM between 00:00 and 23:59")
    return f"{hour:02d}:{minute:02d}"


def parse_days(text: str) -> list[int]:
    """'mon-thu', 'mon,wed,fri', 'sat-sun', 'all', 'weekdays', 'weekends' -> [0..6] (Monday = 0)."""
    text = (text or "").strip().lower()
    named = {"all": "mon-sun", "daily": "mon-sun", "weekdays": "mon-fri", "weekends": "sat-sun"}
    text = named.get(text, text)
    days: set[int] = set()
    for part in filter(None, (p.strip() for p in text.split(","))):
        ends = [p.strip()[:3] for p in part.split("-")]
        if len(ends) > 2 or any(e not in DAYS for e in ends):
            raise ValueError(f"days {part!r}: use mon,tue,... or a range like mon-thu")
        first, last = DAYS.index(ends[0]), DAYS.index(ends[-1])
        day = first
        while True:                       # fri-mon wraps over the weekend
            days.add(day)
            if day == last:
                break
            day = (day + 1) % 7
    if not days:
        raise ValueError("pick at least one day")
    return sorted(days)


def describe_days(days: list[int]) -> str:
    """[0, 1, 2, 3, 5] -> 'Mon-Thu, Sat'."""
    days = sorted(set(days))
    if len(days) == 7:
        return "every day"
    runs, start = [], None
    for i, day in enumerate(days):
        if start is None:
            start = day
        if i + 1 == len(days) or days[i + 1] != day + 1:
            runs.append(DAYS[start].title() if start == day else f"{DAYS[start].title()}-{DAYS[day].title()}")
            start = None
    return ", ".join(runs)


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


@dataclass
class Schedule:
    enabled: bool = False
    days: list[int] = field(default_factory=lambda: list(range(7)))
    start: str = "02:00"
    end: str = "10:00"

    def is_open(self, now: dt.datetime | None = None) -> bool:
        if not self.enabled:
            return True
        now = now or dt.datetime.now()
        start, end = _minutes(self.start), _minutes(self.end)
        minute, day = now.hour * 60 + now.minute, now.weekday()
        if start == end:                                  # the whole day
            return day in self.days
        if start < end:
            return day in self.days and start <= minute < end
        if minute >= start:                               # crosses midnight: tonight's part
            return day in self.days
        return minute < end and (day - 1) % 7 in self.days   # ...and the morning after

    def next_change(self, now: dt.datetime | None = None) -> dt.datetime | None:
        """When is_open() next flips (to the minute), or None if it never does."""
        if not self.enabled:
            return None
        now = (now or dt.datetime.now()).replace(second=0, microsecond=0)
        state = self.is_open(now)
        midnight = now.replace(hour=0, minute=0)
        candidates = []
        for offset in range(9):
            day = midnight + dt.timedelta(days=offset)
            for hhmm in (self.start, self.end, "00:00"):
                candidates.append(day + dt.timedelta(minutes=_minutes(hhmm)))
        for moment in sorted(c for c in candidates if c > now):
            if self.is_open(moment) != state:
                return moment
        return None

    def describe(self) -> str:
        if not self.enabled:
            return "off (24x7)"
        hours = "all day" if self.start == self.end else f"{self.start}-{self.end}"
        return f"{describe_days(self.days)} {hours}"

    def status(self, now: dt.datetime | None = None) -> str:
        """'Mon-Thu 02:00-10:00 · open until 10:00' / '... · closed, opens Mon 02:00'."""
        if not self.enabled:
            return self.describe()
        now = now or dt.datetime.now()
        change = self.next_change(now)
        when = ""
        if change:
            when = change.strftime("%H:%M") if change.date() == now.date() else change.strftime("%a %H:%M")
        if self.is_open(now):
            return f"{self.describe()} · open" + (f" until {when}" if when else "")
        return f"{self.describe()} · closed" + (f", opens {when}" if when else "")

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "days": sorted(set(self.days)), "start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: dict | None) -> Schedule:
        data = data or {}
        try:
            return cls(enabled=bool(data.get("enabled", False)),
                       days=sorted({int(d) for d in data.get("days", range(7)) if 0 <= int(d) <= 6}),
                       start=parse_time(str(data.get("start", "02:00"))),
                       end=parse_time(str(data.get("end", "10:00"))))
        except (TypeError, ValueError):
            return cls()          # a broken file shouldn't stop the workers: run 24x7


def schedule_path(work_dir: str | Path) -> Path:
    return Path(work_dir) / "schedule.json"


def load_schedule(work_dir: str | Path) -> Schedule:
    try:
        with open(schedule_path(work_dir)) as handle:
            return Schedule.from_dict(json.load(handle))
    except (OSError, ValueError):
        return Schedule()


def save_schedule(work_dir: str | Path, schedule: Schedule) -> None:
    path = schedule_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as handle:
        json.dump(schedule.as_dict(), handle)
    try:
        os.chmod(tmp, 0o666)      # controller and workers may run as different users
    except OSError:
        pass
    os.replace(tmp, path)
