"""The pieces a printed sheet is drawn from: ground, type, wordmark, codes, pictures.

Layer: asset engine.
Rules:
  - Every piece takes pixel positions on the bled canvas, already resolved by its product: a flyer
    measures from the top of the trimmed page, a roll-up from the floor of its visible area. The
    pieces know neither, so both draw them alike.
  - Every piece that must survive the cut returns the box it drew, so the safe margin and the
    product's rules are checked against what is on the canvas and not against what a config meant.
  - Nothing the store frames already draw is drawn again: the page-filling crop is
    ``store.compose.cover``, the shrink-to-fit typesetting ``store.render.layout_headline``, the face
    choice by coverage ``store.fonts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from asset_engine.printing.qr import render_qr_card
from asset_engine.store.compose import center_of, cover, paste_rotated
from asset_engine.store.config import Placement, TextStyle
from asset_engine.store.render import layout_headline

Box = tuple[int, int, int, int]

## Stands in for "no limit" where ``TextStyle`` wants a band or a line count the stack decides itself.
UNBOUNDED = 10**6


@dataclass(frozen=True)
class StackLayout:
    """Several paragraphs set at one size, stacked in a band."""

    paragraphs: list[list[str]]
    fonts: list[ImageFont.FreeTypeFont]
    size: int
    line_height: int
    gap: int
    width: int
    height: int
    top: int
    overflows: bool


def fixed_style(*, max_width: int, color: tuple[int, int, int], size: int, line_spacing: float) -> TextStyle:
    """A style ``layout_headline`` wraps at exactly ``size``, leaving the fitting to the caller."""
    return TextStyle(
        band_top=0,
        band_clearance=0,
        fallback_band_bottom=UNBOUNDED,
        max_width=max_width,
        color=color,
        base_size=size,
        min_size=size,
        shrink_step=1,
        line_spacing=line_spacing,
        max_lines=UNBOUNDED,
    )


def layout_stack(
    draw: ImageDraw.ImageDraw,
    paragraphs: list[list[str]],
    faces: list[tuple[Path, ...]],
    *,
    band_top: int,
    band_bottom: int,
    max_width: int,
    base_size: int,
    min_size: int,
    step: int,
    line_spacing: float,
    paragraph_gap: float,
    color: tuple[int, int, int],
    label: str = "",
) -> StackLayout:
    """Set every paragraph at the one size at which the whole stack fits the band.

    ``layout_headline`` fits one block on its own; a page of text is read as one piece, and three
    sizes on it would look like an accident. So the paragraphs are wrapped by it at a fixed size
    and the size is stepped down here until the stack fits, the same idea as
    ``feature_graphic.uniform_layouts``. ``faces`` is one face list per paragraph.
    """
    band_height = band_bottom - band_top
    size = base_size
    while True:
        style = fixed_style(max_width=max_width, color=color, size=size, line_spacing=line_spacing)
        blocks = [
            layout_headline(draw, lines, style, label=label, fonts=face) for lines, face in zip(paragraphs, faces)
        ]
        line_height = round(size * line_spacing)
        gap = round(line_height * paragraph_gap)
        height = sum(len(block.lines) for block in blocks) * line_height + gap * (len(blocks) - 1)
        width = max(block.width for block in blocks)
        fits = height <= band_height and width <= max_width
        if fits or size <= min_size:
            return StackLayout(
                paragraphs=[block.lines for block in blocks],
                fonts=[block.font for block in blocks],
                size=size,
                line_height=line_height,
                gap=gap,
                width=width,
                height=height,
                top=band_top + (band_height - height) // 2,
                overflows=not fits,
            )
        size = max(min_size, size - step)


def draw_stack(draw: ImageDraw.ImageDraw, stack: StackLayout, *, center_x: int, color: tuple[int, int, int]) -> Box:
    """Draw ``stack`` centred on ``center_x`` from its own top, and return the block's box."""
    y = stack.top
    for lines, font in zip(stack.paragraphs, stack.fonts):
        for line in lines:
            draw.text((center_x, y), line, font=font, fill=color, anchor="ma")
            y += stack.line_height
        y += stack.gap
    return (
        center_x - stack.width // 2,
        stack.top,
        center_x + (stack.width + 1) // 2,
        stack.top + stack.height,
    )


