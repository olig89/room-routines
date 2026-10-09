"""Lights out end to end: a whole-house run at 01:00 with rooms in every state,
log only by default, the skip switch, entity and period triggers, and the page's
commands."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.room_routines.const import DOMAIN
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.serial import schedule_to

BATH, BATH_MOTION, BATH_PRESENCE = "light.bath_ceiling", "binary_sensor.bath_motion", "binary_sensor.bath_presence"
OFFICE = "light.office_lamp"
HALL, HALL_MOTION = "light.hall_ceiling", "binary_sensor.hall_motion"
GARDEN = "light.garden"  # in no room
GROUP = "light.downstairs"  # a group of other lights
RAW = "light.raw"  # hidden behind a template light
BED = "input_boolean.bedtime"
SKIP = "switch.room_routines_skip_next_lights_out"
SUNDAY_LATE = "2026-09-27 21:50:00+00:00"  # 00:50 Monday in Tallinn
ONE_AM = "2026-09-27 22:00:00+00:00"

ON = {"lights": {}}


@pytest.fixture(autouse=True)
async def late(hass: HomeAssistant, tallinn, freezer):
    freezer.move_to(SUNDAY_LATE)


def room(name, lights, triggers=(), holds=(), mode="live"):
    return {
        "name": name, "area_id": None, "lights": list(lights), "triggers": list(triggers), "holds": list(holds),
        "threshold_lux": None, "timeout_s": 30, "cooldown_s": 30, "drift_s": 90, "fade_out_s": 0,
        "looks": {"Overnight": {"lights": {light: {"on": True} for light in lights}}},
        "start_mode": mode,
    }


def options(lights_out=None):
    lo = {"id": "night", "name": "Lights out", "when": "time", "at": "01:00", "days": "every_day",
          "style": "once_empty", "mode": "live"}
    lo.update(lights_out or {})
    return {
        **schedule_to(default_schedule()),
        "rooms": {
            "bath": room("Bathroom", [BATH], [BATH_MOTION], [BATH_PRESENCE]),
            "office": room("Office", [OFFICE]),
            "hall": room("Hall", [HALL], [HALL_MOTION], mode="off"),
        },
        "lights_outs": [lo],
    }


async def setup(hass: HomeAssistant, lights_out=None) -> MockConfigEntry:
    # Registered (hidden) before its state exists, so it keeps the id light.raw.
    er.async_get(hass).async_get_or_create("light", "test", "raw", suggested_object_id="raw", hidden_by=er.RegistryEntryHider.USER)
    for light in (BATH, OFFICE, HALL, GARDEN, RAW):
        hass.states.async_set(light, "off")
    hass.states.async_set(GROUP, "off", {"entity_id": [BATH, OFFICE]})
    for sensor in (BATH_MOTION, BATH_PRESENCE, HALL_MOTION):
        hass.states.async_set(sensor, "off")
    hass.states.async_set(BED, "off")
    entry = MockConfigEntry(domain=DOMAIN, title="Room Routines", data={}, options=options(lights_out))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def house(hass):
    return hass.config_entries.async_entries(DOMAIN)[0].runtime_data


async def at(hass: HomeAssistant, freezer, when: str) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def evening_scene(hass):
    """Someone in the bathroom (sitting still), the office lamp on, the hall lit with
    someone in it, the garden and the hidden light on."""
    hass.states.async_set(BATH_MOTION, "on")
    await hass.async_block_till_done()
    hass.states.async_set(BATH_PRESENCE, "on")
    hass.states.async_set(BATH_MOTION, "off")
    for light in (OFFICE, HALL, GARDEN, RAW, GROUP):
        hass.states.async_set(light, "on", {"entity_id": [BATH, OFFICE]} if light == GROUP else {})
    hass.states.async_set(HALL_MOTION, "on")
    await hass.async_block_till_done()


def turned_off(lights) -> set[str]:
    out: set[str] = set()
    for data in lights.of("turn_off"):
        ids = data["entity_id"]
        out.update([ids] if isinstance(ids, str) else ids)
    return out


async def test_whole_house_once_empty(hass, lights, freezer):
    await setup(hass)
    await evening_scene(hass)
    assert hass.states.get(BATH).state == "on"
    await at(hass, freezer, ONE_AM)
    # Nobody in the office: off. Lights in no room: off, but never a group or a hidden light.
    assert turned_off(lights) == {OFFICE, GARDEN}
    [run] = house(hass).lights_out_runs
    assert run["off"] == ["office"] and set(run["waiting"]) == {"bath", "hall"} and run["loose"] == [GARDEN]
    assert house(hass).rooms["bath"].lights_out_waiting()["name"] == "Lights out"
    # The bathroom empties at 01:05: off after its own timeout, not before.
    await at(hass, freezer, "2026-09-27 22:05:00+00:00")
    assert BATH not in turned_off(lights)
    hass.states.async_set(BATH_PRESENCE, "off")
    await hass.async_block_till_done()
    await at(hass, freezer, "2026-09-27 22:05:20+00:00")
    assert BATH not in turned_off(lights)
    await at(hass, freezer, "2026-09-27 22:05:31+00:00")
    assert BATH in turned_off(lights)
    assert house(hass).rooms["bath"].room.state.value == "idle"
    # The hall's routine is off, so Lights out watches its sensor itself.
    hass.states.async_set(HALL_MOTION, "off")
    await hass.async_block_till_done()
    await at(hass, freezer, "2026-09-27 22:06:02+00:00")
    assert HALL in turned_off(lights)
    assert house(hass).rooms["hall"].lights_out_waiting() is None


async def test_motion_after_lights_out_works_as_usual(hass, lights, freezer):
    await setup(hass)
    await at(hass, freezer, ONE_AM)
    hass.states.async_set(BATH_MOTION, "on")
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1]["entity_id"] == BATH


async def test_all_off_now(hass, lights, freezer):
    await setup(hass, {"style": "now"})
    await evening_scene(hass)
    await at(hass, freezer, ONE_AM)
    assert turned_off(lights) == {BATH, OFFICE, HALL, GARDEN}


async def test_log_only_changes_nothing(hass, lights, freezer):
    await setup(hass, {"mode": "log_only"})
    await evening_scene(hass)
    sent = len(lights.calls)
    await at(hass, freezer, ONE_AM)
    assert len(lights.calls) == sent
    [run] = house(hass).lights_out_runs
    assert run["mode"] == "log_only" and run["off"] == ["office"] and run["loose"] == [GARDEN]
    assert "would switch off Office" in house(hass).plan_text(run, live=False)


async def test_chosen_rooms_leave_the_rest_alone(hass, lights, freezer):
    await setup(hass, {"rooms": ["office"]})
    await evening_scene(hass)
    await at(hass, freezer, ONE_AM)
    assert turned_off(lights) == {OFFICE}


async def test_the_skip_switch_skips_one_run(hass, lights, freezer):
    await setup(hass)
    await hass.services.async_call("switch", "turn_on", {"entity_id": SKIP}, blocking=True)
    await evening_scene(hass)
    await at(hass, freezer, ONE_AM)
    assert not turned_off(lights)
    assert hass.states.get(SKIP).state == "off"
    assert "skipped" in house(hass).lights_out_runs[-1]
    await at(hass, freezer, "2026-09-28 22:00:00+00:00")  # the next night
    assert OFFICE in turned_off(lights)


async def test_workdays_only(hass, lights, freezer):
    await setup(hass, {"days": "days", "weekdays": [5]})  # Saturdays
    await evening_scene(hass)
    await at(hass, freezer, ONE_AM)  # Monday
    assert not turned_off(lights)
    assert house(hass).lights_out_runs[-1]["skipped"] == "not on Mondays"


async def test_an_entity_turning_on_runs_it(hass, lights, freezer):
    await setup(hass, {"when": "entity", "entity": BED, "state": "on"})
    await evening_scene(hass)
    hass.states.async_set(BED, "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set(BED, "on")  # back after a restart: not the moment it became true
    await hass.async_block_till_done()
    assert not turned_off(lights)
    hass.states.async_set(BED, "off")
    await hass.async_block_till_done()
    hass.states.async_set(BED, "on")
    await hass.async_block_till_done()
    assert OFFICE in turned_off(lights)


async def test_a_period_start_runs_it_and_the_mark_ends_at_the_next(hass, lights, freezer):
    freezer.move_to("2026-09-27 19:50:00+00:00")  # 22:50
    await setup(hass, {"when": "period", "period": "Overnight"})
    await evening_scene(hass)
    await at(hass, freezer, "2026-09-27 20:00:01+00:00")  # 23:00: Overnight starts
    assert OFFICE in turned_off(lights)
    bath = house(hass).rooms["bath"]
    assert bath.room.lights_out_until is not None
    # Someone is still there at 05:30: the mark goes, the lights stay on.
    await at(hass, freezer, "2026-09-28 02:30:01+00:00")
    assert bath.room.lights_out_until is None and BATH not in turned_off(lights)
    assert bath.room.state.value == "owned"


async def test_the_page_saves_and_previews(hass, lights, freezer, hass_ws_client):
    await setup(hass, {"mode": "log_only"})
    await evening_scene(hass)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "room_routines/lights_out_preview",
                        "lights_out": {"when": "time", "at": "01:00", "style": "now"}})
    reply = await ws.receive_json()
    assert reply["success"], reply
    assert set(reply["result"]["off"]) == {"bath", "office", "hall"}
    assert "would switch off" in reply["result"]["text"]
    assert not lights.of("turn_off")  # a preview changes nothing
    await ws.send_json({"id": 2, "type": "room_routines/save_lights_outs", "lights_outs": [
        {"name": "Bedtime", "when": "time", "at": "00:30", "style": "now", "mode": "off"}]})
    reply = await ws.receive_json()
    assert reply["success"], reply
    await hass.async_block_till_done()
    [saved] = hass.config_entries.async_entries(DOMAIN)[0].options["lights_outs"]
    assert saved["name"] == "Bedtime" and saved["id"] and saved["mode"] == "off"
    await ws.send_json({"id": 3, "type": "room_routines/save_lights_outs", "lights_outs": [
        {"when": "period", "period": "Brunch"}]})
    reply = await ws.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "invalid_lights_out"
    await ws.send_json({"id": 4, "type": "room_routines/subscribe"})
    assert (await ws.receive_json())["success"]
    event = (await ws.receive_json())["event"]
    assert event["house"]["lights_outs"][0]["text"] == "at 00:30"
    assert event["house"]["skip_entity"] == SKIP


# ---- from the review ----


async def test_a_rooms_group_members_and_lights_in_its_area_wait_with_it(hass, lights, freezer):
    from homeassistant.helpers import area_registry as ar

    area = ar.async_get(hass).async_create("Bath area")
    lamp = er.async_get(hass).async_get_or_create("light", "test", "bath_lamp", suggested_object_id="bath_lamp")
    er.async_get(hass).async_update_entity(lamp.entity_id, area_id=area.id)
    opts = options()
    opts["rooms"]["bath"]["lights"] = ["light.bath_group"]
    opts["rooms"]["bath"]["looks"] = {"Overnight": {"lights": {"light.bath_group": {"on": True}}}}
    opts["rooms"]["bath"]["area_id"] = area.id
    for light in ("light.member_1", "light.member_2", lamp.entity_id, GARDEN):
        hass.states.async_set(light, "on")
    hass.states.async_set("light.bath_group", "on", {"entity_id": ["light.member_1", "light.member_2"]})
    for sensor in (BATH_MOTION, BATH_PRESENCE, HALL_MOTION):
        hass.states.async_set(sensor, "off")
    hass.states.async_set(BATH_PRESENCE, "on")
    for light in (OFFICE, HALL, RAW):
        hass.states.async_set(light, "off")
    entry = MockConfigEntry(domain=DOMAIN, title="Room Routines", data={}, options=opts)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await at(hass, freezer, ONE_AM)
    [run] = house(hass).lights_out_runs
    assert run["waiting"] == ["bath"]
    assert run["loose"] == [GARDEN]  # not the group's members, not the lamp where someone is
    assert turned_off(lights) == {GARDEN}


async def test_a_log_only_run_leaves_the_skip_for_the_live_one(hass, lights, freezer):
    entry = await setup(hass)
    opts = dict(entry.options)
    opts["lights_outs"] = [
        {"id": "trial", "name": "Trial", "when": "time", "at": "00:55", "mode": "log_only"},
        {"id": "real", "name": "Real", "when": "time", "at": "01:00", "mode": "live"},
    ]
    hass.config_entries.async_update_entry(entry, options=opts)
    await hass.async_block_till_done()
    await hass.services.async_call("switch", "turn_on", {"entity_id": SKIP}, blocking=True)
    await evening_scene(hass)
    await at(hass, freezer, "2026-09-27 21:55:00+00:00")
    assert hass.states.get(SKIP).state == "on"
    await at(hass, freezer, ONE_AM)
    assert hass.states.get(SKIP).state == "off" and not turned_off(lights)
    assert [r["name"] for r in house(hass).lights_out_runs] == ["Trial", "Real"]


async def test_a_mode_change_carries_the_wait(hass, lights, freezer):
    await setup(hass)
    await evening_scene(hass)
    await at(hass, freezer, ONE_AM)
    hall = house(hass).rooms["hall"]
    assert hall.lights_out_waiting() is not None  # off room: the plain path waits
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.hall_routine_mode", "option": "live"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hall.room.lights_out_until is not None  # the live room took it over
    hass.states.async_set(HALL_MOTION, "off")
    await hass.async_block_till_done()
    await at(hass, freezer, "2026-09-27 22:00:31+00:00")
    assert HALL in turned_off(lights) and hall.room.state.value == "idle"
    hass.states.async_set(HALL_MOTION, "on")  # motion works again afterwards
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1]["entity_id"] == HALL


async def test_a_reload_keeps_the_wait(hass, lights, freezer):
    entry = await setup(hass)
    await evening_scene(hass)
    await at(hass, freezer, ONE_AM)
    assert set(house(hass).lights_out_waits) == {"bath", "hall"}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert house(hass).rooms["bath"].lights_out_waiting() is not None
    assert house(hass).rooms["hall"].lights_out_waiting() is not None
    await at(hass, freezer, "2026-09-27 22:05:00+00:00")
    hass.states.async_set(BATH_PRESENCE, "off")
    hass.states.async_set(HALL_MOTION, "off")
    await hass.async_block_till_done()
    await at(hass, freezer, "2026-09-27 22:05:31+00:00")
    assert {BATH, HALL} <= turned_off(lights)
    assert house(hass).lights_out_waits == {}
