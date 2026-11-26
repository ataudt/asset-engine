"""The app an asset run belongs to: where its files are, and what only the app knows.

Layer: asset engine.
Rules:
  - The engine never derives a path from where its own code sits. Every relative path in the JSON
    resolves against ``AssetProject.root``, and the JSON files themselves are named here, because
    the code is shared between apps and the definitions are not.
  - What the engine cannot know is asked of the project, never imported from the app: the locale
    set, the brand faces, the words a chip prints and the store pages (``PageRegistry``).
  - The engine holds no credentials and reads no ``.env``. A model client or a store key is built by
    the app, from its own secrets, and handed over as a callable (``LogoFiles.google_client``,
    ``PublishFiles.apple_key`` / ``play_key``).
  - Each product is a section an app may leave out — an app with no store set has ``store=None``.
    Asking for a section the app did not give is an error naming it (``store_files()``,
    ``flyer_files()``, ``rollup_files()``, ``banner_files()``, ``logo_files()``, ``publish_files()``), never a guess; the fields themselves are ``None``.
  - One project per process, installed at startup by ``set_project``; ``get_project`` raises until
    then rather than guessing one. The app's own package installs it, so every entry into the engine
    from that app passes through it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    show. ``dishes`` may name a file holding ``{}`` for an app whose frames show no food. The
    feature graphic and the App Store creatives stand on the same pieces."""

    screenshots_config: Path
    captures: Path
    variants: Path
    dishes: Path
    feature_graphic: Path
    ## ``language -> nested table`` a chip's name is looked up in: a dotted key walks it one level
    ## per part, as in NutriSpy's ``frontend/locales/<lang>/data.json``.
    labels: Callable[[str], dict]
    ## The App Store creative assets' layout (``store.creatives``); ``None`` for an app without.
    creatives: Path | None = None

    def creatives_config(self) -> Path:
        return _required(self.creatives, "store.creatives")


@dataclass(frozen=True)
class PublishFiles:
    """Store publishing: where the listing source is, the store ids, and the keys.

    The listing source is one tree per app in a shared layout (``store.listing``): ``locales/<code>/
    <page>/listing.json`` and ``keywords.json``, ``locales.config.json`` (caps), ``update/apple.md``
    and ``update/google.md`` (What's New), ``store.json`` (territories, record settings) and
    ``subscriptions.json``, all under ``appstore_dir``. What differs between apps is a field here.
    """

    appstore_dir: Path
    ## App locale code -> the store's own tag ("de" -> "de-DE"). A locale missing from one map is not
    ## published to that store.
    apple_locales: dict[str, str]
    play_locales: dict[str, str]
    ## Locale code -> the support page Apple requires in every localization.
    support_url: Callable[[str], str]
    marketing_url: Callable[[str], str] | None = None
    apple_bundle_id: str | None = None
    play_package: str | None = None
    ## The keys, built by the app from its own config: Apple ``{"key_id", "issuer_id",
    ## "private_key"}``, Play the service account's JSON as a dict. The engine reads no config.
    apple_key: Callable[[], dict] | None = None
    play_key: Callable[[], dict] | None = None
    ## Listing fields an app requires beyond what the stores need (kcalculate's ``description.sources``).
    required_fields: tuple[str, ...] = ()
    ## Play image type -> the screenshot device whose frames fill it. Every listing carries phone and
    ## tablet on both stores, as every app here runs on tablets: the iPad set fills both of Play's
    ## tablet classes, which Play sizes by pixel bounds alone.
    play_screenshots: tuple[tuple[str, str], ...] = (
        ("phoneScreenshots", "phone"),
        ("tenInchScreenshots", "ipad"),
        ("sevenInchScreenshots", "ipad"),
    )
    ## App Store screenshot display type -> device. The iPad set is the phone layout on the
    ## 2048 × 2732 canvas (``devices.ipad``).
    apple_screenshots: tuple[tuple[str, str], ...] = (
        ("APP_IPHONE_65", "phone"),
        ("APP_IPAD_PRO_3GEN_129", "ipad"),
    )
    ## ``(language, page) -> listing dict`` for an app whose pages overlay one another (NutriSpy's
    ## custom pages); ``None`` reads ``locales/<language>/<page>/listing.json`` as it stands.
    listing_reader: Callable[[str, str], dict] | None = None
    ## Whether a locale is published at all (kcalculate parks locales whose search terms are not
    ## researched yet); ``None`` publishes every locale whose copy is complete.
    is_listed: Callable[[str], bool] | None = None
    ## The Play listing's 512 × 512 icon, which Play holds apart from the one in the build: the
    ## ``play-store`` target of the app's logo config. ``None`` leaves the listing's icon as it is.
    play_icon: Path | None = None


@dataclass(frozen=True)
class PrintFiles:
    """A product laid out from one file, with its copy in one JSON file per language: a flyer, a
    roll-up, a profile banner."""

    config: Path
    copy_dir: Path


@dataclass(frozen=True)
class LogoFiles:
    """App icons: drafts drawn by an image model from a description, and the icon set derived from
    the draft the app chose as its master."""

    config: Path
    ## Vertex location (from the logo config) -> a ready ``google.genai.Client``. The app builds it
    ## from its own credentials; ``Any`` so this module imports without the ``logos`` extra.
    google_client: Callable[[str], Any]


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
    banners: PrintFiles | None = None
    logos: LogoFiles | None = None
    publish: PublishFiles | None = None

    def store_files(self) -> StoreFiles:
        return _required(self.store, "store")

    def flyer_files(self) -> PrintFiles:
        return _required(self.flyers, "flyers")

    def rollup_files(self) -> PrintFiles:
        return _required(self.rollups, "rollups")

    def banner_files(self) -> PrintFiles:
        return _required(self.banners, "banners")

    def logo_files(self) -> LogoFiles:
        return _required(self.logos, "logos")

    def publish_files(self) -> PublishFiles:
        return _required(self.publish, "publish")


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


__all__ = [
    "AssetProject",
    "LogoFiles",
    "PageRegistry",
    "PrintFiles",
    "PublishFiles",
    "StoreFiles",
    "get_project",
    "set_project",
]
