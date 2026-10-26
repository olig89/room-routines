"""The Timeline's "today so far", against a real recorder."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant

from .test_timeline import BATH, CALL, MOTION, THURSDAY_NOON, setup


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(noon, recorder_mock, enable_custom_integrations):
    """The clock is set, then the recorder starts, then Home Assistant."""
    yield


@pytest.fixture
def noon(freezer):
    freezer.move_to(THURSDAY_NOON)


async def tick(hass, freezer, seconds):
    for _ in range(seconds):
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_today_so_far(recorder_mock, hass: HomeAssistant, helsinki, hass_ws_client, lights, freezer):
    await setup(hass)
    await tick(hass, freezer, 60)
    hass.states.async_set(MOTION, "on")  # the room switches the bathroom on: owned
    hass.states.async_set(CALL, "on")
    await hass.async_block_till_done()
    assert hass.states.get(BATH).state == "on"
    await tick(hass, freezer, 120)
    hass.states.async_set(MOTION, "off")
    await tick(hass, freezer, 40)  # off after its 30 s timeout
    assert hass.states.get(BATH).state == "off"
    hass.states.async_set(CALL, "off")
    await tick(hass, freezer, 60)
    from homeassistant.components.recorder import get_instance

    await get_instance(hass).async_block_till_done()
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "room_routines/timeline"})
    reply = await ws.receive_json()
    assert reply["success"], reply
    rooms = {r["id"]: r for r in reply["result"]["rooms"]}
    [[start, end, owned]] = rooms["bath"]["history"]["on"]
    assert owned and start == 12 * 60 + 1 and end >= start + 2
    [[a, b]] = rooms["office"]["history"]["held"]["inform:1"]  # In a call
    assert a == 12 * 60 + 1 and b >= a + 2
    assert rooms["office"]["history"]["held"]["inform:0"] == []
