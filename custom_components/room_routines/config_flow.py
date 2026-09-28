"""Setup and options screens.

Setup creates the house with the five default periods and no rooms. The
options screen then offers: add a room, change a room, remove a room, and the
periods (start times, add, rename or remove; any number from one up).

Adding a room asks for its name and area first, then suggests the area's
lights, motion/presence sensors and light-level sensor. They are suggestions:
every field takes any entity, and any sensor can have either role.

Looks are saved from the lights themselves with the ``room_routines.set_look``
action (a sidebar page for them comes later).
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)

from .const import CONF_ALT_DAYS, CONF_PERIODS, CONF_ROOMS, DOMAIN, NAME, clean_options
from .core.periods import default_schedule
from .core.serial import (
    DEFAULT_COOLDOWN_S,
    DEFAULT_DRIFT_S,
    DEFAULT_FADE_OUT_S,
    DEFAULT_THRESHOLD_LUX,
    DEFAULT_TIMEOUT_S,
    first_look,
    room_from,
    schedule_from,
    schedule_to,
)
from .suggest import room_suggestions

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ROOM = "room"
PERIOD = "period"
OTHER_DAYS = " (other days)"


# ---- form pieces ---------------------------------------------------------------


def _seconds(maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=0, max=maximum, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
    )


def _entities(domain: str, multiple: bool = True) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domain, multiple=multiple))


def _suggest(defaults: dict[str, Any], key: str) -> dict[str, Any]:
    value = defaults.get(key)
    return {} if value in (None, [], "") else {"description": {"suggested_value": value}}


def identity_fields(defaults: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required("name", **_suggest(defaults, "name")): TextSelector(),
        vol.Optional("area_id", **_suggest(defaults, "area_id")): AreaSelector(),
    }


def detail_fields(defaults: dict[str, Any]) -> dict[Any, Any]:
    def number(key: str, fallback: int) -> dict[str, Any]:
        return {"default": defaults.get(key, fallback)}

    return {
        vol.Required("lights", **_suggest(defaults, "lights")): _entities("light"),
        vol.Required("triggers", **_suggest(defaults, "triggers")): _entities("binary_sensor"),
        vol.Optional("holds", **_suggest(defaults, "holds")): _entities("binary_sensor"),
        vol.Optional("lux_sensor", **_suggest(defaults, "lux_sensor")): _entities("sensor", False),
        vol.Optional("threshold_lux", **_suggest(defaults, "threshold_lux")): NumberSelector(
            NumberSelectorConfig(min=0, max=100000, step=1, unit_of_measurement="lx", mode=NumberSelectorMode.BOX)
        ),
        vol.Required("timeout_s", **number("timeout_s", DEFAULT_TIMEOUT_S)): _seconds(7200),
        vol.Required("fade_out_s", **number("fade_out_s", DEFAULT_FADE_OUT_S)): _seconds(300),
        vol.Optional("self_fading", **_suggest(defaults, "self_fading")): _entities("light"),
        vol.Required("cooldown_s", **number("cooldown_s", DEFAULT_COOLDOWN_S)): _seconds(600),
        vol.Required("drift_s", **number("drift_s", DEFAULT_DRIFT_S)): _seconds(600),
    }


def clean_room(user_input: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Stored room settings from the form. Raises ValueError if they don't make a room."""
    room = copy.deepcopy(previous or {})
    room.update(
        name=str(user_input.get("name", room.get("name", ""))).strip(),
        area_id=user_input.get("area_id", room.get("area_id")) or None,
        lights=list(user_input.get("lights") or []),
        triggers=list(user_input.get("triggers") or []),
        holds=list(user_input.get("holds") or []),
        lux_sensor=user_input.get("lux_sensor") or None,
        threshold_lux=user_input.get("threshold_lux"),
        timeout_s=int(user_input["timeout_s"]),
        fade_out_s=int(user_input["fade_out_s"]),
        cooldown_s=int(user_input["cooldown_s"]),
        drift_s=int(user_input["drift_s"]),
    )
    # Only the room's own lights can be marked as fading by themselves.
    room["self_fading"] = [light for light in user_input.get("self_fading") or [] if light in room["lights"]]
    if not room["name"]:
        raise ValueError("name")
    # Looks may only mention the room's lights.
    kept = set(room["lights"])
    for look in (room.get("looks") or {}).values():
        if "lights" in look:
            look["lights"] = {light: t for light, t in look["lights"].items() if light in kept}
    room_from(room)  # raises ValueError if it isn't a valid room
    return room


