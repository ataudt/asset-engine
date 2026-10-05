"""Typeset a localized headline onto a text-free device mockup.

Layer: asset engine.
Rules:
  - The headline block is centered horizontally on the canvas and vertically in the empty band
    above the phone. See ``config.json``. Where the phone starts is the composer's to say
    (``compose.Mockup.phone_top``) — it is geometry the config already states, and reading it back
    off the pixels, as this module did until Oct 2026, assumed a pale ground: on a dark one the
    first row of the image is already "dark enough" and the band collapses to nothing.
  - Shrink to fit, never overflow: if a caption cannot fit above ``max_bottom`` at ``min_size``,
    the layout is reported as overflowing so the copy gets trimmed instead of the art getting
    covered.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from asset_engine.store.config import ScreenshotConfig, TextStyle
from asset_engine.store.fonts import font_for_text

## Pixels a wrapped line keeps clear of ``max_width``, so a glyph's side bearing cannot touch the edge.
FIT_SAFETY_MARGIN = 4


def wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """Greedy word wrap of ``text`` to ``max_width``, measured with ``font``.

    A copy of ``sanipy.share.caption_stickers.wrap_text_lines``, not an import of it: the engine is
    shared between apps and must not depend on one of them. The two may drift; this one is the
    store set's.
    """
    words = text.split()
    if not words:
        return [text] if text else []

    lines: list[str] = []
    current: list[str] = []
    target_w = max(1, max_width - FIT_SAFETY_MARGIN)
    for word in words:
        candidate = " ".join([*current, word]) if current else word
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= target_w:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines or [text]


@dataclass(frozen=True)
class HeadlineLayout:
    """The result of fitting one caption into the text band.

    ``height``/``ink_top``/``ink_bottom`` describe the real inked box, measured from the glyphs
    rather than estimated from the font size, so blocks in different scripts (Devanagari sits
    higher, Greek lower) still center identically.
    """

    lines: list[str]
    font: ImageFont.FreeTypeFont
    size: int
    line_height: int
    height: int
    width: int
    ink_top: int
    ink_bottom: int
    origin_y: int
    overflows: bool
    shrunk: bool


def _balanced_wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Wrap ``text`` into the same number of lines a greedy wrap needs, but evenly.

    Greedy wrapping leaves a stub last line ("voice, photo, barcode, or Health / Sync"). Narrowing
    the wrap width as far as it can go without adding a line balances the lines instead.
    """
    lines = wrap_text_lines(draw, text, font, max_width)
    if len(lines) < 2:
        return lines

    target = len(lines)
    lo, hi = 1, max_width
    while lo < hi:
        mid = (lo + hi) // 2
        if len(wrap_text_lines(draw, text, font, mid)) <= target:
            hi = mid
        else:
            lo = mid + 1
    return wrap_text_lines(draw, text, font, lo)


def _wrapped_lines(
    draw: ImageDraw.ImageDraw,
    caption: list[str],
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    ## The caption's own lines come first; word-wrap is only a safety net for a line that is
    ## still too wide at this size.
    lines: list[str] = []
    for segment in caption:
        lines.extend(_balanced_wrap(draw, segment, font, max_width))
    return lines


def _line_width(draw: ImageDraw.ImageDraw, line: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), line, font=font)
    return bbox[2] - bbox[0]


def _measure_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    line_height: int,
) -> tuple[int, int, int]:
    """Ink extent of the block relative to the first line's draw origin: (top, bottom, width)."""
    tops, bottoms, widths = [], [], []
    for index, line in enumerate(lines):
        ## anchor="ma" = horizontally middle, vertically ascender; measuring with the same anchor
        ## used to draw keeps placement and measurement consistent.
        left, top, right, bottom = draw.textbbox((0, index * line_height), line, font=font, anchor="ma")
        tops.append(top)
        bottoms.append(bottom)
        widths.append(right - left)
    return min(tops), max(bottoms), max(widths, default=0)


