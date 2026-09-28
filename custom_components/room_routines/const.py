"""Names shared across the integration."""

from __future__ import annotations

DOMAIN = "room_routines"
NAME = "Room Routines"

MODE_OFF = "off"
MODE_LOG_ONLY = "log_only"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_LOG_ONLY, MODE_LIVE)

CONF_PERIODS = "periods"
CONF_ALT_DAYS = "alt_days"
CONF_ROOMS = "rooms"

SERVICE_SET_LOOK = "set_look"


def room_signal(entry_id: str, room_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_room_{room_id}"


def house_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_house"
