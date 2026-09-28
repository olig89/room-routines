# Room Routines

Motion lights that follow your household's daily routine, for Home Assistant.

Each room switches to the look chosen for the current period of the day
(overnight, early morning, morning, day, evening) when someone arrives, scaled by
how dark the room really is, and switches off when they leave. A light switched
on by hand is left alone. Each room can run in log-only mode first.

**Status: early development (0.1.0).** Install through HACS as a custom repository.

## What you get

- **Setup** creates five periods (overnight 23:00, early morning 05:30, morning
  07:00, day 09:00, evening 17:00). In the settings you can change the times,
  give some days different times, and add, rename or remove periods (one or
  more).
- **Rooms** are added in the settings. Pick an area and its lights and
  motion/presence sensors are suggested; every field takes any entity. Each
  sensor is either one that can switch the lights on or one that can only keep
  them on (any kind of sensor can take either role). Plus a light-level sensor,
  the lux threshold, the timeout, the fade-out (15 s by default), the cooldown
  after a hand switch-off, and the drift time when the period changes.
- **Fade-out:** lights with a native transition fade by themselves; others (KNX)
  get small brightness steps, and come back at their old level next time.
- **Per room:** a *Status* sensor (idle / lights on by motion / switched on by
  hand, with the reason), a *Mode* select (off / log only / live; new rooms start
  in log only) and a *Light level* sensor (the held ambient value).
- **House-wide:** a *Period* select (choose one by hand to hold it until the next
  scheduled start) and a *Stealth mode* switch (every motion sensor reads as
  "nobody here"; lit rooms still go dark).
- **Looks:** a new room switches every light on at its last brightness in every
  period. Set the lights how you want them and run the **Save a room look**
  action (`room_routines.set_look`) for the period; "nothing" keeps the room dark
  in a period; "borrow" makes it use the previous period's look. Saving a look
  doesn't restart anything.

## Development

`core/` has no Home Assistant imports, so its tests run anywhere:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/core

The full suite needs `pytest-homeassistant-custom-component`, which runs on Linux
(or WSL) only.
