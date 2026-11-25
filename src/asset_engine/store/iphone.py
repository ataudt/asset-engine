"""What the App Store on an iPhone shows of the store assets, and how large their type arrives there.

Layer: asset engine.
Rules:
  - The figures are measurements (``app_store_iphone.json``, with the screenshots and the date), not
    Apple's specification; nothing here spells a size.
  - The iPhone is the measure: where a placement has an iPhone view, its key art is judged against
    that window, not against Apple's art safe area, which also keeps what a Mac would crop.
  - Type smaller than ``min_points()`` on the phone is a finding, never a refusal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

Box = tuple[int, int, int, int]

IPHONE_PATH = Path(__file__).resolve().parent / "app_store_iphone.json"


@lru_cache(maxsize=1)
def _raw() -> dict:
    return json.loads(IPHONE_PATH.read_text(encoding="utf-8"))


def min_points() -> float:
    """The smallest type, in points on the phone, that Apple's guidelines allow."""
    return float(_raw()["min_points"])


@dataclass(frozen=True)
class IphoneView:
    """The middle of a creative an iPhone shows, and what the App Store lays over it."""

    aspect: tuple[int, int]
    measured: tuple[int, int]
    points: float
    overlays: dict[str, Box]

    def window(self, canvas: tuple[int, int]) -> Box:
        """The part of a canvas of ``canvas`` the phone shows: its middle at ``aspect``."""
        width, height = canvas
        if width * self.aspect[1] > height * self.aspect[0]:
            shown = height * self.aspect[0] / self.aspect[1]
            left = (width - shown) / 2
            return (round(left), 0, round(left + shown), height)
        shown = width * self.aspect[1] / self.aspect[0]
        top = (height - shown) / 2
        return (0, round(top), width, round(top + shown))

    def overlay_boxes(self, canvas: tuple[int, int]) -> dict[str, Box]:
        """The overlays in canvas pixels."""
        left, top, right, bottom = self.window(canvas)
        sx = (right - left) / self.measured[0]
        sy = (bottom - top) / self.measured[1]
        return {
            name: (round(left + x0 * sx), round(top + y0 * sy), round(left + x1 * sx), round(top + y1 * sy))
            for name, (x0, y0, x1, y1) in self.overlays.items()
        }

    def points_per_pixel(self, canvas: tuple[int, int]) -> float:
        """How many points on the phone one canvas pixel becomes."""
        left, _, right, _ = self.window(canvas)
        return self.points / (right - left)


def iphone_view(placement: str) -> IphoneView | None:
    """The measured iPhone view of a creative placement, or ``None`` where nobody has measured one."""
    entry = _raw().get(placement)
    if not isinstance(entry, dict) or "aspect" not in entry:
        return None
    return IphoneView(
        aspect=tuple(entry["aspect"]),  # type: ignore[arg-type]
        measured=tuple(entry["measured"]),  # type: ignore[arg-type]
        points=float(entry["points"]),
        overlays={name: tuple(box) for name, box in entry["overlays"].items()},  # type: ignore[misc]
    )


def gallery_points_per_pixel(frame_width: int) -> float:
    """How many points one pixel of a portrait screenshot of ``frame_width`` becomes in the gallery."""
    return float(_raw()["screenshots"]["frame_points"]) / frame_width


def too_small(size_px: int, points_per_pixel: float) -> float | None:
    """The size in points if type of ``size_px`` arrives below ``min_points()``, else ``None``."""
    points = size_px * points_per_pixel
    return points if points < min_points() else None


__all__ = ["IPHONE_PATH", "IphoneView", "gallery_points_per_pixel", "iphone_view", "min_points", "too_small"]
