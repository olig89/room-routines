"""0.3.0 end to end: Normal and Dim days, scene looks, and learning from hand changes."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.room_routines.const import DOMAIN
from custom_components.room_routines.core.habits import Change
from custom_components.room_routines.core.looks import LightTarget
from custom_components.room_routines.core.room import ADJUSTED
from custom_components.room_routines.house import scene_lights

from .test_integration import CEILING, LUX, MODE, MONDAY_EVENING, MOTION, STATUS, motion, options, wait

WINDOW = "sensor.window_lux"
TRACK = "select.room_routines_track"
SCENE = "scene.wc_evening"


@pytest.fixture(autouse=True)
async def evening(hass: HomeAssistant, tallinn, freezer):
    freezer.move_to(MONDAY_EVENING)


async def start(hass: HomeAssistant, window: str | None = "300", mode: str = "live", tracks=True, auto_dim=100, **room_over):
    hass.states.async_set(CEILING, "off")
    hass.states.async_set(MOTION, "off")
    hass.states.async_set(LUX, "8")
    if window is not None:
        hass.states.async_set(WINDOW, window)
    opts = options(**room_over)
    if tracks:
        opts["tracks"] = {"sensor": WINDOW, "fallback": None, "dim_below": 800, "normal_above": 1500, "auto_dim_pct": auto_dim}
    entry = MockConfigEntry(domain=DOMAIN, title="Room Routines", data={}, options=opts)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call("select", "select_option", {"entity_id": MODE, "option": mode}, blocking=True)
    return entry


async def scenes(hass: HomeAssistant, brightness: int = 77) -> None:
    assert await async_setup_component(
        hass,
        "scene",
        {"scene": [{"id": "wc_evening_id", "name": "WC Evening", "entities": {CEILING: {"state": "on", "brightness": brightness}}}]},
    )
    await hass.async_block_till_done()


# ---- tracks ------------------------------------------------------------------


async def test_dark_window_starts_on_dim_and_the_room_uses_its_dim_look(hass, lights):
    await start(hass, dim_looks={"Evening": {"lights": {CEILING: {"on": True, "brightness_pct": 25}}}})
    track = hass.states.get(TRACK)
    assert track.state == "dim"
    assert track.attributes["light_level"] == 300
    await motion(hass, True)
    assert lights.of("turn_on") == [{"entity_id": CEILING, "brightness_pct": 25}]
    assert hass.states.get(STATUS).attributes["track"] == "dim"


async def test_no_sensor_means_normal_every_day(hass, lights):
    await start(hass, window=None, tracks=False)
    assert hass.states.get(TRACK).state == "normal"
    assert hass.states.get(TRACK).attributes["enabled"] is False


async def test_brighter_day_moves_a_lit_room_to_normal_after_the_hold(hass, lights, freezer):
    await start(hass, dim_looks={"Evening": {"lights": {CEILING: {"on": True, "brightness_pct": 25}}}})
    await motion(hass, True)
    hass.states.async_set(WINDOW, "5000")
    await hass.async_block_till_done()
    assert hass.states.get(TRACK).state == "dim"  # held: not yet 20 min
    freezer.tick(timedelta(minutes=21))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(TRACK).state == "normal"
    assert "Normal day" in hass.states.get(STATUS).attributes["reason"]


async def test_choosing_a_track_by_hand_holds_until_the_next_period(hass, lights, freezer):
    await start(hass)
    await hass.services.async_call("select", "select_option", {"entity_id": TRACK, "option": "normal"}, blocking=True)
    assert hass.states.get(TRACK).state == "normal"
    assert hass.states.get(TRACK).attributes["chosen_by_hand"] is True
    freezer.move_to("2026-09-28 20:00:01+00:00")  # 23:00 Tallinn: Overnight starts
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(TRACK).state == "dim"
    assert hass.states.get(TRACK).attributes["chosen_by_hand"] is False


async def test_unavailable_sensor_keeps_the_track(hass, lights, freezer):
    await start(hass)
    hass.states.async_set(WINDOW, "unavailable")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(TRACK).state == "dim"


# ---- scene looks -----------------------------------------------------------------


async def test_a_scene_look_turns_the_scene_on_and_its_result_counts_as_the_rooms_own(hass, lights):
    await scenes(hass)
    await start(hass, tracks=False, looks={"Evening": {"scene": SCENE}})
    await motion(hass, True)
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1]["brightness"] == 77
    status = hass.states.get(STATUS)
    assert status.state == "owned"
    assert "adjusted by hand" not in status.attributes["reason"]


async def test_scene_settings_can_be_read(hass, lights):
    await scenes(hass, brightness=128)
    await start(hass, tracks=False)
    assert scene_lights(hass, SCENE, [CEILING]) == {CEILING: LightTarget(True, 50.2)}
    assert scene_lights(hass, "scene.nope", [CEILING]) is None


async def test_set_look_action_can_save_a_dim_scene_look(hass, lights):
    await scenes(hass)
    entry = await start(hass)
    await hass.services.async_call(
        DOMAIN, "set_look",
        {"entity_id": STATUS, "period": "Evening", "track": "dim", "look": "scene", "scene": SCENE},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options["rooms"]["wc"]["dim_looks"]["Evening"] == {"scene": SCENE}


# ---- hand changes -------------------------------------------------------------------


async def test_a_hand_adjustment_is_remembered_once_the_lights_settle(hass, lights, freezer):
    entry = await start(hass, tracks=False)
    await motion(hass, True)
    await wait(hass, freezer, 15)  # a change in the first seconds counts as the room's own (matching.py)
    hass.states.async_set(CEILING, "on", {"brightness": 64}, context=Context())  # dimmed by hand
    await hass.async_block_till_done()
    await wait(hass, freezer, 11)
    house = entry.runtime_data
    [change] = house.changes
    assert change.kind == ADJUSTED
    assert change.period == "Evening"
    assert change.lights == {CEILING: LightTarget(True, 25.1)}


async def test_log_only_rooms_learn_nothing(hass, lights, freezer):
    entry = await start(hass, tracks=False, mode="log_only")
    await motion(hass, True)
    await wait(hass, freezer, 15)
    hass.states.async_set(CEILING, "on", {"brightness": 64}, context=Context())
    await hass.async_block_till_done()
    await wait(hass, freezer, 11)
    assert entry.runtime_data.changes == []


async def test_suggestions_reach_the_page_and_can_be_dismissed(hass, lights, hass_ws_client):
    entry = await start(hass, tracks=False, looks={"Evening": {"lights": {CEILING: {"on": True, "brightness_pct": 30}}}})
    house = entry.runtime_data
    base = dt_util.now()
    for days in (1, 2, 3, 4):
        house.changes.append(
            Change(base - timedelta(days=days), "wc", ADJUSTED, "Evening", "normal", 60,
                   {CEILING: LightTarget(True, 70)})
        )
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "room_routines/subscribe"})
    assert (await ws.receive_json())["success"]
    room = (await ws.receive_json())["event"]["rooms"][0]
    [s] = room["suggestions"]
    assert s["kind"] == "save_look"
    assert s["scene_entities"] == {CEILING: {"state": "on", "brightness": 178}}
    await ws.send_json({"id": 2, "type": "room_routines/dismiss", "key": s["key"]})
    assert (await ws.receive_json())["success"]
    assert house.suggestions(house.rooms["wc"]) == []


# ---- the page's new commands ------------------------------------------------------------


async def test_scene_draft_then_save_the_look_by_scene_id(hass, lights, hass_ws_client):
    await scenes(hass)
    entry = await start(hass, tracks=False)
    hass.states.async_set(CEILING, "on", {"brightness": 128, "color_mode": "brightness"})
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "room_routines/scene_draft", "room_id": "wc", "period": "Evening", "track": "dim"})
    draft = (await ws.receive_json())["result"]
    assert draft["name"] == "Downstairs Toilet · Evening · Dim"
    assert draft["config_id"] == "room_routines_wc_evening_dim"
    assert draft["existing"] is False
    assert draft["entities"] == {CEILING: {"state": "on", "brightness": 128, "color_mode": "brightness"}}
    # The page saves the scene through Home Assistant's scene API; here the scene already exists.
    await ws.send_json({"id": 2, "type": "room_routines/save_look", "room_id": "wc", "period": "Evening",
                        "track": "dim", "how": "scene", "scene_id": "wc_evening_id"})
    reply = await ws.receive_json()
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert entry.options["rooms"]["wc"]["dim_looks"]["Evening"] == {"scene": SCENE}
    # Saving again updates the same scene.
    await ws.send_json({"id": 3, "type": "room_routines/scene_draft", "room_id": "wc", "period": "Evening", "track": "dim"})
    again = (await ws.receive_json())["result"]
    assert again["config_id"] == "wc_evening_id" and again["existing"] is True


async def test_saving_the_dark_day_sensor_is_for_admins(hass, lights, hass_ws_client, hass_read_only_access_token):
    entry = await start(hass, tracks=False)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "room_routines/save_tracks", "sensor": WINDOW, "dim_below": 600, "normal_above": 1200})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert entry.options["tracks"]["dim_below"] == 600
    ws2 = await hass_ws_client(hass, hass_read_only_access_token)
    await ws2.send_json({"id": 1, "type": "room_routines/save_tracks", "sensor": None})
    reply = await ws2.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "unauthorized"
    await ws.send_json({"id": 2, "type": "room_routines/save_tracks", "sensor": WINDOW, "dim_below": 900, "normal_above": 900})
    reply = await ws.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "invalid_tracks"


# ---- auto-dim ----------------------------------------------------------------------


async def test_auto_dim_reads_a_home_assistant_scene_and_turns_it_down(hass, lights):
    await scenes(hass, brightness=200)  # 78.4 %
    await start(hass, auto_dim=50, looks={"Evening": {"scene": SCENE}})
    await motion(hass, True)
    await hass.async_block_till_done()
    assert lights.of("turn_on")[-1] == {"entity_id": CEILING, "brightness_pct": 39.2}
    assert "Evening look at 50 %" in hass.states.get(STATUS).attributes["reason"]


async def test_a_scene_from_another_app_is_turned_on_as_it_is(hass, lights, caplog):
    calls = []

    async def turn_on(call):
        calls.append(dict(call.data))

    hass.services.async_register("scene", "turn_on", turn_on)
    hass.states.async_set("scene.hue_relax", "unknown", {"friendly_name": "Relax", "group_name": "WC"})
    await start(hass, auto_dim=50, looks={"Evening": {"scene": "scene.hue_relax"}})
    await motion(hass, True)
    await hass.async_block_till_done()
    assert calls == [{"entity_id": "scene.hue_relax"}]
    assert "can't be read" in caplog.text
