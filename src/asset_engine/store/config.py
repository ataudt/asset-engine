"""Screenshot generator configuration — geometry, paths and caption -> slot mapping.

Layer: asset engine.
Rules:
  - Geometry lives in the project's ``config.json``, never inline in code, so the layout can be
    retuned without touching the renderer. Which file that is, and the root its relative paths
    resolve against, is the installed ``AssetProject``'s to say (``project.py``).
  - The caption -> slot mapping is explicit. A caption with no artwork is simply absent from
    ``slots``; the CLI reports it rather than silently dropping it.
  - Slots 4 onward share one layout (left background, upright phone, no mascot). That is said once
    in ``slot_defaults``; a slot lists only what it does differently.
  - Raw captures are named by what they *show*, in the capturer's own language, so the caption ->
    file name mapping lives per locale in ``captures.json``. ``Slot.template`` stays ``Phone N``:
    it is the output file name, not the capture's.
  - A frame that shows food names a *dish* of ``dishes.json``, and the dish owns both halves of
    what the frame says about it: the capture per language and every number a chip may show. A
    chip carries a nutrient and a place, never a value, so a picture of one dish under the numbers
    of another cannot be written down. ``captures.json`` and a page's ``captures`` may not name a
    stem for such a frame.
  - A locale's set need not be complete: ``capture_fallbacks`` names the languages to borrow a
    missing slot from, in order. Walking that chain needs the filesystem, so it lives in
    ``compose.resolve_capture``; here it is only read.
  - A *device* is the phone layout moved onto another canvas, never a second geometry. The
    ``devices`` block in ``config.json`` carries an ``offset`` that every placement and the text
    band are shifted by, and that is all: retuning a slot retunes both devices at once. See
    ``_comment_devices`` there for why the iPad set is a translation and not a rescale.
  - A *design* is the same set in another look: ``designs`` in ``config.json`` is laid over the
    layout block by block, exactly as ``flyers/config.py`` does it, and may change only
    what the look owns (``DESIGN_BLOCKS``). The canvas, the slot geometry and the captures belong
    to the product, so a design cannot reach them.
  - A *variant* is the billboard re-shot for one audience — an Apple custom product page. It may
    change only the frames ``variant_slots`` names, it renders exactly those and inherits every
    other frame from the main set, and its artwork lives in the project's ``variants.json``.
    The merge order is stated once and holds everywhere: **main slot -> variant slot -> device slot
    -> variant device slot**. A variant that restates a field the device also restates for that
    slot, without saying what the device should do with it, is refused rather than rendered half
    one way: the stores require every device's set, so the page would ship a phone frame of its own
    beside an iPad frame of the main set's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace
from functools import lru_cache
from pathlib import Path

from asset_engine.store.output import FRAME_SUFFIX
from asset_engine.project import get_project

## The device whose geometry ``config.json`` states outright; every other device is an offset from
## it. Also the device the raw captures come from, which is why ``capture_aspect`` is taken from
## this canvas and not from the one being rendered.
PHONE = "phone"

## The layout blocks a design may change. Composition is part of a look — how big the device is,
## how far it leans, which ground it stands on — so ``slot_defaults`` and ``slot_overrides`` are in.
## What is not: the canvas, the caption -> slot mapping, the capture mapping and the output paths,
## which are the product's whatever it looks like. A design that names anything else is refused with
## this list, the same split ``flyers/config.py`` draws.
DESIGN_BLOCKS = (
    "text",
    "frame",
    "shadow",
    "backgrounds",
    "ground",
    "scrim",
    "slot_defaults",
    "slot_overrides",
    "subline",
    "scanner_style",
    "chip_style",
)


@dataclass(frozen=True)
class TextStyle:
    """Where and how the headline is typeset on the mockup.

    The headline is optically centered in the empty band between the top of the canvas and the
    phone mockup, so the block sits in the gap rather than hanging off the top edge. The band's
    lower edge is measured per mockup (the slots differ by ~80px); ``fallback_band_bottom`` is
    the tightest of them, used when no mockup is composed (``report``, ``--dry-run``).
    """

    band_top: int
    band_clearance: int
    fallback_band_bottom: int
    max_width: int
    color: tuple[int, int, int]
    base_size: int
    min_size: int
    shrink_step: int
    line_spacing: float
    max_lines: int

    def band_height(self, band_bottom: int) -> int:
        return max(0, band_bottom - self.band_clearance - self.band_top)


@dataclass(frozen=True)
class Placement:
    """Where an unrotated layer sits before it is spun about its own center.

    ``left``/``top`` describe the layer's bounding box *before* rotation; ``width`` is what the layer
    is scaled to. Rotation expands the box, so the renderer keeps the center fixed rather than the
    corner — a corner would drift as the angle changes.

    ``bottom`` anchors the layer by its foot instead, and when it is set ``top`` is not used. A
    device's height is not in the config — it comes out of the raw capture — so a placement stated by
    its top moves its *bottom* edge whenever that screen is re-shot a few pixels taller, which is how
    slot 4 came to stand 9px lower than the five frames beside it. Anchored by the foot, the number
    somebody chose is the one that holds and the capture can change freely. Resolved in ``compose``,
    not here: the framed height is only known once the capture has been framed.
    """

    left: int
    top: int
    width: int
    rotation: float
    bottom: int | None = None


@dataclass(frozen=True)
class MascotPlacement(Placement):
    """A ``Placement`` that also names its artwork, relative to the project root.

    Artwork may live under several roots of the app (NutriSpy's poses are its logo and an
    onboarding asset), so the path is spelled out per slot instead of being a filename under one
    mascot directory.
    """

    asset: str = ""


@dataclass(frozen=True)
class FrameSpec:
    """The bezel drawn around a raw capture. Shape fractions live in ``frame.py``."""

    stroke: int
    color: tuple[int, int, int]


@dataclass(frozen=True)
class ShadowSpec:
    """The drop shadow under the phone, which the clean background plates do not carry."""

    offset: tuple[int, int]
    blur: int
    opacity: float
    color: tuple[int, int, int]


@dataclass(frozen=True)
class GroundExtend:
    """How one photograph is carried across a billboard wider than itself.

    The photograph is mirror-tiled rightward, so the first column of the continuation is the last
    column of the photograph reflected and the junction cannot show. Over that, two ramps: the
    continuation blurs from sharp at the junction to ``blur`` over ``blend`` pixels, which reads as
    a depth-of-field falloff rather than a cut; and the mean colour of the photograph's rightmost
    ``edge`` pixels — one averaged column, stretched — fades in from nothing at the junction to full
    at the far end. The ground therefore arrives at a colour the photograph itself states, instead
    of at one someone mixed to match it.

    ``prefade`` says the blur starts *before* the fold instead of at it: the last ``prefade`` pixels
    of the photograph itself go soft, reaching full blur by the junction, and the continuation is
    then blurred throughout rather than ramped in over ``blend``. Continuity of colour was never the
    problem — a mirror is continuous by construction — but a *sharp* reflection is still legible as
    one, and on a canvas as small as the feature graphic the eye finds the axis immediately. Out of
    focus it cannot. At the default of 0 the fold is sharp and ``blend`` governs, which is what the
    screenshots have always done.
    """

    blur: int
    blend: int
    edge: int
    prefade: int = 0


@dataclass(frozen=True)
class PhotoGround:
    """One photograph standing in for the background plates, cut into one window per slot.

    A plate was drawn for the canvas; a photograph was framed for its own aspect, so it is scaled to
    the canvas *height* and widened to as many canvases as there are background names. A name then
    says which window a slot cuts, which is what makes slots 1-3 one continuous billboard rather
    than three crops of the same picture.

    Three numbers decide *what* the first frame shows, and it is the frame that carries no phone,
    so the plate has to sit in it whole: ``zoom`` enlarges the photograph past the canvas height,
    ``shift`` is how many pixels are then cut off its left before slot 1 starts, and ``anchor`` says
    where the vertical overflow is spent — 1.0 keeps the foot of the picture, where the food is, 0.0
    the head. The two devices need different ones: the photograph is scaled by the canvas *height*,
    and that is the axis the two canvases agree on.
    """

    asset: str
    extend: GroundExtend
    shift: int = 0
    zoom: float = 1.0
    anchor: float = 1.0


@dataclass(frozen=True)
class ScrimSpec:
    """A vertical wash over the ground, so type can sit on a photograph.

    ``stops`` are ``(fraction of the canvas height, opacity)`` pairs, interpolated between, the
    same shape the website's ``--hero-scrim`` gradient has. A plate drawn for the canvas needs
    none, so the base layout carries ``null``.
    """

    color: tuple[int, int, int]
    stops: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Scanner:
    """A viewfinder drawn round the food: four corner brackets, the box they span.

    The frame that carries no phone still has to say "you photograph your meal", and a viewfinder
    over a plate says it without a word — so it needs no copy in any language.
    """

    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class ScannerStyle:
    """How a ``Scanner`` is drawn: the length of a bracket's arm, its stroke, its corner radius."""

    arm: int
    stroke: int
    radius: int
    color: tuple[int, int, int]
    shadow: ShadowSpec


