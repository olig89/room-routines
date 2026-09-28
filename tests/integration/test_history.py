"""The panel's history and dry-run check, against a real recorder."""

from datetime import timedelta

import pytest

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant

from .test_integration import CEILING, MONDAY_EVENING, motion, setup


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(evening, recorder_mock, enable_custom_integrations):
    """The clock is set, then the recorder starts, then Home Assistant."""
    yield


@pytest.fixture
def evening(freezer):
    freezer.move_to(MONDAY_EVENING)


async def test_history_and_the_dry_run_check(recorder_mock, hass: HomeAssistant, tallinn, hass_ws_client, lights, freezer):
    await setup(hass, mode="log_only")
    lights.follow = False  # the sensor's own link switches the light, not the room
    await hass.async_block_till_done()
    await motion(hass, True)
    hass.states.async_set(CEILING, "on")  # the sensor's direct link, a moment later
    await hass.async_block_till_done()
    await motion(hass, False)
    for _ in range(35):
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    hass.states.async_set(CEILING, "off")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=1))  # history ends at "now", exclusive
    from homeassistant.components.recorder import get_instance

    await get_instance(hass).async_block_till_done()

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "room_routines/history", "room_id": "wc", "hours": 2})
    reply = await ws.receive_json()
    assert reply["success"], reply
    result = reply["result"]
    assert [s["state"] for s in result["status"]][-2:] == ["owned", "idle"]
    assert result["parity"]["total"] == 2
    assert result["parity"]["matched"] == 2
