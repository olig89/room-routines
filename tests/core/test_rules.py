"""Starters, "only when" conditions and "while X" rules."""

import pytest

from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import ApplyLook, Room, RoomConfig, State
from custom_components.room_routines.core.rules import (
    CAP,
    LOOK,
    NOTHING,
    Condition,
    Rule,
    active_rule,
    condition_from,
    condition_to,
    describe_rule,
    first_unmet,
    rule_from,
    rule_to,
)
from custom_components.room_routines.core.serial import room_from

from .conftest import at as _at


def at(hhmm: str):
    h, m = hhmm.split(":")
    return _at(28, int(h), int(m))

A = "light.landing"
M = "binary_sensor.landing_motion"
BEDTIME = "binary_sensor.baby_bedtime"
BRIGHT = Look({A: LightTarget(True, 80, 3000)})
NIGHT = Look({A: LightTarget(True, 3, 2200)})


def landing(**over) -> RoomConfig:
    base = dict(name="Landing", lights=(A,), triggers=(M,), threshold_lux=None,
                looks={"Morning": BRIGHT, "Overnight": NIGHT})
    base.update(over)
    return RoomConfig(**base)


def make(config: RoomConfig, period="Day", now=None) -> Room:
    return Room(config, default_schedule(), period, False, now or at("12:00"))


def applied(decision) -> Look:
    return next(a for a in decision.actions if isinstance(a, ApplyLook)).look


# ---- conditions ----------------------------------------------------------------------


def test_a_condition_reads_a_state_and_a_negated_one_reads_its_absence():
    on = Condition(BEDTIME)
    off = Condition(BEDTIME, negate=True)
    assert on.holds({BEDTIME: "on"}) and not off.holds({BEDTIME: "on"})
    assert off.holds({BEDTIME: "off"})
    home = Condition("zone.home", "0")
    assert home.holds({"zone.home": "0"}) and not home.holds({"zone.home": "2"})


def test_an_unknown_state_is_never_true_but_never_locks_a_room_out():
    assert not Condition(BEDTIME).holds({BEDTIME: "unavailable"})
    assert not Condition(BEDTIME).holds({})
    assert Condition(BEDTIME, negate=True).holds({BEDTIME: "unavailable"})


def test_the_first_active_rule_wins():
    quiet = Rule(Condition(BEDTIME), CAP, max_pct=10)
    away = Rule(Condition("zone.home", "0"), NOTHING)
    assert active_rule([quiet, away], {BEDTIME: "on", "zone.home": "0"}) is quiet
    assert active_rule([quiet, away], {BEDTIME: "off", "zone.home": "0"}) is away
    assert active_rule([quiet, away], {BEDTIME: "off", "zone.home": "1"}) is None
    assert first_unmet([Condition(BEDTIME, negate=True)], {BEDTIME: "on"}) == Condition(BEDTIME, negate=True)


def test_rules_check_what_they_need():
    with pytest.raises(ValueError):
        Rule(Condition(BEDTIME), LOOK)
    with pytest.raises(ValueError):
        Rule(Condition(BEDTIME), CAP, max_pct=0)
    with pytest.raises(ValueError):
        Rule(Condition(BEDTIME), "sing")


def test_rules_and_conditions_round_trip():
    rules = [
        Rule(Condition(BEDTIME), LOOK, period="Overnight", rooms=("landing",), name="Bedtime"),
        Rule(Condition("zone.home", "0"), NOTHING),
        Rule(Condition(BEDTIME, negate=True), CAP, max_pct=40),
        Rule(Condition(BEDTIME), LOOK, scene="scene.landing_nightlight"),
    ]
    for rule in rules:
        assert rule_from(rule_to(rule)) == rule
    cond = Condition("person.oli", "home", negate=True)
    assert condition_from(condition_to(cond)) == cond
    assert describe_rule(rules[0], "Baby bedtime") == "while Baby bedtime is on: Overnight look"


def test_a_stored_room_carries_its_starters_conditions_and_rules():
    config = room_from({
        "name": "Office", "lights": [A], "triggers": [],
        "starters": [{"entity": "binary_sensor.pc"}],
        "only_when": [{"entity": "person.oli", "state": "home"}],
        "rules": [{"when": {"entity": BEDTIME}, "action": "cap", "max_pct": 20}],
    })
    assert config.starters == (Condition("binary_sensor.pc"),)
    assert config.only_when == (Condition("person.oli", "home"),)
    assert config.rules == (Rule(Condition(BEDTIME), CAP, max_pct=20),)


# ---- the room ------------------------------------------------------------------------


