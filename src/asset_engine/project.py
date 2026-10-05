"""The app an asset run belongs to: where its files are, and what only the app knows.

Layer: asset engine.
Rules:
  - The engine never derives a path from where its own code sits. Every relative path in the JSON
    resolves against ``AssetProject.root``, and the JSON files themselves are named here, because
    the code is shared between apps and the definitions are not.
  - What the engine cannot know is asked of the project, never imported from the app: the locale
    set, the brand faces, the words a chip prints and the store pages (``PageRegistry``).
  - Each product is a section an app may leave out — an app with no store set has ``store=None``.
    Asking for a section the app did not give is an error naming it (``store_files()``,
    ``flyer_files()``, ``rollup_files()``), never a guess; the fields themselves are ``None``.
  - One project per process, installed at startup by ``set_project``; ``get_project`` raises until
    then rather than guessing one. The app's own package installs it, so every entry into the engine
    from that app passes through it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class PageRegistry:
    """The store pages an app publishes: its own listing and any Apple custom product pages.

    This base is an app with one page and nothing layered on it. An app with custom pages
    subclasses it; its answers come from wherever that app keeps the page list.
    """

    def default(self) -> str:
        """The slug of the app's own set — the one folder name every tree uses for it."""
        return "default"

    def slugs(self, *, all_layers: bool = False) -> list[str]:
        """Every custom page, without the default. ``all_layers`` is every layer with a tree of its
        own: the default too, and any layer no page is built from but other pages resolve through.
        It is what a design's name is checked against, so a design cannot write into one."""
        return [self.default()] if all_layers else []

    def check_slug(self, slug: str) -> str:
        """``slug`` if it is a page that may be built, else ``ValueError`` naming the ones that may."""
        raise ValueError(f"no store page {slug!r}: this app has only {self.default()!r}")

    def is_custom(self, slug: str) -> bool:
        return slug != self.default()

    def locales(self, slug: str) -> list[str]:
        """The locales that carry copy for a custom page."""
        return []

    def overlay_layers(self, slug: str) -> list[str]:
        """A page's layers, lowest precedence first, without the default listing at their foot."""
        return []


@dataclass(frozen=True)
class StoreFiles:
    """The store set: geometry, the caption -> capture map, page artwork and the dishes a frame may
    show. ``dishes`` may name a file holding ``{}`` for an app whose frames show no food."""

    screenshots_config: Path
    captures: Path
    variants: Path
    dishes: Path
    feature_graphic: Path
    ## ``language -> nested table`` a chip's name is looked up in: a dotted key walks it one level
    ## per part, as in NutriSpy's ``frontend/locales/<lang>/data.json``.
    labels: Callable[[str], dict]


@dataclass(frozen=True)
class PrintFiles:
    """A printed product: its layout file and its copy, one JSON file per language."""

    config: Path
    copy_dir: Path


@dataclass(frozen=True)
class AssetProject:
    """Everything an app hands the engine. Paths are absolute."""

    root: Path
    ## The brand faces, highest priority first; the first whose cmap covers a text is the one used.
    fonts_dir: Path
    font_priority: tuple[Path, ...]
    ## The locales the app ships in, and the hand-written one the others are measured against.
    locales: tuple[str, ...]
    source_locale: str
    pages: PageRegistry
    ## The products, each ``None`` for an app that has none.
    store: StoreFiles | None = None
    flyers: PrintFiles | None = None
    rollups: PrintFiles | None = None

    def store_files(self) -> StoreFiles:
        return _required(self.store, "store")

    def flyer_files(self) -> PrintFiles:
        return _required(self.flyers, "flyers")

    def rollup_files(self) -> PrintFiles:
        return _required(self.rollups, "rollups")


def _required(section, name: str):
    if section is None:
        raise RuntimeError(f"the installed asset project has no {name!r} section — the app's adapter gives none")
    return section


_PROJECT: AssetProject | None = None


def set_project(project: AssetProject) -> None:
    """Install the project for this process."""
    global _PROJECT
    _PROJECT = project


def get_project() -> AssetProject:
    if _PROJECT is None:
        raise RuntimeError(
            "no asset project installed — the app's package calls set_project() before it uses "
            "the engine"
        )
    return _PROJECT


__all__ = ["AssetProject", "PageRegistry", "PrintFiles", "StoreFiles", "get_project", "set_project"]
