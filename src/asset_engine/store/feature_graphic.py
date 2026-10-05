"""Compose the 1024x500 Google Play feature graphic from the same pieces as the screenshots.

Layer: asset engine.
Rules:
  - Geometry lives in the project's ``feature_graphic.json``, never inline here — same rule as
    ``config.json``. The layout collates slots 1-3 of ``config.json`` into one banner.
  - Nothing is re-implemented: the bezel comes from ``compose.framed_capture``, the drop shadow and
    rotation from ``compose``, the shrink-to-fit typesetting from ``render.layout_headline``, and
    the ground is the screenshots' own billboard under ``compose.ground_plate`` and
    ``compose.apply_scrim`` — the banner sits at the top of the same listing as the frames, so it
    cannot be lit differently. It stood on a plate of its own
    (``images/store-pieces/feature-graphic-background.png``) until Oct 2026, when the set moved onto
    the photograph; it kept a window and a blur of its own until the layout followed the frames.
  - The three headlines share one type size (``uniform_layouts``); unlike a screenshot, where only
    one headline is ever on screen, here they are read side by side.
  - Only the headlines are localized. The device screens are drawn ~180px wide, where in-app text
    is illegible, so they come from whichever locale has raw captures (``--phone-language``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

from asset_engine.store.captions import CaptionError
from asset_engine.store.compose import (
    apply_scrim,
    center_of,
    framed_capture,
    ground_plate,
    paste_furniture,
    paste_rotated,
    resolve_capture,
    shadow_for,
)
from asset_engine.store.config import (
    ChipStyle,
    FrameSpec,
    GroundExtend,
    ScannerStyle,
    ScreenshotConfig,
    ShadowSpec,
    Slot,
    TextStyle,
    load_config,
    parse_chip_style,
    parse_scanner_style,
    parse_slot,
)
from asset_engine.store.output import FRAME_SUFFIX
from asset_engine.project import get_project
from asset_engine.store.render import HeadlineLayout, layout_headline

OUTPUT_NAME = f"featureGraphic{FRAME_SUFFIX}"

## The file name Play reserves for this asset, and what the metadata builder copies it to. Play
## matches the stem against {png,jpg,jpeg}, so the suffix follows the rest of the frames.
PLAY_ASSET_NAME = OUTPUT_NAME


class FeatureGraphicError(RuntimeError):
    """The feature graphic cannot be composed as configured."""


@dataclass(frozen=True)
class Block:
    """One column of the graphic: a headline, and the frame standing under it.

    A block *is* a slot drawn on another canvas — the same stance ``config.json``'s ``_comment_devices``
    takes for the iPad. It carries the whole of a slot's furniture (phone, scanner, chips, callouts,
    mascot, stickers) in banner pixels, so block 1 can say ``"phone": null`` and be the photograph
    exactly as frame 1 is. ``Slot.band_bottom`` and ``Slot.template`` go unused here: the banner has
    one headline band for all three blocks and writes one file.
    """

    headline: int
    center_x: int
    text: TextStyle
    capture: int
    slot: Slot


@dataclass(frozen=True)
class Ground:
    """The banner's share of the billboard — the screenshots' own numbers, at the banner's scale.

    ``_ground_panorama`` scales the photograph by the canvas *height*, and ``zoom`` and ``anchor``
    are dimensionless, so passing the screenshots' values with a 500px canvas reproduces their
    picture at 18.6%. ``shift`` and ``extend`` are in panorama pixels and are the only numbers that
    have to be restated: a ``blend`` of 360 would never resolve across a tail this short.

    There is no blur here. The panorama already grades its own depth of field into the mirrored
    continuation, and a second falloff over the top of it is what flattened the banner to a brown
    smear; the type is carried by the scrim, which covers the headline band exactly.
    """

    shift: int
    zoom: float
    anchor: float
    extend: GroundExtend


@dataclass(frozen=True)
class FeatureGraphicConfig:
    canvas: tuple[int, int]
    ## The locale whose raw captures fill the device screens. At ~120px wide the in-app text is
    ## illegible, so only the headlines are localized; this is the app's most complete capture set.
    capture_language: str
    ground: Ground
    frame: FrameSpec
    shadow: ShadowSpec
    scanner_style: ScannerStyle
    chip_style: ChipStyle
    blocks: tuple[Block, ...]


def _text_style(base: dict, override: dict) -> tuple[TextStyle, int]:
    merged = {**base, **override}
    style = TextStyle(
        band_top=merged["band_top"],
        band_clearance=merged["band_clearance"],
        fallback_band_bottom=merged["fallback_band_bottom"],
        max_width=merged["max_width"],
        color=tuple(merged["color"]),  # type: ignore[arg-type]
        base_size=merged["base_size"],
        min_size=merged["min_size"],
        shrink_step=merged["shrink_step"],
        line_spacing=merged["line_spacing"],
        max_lines=merged["max_lines"],
    )
    return style, merged["center_x"]


@lru_cache(maxsize=1)
def load_feature_config(path: Path | None = None) -> FeatureGraphicConfig:
    raw = json.loads((path or get_project().store_files().feature_graphic).read_text(encoding="utf-8"))
    blocks = []
    for entry in raw["blocks"]:
        style, center_x = _text_style(raw["text"], entry.get("text", {}))
        ## The block's furniture is parsed by the screenshots' own slot parser, at offset zero: the
        ## numbers are already the banner's. ``caption`` is the capture number, which is what the
        ## capture lookup keys on, so no second naming scheme appears here.
        blocks.append(
            Block(
                headline=entry["headline"],
                center_x=center_x,
                text=style,
                capture=entry["capture"],
                slot=parse_slot({**entry, "caption": entry["capture"]}, {"phone": {}}, (0, 0), OUTPUT_NAME),
            )
        )
    return FeatureGraphicConfig(
        canvas=tuple(raw["canvas"]),  # type: ignore[arg-type]
        capture_language=raw["capture_language"],
        ground=Ground(
            shift=raw["ground"]["shift"],
            zoom=raw["ground"]["zoom"],
            anchor=raw["ground"]["anchor"],
            extend=GroundExtend(
                blur=raw["ground"]["extend"]["blur"],
                blend=raw["ground"]["extend"]["blend"],
                edge=raw["ground"]["extend"]["edge"],
                prefade=raw["ground"]["extend"].get("prefade", 0),
            ),
        ),
        frame=FrameSpec(stroke=raw["frame"]["stroke"], color=tuple(raw["frame"]["color"])),  # type: ignore[arg-type]
        shadow=ShadowSpec(
            offset=tuple(raw["shadow"]["offset"]),  # type: ignore[arg-type]
            blur=raw["shadow"]["blur"],
            opacity=raw["shadow"]["opacity"],
            color=tuple(raw["shadow"]["color"]),  # type: ignore[arg-type]
        ),
        scanner_style=parse_scanner_style(raw["scanner_style"]),
        chip_style=parse_chip_style(raw["chip_style"]),
        blocks=tuple(blocks),
    )


def read_headlines(
    language: str,
    config: ScreenshotConfig | None = None,
) -> list[list[str]]:
    """Feature-graphic headlines for one locale, in block order.

    Same shape and same failure modes as ``captions.read_captions`` — a headline is its list of
    hard-broken lines, and a bare string is a malformed file rather than a shorthand. One set per
    locale: a custom store page differs in text only, never in artwork.
    """
    config = config or load_config()
    ## The app's own layer by name, never ``config.variant``: Play has no custom store listing API,
    ## so one feature graphic ships for the whole app whatever page is being built.
    path = config.promotional_dir / f"{language}" / get_project().pages.default() / "featureGraphic.json"
    if not path.is_file():
        raise CaptionError(f"[{language}] missing feature graphic file: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    headlines = data.get("headlines")
    if not isinstance(headlines, list) or not headlines:
        raise CaptionError(f"[{language}] no 'headlines' array in {path}")

    normalized: list[list[str]] = []
    for index, headline in enumerate(headlines, start=1):
        if not isinstance(headline, list) or not all(isinstance(line, str) for line in headline):
            raise CaptionError(
                f"[{language}] headline {index} in {path} must be a list of strings, "
                f"got {type(headline).__name__}"
            )
        lines = [line.strip() for line in headline if line.strip()]
        if not lines:
            raise CaptionError(f"[{language}] headline {index} in {path} is empty")
        normalized.append(lines)
    return normalized


def uniform_layouts(
    draw: ImageDraw.ImageDraw,
    headlines: list[list[str]],
    blocks: tuple[Block, ...],
    *,
    label: str = "",
) -> list[HeadlineLayout]:
    """Typeset every headline at the one size that all three of them fit.

    ``layout_headline`` shrinks each caption on its own, which is right for a screenshot — only one
    headline is ever on screen there. Here the three are read side by side, and three sizes in one
    image read as an accident rather than a hierarchy, so the smallest fitting size wins for all.
    Overflow is still decided per block afterwards: the shared size can only be smaller than the one
    a block reached alone, and ``min_size`` floors both passes.
    """
    ## Pass 1: what each block fits on its own. Pass 2: everyone at the smallest of those.
    common = min(layout_headline(draw, h, b.text, label=label).size for h, b in zip(headlines, blocks))
    return [
        layout_headline(draw, headline, replace(block.text, base_size=common), label=label)
        for headline, block in zip(headlines, blocks)
    ]


def compose_feature_graphic(
    headlines: list[list[str]],
    *,
    language: str,
    phone_language: str,
    screenshots: ScreenshotConfig | None = None,
    config: FeatureGraphicConfig | None = None,
    label: str = "",
) -> Image.Image:
    """Draw the full graphic: background plate, three phones, two mascots, three headlines."""
    screenshots = screenshots or load_config()
    config = config or load_feature_config()

    if len(headlines) != len(config.blocks):
        raise FeatureGraphicError(
            f"{len(headlines)} headline(s) for {len(config.blocks)} block(s) — "
            f"featureGraphic.json and feature_graphic.json disagree"
        )

    canvas = ground_plate(
        screenshots.ground_source,
        config.canvas,
        extend=config.ground.extend,
        shift=config.ground.shift,
        zoom=config.ground.zoom,
        anchor=config.ground.anchor,
    )
    if screenshots.scrim is not None:
        apply_scrim(canvas, screenshots.scrim)

    ## Each block is drawn whole — phone, then its furniture — rather than every phone first and
    ## every pose after. The poses sit between the columns and no pose overlaps a neighbour's
    ## bezel, so the order is free today; it is a constraint on anything moved sideways later.
    for block in config.blocks:
        if block.slot.phone is not None:
            framed = framed_capture(
                screenshots,
                phone_language,
                block.slot,
                frame=config.frame,
                phone_width=block.slot.phone.width,
            )
            center = center_of(block.slot.phone, framed.size)
            offset = config.shadow.offset
            paste_rotated(
                canvas,
                shadow_for(framed, config.shadow),
                (center[0] + offset[0], center[1] + offset[1]),
                block.slot.phone.rotation,
            )
            paste_rotated(canvas, framed, center, block.slot.phone.rotation)

        ## Which dish the chips describe follows the capture, and the banner's block names one dish
        ## outright — so a locale that records a dish of its own never reaches the banner.
        resolved = resolve_capture(screenshots, phone_language, block.slot) if block.slot.phone is not None else None
        paste_furniture(
            canvas,
            screenshots,
            language,
            block.slot,
            scanner_style=config.scanner_style,
            chip_style=config.chip_style,
            shown=phone_language if resolved is None else resolved[1],
            capture_language=phone_language,
        )

    image = canvas.convert("RGB")
    draw = ImageDraw.Draw(image)
    overflows: list[str] = []
    layouts = uniform_layouts(draw, headlines, config.blocks, label=label)
    for block, layout in zip(config.blocks, layouts):
        if layout.overflows:
            overflows.append(
                f"block {block.headline}: {len(layout.lines)} lines at {layout.size}px "
                f"({layout.width}x{layout.height}px)"
            )
        for index, line in enumerate(layout.lines):
            draw.text(
                (block.center_x, layout.origin_y + index * layout.line_height),
                line,
                font=layout.font,
                fill=block.text.color,
                anchor="ma",
            )

    if overflows:
        ## Same stance as the screenshot build: clipped type is a rejected asset, not a warning.
        raise FeatureGraphicError(
            f"headline(s) do not fit the feature graphic — trim the copy: {'; '.join(overflows)}"
        )
    return image


__all__ = [
    "OUTPUT_NAME",
    "PLAY_ASSET_NAME",
    "Block",
    "FeatureGraphicConfig",
    "Ground",
    "FeatureGraphicError",
    "compose_feature_graphic",
    "load_feature_config",
    "read_headlines",
]
