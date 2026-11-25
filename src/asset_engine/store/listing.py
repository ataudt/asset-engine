"""The store listing as the stores take it, assembled from an app's listing source.

Layer: asset engine, store publishing. Pure: reads files, writes nothing, talks to no store.
Rules:
  - One source per app, in one layout (``PublishFiles``): ``appstore/locales/<code>/<page>/
    listing.json`` holds the copy both stores share, ``keywords.json`` Apple's keyword field,
    ``update/apple.md`` and ``update/google.md`` What's New as ``<store tag>…</store tag>`` blocks,
    ``locales.config.json`` the caps. Each store gets a ``PlayListing`` / ``AppleListing`` per
    locale in memory; nothing is written to disk for an uploader to pick up.
  - A locale whose copy lacks a field its store needs raises ``ListingIncomplete`` and is skipped
    whole: a half-filled localization would blank fields on the store. A field over its cap raises
    ``CapExceeded``: it is a rejected edit, never a warning.
  - The long description is plain text, the same body for both stores (Play strips markdown, and it
    indexes the whole body): intro, the sections in file order (heading, then body), then the
    disclaimer and the data sources where an app has them, the two legal links, the support line.
    A value may be given per store, ``{"apple": …, "android": …}`` as the subtitle is, where a
    feature works differently on each platform.
  - Frames are found through the renderer's own config (``ScreenshotConfig.frame_path``), so a frame
    name is spelled in one place. A store's image class whose frames are not all rendered is an
    error for that locale when images were asked for: an upload would replace a full set with a
    partial one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re

from asset_engine.project import PublishFiles, get_project
from asset_engine.store.config import load_config


DEFAULT_PAGE = "default"

## Play's limit per image class and Apple's per display type.
MAX_PLAY_SCREENSHOTS = 8
MAX_APPLE_SCREENSHOTS = 10

## The description blocks every app's copy carries; ``disclaimer`` and ``sources`` are optional.
DESCRIPTION_FIELDS = (
    "description.intro",
    "description.sections",
    "description.termsLabel",
    "description.privacyLabel",
    "description.support",
    "links.terms",
    "links.privacy",
)
## What each store needs of a locale's copy before anything of it is published.
PLAY_FIELDS = ("title", "subtitle.android", *DESCRIPTION_FIELDS)
APPLE_FIELDS = ("title", "subtitle.apple", "promoText", *DESCRIPTION_FIELDS)
## The live App Store version takes nothing but the promotional text.
APPLE_PROMO_FIELDS = ("promoText",)

_BLOCK_RE = re.compile(
    r"^<(?P<tag>[a-z]{2,3}(?:-[A-Za-z0-9]{2,4})?)>\n(?P<body>.*?)\n</(?P=tag)>", re.MULTILINE | re.DOTALL
)


class ListingIncomplete(RuntimeError):
    """A locale's copy is not ready for a store — the locale is skipped, not partially written."""


class CapExceeded(RuntimeError):
    """A field is longer than the store accepts. Never a warning: it is a rejected edit."""


class FramesMissing(RuntimeError):
    """Images were asked for, and a store image class of a locale is not (fully) rendered."""


## -- the source ---------------------------------------------------------------------------------


def publish_files() -> PublishFiles:
    return get_project().publish_files()


def appstore_dir() -> Path:
    return publish_files().appstore_dir


def locales_config() -> dict:
    return json.loads((appstore_dir() / "locales.config.json").read_text(encoding="utf-8"))


def read_json(name: str) -> dict:
    """A file of the listing source by name (``store.json``, ``subscriptions.json``)."""
    return json.loads((appstore_dir() / name).read_text(encoding="utf-8"))


def read_tag_blocks(path: Path) -> dict[str, str]:
    """``{store tag: body}`` from a file of ``<tag>…</tag>`` blocks; prose outside them is ignored."""
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    return {match.group("tag"): match.group("body").strip() for match in _BLOCK_RE.finditer(text)}


