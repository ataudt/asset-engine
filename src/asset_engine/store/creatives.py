"""The App Store's creative assets: the product page header, the search results image, the universal asset.

Layer: asset engine.
Rules:
  - What Apple accepts — size, aspect, art safe area, file formats — is ``app_store_creatives.json``,
    read off Apple's specification with the date; nothing here spells a size. The layout is the
    project's ``creatives.json`` (``StoreFiles.creatives``), one entry per placement, keyed by the
    placement's name in that file.
  - A placement is the feature graphic's idea on Apple's canvas: the set's own billboard under its
    scrim, frames of the set standing on it (``feature_graphic.draw_blocks``), type set by
    ``render.layout_headline``. Nothing the frames already draw is drawn again. It sits above the
    screenshots on the product page, so it cannot be lit differently from them.
  - The words are the screenshots' captions, by number (``captions.read_captions``), with a caption's
    subline under it on request: a creative says what the frames under it say, and needs no copy —
    and no translation round — of its own.
  - The phones show the reader's own captures, through the set's fallback chain. On these canvases a
    phone is drawn large enough for its interface to be read, unlike on the feature graphic, so a
    German reader is shown German screens wherever there are any.
  - Every element with ``role: "key"`` (the default) must lie where it is seen, judged on the box it
    actually drew. The iPhone is the measure: where the placement has a measured iPhone view
    (``iphone.iphone_view``), that is the window the phone shows, clear of what the App Store lays
    over it; elsewhere it is Apple's art safe area, which also keeps what a Mac would crop.
    ``"aux"`` marks art that may run past it and be cropped.
  - Type in key art must arrive at ``iphone.min_points()`` or larger on the phone, headline and
    subline each.
  - No finding withholds a file (``printing.report``): type that overflows or is too small and key
    art reaching past its zone are drawn as they fall and printed loudly. What cannot be drawn at all — a canvas
    Apple refuses, a caption that does not exist — is an error.
  - No alpha: the image is flattened to RGB. A placement that takes JPEG is written the way the
    frames are (``output.save_frame``); one that takes PNG only is written as PNG.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from asset_engine.project import get_project
from asset_engine.store.captions import read_captions, read_sublines
from asset_engine.store.compose import apply_scrim, ground_plate
from asset_engine.store.config import (
    ChipStyle,
    FrameSpec,
    GroundExtend,
    ScannerStyle,
    ScreenshotConfig,
    ScrimSpec,
    ShadowSpec,
    Slot,
    SublineStyle,
    TextStyle,
    load_config,
    overlay,
    parse_chip_style,
    parse_scanner_style,
    parse_shadow,
    parse_slot,
)
from asset_engine.store.feature_graphic import Box, Ground, draw_blocks, parse_text_style
from asset_engine.store.fonts import font_for_text
from asset_engine.store.iphone import IphoneView, iphone_view, min_points, too_small
from asset_engine.store.output import FRAME_SUFFIX, save_frame
from asset_engine.store.render import _wrapped_lines, layout_headline

SPECS_PATH = Path(__file__).resolve().parent / "app_store_creatives.json"

## A canvas within this share of Apple's aspect is taken as it: 3840x1646 is 21:9 to 0.02 %, and
## 5244x2950 is 16:9 to 0.01 %.
ASPECT_TOLERANCE = 0.001

ROLES = ("key", "aux")

## The style sections a placement may restate over the file's own; each merges key by key.
STYLE_KEYS = ("frame", "shadow", "scanner_style", "chip_style", "text", "subline")


class CreativeError(RuntimeError):
    """A creative cannot be composed as configured."""


@dataclass(frozen=True)
class CreativeSpec:
    """One placement as Apple specifies it (``app_store_creatives.json``)."""

    name: str
    aspect: tuple[int, int]
    reference: tuple[int, int]
    min: tuple[int, int]
    max: tuple[int, int]
    safe_origin: tuple[int, int]
    safe_size: tuple[int, int]
    formats: tuple[str, ...]

    def check_canvas(self, size: tuple[int, int]) -> None:
        """``CreativeError`` unless Apple accepts an image of ``size`` for this placement."""
        width, height = size
        if not (self.min[0] <= width <= self.max[0] and self.min[1] <= height <= self.max[1]):
            raise CreativeError(
                f"{self.name}: a {width}x{height} canvas is outside what Apple accepts "
                f"({self.min[0]}x{self.min[1]} to {self.max[0]}x{self.max[1]})"
            )
        if abs(width * self.aspect[1] / (height * self.aspect[0]) - 1) > ASPECT_TOLERANCE:
            raise CreativeError(f"{self.name}: a {width}x{height} canvas is not {self.aspect[0]}:{self.aspect[1]}")

    def safe_area(self, size: tuple[int, int]) -> Box:
        """Apple's art safe area on a canvas of ``size``: stated for ``reference``, scaled with the width."""
        scale = size[0] / self.reference[0]
        (x, y), (width, height) = self.safe_origin, self.safe_size
        return (round(x * scale), round(y * scale), round((x + width) * scale), round((y + height) * scale))

    @property
    def suffix(self) -> str:
        """The frames' own format where Apple takes it, else PNG."""
        return FRAME_SUFFIX if "jpeg" in self.formats else ".png"