@dataclass(frozen=True)
class Chip:
    """A drawn label: a name out of the app's own locale file, and one line under it.

    The store listings this set stands among do not lift real interface out of the phone; they draw
    a label larger and plainer than anything on the screen. A chip is that. Its ``key`` is a dotted
    path into ``frontend/locales/<lang>/data.json``, where nutrient and symptom names already exist
    in every language the app ships, so a chip needs no translation run of its own. The second line
    is ``value`` — a number with its unit, the same in every language — or another name
    (``detail_key``) behind ``detail_prefix``. ``value`` is never written on the chip: it is what
    the frame's dish states for ``key`` in ``dishes.json``, filled in when the slot is parsed.
    ``size`` is the name's type size; ``left``/``top`` place the card before rotation, as for every
    other layer.

    ``limited`` is for a nutrient the app holds *under* a limit instead of counting up — alcohol,
    caffeine, free sugars: its value line is drawn in ``ChipStyle.limit_color`` rather than the
    collecting green, so the frame can say "watch this one" with no word of copy to translate.
    """

    key: str
    left: int
    top: int
    size: int
    ## ``right`` anchors the card by its right edge instead, for a chip that hangs off that side of
    ## the phone. A card's width is whatever the locale's own word measures, not a number in this
    ## file, so ``left`` lets the far edge land wherever the translation puts it — "Potassium" ran
    ## 50px off the canvas in English and French where German "Kalium" had 100px to spare. Resolved
    ## in ``compose``, once the card has been drawn and its width is known.
    right: int | None = None
    rotation: float = 0.0
    value: str = ""
    detail_key: str = ""
    detail_prefix: str = ""
    limited: bool = False


@dataclass(frozen=True)
class Dish:
    """One plate or glass the store set photographs: its captures, and what is in it.

    The single statement of both, so a frame cannot show one dish and quote another. ``captures``
    is ``{language: file stem}`` under ``raw_dir``, and ``values`` is ``{nutrient key: "630 kcal"}``
    — every number any chip may show for this dish, whichever page draws it.
    """

    captures: dict[str, str]
    values: dict[str, str]


@dataclass(frozen=True)
class DishChoice:
    """Which dish a frame shows: one for everybody, or another for a locale that shot its own.

    Italy records a coconut cocktail where everyone else records a filter coffee. Looked up with
    the language the *capture* comes from, so a locale that borrows another's capture is shown —
    and labelled with — that locale's dish.
    """

    default: str
    by_language: dict[str, str] = field(default_factory=dict)

    def for_language(self, language: str) -> str:
        return self.by_language.get(language, self.default)

    @property
    def names(self) -> frozenset[str]:
        return frozenset((self.default, *self.by_language.values()))


