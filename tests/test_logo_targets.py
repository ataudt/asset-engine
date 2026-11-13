from __future__ import annotations

import math

from PIL import Image, ImageDraw
import pytest

from asset_engine.logos.config import targets
from asset_engine.logos.derive import cut_out, render

KEY = (255, 0, 255)
BACKGROUND = (20, 33, 61)


def _master() -> Image.Image:
    """A draft as a model paints one: an off-centre, lopsided motif on a key that is not exactly the
    configured colour, with a long thin arm, so its bounding circle is not its bounding box's."""
    master = Image.new("RGB", (1024, 1024), (252, 40, 252))
    draw = ImageDraw.Draw(master)
    draw.ellipse((220, 300, 640, 720), fill=(224, 152, 36))
    draw.polygon([(600, 480), (900, 180), (930, 210), (640, 520)], fill=(255, 190, 110))
    return master


@pytest.mark.parametrize("target", list(targets().values()), ids=lambda target: target.name)
def test_every_target_keeps_the_motif_inside_the_circle_its_platform_never_masks(target):
    """Each platform names a centred circle no device mask clips (Android: the inner 66 of 108 dp;
    an Android 12 splash: 192 of 288 dp; iOS and Play: the inscribed circle) — ``platforms.json``.
    At the largest fill an app may set, every pixel the motif touches must still overlap that circle.

    Fails when a target is added or a placement change (resampling, rounding, a margin) lets the motif
    spill past the circle, where a launcher's mask would cut it off on some device.
    """
    motif, findings = cut_out(_master(), KEY)
    image = render(motif, target, background=BACKGROUND, fill=1.0)

    assert findings == []
    assert image.size == (target.size, target.size)
    assert image.mode == ("RGBA" if target.layer in ("foreground", "monochrome") else "RGB")
    if target.layer == "background":
        return

    radius = target.safe_circle * target.size / 2
    centre = target.size / 2
    pixels = image.load()
    outside = []
    for y in range(target.size):
        for x in range(target.size):
            value = pixels[x, y]
            drawn = value[3] > 0 if image.mode == "RGBA" else value != BACKGROUND
            ## A pixel reaches into the circle while its centre lies within half a diagonal of the rim.
            if drawn and math.hypot(x + 0.5 - centre, y + 0.5 - centre) > radius + math.sqrt(0.5):
                outside.append((x, y))
    assert outside == [], f"{len(outside)} motif pixels outside the safe circle, e.g. {outside[:5]}"