def layout_headline(
    draw: ImageDraw.ImageDraw,
    caption: list[str],
    style: TextStyle,
    *,
    band_bottom: int | None = None,
    label: str = "",
    fonts: tuple[Path, ...] | None = None,
    keep_lines: bool = False,
) -> HeadlineLayout:
    """Fit ``caption`` into the band and center it there, shrinking until it fits.

    ``fonts`` is the list the face is picked from by coverage; the store assets leave it alone.
    ``keep_lines`` (``Slot.keep_lines``) takes the caption's lines as written: a line too wide for
    the band shrinks the type instead of wrapping.
    """
    band_bottom = style.fallback_band_bottom if band_bottom is None else band_bottom
    band_height = style.band_height(band_bottom)
    size = style.base_size

    ## Font coverage is a property of the characters, not the line breaks — join before asking.
    coverage_text = "".join(caption)

    while True:
        font = font_for_text(coverage_text, size, label=label, priority=fonts)
        lines = list(caption) if keep_lines else _wrapped_lines(draw, caption, font, style.max_width)
        line_height = round(size * style.line_spacing)
        rel_top, rel_bottom, width = _measure_block(draw, lines, font, line_height)
        height = rel_bottom - rel_top

        ## Center the measured ink box in the band, then back out the draw origin from it.
        ink_top = style.band_top + (band_height - height) // 2
        origin_y = ink_top - rel_top

        fits = width <= style.max_width and height <= band_height and len(lines) <= style.max_lines
        layout = HeadlineLayout(
            lines=lines,
            font=font,
            size=size,
            line_height=line_height,
            height=height,
            width=width,
            ink_top=ink_top,
            ink_bottom=ink_top + height,
            origin_y=origin_y,
            overflows=not fits,
            shrunk=size < style.base_size,
        )
        if fits or size <= style.min_size:
            return layout
        size = max(style.min_size, size - style.shrink_step)


def render_slot(
    mockup: Image.Image,
    caption: list[str],
    config: ScreenshotConfig,
    *,
    band_bottom: int | None = None,
    subline: list[str] | None = None,
    label: str = "",
    keep_lines: bool = False,
) -> tuple[Image.Image, HeadlineLayout]:
    """Draw ``caption`` onto the mockup and return the composed image plus its layout.

    ``mockup`` is the text-free canvas ``compose.compose_mockup`` builds from the pieces, and
    ``band_bottom`` the row its phone starts on. Without one the band falls back to the tightest
    slot's, which is what ``report`` and ``--dry-run`` lay out against since they compose nothing.

    ``subline`` is the small line under the headline, for a frame that has one. The band is
    shortened by what it needs before the headline is laid out, so headline and subline are centered
    in the band as one block rather than the subline being hung under a headline that already was.
    """
    image = mockup.convert("RGB")
    if image.size != config.canvas:
        raise ValueError(
            f"Mockup for {label or 'slot'} is {image.size}, expected {config.canvas} — "
            f"the text geometry in config.json is calibrated for that canvas only"
        )

    draw = ImageDraw.Draw(image)
    style = config.text
    band_bottom = style.fallback_band_bottom if band_bottom is None else band_bottom

    small = config.subline
    subline_font = None
    subline_lines: list[str] = []
    subline_pitch = round(small.size * small.line_spacing)
    if subline:
        subline_font = font_for_text("".join(subline), small.size, label=label)
        subline_lines = _wrapped_lines(draw, subline, subline_font, style.max_width)
        band_bottom -= small.gap + subline_pitch * len(subline_lines)

    layout = layout_headline(
        draw, caption, style, band_bottom=band_bottom, label=label, keep_lines=keep_lines
    )

    center_x = image.width // 2
    for index, line in enumerate(layout.lines):
        draw.text(
            (center_x, layout.origin_y + index * layout.line_height),
            line,
            font=layout.font,
            fill=style.color,
            anchor="ma",
        )
    for index, line in enumerate(subline_lines):
        draw.text(
            (center_x, layout.ink_bottom + small.gap + index * subline_pitch),
            line,
            font=subline_font,
            fill=small.color,
            anchor="ma",
        )
    return image, layout


__all__ = ["HeadlineLayout", "layout_headline", "render_slot"]
