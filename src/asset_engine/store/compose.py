"""Build a text-free store mockup from pieces: background + framed capture + mascot.

Layer: asset engine.
Rules:
  - The only per-language input is a plain app capture under ``<raw_dir>/<lang>/`` — no
    background, frame or decoration. Everything else is shared artwork. The captures are named for
    what they show, in the capturer's language, so ``captures.json`` maps caption -> file name per
    locale. A slot that locale has not shot is borrowed along ``config.capture_fallbacks``. The chain
    ends in a capture only if the app keeps one complete set (NutriSpy commits ``captures/de/``);
    where it does not, the slot is reported missing.
  - Geometry lives in ``config.json``. Nothing here hardcodes a slot.
  - The whole set stands on one *photograph* (``PhotoGround``), widened to as many canvases as
    there are background names, each slot cutting its own window — so slots 1-3 are one continuous
    billboard and not three crops of the same picture. The photograph only reaches so far, and what
    carries it the rest of the way is its own mirror image under a rising blur, dissolving into the
    colour its right edge states — see ``_ground_panorama``. Until Oct 2026 the set stood on three
    painted plates instead, stitched for a wider canvas; the photograph replaced them outright.
  - The mockup reports where the phone's top edge landed. The headline band ends there, and that
    is geometry the config already states; reading it back off the pixels assumed a pale ground and
    collapsed the band to nothing on a dark one.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path

from dataclasses import dataclass, replace

from PIL import Image, ImageDraw, ImageFilter

from asset_engine.store.config import (
    Callout,
    Chip,
    ChipStyle,
    FrameSpec,
    GroundExtend,
    Placement,
    Scanner,
    ScannerStyle,
    ScreenshotConfig,
    ScrimSpec,
    ShadowSpec,
    Slot,
)
from asset_engine.store.fonts import font_for_text
from asset_engine.store.frame import FrameStyle, render_frame, screen_height_for
from asset_engine.project import get_project

## A capture whose aspect is further than this from the *canvas's* is a wrong-device capture, not a
## rounding difference; ``_cover`` would silently crop a strip off it. The canvas is the reference
## because it is the only aspect fixed independently of the capture: the bezel's height is computed
## from the capture itself (``screen_height_for``), so comparing against that compares a number with
## itself — which is exactly what this check did until Sep 2026, passing everything.
ASPECT_TOLERANCE = 0.02

## Shapes the ``GroundExtend.prefade`` ramp. Blending a blurred copy is linear in opacity but not in
## perceived focus — at half opacity the sharp original still reads through it — so a straight ramp
## spends most of its length doing nothing visible and then arrives at the fold still crisper than
## the far side. Below 1 this front-loads the fade: at 1.0 the photograph reaches the fold at 0.62
## mean |d/dx| against the continuation's 0.43, and at 0.45 it reaches it at 0.42. The table in
## ``_ground_panorama`` has the measurements.
PREFADE_GAMMA = 0.45

class RawCaptureError(RuntimeError):
    """A raw capture is missing, or is not the shape the frame expects."""


@dataclass(frozen=True)
class Mockup:
    """A text-free slot, and the row its phone starts on — the bottom of the headline band."""

    image: Image.Image
    phone_top: int


def _match_key(stem: str) -> str:
    """A stem reduced to what identifies it: case and Unicode composition do not.

    Captures come off a phone and land as ``.PNG`` or ``.png`` depending on how they were copied,
    so the extension and case cannot be part of the key. Neither can composition: macOS hands back
    ``ä`` as a decomposed a + combining diaeresis, while a stem typed into ``captures.json`` is
    composed, so ``Nährstoffgleichgewicht`` compares unequal to itself and the slot quietly falls
    through to the next language in the chain.
    """
    return unicodedata.normalize("NFC", stem).casefold()


def find_capture(directory: Path, stem: str) -> Path | None:
    """The file in ``directory`` named ``stem``, whatever its case, accents' spelling or extension.

    ``sorted`` keeps the choice stable if two spellings ever sit side by side.
    """
    if not directory.is_dir():
        return None
    target = _match_key(stem)
    return next(
        (entry for entry in sorted(directory.iterdir()) if entry.is_file() and _match_key(entry.stem) == target),
        None,
    )


def resolve_capture(config: ScreenshotConfig, language: str, slot: Slot) -> tuple[Path, str] | None:
    """This slot's capture and the language it actually came from, or ``None`` if nobody has one.

    A locale's set is rarely complete — the FAB menu was only ever shot in German — so a missing
    screen falls through the ``capture_fallbacks`` chain and borrows another language's shot.

    A page's own stems are walked down the whole chain before the main set is asked at all. The
    other order answers a page's missing French stem with the *main* set's French capture, which
    is a different screen: Oct 2026, two pages showed the main set's coffee under a poke bowl's
    numbers in French, because the page's English poke bowl was never reached.
    """
    candidates = tuple(dict.fromkeys((language, *config.capture_fallbacks)))
    for stem_of in (config.variant_capture_stem, config.capture_stem):
        for candidate in candidates:
            stem = stem_of(candidate, slot)
            if stem is None:
                continue
            found = find_capture(config.raw_dir / candidate, stem)
            if found is not None:
                return found, candidate
    return None


def raw_path(config: ScreenshotConfig, language: str, slot: Slot) -> Path:
    """Where this slot's capture is, or the path whose absence explains why there is none."""
    resolved = resolve_capture(config, language, slot)
    if resolved is not None:
        return resolved[0]
    ## Nothing in this locale or the fallbacks: name a path that does not exist, so the caller's
    ## "not a file" branch reports which locale and slot came up empty. The suffix is dropped
    ## because ``slot.template`` is the *output* name: captures are PNG off a phone, and pointing at
    ## a rendered frame's extension would send somebody looking for the wrong file.
    return config.raw_dir / language / Path(slot.template).stem


