"""The integration end to end, on a fake downstairs toilet."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant import config_entries
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, entity_registry as er, floor_registry as fr

from custom_components.room_routines.const import DOMAIN
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.serial import first_look, schedule_to

CEILING = "light.downstairs_toilet_ceiling"
MOTION = "binary_sensor.downstairs_toilet_motion"
LUX = "sensor.downstairs_toilet_lux"
STATUS = "sensor.downstairs_toilet_routine_status"
MODE = "select.downstairs_toilet_routine_mode"
STEALTH = "switch.room_routines_stealth_mode"
PERIOD = "select.room_routines_period"

MONDAY_EVENING = "2026-09-28 17:30:00+00:00"  # 20:30 in Tallinn


def options(**room_over):
    room = {
        "name": "Downstairs Toilet",
        "area_id": None,
        "lights": [CEILING],
        "triggers": [MOTION],
        "holds": [],
        "lux_sensor": LUX,
        "threshold_lux": 50,
        "timeout_s": 30,
        "cooldown_s": 30,
        "drift_s": 90,
        "fade_out_s": 15,
        "looks": first_look([CEILING], default_schedule()),
    }
    room.update(room_over)
    return {**schedule_to(default_schedule()), "rooms": {"wc": room}}


async def setup(hass: HomeAssistant, mode: str | None = "live", **room_over) -> MockConfigEntry:
    hass.states.async_set(CEILING, "off")
    hass.states.async_set(MOTION, "off")
    hass.states.async_set(LUX, "8")
    entry = MockConfigEntry(domain=DOMAIN, title="Room Routines", data={}, options=options(**room_over))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if mode:
        await hass.services.async_call("select", "select_option", {"entity_id": MODE, "option": mode}, blocking=True)
    return entry


async def wait(hass: HomeAssistant, freezer, seconds: float) -> None:
    """Let time pass one second at a time, so every timer fires in order."""
    for _ in range(int(seconds)):
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def motion(hass: HomeAssistant, on: bool) -> None:
    hass.states.async_set(MOTION, "on" if on else "off")
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
async def evening(hass: HomeAssistant, tallinn, freezer: FrozenDateTimeFactory):
    freezer.move_to(MONDAY_EVENING)


async def test_entities_are_created(hass: HomeAssistant, lights):
    await setup(hass, mode=None)
    assert hass.states.get(STATUS).state == "idle"
    assert hass.states.get(MODE).state == "log_only"  # a new room starts in log-only
    assert hass.states.get(STEALTH).state == "off"
    period = hass.states.get(PERIOD)
    assert period.state == "Evening"
    assert period.attributes["options"] == list(default_schedule().order())
    assert hass.states.get("sensor.downstairs_toilet_routine_light_level").state == "8.0"


async def test_live_motion_switches_on_then_off_after_the_timeout(hass, lights, freezer):
    await setup(hass)
    await motion(hass, True)
    assert lights.of("turn_on") == [{"entity_id": CEILING}]
    assert hass.states.get(STATUS).state == "owned"
    await motion(hass, False)
    assert "lights_off_at" in hass.states.get(STATUS).attributes
    await wait(hass, freezer, 31)
    assert hass.states.get(STATUS).state == "idle"
    await wait(hass, freezer, 16)
    # No native transition on this light, so a stepped 15 s fade, then off.
    assert [c.get("brightness_pct") for c in lights.of("turn_on")[1:]] == [80.2, 60.4, 40.6, 20.8]
    assert lights.of("turn_off") == [{"entity_id": CEILING}]


async def test_fade_out_uses_the_lights_own_transition_when_it_has_one(hass, lights, freezer):
    await setup(hass)
    hass.states.async_set(CEILING, "off", {"supported_features": 32})  # LightEntityFeature.TRANSITION
    await motion(hass, True)
    await motion(hass, False)
    await wait(hass, freezer, 31)
    assert lights.of("turn_off") == [{"entity_id": CEILING, "transition": 15.0}]


async def test_after_a_stepped_fade_the_light_returns_at_its_old_level(hass, lights, freezer):
    await setup(hass)
    hass.states.async_set(CEILING, "off", {"brightness": 191})  # last used at 75 %
    await motion(hass, True)
    await motion(hass, False)
    await wait(hass, freezer, 50)  # timeout + the whole fade
    assert lights.of("turn_off") == [{"entity_id": CEILING}]
    await motion(hass, True)
    assert lights.of("turn_on")[-1] == {"entity_id": CEILING, "brightness_pct": 74.9}


async def test_a_level_set_by_hand_replaces_the_remembered_one(hass, lights, freezer):
    await setup(hass)
    await motion(hass, True)
    await motion(hass, False)
    await wait(hass, freezer, 50)
    await wait(hass, freezer, 15)  # well after the fade (see matching.py's known limit)
    hass.states.async_set(CEILING, "on", {"brightness": 128}, context=Context())  # by hand
    await hass.async_block_till_done()
    hass.states.async_set(CEILING, "off", {"brightness": 128}, context=Context())
    await hass.async_block_till_done()
    await wait(hass, freezer, 31)  # past the cooldown
    await motion(hass, False)
    await motion(hass, True)
    assert lights.of("turn_on")[-1] == {"entity_id": CEILING}


async def test_a_light_that_fades_by_itself_gets_plain_commands(hass, lights, freezer):
    looks = {
        "Evening": {"lights": {CEILING: {"on": True, "brightness_pct": 80}}},
        "Overnight": {"lights": {CEILING: {"on": True, "brightness_pct": 10}}},
    }
    await setup(hass, looks=looks, self_fading=[CEILING])
    await motion(hass, True)
    freezer.move_to("2026-09-28 20:00:00+00:00")  # 23:00 Tallinn: drift to Overnight
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await wait(hass, freezer, 100)
    # One command for the drift, no steps...
    assert lights.of("turn_on") == [
        {"entity_id": CEILING, "brightness_pct": 80},
        {"entity_id": CEILING, "brightness_pct": 10},
    ]
    await motion(hass, False)
    await wait(hass, freezer, 50)
    # ...and a plain off for the fade-out.
    assert lights.of("turn_off") == [{"entity_id": CEILING}]


async def test_walking_back_in_during_the_fade_out(hass, lights, freezer):
    await setup(hass)
    await motion(hass, True)
    await motion(hass, False)
    await wait(hass, freezer, 31)
    await wait(hass, freezer, 4)  # one step into the fade
    await motion(hass, True)
    await wait(hass, freezer, 20)
    assert lights.of("turn_off") == []
    assert lights.of("turn_on")[-1] == {"entity_id": CEILING, "brightness_pct": 100.0}  # back to where it was
    assert hass.states.get(STATUS).state == "owned"


async def test_log_only_sends_nothing(hass, lights):
    await setup(hass, mode="log_only")
    await motion(hass, True)
    assert lights.calls == []
    status = hass.states.get(STATUS)
    assert status.state == "owned"  # what it would have done
    assert "Evening look" in status.attributes["reason"]


async def test_too_bright_stays_off(hass, lights):
    await setup(hass)
    hass.states.async_set(LUX, "120")
    await hass.async_block_till_done()
    await motion(hass, True)
    assert lights.calls == []
    assert "too bright" in hass.states.get(STATUS).attributes["reason"]


async def test_light_switched_on_by_hand_is_left_alone(hass, lights, freezer):
    await setup(hass)
    hass.states.async_set(CEILING, "on", {"brightness": 128}, context=Context())
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == "manual"
    await motion(hass, True)
    await motion(hass, False)
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert lights.calls == []


async def test_own_late_status_report_is_not_mistaken_for_a_person(hass, lights):
    await setup(hass)
    lights.follow = False  # the light answers later, with its own context (like KNX)
    await motion(hass, True)
    hass.states.async_set(CEILING, "on", {"brightness": 200}, context=Context())
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == "owned"


async def test_stealth_ignores_motion_and_rooms_still_go_dark(hass, lights, freezer):
    await setup(hass)
    await motion(hass, True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": STEALTH}, blocking=True)
    assert hass.states.get(STEALTH).state == "on"
    await wait(hass, freezer, 50)
    assert lights.of("turn_off") == [{"entity_id": CEILING}]
    await motion(hass, False)
    await motion(hass, True)
    switch_ons = [c for c in lights.of("turn_on") if "brightness_pct" not in c]  # not fade steps
    assert len(switch_ons) == 1


async def test_period_change_drifts_a_lit_room(hass, lights, freezer):
    looks = {
        "Evening": {"lights": {CEILING: {"on": True, "brightness_pct": 80}}},
        "Overnight": {"lights": {CEILING: {"on": True, "brightness_pct": 10}}},
    }
    await setup(hass, looks=looks)
    await motion(hass, True)
    assert lights.of("turn_on")[-1]["brightness_pct"] == 80
    freezer.move_to("2026-09-28 20:00:00+00:00")  # 23:00 Tallinn
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(PERIOD).state == "Overnight"
    # The fake light has no transition support, so the drift arrives in steps.
    for _ in range(10):
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert lights.of("turn_on")[-1]["brightness_pct"] == 10
    assert len(lights.of("turn_on")) > 3


async def test_choosing_a_period_by_hand(hass, lights):
    await setup(hass)
    await hass.services.async_call("select", "select_option", {"entity_id": PERIOD, "option": "Overnight"}, blocking=True)
    state = hass.states.get(PERIOD)
    assert state.state == "Overnight"
    assert state.attributes["chosen_by_hand"] is True


async def test_saving_a_look_needs_no_reload(hass, lights):
    entry = await setup(hass)
    await motion(hass, True)
    hass.states.async_set(CEILING, "on", {"brightness": 64, "color_mode": "color_temp", "color_temp_kelvin": 2200})
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "set_look", {"entity_id": STATUS, "period": "Overnight"}, blocking=True)
    await hass.async_block_till_done()
    saved = entry.options["rooms"]["wc"]["looks"]["Overnight"]
    assert saved == {"lights": {CEILING: {"on": True, "brightness_pct": 25.1, "color_temp_kelvin": 2200}}}
    assert hass.states.get(STATUS).state == "owned"  # still owns its lights: no reload


async def test_saving_nothing_and_borrowing(hass, lights):
    entry = await setup(hass)
    await hass.services.async_call(DOMAIN, "set_look", {"entity_id": STATUS, "period": "Overnight", "look": "nothing"}, blocking=True)
    await hass.async_block_till_done()
    assert entry.options["rooms"]["wc"]["looks"]["Overnight"] == {"nothing": True}
    await hass.services.async_call(DOMAIN, "set_look", {"entity_id": STATUS, "period": "Overnight", "look": "borrow"}, blocking=True)
    await hass.async_block_till_done()
    assert "Overnight" not in entry.options["rooms"]["wc"]["looks"]


async def test_setup_and_add_a_room_with_area_suggestions(hass):
    area = ar.async_get(hass).async_create("Pantry")
    reg = er.async_get(hass)
    for domain, uid, cls in [("light", "pantry_ceiling", None), ("binary_sensor", "pantry_motion", "motion"),
                             ("binary_sensor", "pantry_door", "door")]:
        reg.async_get_or_create(domain, "test", uid, suggested_object_id=uid, original_device_class=cls)
        reg.async_update_entity(f"{domain}.{uid}", area_id=area.id)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.async_block_till_done()

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.MENU
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "add_room"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"name": "Pantry", "area_id": area.id})
    assert flow["step_id"] == "room_details"
    suggested = {str(k): k.description.get("suggested_value") for k in flow["data_schema"].schema if k.description}
    assert suggested["lights"] == ["light.pantry_ceiling"]
    assert suggested["triggers"] == ["binary_sensor.pantry_motion"]  # the door sensor isn't suggested
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"lights": ["light.pantry_ceiling"], "triggers": ["binary_sensor.pantry_door"],  # but any sensor is allowed
         "threshold_lux": 50, "timeout_s": 30, "fade_out_s": 15, "cooldown_s": 30, "drift_s": 90},
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    rooms = entry.options["rooms"]
    # The area became a room on its own at set-up: no sensors, starting off.
    made = rooms[f"area_{area.id}"]
    assert made["lights"] == ["light.pantry_ceiling"] and made["triggers"] == [] and made["start_mode"] == "off"
    room = next(r for r in rooms.values() if r["triggers"])
    assert room["name"] == "Pantry" and room["area_id"] == area.id
    assert room["triggers"] == ["binary_sensor.pantry_door"]
    assert room["looks"] == {"Early morning": {"lights": {"light.pantry_ceiling": {"on": True}}}}


async def test_a_sensor_cannot_be_both_trigger_and_hold(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={**schedule_to(default_schedule()), "rooms": {}})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "add_room"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"name": "X"})
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"lights": ["light.x"], "triggers": ["binary_sensor.m"], "holds": ["binary_sensor.m"],
         "timeout_s": 30, "fade_out_s": 15, "cooldown_s": 30, "drift_s": 90},
    )
    assert flow["errors"] == {"base": "invalid_room"}


async def periods_flow(hass, entry, step):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "periods"})
    return await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": step})


async def remove_period(hass, entry, name):
    flow = await periods_flow(hass, entry, "pick_period")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"period": name})
    start = next(p["start"] for p in entry.options["periods"] if p["name"] == name)
    await hass.config_entries.options.async_configure(flow["flow_id"], {"name": name, "start": start + ":00", "remove": True})
    await hass.async_block_till_done()


async def test_period_start_times(hass, lights):
    entry = await setup(hass)
    flow = await periods_flow(hass, entry, "period_times")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"Overnight": "23:30:00", "Early morning": "05:30:00", "Morning": "07:00:00", "Day": "09:00:00",
         "Evening": "17:00:00", "alt_days": ["Saturday", "Sunday"], "Morning (other days)": "08:30:00"},
    )
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    periods = {p["name"]: p for p in entry.options["periods"]}
    assert periods["Overnight"]["start"] == "23:30"
    assert periods["Morning"]["alt_start"] == "08:30"
    assert entry.options["alt_days"] == [5, 6]


async def test_add_rename_and_remove_periods(hass, lights):
    looks = {"Evening": {"lights": {CEILING: {"on": True, "brightness_pct": 70}}}}
    entry = await setup(hass, looks=looks)
    flow = await periods_flow(hass, entry, "add_period")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"name": "Late evening", "start": "21:00:00"})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.options["periods"]) == 6
    assert "Late evening" in hass.states.get(PERIOD).attributes["options"]

    flow = await periods_flow(hass, entry, "pick_period")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"period": "Evening"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"name": "Dusk", "start": "17:00:00"})
    await hass.async_block_till_done()
    assert "Dusk" in entry.options["rooms"]["wc"]["looks"]  # the room's look followed the rename

    await remove_period(hass, entry, "Dusk")
    assert [p["name"] for p in entry.options["periods"]] == ["Overnight", "Early morning", "Morning", "Day", "Late evening"]
    assert "Dusk" not in entry.options["rooms"]["wc"]["looks"]


async def test_a_single_period_is_allowed_and_cannot_be_removed(hass, lights):
    entry = await setup(hass)
    for name in ["Overnight", "Early morning", "Morning", "Day"]:
        await remove_period(hass, entry, name)
    assert [p["name"] for p in entry.options["periods"]] == ["Evening"]
    assert hass.states.get(PERIOD).state == "Evening"
    flow = await periods_flow(hass, entry, "pick_period")
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"period": "Evening"})
    assert "remove" not in [str(k) for k in flow["data_schema"].schema]


async def test_removing_a_room_removes_its_entities(hass, lights):
    entry = await setup(hass)
    assert hass.states.get(STATUS) is not None
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "remove_room"})
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"room": "wc"})
    await hass.async_block_till_done()
    assert entry.options["rooms"] == {}
    assert hass.states.get(STATUS) is None
    assert hass.states.get(STEALTH) is not None


async def test_entity_ids_stay_short_in_an_area_on_a_floor(hass, lights):
    floor = fr.async_get(hass).async_create("2F")
    area = ar.async_get(hass).async_create("Downstairs Toilet", floor_id=floor.floor_id)
    await setup(hass, area_id=area.id)
    # Without our own suggestion HA would name it sensor.2f_downstairs_toilet_downstairs_toilet_routine_status.
    assert hass.states.get(STATUS) is not None
    assert hass.states.get(MODE) is not None
    assert hass.states.get("sensor.downstairs_toilet_routine_light_level") is not None
    assert not [s.entity_id for s in hass.states.async_all() if "2f_" in s.entity_id]


async def test_stray_option_keys_are_dropped_on_setup(hass, lights):
    hass.states.async_set(CEILING, "off")
    opts = options()
    opts["wc"] = dict(opts["rooms"]["wc"])  # a copy of the room at the top level, as seen on a live install
    entry = MockConfigEntry(domain=DOMAIN, title="Room Routines", data={}, options=opts)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert set(entry.options) == {"periods", "alt_days", "rooms"}
    assert hass.states.get(STATUS) is not None


async def test_period_select_knows_the_next_change(hass, lights, freezer):
    await setup(hass)
    # Monday 20:30 Tallinn: Evening, and Overnight starts at 23:00.
    assert hass.states.get(PERIOD).attributes["next_change"] == "2026-09-28T23:00:00+03:00"
    freezer.move_to("2026-09-28 20:00:01+00:00")  # 23:00:01 Tallinn
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(PERIOD)
    assert state.state == "Overnight"
    assert state.attributes["next_change"] == "2026-09-29T05:30:00+03:00"


async def test_any_dark_light_sensor_is_dark_enough(hass, lights):
    """Two light sensors (each end of a staircase): dark at either end lights the room."""
    other = "sensor.landing_lux"
    hass.states.async_set(other, "400")
    await setup(hass, lux_sensor=None, lux_sensors=[other, LUX])
    hass.states.async_set(LUX, "8")  # this end is dark
    await hass.async_block_till_done()
    await motion(hass, True)
    assert lights.of("turn_on")[-1]["entity_id"] == CEILING


async def test_both_light_sensors_bright_stays_off(hass, lights):
    other = "sensor.landing_lux"
    hass.states.async_set(other, "400")
    await setup(hass, lux_sensor=None, lux_sensors=[other, LUX])
    hass.states.async_set(LUX, "300")
    await hass.async_block_till_done()
    await motion(hass, True)
    assert not lights.of("turn_on")
