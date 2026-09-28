"""A fake house: plain light states plus stand-in light services that record calls."""

import pytest

from homeassistant.core import HomeAssistant, ServiceCall


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def tallinn(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Europe/Tallinn")


class LightCalls:
    """Records light service calls and moves the fake light's state, like a real light would."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, dict]] = []
        self.follow = True  # update the light's state when called

    async def handle(self, call: ServiceCall) -> None:
        self.calls.append((call.service, dict(call.data)))
        if not self.follow:
            return
        ids = call.data["entity_id"]
        for entity_id in [ids] if isinstance(ids, str) else ids:
            if call.service == "turn_off":
                self.hass.states.async_set(entity_id, "off", context=call.context)
            else:
                old = self.hass.states.get(entity_id)
                attrs = dict(old.attributes) if old else {}
                if "brightness_pct" in call.data:
                    attrs["brightness"] = round(call.data["brightness_pct"] / 100 * 255)
                self.hass.states.async_set(entity_id, "on", attrs, context=call.context)

    def of(self, service: str) -> list[dict]:
        return [data for svc, data in self.calls if svc == service]


@pytest.fixture
def lights(hass: HomeAssistant) -> LightCalls:
    recorder = LightCalls(hass)
    hass.services.async_register("light", "turn_on", recorder.handle)
    hass.services.async_register("light", "turn_off", recorder.handle)
    return recorder