def paste_rotated(
    base: Image.Image,
    layer: Image.Image,
    center: tuple[float, float],
    rotation: float,
) -> tuple[int, int]:
    """Spin ``layer`` about its own center, paste it on ``center``, and say where it landed.

    Rotation with ``expand`` grows the box, so the center is what is held fixed — pinning a corner
    instead would make the artwork drift every time an angle is retuned. The returned top-left is
    the expanded box's, and for a rotated rectangle its top edge is the layer's topmost inked row:
    a corner of the rectangle touches it. That is what makes it usable as the headline band's floor.
    """
    if rotation:
        layer = layer.rotate(rotation, resample=Image.BICUBIC, expand=True)
    at = (round(center[0] - layer.width / 2), round(center[1] - layer.height / 2))
    base.alpha_composite(layer, at)
    return at


def center_of(placement: Placement, size: tuple[int, int]) -> tuple[float, float]:
    """Center of a layer of ``size`` placed at ``placement``'s top-left, before rotation.

    A placement that states a ``bottom`` is anchored by its foot instead: the height is only known
    here, once the layer has been built, which is the whole reason the choice cannot be made in the
    config. See ``Placement``.
    """
    if placement.bottom is not None:
        return (placement.left + size[0] / 2, placement.bottom - size[1] / 2)
    return (placement.left + size[0] / 2, placement.top + size[1] / 2)


def shadow_for(frame: Image.Image, shadow: ShadowSpec) -> Image.Image:
    """A blurred silhouette of ``frame``, for the drop shadow the clean plates do not carry."""
    pad = shadow.blur * 3
    canvas = Image.new("RGBA", (frame.width + 2 * pad, frame.height + 2 * pad), (0, 0, 0, 0))
    silhouette = Image.new("RGBA", frame.size, (*shadow.color, round(255 * shadow.opacity)))
    canvas.paste(silhouette, (pad, pad), frame.getchannel("A"))
    return canvas.filter(ImageFilter.GaussianBlur(shadow.blur))


