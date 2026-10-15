"""What each room's lights will do through a day, for the page's Timeline.

Worked out from the same settings the rooms run on (periods, looks, Dark Day
looks and brightness, blends, timers, rules, Someone's there looks, Informs,
Lights out), so the drawing can't drift from what actually happens.

A room gets one *lane* per way its day can go:

- the **base** lanes: the day as it normally runs. Days that run the same are
  merged ("Every day", "Mon–Fri"), and a Dark Day gets its own lane only where
  it differs;
- a **someone** lane above the routine, for a room whose routine runs all day
  and that brightens while someone's there;
- one **rule** lane per "while …" rule (the room's own, then the house's), the
  whole day as it goes while that rule holds, in priority order;
- one **inform** lane per Inform, in priority order: Informs follow things that
  can't be predicted (a call), so the lane says what the lights show, and
  today's history fills it in.

Times are minutes from midnight. A band either lasts while someone's there
(``visit``: the room is dark until someone comes in) or is on throughout (an
all-day routine). A blend band moves from ``look`` to ``blend_to``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from .looks import Look, LookSource, resolve
from .periods import Schedule
from .room import RoomConfig
from .rules import CAP, LOOK, NOTHING as RULE_NOTHING, Rule
from .timers import ON as TIMER_ON, Today, due
from .tracks import DIM, NORMAL

DAY = 24 * 60
WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Band:
    start: int
    end: int
    period: str | None
    look: Look
    track: str = NORMAL
    factor: float = 1.0  # Dark Day brightness on a Normal look
    cap: float | None = None  # a "no brighter than" rule
    visit: bool = True  # only while someone's there
    blend_to: Look | None = None
    blend_period: str | None = None
    blend_factor: float = 1.0  # Dark Day brightness on the look being blended into


@dataclass(frozen=True)
class Lane:
    key: str  # "base", "someone", "rule:<n>", "house_rule:<n>", "inform:<n>"
    label: str
    bands: tuple[Band, ...] = ()
    note: str = ""
    days: tuple[int, ...] = ()  # weekdays a base lane covers
    dark: bool = False
    today: bool = False
    index: int | None = None  # position in its list (rules, house rules, Informs)


@dataclass(frozen=True)
class DayFacts:
    """What the integration knows about the days being drawn."""

    today: date
    workday_today: bool | None  # None: no Workday sensor (Mon–Fri then)
    dark_possible: bool  # Dark Days are switched on
    dark_periods: tuple[str, ...] = ()
    auto_dim: float = 1.0
    sun_down: Callable[[int], bool] = field(default=lambda minute: False)
    # When a live Lights out covering the room runs on a day: a minute, or
    # (day, workday) -> minute or None.
    lights_out: int | Callable[[date, bool], int | None] | None = None
    house_schedule: Schedule | None = None  # Dark Days follow the house's periods

    def lights_out_on(self, day: date, workday: bool) -> int | None:
        return self.lights_out(day, workday) if callable(self.lights_out) else self.lights_out

    def workday(self, day: date) -> bool:
        if day == self.today and self.workday_today is not None:
            return self.workday_today
        return day.weekday() < 5

    def house_period_at(self, schedule: Schedule, day: date, minute: int) -> str:
        spans = day_spans(self.house_schedule or schedule, day)
        return next((name for start, end, name in spans if start <= minute < end), spans[-1][2])

    def dim_at(self, schedule: Schedule, day: date, minute: int, dark: bool) -> bool:
        """On the Dark Day track then: a Dark Day, inside one of the house's dark periods."""
        return dark and self.house_period_at(schedule, day, minute) in self.dark_periods