@lru_cache(maxsize=1)
def creative_specs() -> dict[str, CreativeSpec]:
    """Every placement Apple specifies, by key."""
    raw = json.loads(SPECS_PATH.read_text(encoding="utf-8"))
    return {
        name: CreativeSpec(
            name=name,
            aspect=tuple(entry["aspect"]),  # type: ignore[arg-type]
            reference=tuple(entry["reference"]),  # type: ignore[arg-type]
            min=tuple(entry["min"]),  # type: ignore[arg-type]
            max=tuple(entry["max"]),  # type: ignore[arg-type]
            safe_origin=tuple(entry["art_safe_area"]["origin"]),  # type: ignore[arg-type]
            safe_size=tuple(entry["art_safe_area"]["size"]),  # type: ignore[arg-type]
            formats=tuple(entry["formats"]),
        )
        for name, entry in raw.items()
        if not name.startswith("_")
    }


def creative_spec(name: str) -> CreativeSpec:
    specs = creative_specs()
    if name not in specs:
        raise CreativeError(f"no App Store placement {name!r} — app_store_creatives.json has {sorted(specs)}")
    return specs[name]


@dataclass(frozen=True)
class CreativeBlock:
    """A frame of the set standing on the creative: a slot in the creative's pixels, as on the
    feature graphic. ``capture`` in the config is the caption number the capture is looked up by."""

    role: str
    slot: Slot


@dataclass(frozen=True)
class CreativeText:
    """A screenshot caption set in a band, centred on ``center_x``, with its subline if asked."""

    role: str
    caption: int
    center_x: int
    style: TextStyle
    subline: SublineStyle | None


@dataclass(frozen=True)
class Creative:
    """One placement's layout, from the project's ``creatives.json``."""

    name: str
    spec: CreativeSpec
    canvas: tuple[int, int]
    ground: Ground
    ## ``inherit_scrim``: the placement said nothing, so the screenshots' own scrim is used.
    scrim: ScrimSpec | None
    inherit_scrim: bool
    frame: FrameSpec
    shadow: ShadowSpec
    scanner_style: ScannerStyle
    chip_style: ChipStyle
    blocks: tuple[CreativeBlock, ...]
    texts: tuple[CreativeText, ...]

    @property
    def safe_area(self) -> Box:
        return self.spec.safe_area(self.canvas)

    @property
    def iphone(self) -> IphoneView | None:
        return iphone_view(self.name)


@dataclass(frozen=True)
class CreativesConfig:
    ## ``{set}``, ``{language}`` and ``{placement}``; the suffix follows the placement's format.
    name_template: str
    placements: dict[str, Creative]

    def placement(self, name: str) -> Creative:
        if name not in self.placements:
            raise CreativeError(f"no placement {name!r} in creatives.json — it lays out {list(self.placements)}")
        return self.placements[name]

    def file_name(self, name: str, *, language: str, set_name: str) -> str:
        creative = self.placement(name)
        return self.name_template.format(set=set_name, language=language, placement=name) + creative.spec.suffix


def _role(raw: dict, label: str) -> str:
    role = raw.get("role", "key")
    if role not in ROLES:
        raise CreativeError(f"{label} has role {role!r}; the roles are {list(ROLES)}")
    return role


def _scrim(raw: dict | None) -> ScrimSpec | None:
    if not raw:
        return None
    return ScrimSpec(
        color=tuple(raw["color"]),  # type: ignore[arg-type]
        stops=tuple((float(at), float(opacity)) for at, opacity in raw["stops"]),
    )


def _subline(base: dict | None, value: bool | dict | None) -> SublineStyle | None:
    if not value:
        return None
    merged = overlay(base or {}, value if isinstance(value, dict) else {})
    return SublineStyle(
        size=merged["size"],
        color=tuple(merged["color"]),  # type: ignore[arg-type]
        gap=merged["gap"],
        line_spacing=merged["line_spacing"],
    )


