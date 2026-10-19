"""Compose a banner from its element list.

Layer: asset engine.
Rules:
  - Every number in an element is a pixel of the platform's canvas; it is multiplied by the banner's
    ``scale`` here and nowhere else, so the config reads like the platform's own figures.
  - Every element reports what it drew (``Drawn``): its box in canvas pixels, clipped to the canvas,
    and the edges it runs off on purpose (``bleeds``, e.g. a phone cut by the bottom edge). ``rules``
    judges the banner from these, not from what the config meant.
  - Type wider than its ``max_width`` is shrunk to ``min_size`` and then drawn anyway, recorded on its
    ``Drawn`` (``overflow``); the build writes the banner as it fell and prints it.
  - Nothing the store frames already draw is drawn again: the bezel is ``store.frame.render_frame``,
    the drop shadow ``store.compose.shadow_for``, the crop ``cover``, the corners ``rounded_corners``,
    the face choice by coverage ``store.fonts``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from PIL import Image, ImageDraw

from asset_engine.banners.config import Banner, BannerConfig, BannerConfigError, Box, read_copy
from asset_engine.project import get_project
from asset_engine.store.compose import cover, rounded_corners, shadow_for
from asset_engine.store.config import ShadowSpec
from asset_engine.store.fonts import font_for_text
from asset_engine.store.frame import FrameStyle, render_frame, screen_height_for

EDGES = ("left", "top", "right", "bottom")


@dataclass
class Drawn:
    """What one element put on the canvas, for the rules."""

    id: str
    type: str
    role: str = ""
    ## Canvas pixels of the platform (not of the scaled image), clipped to the canvas; ``None`` for
    ## an element nothing has to survive (the ground).
    box: Box | None = None
    bleeds: tuple[str, ...] = ()
    text: str = ""
    ## Why the type did not fit its width, when it did not; drawn anyway at its smallest size.
    overflow: str = ""


@dataclass(frozen=True)
class ComposedBanner:
    image: Image.Image
    drawn: list[Drawn] = field(default_factory=list)


def _composite(canvas: Image.Image, layer: Image.Image, at: tuple[int, int]) -> None:
    """``alpha_composite`` that accepts a layer reaching past any edge, a shadow's blur above the top."""
    left, top = at
    crop = (max(0, -left), max(0, -top), layer.width, layer.height)
    if crop[0] >= crop[2] or crop[1] >= crop[3]:
        return
    canvas.alpha_composite(layer.crop(crop), (max(0, left), max(0, top)))


def _place(canvas: Image.Image, layer: Image.Image, center: tuple[float, float], rotation: float) -> None:
    """Spin ``layer`` about its own centre and composite it on ``center``."""
    if rotation:
        layer = layer.rotate(rotation, resample=Image.BICUBIC, expand=True)
    _composite(canvas, layer, (round(center[0] - layer.width / 2), round(center[1] - layer.height / 2)))


def _lifted(canvas: Image.Image, config: BannerConfig, spec: dict[str, Any], layer: Image.Image, center: tuple[float, float], scale: int) -> None:
    """``layer`` on ``center`` over its drop shadow, both turned by the element's ``rotation``."""
    rotation = spec.get("rotation", 0)
    if "shadow" in spec:
        raw = spec["shadow"]
        shadow = ShadowSpec(
            offset=(raw["offset"][0] * scale, raw["offset"][1] * scale),
            blur=raw["blur"] * scale,
            opacity=raw["opacity"],
            color=config.color(raw["color"]),
        )
        _place(canvas, shadow_for(layer, shadow), (center[0] + shadow.offset[0], center[1] + shadow.offset[1]), rotation)
    _place(canvas, layer, center, rotation)


def _box(banner: Banner, left: float, top: float, right: float, bottom: float) -> Box:
    """A box in scaled pixels as canvas pixels of the platform, clipped to the canvas."""
    width, height = banner.platform.canvas
    s = banner.scale
    return (
        max(0, math.floor(left / s)),
        max(0, math.floor(top / s)),
        min(width, math.ceil(right / s)),
        min(height, math.ceil(bottom / s)),
    )


## Pillow's radial gradient reaches 255 in its square's corners, so its inscribed circle's rim, where
## a glow must have faded out, reads 255 / sqrt(2).
RADIAL_RIM = 255 / math.sqrt(2)


def _smooth(opacity: float):
    """A glow's mask from Pillow's radial gradient: full ``opacity`` at the centre, easing to nothing
    at the rim of the inscribed circle and staying there into the corners."""

    def level(value: int) -> int:
        t = max(0.0, 1 - value / RADIAL_RIM)
        return round(255 * opacity * t * t * (3 - 2 * t))

    return level


def _gradient(canvas: Image.Image, config: BannerConfig, banner: Banner, spec: dict[str, Any], **_) -> Drawn:
    """The ground: one colour running into another at ``angle`` (0 = left to right, 90 = top to
    bottom), with soft round ``glows`` of light laid over it."""
    width, height = canvas.size
    side = math.ceil(math.hypot(width, height))
    ## Pillow's gradient runs top to bottom; turned about the square's centre, the square still
    ## covers the canvas, whose half-diagonal is the square's half-side.
    ramp = Image.linear_gradient("L").resize((side, side), Image.BILINEAR).rotate(90 - spec["angle"], resample=Image.BILINEAR)
    left, top = (side - width) // 2, (side - height) // 2
    mask = ramp.crop((left, top, left + width, top + height))
    start = Image.new("RGBA", canvas.size, (*config.color(spec["from"]), 255))
    end = Image.new("RGBA", canvas.size, (*config.color(spec["to"]), 255))
    canvas.paste(Image.composite(end, start, mask), (0, 0))
    s = banner.scale
    for glow in spec.get("glows", []):
        radius = glow["radius"] * s
        falloff = Image.radial_gradient("L").resize((2 * radius, 2 * radius), Image.BILINEAR)
        light = Image.new("RGBA", falloff.size, (*config.color(glow["color"]), 255))
        canvas.paste(light, (glow["center"][0] * s - radius, glow["center"][1] * s - radius), falloff.point(_smooth(glow["opacity"])))
    return Drawn(id=spec["id"], type="gradient")


