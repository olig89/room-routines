"""Time-based routines end to end: a room without sensors, started by an action,
following the day with blending and its own evening, paused by a hand change,
and timers."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.light import LightEntityFeature
from homeassistant.core import HomeAssistant

from custom_components.room_routines.const import DOMAIN
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.serial import schedule_to

PLAY = "light.office_play"
STATUS = "sensor.office_routine_status"
WORKDAY = "binary_sensor.workday_sensor"
MONDAY_10 = "2026-09-28 07:00:00+00:00"  # 10:00 in Tallinn

FOCUS = {"lights": {PLAY: {"on": True, "brightness_pct": 40, "color_temp_kelvin": 5000}}}
DARK = {"lights": {PLAY: {"on": True, "brightness_pct": 5, "color_temp_kelvin": 2200}}}


@pytest.fixture(autouse=True)
async def morning(hass: HomeAssistant, tallinn, freezer):
    freezer.move_to(MONDAY_10)


def office(**over):
    room = {
        "name": "Office", "area_id": None, "lights": [PLAY], "triggers": [], "holds": [],
        "threshold_lux": None, "timeout_s": 300, "cooldown_s": 30, "drift_s": 90, "fade_out_s": 15,
        "looks": {"Morning": FOCUS, "Evening": DARK},
        "blends": {"Day": 300},
        "period_starts": {"Evening": "18:00"},
    }
    room.update(over)
    return {**schedule_to(default_schedule()), "rooms": {"office": room}}


async def setup(hass: HomeAssistant, **over) -> MockConfigEntry:
    # A light that fades itself (a Hue bulb), so blending is one command per step.
    hass.states.async_set(PLAY, "off", {"supported_features": LightEntityFeature.TRANSITION})
    entry = MockConfigEntry(domain=DOMAIN, title="Room Routines", data={}, options=office(**over))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.office_routine_mode", "option": "live"}, blocking=True
    )
    return entry


async def at(hass: HomeAssistant, freezer, when: str) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def room(hass: HomeAssistant):
    return hass.config_entries.async_entries(DOMAIN)[0].runtime_data.rooms["office"].room


async def test_the_action_starts_the_routine_and_the_room_follows_the_day(hass, lights, freezer):
    await setup(hass)
    await hass.services.async_call(DOMAIN, "switch_on", {"entity_id": STATUS}, blocking=True)
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1] == {
        "entity_id": PLAY, "brightness_pct": 40, "color_temp_kelvin": 5000,
    }
    # 15:30 is halfway through the 13:00-18:00 blend.
    await at(hass, freezer, "2026-09-28 12:30:00+00:00")
    last = lights.of("turn_on")[-1]
    assert last["brightness_pct"] == pytest.approx(22.5)
    assert last["transition"] == 120
    assert 2900 < last["color_temp_kelvin"] < 3200
    # 17:30: the house is in its Evening, the office isn't yet.
    await at(hass, freezer, "2026-09-28 14:30:00+00:00")
    assert room(hass).period == "Day"
    # 18:00: the office's own Evening.
    await at(hass, freezer, "2026-09-28 15:00:01+00:00")
    assert room(hass).period == "Evening"
    assert lights.of("turn_on")[-1] == {
        "entity_id": PLAY, "brightness_pct": 5, "color_temp_kelvin": 2200, "transition": 90,
    }
    assert "Evening" in hass.states.get(STATUS).attributes["reason"]


async def test_a_hand_change_pauses_the_routine_until_the_lights_go_off(hass, lights, freezer):
    await setup(hass)
    await hass.services.async_call(DOMAIN, "switch_on", {"entity_id": STATUS}, blocking=True)
    await hass.async_block_till_done()
    await at(hass, freezer, "2026-09-28 07:00:20+00:00")  # past the matcher's settle window
    hass.states.async_set(PLAY, "on", {"brightness": 255})  # someone turns it up
    await hass.async_block_till_done()
    assert room(hass).paused
    sent = len(lights.calls)
    await at(hass, freezer, "2026-09-28 12:30:00+00:00")
    assert len(lights.calls) == sent  # no blending while paused
    hass.states.async_set(PLAY, "off")
    await hass.async_block_till_done()
    assert not room(hass).paused and room(hass).state.value == "idle"


async def test_lights_switched_on_by_hand_join_the_routine_when_asked(hass, lights, freezer):
    await setup(hass, on_by_hand="routine")
    hass.states.async_set(PLAY, "on", {"brightness": 255})
    await hass.async_block_till_done()
    assert not lights.of("turn_on")
    await at(hass, freezer, "2026-09-28 07:00:04+00:00")
    assert lights.of("turn_on")[-1]["brightness_pct"] == 40


async def test_a_workday_timer_switches_on_only_on_workdays(hass, lights, freezer):
    hass.states.async_set(WORKDAY, "on")
    await setup(hass, timers=[{"at": "10:30", "action": "on", "days": "workdays"},
                              {"at": "11:00", "action": "off"}])
    await at(hass, freezer, "2026-09-28 07:30:01+00:00")  # 10:30
    assert lights.of("turn_on")[-1]["brightness_pct"] == 40
    await at(hass, freezer, "2026-09-28 08:00:01+00:00")  # 11:00
    assert lights.of("turn_off")[-1]["entity_id"] == PLAY
    hass.states.async_set(WORKDAY, "off")  # Tuesday is a public holiday, say
    on_before = len(lights.of("turn_on"))
    await at(hass, freezer, "2026-09-29 07:30:01+00:00")
    assert len(lights.of("turn_on")) == on_before


async def test_a_dark_only_timer_waits_for_the_dark(hass, lights, freezer):
    await setup(hass, timers=[{"at": "10:30", "action": "on", "only_dark": True},
                              {"at": "21:00", "action": "on", "only_dark": True}])
    await at(hass, freezer, "2026-09-28 07:30:01+00:00")  # 10:30: sun up, Dark Days off
    assert not lights.of("turn_on")
    await at(hass, freezer, "2026-09-28 18:00:01+00:00")  # 21:00: dark
    assert lights.of("turn_on")[-1]["brightness_pct"] == 5


PC = "binary_sensor.office_pc"
BEDTIME = "binary_sensor.baby_bedtime"


async def test_a_starter_starts_the_routine_when_it_turns_on(hass, lights, freezer):
    hass.states.async_set(PC, "off")
    await setup(hass, starters=[{"entity": PC, "state": "on"}])
    hass.states.async_set(PC, "on")
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1]["brightness_pct"] == 40
    assert "is on" in hass.states.get(STATUS).attributes["reason"]
    # Staying on, or going off, does nothing more.
    sent = len(lights.calls)
    hass.states.async_set(PC, "on", {"x": 1})
    hass.states.async_set(PC, "off")
    await hass.async_block_till_done()
    assert len(lights.calls) == sent


async def test_only_when_holds_back_a_timer(hass, lights, freezer):
    hass.states.async_set(BEDTIME, "on")
    await setup(hass, timers=[{"at": "10:30", "action": "on"}],
                only_when=[{"entity": BEDTIME, "state": "on", "negate": True}])
    await at(hass, freezer, "2026-09-28 07:30:01+00:00")
    assert not lights.of("turn_on")
    assert "only when" in hass.states.get(STATUS).attributes["reason"]


async def test_a_house_rule_dims_a_lit_room_and_lets_go_after(hass, lights, freezer):
    hass.states.async_set(BEDTIME, "off")
    options = office()
    options["house_rules"] = [{"when": {"entity": BEDTIME, "state": "on"}, "action": "cap", "max_pct": 10}]
    hass.states.async_set(PLAY, "off", {"supported_features": LightEntityFeature.TRANSITION})
    entry = MockConfigEntry(domain=DOMAIN, title="Room Routines", data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.office_routine_mode", "option": "live"}, blocking=True
    )
    await hass.services.async_call(DOMAIN, "switch_on", {"entity_id": STATUS}, blocking=True)
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1]["brightness_pct"] == 40
    hass.states.async_set(BEDTIME, "on")
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1]["brightness_pct"] == 10
    assert "no brighter than 10 %" in hass.states.get(STATUS).attributes["reason"]
    hass.states.async_set(BEDTIME, "off")
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1]["brightness_pct"] == 40
