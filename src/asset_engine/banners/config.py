"""Banner configuration — the platform's format, and each banner as a list of elements.

Layer: asset engine.
Rules:
  - The format is the platform's, named by key from ``platforms.json``; nothing here spells a size.
  - Positions and sizes are pixels of the platform's canvas (LinkedIn: 1584 x 396), from its top-left.
    A banner's ``scale`` multiplies every one of them when drawing, so a banner is laid out once and
    rendered at twice the size for sharp screens without a second set of numbers.
  - The layout is an ordered list of elements; later elements are drawn over earlier ones. Each has
    an ``id`` and a ``type`` (``compose.ELEMENT_TYPES``); one with ``role: "key"`` must survive the
    platform's photo and crops, and the rules check it. Colours are RGB lists or names from ``palette``.
  - Copy is one file per language in the project's banner copy directory (``printing.copy``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from asset_engine.printing.copy import read_copy as read_product_copy
from asset_engine.project import get_project

PLATFORMS_PATH = Path(__file__).resolve().parent / "platforms.json"

Box = tuple[int, int, int, int]


class BannerConfigError(RuntimeError):
    """The banner cannot be built as configured."""


@dataclass(frozen=True)
class Zone:
    """A part of the canvas the platform covers (``keep-out``) or the part that survives its
    narrowest crop (``keep-in``)."""

    name: str
    kind: str
    box: Box


@dataclass(frozen=True)
class Photo:
    """The round profile photo as one view shows it over the banner, for the guides preview."""

    center: tuple[int, int]
    diameter: int


@dataclass(frozen=True)
class Platform:
    name: str
    canvas: tuple[int, int]
    zones: tuple[Zone, ...]
    photos: dict[str, Photo]
    ## The keep-in zone a phone shows of the banner.
    mobile_view: str

    def zone(self, name: str) -> Zone:
        for zone in self.zones:
            if zone.name == name:
                return zone
        raise BannerConfigError(f"platform {self.name!r} has no zone {name!r}")


@lru_cache(maxsize=1)
def platforms() -> dict[str, dict[str, Any]]:
    """Every banner format the engine knows, by key."""
    raw = json.loads(PLATFORMS_PATH.read_text(encoding="utf-8"))
    return {name: entry for name, entry in raw.items() if not name.startswith("_")}


def platform(name: str) -> Platform:
    known = platforms()
    if name not in known:
        raise BannerConfigError(f"no banner platform {name!r} — platforms.json has {sorted(known)}")
    entry = known[name]
    return Platform(
        name=name,
        canvas=tuple(entry["canvas"]),  # type: ignore[arg-type]
        zones=tuple(Zone(zone_name, zone["kind"], tuple(zone["box"])) for zone_name, zone in entry["zones"].items()),  # type: ignore[arg-type]
        photos={view: Photo(tuple(photo["center"]), photo["diameter"]) for view, photo in entry["photos"].items()},  # type: ignore[arg-type]
        mobile_view=entry["mobile_view"],
    )


@dataclass(frozen=True)
class Banner:
    id: str
    platform: Platform
    language: str
    scale: int
    elements: tuple[dict[str, Any], ...]

    @property
    def size(self) -> tuple[int, int]:
        """The rendered image's size: the platform's canvas times ``scale``."""
        return (self.platform.canvas[0] * self.scale, self.platform.canvas[1] * self.scale)


@dataclass(frozen=True)
class BannerConfig:
    palette: dict[str, list[int]]
    ## Role -> the faces to pick from by coverage: the banner's own first, then the project's list.
    fonts: dict[str, tuple[Path, ...]]
    default_out_dir: Path
    banners: dict[str, Banner]

    def banner(self, name: str) -> Banner:
        if name not in self.banners:
            raise BannerConfigError(f"no banner {name!r} — the banner config has {list(self.banners)}")
        return self.banners[name]

    def color(self, value: str | list[int]) -> tuple[int, int, int]:
        """An RGB list as given, or a name from the palette."""
        if isinstance(value, str):
            if value not in self.palette:
                raise BannerConfigError(f"no colour {value!r} in the palette — it has {sorted(self.palette)}")
            value = self.palette[value]
        return tuple(value)  # type: ignore[return-value]


def _banner(name: str, raw: dict[str, Any]) -> Banner:
    elements = tuple(raw["elements"])
    ids = [element["id"] for element in elements]
    duplicates = sorted({element_id for element_id in ids if ids.count(element_id) > 1})
    if duplicates:
        raise BannerConfigError(f"banner {name!r} uses element ids {duplicates} twice")
    return Banner(
        id=name,
        platform=platform(raw["platform"]),
        language=raw["language"],
        scale=raw.get("scale", 1),
        elements=elements,
    )


@lru_cache(maxsize=None)
def load_config(path: Path | None = None) -> BannerConfig:
    project = get_project()
    source = path or project.banner_files().config
    raw = json.loads(source.read_text(encoding="utf-8"))
    return BannerConfig(
        palette={name: value for name, value in raw["palette"].items() if not name.startswith("_")},
        fonts={
            role: (project.fonts_dir / face, *project.font_priority)
            for role, face in raw["fonts"].items()
            if not role.startswith("_")
        },
        default_out_dir=project.root / raw["default_out_dir"],
        banners={name: _banner(name, entry) for name, entry in raw["banners"].items() if not name.startswith("_")},
    )


def read_copy(language: str) -> dict[str, Any]:
    """The banners' text for one language."""
    return read_product_copy(get_project().banner_files().copy_dir, language, product="banner")


__all__ = [
    "PLATFORMS_PATH",
    "Banner",
    "BannerConfig",
    "BannerConfigError",
    "Photo",
    "Platform",
    "Zone",
    "load_config",
    "platform",
    "platforms",
    "read_copy",
]