def _ramp(size: tuple[int, int], *, horizontal: bool = False) -> Image.Image:
    """A 0->255 mask of ``size``, used to fade one layer into another along one axis."""
    gradient = Image.linear_gradient("L").rotate(180)
    if horizontal:
        gradient = gradient.transpose(Image.ROTATE_90).transpose(Image.FLIP_LEFT_RIGHT)
    return gradient.resize(size, Image.BILINEAR)


def _opacity_at(stops: tuple[tuple[float, float], ...], fraction: float) -> float:
    """``stops`` read as a piecewise-linear curve, flat outside its ends."""
    if fraction <= stops[0][0]:
        return stops[0][1]
    for (left_at, left_value), (right_at, right_value) in zip(stops, stops[1:]):
        if fraction <= right_at:
            span = right_at - left_at
            share = 0.0 if span <= 0 else (fraction - left_at) / span
            return left_value + (right_value - left_value) * share
    return stops[-1][1]


def apply_scrim(canvas: Image.Image, scrim: ScrimSpec) -> None:
    """Wash ``canvas`` with ``scrim``, in place, so type can sit on a photograph.

    The mask is built one row at a time and widened, because the curve is vertical only — the same
    shape as the website's ``--hero-scrim`` gradient, which this follows down to the 0.62 at the top.
    """
    width, height = canvas.size
    column = Image.new("L", (1, height))
    pixels = column.load()
    for y in range(height):
        pixels[0, y] = round(255 * _opacity_at(scrim.stops, y / max(1, height - 1)))
    wash = Image.new("RGBA", canvas.size, (*scrim.color, 255))
    canvas.paste(wash, (0, 0), column.resize((width, height), Image.NEAREST))


def cover(
    image: Image.Image,
    size: tuple[int, int],
    *,
    anchor: str | float,
    anchor_x: float = 0.5,
) -> Image.Image:
    """Scale ``image`` to cover ``size`` and crop the overflow, keeping ``anchor`` in view.

    ``anchor`` is ``"bottom"``, anything else for the middle, or a number from 0 (top) to 1
    (bottom) saying how far down the vertical overflow the window sits. ``anchor_x`` does the same
    across, from 0 (left) to 1 (right); the middle unless a caller wants another cut.

    Every consumer of this package that puts an image on a canvas it was not drawn for goes
    through here: the feature graphic cropping its 1536x1024 plate to 1024x500 (``bottom`` is what
    keeps the food table at its foot), the print flyer filling its page, and a design's photographic
    ground (``ground_panorama``).
    """
    scale = max(size[0] / image.width, size[1] / image.height)
    scaled = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    left = int((scaled.width - size[0]) * anchor_x)
    share = 1.0 if anchor == "bottom" else float(anchor) if isinstance(anchor, (int, float)) else 0.5
    top = int((scaled.height - size[1]) * share)
    return scaled.crop((left, top, left + size[0], top + size[1]))