@dataclass(frozen=True)
class Callout(Placement):
    """One element of the capture, lifted out of the phone and shown larger than the screen.

    Where a chip is drawn, a callout is the interface itself: ``crop`` is a rectangle of the *raw
    capture* as fractions (left, top, right, bottom), so it is cut from each language's own capture
    and needs no copy. A screen whose element sits elsewhere in another language — a description
    that wraps onto a third line pushes everything under it down — states that language's own
    rectangle in ``crop_by_language``, looked up, like a dish, with the language the capture
    actually came from. It is finished like a chip: ``chip_style``'s radius and shadow.
    """

    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    crop_by_language: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)

    def crop_for(self, language: str) -> tuple[float, float, float, float]:
        return self.crop_by_language.get(language, self.crop)

    @property
    def crops(self) -> tuple[tuple[float, float, float, float], ...]:
        return (self.crop, *self.crop_by_language.values())


@dataclass(frozen=True)
class ChipStyle:
    """How every chip is finished. Its shadow is its own, as the glow under the phone is pale and a
    pale glow under a pale card separates nothing."""

    padding: tuple[int, int]
    radius: int
    fill: tuple[int, int, int]
    color: tuple[int, int, int]
    detail_color: tuple[int, int, int]
    ## The value line of a ``limited`` chip, in the app's own red for alcohol.
    limit_color: tuple[int, int, int]
    detail_scale: float
    shadow: ShadowSpec


@dataclass(frozen=True)
class SublineStyle:
    """The small line under a headline. Read on the product page, not in the search result.

    The headline is sized for the thumbnail; what has to be *said* on a frame but cannot be said
    that large — the flyer's four keywords on frame 1 — goes here. ``gap`` is the distance from the
    headline's ink to the subline's.
    """

    size: int
    color: tuple[int, int, int]
    gap: int
    line_spacing: float


@dataclass(frozen=True)
class Slot:
    """One screenshot: the caption bullet, the file name, and how the artwork is assembled."""

    caption: int
    template: str
    background: str
    ## ``None`` for a frame that is a picture and not a screen: the billboard's first.
    phone: Placement | None
    mascot: MascotPlacement | None
    scanner: Scanner | None = None
    ## The dish on the phone, or ``None`` for a frame that shows no food. It answers for the frame's
    ## capture and for the value of every chip.
    dish: DishChoice | None = None
    ## The chips, by the dish they describe (``""`` on a frame without one). A frame says what is in
    ## the food on its screen, so the labels belong to the dish and not to the reader: Italy's
    ## cocktail cannot carry the coffee's caffeine.
    chips: dict[str, tuple[Chip, ...]] = field(default_factory=dict)
    ## Pieces of the interface lifted out of the phone, drawn in order over the chips.
    callouts: tuple[Callout, ...] = ()
    ## Artwork laid over the frame after the mascot — an emoji standing for what a word says.
    stickers: tuple[MascotPlacement, ...] = ()
    ## Where the headline band ends on a frame with no phone to end it; ``None`` for the layout's.
    band_bottom: int | None = None
    ## The headline's lines stand as written and only the type shrinks: for a headline whose two
    ## lines are a pair — a rhyme — and that word-wrap would break into four.
    keep_lines: bool = False

    def dish_for(self, language: str) -> str | None:
        """The dish ``language``'s capture of this frame shows, or ``None`` on a frame without one."""
        return None if self.dish is None else self.dish.for_language(language)

    def chips_for(self, language: str) -> tuple[Chip, ...]:
        """The labels for the dish ``language``'s capture shows."""
        return self.chips.get(self.dish_for(language) or "", ())


