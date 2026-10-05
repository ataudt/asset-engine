"""Flyer configuration — the printed page, its layout in millimetres, links and campaigns.

Layer: asset engine.
Rules:
  - Geometry lives in the project's flyer ``config.json``, in millimetres from the top-left corner
    of the trimmed page. ``Page`` — a ``printing.Sheet`` with the same bleed and margin on every
    edge and nothing hidden — is the only place a millimetre becomes a pixel.
  - A *campaign* is one print run. It is the unit that gets built, because the QR codes carry its
    name: two runs of the same German flyer are two campaigns with one copy file.
  - Copy is one file per language in the project's flyer copy directory (``printing.copy``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from asset_engine.printing.copy import read_copy as read_product_copy
from asset_engine.printing.links import Campaign, LinkSettings, parse_links
from asset_engine.printing.sheet import Edges, Sheet
from asset_engine.project import get_project
from asset_engine.store.config import overlay


class FlyerConfigError(RuntimeError):
    """The flyer cannot be built as configured."""


@dataclass(frozen=True)
class Page(Sheet):
    """A flyer page: the same bleed and safety margin on every edge, and nothing hidden."""

    @classmethod
    def of(cls, *, trim_mm: tuple[float, float], bleed_mm: float, safe_mm: float, dpi: int) -> Page:
        return cls(
            trim_mm=trim_mm,
            bleed_mm=Edges.uniform(bleed_mm),
            hidden_mm=Edges.uniform(0),
            safe_mm=Edges.uniform(safe_mm),
            dpi=dpi,
        )

    def at(self, mm: float) -> int:
        """A coordinate from the trim edge as a pixel coordinate on the bled canvas, either axis:
        the bleed is the same on every edge."""
        return self.x(mm)


@dataclass(frozen=True)
class FlyerConfig:
    page: Page
    ## Role -> the faces to pick from by coverage: the flyer's own first, then the store's list.
    fonts: dict[str, tuple[Path, ...]]
    badges_dir: Path
    default_out_dir: Path
    ## The layout blocks are kept as read: they are a tree of millimetre values with one reader
    ## each in ``compose``, and a dataclass per block would restate ``config.json`` field by field.
    wordmark: dict[str, Any]
    qr: dict[str, Any]
    text_page: dict[str, Any]
    image_page: dict[str, Any]
    links: LinkSettings
    campaigns: tuple[Campaign, ...]
    ## Design name -> the part of the layout it changes, in ``config.json`` order.
    designs: dict[str, dict[str, Any]]

    def for_design(self, name: str) -> FlyerConfig:
        """This configuration with one design's changes laid over the layout."""
        if name not in self.designs:
            raise FlyerConfigError(f"no design {name!r} in config.json — it has {list(self.designs)}")
        design = self.designs[name]
        unknown = set(design) - set(DESIGN_BLOCKS)
        if unknown:
            raise FlyerConfigError(
                f"design {name!r} changes {sorted(unknown)}; a design may only change {list(DESIGN_BLOCKS)}"
            )
        merged = replace(self, **{block: overlay(getattr(self, block), design[block]) for block in design})
        for page in ("text_page", "image_page"):
            if "background" not in getattr(merged, page):
                raise FlyerConfigError(f"design {name!r} names no background for {page}")
        return merged

    def campaign(self, name: str) -> Campaign:
        for campaign in self.campaigns:
            if campaign.name == name:
                return campaign
        raise FlyerConfigError(
            f"no campaign {name!r} in config.json — it has {[c.name for c in self.campaigns]}"
        )


## The layout blocks a design may change. Page size, links and campaigns are the print run's, not
## the look's, so a design cannot reach them.
DESIGN_BLOCKS = ("wordmark", "qr", "text_page", "image_page")


@lru_cache(maxsize=None)
def load_config(path: Path | None = None) -> FlyerConfig:
    project = get_project()
    source = path or project.flyer_files().config
    raw = json.loads(source.read_text(encoding="utf-8"))
    page = raw["page"]
    return FlyerConfig(
        page=Page.of(
            trim_mm=tuple(page["trim_mm"]),  # type: ignore[arg-type]
            bleed_mm=page["bleed_mm"],
            safe_mm=page["safe_mm"],
            dpi=page["dpi"],
        ),
        fonts={
            role: (project.fonts_dir / name, *project.font_priority)
            for role, name in raw["fonts"].items()
            if not role.startswith("_")
        },
        badges_dir=project.root / raw["badges_dir"],
        default_out_dir=project.root / raw["default_out_dir"],
        wordmark=raw["wordmark"],
        qr=raw["qr"],
        text_page=raw["text_page"],
        image_page=raw["image_page"],
        links=parse_links(raw["links"], source=source),
        campaigns=tuple(
            Campaign(name=name, language=entry["language"])
            for name, entry in raw["campaigns"].items()
            if not name.startswith("_")
        ),
        designs={name: entry for name, entry in raw["designs"].items() if not name.startswith("_")},
    )


def read_copy(language: str) -> dict[str, Any]:
    """The flyer's text for one language: paragraphs and tagline as lists of hard-broken lines."""
    return read_product_copy(get_project().flyer_files().copy_dir, language, product="flyer")


__all__ = [
    "Campaign",
    "FlyerConfig",
    "FlyerConfigError",
    "LinkSettings",
    "Page",
    "load_config",
    "read_copy",
]