def _creative(name: str, entry: dict, shared: dict) -> Creative:
    spec = creative_spec(name)
    styles = {key: overlay(shared.get(key) or {}, entry.get(key) or {}) for key in STYLE_KEYS}
    canvas = tuple(entry.get("canvas", spec.reference))
    spec.check_canvas(canvas)  # type: ignore[arg-type]
    ground = entry["ground"]
    blocks = []
    for index, raw in enumerate(entry.get("blocks", ()), start=1):
        ## Parsed by the screenshots' own slot parser at offset zero, as the feature graphic's blocks
        ## are: the numbers are already the creative's.
        slot = parse_slot({**raw, "caption": raw["capture"]}, {"phone": {}}, (0, 0))
        blocks.append(CreativeBlock(role=_role(raw, f"{name} block {index}"), slot=slot))
    texts = []
    for index, raw in enumerate(entry.get("texts", ()), start=1):
        style, center_x = parse_text_style(styles["text"], raw)
        texts.append(
            CreativeText(
                role=_role(raw, f"{name} text {index}"),
                caption=raw["caption"],
                center_x=center_x,
                style=style,
                subline=_subline(styles["subline"], raw.get("subline")),
            )
        )
    return Creative(
        name=name,
        spec=spec,
        canvas=canvas,  # type: ignore[arg-type]
        ground=Ground(
            shift=ground["shift"],
            zoom=ground["zoom"],
            anchor=ground["anchor"],
            extend=GroundExtend(
                blur=ground["extend"]["blur"],
                blend=ground["extend"]["blend"],
                edge=ground["extend"]["edge"],
                prefade=ground["extend"].get("prefade", 0),
            ),
        ),
        scrim=_scrim(entry.get("scrim")),
        inherit_scrim="scrim" not in entry,
        frame=FrameSpec(stroke=styles["frame"]["stroke"], color=tuple(styles["frame"]["color"])),  # type: ignore[arg-type]
        shadow=parse_shadow(styles["shadow"]),
        scanner_style=parse_scanner_style(styles["scanner_style"]),
        chip_style=parse_chip_style(styles["chip_style"]),
        blocks=tuple(blocks),
        texts=tuple(texts),
    )


@lru_cache(maxsize=None)
def load_creatives_config(path: Path | None = None) -> CreativesConfig:
    source = path or get_project().store_files().creatives_config()
    raw = json.loads(source.read_text(encoding="utf-8"))
    shared = {key: raw.get(key) for key in STYLE_KEYS}
    return CreativesConfig(
        name_template=raw["name_template"],
        placements={
            name: _creative(name, entry, shared)
            for name, entry in raw["placements"].items()
            if not name.startswith("_")
        },
    )


@dataclass
class Drawn:
    """What one element put on the creative: its box in canvas pixels, ``None`` if nothing."""

    id: str
    role: str
    box: Box | None


@dataclass(frozen=True)
class ComposedCreative:
    creative: Creative
    image: Image.Image
    drawn: list[Drawn] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)


def _union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _outside(box: Box, zone: Box) -> list[str]:
    """The edges of ``box`` that reach past ``zone``, with how far."""
    reach = {"left": zone[0] - box[0], "top": zone[1] - box[1], "right": box[2] - zone[2], "bottom": box[3] - zone[3]}
    return [f"{edge} by {px}px" for edge, px in reach.items() if px > 0]


def _overlaps(box: Box, other: Box) -> bool:
    return box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]


def _zone_findings(creative: Creative, item: Drawn) -> list[str]:
    """Where a key element is not seen: past the iPhone window or under an overlay where the
    placement has an iPhone view, past Apple's art safe area where it has none."""
    assert item.box is not None
    view = creative.iphone
    if view is None:
        safe = creative.safe_area
        past = _outside(item.box, safe)
        return [f"{item.id}: reaches past Apple's art safe area {list(safe)} — {', '.join(past)}"] if past else []
    window = view.window(creative.canvas)
    findings = []
    past = _outside(item.box, window)
    if past:
        findings.append(f"{item.id}: reaches past what an iPhone shows {list(window)} — {', '.join(past)}")
    for name, box in view.overlay_boxes(creative.canvas).items():
        if _overlaps(item.box, box):
            findings.append(f"{item.id}: lies under the iPhone's {name} {list(box)}")
    return findings