def _text(canvas: Image.Image, config: BannerConfig, banner: Banner, spec: dict[str, Any], *, copy: dict, **_) -> Drawn:
    """One line of copy set from ``left`` on ``baseline``, shrunk until it fits ``max_width``."""
    s = banner.scale
    text = copy[spec["copy"]]
    faces = config.fonts[spec["font"]]
    label = f"{banner.id} {spec['id']}"
    max_width = spec["max_width"] * s
    size = spec["size"]
    while True:
        font = font_for_text(text, size * s, label=label, priority=faces)
        ink = font.getbbox(text, anchor="ls")
        if ink[2] - ink[0] <= max_width or size <= spec["min_size"]:
            break
        size -= 1
    overflow = (
        f"is {(ink[2] - ink[0]) // s}px wide at {size}px, over its max_width {spec['max_width']} — shorten the copy or widen it"
        if ink[2] - ink[0] > max_width
        else ""
    )
    x, y = spec["left"] * s, spec["baseline"] * s
    ImageDraw.Draw(canvas).text((x, y), text, font=font, fill=(*config.color(spec["color"]), 255), anchor="ls")
    return Drawn(
        id=spec["id"],
        type="text",
        role=spec.get("role", ""),
        box=_box(banner, x + ink[0], y + ink[1], x + ink[2], y + ink[3]),
        bleeds=tuple(spec.get("bleeds", ())),
        text=text,
        overflow=overflow,
    )


def _phone(canvas: Image.Image, config: BannerConfig, banner: Banner, spec: dict[str, Any], **_) -> Drawn:
    """A phone showing ``screen`` (a capture, cover-cropped to the screen), ``width`` wide from
    ``top``, centred on ``center_x``. Its height is the capture's: a phone taller than the banner runs
    off the bottom, which ``bleeds: ["bottom"]`` declares."""
    s = banner.scale
    stroke = spec["frame"]["stroke"] * s
    screen_width = spec["width"] * s - 2 * stroke
    capture = Image.open(get_project().root / spec["screen"]).convert("RGB")
    screen = (screen_width, screen_height_for(capture, screen_width))
    frame = render_frame(capture, screen_size=screen, style=FrameStyle(stroke=stroke, color=config.color(spec["frame"]["color"])))
    left = spec["center_x"] * s - frame.width / 2
    top = spec["top"] * s
    _lifted(canvas, config, spec, frame, (left + frame.width / 2, top + frame.height / 2), s)
    return Drawn(
        id=spec["id"],
        type="phone",
        role=spec.get("role", ""),
        box=_box(banner, left, top, left + frame.width, top + frame.height),
        bleeds=tuple(spec.get("bleeds", ())),
    )


def _tile(canvas: Image.Image, config: BannerConfig, banner: Banner, spec: dict[str, Any], **_) -> Drawn:
    """An app icon as the home screen shows it: a square ``size`` wide at ``left``/``top``, its
    corners rounded by ``radius`` (a share of the side; iOS draws about 0.2237), over a shadow.
    The source is the full-bleed square icon the stores take, never one with its own corners."""
    s = banner.scale
    size = spec["size"] * s
    icon = cover(Image.open(get_project().root / spec["asset"]).convert("RGBA"), (size, size), anchor=0.5)
    tile = rounded_corners(icon, round(spec["radius"] * size))
    left, top = spec["left"] * s, spec["top"] * s
    _lifted(canvas, config, spec, tile, (left + size / 2, top + size / 2), s)
    return Drawn(
        id=spec["id"],
        type="tile",
        role=spec.get("role", ""),
        box=_box(banner, left, top, left + size, top + size),
        bleeds=tuple(spec.get("bleeds", ())),
    )


ELEMENT_TYPES = {
    "gradient": _gradient,
    "text": _text,
    "phone": _phone,
    "tile": _tile,
}


def compose_banner(config: BannerConfig, banner: Banner) -> ComposedBanner:
    """The whole banner at its scale, and what each element drew."""
    copy = read_copy(banner.language)
    canvas = Image.new("RGBA", banner.size, (255, 255, 255, 255))
    drawn = []
    for spec in banner.elements:
        if spec["type"] not in ELEMENT_TYPES:
            raise BannerConfigError(f"element {spec['id']!r} has type {spec['type']!r}; there are {sorted(ELEMENT_TYPES)}")
        unknown = sorted(set(spec.get("bleeds", ())) - set(EDGES))
        if unknown:
            raise BannerConfigError(f"element {spec['id']!r} bleeds off {unknown}; the edges are {list(EDGES)}")
        drawn.append(ELEMENT_TYPES[spec["type"]](canvas, config, banner, spec, copy=copy))
    return ComposedBanner(image=canvas.convert("RGB"), drawn=drawn)


__all__ = ["EDGES", "ELEMENT_TYPES", "ComposedBanner", "Drawn", "compose_banner"]
