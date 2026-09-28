"""Pure decision logic for Room Routines. Nothing in here imports Home Assistant.

- ``periods``: which period of the day it is (overnight, morning, ...).
- ``looks``: what a room's lights should do in a period, and scaling by lux.
- ``lux``: the room's light level, read only while its own lights are off.
- ``matching``: telling the integration's own light changes from a person's.
- ``fade``: a stepped fade for lights that can't transition on their own.
- ``room``: one room's behaviour, as events in and actions out.
"""