def day_spans(schedule: Schedule, day: date) -> list[tuple[int, int, str]]:
    """The day's periods as (start, end, name), covering midnight to midnight."""
    starts = sorted(
        ((schedule.start_on(p, day).hour * 60 + schedule.start_on(p, day).minute, p.name) for p in schedule.periods)
    )
    out: list[tuple[int, int, str]] = []
    if starts[0][0] > 0:
        # Yesterday's last period runs on past midnight (yesterday's times: alt days differ).
        before = day - timedelta(days=1)
        last = max(schedule.periods, key=lambda p: (schedule.start_on(p, before), p.name))
        out.append((0, starts[0][0], last.name))
    for i, (start, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else DAY
        out.append((start, end, name))
    return out


def _next_period(spans: list[tuple[int, int, str]], index: int) -> str | None:
    """The period after this span; None where it carries on past midnight."""
    if index + 1 < len(spans):
        return spans[index + 1][2]
    return None if spans[0][0] == 0 and spans[0][1] < DAY and spans[0][2] == spans[index][2] else spans[0][2]


def _own_someone(config: RoomConfig, period: str, track: str) -> LookSource | None:
    """A period's own Someone's there look (they never borrow from other periods)."""
    if track != NORMAL and period in config.someone_dim_looks:
        return LookSource(config.someone_dim_looks[period], period, track)
    if period in config.someone_looks:
        return LookSource(config.someone_looks[period], period, NORMAL)
    return None


def _source(config: RoomConfig, schedule: Schedule, period: str, track: str, rule: Rule | None,
            someone: bool, visiting: bool = False) -> LookSource | None:
    """The look a period gets, as the room works it out. ``someone``: the row
    above an all-day routine (None where Someone's there has no look then).
    ``visiting``: someone came in (motion) while no routine was running."""
    if someone:
        return _own_someone(config, period, track)
    if rule is not None and rule.action == LOOK:
        if rule.scene:
            return LookSource(Look(scene=rule.scene), period, track)
        period = rule.period or period
    if visiting and (own := _own_someone(config, period, track)):
        return own  # someone's there: their own look where the period has one
    return resolve(period, track, config.looks, config.dim_looks, schedule)


def _while(condition, who: str) -> str:
    verb = "isn't" if condition.negate else "is"
    return f"{who} {verb} {condition.state}"


def _events(config: RoomConfig, schedule: Schedule, day: date, facts: DayFacts, dark: bool) -> list[tuple[int, bool]]:
    """A day's timer and Lights out switches, as (minute, on), in the order they act."""
    workday = facts.workday(day)
    events: list[tuple[int, bool]] = []
    for timer in config.timers:
        minute = timer.at.hour * 60 + timer.at.minute
        # Dark for a timer: on the Dark Day track then, or the sun is down (as the house works it out).
        dark_then = facts.dim_at(schedule, day, minute, dark) or facts.sun_down(minute)
        if due(timer, Today(day, workday, dark_then, {p: True for p in timer.only_home}))[0]:
            events.append((minute, timer.action == TIMER_ON))
    if (lights_out := facts.lights_out_on(day, workday)) is not None:
        events.append((lights_out, False))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def _routine_on(config: RoomConfig, schedule: Schedule, day: date, facts: DayFacts, dark: bool,
                rule: Rule | None) -> list[tuple[int, int]] | None:
    """When an all-day routine is running: [(start, end)], or None if nothing in
    its settings says when (it starts from a starter, a button or by hand)."""
    if rule is not None and rule.action == RULE_NOTHING:
        return []
    if not any(t.action == TIMER_ON for t in config.timers):
        return None
    # Running at midnight: the last switch on the days before (within a week) left it on.
    on = False
    for back in range(1, 8):
        if before := _events(config, schedule, day - timedelta(days=back), facts, dark):
            on = before[-1][1]
            break
    out: list[tuple[int, int]] = []
    since = 0
    for minute, switch_on in _events(config, schedule, day, facts, dark):
        if switch_on and not on:
            on, since = True, minute
        elif not switch_on and on:
            if minute > since:
                out.append((since, minute))
            on = False
    if on and since < DAY:
        out.append((since, DAY))
    return out


def _segments(windows: list[tuple[int, int]], visits: bool) -> list[tuple[int, int, bool]]:
    """The routine's windows (not visits), and between them, if motion starts
    visits, the rest of the day (visits)."""
    out = [(a, b, False) for a, b in windows]
    if visits:
        at = 0
        for a, b in sorted(windows):
            if a > at:
                out.append((at, a, True))
            at = max(at, b)
        if at < DAY:
            out.append((at, DAY, True))
    return sorted(out)


def _bands(config: RoomConfig, schedule: Schedule, day: date, facts: DayFacts, dark: bool,
           rule: Rule | None = None, someone: bool = False,
           within: list[tuple[int, int]] | None = None) -> tuple[Band, ...] | None:
    """One way the day can go. None: the routine has no known times (a lane note
    says so). ``within``: the someone row, only while the routine runs."""
    if rule is not None and rule.action == RULE_NOTHING:
        return ()
    spans = day_spans(schedule, day)
    ambient = config.runs_ambient and not someone
    if ambient:
        windows = _routine_on(config, schedule, day, facts, dark, rule)
        if windows is None:
            return None
        segments = _segments(windows, config.has_sensors)  # motion outside the routine is a visit
    else:
        segments = [(a, b, True) for a, b in (within if within is not None else [(0, DAY)])]
    # Where the Dark Day track starts and stops (the house's periods), inside the room's.
    house_cuts = {s for s, _, _ in day_spans(facts.house_schedule or schedule, day)} if dark else set()
    cap = rule.max_pct if rule is not None and rule.action == CAP else None
    no_blend = (rule is not None and rule.action == LOOK) or someone
    bands: list[Band] = []
    for i, (start, end, period) in enumerate(spans):
        nxt = _next_period(spans, i)
        blend_minutes = 0 if no_blend or nxt is None else int(config.blends.get(period, 0))
        blend_from = max(start, end - blend_minutes) if blend_minutes else end
        for seg_a, seg_b, visiting in segments:
            lo, hi = max(seg_a, start), min(seg_b, end)
            if hi <= lo:
                continue
            cuts = sorted({lo, hi, *(c for c in (blend_from, *house_cuts) if lo < c < hi)})
            for a, b in zip(cuts, cuts[1:]):
                track = DIM if facts.dim_at(schedule, day, a, dark) else NORMAL
                source = _source(config, schedule, period, track, rule, someone, visiting)
                if source is None or source.look.nothing:
                    continue
                factor = facts.auto_dim if track != NORMAL and source.track == NORMAL else 1.0
                common = dict(period=period, look=source.look, track=track, factor=factor, cap=cap,
                              visit=visiting)
                target = None
                if a >= blend_from:
                    # The room blends into the next period's look as it shows on the track it's on now.
                    nsrc = _source(config, schedule, nxt, track, rule, someone, visiting)
                    if nsrc is not None and not nsrc.look.nothing:
                        target = nsrc
                if target is not None:
                    tfactor = facts.auto_dim if track != NORMAL and target.track == NORMAL else 1.0
                    bands.append(Band(a, b, blend_to=target.look, blend_period=nxt, blend_factor=tfactor, **common))
                else:
                    bands.append(Band(a, b, **common))
    return _merged(bands)


def _merged(bands: list[Band]) -> tuple[Band, ...]:
    """Neighbouring bands that look the same become one (named after both ends)."""
    out: list[Band] = []
    for band in bands:
        last = out[-1] if out else None
        same = last is not None and last.end == band.start and (
            last.look, last.track, last.factor, last.cap, last.visit, last.blend_to, last.blend_factor
        ) == (band.look, band.track, band.factor, band.cap, band.visit, band.blend_to, band.blend_factor)
        if same and band.blend_to is not None:
            same = last.period == band.period and last.blend_period == band.blend_period  # one blend, cut in two
        if same:
            first = (last.period or "").split(" to ")[0]
            name = first if first == band.period else f"{first} to {band.period}"
            if last.start == 0 and band.end == DAY:
                name = "All day"
            out[-1] = Band(last.start, band.end, name, last.look, last.track, last.factor, last.cap, last.visit,
                           last.blend_to, last.blend_period, last.blend_factor)
        else:
            out.append(band)
    return tuple(out)


def _days_label(days: Iterable[int]) -> str:
    days = sorted(set(days))
    if days == list(range(7)):
        return "Every day"
    if days == [0, 1, 2, 3, 4]:
        return "Mon–Fri"
    if days == [5, 6]:
        return "Sat, Sun"
    return ", ".join(WEEKDAY_SHORT[d] for d in days)


def room_lanes(
    config: RoomConfig,
    schedule: Schedule,
    facts: DayFacts,
    house_rules: Iterable[tuple[int, Rule]] = (),
    names: Mapping[str, str] | None = None,
) -> list[Lane]:
    """Every lane for a room, in the order the page draws them."""
    names = names or {}
    name = lambda entity: names.get(entity, entity)  # noqa: E731
    dark_differs = facts.dark_possible and bool(facts.dark_periods) and (
        bool(config.dim_looks) or bool(config.someone_dim_looks) or facts.auto_dim != 1.0
        or any(t.only_dark for t in config.timers)
    )

    # The base day, over the next seven days, Normal and (where it differs) Dark.
    variants: list[tuple[tuple[Band, ...] | None, int, bool, bool]] = []
    for offset in range(7):
        day = facts.today + timedelta(days=offset)
        normal = _bands(config, schedule, day, facts, False)
        variants.append((normal, day.weekday(), False, offset == 0))
        if dark_differs:
            dark = _bands(config, schedule, day, facts, True)
            if dark != normal:  # a Dark Day the same as that day's Normal day isn't drawn twice
                variants.append((dark, day.weekday(), True, offset == 0))
    groups: list[tuple[tuple[Band, ...] | None, bool, list[int], bool]] = []
    for bands, weekday, dark, is_today in variants:
        for i, (gb, gdark, gdays, gtoday) in enumerate(groups):
            if gb == bands and gdark == dark:
                gdays.append(weekday)
                groups[i] = (gb, gdark, gdays, gtoday or is_today)
                break
        else:
            groups.append((bands, dark, [weekday], is_today))
    groups.sort(key=lambda g: (not g[3], g[1]))
    lanes: list[Lane] = []
    only_one = len([g for g in groups if not g[1]]) == 1
    for bands, dark, days, is_today in groups:
        label = "Every day" if only_one and not dark else _days_label(days)
        if dark:
            label = "Dark Day" if len(set(days)) == 7 else f"{label} · Dark Day"
        note = "" if bands is not None else "Runs when it's started (a starter, a button or the lights switched on), until it's switched off."
        lanes.append(Lane("base", label, bands or (), note, tuple(sorted(days)), dark, is_today))

    today = facts.today
    if config.runs_ambient and config.has_someone_looks:
        # Someone's there brightens the routine while it runs (outside it, a visit is on the routine's lane).
        within = _routine_on(config, schedule, today, facts, False, None)
        lanes.insert(0, Lane("someone", "Someone's here",
                             _bands(config, schedule, today, facts, False, someone=True, within=within) or ()))

    for key, items in (("rule", list(enumerate(config.rules))), ("house_rule", list(house_rules))):
        for index, rule in items:
            who = name(rule.when.entity)
            label = rule.name or f"While {_while(rule.when, who)}"
            bands = _bands(config, schedule, today, facts, False, rule=rule)
            note = ""
            if rule.action == RULE_NOTHING:
                note = ("Nothing starts while this holds (a routine already running carries on)."
                        if config.runs_ambient else "Does nothing while this holds: no lights come on.")
            if bands is None:
                bands, note = (), "Runs when it's started, with this rule's look."
            lanes.append(Lane(key, label, bands, note, index=index))

    for index, signal in enumerate(config.signals):
        who = name(signal.when.entity)
        label = signal.name or f"While {_while(signal.when, who)}"
        lights = ", ".join(name(light) for light in signal.lights)
        lanes.append(Lane("inform", label, (), f"Whenever {_while(signal.when, who)}: {lights}", index=index))
    return lanes
