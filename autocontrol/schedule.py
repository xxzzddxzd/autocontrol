"""Service-day calendar and wake times: the single time source of the console."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import math

BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')
SAME_TIME = timedelta(seconds=120)  # wakes this close are the same wake
QUIET = timedelta(minutes=10)  # server times this close to the day reset never wake


def _clock(text: str) -> tuple[int, int]:
    hour, minute = map(int, text.split(':'))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f'invalid clock time {text!r}')
    return hour, minute


@dataclass(frozen=True)
class Schedule:
    """Fixed daily wakes plus the server's day boundary, in one fixed-offset zone.

    ``slots`` gives a phase its own times, e.g. ``(('cardbook', ('06:00',
    '14:00', '22:00')),)``: that phase runs only at its slots (a slot that is
    also a fixed time joins that full round) and is left out of every other
    wake. Each slot opens a window that lasts until the next slot or the day
    reset; a window without a successful run is caught up at the next wake.
    """

    times: tuple[str, ...]
    reset: str = '05:00'
    tz: tzinfo = BEIJING
    tz_name: str = 'Asia/Shanghai'
    slots: tuple = ()
    # Phases a server-reported time wakes (None: every phase without slots).
    dynamic_phases: tuple | None = None

    def __post_init__(self):
        if not self.times:
            raise ValueError('a schedule needs at least one wake time')
        for value in (*self.times, self.reset):
            _clock(value)
        phases = [phase for phase, _ in self.slots]
        if len(phases) != len(set(phases)):
            raise ValueError('a phase has one slot list')
        for _, texts in self.slots:
            if not texts or len(set(texts)) != len(texts):
                raise ValueError('slots must be distinct clock times')
            for value in texts:
                _clock(value)

    @property
    def slot_map(self) -> dict[str, tuple[str, ...]]:
        return {phase: tuple(texts) for phase, texts in self.slots}

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def local(self, value=None) -> datetime | None:
        """Parse a datetime, epoch seconds or aware ISO text; ``None`` means now."""
        if value is None:
            return self.now()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=self.tz)
            return value.astimezone(self.tz)
        if type(value) in (int, float):
            try:
                if not math.isfinite(value):
                    return None
                return datetime.fromtimestamp(value, self.tz)
            except (ValueError, OverflowError, OSError):
                return None
        if type(value) is str and len(value) <= 40:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed.astimezone(self.tz) if parsed.tzinfo is not None else None
        return None

    def _require(self, value) -> datetime:
        current = self.local(value)
        if current is None:
            raise ValueError('invalid board time')
        return current

    def _at(self, day: date, text: str) -> datetime:
        hour, minute = _clock(text)
        return datetime.combine(day, time(hour, minute), self.tz)

    @property
    def _late_reset(self) -> bool:
        # A day that starts in the afternoon or later mostly falls on the next date.
        return _clock(self.reset) >= (12, 0)

    def service_day(self, now=None) -> str:
        """The server day, named by the date it mostly falls on.

        A 05:00 reset names 11-17 05:00 → 11-18 05:00 "11-17"; a 23:00 reset
        names 11-17 23:00 → 11-18 23:00 "11-18".
        """
        hour, minute = _clock(self.reset)
        start = (self._require(now) - timedelta(hours=hour, minutes=minute)).date()
        return (start + timedelta(days=1 if self._late_reset else 0)).isoformat()

    def day_start(self, day: str) -> datetime:
        """The reset moment that begins the given service day."""
        start = date.fromisoformat(day) - timedelta(days=1 if self._late_reset else 0)
        return self._at(start, self.reset)

    def day_anchor(self, day: str) -> datetime:
        """A moment that is certainly inside the given service day."""
        return self.day_start(day) + timedelta(hours=1)

    def on_day(self, day: str, text: str) -> datetime:
        """The clock time ``text`` within the given service day."""
        start = self.day_start(day)
        stamp = self._at(start.date(), text)
        return stamp if stamp >= start else stamp + timedelta(days=1)

    def slot_stamps(self, phase: str, day: str) -> list[datetime]:
        """The phase's slots within one service day, in order."""
        texts = self.slot_map.get(phase, ())
        return sorted(self.on_day(day, text) for text in texts)

    def slot_window(self, phase: str, when) -> int | None:
        """Index of the slot window holding ``when``; ``None`` before the first slot."""
        current = self._require(when)
        stamps = self.slot_stamps(phase, self.service_day(current))
        index = None
        for position, stamp in enumerate(stamps):
            if current >= stamp - SAME_TIME:
                index = position
        return index

    def is_slot(self, phase: str, when) -> bool:
        current = self._require(when)
        return any(
            abs(current - stamp) < SAME_TIME
            for stamp in self.slot_stamps(phase, self.service_day(current))
        )

    def is_fixed(self, when) -> bool:
        current = self._require(when)
        return any(
            abs(current - self._at(current.date() + timedelta(days=offset), text)) < SAME_TIME
            for offset in (-1, 0, 1)
            for text in self.times
        )

    def quiet(self, when) -> bool:
        """Close to the day reset: the reset discovery and the first round cover it."""
        current = self._require(when)
        reset = self._at(current.date(), self.reset)
        return any(abs(current - (reset + timedelta(days=day))) < QUIET for day in (-1, 0, 1))

    def dynamic_stamps(self, dynamic=()) -> list[datetime]:
        """Server-reported wake times, minus those inside the reset's quiet window."""
        stamps = []
        for value in dynamic:
            stamp = self.local(value) if value is not None else None
            if stamp is not None and not self.quiet(stamp):
                stamps.append(stamp)
        return stamps

    def is_dynamic(self, when, dynamic=()) -> bool:
        current = self._require(when)
        return any(abs(current - stamp) < SAME_TIME for stamp in self.dynamic_stamps(dynamic))

    def is_regular(self, when, dynamic=()) -> bool:
        """A fixed time or a server-reported time (not only some phase's slot)."""
        return self.is_fixed(when) or self.is_dynamic(when, dynamic)

    def next_check(self, now=None, dynamic=()) -> datetime:
        """Next fixed wake or slot, or an earlier future time the server reported."""
        current = self._require(now)
        texts = (*self.times, *(text for _, slots in self.slots for text in slots))
        candidates = [
            stamp
            for offset in (0, 1)
            for text in texts
            if (stamp := self._at(current.date() + timedelta(days=offset), text)) > current
        ]
        candidates.extend(stamp for stamp in self.dynamic_stamps(dynamic) if stamp > current)
        return min(candidates)

    def next_reset(self, now=None) -> datetime:
        current = self._require(now)
        stamp = self._at(current.date(), self.reset)
        return stamp if stamp > current else stamp + timedelta(days=1)

    def next_wake(self, now=None, dynamic=()) -> tuple[datetime, bool]:
        """``(due, observe_only)``; the day reset wakes only for read-only discovery."""
        ordinary = self.next_check(now, dynamic)
        reset = self.next_reset(now)
        return (reset, True) if reset < ordinary else (ordinary, False)