def _points_per_pixel(creative: Creative) -> float | None:
    """Points on the phone per canvas pixel, where the placement has a measured iPhone view."""
    view = creative.iphone
    return None if view is None else view.points_per_pixel(creative.canvas)


def _draw_text(
    draw: ImageDraw.ImageDraw,
    text: CreativeText,
    caption: list[str],
    subline: list[str] | None,
    *,
    label: str,
) -> tuple[Box | None, str, dict[str, int]]:
    """Set one caption, and its subline under it, the way a frame does (``render.render_slot``):
    the band is shortened by what the subline needs first, so the two are centred as one block.
    Returns the inked box, why it overflowed, if it did, and the pixel size each part was set in."""
    style = text.style
    band_bottom = style.fallback_band_bottom
    small_font: ImageFont.FreeTypeFont | None = None
    small_lines: list[str] = []
    pitch = 0
    if subline and text.subline is not None:
        small = text.subline
        pitch = round(small.size * small.line_spacing)
        small_font = font_for_text("".join(subline), small.size, label=label)
        small_lines = _wrapped_lines(draw, subline, small_font, style.max_width)
        band_bottom -= small.gap + pitch * len(small_lines)

    layout = layout_headline(draw, caption, style, band_bottom=band_bottom, label=label)
    inked: list[Box] = []
    for index, line in enumerate(layout.lines):
        at = (text.center_x, layout.origin_y + index * layout.line_height)
        draw.text(at, line, font=layout.font, fill=style.color, anchor="ma")
        inked.append(draw.textbbox(at, line, font=layout.font, anchor="ma"))
    for index, line in enumerate(small_lines):
        at = (text.center_x, layout.ink_bottom + text.subline.gap + index * pitch)  # type: ignore[union-attr]
        draw.text(at, line, font=small_font, fill=text.subline.color, anchor="ma")  # type: ignore[union-attr]
        inked.append(draw.textbbox(at, line, font=small_font, anchor="ma"))
    overflow = (
        f"{len(layout.lines)} lines at {layout.size}px ({layout.width}x{layout.height}px) do not fit "
        f"max_width {style.max_width} / the band — trim the caption or widen the band"
        if layout.overflows
        else ""
    )
    sizes = {"headline": layout.size}
    if small_lines:
        sizes["subline"] = text.subline.size  # type: ignore[union-attr]
    return _union(inked), overflow, sizes


def compose_creative(
    creative: Creative,
    language: str,
    *,
    screenshots: ScreenshotConfig | None = None,
) -> ComposedCreative:
    """The whole creative for one locale, what each element drew, and every finding."""
    screenshots = screenshots or load_config()
    canvas = ground_plate(
        screenshots.ground_source,
        creative.canvas,
        extend=creative.ground.extend,
        shift=creative.ground.shift,
        zoom=creative.ground.zoom,
        anchor=creative.ground.anchor,
    )
    scrim = screenshots.scrim if creative.inherit_scrim else creative.scrim
    if scrim is not None:
        apply_scrim(canvas, scrim)

    boxes = draw_blocks(
        canvas,
        screenshots,
        [block.slot for block in creative.blocks],
        frame=creative.frame,
        shadow=creative.shadow,
        scanner_style=creative.scanner_style,
        chip_style=creative.chip_style,
        language=language,
        phone_language=language,
    )
    drawn = [
        Drawn(id=f"block {index} (capture {block.slot.caption})", role=block.role, box=box)
        for index, (block, box) in enumerate(zip(creative.blocks, boxes), start=1)
    ]

    image = canvas.convert("RGB")
    findings: list[str] = []
    if creative.texts:
        draw = ImageDraw.Draw(image)
        captions = read_captions(language, screenshots)
        sublines = read_sublines(language, screenshots) if any(t.subline for t in creative.texts) else {}
        for text in creative.texts:
            if not 1 <= text.caption <= len(captions):
                raise CreativeError(
                    f"[{language}] {creative.name}: caption {text.caption} does not exist — the set has {len(captions)}"
                )
            element = f"caption {text.caption}"
            box, overflow, sizes = _draw_text(
                draw,
                text,
                captions[text.caption - 1],
                sublines.get(text.caption),
                label=f"{language} {creative.name} {element}",
            )
            drawn.append(Drawn(id=element, role=text.role, box=box))
            if overflow:
                findings.append(f"{element}: {overflow}")
            scale = _points_per_pixel(creative)
            if scale is not None and text.role == "key":
                for part, size in sizes.items():
                    points = too_small(size, scale)
                    if points is not None:
                        findings.append(
                            f"{element} {part}: {size}px arrives at {points:.1f} pt on an iPhone, "
                            f"below the {min_points():g} pt minimum"
                        )

    for item in drawn:
        if item.role != "key" or item.box is None:
            continue
        findings.extend(_zone_findings(creative, item))
    return ComposedCreative(creative=creative, image=image, drawn=drawn, findings=findings)


