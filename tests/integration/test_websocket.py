"""The panel's websocket API."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant

from .test_integration import CEILING, MODE, MONDAY_EVENING, STATUS, motion, setup


@pytest.fixture(autouse=True)
async def evening(hass: HomeAssistant, tallinn, freezer):
    freezer.move_to(MONDAY_EVENING)


@pytest.fixture
async def ws(hass: HomeAssistant, hass_ws_client, lights):
    await setup(hass, mode="log_only")
    return await hass_ws_client(hass)


async def test_subscribe_sends_the_house_now_and_after_changes(hass, ws, freezer):
    await ws.send_json({"id": 1, "type": "room_routines/subscribe"})
    assert (await ws.receive_json())["success"]
    first = (await ws.receive_json())["event"]
    assert first["set_up"] is True
    assert first["house"]["period"] == "Evening"
    room = first["rooms"][0]
    assert room["name"] == "Downstairs Toilet"
    assert room["mode"] == "log_only"
    assert room["status_entity"] == STATUS and room["mode_entity"] == MODE
    assert room["settings"]["lights"] == [CEILING]

    await motion(hass, True)
    freezer.tick(timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    changed = (await ws.receive_json())["event"]
    assert changed["rooms"][0]["state"] == "owned"


async def test_save_a_look_from_the_panel(hass, ws):
    look = {"lights": {CEILING: {"on": True, "brightness_pct": 30}}}
    await ws.send_json({"id": 1, "type": "room_routines/save_look", "room_id": "wc", "period": "Evening", "how": "custom", "look": look})
    reply = await ws.receive_json()
    assert reply["success"], reply
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries("room_routines")[0]
    assert entry.options["rooms"]["wc"]["looks"]["Evening"]["lights"][CEILING]["brightness_pct"] == 30

    await ws.send_json({"id": 2, "type": "room_routines/save_look", "room_id": "wc", "period": "Brunch", "how": "nothing"})
    reply = await ws.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "unknown_period"


async def test_add_edit_and_remove_a_room(hass, ws):
    room = {"name": "Pantry", "lights": ["light.pantry"], "triggers": ["binary_sensor.pantry_motion"],
            "timeout_s": 60, "fade_out_s": 10, "cooldown_s": 30, "drift_s": 90}
    await ws.send_json({"id": 1, "type": "room_routines/save_room", "room": room})
    reply = await ws.receive_json()
    assert reply["success"], reply
    room_id = reply["result"]["room_id"]
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries("room_routines")[0]
    assert entry.options["rooms"][room_id]["name"] == "Pantry"
    assert hass.states.get("sensor.pantry_routine_status") is not None

    await ws.send_json({"id": 2, "type": "room_routines/save_room", "room_id": room_id, "room": {**room, "timeout_s": 120}})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert entry.options["rooms"][room_id]["timeout_s"] == 120

    await ws.send_json({"id": 3, "type": "room_routines/save_room", "room": {**room, "triggers": []}})
    reply = await ws.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "invalid_room"

    await ws.send_json({"id": 4, "type": "room_routines/remove_room", "room_id": room_id})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert room_id not in entry.options["rooms"]


async def test_periods_from_the_panel(hass, ws):
    periods = [{"name": "Night", "start": "22:00"}, {"name": "Day", "start": "07:00"}]
    await ws.send_json({"id": 1, "type": "room_routines/save_periods", "periods": periods, "alt_days": [5, 6], "renames": {"Overnight": "Night"}})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries("room_routines")[0]
    assert [p["name"] for p in entry.options["periods"]] == ["Night", "Day"]


async def test_deeper_settings_are_for_admins_only(hass, hass_ws_client, hass_read_only_access_token, lights):
    await setup(hass)
    ws = await hass_ws_client(hass, hass_read_only_access_token)
    await ws.send_json({"id": 1, "type": "room_routines/remove_room", "room_id": "wc"})
    reply = await ws.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "unauthorized"
    # Everyday: saving a look is allowed.
    await ws.send_json({"id": 2, "type": "room_routines/save_look", "room_id": "wc", "period": "Evening", "how": "nothing"})
    assert (await ws.receive_json())["success"]
