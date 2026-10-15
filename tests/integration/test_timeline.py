"""The page's Timeline command and reordering rules and Informs."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.room_routines.const import DOMAIN
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.serial import schedule_to

LAMP, BATH = "light.office_lamp", "light.bath_ceiling"
MOTION, CALL, MUTED, BEDTIME = "binary_sensor.bath_motion", "binary_sensor.call", "binary_sensor.muted", "binary_sensor.bedtime"
THURSDAY_NOON = "2026-10-15 09:00:00+00:00"  # 12:00 in Tallinn


@pytest.fixture(autouse=True)
async def noon(hass: HomeAssistant, tallinn, freezer):
    freezer.move_to(THURSDAY_NOON)


def options():
    office = {
        "name": "Office", "area_id": None, "lights": [LAMP], "triggers": [], "holds": [],
        "threshold_lux": None, "timeout_s": 300, "cooldown_s": 30, "drift_s": 90, "fade_out_s": 0,
        "looks": {"Day": {"lights": {LAMP: {"on": True, "brightness_pct": 40, "color_temp_kelvin": 5000}}},
                  "Evening": {"lights": {LAMP: {"on": True, "brightness_pct": 5, "color_temp_kelvin": 2200}}}},
        "blends": {"Day": 60},
        "timers": [{"at": "09:00", "action": "on", "days": "every_day"}],
        "signals": [
            {"name": "Muted", "when": {"entity": MUTED, "state": "on"}, "lights": {LAMP: {"on": True, "brightness_pct": 100, "rgb": [0, 255, 0]}}},
            {"name": "In a call", "when": {"entity": CALL, "state": "on"}, "lights": {LAMP: {"on": True, "brightness_pct": 100, "rgb": [102, 0, 255]}}},
        ],
        "start_mode": "live",
    }
    bath = {
        "name": "Bathroom", "area_id": None, "lights": [BATH], "triggers": [MOTION], "holds": [],
        "threshold_lux": None, "timeout_s": 30, "cooldown_s": 30, "drift_s": 90, "fade_out_s": 0,
        "looks": {"Early morning": {"lights": {BATH: {"on": True}}}},
        "rules": [{"when": {"entity": BEDTIME, "state": "on"}, "action": "cap", "max_pct": 5},
                  {"when": {"entity": "person.someone", "state": "not_home"}, "action": "nothing"}],
        "start_mode": "live",
    }
    return {**schedule_to(default_schedule()), "rooms": {"office": office, "bath": bath},
            "lights_outs": [{"id": "n", "name": "Lights out", "when": "time", "at": "01:00", "mode": "live"}]}


async def setup(hass: HomeAssistant) -> MockConfigEntry:
    for entity in (LAMP, BATH, MOTION, CALL, MUTED, BEDTIME):
        hass.states.async_set(entity, "off")
    entry = MockConfigEntry(domain=DOMAIN, title="Room Routines", data={}, options=options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_timeline_gives_each_room_its_lanes(hass, lights, hass_ws_client):
    await setup(hass)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "room_routines/timeline"})
    reply = await ws.receive_json()
    assert reply["success"], reply
    data = reply["result"]
    assert data["now"] == 12 * 60 and data["lights_outs"][0]["minute"] == 60
    rooms = {r["id"]: r for r in data["rooms"]}
    office = rooms["office"]
    assert office["group"] == "allday" and office["lights_out"] == 60
    [base, *informs] = office["lanes"]
    assert base["label"] == "Every day"
    starts = [(b["start"], b["end"], b["blend_period"]) for b in base["bands"]]
    assert (540, 960, None) in starts and (960, 1020, "Evening") in starts  # the hour-long blend before Evening
    day = next(b for b in base["bands"] if b["start"] == 540)
    assert day["look"]["lights"][0]["pct"] == 40 and day["look"]["lights"][0]["kelvin"] == 5000
    assert [(l["key"], l["label"], l["index"]) for l in informs] == [("inform", "Muted", 0), ("inform", "In a call", 1)]
    assert informs[1]["look"]["lights"][0]["rgb"] == [102, 0, 255]
    bath = rooms["bath"]
    assert bath["group"] == "someone"
    assert [l["key"] for l in bath["lanes"]] == ["base", "rule", "rule"]
    assert all(b["look"]["lights"][0]["pct"] == 5 for b in bath["lanes"][1]["bands"])  # the cap rule
    assert bath["lanes"][2]["bands"] == [] and "Does nothing" in bath["lanes"][2]["note"]


async def test_reordering_informs_and_rules_saves_the_new_priority(hass, lights, hass_ws_client):
    entry = await setup(hass)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "room_routines/reorder", "kind": "signals", "room_id": "office", "order": [1, 0]})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert [s["name"] for s in entry.options["rooms"]["office"]["signals"]] == ["In a call", "Muted"]
    await ws.send_json({"id": 2, "type": "room_routines/reorder", "kind": "rules", "room_id": "bath", "order": [0, 0]})
    reply = await ws.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "invalid_order"
