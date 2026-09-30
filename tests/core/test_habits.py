"""Hand changes, remembered, and what the page suggests from them."""

from datetime import timedelta

from custom_components.room_routines.core.habits import (
    KEEP_DARK,
    SAVE_LOOK,
    START_EARLIER,
    START_LATER,
    Change,
    change_from,
    change_to,
    similar,
    suggest,
)
from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import (
    ADJUSTED,
    SWITCHED_OFF,
    Room,
    RoomConfig,
)
from custom_components.room_routines.core.tracks import DIM, NORMAL

from .conftest import at

CEILING = "light.kitchen_ceiling"
MOTION = "binary_sensor.kitchen_motion"
DAY = Look({CEILING: LightTarget(True, 100)})
EVENING = Look({CEILING: LightTarget(True, 30)})
LOOKS = {"Day": DAY, "Evening": EVENING}
NOW = at(30, 12)


def look_of(period, track):
    return LOOKS.get(period)


def change(day, hour, minute=0, kind=ADJUSTED, pct=30.0, period="Day", after=60.0, track=NORMAL, month=9):
    lights = {CEILING: LightTarget(True, pct)} if kind == ADJUSTED else None
    return Change(at(day, hour, minute, month=month), "k", kind, period, track, after, lights)


def run(changes):
    return suggest(changes, "k", "Kitchen", default_schedule(), look_of, NOW)


def test_one_odd_evening_suggests_nothing():
    assert run([change(29, 16, 30)]) == []


def test_needs_enough_changes_on_enough_days():
    # Four changes, but all on the same two days.
    assert run([change(28, 16, 30), change(28, 16, 40), change(29, 16, 30), change(29, 16, 45)]) == []


def test_changed_to_the_next_periods_look_near_its_start_suggests_starting_it_earlier():
    changes = [change(d, 16, m) for d, m in ((25, 10), (26, 25), (27, 20), (28, 30))]
    [s] = run(changes)
    assert s.kind == START_EARLIER
    assert s.move_period == "Evening"
    assert s.new_start.strftime("%H:%M") == "16:15"
    assert "Start Evening at 16:15" in s.text


def test_changed_back_to_the_previous_look_soon_after_a_period_starts_suggests_starting_it_later():
    changes = [change(d, 17, m, pct=100, period="Evening") for d, m in ((25, 10), (26, 30), (27, 20), (28, 40))]
    [s] = run(changes)
    assert s.kind == START_LATER
    assert s.move_period == "Evening"
    assert s.new_start.strftime("%H:%M") == "17:30"


def test_changed_some_other_way_suggests_saving_the_latest_change_as_the_look():
    changes = [change(d, 12, 0, pct=p) for d, p in ((25, 70), (26, 70), (27, 72), (28, 68))]
    [s] = run(changes)
    assert s.kind == SAVE_LOOK
    assert s.look.lights[CEILING].brightness_pct == 68


def test_no_save_suggestion_once_the_look_already_matches():
    changes = [change(d, 20, 0, pct=32, period="Evening") for d in (25, 26, 27, 28)]
    assert run(changes) == []  # Evening is already 30 %


def test_switched_off_straight_away_again_and_again_suggests_keeping_dark():
    changes = [change(d, 12, kind=SWITCHED_OFF, after=20) for d in (24, 25, 26, 28)]
    [s] = run(changes)
    assert s.kind == KEEP_DARK
    assert s.key == "k|Day|normal|keep_dark"


def test_dim_days_are_counted_apart_from_normal_ones():
    changes = [change(d, 12, pct=70, track=DIM) for d in (25, 26, 27, 28)]
    [s] = run(changes)
    assert s.track == DIM
    assert "on Dim days" in s.text


def test_old_changes_are_forgotten():
    changes = [change(d, 12, pct=70, month=8) for d in (25, 26, 27, 28)]
    assert run(changes) == []


def test_similar_ignores_small_brightness_differences():
    assert similar({CEILING: LightTarget(True, 35)}, EVENING)
    assert not similar({CEILING: LightTarget(True, 60)}, EVENING)
    assert not similar({CEILING: LightTarget(False)}, EVENING)
    assert not similar({CEILING: LightTarget(True, 30)}, Look(scene="scene.x"))


def test_changes_round_trip_through_storage():
    c = change(28, 16, 30)
    assert change_from(change_to(c)) == c
    off = change(28, 16, 30, kind=SWITCHED_OFF)
    assert change_from(change_to(off)) == off


# ---- the room reports them -----------------------------------------------------

CFG = RoomConfig("Kitchen", (CEILING,), (MOTION,), looks=LOOKS)


def test_room_reports_a_hand_adjustment_with_period_track_and_time_since_on():
    r = Room(CFG, default_schedule(), "Day", False, at(30, 12), track=DIM)
    r.sensor(MOTION, True, at(30, 12))
    d = r.lights(True, own=False, now=at(30, 12, 3))
    assert d.change.kind == ADJUSTED
    assert (d.change.period, d.change.track) == ("Day", DIM)
    assert d.change.after == timedelta(minutes=3)


def test_room_reports_a_hand_switch_off_only_when_it_had_switched_the_lights_on():
    r = Room(CFG, default_schedule(), "Day", False, at(30, 12))
    r.sensor(MOTION, True, at(30, 12))
    assert r.lights(False, own=False, now=at(30, 12, 0, 20)).change.kind == SWITCHED_OFF
    r2 = Room(CFG, default_schedule(), "Day", False, at(30, 12))
    r2.lights(True, own=False, now=at(30, 12))  # switched on by hand: manual
    assert r2.lights(False, own=False, now=at(30, 12, 1)).change is None


def test_own_changes_are_not_hand_changes():
    r = Room(CFG, default_schedule(), "Day", False, at(30, 12))
    r.sensor(MOTION, True, at(30, 12))
    assert r.lights(True, own=True, now=at(30, 12, 0, 1)).change is None
