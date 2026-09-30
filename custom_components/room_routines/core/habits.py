"""Learning from hand changes.

When someone changes lights a room switched on (dims them, picks another
scene, switches them off), the integration writes it down as a ``Change``.
When the same thing keeps happening in the same room, period and track, that
is a sign the look or the schedule is wrong, so ``suggest`` turns it into a
suggestion the page can offer:

- **Switched off soon after coming on**, again and again: keep the room dark in
  that period.
- **Changed to what the next period looks like**, near the end of the period:
  start the next period earlier. (Or to the previous period's look near the
  start of a period: start this period later.)
- **Changed some other way**: save the latest change as the look.

A suggestion needs ``min_count`` changes on at least ``min_days`` different days
within ``window``, so one odd evening suggests nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import median

from .looks import LightTarget, Look
from .periods import Schedule
from .room import ADJUSTED, SWITCHED_OFF
from .tracks import NORMAL

KEEP_DARK = "keep_dark"
SAVE_LOOK = "save_look"
START_EARLIER = "start_earlier"
START_LATER = "start_later"

QUICK_OFF = timedelta(minutes=2)  # switched off this soon after coming on = didn't want it
NEAR_EDGE = timedelta(minutes=90)  # "near the end / start of a period"
SIMILAR_PCT = 10.0  # brightness points either way that still count as the same look
SHARE = 0.6  # of the changes that must agree


@dataclass(frozen=True)
class Change:
    at: datetime
    room_id: str
    kind: str  # room.ADJUSTED or room.SWITCHED_OFF
    period: str
    track: str
    after_s: float  # since the room switched the lights on
    lights: Mapping[str, LightTarget] | None = None  # how they ended up (adjusted only)


@dataclass(frozen=True)
class Suggestion:
    key: str  # stable, so a dismissed suggestion stays dismissed
    room_id: str
    kind: str
    period: str
    track: str
    count: int
    days: int
    text: str
    look: Look | None = None  # SAVE_LOOK: the look to save
    move_period: str | None = None  # START_EARLIER / START_LATER: the period to move
    new_start: time | None = None


def similar(a: Mapping[str, LightTarget], b: Look) -> bool:
    """Whether lights ``a`` are the same as look ``b`` for every light ``b`` sets."""
    if b.nothing or b.scene or not b.lights:
        return False
    for light, want in b.lights.items():
        got = a.get(light)
        if got is None:
            continue
        if got.on != want.on:
            return False
        if want.on and want.brightness_pct is not None and got.brightness_pct is not None:
            if abs(got.brightness_pct - want.brightness_pct) > SIMILAR_PCT:
                return False
    return True


def _round(t: datetime, up: bool) -> time:
    minutes = t.hour * 60 + t.minute + (1 if t.second and up else 0)
    minutes = (minutes + (14 if up else 0)) // 15 * 15
    minutes = min(minutes, 23 * 60 + 45)
    return time(minutes // 60, minutes % 60)


def _median_time(times: list[datetime], up: bool) -> time:
    ordered = sorted(times, key=lambda t: (t.hour, t.minute, t.second))
    return _round(ordered[len(ordered) // 2], up)


def _days(changes: list[Change]) -> int:
    return len({c.at.date() for c in changes})


def _where(period: str, track: str) -> str:
    return f"{period}{' on Dim days' if track != NORMAL else ''}"


def suggest(
    changes: Iterable[Change],
    room_id: str,
    room_name: str,
    schedule: Schedule,
    look_of: Callable[[str, str], Look | None],
    now: datetime,
    window: timedelta = timedelta(days=28),
    min_count: int = 4,
    min_days: int = 3,
) -> list[Suggestion]:
    """Suggestions for one room. ``look_of(period, track)`` gives a period's look
    as lights (None where it can't be read, such as some scenes)."""
    recent = [c for c in changes if c.room_id == room_id and now - c.at <= window]
    groups: dict[tuple[str, str], list[Change]] = {}
    for c in recent:
        groups.setdefault((c.period, c.track), []).append(c)
    out: list[Suggestion] = []
    names = set(schedule.order())
    for (period, track), group in groups.items():
        if period not in names:
            continue
        where = _where(period, track)
        offs = [c for c in group if c.kind == SWITCHED_OFF and c.after_s <= QUICK_OFF.total_seconds()]
        if len(offs) >= min_count and _days(offs) >= min_days and len(offs) >= SHARE * len(group):
            out.append(Suggestion(
                f"{room_id}|{period}|{track}|{KEEP_DARK}", room_id, KEEP_DARK, period, track,
                len(offs), _days(offs),
                f"{room_name}'s lights were switched off by hand soon after coming on {len(offs)} times "
                f"on {_days(offs)} days in {where}. Keep the room dark then?",
            ))
            continue
        adjusted = [c for c in group if c.kind == ADJUSTED and c.lights]
        if len(adjusted) < min_count or _days(adjusted) < min_days:
            continue
        count, days = len(adjusted), _days(adjusted)
        nxt = schedule.current(adjusted[0].at).next_name
        nxt_look = look_of(nxt, track) if nxt != period else None
        prev = schedule.previous(period)
        prev_look = look_of(prev, track) if prev != period else None
        like_next = [c for c in adjusted if nxt_look and similar(c.lights, nxt_look)]
        like_prev = [c for c in adjusted if prev_look and similar(c.lights, prev_look)]
        late = [c for c in like_next if schedule.current(c.at).next_start - c.at <= NEAR_EDGE]
        early = [c for c in like_prev if c.at - schedule.current(c.at).started <= NEAR_EDGE]
        if len(late) >= SHARE * count:
            start = _median_time([c.at for c in late], up=False)
            out.append(Suggestion(
                f"{room_id}|{period}|{track}|{START_EARLIER}", room_id, START_EARLIER, period, track, count, days,
                f"In {room_name} the lights were changed to the {nxt} look {count} times on {days} days, "
                f"near the end of {where}. Start {nxt} at {start.strftime('%H:%M')}?",
                move_period=nxt, new_start=start,
            ))
            continue
        if len(early) >= SHARE * count:
            start = _median_time([c.at for c in early], up=True)
            out.append(Suggestion(
                f"{room_id}|{period}|{track}|{START_LATER}", room_id, START_LATER, period, track, count, days,
                f"In {room_name} the lights were changed back to the {prev} look {count} times on {days} days, "
                f"soon after {period} started. Start {period} at {start.strftime('%H:%M')}?",
                move_period=period, new_start=start,
            ))
            continue
        latest = max(adjusted, key=lambda c: c.at)
        current = look_of(period, track)
        if current is not None and similar(latest.lights, current):
            continue  # already the look: nothing to suggest
        out.append(Suggestion(
            f"{room_id}|{period}|{track}|{SAVE_LOOK}", room_id, SAVE_LOOK, period, track, count, days,
            f"{room_name}'s lights were changed by hand {count} times on {days} days in {where}. "
            f"Save the latest change as the {period}{' Dim' if track != NORMAL else ''} look?",
            look=Look(dict(latest.lights)),
        ))
    out.sort(key=lambda s: -s.count)
    return out


# ---- storage -------------------------------------------------------------------


def change_to(c: Change) -> dict:
    from .serial import target_to

    out = {
        "at": c.at.isoformat(), "room_id": c.room_id, "kind": c.kind,
        "period": c.period, "track": c.track, "after_s": round(c.after_s, 1),
    }
    if c.lights is not None:
        out["lights"] = {light: target_to(t) for light, t in c.lights.items()}
    return out


def change_from(data: Mapping) -> Change:
    from .serial import target_from

    lights = data.get("lights")
    return Change(
        datetime.fromisoformat(data["at"]), data["room_id"], data["kind"], data["period"],
        data.get("track", NORMAL), float(data.get("after_s", 0)),
        {light: target_from(t) for light, t in lights.items()} if lights is not None else None,
    )