@dataclass(frozen=True)
class ScreenshotConfig:
    ## Which entry of the ``devices`` block this is — ``PHONE`` for the base geometry. It names the
    ## output set, so it is what the callers switch on; the file names come from ``Slot.template``.
    device: str
    ## Which entry of the ``designs`` block is laid over the layout, or ``None`` for the layout as
    ## written — the look the stores get. It names the output set apart from the base one.
    design: str | None
    ## Which store page this is rendered for: a slug, or ``default`` for the app's own set. Never
    ## ``None`` — the default listing is a page like any other, which is what leaves one folder
    ## formula and no main-versus-page branch in any path. A custom page narrows ``slots`` to the
    ## frames it owns, so a consumer counting captions against slots must not be handed one — see
    ## ``load_config``.
    variant: str
    canvas: tuple[int, int]
    ## The aspect a raw capture must have, from the *phone* canvas — the captures are phone captures
    ## whatever device is being rendered, so comparing them against a wider canvas would reject
    ## every one of them.
    capture_aspect: float
    raw_dir: Path
    promotional_dir: Path
    default_out_dir: Path
    text: TextStyle
    frame: FrameSpec
    shadow: ShadowSpec
    ## The windows of the billboard, in order, by the name a slot calls them: the ground is widened
    ## to one canvas per name and a slot cuts the one its name picks.
    backgrounds: tuple[str, ...]
    ## The one photograph the whole set stands on.
    ground: PhotoGround
    ## The wash over the ground, or ``None`` where the ground needs none.
    scrim: ScrimSpec | None
    subline: SublineStyle
    scanner_style: ScannerStyle
    chip_style: ChipStyle
    slots: tuple[Slot, ...]
    ## {language: {caption number: capture file stem}}, from captures.json, for the frames that show
    ## no dish. The main set, which every such frame falls back to: an Apple page owns the captures
    ## of the frames `variant_slots` names and inherits the rest, and Play inherits all of them,
    ## because its API has no custom store listing and a page's graphics come from duplicating the
    ## default one in the Console (docs/APP-STORE-CUSTOM-PAGES.md §4b).
    captures: dict[str, dict[int, str]]
    ## The same mapping for the variant being rendered, from ``variants.json``, and empty without
    ## one. Kept apart from ``captures`` rather than merged over it: ``compose.resolve_capture``
    ## walks ``(language, *capture_fallbacks)`` through the page's own stems *before* it asks the
    ## main set, so a page's missing French stem is answered by the page's English capture and not
    ## by the main set's French one — another screen under the page's headline.
    variant_captures: dict[str, dict[int, str]]
    ## The frames whose dish the variant itself states, so the reporter can tell a page's own dish
    ## from one it took off the main set.
    variant_dishes: frozenset[int]
    ## Every dish of dishes.json, by name.
    dishes: dict[str, Dish]
    ## Languages to try, in order, when the requested one has no capture for a slot.
    capture_fallbacks: tuple[str, ...]

    def capture_stem(self, language: str, slot: Slot) -> str | None:
        """The raw capture's file name (no extension) for ``slot`` in ``language``.

        A frame with a dish asks the dish and nobody else. ``None`` means this locale has no
        capture of it yet — most of them, until someone shoots a set. The language fallback chain
        then applies on top, in ``compose.resolve_capture``.
        """
        dish = slot.dish_for(language)
        if dish is not None:
            return self.dishes[dish].captures.get(language)
        return self.variant_capture_stem(language, slot) or self.captures.get(language, {}).get(
            slot.caption
        )

    def variant_capture_stem(self, language: str, slot: Slot) -> str | None:
        """This slot's capture as the *variant* names it, or ``None`` if it names none.

        What the reporter asks to tell a page's own frame from one it took off the main set, and
        what ``compose.resolve_capture`` walks first. A page names a dish frame's capture by
        stating the dish.
        """
        if slot.dish is not None:
            return self.capture_stem(language, slot) if slot.caption in self.variant_dishes else None
        return self.variant_captures.get(language, {}).get(slot.caption)

    @property
    def is_custom_page(self) -> bool:
        """Whether this is an audience-specific page rather than the app's own listing.

        What the renderer asks wherever a page genuinely differs — it owns only the billboard, and
        its headlines are overrides rather than the whole ordered set. Everything that was only
        ever about *where the files are* asks nothing now: that is ``out_subdir``.
        """
        return self.variant != _default_page()

    @property
    def out_subdir(self) -> str:
        """The one level under the locale: a design, a page, or ``default``.

        A design and a page are mutually exclusive (``load_config`` refuses the pair), so one of
        them names the set and the app's own names it ``default`` like any other. A design therefore
        shares this level with the pages, which is why ``load_config`` refuses a design named after
        one.
        """
        return self.design or self.variant

    def frames_dir(self, language: str) -> Path:
        """Where this set's frames are written, for one locale.

        ``<root>/<locale>/<set>``, locale first — the shape ``appstore/locales/``, ``frontend/locales/``,
        ``marketing/website/locales/``, ``website/`` and ``images/captures/`` all have in NutriSpy, so reading one
        tree in this repo tells you how to read the rest.

        The formula lives here rather than at each caller, because two of them are a release away
        from each other: NutriSpy's ``marketing.app_store_metadata`` assembles the Apple bundle out of it, and
        ``marketing.website.build_og_images`` composites the same frames into the page's share card.
        A second spelling of it is a tree one of them writes and the other cannot find.
        """
        return self.default_out_dir / language / self.out_subdir

    @property
    def ground_source(self) -> Path:
        """The photograph the whole set stands on."""
        return get_project().root / self.ground.asset

    @property
    def mapped_captions(self) -> frozenset[int]:
        return frozenset(slot.caption for slot in self.slots)


def _placement(raw: dict, defaults: dict, offset: tuple[int, int]) -> Placement:
    merged = {**defaults, **raw}
    ## A placement is anchored by its top or by its foot, never by both, and whichever the slot
    ## itself states wins outright. Merging the two keys the usual way would let a slot that had
    ## deliberately placed its own device inherit the other anchor from the defaults and be silently
    ## re-anchored by it — slot 3 states a top, the defaults state a foot, and only one can hold.
    vertical = raw if ("top" in raw or "bottom" in raw) else defaults
    bottom = vertical.get("bottom")
    return Placement(
        left=merged["left"] + offset[0],
        top=vertical.get("top", 0) + offset[1],
        width=merged["width"],
        rotation=merged.get("rotation", 0.0),
        bottom=None if bottom is None else bottom + offset[1],
    )


def _mascot(raw: dict | None, offset: tuple[int, int]) -> MascotPlacement | None:
    if not raw:
        return None
    return MascotPlacement(
        left=raw["left"] + offset[0],
        top=raw["top"] + offset[1],
        width=raw["width"],
        rotation=raw.get("rotation", 0.0),
        asset=raw["asset"],
    )


def parse_shadow(raw: dict) -> ShadowSpec:
    return ShadowSpec(
        offset=tuple(raw["offset"]),  # type: ignore[arg-type]
        blur=raw["blur"],
        opacity=raw["opacity"],
        color=tuple(raw["color"]),  # type: ignore[arg-type]
    )


def _scanner(raw: dict | None, offset: tuple[int, int]) -> Scanner | None:
    if not raw:
        return None
    return Scanner(
        left=raw["left"] + offset[0],
        top=raw["top"] + offset[1],
        width=raw["width"],
        height=raw["height"],
    )


def _callout(raw: dict, offset: tuple[int, int]) -> Callout:
    """One callout. ``crop`` is a rectangle, or ``{"default": rect, "<language>": rect}`` for a
    screen whose element sits elsewhere in that language's capture."""
    crop = raw["crop"]
    by_language = {}
    if isinstance(crop, dict):
        by_language = {language: tuple(rect) for language, rect in crop.items() if language != "default"}
        crop = crop["default"]
    return Callout(
        left=raw["left"] + offset[0],
        top=raw["top"] + offset[1],
        width=raw["width"],
        rotation=raw.get("rotation", 0.0),
        crop=tuple(crop),  # type: ignore[arg-type]
        crop_by_language=by_language,  # type: ignore[arg-type]
    )


