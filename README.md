# Room Routines

Lights that follow your household's daily routine, for Home Assistant: by
motion, by the clock, or both.

Each room switches to the look chosen for the current period of the day
(overnight, early morning, morning, day, evening) when someone arrives, if the
room is dark enough, and switches off when they leave. A room without sensors
(an office, a living room) can run a time-based routine instead: started by a
timer, a button or the lights being switched on, it follows the day, blending
gradually from one period's look to the next, and a room can have its own
period times (an office's evening starting after the working day). On Dark Days (grey days,
judged from the weather and/or a light sensor) rooms can use a second set of
looks, or their usual looks dimmer or brighter. A look can
be a Home Assistant scene. A light switched on by hand is left alone, and changes
made by hand are remembered so the page can suggest better looks or times. Each
room can run in log-only mode first.

**Status: early development (0.6.1).** Install through HACS as a custom repository.

## What you get

- **A sidebar page** (admins only for now):
  - **Status**: each room's mode, what it's doing and why, the switch-off countdown, its light level against the threshold, its sensors (lit when they see someone), its lights and the look for the current period.
  - **Room detail**: a looks table (period × light) for Normal days and for Dark Days, with icon buttons under each period name: *Save as now* (saves the lights as a Home Assistant scene through the scene editor's own API, so the same scene can go on a wall button), *Pick a scene*, *Edit*, *Keep dark* and *Use previous* / *Use Normal*; suggestions from how the lights get changed by hand; what the room did, from Home Assistant's history; and for rooms in log-only, a **dry-run check** that pairs every switch the room would have made with the light's real switch and scores the match.
  - **Your day**: the periods as a 24-hour strip, with a row for days that start differently, and whether today is a Normal day or a Dark Day, and why.
  - **Settings** (administrators only, and refused to anyone else by the server): add, change or remove rooms, with lights and sensors suggested from the area and any entity choosable; change, add, rename or remove periods (rooms' looks follow a renamed period, and a removed period's look passes to the period after it when that one was using it); Dark Days (where the reading comes from, which periods, the thresholds, the sunlight floor and the brightness).
  - A header with the current period, a stealth switch, and a banner across the page while stealth is on.
- **Setup** creates five periods (overnight 23:00, early morning 05:30, morning
  07:00, day 09:00, evening 17:00). In the settings you can change the times,
  give some days different times, and add, rename or remove periods (one or
  more).
- **Every area with lights becomes a room**, switched off so it does nothing
  until you set it up; hide the ones you'll never use (they stay hidden until
  you show them again). More rooms can be added in the settings.
- **Rooms** are set up in the settings. Pick an area and its lights and
  motion/presence sensors are suggested; every field takes any entity. Each
  sensor is either one that can switch the lights on or one that can only keep
  them on (any kind of sensor can take either role). Plus a light-level sensor,
  the lux threshold, the timeout, the fade-out (15 s by default), the cooldown
  after a hand switch-off, and the drift time when the period changes.
  Sensors are optional.
