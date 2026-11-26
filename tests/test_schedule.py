from datetime import datetime, timedelta

import pytest

from autocontrol.projects.yslzm.catalog import SCHEDULE
from autocontrol.schedule import BEIJING, Schedule

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=BEIJING)


def test_server_day_boundary_includes_early_morning_in_previous_day():
    assert SCHEDULE.service_day(NOW.replace(hour=4, minute=59)) == '2026-11-16'
    assert SCHEDULE.service_day(NOW.replace(hour=5, minute=0)) == '2026-11-17'
    assert SCHEDULE.day_anchor('2026-11-16') == datetime(2026, 11, 16, 6, 0, tzinfo=BEIJING)


def test_fixed_and_dynamic_wakes_share_one_time_source():
    assert SCHEDULE.next_check(NOW) == NOW.replace(hour=13, minute=15)
    later = NOW + timedelta(minutes=3)
    assert SCHEDULE.next_check(NOW, [NOW - timedelta(seconds=1), later.timestamp()]) == later
    assert SCHEDULE.next_check(NOW, [float('nan'), 'bad', 10**500, None]) == NOW.replace(
        hour=13, minute=15
    )
    # 22:00 is the 事件簿 slot; the next fixed wake after it is 01:15.
    assert SCHEDULE.next_check(NOW.replace(hour=21, minute=30)) == NOW.replace(hour=22, minute=0)
    assert SCHEDULE.next_check(NOW.replace(hour=22, minute=1)) == datetime(
        2026, 11, 18, 4, 30, tzinfo=BEIJING
    )
    # Server times within 10 minutes of the 05:00 reset never wake.
    near = [datetime(2026, 11, 18, 4, 59, 59, tzinfo=BEIJING).timestamp()]
    assert SCHEDULE.next_check(datetime(2026, 11, 18, 4, 40, tzinfo=BEIJING), near) == datetime(
        2026, 11, 18, 6, 0, tzinfo=BEIJING
    )


def _at(hour, minute=0, day=17):
    return datetime(2026, 11, day, hour, minute, tzinfo=BEIJING)


def test_slot_windows_and_wake_kinds():
    at = _at
    assert [stamp.hour for stamp in SCHEDULE.slot_stamps('cardbook', '2026-11-17')] == [6, 14, 22]
    assert SCHEDULE.slot_window('cardbook', at(5, 30)) is None  # before the first slot
    assert SCHEDULE.slot_window('cardbook', at(6)) == 0
    assert SCHEDULE.slot_window('cardbook', at(13, 59)) == 1  # a slot wake may fire a little early
    assert SCHEDULE.slot_window('cardbook', at(21, 30)) == 1
    assert SCHEDULE.slot_window('cardbook', at(2, 0, day=18)) == 2  # still the 17th's service day
    assert SCHEDULE.is_slot('cardbook', at(14)) and not SCHEDULE.is_slot('cardbook', at(13, 15))
    assert SCHEDULE.is_regular(at(13, 15)) and SCHEDULE.is_regular(at(6))
    assert not SCHEDULE.is_regular(at(14))
    assert SCHEDULE.is_regular(at(14), [at(14).timestamp()])  # a server time joins the wake


def test_reset_wakes_for_read_only_discovery():
    due, observe = SCHEDULE.next_wake(datetime(2026, 11, 17, 4, 59, tzinfo=BEIJING))
    assert (due.hour, due.minute, observe) == (5, 0, True)
    candidate = datetime(2026, 11, 17, 10, 30, tzinfo=BEIJING)
    due, observe = SCHEDULE.next_wake(
        datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING), [candidate.timestamp()]
    )
    assert (due, observe) == (candidate, False)


def test_parsing_accepts_only_aware_or_epoch_values():
    assert SCHEDULE.local('2026-11-17T09:00:00') is None
    assert SCHEDULE.local('2026-11-17T01:00:00+00:00') == NOW
    assert SCHEDULE.local(NOW.timestamp()) == NOW
    assert SCHEDULE.local(datetime(2026, 11, 17, 9, 0)) == NOW
    with pytest.raises(ValueError):
        SCHEDULE.service_day('garbage')


def test_invalid_schedules_are_rejected():
    with pytest.raises(ValueError):
        Schedule(times=())
    with pytest.raises(ValueError):
        Schedule(times=('25:00',))


def _late_at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=BEIJING)


def test_a_day_that_resets_late_is_named_by_the_date_it_mostly_falls_on():
    late = Schedule(times=('00:05', '06:05', '12:05', '18:05'), reset='23:00')
    at = _late_at
    assert late.service_day(at(17, 22, 59)) == '2026-11-17'
    assert late.service_day(at(17, 23, 30)) == '2026-11-18'
    assert late.service_day(at(18, 12)) == '2026-11-18'
    assert late.day_start('2026-11-18') == at(17, 23)
    assert late.on_day('2026-11-18', '23:30') == at(17, 23, 30)
    assert late.on_day('2026-11-18', '06:05') == at(18, 6, 5)
    assert late.service_day(late.day_anchor('2026-11-18')) == '2026-11-18'
    # An early reset keeps naming the day by the date it starts on.
    assert SCHEDULE.service_day(at(18, 4, 59)) == '2026-11-17'
    assert SCHEDULE.on_day('2026-11-17', '01:15') == at(18, 1, 15)


def test_a_slot_with_a_deadline_is_never_caught_up_after_it():
    boat = (('boat', ('18:45',)),)
    schedule = Schedule(
        times=('09:00',), reset='23:00', slots=boat, slot_until=(('boat', ('18:57',)),)
    )

    def at(text):
        return datetime.fromisoformat(f'2026-11-18T{text}:00+08:00')

    day = schedule.service_day(at('18:50'))
    assert schedule.slot_deadline('boat', day, 0) == at('18:57')
    assert schedule.slot_due('boat', at('18:50'))  # still inside its window: retried
    assert not schedule.slot_due('boat', at('18:50'), {0})  # already ran
    assert not schedule.slot_due('boat', at('19:31'))  # missed: never caught up
    open_ended = Schedule(times=('09:00',), reset='23:00', slots=boat)
    assert open_ended.slot_due('boat', at('19:31'))  # without a deadline: caught up later
    with pytest.raises(ValueError):
        Schedule(times=('09:00',), slots=boat, slot_until=(('boat', ('18:57', '19:00')),))
    with pytest.raises(ValueError):
        Schedule(times=('09:00',), slots=boat, slot_until=(('boat', ('18:45',)),))


def test_idle_times_replace_a_phases_slots_while_its_work_is_out():
    schedule = Schedule(
        times=('12:00',),
        slots=(('ship', ('06:00', '08:00', '10:00')),),
        slot_until=(('ship', ('06:30', '08:30', '10:30')),),
        idle=(('ship', 'insufficient_materials', ('06:00',)),),
    )
    assert schedule.idle_reasons == {'ship': 'insufficient_materials'}
    assert schedule.with_idle(frozenset()) is schedule
    idle = schedule.with_idle({'ship'})
    assert idle.slot_map == {'ship': ('06:00',)} and idle.slot_until == ()
    for bad in (
        (('ghost', 'x', ('06:00',)),),
        (('ship', 'x', ()),),
        (('ship', '', ('06:00',)),),
        (('ship', 'x', ('06:00',)), ('ship', 'y', ('07:00',))),
    ):
        with pytest.raises(ValueError):
            Schedule(times=('12:00',), slots=(('ship', ('06:00',)),), idle=bad)
