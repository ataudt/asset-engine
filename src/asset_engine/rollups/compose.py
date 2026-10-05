"""Compose a roll-up from its element list.

Layer: asset engine.
Rules:
  - The elements are drawn in list order on the whole data format; the ground runs into the bleed
    and the hidden strips, because a shop's cut and a cassette's grip both have tolerance.
  - Every element reports what it drew (``Drawn``): its box, its words, the real cap and x-heights
    of its face, its colour against the ground measured under it before it was drawn, and for a
    photograph the resolution it actually prints at. ``rules`` judges the roll-up from these, not
    from what the config meant.
  - Type that does not fit its band stops the build, as on the flyer: a clipped line on a two-metre
    banner is a reprint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageStat

from asset_engine.printing.elements import Box, background, layout_stack, paste_qr
from asset_engine.printing.links import Campaign, campaign_links
from asset_engine.project import get_project
from asset_engine.rollups.config import RollupConfig, RollupConfigError, read_copy
from asset_engine.store.compose import apply_scrim, cover
from asset_engine.store.config import ScrimSpec, TextStyle
from asset_engine.store.render import layout_headline


class RollupOverflowError(RollupConfigError):
    """Copy does not fit its band even at the smallest size."""


@dataclass(frozen=True)
class Raster:
    """A picture on the banner, and the resolution it prints at once scaled to its place."""

    path: Path
    source_px: tuple[int, int]
    placed_px: tuple[int, int]
    effective_dpi: float


@dataclass
class Drawn:
    """What one element put on the canvas, for the rules."""

    id: str
    type: str
    role: str = ""
    ## The drawn box on the bled canvas, or ``None`` for an element nothing has to survive (ground,
    ## photograph, panel).
    box: Box | None = None
    text: str = ""
    cap_height_px: int = 0
    x_height_px: int = 0
    family: str = ""
    color: tuple[int, int, int] | None = None
    ## The mean colour of the canvas under the text box, measured before the text was drawn.
    ground: tuple[int, int, int] | None = None
    raster: Raster | None = None


@dataclass(frozen=True)
class ComposedRollup:
    image: Image.Image
    drawn: list[Drawn] = field(default_factory=list)


def _metrics(font: ImageFont.FreeTypeFont) -> tuple[int, int, str]:
    """Cap height and x-height of ``font`` in pixels, as drawn, and its family name."""
    cap = -font.getbbox("H", anchor="ls")[1]
    x_height = -font.getbbox("x", anchor="ls")[1]
    return cap, x_height, font.getname()[0]


def _ground_under(canvas: Image.Image, box: Box) -> tuple[int, int, int]:
    left, top, right, bottom = box
    region = canvas.crop((max(0, left), max(0, top), min(canvas.width, right), min(canvas.height, bottom)))
    mean = ImageStat.Stat(region.convert("RGB")).mean
    return (round(mean[0]), round(mean[1]), round(mean[2]))


def _x(config: RollupConfig, spec: dict[str, Any]) -> int:
    return config.sheet.x(spec["center_x_mm"] if "center_x_mm" in spec else spec["left_mm"])


def _background(canvas: Image.Image, config: RollupConfig, spec: dict[str, Any], **_) -> Drawn:
    ground = background(canvas.size, {"color": config.color(spec["color"])}, get_project().root)
    if "scrim" in spec:
        scrim = spec["scrim"]
        apply_scrim(ground, ScrimSpec(color=config.color(scrim["color"]), stops=tuple(map(tuple, scrim["stops"]))))
    canvas.paste(ground, (0, 0))
    return Drawn(id=spec["id"], type="background")


def _ramp(height: int, width: int, *, rising: bool) -> Image.Image:
    """A vertical 0 -> 255 mask (``rising`` from the top), for fading a band's edge into the ground."""
    column = Image.linear_gradient("L").resize((1, height), Image.BILINEAR)
    if not rising:
        column = column.transpose(Image.FLIP_TOP_BOTTOM)
    return column.resize((width, height), Image.NEAREST)


def _photo(canvas: Image.Image, config: RollupConfig, spec: dict[str, Any], **_) -> Drawn:
    """A photograph filling a band the full width of the data format, its edges faded into the ground.

    ``top_mm`` may be ``"edge"``: the band then runs to the top of the data format. ``shade`` darkens
    the photograph along its height (``stops`` as fractions of the band, 0 at its top), so type set
    over it keeps its contrast; ``fade_mm`` dissolves its top or bottom edge into what lies under it.
    """
    top = 0 if spec["top_mm"] == "edge" else config.floor_y(spec["top_mm"])
    bottom = config.floor_y(spec["bottom_mm"])
    size = (canvas.width, bottom - top)
    path = get_project().root / spec["asset"]
    source = Image.open(path).convert("RGBA")
    band = cover(source, size, anchor=spec.get("anchor", 0.5), anchor_x=spec.get("anchor_x", 0.5))
    scale = max(size[0] / source.width, size[1] / source.height)
    if "shade" in spec:
        shade = spec["shade"]
        apply_scrim(band, ScrimSpec(color=config.color(shade["color"]), stops=tuple(map(tuple, shade["stops"]))))

    mask = Image.new("L", size, 255)
    fade = spec.get("fade_mm", {})
    if fade.get("bottom"):
        height = config.sheet.px(fade["bottom"])
        mask.paste(_ramp(height, size[0], rising=False), (0, size[1] - height))
    if fade.get("top"):
        height = config.sheet.px(fade["top"])
        mask.paste(_ramp(height, size[0], rising=True), (0, 0))
    band.putalpha(Image.composite(band.getchannel("A"), Image.new("L", size, 0), mask))
    canvas.alpha_composite(band, (0, top))
    return Drawn(
        id=spec["id"],
        type="photo",
        raster=Raster(path, source.size, size, config.sheet.dpi / scale),
    )


