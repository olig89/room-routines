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
    lights_out: int | None = None  # minute a Lights out covering the room runs, if known


def day_spans(schedule: Schedule, day: date) -> list[tuple[int, int, str]]:
    """The day's periods as (start, end, name), covering midnight to midnight."""
    starts = sorted(
        ((schedule.start_on(p, day).hour * 60 + schedule.start_on(p, day).minute, p.name) for p in schedule.periods)
    )
    out: list[tuple[int, int, str]] = []
    if starts[0][0] > 0:
        out.append((0, starts[0][0], starts[-1][1]))  # the last period runs on past midnight
    for i, (start, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else DAY
        out.append((start, end, name))
    return out


def _next_period(spans: list[tuple[int, int, str]], index: int) -> str:
    return spans[(index + 1) % len(spans)][2] if index + 1 < len(spans) else spans[0][2]


def _own_someone(config: RoomConfig, period: str, track: str) -> LookSource | None:
    """A period's own Someone's there look (they never borrow from other periods)."""
    if track != NORMAL and period in config.someone_dim_looks:
        return LookSource(config.someone_dim_looks[period], period, track)
    if period in config.someone_looks:
        return LookSource(config.someone_looks[period], period, NORMAL)
    return None


def _source(config: RoomConfig, schedule: Schedule, period: str, track: str, rule: Rule | None,
            someone: bool) -> LookSource | None:
    """The look a period gets, as the room works it out. ``someone``: the row
    above an all-day routine (None where Someone's there has no look then)."""
    if someone:
        return _own_someone(config, period, track)
    if rule is not None and rule.action == LOOK:
        if rule.scene:
            return LookSource(Look(scene=rule.scene), period, track)
        period = rule.period or period
    if config.has_sensors and not config.runs_ambient and (own := _own_someone(config, period, track)):
        return own  # a motion room uses its Someone's there look where it has one
    return resolve(period, track, config.looks, config.dim_looks, schedule)


def _while(condition, who: str) -> str:
    return f"{who} {'is not' if condition.negate else 'is'} {condition.state}"


def _routine_on(config: RoomConfig, day: date, facts: DayFacts, workday: bool, dark: bool,
                rule: Rule | None) -> list[tuple[int, int]] | None:
    """When an all-day routine is running: [(start, end)], or None if nothing in
    its settings says when (it starts from a starter, a button or by hand)."""
    if rule is not None and rule.action == RULE_NOTHING:
        return []
    events: list[tuple[int, bool]] = []
    for timer in config.timers:
        minute = timer.at.hour * 60 + timer.at.minute
        today = Today(day, workday, dark or facts.sun_down(minute), {p: True for p in timer.only_home})
        if due(timer, today)[0]:
            events.append((minute, timer.action == TIMER_ON))
    if facts.lights_out is not None:
        events.append((facts.lights_out, False))
    if not any(on for _, on in events):
        # A timer that isn't due today: off all day. No timer at all: it starts some other way.
        return [] if any(t.action == TIMER_ON for t in config.timers) else None
    events.sort(key=lambda e: (e[0], e[1]))

    def run(on_at_midnight: bool) -> tuple[list[tuple[int, int]], bool]:
        out, on, since = [], on_at_midnight, 0
        for minute, switch_on in events:
            if switch_on and not on:
                on, since = True, minute
            elif not switch_on and on:
                if minute > since:
                    out.append((since, minute))
                on = False
        if on and since < DAY:
            out.append((since, DAY))
        return out, on

    first, ends_on = run(False)
    if ends_on:
        first, _ = run(True)  # yesterday ran on past midnight in the same way
    return [(a, b) for a, b in first if b > a]


def _bands(config: RoomConfig, schedule: Schedule, day: date, facts: DayFacts, workday: bool, dark: bool,
           rule: Rule | None = None, someone: bool = False) -> tuple[Band, ...] | None:
    """One way the day can go. None: the routine has no known times (a lane note says so)."""
    if rule is not None and rule.action == RULE_NOTHING:
        return ()
    spans = day_spans(schedule, day)
    ambient = config.runs_ambient and not someone
    windows = _routine_on(config, day, facts, workday, dark, rule) if ambient else [(0, DAY)]
    if windows is None:
        return None
    cap = rule.max_pct if rule is not None and rule.action == CAP else None
    bands: list[Band] = []
    for i, (start, end, period) in enumerate(spans):
        track = DIM if dark and period in facts.dark_periods else NORMAL
        source = _source(config, schedule, period, track, rule, someone)
        if source is None or source.look.nothing:
            continue
        factor = facts.auto_dim if track != NORMAL and source.track == NORMAL else 1.0
        blend_minutes = 0 if (rule is not None and rule.action == LOOK) or someone else config.blends.get(period, 0)
        nxt = _next_period(spans, i)
        blend_from = max(start, end - int(blend_minutes)) if blend_minutes else end
        target = None
        if blend_minutes:
            next_track = DIM if dark and nxt in facts.dark_periods else NORMAL
            nsrc = _source(config, schedule, nxt, next_track, rule, someone)
            target = None if nsrc is None or nsrc.look.nothing else nsrc.look
            if target is None:
                blend_from = end
        for a, b in windows:
            lo, hi = max(a, start), min(b, end)
            if hi <= lo:
                continue
            common = dict(period=period, look=source.look, track=track, factor=factor, cap=cap, visit=not ambient)
            if target is not None and blend_from < hi:
                if blend_from > lo:
                    bands.append(Band(lo, blend_from, **common))
                bands.append(Band(max(lo, blend_from), hi, blend_to=target, blend_period=nxt, **common))
            else:
                bands.append(Band(lo, hi, **common))
    return _merged(bands)


def _merged(bands: list[Band]) -> tuple[Band, ...]:
    """Neighbouring bands that look the same become one (named after both ends)."""
    out: list[Band] = []
    for band in bands:
        last = out[-1] if out else None
        if (
            last is not None and last.end == band.start and last.blend_to is None and band.blend_to is None
            and (last.look, last.track, last.factor, last.cap, last.visit)
            == (band.look, band.track, band.factor, band.cap, band.visit)
        ):
            first = (last.period or "").split(" to ")[0]
            name = first if first == band.period else f"{first} to {band.period}"
            if last.start == 0 and band.end == DAY:
                name = "All day"
            out[-1] = Band(last.start, band.end, name, last.look, last.track, last.factor, last.cap, last.visit)
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
        workday = facts.workday_today if offset == 0 and facts.workday_today is not None else day.weekday() < 5
        for dark in (False, True) if dark_differs else (False,):
            variants.append((_bands(config, schedule, day, facts, workday, dark), day.weekday(), dark, offset == 0))
    groups: list[tuple[tuple[Band, ...] | None, bool, list[int], bool]] = []
    for bands, weekday, dark, is_today in variants:
        for i, (gb, gdark, gdays, gtoday) in enumerate(groups):
            if gb == bands and gdark == dark:
                gdays.append(weekday)
                groups[i] = (gb, gdark, gdays, gtoday or is_today)
                break
        else:
            groups.append((bands, dark, [weekday], is_today))
    if dark_differs:
        # A Dark Day lane the same as its Normal day isn't drawn twice.
        normal = [(sorted(d), b) for b, dk, d, _ in groups if not dk]
        groups = [g for g in groups if not g[1] or (sorted(g[2]), g[0]) not in normal]
    groups.sort(key=lambda g: (not g[3], g[1]))
    lanes: list[Lane] = []
    only_one = len([g for g in groups if not g[1]]) == 1
    for bands, dark, days, is_today in groups:
        label = _days_label(days) if not only_one or dark else "Every day"
        if dark:
            label = "Dark Day" if only_one else f"{label} · Dark Day"
        note = "" if bands is not None else "Runs when it's started (a starter, a button or the lights switched on), until it's switched off."
        lanes.append(Lane("base", label, bands or (), note, tuple(sorted(days)), dark, is_today))

    today = facts.today
    workday = facts.workday_today if facts.workday_today is not None else today.weekday() < 5
    if config.runs_ambient and config.has_someone_looks:
        lanes.insert(0, Lane("someone", "Someone's here",
                             _bands(config, schedule, today, facts, workday, False, someone=True) or ()))

    for key, items in (("rule", list(enumerate(config.rules))), ("house_rule", list(house_rules))):
        for index, rule in items:
            who = name(rule.when.entity)
            label = rule.name or f"While {_while(rule.when, who)}"
            bands = _bands(config, schedule, today, facts, workday, False, rule=rule)
            note = "Does nothing while this holds: no lights come on." if rule.action == RULE_NOTHING else ""
            if bands is None:
                bands, note = (), "Runs when it's started, with this rule's look."
            lanes.append(Lane(key, label, bands, note, index=index))

    for index, signal in enumerate(config.signals):
        who = name(signal.when.entity)
        label = signal.name or f"While {_while(signal.when, who)}"
        lights = ", ".join(name(light) for light in signal.lights)
        lanes.append(Lane("inform", label, (), f"Whenever {_while(signal.when, who)}: {lights}", index=index))
    return lanes