@lru_cache(maxsize=4)
def _ground_panorama(
    asset: Path,
    canvas_height: int,
    width: int,
    extend: GroundExtend,
    shift: int = 0,
    zoom: float = 1.0,
    anchor: float = 1.0,
) -> Image.Image:
    """One photograph at ``canvas_height``, carried out to ``width`` by its own mirror image.

    The photograph only reaches so far — a 2:3 frame at 2688px tall is 1792px wide, and a billboard
    of three 1242px canvases wants 3726. What carries it the rest of the way is built in three
    layers, each with a ramp, so that nothing in it is a decision about colour:

    1. the photograph mirror-tiled rightward, so the continuation's first column is the last column
       of the photograph reflected and the junction is continuous by construction;
    2. that continuation blurred, faded in over ``blend`` pixels, which reads as the depth of field
       falling off rather than as a cut;
    3. the mean colour of the photograph's rightmost ``edge`` pixels — averaged to one column and
       stretched — faded in across the whole continuation, so the far end *is* the photograph's own
       vertical colour profile.

    ``prefade`` adds a step 0 that softens the photograph before it folds, and then layer 2 is a flat
    blur rather than a ramp. What it is worth was measured as mean |d/dx| per column, on the feature
    graphic's own window — the fold is at x=373, and the continuation settles at 0.43:

    ======================================  ==================  =================
    column band                             before (no prefade)  with prefade 60
    ======================================  ==================  =================
    last 10 columns of the photograph       0.99                 0.42
    first 10 columns of the continuation    0.07                 0.08
    ======================================  ==================  =================

    So the picture used to arrive at the fold more than twice as sharp as the ground it hands over
    to, which is what made the mirror findable; now it arrives at the continuation's own figure. Two
    traps are priced into those numbers: the fade must be masked out of a blur of the *whole* picture
    (a Gaussian this wide has nothing but a replicated edge to read on a 60px crop), and the tail
    must be tiled from the photograph *before* it is faded, or its first columns go through the blur
    twice and the continuation is at its flattest exactly where it meets the picture.

    Cached like ``_panorama``: every slot of every locale wants the same one.
    """
    photo = Image.open(asset).convert("RGBA")
    scaled_height = round(canvas_height * zoom)
    real = photo.resize((round(photo.width * scaled_height / photo.height), scaled_height), Image.LANCZOS)
    ## ``anchor`` spends the vertical overflow — 1.0 keeps the foot, where the food is — and
    ## ``shift`` drops what lies left of the first frame, so the mirror below still starts at the
    ## photograph's own right edge.
    top = round((scaled_height - canvas_height) * anchor)
    real = real.crop((shift, top, real.width, top + canvas_height))
    real_width = real.width

    ## 0. the photograph goes out of focus *before* it folds, where ``prefade`` asks for it. A mirror
    ## is colour-continuous by construction, but a sharp reflection is still readable as one — the
    ## axis is the first thing the eye finds on a small canvas. Softened on the way in, there is no
    ## edge to find.
    ##
    ## ``sharp`` is what the continuation is tiled from, and it is kept because the tail must be
    ## blurred exactly once. Mirroring the faded picture instead put already-soft columns through the
    ## blur a second time, so the continuation was at its flattest in the very place it meets the
    ## photograph — a dead band at the fold, measurably blurrier than the tail further out.
    sharp = real.copy() if extend.prefade > 0 else real
    if extend.prefade > 0 and real_width > extend.prefade:
        strip_left = real_width - extend.prefade
        ## The whole picture is blurred and then masked back in, rather than a 60px strip blurred on
        ## its own: a Gaussian this wide reads far outside the strip, and on a crop it has only the
        ## edge column replicated to read, so the strip came out flatter than the continuation it is
        ## supposed to meet.
        blurred = real.filter(ImageFilter.GaussianBlur(extend.blur))
        ## The mask is eased rather than linear. Alpha-blending a blurred copy is linear in opacity
        ## but not in *perceived* focus — at half opacity the sharp original still shows through and
        ## the picture reads as sharp, so a straight ramp spends most of its length doing nothing
        ## visible and then arrives at the fold still crisper than the far side. ``PREFADE_GAMMA``
        ## below 1 front-loads it, so focus is given up early and the two sides meet at one.
        ramp = _ramp((extend.prefade, canvas_height), horizontal=True).point(
            lambda value: round(255 * (value / 255) ** PREFADE_GAMMA)
        )
        real.paste(blurred.crop((strip_left, 0, real_width, canvas_height)), (strip_left, 0), ramp)
    if real_width >= width:
        return real

    ## 1. mirror-tile rightward, alternating, so every junction meets its own reflection
    tail_width = width - real_width
    tail = Image.new("RGBA", (tail_width, canvas_height))
    x = 0
    flipped = True
    while x < tail_width:
        piece = sharp.transpose(Image.FLIP_LEFT_RIGHT) if flipped else sharp
        tail.paste(piece, (x, 0))
        x += real_width
        flipped = not flipped

    ## 2. blur. With ``prefade`` the picture is already soft where it folds, so the continuation is
    ## blurred throughout — ramping up again from sharp would undo the softening and put a second,
    ## sharper copy of the seam one ``blend`` to the right of the first. Without it, the original
    ## behaviour: brought up over the first ``blend`` pixels.
    if extend.prefade > 0:
        ## Blurred with the photograph's own last columns in front of it, then cropped back. Blurring
        ## the tail alone gave its first columns nothing to read but their own replicated edge, so
        ## the continuation came out *flatter* right where it meets the picture than it is further
        ## out — a dead band at the one place the two sides have to agree.
        context = extend.blur * 3
        padded = Image.new("RGBA", (context + tail_width, canvas_height))
        padded.paste(sharp.crop((real_width - context, 0, real_width, canvas_height)), (0, 0))
        padded.paste(tail, (context, 0))
        tail.paste(
            padded.filter(ImageFilter.GaussianBlur(extend.blur)).crop(
                (context, 0, context + tail_width, canvas_height)
            ),
            (0, 0),
        )
    else:
        blurred = tail.filter(ImageFilter.GaussianBlur(extend.blur))
        tail.paste(blurred, (0, 0), _ramp((tail_width, canvas_height), horizontal=True).point(
            lambda value, span=extend.blend, total=tail_width: min(255, round(value * total / span))
        ))

    ## 3. the photograph's own right edge, averaged to one column and stretched, faded in to the end
    edge = real.crop((real_width - extend.edge, 0, real_width, canvas_height))
    ## Averaged to one column, then blurred *before* it is widened: on a one-pixel-wide image a
    ## Gaussian can only act vertically, and it costs nothing there. Without it the profile keeps
    ## every row of the photograph it came from — the bright edge of the table arrives as a light
    ## stripe across the frame instead of as the gradient this is meant to be.
    field = edge.resize((1, canvas_height), Image.BOX).filter(ImageFilter.GaussianBlur(extend.blur))
    field = field.resize((tail_width, canvas_height), Image.BILINEAR)
    tail.paste(field, (0, 0), _ramp((tail_width, canvas_height), horizontal=True))

    panorama = Image.new("RGBA", (width, canvas_height))
    panorama.paste(real, (0, 0))
    panorama.paste(tail, (real_width, 0))
    return panorama