def test_do_nothing_stops_motion_and_starts():
    room = make(landing())
    room.set_context(Rule(Condition("zone.home", "0"), NOTHING), "while Home is 0: do nothing", "", at("12:00"))
    d = room.sensor(M, True, at("12:00"))
    assert not d.actions and "do nothing" in d.reason
    assert room.state is State.IDLE
    d = room.start(at("12:01"), "timer at 12:01")
    assert not d.actions and "do nothing" in d.reason


def test_when_do_nothing_ends_someone_still_there_gets_light():
    room = make(landing())
    rule = Rule(Condition("zone.home", "0"), NOTHING)
    room.set_context(rule, "while Home is 0: do nothing", "", at("12:00"))
    room.sensor(M, True, at("12:00"))
    d = room.set_context(None, "", "", at("12:05"))
    assert room.state is State.OWNED
    assert applied(d) == BRIGHT


def test_do_nothing_leaves_a_lit_room_to_go_dark_as_usual():
    room = make(landing())
    room.sensor(M, True, at("12:00"))
    room.set_context(Rule(Condition("zone.home", "0"), NOTHING), "away: do nothing", "", at("12:01"))
    assert room.state is State.OWNED  # not switched off, not stranded
    room.sensor(M, False, at("12:02"))
    assert room.deadline is not None


def test_only_when_blocks_every_start_and_says_why():
    room = make(landing(only_when=(Condition(BEDTIME, negate=True),)))
    room.set_context(None, "", "Baby bedtime isn't on", at("20:00"))
    d = room.sensor(M, True, at("20:00"))
    assert not d.actions and d.reason == f"motion at {M}, but only when Baby bedtime isn't on"


def test_another_look_while_a_rule_holds_and_back_after():
    room = make(landing(), period="Day")
    rule = Rule(Condition(BEDTIME), LOOK, period="Overnight")
    room.set_context(rule, "while Baby bedtime is on: Overnight look", "", at("19:00"))
    d = room.sensor(M, True, at("19:30"))
    assert applied(d) == NIGHT
    assert "Overnight look" in d.reason
    d = room.set_context(None, "", "", at("19:40"))
    assert applied(d) == BRIGHT  # the lit room drifts back to the Day look


def test_a_scene_look_from_a_rule():
    room = make(landing())
    room.set_context(Rule(Condition(BEDTIME), LOOK, scene="scene.landing_nightlight"), "bedtime", "", at("19:00"))
    d = room.sensor(M, True, at("19:30"))
    assert applied(d) == Look(scene="scene.landing_nightlight")


def test_a_brightness_limit_caps_set_levels_and_last_brightness():
    last = Look({A: LightTarget(True)})
    room = make(landing(looks={"Morning": last}))
    room.set_context(Rule(Condition(BEDTIME), CAP, max_pct=20), "bedtime: no brighter than 20 %", "", at("19:00"))
    assert applied(room.sensor(M, True, at("19:30"))).lights[A].brightness_pct == 20
    room2 = make(landing(looks={"Morning": Look({A: LightTarget(True, 10, 2700)})}))
    room2.set_context(Rule(Condition(BEDTIME), CAP, max_pct=20), "bedtime", "", at("19:00"))
    assert applied(room2.sensor(M, True, at("19:30"))).lights[A].brightness_pct == 10


def test_a_brightness_limit_reads_a_scene():
    room = Room(
        landing(looks={"Morning": Look(scene="scene.bright")}), default_schedule(), "Day", False, at("12:00"),
        scene_reader=lambda s: {A: LightTarget(True, 90, 3000)} if s == "scene.bright" else None,
    )
    room.set_context(Rule(Condition(BEDTIME), CAP, max_pct=15), "bedtime", "", at("19:00"))
    assert applied(room.sensor(M, True, at("19:30"))).lights[A] == LightTarget(True, 15, 3000)


def test_a_rule_look_stops_blending():
    config = landing(triggers=(), blends={"Day": 600}, period_starts={})
    room = Room(config, default_schedule(), "Day", False, at("15:00"))
    assert room.blending(at("15:00")) is not None
    room.set_context(Rule(Condition(BEDTIME), LOOK, period="Overnight"), "bedtime", "", at("15:00"))
    assert room.blending(at("15:00")) is None


def test_a_changed_rule_while_paused_leaves_the_lights():
    room = make(landing())
    room.sensor(M, True, at("12:00"))
    room.hand = {light: None for light in room.config.switchable()}
    d = room.set_context(Rule(Condition(BEDTIME), CAP, max_pct=5), "bedtime", "", at("19:00"))
    assert not d.actions

