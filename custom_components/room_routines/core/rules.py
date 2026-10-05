"""What else in the house a room listens to.

Three things, all built from one small piece, a ``Condition`` (an entity in a
state, or not in it):

- **starters**: the room's routine starts when one of these becomes true (a
  computer switching on, a door opening). Unlike a motion sensor, a starter going
  quiet again doesn't switch the room off.
- **only when**: every way of starting the room (motion, a starter, a timer,
  lights switched on by hand being taken over, the switch_on action) needs all
  of these to hold. "Not while the baby is asleep" is ``Condition(baby_bedtime,
  "on", negate=True)``.
- **rules**: while a condition holds, the room behaves differently: it does
  nothing (no starts at all), uses another look (a scene, or another period's
  look), or keeps every light at or below a brightness. Rules can belong to a
  room or to the house (for chosen rooms, or every room). The first active
  rule wins, the room's own before the house's.

A rule never strands a lit room: "do nothing" stops new starts, but a room
already lit still counts down and goes dark as usual.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

NOTHING = "nothing"
LOOK = "look"
CAP = "cap"
RULE_ACTIONS = (NOTHING, LOOK, CAP)

States = Mapping[str, str | None]


@dataclass(frozen=True)
class Condition:
    entity: str
    state: str = "on"
    negate: bool = False

    def __post_init__(self) -> None:
        if not self.entity:
            raise ValueError("a condition needs an entity")
        if not self.state:
            raise ValueError("a condition needs a state")

    def holds(self, states: States) -> bool:
        current = states.get(self.entity)
        if current is None or current in ("unavailable", "unknown"):
            # Not knowing is never "true": a starter doesn't fire and an "only
            # when" isn't met. A "not while" is met, so a missing sensor doesn't
            # lock a room out for good.
            return self.negate
        return (current == self.state) != self.negate


@dataclass(frozen=True)
class Rule:
    when: Condition
    action: str = NOTHING
    scene: str | None = None  # LOOK: turn on this scene
    period: str | None = None  # LOOK: use this period's look
    max_pct: float | None = None  # CAP: no light brighter than this
    rooms: tuple[str, ...] = ()  # house rules only: these rooms (none = every room)
    name: str = ""

    def __post_init__(self) -> None:
        if self.action not in RULE_ACTIONS:
            raise ValueError(f"unknown rule action {self.action!r}")
        if self.action == LOOK and not (self.scene or self.period):
            raise ValueError("a 'use another look' rule needs a scene or a period")
        if self.action == CAP and not (self.max_pct and 0 < self.max_pct <= 100):
            raise ValueError("a brightness rule needs a level between 1 and 100 %")

    def applies_to(self, room_id: str) -> bool:
        return not self.rooms or room_id in self.rooms


def first_unmet(conditions: Iterable[Condition], states: States) -> Condition | None:
    for condition in conditions:
        if not condition.holds(states):
            return condition
    return None


def active_rule(rules: Iterable[Rule], states: States) -> Rule | None:
    for rule in rules:
        if rule.when.holds(states):
            return rule
    return None


# ---- words -----------------------------------------------------------------------


def describe_condition(condition: Condition, name: str | None = None) -> str:
    who = name or condition.entity
    if condition.negate:
        return f"{who} isn't {condition.state}"
    return f"{who} is {condition.state}"


def describe_rule(rule: Rule, name: str | None = None) -> str:
    """'while Baby bedtime is on: Overnight look'"""
    who = name or rule.when.entity
    when = f"while {who} is{' not' if rule.when.negate else ''} {rule.when.state}"
    if rule.action == NOTHING:
        what = "do nothing"
    elif rule.action == CAP:
        what = f"no brighter than {rule.max_pct:g} %"
    elif rule.scene:
        what = f"scene {rule.scene}"
    else:
        what = f"{rule.period} look"
    return f"{when}: {what}"


# ---- storage ---------------------------------------------------------------------


def condition_from(data: Mapping[str, Any]) -> Condition:
    return Condition(
        entity=str(data["entity"]),
        state=str(data.get("state") or "on"),
        negate=bool(data.get("negate", False)),
    )


def condition_to(condition: Condition) -> dict[str, Any]:
    out: dict[str, Any] = {"entity": condition.entity, "state": condition.state}
    if condition.negate:
        out["negate"] = True
    return out


def rule_from(data: Mapping[str, Any]) -> Rule:
    max_pct = data.get("max_pct")
    return Rule(
        when=condition_from(data["when"]),
        action=str(data.get("action", NOTHING)),
        scene=data.get("scene") or None,
        period=data.get("period") or None,
        max_pct=float(max_pct) if max_pct not in (None, "") else None,
        rooms=tuple(str(r) for r in data.get("rooms") or ()),
        name=str(data.get("name") or ""),
    )


def rule_to(rule: Rule) -> dict[str, Any]:
    out: dict[str, Any] = {"when": condition_to(rule.when), "action": rule.action}
    if rule.action == LOOK:
        if rule.scene:
            out["scene"] = rule.scene
        else:
            out["period"] = rule.period
    if rule.action == CAP:
        out["max_pct"] = rule.max_pct
    if rule.rooms:
        out["rooms"] = list(rule.rooms)
    if rule.name:
        out["name"] = rule.name
    return out