def ground_plate(
    asset: Path,
    size: tuple[int, int],
    *,
    extend: GroundExtend,
    shift: int,
    zoom: float,
    anchor: float,
) -> Image.Image:
    """A canvas-sized image of the photograph carried right across ``size[0]``.

    ``ground_window`` cuts one screenshot's share of the billboard; this is for a canvas that *is*
    the whole billboard — the feature graphic, which stands on the same picture at its own scale.
    Because ``_ground_panorama`` scales by the canvas height, passing the screenshots' own ``zoom``
    and ``anchor`` with a shorter canvas yields their picture outright, just smaller.

    The crop is not a formality: ``_ground_panorama`` is cached and ``apply_scrim`` paints in place,
    so handing the cached panorama itself to a caller would let the first locale's scrim bake into
    every later one, and into the other device built in the same process.
    """
    panorama = _ground_panorama(asset, size[1], size[0], extend, shift, zoom, anchor)
    return panorama.crop((0, 0, size[0], size[1]))


def ground_window(config: ScreenshotConfig, slot: Slot) -> Image.Image:
    """This slot's canvas-sized window into the one photograph the design puts under the set.

    The panorama is exactly one canvas per background name, so the windows land edge to edge:
    slots 1-3 read as one picture rather than as three crops of it.
    """
    ground = config.ground
    canvas_width, canvas_height = config.canvas
    names = list(config.backgrounds)
    panorama = _ground_panorama(
        get_project().root / ground.asset,
        canvas_height,
        canvas_width * len(names),
        ground.extend,
        ground.shift,
        ground.zoom,
        ground.anchor,
    )
    position = names.index(slot.background) / max(1, len(names) - 1)
    offset = round((panorama.width - canvas_width) * position)
    return panorama.crop((offset, 0, offset + canvas_width, canvas_height))


def background_plate(config: ScreenshotConfig, slot: Slot) -> Image.Image:
    """The canvas-sized background this slot is composed on."""
    plate = ground_window(config, slot)
    if config.scrim is not None:
        apply_scrim(plate, config.scrim)
    return plate


