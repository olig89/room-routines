"""The Timeline's lanes: worked out from the same settings the rooms run on."""

from datetime import date, time

from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import Period, Schedule
from custom_components.room_routines.core.room import RoomConfig
from custom_components.room_routines.core.rules import Condition, Rule
from custom_components.room_routines.core.signals import Signal
from custom_components.room_routines.core.timeline import DayFacts, day_spans, room_lanes
from custom_components.room_routines.core.timers import Timer

THURSDAY = date(2026, 10, 15)
SCHEDULE = Schedule((
    Period("Morning", time(6, 30)), Period("Day", time(9, 0)), Period("Late Afternoon", time(17, 0)),
    Period("Bedtime", time(20, 0)), Period("Overnight", time(23, 0)),
))
LIGHT = "light.ceiling"
DIM = Look({LIGHT: LightTarget(True, 25, 2700)})
BRIGHT = Look({LIGHT: LightTarget(True, 50, 2700)})
NIGHT = Look({LIGHT: LightTarget(True, 1, 2200)})
GAME = Look(scene="scene.game_time")


def facts(**over):
    base = dict(today=THURSDAY, workday_today=True, dark_possible=False, dark_periods=("Morning", "Day"))
    base.update(over)
    return DayFacts(**base)


def toilet(**over):
    base = dict(name="Toilet", lights=(LIGHT,), triggers=("binary_sensor.motion",),
                looks={"Morning": DIM, "Day": BRIGHT, "Bedtime": DIM, "Overnight": NIGHT})
    base.update(over)
    return RoomConfig(**base)


def office(**over):
    base = dict(name="Office", lights=(LIGHT,), triggers=(),
                looks={"Day": BRIGHT, "Bedtime": DIM, "Overnight": GAME},
                blends={"Bedtime": 120}, timers=(Timer(time(9, 0)),))
    base.update(over)
    return RoomConfig(**base)


def test_the_last_period_runs_on_past_midnight():
    spans = day_spans(SCHEDULE, THURSDAY)
    assert spans[0] == (0, 390, "Overnight")
    assert spans[-1] == (1380, 1440, "Overnight")


def test_a_motion_room_gets_one_every_day_lane_of_visits():
    [lane] = room_lanes(toilet(), SCHEDULE, facts())
    assert lane.key == "base" and lane.label == "Every day" and lane.today
    assert all(b.visit for b in lane.bands)
    # Late Afternoon has no look of its own: it borrows Day's, so the two make one band.
    day = next(b for b in lane.bands if b.period == "Day to Late Afternoon")
    assert day.look == BRIGHT and (day.start, day.end) == (540, 1200)


def test_an_all_day_routine_runs_from_its_timer_to_lights_out_with_its_blend():
    [lane] = room_lanes(office(), SCHEDULE, facts(lights_out=60))
    spans = [(b.start, b.end, b.period, b.blend_period) for b in lane.bands]
    assert spans[0] == (0, 60, "Overnight", None)  # still on from yesterday until Lights out
    assert (540, 1200, "Day", None) not in spans and spans[1][:2] == (540, 1200)  # Day and its borrower
    assert (1200, 1260, "Bedtime", None) in spans
    assert (1260, 1380, "Bedtime", "Overnight") in spans  # the two-hour blend into Game Time
    assert not any(b.visit for b in lane.bands)
    morning = [b for b in lane.bands if b.period == "Morning"]
    assert morning == []  # the room starts at 09:00, so Morning never shows


def test_workday_timers_split_the_week_and_each_day_starts_as_the_day_before_ended():
    lanes = room_lanes(office(timers=(Timer(time(8, 30), days="workdays"),)), SCHEDULE, facts(lights_out=60))
    by_label = {l.label: l for l in lanes}
    assert list(by_label) == ["Tue, Wed, Thu, Fri", "Sat", "Sun", "Mon"]
    assert by_label["Tue, Wed, Thu, Fri"].today
    assert by_label["Tue, Wed, Thu, Fri"].bands[0].start == 0  # on from the night before until Lights out
    assert by_label["Mon"].bands[0].start == 510  # Sunday didn't run
    [(start, end)] = [(b.start, b.end) for b in by_label["Sat"].bands]
    assert (start, end) == (0, 60)  # on from Friday until Lights out, then off all weekend
    assert by_label["Sun"].bands == () and not by_label["Sun"].note


def test_a_lights_out_counts_only_on_the_days_it_runs():
    workdays_only = lambda day, workday: 60 if workday else None  # noqa: E731
    lanes = room_lanes(office(), SCHEDULE, facts(lights_out=workdays_only))
    by_label = {l.label: l for l in lanes}
    assert by_label["Sat, Sun"].bands[0].start == 0 and by_label["Sat, Sun"].bands[-1].end == 1440  # on all weekend
    assert by_label["Mon–Fri"].bands[0].end == 60


def test_no_blend_into_the_same_period_before_midnight():
    [lane] = room_lanes(office(blends={"Overnight": 30}), SCHEDULE, facts(lights_out=60))
    assert not any(b.blend_period == "Overnight" for b in lane.bands)
    assert lane.bands[-1].end == 1440 and lane.bands[-1].blend_to is None


