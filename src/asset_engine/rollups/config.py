"""Roll-up configuration — the print format, the layout as a list of elements, links and campaigns.

Layer: asset engine.
Rules:
  - The format is the print shop's, named by key from ``printers.json``; nothing here spells a size.
  - Vertical positions are millimetres **above the floor of the visible area** — the bottom edge of
    what is seen once the banner is pulled out of its cassette. That is how a roll-up is read and
    talked about (eye level, the band a table hides), and it is what lets ``rules`` check zones
    directly. Horizontal positions are millimetres from the left trim edge.
  - The layout is an ordered list of elements; later elements are drawn over earlier ones. Each has
    an ``id`` and a ``type`` (``compose.ELEMENT_TYPES``), and text elements a ``role`` the rules read
    (``headline``, ``body``, ``cta``, ``url``). Colours are RGB lists or names from ``palette``.
  - A *design* is the same roll-up in another look: it may change ``palette`` and ``fonts``, and
    reach into an element by its ``id`` (``{"elements": {"photo": {"asset": ...}}}``). The format,
    the order of the elements, the links and the campaigns are the print run's.
  - Copy is one file per language in the project's roll-up copy directory (``printing.copy``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from asset_engine.printing.copy import read_copy as read_product_copy
from asset_engine.printing.links import Campaign, LinkSettings, parse_links
from asset_engine.printing.sheet import Edges, Sheet
from asset_engine.project import get_project
from asset_engine.store.config import overlay

PRINTERS_PATH = Path(__file__).resolve().parent / "printers.json"

## What a design may change; everything else belongs to the print run.
DESIGN_BLOCKS = ("palette", "fonts", "elements")


class RollupConfigError(RuntimeError):
    """The roll-up cannot be built as configured."""


@lru_cache(maxsize=1)
def printers() -> dict[str, dict[str, Any]]:
    """Every print format the engine knows, by key."""
    raw = json.loads(PRINTERS_PATH.read_text(encoding="utf-8"))
    return {name: entry for name, entry in raw.items() if not name.startswith("_")}


def printer_sheet(name: str, dpi: int) -> Sheet:
    """The sheet of print format ``name`` at ``dpi``."""
    known = printers()
    if name not in known:
        raise RollupConfigError(f"no print format {name!r} — printers.json has {sorted(known)}")
    entry = known[name]
    return Sheet(
        trim_mm=tuple(entry["trim_mm"]),  # type: ignore[arg-type]
        bleed_mm=Edges.parse(entry["bleed_mm"]),
        hidden_mm=Edges.parse(entry["hidden_mm"]),
        safe_mm=Edges.parse(entry["safe_mm"]),
        dpi=dpi,
    )


@dataclass(frozen=True)
class RollupConfig:
    printer: str
    sheet: Sheet
    ## The distance the headline is read from, and the one the running text is read from, in metres.
    ## The rules size type by them.
    viewing_distance_m: float
    reading_distance_m: float
    palette: dict[str, list[int]]
    ## Role -> the faces to pick from by coverage: the roll-up's own first, then the project's list.
    fonts: dict[str, tuple[Path, ...]]
    default_out_dir: Path
    qr: dict[str, Any]
    elements: tuple[dict[str, Any], ...]
    links: LinkSettings
    campaigns: tuple[Campaign, ...]
    designs: dict[str, dict[str, Any]]

    def for_design(self, name: str) -> RollupConfig:
        """This configuration with one design's changes laid over it."""
        if name not in self.designs:
            raise RollupConfigError(f"no design {name!r} — the roll-up config has {list(self.designs)}")
        design = self.designs[name]
        unknown = sorted(set(design) - set(DESIGN_BLOCKS))
        if unknown:
            raise RollupConfigError(f"design {name!r} changes {unknown}; a design may only change {list(DESIGN_BLOCKS)}")
        by_id = {element["id"]: element for element in self.elements}
        stray = sorted(set(design.get("elements", {})) - set(by_id))
        if stray:
            raise RollupConfigError(f"design {name!r} changes elements {stray}, which the layout does not have")
        project = get_project()
        return replace(
            self,
            palette={**self.palette, **design.get("palette", {})},
            fonts={**self.fonts, **_fonts(design.get("fonts", {}), project)},
            elements=tuple(
                overlay(element, design.get("elements", {}).get(element["id"], {})) for element in self.elements
            ),
        )

    def campaign(self, name: str) -> Campaign:
        for campaign in self.campaigns:
            if campaign.name == name:
                return campaign
        raise RollupConfigError(f"no campaign {name!r} — the roll-up config has {[c.name for c in self.campaigns]}")

    def color(self, value: str | list[int]) -> tuple[int, int, int]:
        """An RGB list as given, or a name from the palette."""
        if isinstance(value, str):
            if value not in self.palette:
                raise RollupConfigError(f"no colour {value!r} in the palette — it has {sorted(self.palette)}")
            value = self.palette[value]
        return tuple(value)  # type: ignore[return-value]

    def floor_y(self, mm: float) -> int:
        """A height above the floor of the visible area as a pixel row on the bled canvas."""
        return self.sheet.y(self.sheet.visible_mm[3] - mm)


def _fonts(raw: dict[str, str], project) -> dict[str, tuple[Path, ...]]:
    return {
        role: (project.fonts_dir / name, *project.font_priority) for role, name in raw.items() if not role.startswith("_")
    }


@lru_cache(maxsize=None)
def load_config(path: Path | None = None) -> RollupConfig:
    project = get_project()
    source = path or project.rollup_files().config
    raw = json.loads(source.read_text(encoding="utf-8"))
    elements = tuple(raw["elements"])
    ids = [element["id"] for element in elements]
    duplicates = sorted({element_id for element_id in ids if ids.count(element_id) > 1})
    if duplicates:
        raise RollupConfigError(f"element ids {duplicates} are used twice; a design reaches an element by its id")
    return RollupConfig(
        printer=raw["printer"],
        sheet=printer_sheet(raw["printer"], raw["dpi"]),
        viewing_distance_m=raw["viewing_distance_m"],
        reading_distance_m=raw["reading_distance_m"],
        palette={name: value for name, value in raw["palette"].items() if not name.startswith("_")},
        fonts=_fonts(raw["fonts"], project),
        default_out_dir=project.root / raw["default_out_dir"],
        qr=raw["qr"],
        elements=elements,
        links=parse_links(raw["links"], source=source),
        campaigns=tuple(
            Campaign(name=name, language=entry["language"])
            for name, entry in raw["campaigns"].items()
            if not name.startswith("_")
        ),
        designs={name: entry for name, entry in raw["designs"].items() if not name.startswith("_")},
    )


def read_copy(language: str) -> dict[str, Any]:
    """The roll-up's text for one language."""
    return read_product_copy(get_project().rollup_files().copy_dir, language, product="roll-up")


__all__ = [
    "DESIGN_BLOCKS",
    "PRINTERS_PATH",
    "RollupConfig",
    "RollupConfigError",
    "load_config",
    "printer_sheet",
    "printers",
    "read_copy",
]
