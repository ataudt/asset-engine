"""Where the clips sit in the frame: one clip, two stacked, or one in a bubble over the other.

Layer: asset engine.
Rules:
  - ``full`` is the speaker alone. ``stack`` puts a second clip (a screen recording) above the
    speaker; ``pip`` fills the frame with the second clip and shows the speaker in a round bubble.
  - A clip fills its field and is cropped to it (``panel``); the anchor says which part stays. A
    phone's screen recording is taller than any field, so its track names the height to look at.
  - The second clip has no clock of its own in the result. Its track ties a stretch of it to a moment
    of the speech: a stretch longer than its slot is sped up to fit, a shorter one plays as recorded
    and then holds its last frame. It is never slowed down, which reads as a stutter on a screen.
  - In ``stack`` the speaker's field is the lower one, and the platforms cover its lower part with
    name, text and sound, so the anchor keeps the head at the top of the field and the captions sit
    on the seam.
  - The headline is drawn with a face the caller hands over: the engine holds none.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from PIL import Image, ImageDraw, ImageFont

from asset_engine.video.cuts import Timeline, parse_time

LAYOUTS = ("full", "stack", "pip")
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
TITLE_PAD, TITLE_RADIUS = 28, 26


class LayoutError(RuntimeError):
    """The layout or the second clip's track cannot be used as given."""


@dataclass(frozen=True)
class Layout:
    kind: str = "full"
    ## stack: the upper field's share of the height.
    split: float = 0.6
    ## Which part of the speaker's picture stays when it is cropped to its field, 0..1 per axis.
    face_anchor: tuple[float, float] = (0.5, 0.25)
    ## pip: the bubble's diameter as a share of the width, and where it sits in the safe zone.
    bubble: float = 0.36
    ## Left, and just above the captions' line.
    bubble_anchor: tuple[float, float] = (0.0, 0.75)
    ## Each second segment is enlarged by this share, so a cut reads as a change of shot.
    punch: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in LAYOUTS:
            raise LayoutError(f"layout {self.kind!r}: one of {', '.join(LAYOUTS)}")
        if self.kind == "stack" and not 0.2 <= self.split <= 0.8:
            raise LayoutError(f"split {self.split}: between 0.2 and 0.8")


@dataclass(frozen=True)
class ScreenSpan:
    ## When it starts, in the speech clip's raw time.
    at: float
    ## The stretch of the second clip; ``None`` continues where the last one ended / runs as recorded.
    source_start: float | None
    source_end: float | None
    focus: tuple[float, float]


@dataclass(frozen=True)
class Slot:
    """One span on the cut's clock: ``length`` seconds showing the source from ``source_start`` at ``speed``."""

    length: float
    source_start: float
    source_length: float
    speed: float
    focus: tuple[float, float]


def parse_screen_track(path: Path) -> list[ScreenSpan]:
    """The second clip's track, one span per line (``#`` starts a comment):

        00:00    von 00:21 bis 00:26   fokus=35%
        00:09.5  von 00:26             fokus=50%,60%
        00:14                          # goes on where the last one ended

    The first time is the speech clip's; ``von``/``bis`` (``from``/``until``) are the second clip's.
    ``fokus`` is the height to look at, or ``x,y``."""
    spans = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        try:
            at, start, end, focus = parse_time(toks[0]), None, None, (0.5, 0.5)
            i = 1
            while i < len(toks):
                t = toks[i].lower()
                if t in ("von", "from"):
                    i += 1
                    start = parse_time(toks[i])
                elif t in ("bis", "until"):
                    i += 1
                    end = parse_time(toks[i])
                elif t.startswith(("fokus=", "focus=")):
                    parts = [float(p.rstrip("%")) / 100 for p in t.split("=", 1)[1].split(",")]
                    focus = (parts[0], parts[1]) if len(parts) == 2 else (0.5, parts[0])
                else:
                    raise ValueError(f"unknown field {toks[i]!r}")
                i += 1
        except (IndexError, ValueError) as error:
            raise LayoutError(f"{path}:{lineno}: {error}: {raw.strip()}") from error
        if start is not None and end is not None and end <= start:
            raise LayoutError(f"{path}:{lineno}: ends before it starts")
        spans.append(ScreenSpan(at, start, end, focus))
    if not spans:
        raise LayoutError(f"{path}: no span")
    return sorted(spans, key=lambda s: s.at)


