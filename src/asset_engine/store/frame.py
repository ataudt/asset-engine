"""Draw the phone bezel around a raw app capture.

Layer: asset engine.
Rules:
  - Pillow only. The screen's rounded corners need a mask whatever happens, so stroking the bezel
    from the same helper is free — a vector rasterizer (cairosvg -> libcairo) would be a native
    dependency bought for nothing.
  - The shape is transcribed from a phone-frame SVG (NutriSpy's ``images/store-pieces/phone-frame.svg``) as
    *fractions* of the frame's stroked bounding box, so the constants below survive any rescale.
    The artwork scales the frame non-uniformly to hug a 1242x2688 capture and keeps the stroke at a
    constant width, which is why the stroke is a plain pixel number and not a fraction.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw

## --- Transcribed from phone-frame.svg -------------------------------------------------------
## viewBox is "-341.818 -729.51 683.637 1732.61", which is exactly the stroked bounding box, so
## every fraction below is (svg length / 683.637) horizontally or (/ 1732.61) vertically. The SVG
## paths are centerlines; half the 17.559 stroke is added to each so the fractions describe the
## *visible* silhouette, which is what gets drawn here.
_SVG_W, _SVG_H, _SVG_STROKE = 683.637, 1732.610, 17.559

CORNER_RX_FRACTION = (57.858 + _SVG_STROKE / 2) / _SVG_W  # 0.09748
CORNER_RY_FRACTION = (68.838 + _SVG_STROKE / 2) / _SVG_H  # 0.04480
NOTCH_WIDTH_FRACTION = (243.660 + _SVG_STROKE) / _SVG_W  # 0.38209
NOTCH_HEIGHT_FRACTION = (57.514 + _SVG_STROKE) / _SVG_H  # 0.04333
NOTCH_RX_FRACTION = (20.080 + _SVG_STROKE / 2) / _SVG_W  # 0.04222
NOTCH_RY_FRACTION = (23.456 + _SVG_STROKE / 2) / _SVG_H  # 0.01860

## Supersample before rotating and downsampling; the bezel is an 18px stroke with a hard edge and
## aliases badly at 1x.
SUPERSAMPLE = 2


@dataclass(frozen=True)
class FrameStyle:
    """Bezel appearance. ``stroke`` is in canvas pixels — see the module docstring."""

    stroke: int
    color: tuple[int, int, int]


def _rounded_rect_mask(size: tuple[int, int], rx: float, ry: float) -> Image.Image:
    """An ``L`` mask of a rectangle with *elliptical* corners.

    ``ImageDraw.rounded_rectangle`` only does circular corners. The SVG's are elliptical, and at the
    aspect the artwork uses they land close to circular — but deriving rx and ry independently keeps
    the shape right if the frame is ever placed at a different aspect.
    """
    width, height = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    rx_i, ry_i = round(rx), round(ry)
    draw.rectangle((rx_i, 0, width - 1 - rx_i, height - 1), fill=255)
    draw.rectangle((0, ry_i, width - 1, height - 1 - ry_i), fill=255)
    for x0, y0, start in (
        (0, 0, 180),
        (width - 1 - 2 * rx_i, 0, 270),
        (0, height - 1 - 2 * ry_i, 90),
        (width - 1 - 2 * rx_i, height - 1 - 2 * ry_i, 0),
    ):
        draw.pieslice((x0, y0, x0 + 2 * rx_i, y0 + 2 * ry_i), start, start + 90, fill=255)
    return mask


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale ``image`` to fill ``size`` and center-crop the overflow — never distort."""
    width, height = size
    scale = max(width / image.width, height / image.height)
    scaled = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    left = (scaled.width - width) // 2
    top = (scaled.height - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def screen_height_for(capture: Image.Image, screen_width: int) -> int:
    """How tall the screen rect must be to show ``capture`` at ``screen_width`` undistorted."""
    return round(screen_width * capture.height / capture.width)


def render_frame(
    capture: Image.Image,
    *,
    screen_size: tuple[int, int],
    style: FrameStyle,
) -> Image.Image:
    """Return an RGBA image of ``capture`` inside the bezel, sized screen + stroke on all sides."""
    scale = SUPERSAMPLE
    stroke = style.stroke * scale
    screen_w, screen_h = screen_size[0] * scale, screen_size[1] * scale
    outer_w, outer_h = screen_w + 2 * stroke, screen_h + 2 * stroke

    outer_rx = outer_w * CORNER_RX_FRACTION
    outer_ry = outer_h * CORNER_RY_FRACTION

    frame = Image.new("RGBA", (outer_w, outer_h), (0, 0, 0, 0))
    ## The bezel is the outer silhouette painted flat; the screen is then punched into it, so the
    ## stroke is whatever is left between the two rounded rects.
    frame.paste(
        Image.new("RGBA", (outer_w, outer_h), (*style.color, 255)),
        (0, 0),
        _rounded_rect_mask((outer_w, outer_h), outer_rx, outer_ry),
    )
    frame.paste(
        _cover(capture.convert("RGB"), (screen_w, screen_h)),
        (stroke, stroke),
        _rounded_rect_mask((screen_w, screen_h), max(0.0, outer_rx - stroke), max(0.0, outer_ry - stroke)),
    )

    ## The notch hangs from the top edge: square where it meets the bezel, rounded at the bottom.
    ## Drawn as a rounded rect pushed up by its own corner height, so the top corners fall off the
    ## frame entirely instead of pinching in.
    notch_rx = outer_w * NOTCH_RX_FRACTION
    notch_ry = outer_h * NOTCH_RY_FRACTION
    notch_w = round(outer_w * NOTCH_WIDTH_FRACTION)
    notch_h = round(outer_h * NOTCH_HEIGHT_FRACTION) + round(notch_ry)
    notch = _rounded_rect_mask((notch_w, notch_h), notch_rx, notch_ry)
    frame.paste(
        Image.new("RGBA", notch.size, (*style.color, 255)),
        ((outer_w - notch_w) // 2, -round(notch_ry)),
        notch,
    )

    return frame.resize((outer_w // scale, outer_h // scale), Image.LANCZOS)


__all__ = ["FrameStyle", "render_frame", "screen_height_for"]