def _hhmm(value: Any) -> str | None:
    return str(value)[:5] if value else None


# ---- setup ---------------------------------------------------------------------


class RoomRoutinesConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            options = {**schedule_to(default_schedule()), CONF_ROOMS: {}}
            return self.async_create_entry(title=NAME, data={}, options=options)
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RoomRoutinesOptionsFlow()


# ---- options -------------------------------------------------------------------


class RoomRoutinesOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._room_id: str | None = None
        self._new_room: dict[str, Any] = {}
        self._period: str | None = None

    def _options(self) -> dict[str, Any]:
        options = clean_options(self.config_entry.options)
        options.setdefault(CONF_ROOMS, {})
        options.setdefault(CONF_PERIODS, schedule_to(default_schedule())[CONF_PERIODS])
        return options

    def _choices(self, values: list[tuple[str, str]]) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=v, label=label) for v, label in values],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    def _room_choices(self) -> SelectSelector:
        return self._choices([(rid, r["name"]) for rid, r in self._options()[CONF_ROOMS].items()])

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["add_room"]
        if self._options()[CONF_ROOMS]:
            menu += ["pick_room", "remove_room"]
        menu.append("periods")
        return self.async_show_menu(step_id="init", menu_options=menu)

    # -- rooms --

    async def async_step_add_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if str(user_input.get("name", "")).strip():
                self._new_room = {"name": str(user_input["name"]).strip(), "area_id": user_input.get("area_id")}
                return await self.async_step_room_details()
            errors["base"] = "no_name"
        return self.async_show_form(
            step_id="add_room", data_schema=vol.Schema(identity_fields(user_input or {})), errors=errors
        )

    async def async_step_room_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self._options()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                room = clean_room({**self._new_room, **user_input}, None)
            except ValueError:
                errors["base"] = "invalid_room"
            else:
                room["looks"] = first_look(room["lights"], schedule_from(options))
                options[CONF_ROOMS][uuid.uuid4().hex[:8]] = room
                return self.async_create_entry(data=options)
        defaults = {"threshold_lux": DEFAULT_THRESHOLD_LUX, **room_suggestions(self.hass, self._new_room.get("area_id"))}
        if user_input is not None:
            defaults.update(user_input)
        return self.async_show_form(
            step_id="room_details",
            data_schema=vol.Schema(detail_fields(defaults)),
            errors=errors,
            description_placeholders={"room": self._new_room.get("name", "")},
        )

    async def async_step_pick_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._room_id = user_input[ROOM]
            return await self.async_step_edit_room()
        return self.async_show_form(
            step_id="pick_room", data_schema=vol.Schema({vol.Required(ROOM): self._room_choices()})
        )

    async def async_step_edit_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self._options()
        previous = options[CONF_ROOMS][self._room_id]
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                room = clean_room(user_input, previous)
            except ValueError:
                errors["base"] = "invalid_room"
            else:
                options[CONF_ROOMS][self._room_id] = room
                return self.async_create_entry(data=options)
        defaults = {**previous, **(user_input or {})}
        return self.async_show_form(
            step_id="edit_room",
            data_schema=vol.Schema({**identity_fields(defaults), **detail_fields(defaults)}),
            errors=errors,
        )

    async def async_step_remove_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            options = self._options()
            options[CONF_ROOMS].pop(user_input[ROOM], None)
            return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="remove_room", data_schema=vol.Schema({vol.Required(ROOM): self._room_choices()})
        )

    # -- periods --

    async def async_step_periods(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="periods", menu_options=["period_times", "add_period", "pick_period"]
        )

    def _save_periods(self, options: dict[str, Any], rows: list[dict], days: list[int]) -> ConfigFlowResult | None:
        try:
            schedule_from({CONF_PERIODS: rows, CONF_ALT_DAYS: days})
        except ValueError:
            return None
        options[CONF_PERIODS] = rows
        options[CONF_ALT_DAYS] = days
        return self.async_create_entry(data=options)

    async def async_step_period_times(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self._options()
        schedule = schedule_from(options)
        errors: dict[str, str] = {}
        if user_input is not None:
            rows = [
                {"name": p.name, "start": _hhmm(user_input[p.name]), "alt_start": _hhmm(user_input.get(p.name + OTHER_DAYS))}
                for p in schedule.periods
            ]
            days = [WEEKDAYS.index(d) for d in user_input.get(CONF_ALT_DAYS, [])]
            if (done := self._save_periods(options, rows, days)) is not None:
                return done
            errors["base"] = "invalid_periods"
        fields: dict[Any, Any] = {}
        for p in schedule.periods:
            fields[vol.Required(p.name, default=p.start.strftime("%H:%M:%S"))] = TimeSelector()
        fields[vol.Optional(CONF_ALT_DAYS, default=[WEEKDAYS[d] for d in sorted(schedule.alt_days)])] = SelectSelector(
            SelectSelectorConfig(options=WEEKDAYS, multiple=True, mode=SelectSelectorMode.LIST)
        )
        for p in schedule.periods:
            extra = {} if p.alt_start is None else {"description": {"suggested_value": p.alt_start.strftime("%H:%M:%S")}}
            fields[vol.Optional(p.name + OTHER_DAYS, **extra)] = TimeSelector()
        return self.async_show_form(step_id="period_times", data_schema=vol.Schema(fields), errors=errors)

    def _period_fields(self, defaults: dict[str, Any], removable: bool) -> vol.Schema:
        fields: dict[Any, Any] = {
            vol.Required("name", **_suggest(defaults, "name")): TextSelector(),
            vol.Required("start", **_suggest(defaults, "start")): TimeSelector(),
            vol.Optional("alt_start", **_suggest(defaults, "alt_start")): TimeSelector(),
        }
        if removable:
            fields[vol.Optional("remove", default=False)] = BooleanSelector()
        return vol.Schema(fields)

    async def async_step_add_period(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self._options()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input["name"]).strip()
            rows = options[CONF_PERIODS] + [
                {"name": name, "start": _hhmm(user_input["start"]), "alt_start": _hhmm(user_input.get("alt_start"))}
            ]
            if name and (done := self._save_periods(options, rows, options.get(CONF_ALT_DAYS) or [])) is not None:
                return done
            errors["base"] = "invalid_periods"
        return self.async_show_form(
            step_id="add_period", data_schema=self._period_fields(user_input or {}, False), errors=errors
        )

    async def async_step_pick_period(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._period = user_input[PERIOD]
            return await self.async_step_edit_period()
        names = [r["name"] for r in self._options()[CONF_PERIODS]]
        return self.async_show_form(
            step_id="pick_period", data_schema=vol.Schema({vol.Required(PERIOD): self._choices([(n, n) for n in names])})
        )

    async def async_step_edit_period(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self._options()
        rows = options[CONF_PERIODS]
        old = next(r for r in rows if r["name"] == self._period)
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("remove"):
                rows = [r for r in rows if r["name"] != self._period]
                new_name = None
            else:
                new_name = str(user_input["name"]).strip()
                rows = [
                    {"name": new_name, "start": _hhmm(user_input["start"]), "alt_start": _hhmm(user_input.get("alt_start"))}
                    if r is old else r
                    for r in rows
                ]
            if (new_name is None or new_name) and rows:
                # Rooms' looks follow the period: renamed with it, dropped with it
                # (a dropped period's rooms then borrow the previous period's look).
                for room in options[CONF_ROOMS].values():
                    looks = room.get("looks") or {}
                    if self._period in looks:
                        look = looks.pop(self._period)
                        if new_name:
                            looks[new_name] = look
                if (done := self._save_periods(options, rows, options.get(CONF_ALT_DAYS) or [])) is not None:
                    return done
            errors["base"] = "invalid_periods" if rows else "last_period"
        defaults = {"name": old["name"], "start": f"{old['start']}:00",
                    "alt_start": f"{old['alt_start']}:00" if old.get("alt_start") else None}
        return self.async_show_form(
            step_id="edit_period",
            data_schema=self._period_fields(defaults, len(rows) > 1),
            errors=errors,
            description_placeholders={"period": old["name"]},
        )