def _panel(canvas: Image.Image, config: RollupConfig, spec: dict[str, Any], **_) -> Drawn:
    """A plain rounded shape — a band of colour behind a call to action."""
    sheet = config.sheet
    box = (sheet.x(spec["left_mm"]), config.floor_y(spec["top_mm"]), sheet.x(spec["right_mm"]), config.floor_y(spec["bottom_mm"]))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        box, radius=sheet.px(spec.get("radius_mm", 0)), fill=(*config.color(spec["color"]), 255)
    )
    canvas.alpha_composite(layer)
    return Drawn(id=spec["id"], type="panel")


def _image(canvas: Image.Image, config: RollupConfig, spec: dict[str, Any], **_) -> Drawn:
    """A picture (a logo) by its height, standing on ``bottom_mm``. ``trim`` drops a transparent margin."""
    sheet = config.sheet
    path = get_project().root / spec["asset"]
    art = Image.open(path).convert("RGBA")
    if spec.get("trim"):
        art = art.crop(art.getchannel("A").getbbox())
    source = art.size
    height = sheet.px(spec["height_mm"])
    art = art.resize((round(art.width * height / art.height), height), Image.LANCZOS)
    bottom = config.floor_y(spec["bottom_mm"])
    x = _x(config, spec)
    left = x - art.width // 2 if "center_x_mm" in spec else x
    canvas.alpha_composite(art, (left, bottom - height))
    return Drawn(
        id=spec["id"],
        type="image",
        role=spec.get("role", ""),
        box=(left, bottom - height, left + art.width, bottom),
        raster=Raster(path, source, art.size, sheet.dpi * source[1] / height),
    )


def _qr(canvas: Image.Image, config: RollupConfig, spec: dict[str, Any], *, links: dict, **_) -> Drawn:
    """The code to one of the campaign's links on its white card, optionally framed in a colour."""
    sheet = config.sheet
    if spec["link"] not in links:
        raise RollupConfigError(f"element {spec['id']!r} wants the {spec['link']!r} link; this app has {sorted(links)}")
    size = sheet.px(spec["size_mm"])
    center_x = _x(config, spec)
    top = config.floor_y(spec["bottom_mm"]) - size
    if "frame" in spec:
        frame = sheet.px(spec["frame"]["width_mm"])
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (center_x - size // 2 - frame, top - frame, center_x - size // 2 + size + frame, top + size + frame),
            radius=sheet.px(config.qr["corner_radius_mm"]) + frame,
            fill=(*config.color(spec["frame"]["color"]), 255),
        )
        canvas.alpha_composite(layer)
    box = paste_qr(
        canvas,
        links[spec["link"]].url,
        center_x=center_x,
        top=top,
        size=size,
        quiet_modules=config.qr["quiet_modules"],
        corner_radius=sheet.px(config.qr["corner_radius_mm"]),
        dark=config.color(config.qr["dark"]),
    )
    return Drawn(id=spec["id"], type="qr", role="qr", box=box)


def _lines(copy: dict[str, Any], key: str) -> list[str]:
    value = copy[key]
    return value if isinstance(value, list) else [value]


def _text(canvas: Image.Image, config: RollupConfig, spec: dict[str, Any], *, copy: dict, label: str, **_) -> Drawn:
    """One block of copy fitted into a band: shrunk until it fits, centred or set from the left."""
    sheet = config.sheet
    draw = ImageDraw.Draw(canvas)
    low, high = spec["band_mm"]
    color = config.color(spec["color"])
    style = TextStyle(
        band_top=config.floor_y(high),
        band_clearance=0,
        fallback_band_bottom=config.floor_y(low),
        max_width=sheet.px(spec["max_width_mm"]),
        color=color,
        base_size=sheet.px(spec["base_size_mm"]),
        min_size=sheet.px(spec["min_size_mm"]),
        shrink_step=max(1, sheet.px(spec["shrink_step_mm"])),
        line_spacing=spec["line_spacing"],
        max_lines=spec["max_lines"],
    )
    lines = _lines(copy, spec["copy"])
    layout = layout_headline(
        draw, lines, style, label=f"{label} {spec['id']}", fonts=config.fonts[spec["font"]], keep_lines=spec.get("keep_lines", False)
    )
    if layout.overflows:
        raise RollupOverflowError(
            f"[{label}] {spec['id']} needs {len(layout.lines)} lines at {layout.size}px and does not fit "
            f"its band — trim the copy or widen band_mm"
        )
    x = _x(config, spec)
    centred = "center_x_mm" in spec
    left = x - layout.width // 2 if centred else x
    box = (left, layout.ink_top, left + layout.width + (1 if centred else 0), layout.ink_bottom)
    ground = _ground_under(canvas, box)
    for index, line in enumerate(layout.lines):
        draw.text(
            (x, layout.origin_y + index * layout.line_height),
            line,
            font=layout.font,
            fill=color,
            anchor="ma" if centred else "la",
        )
    cap, x_height, family = _metrics(layout.font)
    return Drawn(
        id=spec["id"],
        type="text",
        role=spec["role"],
        box=box,
        text=" ".join(lines),
        cap_height_px=cap,
        x_height_px=x_height,
        family=family,
        color=color,
        ground=ground,
    )


