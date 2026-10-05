"""The printed sheet: its size in millimetres, what is cut off or hidden, and the pixel grid.

Layer: asset engine.
Rules:
  - A sheet is specified the way a print shop's data sheet states it: the trim size (Endformat),
    the bleed added around it (Beschnitt), the strips of the trimmed sheet that are printed but
    never seen (a roll-up's top rail and its cassette), and the safety margin content keeps from the
    visible edge (Sicherheitsabstand). Every one of them is per edge, because a roll-up's foot and
    head are nothing like its sides.
  - ``Sheet`` is the only place a millimetre becomes a pixel. Coordinates are measured from the
    top-left corner of the *trimmed* sheet: ``x(mm)`` and ``y(mm)`` add the bleed in front of it.
  - The canvas is the whole data format (trim plus bleed). Backgrounds run to its edge; what must
    be read stays inside ``safe_box``.
"""

from __future__ import annotations

from dataclasses import dataclass

MM_PER_INCH = 25.4


@dataclass(frozen=True)
class Edges:
    """One length per edge, in millimetres."""

    top: float
    right: float
    bottom: float
    left: float

    @classmethod
    def uniform(cls, mm: float) -> Edges:
        return cls(mm, mm, mm, mm)

    @classmethod
    def parse(cls, raw: float | dict) -> Edges:
        """A number for all four edges, or ``{"top": .., "right": .., "bottom": .., "left": ..}``."""
        if isinstance(raw, (int, float)):
            return cls.uniform(raw)
        return cls(raw["top"], raw["right"], raw["bottom"], raw["left"])


@dataclass(frozen=True)
class Sheet:
    """Trim size, bleed, hidden strips and safety margin, and the resolution it is drawn at."""

    trim_mm: tuple[float, float]
    bleed_mm: Edges
    hidden_mm: Edges
    safe_mm: Edges
    dpi: int

    def px(self, mm: float) -> int:
        """A length in millimetres as pixels."""
        return round(mm * self.dpi / MM_PER_INCH)

    def x(self, mm: float) -> int:
        """A horizontal coordinate from the trim edge as a pixel column on the bled canvas."""
        return self.px(mm + self.bleed_mm.left)

    def y(self, mm: float) -> int:
        """A vertical coordinate from the trim edge as a pixel row on the bled canvas."""
        return self.px(mm + self.bleed_mm.top)

    @property
    def canvas(self) -> tuple[int, int]:
        width, height = self.trim_mm
        return (
            self.px(width + self.bleed_mm.left + self.bleed_mm.right),
            self.px(height + self.bleed_mm.top + self.bleed_mm.bottom),
        )

    @property
    def trim_box(self) -> tuple[int, int, int, int]:
        return (self.x(0), self.y(0), self.x(self.trim_mm[0]), self.y(self.trim_mm[1]))

    @property
    def visible_mm(self) -> tuple[float, float, float, float]:
        """The part of the trimmed sheet anyone sees, as (left, top, right, bottom) in trim mm."""
        width, height = self.trim_mm
        hidden = self.hidden_mm
        return (hidden.left, hidden.top, width - hidden.right, height - hidden.bottom)

    @property
    def visible_box(self) -> tuple[int, int, int, int]:
        left, top, right, bottom = self.visible_mm
        return (self.x(left), self.y(top), self.x(right), self.y(bottom))

    @property
    def safe_box(self) -> tuple[int, int, int, int]:
        """What survives a cut that lands ``safe_mm`` off and is not hidden: type and codes stay in it."""
        left, top, right, bottom = self.visible_mm
        safe = self.safe_mm
        return (
            self.x(left + safe.left),
            self.y(top + safe.top),
            self.x(right - safe.right),
            self.y(bottom - safe.bottom),
        )


__all__ = ["MM_PER_INCH", "Edges", "Sheet"]