def _chip(raw: dict, offset: tuple[int, int], dish: str) -> Chip:
    """One chip of ``dish`` (``""`` on a frame without one), its value read from the dish."""
    key = raw["key"]
    if "value" in raw:
        raise KeyError(
            f"the {key} chip states a value ({raw['value']!r}); a number belongs to the dish in "
            f"dishes.json, so that every frame showing the dish quotes the same one"
        )
    value = ""
    if not raw.get("detail_key"):
        values = load_dishes()[dish].values if dish else {}
        if key not in values:
            raise KeyError(
                f"the {key} chip has nothing to show: "
                + (f"dish {dish!r} states no value for it in dishes.json" if dish else "its frame names no dish")
            )
        value = values[key]
    return Chip(
        key=key,
        left=raw.get("left", 0) + offset[0],
        top=raw["top"] + offset[1],
        size=raw["size"],
        right=None if raw.get("right") is None else raw["right"] + offset[0],
        rotation=raw.get("rotation", 0.0),
        value=value,
        detail_key=raw.get("detail_key", ""),
        detail_prefix=raw.get("detail_prefix", ""),
        limited=raw.get("limited", False),
    )


def _dish_choice(raw: str | dict | None) -> DishChoice | None:
    """A slot's ``dish``: a name, or ``{"default": name, "<language>": name}`` for a locale's own."""
    if raw is None:
        return None
    if isinstance(raw, str):
        choice = DishChoice(default=raw)
    else:
        choice = DishChoice(
            default=raw["default"],
            by_language={language: name for language, name in raw.items() if language != "default"},
        )
    dishes = load_dishes()
    unknown = sorted(choice.names - set(dishes))
    if unknown:
        raise KeyError(f"no dish {unknown} in dishes.json — it has {sorted(dishes)}")
    ## A dish of its own is only ever shown by a locale that has its own capture of it; keyed by
    ## one that does not, the labels would belong to a capture nobody is shown.
    for language, name in choice.by_language.items():
        if language not in dishes[name].captures:
            raise KeyError(
                f"dish {name!r} is chosen for {language!r}, which has no capture of it in dishes.json"
            )
    return choice


def _chips(
    raw: list | dict, dish: DishChoice | None, offset: tuple[int, int]
) -> dict[str, tuple[Chip, ...]]:
    """A slot's ``chips`` by dish: a list for the frame's one dish, a ``{dish: list}`` for several."""
    shown = dish.names if dish is not None else frozenset({""})
    if not isinstance(raw, dict):
        if len(shown) > 1:
            raise KeyError(
                f"a frame showing {sorted(shown)} states one list of chips; say which dish each "
                f'set describes: "chips": {{"<dish>": [...]}}'
            )
        raw = {next(iter(shown)): raw}
    stray = sorted(set(raw) - shown)
    if stray:
        raise KeyError(
            f"chips for {stray}, but the frame shows {sorted(shown) if dish else 'no dish'} — a "
            f"frame that changes its dish states its own chips"
        )
    return {name: tuple(_chip(chip, offset, name) for chip in chips) for name, chips in raw.items()}


SLOT_FIELDS = frozenset(field.name for field in fields(Slot)) - {"caption", "template"}


def _device_slot(
    raw: dict,
    own: dict,
    defaults: dict,
    offset: tuple[int, int],
    template: str,
) -> Slot:
    """One slot as a device draws it: its own fields in its own pixels, the rest on the offset."""
    slot = parse_slot(raw, defaults, offset, template)
    if not own:
        return slot
    unshifted = parse_slot({**raw, **own}, defaults, (0, 0), template)
    return replace(slot, **{key: getattr(unshifted, key) for key in own})


def parse_slot(raw: dict, defaults: dict, offset: tuple[int, int], template: str) -> Slot:
    dish = _dish_choice(raw.get("dish"))
    return Slot(
        caption=raw["caption"],
        template=raw.get("template", template).format(caption=raw["caption"]),
        background=raw.get("background", defaults.get("background", "")),
        ## ``"phone": null`` is how a slot says it has none; saying nothing inherits the default.
        phone=(
            None
            if "phone" in raw and raw["phone"] is None
            else _placement(raw.get("phone", {}), defaults["phone"], offset)
        ),
        ## A slot that says nothing about a mascot inherits none: the defaults describe slots 4+,
        ## and those carry no mascot. ``"mascot": null`` therefore never needs to be written out.
        mascot=_mascot(raw.get("mascot"), offset),
        scanner=_scanner(raw.get("scanner"), offset),
        dish=dish,
        chips=_chips(raw.get("chips", ()), dish, offset),
        callouts=tuple(_callout(callout, offset) for callout in raw.get("callouts", ())),
        stickers=tuple(_mascot(sticker, offset) for sticker in raw.get("stickers", ())),  # type: ignore[misc]
        band_bottom=None if raw.get("band_bottom") is None else raw["band_bottom"] + offset[1],
        keep_lines=raw.get("keep_lines", False),
    )


def parse_scanner_style(raw: dict) -> ScannerStyle:
    """The viewfinder's look. Shared with the feature graphic, which states its own at its scale."""
    return ScannerStyle(
        arm=raw["arm"],
        stroke=raw["stroke"],
        radius=raw["radius"],
        color=tuple(raw["color"]),  # type: ignore[arg-type]
        shadow=parse_shadow(raw["shadow"]),
    )


def parse_chip_style(raw: dict) -> ChipStyle:
    """A drawn label's look, and a callout's corners and shadow with it."""
    return ChipStyle(
        padding=tuple(raw["padding"]),  # type: ignore[arg-type]
        radius=raw["radius"],
        fill=tuple(raw["fill"]),  # type: ignore[arg-type]
        color=tuple(raw["color"]),  # type: ignore[arg-type]
        detail_color=tuple(raw["detail_color"]),  # type: ignore[arg-type]
        limit_color=tuple(raw["limit_color"]),  # type: ignore[arg-type]
        detail_scale=raw["detail_scale"],
        shadow=parse_shadow(raw["shadow"]),
    )


def _ground(raw: dict) -> PhotoGround:
    extend = raw["extend"]
    return PhotoGround(
        asset=raw["asset"],
        extend=GroundExtend(
            blur=extend["blur"],
            blend=extend["blend"],
            edge=extend["edge"],
            prefade=extend.get("prefade", 0),
        ),
        shift=raw.get("shift", 0),
        zoom=raw.get("zoom", 1.0),
        anchor=raw.get("anchor", 1.0),
    )