def _list(canvas: Image.Image, config: RollupConfig, spec: dict[str, Any], *, copy: dict, label: str, **_) -> Drawn:
    """Short points set left-aligned at one shared size, each behind a round marker.

    ``one_line`` holds every point to a single line, shrinking the type instead of wrapping: a list
    read in passing is read as a column of short lines, and one point broken in two reads as two.
    """
    sheet = config.sheet
    draw = ImageDraw.Draw(canvas)
    low, high = spec["band_mm"]
    color = config.color(spec["color"])
    items = [[item] for item in copy[spec["copy"]]]
    marker = sheet.px(spec["marker"]["size_mm"])
    indent = marker + sheet.px(spec["marker"]["gap_mm"])
    min_size = sheet.px(spec["min_size_mm"])
    step = max(1, sheet.px(spec["shrink_step_mm"]))
    size = sheet.px(spec["base_size_mm"])
    while True:
        stack = layout_stack(
            draw,
            items,
            [config.fonts[spec["font"]]] * len(items),
            band_top=config.floor_y(high),
            band_bottom=config.floor_y(low),
            max_width=sheet.px(spec["max_width_mm"]) - indent,
            base_size=size,
            min_size=min_size,
            step=step,
            line_spacing=spec["line_spacing"],
            paragraph_gap=spec["item_gap"],
            color=color,
            label=f"{label} {spec['id']}",
        )
        wrapped = any(len(lines) > 1 for lines in stack.paragraphs)
        if not (spec.get("one_line") and wrapped) or stack.size <= min_size:
            break
        size = stack.size - step
    if spec.get("one_line") and wrapped:
        raise RollupOverflowError(f"[{label}] {spec['id']}: a point does not fit one line at {stack.size}px — shorten it")
    if stack.overflows:
        raise RollupOverflowError(f"[{label}] {spec['id']} does not fit its band at {stack.size}px — trim the points")
    left = sheet.x(spec["left_mm"])
    box = (left, stack.top, left + indent + stack.width, stack.top + stack.height)
    ground = _ground_under(canvas, box)
    cap, x_height, family = _metrics(stack.fonts[0])
    marker_color = (*config.color(spec["marker"]["color"]), 255)
    y = stack.top
    for lines, font in zip(stack.paragraphs, stack.fonts):
        ## The marker sits on the first line, centred on its x-height: that is where the eye reads it.
        center_y = y + font.getbbox("x", anchor="la")[1] + x_height / 2
        draw.ellipse((left, center_y - marker / 2, left + marker, center_y + marker / 2), fill=marker_color)
        for line in lines:
            draw.text((left + indent, y), line, font=font, fill=color, anchor="la")
            y += stack.line_height
        y += stack.gap
    return Drawn(
        id=spec["id"],
        type="list",
        role=spec["role"],
        box=box,
        text=" ".join(item[0] for item in items),
        cap_height_px=cap,
        x_height_px=x_height,
        family=family,
        color=color,
        ground=ground,
    )


ELEMENT_TYPES = {
    "background": _background,
    "photo": _photo,
    "panel": _panel,
    "image": _image,
    "qr": _qr,
    "text": _text,
    "list": _list,
}


def compose_rollup(config: RollupConfig, campaign: Campaign) -> ComposedRollup:
    """The whole data format of one campaign's roll-up, and what each element drew."""
    copy = read_copy(campaign.language)
    links = campaign_links(config, campaign)
    canvas = Image.new("RGBA", config.sheet.canvas, (255, 255, 255, 255))
    drawn = []
    for spec in config.elements:
        if spec["type"] not in ELEMENT_TYPES:
            raise RollupConfigError(f"element {spec['id']!r} has type {spec['type']!r}; there are {sorted(ELEMENT_TYPES)}")
        drawn.append(ELEMENT_TYPES[spec["type"]](canvas, config, spec, copy=copy, links=links, label=campaign.name))
    return ComposedRollup(image=canvas.convert("RGB"), drawn=drawn)


__all__ = ["ELEMENT_TYPES", "ComposedRollup", "Drawn", "Raster", "RollupOverflowError", "compose_rollup"]