def slots(spans: list[ScreenSpan], timeline: Timeline, source_duration: float) -> list[Slot]:
    """The spans on the cut's clock. Without spans the whole second clip is fitted to the cut."""
    if not spans:
        spans = [ScreenSpan(0.0, 0.0, source_duration, (0.5, 0.5))]
    starts = [timeline.at(s.at) for s in spans]
    starts[0] = 0.0
    out, cursor = [], 0.0
    for n, span in enumerate(spans):
        length = (starts[n + 1] if n + 1 < len(spans) else timeline.duration) - starts[n]
        source_start = cursor if span.source_start is None else span.source_start
        source_end = span.source_end
        if length <= 0:
            cursor = source_start if source_end is None else source_end
            continue
        if source_end is None:
            source_end = min(source_duration, source_start + length)
        source_length = max(0.05, min(source_end, source_duration) - source_start)
        out.append(Slot(length, source_start, source_length, max(1.0, source_length / length), span.focus))
        cursor = source_end
    return out


def panel(width: int, height: int, anchor: tuple[float, float], zoom: float = 1.0) -> str:
    """The filter that makes a clip fill ``width`` × ``height``, keeping the part at ``anchor``."""
    sw, sh = (2 * round(width * zoom / 2), 2 * round(height * zoom / 2))
    return (f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-{width})*{anchor[0]:.3f}:(ih-{height})*{anchor[1]:.3f},setsar=1")


def fit_lines(draw: ImageDraw.ImageDraw, text: str, font: Path, width: int, *, sizes: range,
              max_lines: int) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """``text`` as large as ``width`` allows in at most ``max_lines`` lines: the face, the lines, the size.
    A token that is only punctuation ("…", "!") stays on its word's line."""
    words: list[str] = []
    for token in text.split():
        if words and not re.search(r"\w", token):
            words[-1] += " " + token
        else:
            words.append(token)
    for size in sizes:
        face = ImageFont.truetype(str(font), size)
        lines: list[str] = []
        for word in words:
            trial = f"{lines[-1]} {word}" if lines else word
            if lines and draw.textlength(trial, font=face) <= width:
                lines[-1] = trial
            else:
                lines.append(word)
        if len(lines) <= max_lines and all(draw.textlength(line, font=face) <= width for line in lines):
            break
    return face, lines, size


def title_card(text: str, font: Path, out: Path, *, width: int) -> tuple[int, int]:
    """The headline as a picture: dark type on a white rounded box no wider than ``width``. Returns its size."""
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    face, lines, size = fit_lines(probe, text, font, width - 2 * TITLE_PAD, sizes=range(76, 36, -4), max_lines=2)
    leading = round(size * 1.16)
    text_w = max(round(probe.textlength(line, font=face)) for line in lines)
    box = (text_w + 2 * TITLE_PAD, leading * len(lines) + 2 * TITLE_PAD - round(size * 0.1))
    img = Image.new("RGBA", box, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, box[0] - 1, box[1] - 1), TITLE_RADIUS, fill=(255, 255, 255, 255))
    for n, line in enumerate(lines):
        x = (box[0] - draw.textlength(line, font=face)) / 2
        draw.text((x, TITLE_PAD + n * leading - round(size * 0.12)), line, font=face, fill=(17, 17, 17, 255))
    img.save(out)
    return box


def bubble_images(diameter: int, mask: Path, ring: Path) -> None:
    """The bubble's round mask and the white ring drawn around it."""
    big = diameter * 4
    m = Image.new("L", (big, big), 0)
    ImageDraw.Draw(m).ellipse((0, 0, big - 1, big - 1), fill=255)
    m.resize((diameter, diameter), Image.LANCZOS).save(mask)
    r = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(r).ellipse((0, 0, big - 1, big - 1), outline=(255, 255, 255, 255), width=big // 36)
    r.resize((diameter, diameter), Image.LANCZOS).save(ring)


__all__ = ["LAYOUTS", "Layout", "LayoutError", "ScreenSpan", "Slot", "VIDEO_SUFFIXES", "bubble_images", "fit_lines",
           "panel", "parse_screen_track", "slots", "title_card"]