def background(size: tuple[int, int], spec: dict[str, Any], root: Path, *, veil: float = 0.0) -> Image.Image:
    """A ground of ``size``: a plain ``color``, or the ``asset`` photo cut to fill it.

    ``anchor`` (0 top to 1 bottom, or ``"bottom"``) and ``anchor_x`` (0 left to 1 right) say which
    part of the photo is kept. A photo can be lightened with ``veil`` so type on it stays readable.
    """
    if "color" in spec:
        return Image.new("RGBA", size, (*spec["color"], 255))
    plate = Image.open(root / spec["asset"]).convert("RGBA")
    canvas = cover(plate, size, anchor=spec.get("anchor", "bottom"), anchor_x=spec.get("anchor_x", 0.5))
    if veil:
        canvas.alpha_composite(Image.new("RGBA", canvas.size, (255, 255, 255, round(255 * veil))))
    return canvas


def draw_runs(
    draw: ImageDraw.ImageDraw,
    parts: list[dict[str, Any]],
    font: ImageFont.FreeTypeFont,
    *,
    center_x: float,
    top: int,
) -> Box:
    """A word in several colours (a wordmark), centred on ``center_x``.

    Each run starts where the text before it ends in the *whole* word, so the runs sit exactly
    where one draw call would have put them.
    """
    text = "".join(part["text"] for part in parts)
    left = center_x - draw.textlength(text, font=font) / 2
    drawn = ""
    for part in parts:
        x = left + draw.textlength(drawn, font=font)
        draw.text((x, top), part["text"], font=font, fill=tuple(part["color"]), anchor="la")
        drawn += part["text"]
    box = draw.textbbox((left, top), text, font=font, anchor="la")
    return (round(box[0]), round(box[1]), round(box[2]), round(box[3]))


def paste_qr(
    canvas: Image.Image,
    url: str,
    *,
    center_x: int,
    top: int,
    size: int,
    quiet_modules: int,
    corner_radius: int,
    dark: tuple[int, int, int],
) -> Box:
    """The code for ``url`` on its white card, ``size`` pixels square, centred on ``center_x``."""
    card = render_qr_card(url, size, quiet_modules=quiet_modules, corner_radius=corner_radius, dark=dark)
    left = center_x - size // 2
    canvas.alpha_composite(card, (left, top))
    return (left, top, left + size, top + size)


def paste_by_height(canvas: Image.Image, path: Path, *, center_x: int, top: int, height: int) -> Box:
    """The picture at ``path`` scaled to ``height``, centred on ``center_x``. Raises if it is missing."""
    if not path.is_file():
        raise FileNotFoundError(f"no picture at {path}")
    art = Image.open(path).convert("RGBA")
    art = art.resize((round(art.width * height / art.height), height), Image.LANCZOS)
    left = center_x - art.width // 2
    canvas.alpha_composite(art, (left, top))
    return (left, top, left + art.width, top + art.height)


def paste_placed(canvas: Image.Image, path: Path, placement: Placement) -> None:
    """The picture at ``path`` at ``placement``'s width, spun about its centre — a pose or a prop."""
    art = Image.open(path).convert("RGBA")
    art = art.resize((placement.width, round(art.height * placement.width / art.width)), Image.LANCZOS)
    paste_rotated(canvas, art, center_of(placement, art.size), placement.rotation)


__all__ = [
    "Box",
    "StackLayout",
    "UNBOUNDED",
    "background",
    "draw_runs",
    "draw_stack",
    "fixed_style",
    "layout_stack",
    "paste_by_height",
    "paste_placed",
    "paste_qr",
]