def frame_capture(
    config: ScreenshotConfig,
    capture_path: Path,
    *,
    phone_width: int,
    frame: FrameSpec | None = None,
) -> Image.Image:
    """The capture at ``capture_path``, scaled into its bezel — the phone layer, nothing under it.

    Takes a path so that a capture in no store slot can be framed too: the website shows screens
    the store has no caption for (NutriSpy's ``marketing/website/build_app_screens.py``).
    """
    if not capture_path.is_file():
        raise RawCaptureError(f"No raw capture: {capture_path}")

    spec = frame or config.frame
    frame_style = FrameStyle(stroke=spec.stroke, color=spec.color)
    capture = Image.open(capture_path)
    screen_width = phone_width - 2 * frame_style.stroke
    screen_height = screen_height_for(capture, screen_width)

    expected = config.capture_aspect
    actual = capture.width / capture.height
    if abs(actual - expected) / expected > ASPECT_TOLERANCE:
        raise RawCaptureError(
            f"{capture_path} is {capture.width}x{capture.height} (aspect {actual:.4f}); a phone "
            f"capture is {expected:.4f} — recapture on the right device rather than letting it crop"
        )

    return render_frame(capture, screen_size=(screen_width, screen_height), style=frame_style)


def framed_capture(
    config: ScreenshotConfig,
    language: str,
    slot: Slot,
    *,
    frame: FrameSpec | None = None,
    phone_width: int | None = None,
) -> Image.Image:
    """One slot's raw capture, scaled into its bezel — the phone layer, with no background under it.

    Shared with the feature graphic, which draws the same device on a 1024x500 canvas and therefore
    needs its own (much thinner) bezel and phone width; everything else about the layer is identical.
    """
    return frame_capture(
        config,
        raw_path(config, language, slot),
        phone_width=phone_width if phone_width is not None else slot.phone.width,
        frame=frame,
    )


## Pillow's rounded rectangles and arcs are not antialiased: a chip's corner and a scanner bracket
## are drawn this many times too large and scaled down, or a pale shape on a dark ground shows steps.
SUPERSAMPLE = 4


@lru_cache(maxsize=None)
def _locale_names(language: str) -> dict:
    return get_project().store_files().labels(language)


def locale_name(language: str, key: str) -> str:
    """The app's own word for ``key`` (a dotted path into the project's label table) in ``language``."""
    node = _locale_names(language)
    for part in key.split("."):
        node = node[part]
    if not isinstance(node, str):
        raise KeyError(f"{key} in the {language} label table is not a string")
    return node