def release_notes(store: str) -> dict[str, str]:
    """``{store tag: What's New}`` from ``update/apple.md`` or ``update/google.md``."""
    name = {"apple": "apple.md", "play": "google.md"}[store]
    return read_tag_blocks(appstore_dir() / "update" / name)


def lookup(listing: dict, dotted: str):
    node = listing
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def listing_path(language: str, page: str = DEFAULT_PAGE) -> Path:
    return appstore_dir() / "locales" / language / page / "listing.json"


def read_listing(language: str, page: str = DEFAULT_PAGE) -> dict:
    """A locale's copy for one page: the app's ``listing_reader`` where it layers pages, else the file."""
    files = publish_files()
    if files.listing_reader is not None:
        return files.listing_reader(language, page)
    path = listing_path(language, page)
    if not path.is_file():
        raise ListingIncomplete(f"no listing.json at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def require(listing: dict, fields: tuple[str, ...], *, language: str, page: str = DEFAULT_PAGE) -> None:
    missing = [dotted for dotted in fields if not lookup(listing, dotted)]
    if missing:
        raise ListingIncomplete(f"missing {', '.join(missing)} in {listing_path(language, page)}")


def read_keywords(language: str, page: str = DEFAULT_PAGE) -> str:
    """Apple's keyword field; ``""`` when the file is missing or empty."""
    path = appstore_dir() / "locales" / language / page / "keywords.json"
    if not path.is_file():
        return ""
    data = json.loads(path.read_text(encoding="utf-8"))
    return (data.get("keywords") or "").strip()


def store_fields(store: str) -> tuple[str, ...]:
    base = {"play": PLAY_FIELDS, "apple": APPLE_FIELDS}[store]
    return (*base, *publish_files().required_fields)


def is_listing_complete(language: str, store: str, page: str = DEFAULT_PAGE) -> bool:
    """Whether ``language`` carries every field ``store`` needs (and the app's own required ones)."""
    try:
        require(read_listing(language, page), store_fields(store), language=language, page=page)
    except ListingIncomplete:
        return False
    return True


def store_locales(store: str) -> dict[str, str]:
    files = publish_files()
    return dict(files.play_locales if store == "play" else files.apple_locales)


def listed_languages(store: str) -> list[str]:
    """The locales a full run publishes to ``store``: listed by the app and complete for that store."""
    files = publish_files()
    return [
        language
        for language in store_locales(store)
        if (files.is_listed is None or files.is_listed(language)) and is_listing_complete(language, store)
    ]


## The key a store's own wording carries in listing.json, as ``subtitle.apple`` / ``subtitle.android``.
LISTING_KEYS = {"apple": "apple", "play": "android"}


def for_store(value, store: str) -> str:
    """A copy value as ``store`` gets it: a plain string for both, or ``{"apple": …, "android": …}``
    where the stores need different words (a feature that works differently on each platform)."""
    if isinstance(value, dict):
        return value[LISTING_KEYS[store]]
    return value


def assemble_description(listing: dict, store: str) -> str:
    """The long description, a plain-text body: the same for both stores but where a section's heading
    or body is given per store (``for_store``).

    Section order is the file's order: the keys are stable identifiers, the sequence is the store
    sequence. ``disclaimer`` and ``sources`` appear where an app's copy has them (NutriSpy a
    disclaimer, kcalculate both: its nutrition data's licences ask for attribution on the store page).
    """
    description = listing["description"]
    blocks: list[str] = [for_store(description["intro"], store).strip()]
    for section in description["sections"].values():
        heading = for_store(section["heading"], store).strip()
        body = for_store(section["body"], store).strip()
        blocks.append(f"{heading}\n\n{body}")
    for optional in ("disclaimer", "sources"):
        if description.get(optional):
            blocks.append(for_store(description[optional], store).strip())
    links = listing["links"]
    blocks.append(
        f"{description['termsLabel'].strip()}: {links['terms']}\n"
        f"{description['privacyLabel'].strip()}: {links['privacy']}"
    )
    blocks.append(for_store(description["support"], store).strip())
    return "\n\n".join(blocks) + "\n"


def apple_localization_codes() -> frozenset[str]:
    """The localization codes App Store Connect accepts (``app_store_locales.json``, with its source)."""
    data = json.loads((Path(__file__).parent / "app_store_locales.json").read_text(encoding="utf-8"))
    return frozenset(data["codes"])


## -- caps ---------------------------------------------------------------------------------------


def play_caps() -> dict[str, int]:
    config = locales_config()
    shared, play = config["caps"], config["playCaps"]
    if "changelog" not in play:
        raise KeyError("locales.config.json playCaps has no `changelog` (Play's What's New limit, 500)")
    return {
        "title": shared["title"],
        "shortDescription": shared["subtitle.android"],
        "fullDescription": play["fullDescription"],
        "releaseNotes": play["changelog"],
    }


def apple_caps() -> dict[str, int]:
    config = locales_config()
    shared, apple = config["caps"], config["appStoreCaps"]
    return {
        "name": shared["title"],
        "subtitle": shared["subtitle.apple"],
        "promotionalText": shared["promoText"],
        "keywords": config["keywordsCap"],
        "description": apple["description"],
        "whatsNew": apple["releaseNotes"],
    }


def check_cap(store: str, name: str, value: str, caps: dict[str, int], *, tag: str) -> str:
    cap = caps[name]
    if len(value) > cap:
        raise CapExceeded(f"[{tag}] {store} {name}: {len(value)} chars exceeds the {cap}-char limit")
    return value


## -- frames -------------------------------------------------------------------------------------


def device_frames(language: str, device: str, page: str = DEFAULT_PAGE) -> list[Path]:
    """The locale's rendered frames for one device, in caption order. A caption not rendered is absent."""
    config = load_config(device) if page == DEFAULT_PAGE else load_config(device, variant=page)
    frames = {slot.caption: config.frame_path(language, slot.caption) for slot in config.slots}
    return [frames[caption] for caption in sorted(frames) if frames[caption].is_file()]


def _expected_frames(device: str) -> int:
    return len(load_config(device).slots)


def screenshot_sets(
    language: str, classes: tuple[tuple[str, str], ...], *, cap: int, store: str
) -> dict[str, list[Path]]:
    """``{image class: frames}`` for every class an app publishes; raises when one is incomplete."""
    sets: dict[str, list[Path]] = {}
    for image_class, device in classes:
        frames = device_frames(language, device)
        expected = min(_expected_frames(device), cap)
        if len(frames) < expected:
            raise FramesMissing(
                f"[{language}] {store} {image_class}: {len(frames)} of {expected} {device} frame(s) "
                f"rendered — run the screenshot build for this locale first"
            )
        sets[image_class] = frames[:cap]
    return sets


def feature_graphic(language: str) -> Path | None:
    path = load_config().feature_graphic_path(language)
    return path if path.is_file() else None


## -- the listings -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayListing:
    """One locale of the Play store listing, as the Edits API takes it."""

    language: str
    tag: str
    title: str
    short_description: str
    full_description: str
    ## ``None``: no What's New block for this tag, so the release notes are left as they are.
    release_notes: str | None
    ## Image type -> frames in order; empty when images were not asked for.
    images: dict[str, list[Path]] = field(default_factory=dict)


def play_listing(language: str, *, images: bool) -> PlayListing:
    """A locale's Play listing; raises ``ListingIncomplete`` / ``CapExceeded`` / ``FramesMissing``."""
    tag = store_locales("play")[language]
    listing = read_listing(language)
    require(listing, store_fields("play"), language=language)
    caps = play_caps()
    notes = release_notes("play").get(tag)
    image_sets: dict[str, list[Path]] = {}
    if images:
        image_sets = screenshot_sets(
            language, publish_files().play_screenshots, cap=MAX_PLAY_SCREENSHOTS, store="play"
        )
        graphic = feature_graphic(language)
        if graphic is None:
            raise FramesMissing(f"[{language}] play featureGraphic: not rendered — run `feature-graphic`")
        image_sets["featureGraphic"] = [graphic]
    return PlayListing(
        language=language,
        tag=tag,
        title=check_cap("play", "title", listing["title"].strip(), caps, tag=tag),
        short_description=check_cap(
            "play", "shortDescription", listing["subtitle"]["android"].strip(), caps, tag=tag
        ),
        full_description=check_cap(
            "play", "fullDescription", assemble_description(listing, "play").rstrip("\n"), caps, tag=tag
        ),
        release_notes=check_cap("play", "releaseNotes", notes, caps, tag=tag) if notes else None,
        images=image_sets,
    )


@dataclass(frozen=True)
class AppleListing:
    """One locale of the App Store listing: the version localization and the app-info localization."""

    language: str
    tag: str
    ## App info localization.
    name: str
    subtitle: str
    privacy_url: str
    ## Version localization.
    description: str
    keywords: str | None
    promotional_text: str
    support_url: str
    marketing_url: str | None
    whats_new: str | None
    ## Display type -> frames in order; empty when images were not asked for.
    screenshots: dict[str, list[Path]] = field(default_factory=dict)


def apple_listing(language: str, *, images: bool) -> AppleListing:
    """A locale's App Store listing; raises ``ListingIncomplete`` / ``CapExceeded`` / ``FramesMissing``."""
    files = publish_files()
    tag = store_locales("apple")[language]
    listing = read_listing(language)
    require(listing, store_fields("apple"), language=language)
    caps = apple_caps()
    keywords = read_keywords(language)
    notes = release_notes("apple").get(tag)
    return AppleListing(
        language=language,
        tag=tag,
        name=check_cap("apple", "name", listing["title"].strip(), caps, tag=tag),
        subtitle=check_cap("apple", "subtitle", listing["subtitle"]["apple"].strip(), caps, tag=tag),
        privacy_url=listing["links"]["privacy"].strip(),
        description=check_cap(
            "apple", "description", assemble_description(listing, "apple").rstrip("\n"), caps, tag=tag
        ),
        keywords=check_cap("apple", "keywords", keywords, caps, tag=tag) if keywords else None,
        promotional_text=check_cap("apple", "promotionalText", listing["promoText"].strip(), caps, tag=tag),
        support_url=files.support_url(language),
        marketing_url=files.marketing_url(language) if files.marketing_url else None,
        whats_new=check_cap("apple", "whatsNew", notes, caps, tag=tag) if notes else None,
        screenshots=(
            screenshot_sets(language, files.apple_screenshots, cap=MAX_APPLE_SCREENSHOTS, store="apple")
            if images
            else {}
        ),
    )


## -- custom pages ------------------------------------------------------------------------------


@dataclass(frozen=True)
class PageBundle:
    """One locale of an App Store custom product page: what a page may differ in from the main
    listing — its promotional text, its screenshots — and the words of the app's keyword field it
    ticks."""

    language: str
    tag: str
    promo: str
    keywords: tuple[str, ...]
    ## Display type -> frames in order; empty when images were not asked for.
    screenshots: dict[str, list[Path]] = field(default_factory=dict)


def page_keywords(language: str, page: str) -> tuple[str, ...]:
    """The words a page ticks: ``pages.<page>`` of the locale's main ``keywords.json``, so the pool and
    its allocation are edited together."""
    path = appstore_dir() / "locales" / language / DEFAULT_PAGE / "keywords.json"
    if not path.is_file():
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = ((data.get("pages") or {}).get(page) or "").strip()
    return tuple(word for word in raw.split(",") if word)


def page_frames(language: str, device: str, page: str) -> list[Path]:
    """A page's frames for one device: the ones it owns (``variant_slots``) from its own tree, the rest
    from the main set, merged by caption number.

    A frame the page owns but has not rendered raises rather than falling back to the main set: the
    quiet version of that is a page uploading the default billboard under its own name.
    """
    from asset_engine.store.config import variant_slot_numbers

    config = load_config(device)
    main = {slot.caption: config.frame_path(language, slot.caption) for slot in config.slots}
    main = {caption: path for caption, path in main.items() if path.is_file()}
    owned = set(variant_slot_numbers())
    page_config = load_config(device, variant=page)
    own = {slot.caption: page_config.frame_path(language, slot.caption) for slot in page_config.slots}
    own = {caption: path for caption, path in own.items() if caption in owned and path.is_file()}
    missing = sorted(owned - set(own))
    if missing and main:
        raise FramesMissing(f"[{language}] page {page} has no rendered frame {missing} for {device}")
    main.update(own)
    return [main[caption] for caption in sorted(main)]


def page_bundle(language: str, page: str, *, images: bool) -> PageBundle:
    """A locale's custom page; raises ``ListingIncomplete`` / ``CapExceeded`` / ``FramesMissing``."""
    files = publish_files()
    tag = store_locales("apple")[language]
    listing = read_listing(language, page)
    require(listing, APPLE_PROMO_FIELDS, language=language, page=page)
    promo = check_cap("apple", "promotionalText", listing["promoText"].strip(), apple_caps(), tag=tag)
    screenshots: dict[str, list[Path]] = {}
    if images:
        for display_type, device in files.apple_screenshots:
            frames = page_frames(language, device, page)
            if not frames:
                raise FramesMissing(f"[{language}] page {page}: no {device} frames rendered")
            if len(frames) > MAX_APPLE_SCREENSHOTS:
                raise FramesMissing(f"[{language}] page {page}: {len(frames)} {device} frames, Apple takes {MAX_APPLE_SCREENSHOTS}")
            screenshots[display_type] = frames
    return PageBundle(language=language, tag=tag, promo=promo, keywords=page_keywords(language, page), screenshots=screenshots)


def play_page_texts(language: str, page: str) -> tuple[str, str]:
    """A Play custom store listing's two fields for one locale: short and full description. Play's
    API has no resource for custom listings, so these are pasted into the Console."""
    tag = store_locales("play")[language]
    listing = read_listing(language, page)
    require(listing, ("subtitle.android", *DESCRIPTION_FIELDS), language=language, page=page)
    caps = play_caps()
    return (
        check_cap("play", "shortDescription", listing["subtitle"]["android"].strip(), caps, tag=tag),
        check_cap("play", "fullDescription", assemble_description(listing, "play").rstrip("\n"), caps, tag=tag),
    )


def apple_promotional_text(language: str) -> str:
    """The one field the live App Store version takes: the promotional text alone."""
    tag = store_locales("apple")[language]
    listing = read_listing(language)
    require(listing, APPLE_PROMO_FIELDS, language=language)
    return check_cap("apple", "promotionalText", listing["promoText"].strip(), apple_caps(), tag=tag)


__all__ = [
    "APPLE_FIELDS",
    "DEFAULT_PAGE",
    "MAX_APPLE_SCREENSHOTS",
    "MAX_PLAY_SCREENSHOTS",
    "PLAY_FIELDS",
    "AppleListing",
    "PageBundle",
    "CapExceeded",
    "FramesMissing",
    "ListingIncomplete",
    "PlayListing",
    "apple_caps",
    "apple_listing",
    "apple_localization_codes",
    "apple_promotional_text",
    "appstore_dir",
    "assemble_description",
    "for_store",
    "device_frames",
    "feature_graphic",
    "is_listing_complete",
    "listed_languages",
    "listing_path",
    "locales_config",
    "lookup",
    "page_bundle",
    "page_frames",
    "page_keywords",
    "play_page_texts",
    "play_caps",
    "play_listing",
    "publish_files",
    "read_json",
    "read_keywords",
    "read_listing",
    "read_tag_blocks",
    "release_notes",
    "screenshot_sets",
    "store_fields",
    "store_locales",
]