def test_motion_outside_the_routine_is_a_visit_and_the_someone_row_follows_the_routine():
    config = office(triggers=("binary_sensor.motion",), someone_looks={"Bedtime": BRIGHT},
                    timers=(Timer(time(9, 0)), Timer(time(21, 0), action="off")))
    lanes = room_lanes(config, SCHEDULE, facts())
    someone, base = lanes[0], lanes[1]
    [boost] = someone.bands
    assert (boost.start, boost.end) == (1200, 1260)  # only while the routine runs
    early = next(b for b in base.bands if b.start <= 420 < b.end)
    assert early.visit  # before 09:00 motion lights the room
    late = next(b for b in base.bands if b.start <= 1300 < b.end)
    assert late.visit and late.look == BRIGHT  # after 21:00, Bedtime's Someone's there look
    assert not next(b for b in base.bands if b.start <= 600 < b.end).visit


def test_a_dark_only_timer_needs_the_dark_day_track_or_the_sun_down():
    config = office(timers=(Timer(time(18, 0), only_dark=True),), dim_looks={"Day": DIM})
    lanes = room_lanes(config, SCHEDULE, facts(dark_possible=True))
    assert all(l.bands == () for l in lanes)  # 18:00 is outside the dark periods and the sun is up
    lanes = room_lanes(config, SCHEDULE, facts(dark_possible=True, sun_down=lambda m: m >= 1020))
    assert any(b.start <= 1100 < b.end for l in lanes for b in l.bands)


def test_a_dark_day_lane_follows_the_house_periods():
    own = SCHEDULE.with_starts({"Day": time(10, 0)})  # the room's Day starts later than the house's
    lanes = room_lanes(toilet(dim_looks={"Morning": NIGHT}), own,
                       facts(dark_possible=True, dark_periods=("Morning",), house_schedule=SCHEDULE))
    dark = next(l for l in lanes if l.dark)
    assert next(b for b in dark.bands if b.start <= 400 < b.end).track == "dim"
    # 09:30 is still the room's Morning, but the house's Day: the Dark Day track has ended.
    late = next(b for b in dark.bands if b.start <= 570 < b.end)
    assert late.track == "normal" and late.period == "Morning"


def test_a_dark_day_lane_only_where_it_differs():
    assert [l.label for l in room_lanes(toilet(), SCHEDULE, facts(dark_possible=True))] == ["Every day"]
    lanes = room_lanes(toilet(dim_looks={"Day": DIM}), SCHEDULE, facts(dark_possible=True))
    assert [l.label for l in lanes] == ["Every day", "Dark Day"]
    day = next(b for b in lanes[1].bands if b.start <= 540 < b.end)
    assert day.look == DIM and day.track == "dim"
    # Outside the Dark Day periods the lane is the same as a Normal day.
    evening = next(b for b in lanes[1].bands if b.start <= 1200 < b.end)
    assert evening.track == "normal"


def test_dark_day_brightness_makes_a_lane_with_its_factor():
    lanes = room_lanes(toilet(), SCHEDULE, facts(dark_possible=True, auto_dim=0.5))
    day = next(b for b in lanes[1].bands if b.start <= 540 < b.end)
    assert day.factor == 0.5 and day.look == BRIGHT


def test_rule_lanes_show_the_whole_day_as_it_goes_while_the_rule_holds():
    bedtime = Rule(Condition("binary_sensor.baby_bedtime"), action="look", scene="scene.nightlight")
    house = Rule(Condition("person.someone", "home", negate=True), action="nothing")
    cap = Rule(Condition("input_boolean.film"), action="cap", max_pct=20)
    lanes = room_lanes(toilet(rules=(bedtime, cap)), SCHEDULE, facts(), house_rules=[(3, house)],
                       names={"binary_sensor.baby_bedtime": "Baby bedtime"})
    keys = [(l.key, l.index) for l in lanes]
    assert keys == [("base", None), ("rule", 0), ("rule", 1), ("house_rule", 3)]
    rule = lanes[1]
    assert rule.label == "While Baby bedtime is on"
    [band] = rule.bands  # the same look all day: one band
    assert band.look == Look(scene="scene.nightlight") and band.period == "All day"
    assert all(b.cap == 20 for b in lanes[2].bands)
    assert lanes[3].bands == () and "Does nothing" in lanes[3].note
    assert lanes[3].label == "While person.someone isn't home"


def test_inform_lanes_in_priority_order():
    call = Signal("In a call", Condition("binary_sensor.unmuted"), {LIGHT: LightTarget(True, 100, rgb=(102, 0, 255))})
    muted = Signal("Muted", Condition("binary_sensor.muted"), {LIGHT: LightTarget(True, 100, rgb=(0, 255, 0))})
    lanes = room_lanes(office(signals=(muted, call)), SCHEDULE, facts())
    informs = [l for l in lanes if l.key == "inform"]
    assert [(l.label, l.index) for l in informs] == [("Muted", 0), ("In a call", 1)]
    assert "binary_sensor.muted is on" in informs[0].note


def test_a_routine_that_brightens_for_someone_gets_a_row_above_it():
    config = office(triggers=("binary_sensor.motion",), someone_looks={"Bedtime": BRIGHT})
    lanes = room_lanes(config, SCHEDULE, facts())
    assert lanes[0].key == "someone"
    [band] = lanes[0].bands
    assert band.period == "Bedtime" and band.visit and band.look == BRIGHT


def test_a_routine_with_no_timer_says_how_it_starts():
    [lane] = room_lanes(office(timers=(), on_by_hand="routine"), SCHEDULE, facts())
    assert lane.bands == () and "started" in lane.note
