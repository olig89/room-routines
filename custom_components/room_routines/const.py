"""Names shared across the integration."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

DOMAIN = "room_routines"
NAME = "Room Routines"
VERSION = "0.5.0"  # must match manifest.json and the panel (a test checks)

MODE_OFF = "off"
MODE_LOG_ONLY = "log_only"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_LOG_ONLY, MODE_LIVE)

CONF_PERIODS = "periods"
CONF_ALT_DAYS = "alt_days"
CONF_ROOMS = "rooms"
CONF_TRACKS = "tracks"
CONF_HIDDEN_AREAS = "hidden_areas"

SERVICE_SET_LOOK = "set_look"
SERVICE_SWITCH_ON = "switch_on"
SERVICE_SWITCH_OFF = "switch_off"


def room_signal(entry_id: str, room_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_room_{room_id}"


def house_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_house"


# Sent on any change the panel shows (room, house, setup, unload).
ANY_SIGNAL = f"{DOMAIN}_any"

PANEL_URL = "room-routines"
PANEL_COMPONENT = "room-routines-panel"
STATIC_URL = "/room_routines_static"


OPTION_KEYS = (CONF_PERIODS, CONF_ALT_DAYS, CONF_ROOMS, CONF_TRACKS, CONF_HIDDEN_AREAS)


def clean_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy of the options holding only the keys this integration writes."""
    return {key: copy.deepcopy(value) for key, value in options.items() if key in OPTION_KEYS}