## The guides sheet's width, whatever the canvas: enough to judge a layout, small enough to open.
GUIDES_WIDTH = 1920


def _dashed_rectangle(draw: ImageDraw.ImageDraw, box: Box, color: tuple[int, ...], *, width: int, dash: int = 14) -> None:
    left, top, right, bottom = box
    for x in range(left, right, dash * 2):
        draw.line([(x, top), (min(x + dash, right), top)], fill=color, width=width)
        draw.line([(x, bottom), (min(x + dash, right), bottom)], fill=color, width=width)
    for y in range(top, bottom, dash * 2):
        draw.line([(left, y), (left, min(y + dash, bottom))], fill=color, width=width)
        draw.line([(right, y), (right, min(y + dash, bottom))], fill=color, width=width)


def guides(composed: ComposedCreative) -> Image.Image:
    """The creative at ``GUIDES_WIDTH``, with what key art must stay inside left bright and the
    rest dimmed. Where the placement has an iPhone view that is the phone's window (green), with the
    App Store's overlays on it (red) and Apple's art safe area dashed in cyan for reference; elsewhere
    it is the art safe area (cyan). Key elements' boxes in magenta, auxiliary ones' in orange."""
    image = composed.image
    creative = composed.creative
    scale = GUIDES_WIDTH / image.width
    sheet = image.resize((GUIDES_WIDTH, round(image.height * scale)), Image.LANCZOS).convert("RGBA")

    def scaled(box: Box) -> Box:
        return (round(box[0] * scale), round(box[1] * scale), round(box[2] * scale), round(box[3] * scale))

    safe = scaled(creative.safe_area)
    view = creative.iphone
    zone = safe if view is None else scaled(view.window(creative.canvas))
    veil = Image.new("RGBA", sheet.size, (0, 0, 0, 120))
    ImageDraw.Draw(veil).rectangle(zone, fill=(0, 0, 0, 0))
    sheet.alpha_composite(veil)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=18)
    cyan = (0, 230, 255, 255)
    if view is None:
        draw.rectangle(safe, outline=cyan, width=3)
        draw.text((safe[0] + 6, safe[1] + 4), "art safe area", fill=cyan, font=font)
    else:
        green, red = (40, 220, 90, 255), (255, 50, 50, 255)
        _dashed_rectangle(draw, safe, cyan, width=2)
        draw.text((safe[0] + 6, safe[3] - 24), "art safe area (iPad, Mac)", fill=cyan, font=font)
        draw.rectangle(zone, outline=green, width=3)
        draw.text((zone[0] + 6, zone[3] - 24), "iPhone", fill=green, font=font)
        for overlay in view.overlay_boxes(creative.canvas).values():
            draw.rectangle(scaled(overlay), outline=red, width=2)
    for item in composed.drawn:
        if item.box is None:
            continue
        color = (255, 0, 255, 255) if item.role == "key" else (255, 160, 0, 255)
        box = scaled(item.box)
        draw.rectangle(box, outline=color, width=2)
        draw.text((box[0] + 4, box[3] - 22), item.id, fill=color, font=font)
    return sheet.convert("RGB")


## Every extension a creative may have carried, so a stale sibling in another format is cleared.
_STALE_SUFFIXES = (".png", ".jpeg", ".jpg")


def save_creative(image: Image.Image, dest: Path) -> None:
    """Write a creative to ``dest`` in the format its suffix names, with no alpha either way."""
    if dest.suffix == FRAME_SUFFIX:
        save_frame(image, dest)
        return
    for suffix in _STALE_SUFFIXES:
        if suffix != dest.suffix:
            dest.with_suffix(suffix).unlink(missing_ok=True)
    image.convert("RGB").save(dest, format="PNG")


__all__ = [
    "SPECS_PATH",
    "ComposedCreative",
    "Creative",
    "CreativeBlock",
    "CreativeError",
    "CreativeSpec",
    "CreativeText",
    "CreativesConfig",
    "Drawn",
    "compose_creative",
    "creative_spec",
    "creative_specs",
    "guides",
    "load_creatives_config",
    "save_creative",
]