- **Time-based routines** (per room, all optional):
  - **its own period times**: move a period for this room only;
  - **blending**: over the minutes before the next period starts, brightness and
    colour move gradually from one look to the next (looks that set a level and
    colour temperature or colour);
  - **lights switched on another way** (a wall switch, an app): leave them alone
    (default) or start the routine;
  - **timers**: start the routine or switch off at a time, every day, on workdays
    (from Home Assistant's Workday integration if you have one, else Monday to
    Friday) or on chosen days, optionally only when it's dark and only if chosen
    people are home;
  - the **Start a room's routine** / **Switch a room off** actions
    (`room_routines.switch_on` / `switch_off`, given the room's Status sensor),
    for wall buttons.
  A change made by hand while the routine runs pauses it until the lights are
  switched off. A room without sensors stays on until something switches it off.
  A routine that was running carries on after Home Assistant restarts (a hand
  change that was holding it stays held), instead of the room treating its lit
  lights as switched on by hand. A light that reports in late after the restart
  still counts, for up to five minutes.
- **Listening to the rest of the house** (per room, all optional):
  - **starters**: start the routine when any entity reaches a state (a computer
    switching on, a door opening, someone coming home); unlike a motion sensor it
    doesn't switch the room off when it changes back;
  - **only when**: every way of starting needs all of these (use "isn't" for
    "not while": baby bedtime isn't on);
  - **while X**: rules that hold while an entity is (or isn't) in a state: do
    nothing at all, use another look (a scene or another period's look), or keep
    every light at or below a brightness. A lit room moves to the new look when a
    rule starts or ends. **House rules** do the same for chosen rooms, or every
    room (away from home, the baby asleep); a room's own rules come first.
- **Someone's there over a routine** (optional, for rooms with sensors): a
  second looks table, *Someone's there*. While the room's routine runs (a timer,
  a starter, a button, lights switched on by hand), motion moves the lights set
  there to those looks, the others stay with the routine, and when the room is
  empty they fade back to the routine instead of going off: a hallway that sits
  very dim all evening and brightens as someone walks through. Without such
  looks a motion room works as it always has.
- **Signals** (optional, per room): some of the room's lights show something
  while an entity is in a state: in a call, the desk lamp purple; muted, green.
  Colour and brightness per light, optionally one of the light's own effects and
  a flash when it starts. While a signal holds a light nothing else in the room
  touches it (a scene look is sent without it), it shows even with the room off,
  and when it ends the light goes back to what the room is doing, or to how it
  was before. The first signal in the list wins a light two signals want.
- **Fade-out:** lights with a native transition fade by themselves; others (KNX)
  get small brightness steps, and come back at their old level next time.
- **Per room:** a *Status* sensor (idle / lights on by motion / switched on by
  hand, with the reason, and a `layer` attribute saying what has the lights:
  `ambient` (the routine), `someone` (motion), `hand` (a change made by hand) or
  empty when they're off), a *Mode* select (off / log only / live; new rooms start
  in log only) and a *Light level* sensor (the held ambient value).
- **Dark Days:** a day counts as dark by comparing the light now with a
  *clear* day at the same height of the sun, so an ordinary evening or a bright
  cloudy day isn't dark. Two sources, one checked first (your choice) and the
  other when it has no answer:
  - **the weather** (default, enough on its own): the sunlight reaching the
    ground at Home Assistant's location, from [Open-Meteo](https://open-meteo.com)
    (free, no account, asked four times an hour), against a clear-sky model;
  - **a light sensor** (optional, with a backup): its 15-minute average against
    what it reads on a clear day at that sun height, learned once a day from its
    own statistics in Home Assistant's recorder (so it needs a few days, and one
    bright one, before it can answer).
  Dark Days only happen in the periods you choose (by default the ones starting
  between 06:00 and 15:00); in them, the sun being down counts as dark. Below 40 %
  of a clear day it becomes a Dark Day, above 55 % Normal again, and each holds
  for at least 20 minutes. With the weather on, less than 150 W/m² of sunlight
  on the ground is also a Dark Day, whatever a clear day would give (Normal again
  above 180 W/m²; 0 switches it off): far north in winter even a clear noon is
  darker than a grey summer day. No reading keeps the current day. A period without
  its own Dark Day look uses its Normal one, and a lit room drifts to the new
  look when the day turns.
  **Brightness** (optional): on a Dark Day, a period without its own Dark Day
  look uses its Normal look at this brightness: below 100 % dims (at 50 %, a
  light at 66 % comes on at 33 %), above 100 % brightens, but only up to each
  light's maximum. Lights at their last brightness, and scenes from other apps
  (the Hue app), are left as they are; the page marks those scenes.
- **Scenes:** any Home Assistant scene can be a look. For scenes made in Home
  Assistant, Room Routines reads the settings, so lights without a native
  transition still drift in steps and the result is recognised as its own; a
  scene from another app (the Hue app) is simply turned on.
- **Suggestions:** in Live rooms, a change made by hand to lights the room
  switched on is remembered (kept 60 days, in Home Assistant's storage). Four or
  more on three or more days in four weeks, in the same period, becomes a
  suggestion: keep the room dark then, start the next period earlier (or this one
  later), or save the latest change as the look. *Not now* hides one for four
  weeks.
- **House-wide:** a *Period* select (choose one by hand to hold it until the next
  scheduled start), a *Day* select (Normal day or Dark Day, with the reading and
  the reason; choose one by hand to hold it until the next period) and a *Stealth mode* switch (every motion sensor reads as
  "nobody here"; lit rooms still go dark).
- **Looks:** a new room switches every light on at its last brightness in every
  period. Set the lights how you want them and run the **Save a room look**
  action (`room_routines.set_look`) for the period; "scene" turns on a scene; "nothing" keeps
  the room dark in a period; "borrow" makes it use the previous period's look
  (on the Dark Day track, `dim`: the period's Normal look). Saving a look doesn't restart
  anything.

## Development

`core/` has no Home Assistant imports, so its tests run anywhere:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/core

The full suite needs `pytest-homeassistant-custom-component`, which runs on Linux
(or WSL) only.
