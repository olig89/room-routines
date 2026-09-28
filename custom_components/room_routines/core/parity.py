"""The dry-run check: what a room in log-only would have done, against what its lights did.

While a room runs in log-only, the sensor's own direct link still switches the
light. Each time the room *would* have switched its lights on or off is paired
with the nearest real switch in the same direction. A pair within the
tolerance is a match; anything left over is a difference worth reading.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_TOLERANCE = timedelta(seconds=20)


@dataclass(frozen=True)
class Edge:
    """A switch-on (``on=True``) or switch-off at ``at``."""

    at: datetime
    on: bool


@dataclass(frozen=True)
class Pair:
    on: bool
    routine_at: datetime | None  # when the room would have switched
    light_at: datetime | None  # when the light really switched

    @property
    def matched(self) -> bool:
        return self.routine_at is not None and self.light_at is not None

    @property
    def delta_s(self) -> float | None:
        if not self.matched:
            return None
        return round((self.light_at - self.routine_at).total_seconds(), 1)  # type: ignore[operator]

    @property
    def at(self) -> datetime:
        return self.routine_at or self.light_at  # type: ignore[return-value]


@dataclass(frozen=True)
class Parity:
    pairs: tuple[Pair, ...]

    @property
    def matched(self) -> int:
        return sum(p.matched for p in self.pairs)

    @property
    def total(self) -> int:
        return len(self.pairs)


def edges(timeline: Iterable[tuple[datetime, bool | None]]) -> list[Edge]:
    """Changes in a timeline of (time, on). ``None`` (unknown) keeps the last value.

    The first known value is the starting point, not a change.
    """
    out: list[Edge] = []
    last: bool | None = None
    for at, on in sorted(timeline, key=lambda row: row[0]):
        if on is None:
            continue
        if last is not None and on != last:
            out.append(Edge(at, on))
        last = on
    return out


def any_on(timelines: Mapping[str, Sequence[tuple[datetime, bool | None]]]) -> list[tuple[datetime, bool]]:
    """Several lights merged into one timeline: on while any of them is on."""
    events = sorted(
        ((at, light, on) for light, rows in timelines.items() for at, on in rows),
        key=lambda row: row[0],
    )
    current: dict[str, bool] = {}
    out: list[tuple[datetime, bool]] = []
    for at, light, on in events:
        if on is None:
            continue
        current[light] = on
        out.append((at, any(current.values())))
    return out


def within(edges_: Iterable[Edge], windows: Sequence[tuple[datetime, datetime]]) -> list[Edge]:
    """Only the edges that fall inside one of the windows (e.g. while in log-only)."""
    return [e for e in edges_ if any(start <= e.at <= end for start, end in windows)]


def windows(timeline: Iterable[tuple[datetime, str | None]], value: str, until: datetime) -> list[tuple[datetime, datetime]]:
    """The spans during which a timeline (e.g. a room's mode) held ``value``."""
    out: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    for at, state in sorted(timeline, key=lambda row: row[0]):
        if state == value and start is None:
            start = at
        elif state != value and start is not None:
            out.append((start, at))
            start = None
    if start is not None:
        out.append((start, until))
    return out


def compare(routine: Sequence[Edge], light: Sequence[Edge], tolerance: timedelta = DEFAULT_TOLERANCE) -> Parity:
    """Pair each would-have switch with the nearest real one in the same direction."""
    unused = list(light)
    pairs: list[Pair] = []
    for edge in sorted(routine, key=lambda e: e.at):
        best: Edge | None = None
        for candidate in unused:
            if candidate.on != edge.on or abs(candidate.at - edge.at) > tolerance:
                continue
            if best is None or abs(candidate.at - edge.at) < abs(best.at - edge.at):
                best = candidate
        if best is None:
            pairs.append(Pair(edge.on, edge.at, None))
        else:
            unused.remove(best)
            pairs.append(Pair(edge.on, edge.at, best.at))
    pairs.extend(Pair(e.on, None, e.at) for e in unused)
    return Parity(tuple(sorted(pairs, key=lambda p: p.at)))