def scanner_layer(scanner: Scanner, style: ScannerStyle) -> Image.Image:
    """Four corner brackets spanning ``scanner``'s box, on a transparent layer of that size."""
    scale = SUPERSAMPLE
    width, height = scanner.width * scale, scanner.height * scale
    stroke, arm = style.stroke * scale, style.arm * scale
    ## One rounded outline, of which only the corners are kept: the mask is four squares.
    outline = Image.new("L", (width, height), 0)
    ImageDraw.Draw(outline).rounded_rectangle(
        (stroke // 2, stroke // 2, width - 1 - stroke // 2, height - 1 - stroke // 2),
        radius=style.radius * scale,
        outline=255,
        width=stroke,
    )
    corners = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(corners)
    for x in (0, width - arm):
        for y in (0, height - arm):
            draw.rectangle((x, y, x + arm, y + arm), fill=255)
    alpha = Image.composite(outline, Image.new("L", (width, height), 0), corners)
    layer = Image.new("RGBA", (scanner.width, scanner.height), (*style.color, 255))
    layer.putalpha(alpha.resize((scanner.width, scanner.height), Image.LANCZOS))
    return layer


def _rounded(layer: Image.Image, radius: int) -> Image.Image:
    """``layer`` with its corners rounded, antialiased."""
    scale = SUPERSAMPLE
    mask = Image.new("L", (layer.width * scale, layer.height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1), radius=radius * scale, fill=255)
    layer.putalpha(mask.resize(layer.size, Image.LANCZOS))
    return layer


def callout_layer(capture_path: Path, callout: Callout, style: ChipStyle, shown: str) -> Image.Image:
    """``callout``'s rectangle of the raw capture, scaled to its width, corners rounded.

    ``shown`` is the language the capture came from, which picks the rectangle: a locale that
    borrows another's screen borrows the rectangle cut for it.
    """
    capture = Image.open(capture_path).convert("RGBA")
    left, top, right, bottom = callout.crop_for(shown)
    piece = capture.crop(
        (
            round(capture.width * left),
            round(capture.height * top),
            round(capture.width * right),
            round(capture.height * bottom),
        )
    )
    height = round(piece.height * callout.width / piece.width)
    return _rounded(piece.resize((callout.width, height), Image.LANCZOS), style.radius)


def chip_layer(language: str, chip: Chip, style: ChipStyle, *, label: str = "") -> Image.Image:
    """A drawn card: the app's name for ``chip.key``, and its value or second name under it."""
    name = locale_name(language, chip.key)
    detail = chip.value or chip.detail_prefix + locale_name(language, chip.detail_key)
    detail_size = round(chip.size * style.detail_scale)
    name_font = font_for_text(name, chip.size, label=label)
    detail_font = font_for_text(detail, detail_size, label=label)

    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    name_width = round(probe.textlength(name, font=name_font))
    detail_width = round(probe.textlength(detail, font=detail_font))
    pad_x, pad_y = style.padding
    gap = round(chip.size * 0.18)
    width = max(name_width, detail_width) + 2 * pad_x
    height = chip.size + gap + detail_size + 2 * pad_y

    card = Image.new("RGBA", (width, height), (*style.fill, 255))
    draw = ImageDraw.Draw(card)
    ## "mm" centres on the em box, so both lines sit where the sizes say whatever the script's ascent.
    draw.text((width / 2, pad_y + chip.size / 2), name, font=name_font, fill=style.color, anchor="mm")
    draw.text(
        (width / 2, pad_y + chip.size + gap + detail_size / 2),
        detail,
        font=detail_font,
        fill=style.limit_color if chip.limited else style.detail_color,
        anchor="mm",
    )
    return _rounded(card, style.radius)


def _paste_lifted(canvas: Image.Image, layer: Image.Image, placement, shadow: ShadowSpec) -> None:
    """``layer`` at ``placement`` with ``shadow`` under it, both spun together."""
    rotation = getattr(placement, "rotation", 0.0)
    at = (placement.left + layer.width / 2, placement.top + layer.height / 2)
    paste_rotated(canvas, shadow_for(layer, shadow), (at[0] + shadow.offset[0], at[1] + shadow.offset[1]), rotation)
    paste_rotated(canvas, layer, at, rotation)


def paste_furniture(
    canvas: Image.Image,
    config: ScreenshotConfig,
    language: str,
    slot: Slot,
    *,
    scanner_style: ScannerStyle,
    chip_style: ChipStyle,
    shown: str,
    capture_language: str | None = None,
) -> None:
    """Everything a frame carries over its phone, in the one z-order, drawn in place.

    The styles are arguments rather than read off ``config`` because the feature graphic draws the
    same furniture at its own scale: a 1024x500 banner needs brackets and cards that are *relatively*
    larger than the screenshots', not a proportional reduction of them, or nothing resolves.

    ``capture_language`` is whose screen is on the phone, where ``language`` is who is reading. They
    part company on the banner, which shows one locale's captures to all sixteen: the callout is cut
    out of the interface and must follow the capture, while a chip is drawn type, large enough to
    read, and follows the reader.
    """
    ## Scanner and chips sit over the phone and under the mascot: they are what the frame says, and
    ## the pose is the one thing allowed to stand in front of them.
    if slot.scanner is not None:
        _paste_lifted(canvas, scanner_layer(slot.scanner, scanner_style), slot.scanner, scanner_style.shadow)
    ## A chip names what is in the food on the screen, so the set follows the capture rather than the
    ## reader: a locale that shows a dish of its own gets that dish's labels, and one that borrows
    ## another locale's capture gets the labels that belong to what it is actually showing. The words
    ## are still the reader's — they come out of their own data.json.
    for chip in slot.chips_for(shown):
        card = chip_layer(language, chip, chip_style, label=f"{language} slot {slot.caption}")
        ## A chip stating ``right`` hangs off that edge, so the card is placed by the edge somebody
        ## chose rather than by the one the translation happens to produce. The width is only known
        ## here — see ``Chip.right``.
        if chip.right is not None:
            chip = replace(chip, left=chip.right - card.width)
        _paste_lifted(canvas, card, chip, chip_style.shadow)

    for callout in slot.callouts:
        piece = callout_layer(raw_path(config, capture_language or language, slot), callout, chip_style, shown)
        _paste_lifted(canvas, piece, callout, chip_style.shadow)

    ## The mascot is last: it overlaps the bezel on the billboard slots that carry one. Poses are
    ## placed fully inside the canvas — the stores put a gap between screenshots, so a pose bled off
    ## one edge to be "continued" on the neighbouring frame just reads as a cropped mascot.
    if slot.mascot is not None:
        mascot = Image.open(get_project().root / slot.mascot.asset).convert("RGBA")
        height = round(mascot.height * slot.mascot.width / mascot.width)
        mascot = mascot.resize((slot.mascot.width, height), Image.LANCZOS)
        paste_rotated(canvas, mascot, center_of(slot.mascot, mascot.size), slot.mascot.rotation)
    for sticker in slot.stickers:
        art = Image.open(get_project().root / sticker.asset).convert("RGBA")
        art = art.resize((sticker.width, round(art.height * sticker.width / art.width)), Image.LANCZOS)
        _paste_lifted(canvas, art, sticker, chip_style.shadow)


def compose_mockup(config: ScreenshotConfig, language: str, slot: Slot) -> Mockup:
    """Compose the text-free mockup for one slot of one language, and say where its phone starts."""
    canvas = background_plate(config, slot)

    ## Whose capture this frame actually shows: this locale's own, or one borrowed down the fallback
    ## chain. A frame without a phone shows none at all and keeps the reader's own locale.
    resolved = resolve_capture(config, language, slot) if slot.phone is not None else None
    shown = language if resolved is None else resolved[1]

    ## A frame without a phone is a picture: its headline band is the layout's own, since there is
    ## no device for it to end at.
    phone_top = config.text.fallback_band_bottom if slot.band_bottom is None else slot.band_bottom
    if slot.phone is not None:
        framed = framed_capture(config, language, slot)
        center = center_of(slot.phone, framed.size)
        shadow_offset = config.shadow.offset
        paste_rotated(
            canvas,
            shadow_for(framed, config.shadow),
            (center[0] + shadow_offset[0], center[1] + shadow_offset[1]),
            slot.phone.rotation,
        )
        _, phone_top = paste_rotated(canvas, framed, center, slot.phone.rotation)

    paste_furniture(
        canvas,
        config,
        language,
        slot,
        scanner_style=config.scanner_style,
        chip_style=config.chip_style,
        shown=shown,
    )

    ## The band ends at the phone and not at the mascot: slot 1's pose deliberately reaches up into
    ## it, and the headline is set to clear the device, not the artwork around it.
    return Mockup(image=canvas.convert("RGB"), phone_top=phone_top)


__all__ = [
    "ASPECT_TOLERANCE",
    "Mockup",
    "RawCaptureError",
    "apply_scrim",
    "background_plate",
    "callout_layer",
    "chip_layer",
    "locale_name",
    "scanner_layer",
    "center_of",
    "compose_mockup",
    "cover",
    "find_capture",
    "frame_capture",
    "framed_capture",
    "paste_furniture",
    "paste_rotated",
    "ground_plate",
    "ground_window",
    "raw_path",
    "resolve_capture",
    "shadow_for",
]