def _scrim(raw: dict | None) -> ScrimSpec | None:
    if not raw:
        return None
    return ScrimSpec(
        color=tuple(raw["color"]),  # type: ignore[arg-type]
        stops=tuple((float(at), float(opacity)) for at, opacity in raw["stops"]),
    )


def overlay(base: dict, changes: dict) -> dict:
    """``base`` with ``changes`` laid over it: dicts merge key by key, anything else replaces.

    The one function every design goes through, the flyer's included (``flyers/config.py``), and
    for the same reason: a design states what it changes and inherits the rest, so two looks cannot
    drift apart in everything they do agree on.
    """
    merged = dict(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = overlay(base[key], value)
        else:
            merged[key] = value
    return merged


def _designs(raw: dict) -> dict[str, dict]:
    return {name: entry for name, entry in raw.get("designs", {}).items() if not name.startswith("_")}


def design_names(path: Path | None = None) -> tuple[str, ...]:
    """Every design ``config.json`` describes. The base layout is not one and has no name here."""
    return tuple(_designs(json.loads((path or get_project().store_files().screenshots_config).read_text(encoding="utf-8"))))


def _by_caption(block: dict) -> dict[str, dict[int, str]]:
    return {
        language: {int(caption): stem for caption, stem in names.items()}
        for language, names in block.items()
    }


def _captures(path: Path) -> dict[str, dict[int, str]]:
    """Read ``captures.json``, which is keyed by language and nothing else.

    It held a ``variants`` block until 2026-09-21, when custom store pages stopped having artwork of
    their own. Every key here is a locale now, so a documentation ``_comment`` cannot be added at the
    top level — it would be read as one.
    """
    return _by_caption(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def load_dishes() -> dict[str, Dish]:
    """Read ``dishes.json``: every dish a frame may show, by name."""
    raw = json.loads(get_project().store_files().dishes.read_text(encoding="utf-8"))
    return {
        name: Dish(captures=dict(entry["captures"]), values=dict(entry["values"]))
        for name, entry in raw.items()
        if not name.startswith("_")
    }


def device_names(path: Path | None = None) -> tuple[str, ...]:
    """Every device ``config.json`` describes, the base one first.

    The CLI's ``--device`` choices and its default come from here rather than from a list in the
    parser, so adding a device to ``config.json`` is the whole change.
    """
    raw = json.loads((path or get_project().store_files().screenshots_config).read_text(encoding="utf-8"))
    return (PHONE, *raw.get("devices", {}))


def _variants(raw: dict) -> dict[str, dict]:
    return {name: entry for name, entry in raw.items() if not name.startswith("_")}


def variant_names(path: Path | None = None) -> tuple[str, ...]:
    """Every custom product page ``variants.json`` gives artwork of its own.

    Not every configured page is in here: a page that re-aims the billboard in words alone needs no
    entry, and one that inherits another's (``histamine`` through ``symptom``) is rendered from its
    chain. This is the sweep's enumeration — the pages whose artwork a test has to resolve.
    """
    return tuple(_variants(json.loads((path or get_project().store_files().variants).read_text(encoding="utf-8"))))


def variant_slot_numbers(path: Path | None = None) -> tuple[int, ...]:
    """The frames a custom product page owns, from ``variant_slots`` in ``config.json``.

    A page renders exactly these and inherits the rest, so this is also what the Apple bundle takes
    from a page's tree rather than from the main one.
    """
    raw = json.loads((path or get_project().store_files().screenshots_config).read_text(encoding="utf-8"))
    return tuple(int(caption) for caption in raw.get("variant_slots", ()))


def _default_page() -> str:
    """The slug of the app's own set, from the project's pages.

    Asked of the project rather than spelled again in ``config.json``, because a second spelling is
    a tree one tool writes and another cannot find.
    """
    return get_project().pages.default()


def _variant_layers(variant: str) -> list[str]:
    """One page's artwork layers, lowest precedence first, without the default listing.

    The same chain the listing copy resolves through, so a page cannot inherit its text from
    ``symptom`` and its artwork from somewhere else.
    """
    pages = get_project().pages
    ## Every layer on disk, not just the targetable ones: ``symptom`` is never a page of its own
    ## but is the layer the five symptom pages resolve their artwork through, and ``default`` has
    ## to be loadable because it is what building without ``--variant`` means. Which slugs may be
    ## *built* is the CLI's question, and ``PageRegistry.check_slug`` answers it there.
    known = pages.slugs(all_layers=True)
    if variant not in known:
        raise KeyError(f"no store page {variant!r} — the project's pages are {known}")
    return pages.overlay_layers(variant)


def _variant_slot_raw(base: dict, own: dict) -> dict:
    """One frame as a page restates it: a shallow merge, as a device's own block is.

    Shallow and not ``overlay`` on purpose: a page that restates ``dish`` or ``chips`` replaces
    the main set's whole, Italy's cocktail included. What it may not do is keep one half — chips
    left over from a dish the frame no longer shows are refused in ``_chips``.
    """
    return {**base, **own}


def _variant_blocks(
    variant: str, path: Path | None = None
) -> tuple[dict[int, dict], dict[int, dict[str, dict]], dict[str, dict[int, str]]]:
    """``(slot overrides, per-device slot overrides, capture stems)`` for one page.

    Each is the page's chain applied in order, so ``histamine`` reads as ``symptom`` plus what it
    changes. A layer with no entry is simply nothing laid over: ``variants.json`` carries artwork
    only for the pages that have some.
    """
    entries = _variants(json.loads((path or get_project().store_files().variants).read_text(encoding="utf-8")))
    slots: dict[int, dict] = {}
    devices: dict[int, dict[str, dict]] = {}
    captures: dict[str, dict[int, str]] = {}
    for layer in _variant_layers(variant):
        entry = entries.get(layer) or {}
        unknown = sorted(set(entry) - {"slots", "captures"} - {k for k in entry if k.startswith("_")})
        if unknown:
            raise KeyError(
                f"variant {layer!r} states {unknown} in variants.json; a page states "
                f"['captures', 'slots'] — the canvas, the look and the caption mapping are the "
                f"product's, not the page's"
            )
        for caption, own in (entry.get("slots") or {}).items():
            own = dict(own)
            per_device = own.pop("devices", {})
            slots[int(caption)] = _variant_slot_raw(slots.get(int(caption), {}), own)
            for name, device_fields in per_device.items():
                devices.setdefault(int(caption), {}).setdefault(name, {}).update(device_fields)
        for language, stems in (entry.get("captures") or {}).items():
            captures.setdefault(language, {}).update(
                {int(caption): stem for caption, stem in stems.items()}
            )
    unknown_fields = sorted(
        (
            {key for own in slots.values() for key in own}
            | {key for per in devices.values() for own in per.values() for key in own}
        )
        - SLOT_FIELDS
    )
    if unknown_fields:
        raise KeyError(
            f"variant {variant!r} states {unknown_fields} for a slot; a slot has {sorted(SLOT_FIELDS)}"
        )
    return slots, devices, captures


@lru_cache(maxsize=None)
def load_config(
    device: str = PHONE,
    design: str | None = None,
    variant: str | None = None,
    path: Path | None = None,
) -> ScreenshotConfig:
    """Read ``config.json`` for one device and resolve its relative paths against the project root.

    ``device`` is ``PHONE`` for the geometry as written, or a key of the ``devices`` block: its
    canvas, with every placement and the text band shifted by its ``offset``. ``design`` is
    ``None`` for the look as written — what the stores get — or a key of the ``designs`` block,
    laid over the layout first, so the device's own overrides still win over it.

    ``variant`` is a store page — ``default`` for the app's own set, which is what ``None`` means
    here and the only place ``None`` survives: it is resolved on the first line, so every field and
    every path below reads one slug. For a *custom* page the returned config holds **only the frames
    that page owns** (``variant_slots``), each merged with what the page restates, and its capture
    stems in ``variant_captures``. So anything comparing captions against ``slots`` —
    ``cli._report_unmapped`` — needs the default config, or every frame a page inherits reads as a
    caption with no artwork. A custom page and a design together are refused: a design is an
    experiment on the default look, and the two write into one tree.
    """
    variant = variant or _default_page()
    custom_page = variant != _default_page()
    raw = json.loads((path or get_project().store_files().screenshots_config).read_text(encoding="utf-8"))
    if custom_page and design is not None:
        raise KeyError(
            f"variant {variant!r} with design {design!r}: a design is an experiment on the default "
            f"look and a page is the default look re-aimed, so there is no tree for both"
        )

    if design is not None:
        designs = _designs(raw)
        if design not in designs:
            raise KeyError(f"no design {design!r} in config.json — it has {list(designs)}")
        changes = designs[design]
        unknown = sorted(set(changes) - set(DESIGN_BLOCKS))
        if unknown:
            raise KeyError(
                f"design {design!r} changes {unknown}; a design may only change "
                f"{list(DESIGN_BLOCKS)} — the rest belongs to the product, not the look"
            )
        ## One test, because ``default`` is a slug now: a design sharing any layer's name would
        ## write into that layer's tree, the app's own set included.
        if design in get_project().pages.slugs(all_layers=True):
            raise KeyError(
                f"design {design!r} is named after a store page, so it would write into that "
                f"page's tree — rename one of the two"
            )
        raw = overlay(raw, changes)

    devices = raw.get("devices", {})
    if device != PHONE and device not in devices:
        raise KeyError(f"no device {device!r} in config.json — it has {(PHONE, *devices)}")
    override = devices.get(device, {})
    offset: tuple[int, int] = tuple(override.get("offset", (0, 0)))  # type: ignore[assignment]

    ## The device block restates only what it changes; ``text`` is merged key by key so a device
    ## saying ``max_width`` does not have to repeat the nine other typesetting numbers.
    text = {**raw["text"], **override.get("text", {})}
    ## The one placement a device does restate: the photograph is scaled by the canvas height,
    ## which barely changes between the two, while the canvas gets much wider — see
    ## ``_comment_devices``. Merged key by key, so a device names only the numbers it moves.
    ground = overlay(raw["ground"], override.get("ground", {}))
    ## The band is part of the layout, so it moves with everything else. Stated here rather than in
    ## the device block: a device that had to restate it could put it out of step with the mockup.
    text["band_top"] += offset[1]
    text["fallback_band_bottom"] += offset[1]

    ## The extension is not a device's to choose: the writer, the discovery globs and the Fastfile
    ## all read ``FRAME_SUFFIX``, so a device names only its prefix and the suffix is appended here.
    template_name = override.get("template", "Phone {caption}") + FRAME_SUFFIX
    defaults = raw["slot_defaults"]
    ## A design states only the slots it recomposes, keyed by caption number. ``slots`` itself is a
    ## list, and a list replaces the list it meets, so a design reaching into it directly would have
    ## to restate all eight — and would then own the caption mapping, which is not its to own.
    overrides = {int(key): value for key, value in raw.get("slot_overrides", {}).items()}
    slots = [overlay(entry, overrides.get(entry["caption"], {})) for entry in raw["slots"]]
    ## What a device states about a slot is in *its own* canvas pixels, and only those fields:
    ## ``offset`` moves the phone layout onto a wider canvas, which is right for everything standing
    ## on the ground and wrong for a frame that *is* the ground — a photograph cut for another
    ## aspect needs its viewfinder and poses placed against the picture, not translated by a fixed
    ## number. Everything the device leaves alone still follows the offset. See ``_comment_devices``.
    device_slots = {int(key): value for key, value in override.get("slots", {}).items()}
    unknown_fields = sorted({key for own in device_slots.values() for key in own} - SLOT_FIELDS)
    if unknown_fields:
        raise KeyError(
            f"device {device!r} states {unknown_fields} for a slot; a slot has {sorted(SLOT_FIELDS)}"
        )

    ## A page owns the billboard and inherits every other frame, so its config carries only the
    ## frames ``variant_slots`` names, each merged with what the page restates. The merge order is
    ## main slot -> page -> device -> page's own block for that device, which is the one place the
    ## page can answer for a field the device restates in its own pixels.
    variant_slots: dict[int, dict] = {}
    variant_device_slots: dict[int, dict[str, dict]] = {}
    variant_captures: dict[str, dict[int, str]] = {}
    if custom_page:
        owned = variant_slot_numbers(path)
        if not owned:
            raise KeyError("config.json has no variant_slots, so no frame belongs to a page")
        unmapped = sorted(set(owned) - {entry["caption"] for entry in slots})
        if unmapped:
            raise KeyError(f"variant_slots names captions {unmapped} that no slot carries")
        variant_slots, variant_device_slots, variant_captures = _variant_blocks(variant)
        outside = sorted((set(variant_slots) | set(variant_device_slots)) - set(owned))
        if outside:
            raise KeyError(
                f"variant {variant!r} restates frames {outside}; a page owns {list(owned)} and "
                f"inherits the rest, so a frame it changed outside that range would never render"
            )
        ## A field the device restates for this frame wins over the page, because the device block
        ## is applied last. Refused rather than rendered: the stores want every device's set, so the
        ## page would ship its own phone frame beside the main set's iPad frame.
        for caption, own in variant_slots.items():
            for name, block in devices.items():
                device_own = {int(key): value for key, value in block.get("slots", {}).items()}
                answered = variant_device_slots.get(caption, {}).get(name, {})
                clash = sorted(set(own) & set(device_own.get(caption, {})) - set(answered))
                if clash:
                    raise KeyError(
                        f"variant {variant!r} and device {name!r} both state {clash} for frame "
                        f"{caption}; the device is applied last, so say what {name!r} should do "
                        f'with it under "devices": {{"{name}": ...}} in variants.json'
                    )
        slots = [
            _variant_slot_raw(entry, variant_slots.get(entry["caption"], {}))
            for entry in slots
            if entry["caption"] in set(owned)
        ]

    captures = _captures(get_project().store_files().captures)
    root = get_project().root
    phone_canvas = raw["canvas"]
    parsed_slots = tuple(
        _device_slot(
            s,
            {
                **device_slots.get(s["caption"], {}),
                **variant_device_slots.get(s["caption"], {}).get(device, {}),
            },
            defaults,
            offset,
            template_name,
        )
        for s in slots
    )
    ## A dish frame's capture is the dish's. A stem named for it here would be read by nothing, and
    ## reads as if it decided what the frame shows — which is how a page came to show the main
    ## set's coffee under a poke bowl's numbers.
    dish_frames = {slot.caption for slot in parsed_slots if slot.dish is not None}
    for source, mapping in (("captures.json", captures), (f"variant {variant!r}", variant_captures)):
        stray = sorted(
            f"{language}/{caption}"
            for language, stems in mapping.items()
            for caption in stems
            if caption in dish_frames
        )
        if stray:
            raise KeyError(
                f"{source} names captures for {stray}, frames that show a dish; the dish's "
                f"captures in dishes.json are what they show"
            )
    return ScreenshotConfig(
        device=device,
        design=design,
        variant=variant,
        canvas=tuple(override.get("canvas", phone_canvas)),  # type: ignore[arg-type]
        capture_aspect=phone_canvas[0] / phone_canvas[1],
        raw_dir=root / raw["raw_dir"],
        promotional_dir=root / raw["promotional_dir"],
        default_out_dir=root / raw["default_out_dir"],
        text=TextStyle(
            band_top=text["band_top"],
            band_clearance=text["band_clearance"],
            fallback_band_bottom=text["fallback_band_bottom"],
            max_width=text["max_width"],
            color=tuple(text["color"]),  # type: ignore[arg-type]
            base_size=text["base_size"],
            min_size=text["min_size"],
            shrink_step=text["shrink_step"],
            line_spacing=text["line_spacing"],
            max_lines=text["max_lines"],
        ),
        frame=FrameSpec(stroke=raw["frame"]["stroke"], color=tuple(raw["frame"]["color"])),  # type: ignore[arg-type]
        shadow=parse_shadow(raw["shadow"]),
        backgrounds=tuple(raw["backgrounds"]),
        ground=_ground(ground),
        scrim=_scrim(raw.get("scrim")),
        subline=SublineStyle(
            size=raw["subline"]["size"],
            color=tuple(raw["subline"]["color"]),  # type: ignore[arg-type]
            gap=raw["subline"]["gap"],
            line_spacing=raw["subline"]["line_spacing"],
        ),
        scanner_style=parse_scanner_style(raw["scanner_style"]),
        chip_style=parse_chip_style(raw["chip_style"]),
        slots=parsed_slots,
        captures=captures,
        variant_captures=variant_captures,
        variant_dishes=frozenset(caption for caption, own in variant_slots.items() if "dish" in own),
        dishes=load_dishes(),
        capture_fallbacks=tuple(raw["capture_fallbacks"]),
    )


__all__ = [
    "load_dishes",
    "Dish",
    "DishChoice",
    "variant_names",
    "variant_slot_numbers",
    "parse_chip_style",
    "parse_scanner_style",
    "parse_shadow",
    "parse_slot",
    "Callout",
    "Chip",
    "ChipStyle",
    "DESIGN_BLOCKS",
    "PHONE",
    "GroundExtend",
    "FrameSpec",
    "MascotPlacement",
    "PhotoGround",
    "Placement",
    "Scanner",
    "ScannerStyle",
    "ScreenshotConfig",
    "ScrimSpec",
    "ShadowSpec",
    "Slot",
    "SublineStyle",
    "TextStyle",
    "design_names",
    "device_names",
    "load_config",
    "overlay",
]
