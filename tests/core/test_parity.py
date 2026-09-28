"""The dry-run check, told as a morning in the small bathroom."""

from datetime import timedelta

from custom_components.room_routines.core.parity import any_on, compare, edges, windows, within

from .conftest import at


def test_edges_skip_the_starting_value_and_unknowns():
    rows = [(at(28, 7), False), (at(28, 7, 1), None), (at(28, 7, 2), True), (at(28, 7, 3), True), (at(28, 7, 5), False)]
    assert [(e.at, e.on) for e in edges(rows)] == [(at(28, 7, 2), True), (at(28, 7, 5), False)]


def test_any_on_merges_lights():
    merged = any_on({
        "light.a": [(at(28, 7), False), (at(28, 7, 1), True), (at(28, 7, 4), False)],
        "light.b": [(at(28, 7), False), (at(28, 7, 2), True), (at(28, 7, 3), False)],
    })
    assert [(e.at, e.on) for e in edges(merged)] == [(at(28, 7, 1), True), (at(28, 7, 4), False)]


def test_a_visit_that_matches():
    routine = edges([(at(28, 7), False), (at(28, 7, 10), True), (at(28, 7, 15), False)])
    light = edges([(at(28, 7), False), (at(28, 7, 10, 1), True), (at(28, 7, 15, 4), False)])
    result = compare(routine, light)
    assert result.matched == result.total == 2
    assert [p.delta_s for p in result.pairs] == [1.0, 4.0]


def test_too_far_apart_is_two_differences():
    routine = edges([(at(28, 7), False), (at(28, 7, 10), True)])
    light = edges([(at(28, 7), False), (at(28, 7, 11), True)])  # a minute later
    result = compare(routine, light, tolerance=timedelta(seconds=20))
    assert result.matched == 0 and result.total == 2
    assert [(p.routine_at is None, p.light_at is None) for p in result.pairs] == [(False, True), (True, False)]


def test_a_daylight_visit_the_room_would_skip_but_someone_switched_on():
    routine: list = []
    light = edges([(at(28, 12), False), (at(28, 12, 1), True), (at(28, 12, 3), False)])
    result = compare(routine, light)
    assert result.matched == 0 and result.total == 2


def test_only_edges_while_in_log_only_count():
    mode = [(at(28, 6), "off"), (at(28, 7), "log_only"), (at(28, 9), "live")]
    spans = windows(mode, "log_only", until=at(28, 12))
    assert spans == [(at(28, 7), at(28, 9))]
    kept = within(edges([(at(28, 6), False), (at(28, 6, 30), True), (at(28, 7, 30), False)]), spans)
    assert [(e.at, e.on) for e in kept] == [(at(28, 7, 30), False)]


def test_nearest_real_switch_wins():
    routine = edges([(at(28, 7), False), (at(28, 7, 10), True)])
    light = edges([(at(28, 7), False), (at(28, 7, 9, 50), True), (at(28, 7, 9, 55), False), (at(28, 7, 10, 2), True)])
    result = compare(routine, light)
    matched = [p for p in result.pairs if p.matched]
    assert matched[0].light_at == at(28, 7, 10, 2)
