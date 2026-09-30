"""End to end: Normal days and Dark Days, scene looks, and learning from hand changes."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.room_routines.const import DOMAIN
from custom_components.room_routines.core.habits import Change
from custom_components.room_routines.core.looks import LightTarget
from custom_components.room_routines.core.room import ADJUSTED
from custom_components.room_routines.house import House, scene_lights

from .test_integration import CEILING, LUX, MODE, MONDAY_EVENING, MOTION, STATUS, motion, options, wait

WINDOW = "sensor.window_lux"
TRACK = "select.room_routines_track"
SCENE = "scene.wc_evening"
REAL_FETCH = House._fetch_weather  # kept before the fixture below replaces it
MONDAY_NOON = "2026-09-28 09:00:00+00:00"  # 12:00 in Tallinn, sun at about 28 degrees


@pytest.fixture(autouse=True)
async def evening(hass: HomeAssistant, tallinn, freezer):
    freezer.move_to(MONDAY_EVENING)


@pytest.fixture(autouse=True)
def no_weather_service():
    """Tests hand the house weather answers themselves; nothing goes to the network."""
    with patch("custom_components.room_routines.house.House._fetch_weather", AsyncMock()) as fetch:
        yield fetch


def house(hass: HomeAssistant):
    return hass.config_entries.async_entries(DOMAIN)[0].runtime_data


async def start(
    hass: HomeAssistant,
    window: str | None = "300",
    mode: str = "live",
    tracks=True,
    brightness=100,
    periods=("Evening", "Overnight"),
    weather=True,
    **room_over,
):
    hass.states.async_set(CEILING, "off")
    hass.states.async_set(MOTION, "off")
    hass.states.async_set(LUX, "8")
    if window is not None:
        hass.states.async_set(WINDOW, window)
    opts = options(**room_over)
    if tracks:
        opts["tracks"] = {
            "on": True, "weather": weather, "sensor": WINDOW, "fallback": None, "first": "weather",
            "periods": list(periods), "dark_below_pct": 40, "normal_above_pct": 55, "brightness_pct": brightness,
        }
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


async def weather(hass: HomeAssistant, radiation: float) -> None:
    """A weather answer for the 15 minutes up to now."""
    house(hass).take_weather(radiation, dt_util.utcnow())
    await hass.async_block_till_done()


# ---- Dark Days ------------------------------------------------------------------


async def test_after_sunset_in_a_dark_day_period_the_room_uses_its_dark_day_look(hass, lights):
    await start(hass, dim_looks={"Evening": {"lights": {CEILING: {"on": True, "brightness_pct": 25}}}})
    track = hass.states.get(TRACK)
    assert track.state == "dim"
    assert track.attributes["reason"] == "the sun is down"
    await motion(hass, True)
    assert lights.of("turn_on") == [{"entity_id": CEILING, "brightness_pct": 25}]
    assert hass.states.get(STATUS).attributes["track"] == "dim"


async def test_an_evening_outside_the_dark_day_periods_is_a_normal_day(hass, lights):
    await start(hass, periods=("Morning", "Day"))
    assert hass.states.get(TRACK).state == "normal"
    assert hass.states.get(TRACK).attributes["reason"] == "not a Dark Day period"


async def test_off_means_normal_every_day(hass, lights):
    await start(hass, window=None, tracks=False)
    assert hass.states.get(TRACK).state == "normal"
    assert hass.states.get(TRACK).attributes["enabled"] is False


async def test_the_weather_decides_by_day_and_a_lit_room_follows_after_the_hold(hass, lights, freezer):
    freezer.move_to(MONDAY_NOON)
    look = {"lights": {CEILING: {"on": True, "brightness_pct": 25}}}
    await start(hass, periods=("Day",), dim_looks={"Day": look}, looks={"Day": {"lights": {CEILING: {"on": True, "brightness_pct": 80}}}})
    await weather(hass, 90)  # about 20 % of a clear sky
    track = hass.states.get(TRACK)
    assert track.state == "dim" and track.attributes["source"] == "weather"
    assert 15 < track.attributes["clear_day_pct"] < 25
    await motion(hass, True)
    assert lights.of("turn_on")[-1] == {"entity_id": CEILING, "brightness_pct": 25}
    await weather(hass, 400)  # about 90 %
    assert hass.states.get(TRACK).state == "dim"  # held: not yet 20 min
    freezer.tick(timedelta(minutes=21))
    await weather(hass, 400)
    assert hass.states.get(TRACK).state == "normal"
    assert "Normal day" in hass.states.get(STATUS).attributes["reason"]


async def test_the_page_says_why_there_is_no_figure(hass, lights, freezer):
    await start(hass)  # Monday evening in Tallinn: the sun is down
    a = hass.states.get(TRACK).attributes
    assert a["weather_state"] == "sun_down" and a["light_sensor_state"] == "sun_down"

    freezer.move_to("2026-09-29 09:00:00+00:00")  # Tuesday 12:00 in Tallinn (forwards, so timers fire)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    a = hass.states.get(TRACK).attributes
    assert a["weather_state"] == "waiting"  # no answer yet
    assert a["light_sensor_state"] == "learning"  # 300 lx, but no clear-day reference yet

    session = MagicMock()
    session.get = AsyncMock(side_effect=aiohttp.ClientError("no route"))
    with patch("custom_components.room_routines.house.async_get_clientsession", return_value=session):
        await REAL_FETCH(house(hass))
    await hass.async_block_till_done()
    assert hass.states.get(TRACK).attributes["weather_state"] == "unreachable"

    await weather(hass, 200)
    a = hass.states.get(TRACK).attributes
    assert a["weather_state"] == "ok" and a["weather_pct"] is not None

    freezer.tick(timedelta(hours=2))  # the answer goes stale
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(TRACK).attributes["weather_state"] == "stale"

    hass.states.async_set(WINDOW, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(TRACK).attributes["light_sensor_state"] == "no_reading"


async def test_no_reading_keeps_the_day(hass, lights, freezer):
    freezer.move_to(MONDAY_NOON)
    await start(hass, window="unavailable", periods=("Day",))
    await weather(hass, 90)
    assert hass.states.get(TRACK).state == "dim"
    freezer.tick(timedelta(hours=2))  # the weather answer goes stale
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(TRACK).state == "dim"
    assert hass.states.get(TRACK).attributes["reason"] == "no light reading"


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


async def test_the_light_sensor_learns_a_clear_day_from_its_statistics(hass, lights, freezer):
    freezer.move_to(MONDAY_NOON)
    entry = await start(hass, window="2000", periods=("Day",), weather=False)
    h = house(hass)
    assert hass.states.get(TRACK).attributes["reason"] == "no light reading"  # nothing learned yet
    # Fake a recorder: a bright day last week read 10000 lx at every sun height.
    rows = []
    t = dt_util.utcnow() - timedelta(days=3)
    for i in range(3 * 24 * 12):
        rows.append({"start": (t + timedelta(minutes=5 * i)).timestamp(), "mean": 10000.0})
    hass.config.components.add("recorder")
    with (
        patch("homeassistant.components.recorder.get_instance") as instance,
        patch("homeassistant.components.recorder.statistics.statistics_during_period", return_value={WINDOW: rows}),
    ):
        async def run(func, *args):
            return func(*args)

        instance.return_value.async_add_executor_job = run
        await h._learn()
    await hass.async_block_till_done()
    track = hass.states.get(TRACK)
    assert track.attributes["source"] == "sensor"
    assert track.attributes["light_sensor_pct"] == 20.0  # 2000 of a clear 10000 lx
    assert track.state == "dim"
    assert entry.options["tracks"]["sensor"] == WINDOW


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
    assert draft["name"] == "Downstairs Toilet · Evening · Dark Day"
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
    await ws.send_json({"id": 1, "type": "room_routines/save_tracks", "on": True, "sensor": WINDOW,
                        "dark_below_pct": 30, "normal_above_pct": 50, "periods": ["Day"], "brightness_pct": 150})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert entry.options["tracks"]["dark_below_pct"] == 30
    assert entry.options["tracks"]["brightness_pct"] == 150
    assert entry.options["tracks"]["periods"] == ["Day"]
    ws2 = await hass_ws_client(hass, hass_read_only_access_token)
    await ws2.send_json({"id": 1, "type": "room_routines/save_tracks", "sensor": None})
    reply = await ws2.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "unauthorized"
    await ws.send_json({"id": 2, "type": "room_routines/save_tracks", "on": True, "dark_below_pct": 50, "normal_above_pct": 50})
    reply = await ws.receive_json()
    assert not reply["success"] and reply["error"]["code"] == "invalid_tracks"


# ---- Dark Day brightness ----------------------------------------------------------------------


async def test_auto_dim_reads_a_home_assistant_scene_and_turns_it_down(hass, lights):
    await scenes(hass, brightness=200)  # 78.4 %
    await start(hass, brightness=50, looks={"Evening": {"scene": SCENE}})
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
    await start(hass, brightness=50, looks={"Evening": {"scene": "scene.hue_relax"}})
    await motion(hass, True)
    await hass.async_block_till_done()
    assert calls == [{"entity_id": "scene.hue_relax"}]
    assert "can't be read" in caplog.text
