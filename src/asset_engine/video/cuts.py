"""Which parts of a raw clip stay, and where a time of the raw clip lands in the cut.

Layer: asset engine.
Rules:
  - Every time a person writes down (cues, words, removals, the second clip's track) is the raw
    clip's. The cut is a list of kept segments on that clock (``Timeline``), and only the renderer
    moves a time onto the cut's clock, so no file needs rewriting when the cut moves.
  - A removal is written by a person (or by Claude in the session, from the transcript with times):
    which of two takes goes is a judgement, and no code makes it.
  - Pauses are shortened by rule: a silence longer than ``max_pause`` keeps ``max_pause``, half at
    each side, so neither neighbouring word loses its edge. A removal that touches silence takes the
    silence along and leaves the same pause behind, so a removed take does not leave two pauses.
    Silences less than ``JOIN`` apart are one pause: the level meter splits a pause at a click.
  - Segment edges sit on the frame grid, so picture and sound of a segment are the same length and
    do not drift apart over many cuts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import TypeVar

Span = tuple[float, float]
JOIN = 0.05
T = TypeVar("T")


class CutError(RuntimeError):
    """The cut cannot be read or leaves nothing."""


def parse_time(s: str) -> float:
    secs = 0.0
    for part in s.split(":"):
        secs = secs * 60 + float(part)
    return secs


def parse_cuts(path: Path) -> list[Span]:
    """The removals, one per line in the raw clip's time (``#`` starts a comment):

        00:12.3 - 00:18.9   # first take of the sentence
        31.0 bis 33.2
    """
    removed = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([\d:.]+)\s*(?:-|–|bis|until|\s)\s*([\d:.]+)", line)
        if not m:
            raise CutError(f"{path}:{lineno}: expected START - END: {raw.strip()}")
        start, end = parse_time(m.group(1)), parse_time(m.group(2))
        if end <= start:
            raise CutError(f"{path}:{lineno}: ends before it starts")
        removed.append((start, end))
    return removed


def tighten(silences: list[Span], removed: list[Span], max_pause: float) -> list[Span]:
    """Everything that goes: ``removed``, and the inside of every pause longer than ``max_pause``."""
    tagged = sorted([(s, e, False) for s, e in silences] + [(s, e, True) for s, e in removed])
    runs: list[list] = []                       # start, end, first removal's start, last removal's end
    for start, end, manual in tagged:
        if runs and start <= runs[-1][1] + JOIN:
            run = runs[-1]
            run[1] = max(run[1], end)
        else:
            run = [start, end, None, None]
            runs.append(run)
        if manual:
            run[2] = start if run[2] is None else min(run[2], start)
            run[3] = end if run[3] is None else max(run[3], end)
    out = []
    for start, end, first, last in runs:
        if first is None:
            if end - start > max_pause:
                out.append((start + max_pause / 2, end - max_pause / 2))
        else:
            out.append((start + min(max_pause / 2, first - start), end - min(max_pause / 2, end - last)))
    return out


@dataclass(frozen=True)
class Timeline:
    """The kept segments of the raw clip, in order, on the raw clip's clock."""

    segments: tuple[Span, ...]

    @classmethod
    def of(cls, span: Span, removed: list[Span] = (), *, fps: int) -> "Timeline":
        """``span`` without ``removed``, edges on the frame grid; pieces under two frames are dropped."""
        t0, t1 = span
        kept, at = [], t0
        for start, end in sorted(removed):
            if start > at:
                kept.append((at, min(start, t1)))
            at = max(at, end)
        if at < t1:
            kept.append((at, t1))
        if len(kept) > 1 or removed:
            kept = [(round(a * fps) / fps, round(b * fps) / fps) for a, b in kept]
        kept = [(a, b) for a, b in kept if b - a >= 2 / fps]
        if not kept:
            raise CutError("the cut leaves nothing of the clip")
        return cls(tuple(kept))

    @property
    def duration(self) -> float:
        return sum(b - a for a, b in self.segments)

    def at(self, t: float) -> float:
        """Raw time on the cut's clock; a removed moment lands on the cut that replaced it."""
        offset = 0.0
        for a, b in self.segments:
            if t < b:
                return offset + max(0.0, t - a)
            offset += b - a
        return offset

    def clip(self, items: list[T]) -> list[T]:
        """The items (anything with ``start`` and ``end``) seen in the cut, on the cut's clock."""
        return [replace(i, start=self.at(i.start), end=self.at(i.end)) for i in items
                if any(i.end > a and i.start < b for a, b in self.segments)]


__all__ = ["CutError", "Span", "Timeline", "parse_cuts", "parse_time", "tighten"]
