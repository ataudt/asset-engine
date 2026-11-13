"""The icon set, derived from the master draft the app chose — Pillow only, byte for byte reproducible.

Layer: asset engine.
Rules:
  - The master is a motif on a flat ``key_color``. A model paints that colour only roughly (asked for
    #FF00FF, it paints about #FC28FC), so the key that is cut is measured off the master's border,
    the median per channel; the configured colour only checks that the border is a key at all.
    ``cut_out`` turns the key into transparency by colour distance, with a soft edge between
    ``KEY_CLEAR`` and ``KEY_SOLID`` and the key's tint taken back out of the edge pixels, then trims
    to what is left.
  - Each target places the motif by its bounding circle: the circle around the motif's centre that
    holds every visible pixel is scaled to ``fill`` times the target's safe circle, and the placed
    alpha is clipped to that safe circle, so resampling cannot spill a pixel past it.
  - A master that does not look like a draft on its key (a border that is not the key, a motif that
    runs off the edge) is still built; ``cut_out`` returns the findings for the build to print.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from PIL import Image, ImageChops, ImageDraw

from asset_engine.logos.config import RGB, LogoConfigError, Target, hex_color

## Colour distance (Euclidean, 0-255 per channel) from the measured key up to which a pixel is the
## key, and from which it is fully the motif. Image models encode a flat fill with noise of about 15;
## the band between the two is the anti-aliased edge.
KEY_CLEAR = 32.0
KEY_SOLID = 112.0
## How far the measured border may sit from the configured key and still be taken for it.
KEY_DRIFT = 96.0
## Share of the master's border that must read as the key for the draft to count as cut cleanly.
BORDER_KEYED = 0.9
## Supersampling for masks and clip circles.
SUPERSAMPLE = 4


@dataclass(frozen=True)
class Motif:
    """The cut-out motif, trimmed, and the radius from its centre that holds every visible pixel."""

    image: Image.Image
    radius: float


def _alpha(distance: float) -> float:
    if distance <= KEY_CLEAR:
        return 0.0
    if distance >= KEY_SOLID:
        return 1.0
    t = (distance - KEY_CLEAR) / (KEY_SOLID - KEY_CLEAR)
    return t * t * (3 - 2 * t)


def cut_out(master: Image.Image, key: RGB) -> tuple[Motif, list[str]]:
    """The motif of ``master`` with ``key`` made transparent, and what looked wrong on the way."""
    rgb = master.convert("RGB")
    width, height = rgb.size
    findings = []
    measured = _border_median(rgb)
    if math.dist(measured, key) <= KEY_DRIFT:
        key = measured
    else:
        findings.append(
            f"the master's border is {hex_color(measured)}, not near the key {hex_color(key)} — the draft "
            "was not drawn on the key, so the cut-out keeps part of its background"
        )
    kr, kg, kb = key
    pixels = []
    border_keyed = border_total = 0
    touches_edge = False
    for index, (r, g, b) in enumerate(rgb.get_flattened_data()):
        a = _alpha(math.sqrt((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2))
        x, y = index % width, index // width
        if x in (0, width - 1) or y in (0, height - 1):
            border_total += 1
            border_keyed += a == 0.0
            touches_edge = touches_edge or a > 0.5
        if a == 0.0:
            pixels.append((0, 0, 0, 0))
        elif a == 1.0:
            pixels.append((r, g, b, 255))
        else:
            ## The edge pixel is a·motif + (1-a)·key; take the key back out.
            pixels.append((
                max(0, min(255, round((r - (1 - a) * kr) / a))),
                max(0, min(255, round((g - (1 - a) * kg) / a))),
                max(0, min(255, round((b - (1 - a) * kb) / a))),
                round(a * 255),
            ))
    cut = Image.new("RGBA", rgb.size)
    cut.putdata(pixels)

    keyed_share = border_keyed / border_total
    if not findings and keyed_share < BORDER_KEYED:
        findings.append(
            f"only {keyed_share:.0%} of the master's border is the key colour — the draft was not drawn "
            "on a flat key, so the cut-out keeps part of its background"
        )
    if touches_edge:
        findings.append("the motif reaches the master's edge — the model may have cropped it")

    box = cut.getchannel("A").getbbox()
    if box is None:
        raise LogoConfigError("nothing is left of the master once the key colour is cut away")
    trimmed = cut.crop(box)
    return Motif(trimmed, _bounding_radius(trimmed)), findings


def _border_median(rgb: Image.Image) -> RGB:
    """The median colour of the image's outermost pixel ring, per channel."""
    width, height = rgb.size
    ring = [rgb.getpixel((x, y)) for x in range(width) for y in (0, height - 1)]
    ring += [rgb.getpixel((x, y)) for y in range(1, height - 1) for x in (0, width - 1)]
    middle = len(ring) // 2
    return tuple(sorted(pixel[channel] for pixel in ring)[middle] for channel in range(3))  # type: ignore[return-value]


def _bounding_radius(motif: Image.Image) -> float:
    """The largest distance from the motif's centre to a visible pixel's far corner."""
    width, height = motif.size
    cx, cy = width / 2, height / 2
    widest = 0.0
    for index, alpha in enumerate(motif.getchannel("A").get_flattened_data()):
        if alpha:
            x, y = index % width, index // width
            dx = max(abs(x - cx), abs(x + 1 - cx))
            dy = max(abs(y - cy), abs(y + 1 - cy))
            widest = max(widest, dx * dx + dy * dy)
    return math.sqrt(widest)


def _circle(size: int, diameter: float) -> Image.Image:
    """A centred, anti-aliased disc of ``diameter`` pixels on a ``size`` square, as an L mask."""
    big = size * SUPERSAMPLE
    r = diameter * SUPERSAMPLE / 2
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((big / 2 - r, big / 2 - r, big / 2 + r, big / 2 + r), fill=255)
    return mask.resize((size, size), Image.BOX)


def place(motif: Motif, target: Target, fill: float) -> Image.Image:
    """The motif on a transparent ``target.size`` square, its bounding circle at ``fill`` of the safe
    circle, clipped to the safe circle."""
    size = target.size
    safe = target.safe_circle * size
    scale = (fill * safe / 2) / motif.radius
    width = max(1, round(motif.image.width * scale))
    height = max(1, round(motif.image.height * scale))
    ## Premultiplied while resampling, so the transparent key's colour does not bleed into the edge.
    scaled = motif.image.convert("RGBa").resize((width, height), Image.LANCZOS).convert("RGBA")
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(scaled, (round((size - width) / 2), round((size - height) / 2)))
    canvas.putalpha(ImageChops.multiply(canvas.getchannel("A"), _circle(size, safe)))
    return canvas


def render(motif: Motif, target: Target, *, background: RGB, fill: float) -> Image.Image:
    """The file ``target`` asks for."""
    size = target.size
    if target.layer == "background":
        return Image.new("RGB", (size, size), background)
    placed = place(motif, target, fill)
    if target.layer == "foreground":
        return placed
    if target.layer == "monochrome":
        white = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        white.putalpha(placed.getchannel("A"))
        return white
    ground = Image.new("RGBA", (size, size), (*background, 255))
    ground.alpha_composite(placed)
    return ground.convert("RGB")


__all__ = ["KEY_CLEAR", "KEY_SOLID", "Motif", "cut_out", "place", "render"]
