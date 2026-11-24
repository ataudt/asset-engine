"""The cut of a short video: where a raw time lands, and how much of a pause stays.

Every expected value is worked out by hand from the times in the test, not read off the code.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from asset_engine.video.cuts import Timeline, tighten


@dataclass(frozen=True)
class Item:
    start: float
    end: float


def test_a_time_after_two_removals_moves_forward_by_both() -> None:
    """A 20 s clip loses 4–6 s and 10–13 s. What was said at 15 s is then heard at 15 − 2 − 3 = 10 s,
    and the cut is 20 − 5 = 15 s long. A caption timed against the raw clip that ignored the second
    removal would run three seconds late from there on, which is how a wrong mapping shows."""
    timeline = Timeline.of((0.0, 20.0), [(4.0, 6.0), (10.0, 13.0)], fps=30)

    assert timeline.duration == pytest.approx(15.0)
    assert timeline.at(15.0) == pytest.approx(10.0)
    assert timeline.at(8.0) == pytest.approx(6.0)
    ## A removed moment has no place of its own: it lands on the cut that replaced it.
    assert timeline.at(5.0) == pytest.approx(4.0)


def test_a_word_inside_a_removed_take_is_not_shown() -> None:
    """The first take of a sentence is removed; its words must not come back as captions over the
    second take. A word that only reaches into the removal keeps the part that is still heard."""
    timeline = Timeline.of((0.0, 20.0), [(4.0, 6.0)], fps=30)

    kept = timeline.clip([Item(1.0, 2.0), Item(4.5, 5.5), Item(3.5, 4.5), Item(7.0, 8.0)])

    assert [(round(i.start, 3), round(i.end, 3)) for i in kept] == [(1.0, 2.0), (3.5, 4.0), (5.0, 6.0)]


def test_a_long_pause_keeps_exactly_the_allowed_pause() -> None:
    """A 1 s silence with 0.4 s allowed loses its middle 0.6 s: 0.2 s stays at each side, so neither
    neighbouring word loses its edge. A silence of 0.3 s is not touched."""
    removed = tighten([(2.0, 3.0), (5.0, 5.3)], [], 0.4)

    assert removed == [pytest.approx((2.2, 2.8))]


def test_a_removed_take_leaves_one_pause_not_two() -> None:
    """A take is removed from 5.0 to 8.0 s, with silence before it (4.7–5.1) and after it (7.9–8.5).
    Cutting the take alone would leave 0.3 s + 0.5 s = 0.8 s of silence at the join. With 0.4 s
    allowed, 0.2 s stays at each side: the removal grows to 4.9–8.3."""
    removed = tighten([(4.7, 5.1), (7.9, 8.5)], [(5.0, 8.0)], 0.4)

    assert removed == [pytest.approx((4.9, 8.3))]
