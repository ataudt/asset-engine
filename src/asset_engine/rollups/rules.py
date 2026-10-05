"""What makes a roll-up work at a trade fair, checked against what was drawn.

Layer: asset engine.
Rules:
  - Each check judges ``compose.Drawn`` records — real boxes, real glyph heights, the real ground
    under the type — never the config's intentions.
  - An *error* stops the build: the banner would fail at the stand (unreadable from where people
    stand, hidden behind a table, cut off by the cassette). A *warning* is printed and the PDF is
    written: a judgement call, or a known follow-up such as a photograph not yet at full resolution.
  - The thresholds are rules of thumb from print shops and trade-fair guides, not standards; each
    names its source. Sources read 2026-10-05:
      J-A-B    https://www.j-a-b.net/how-to-design-a-trade-show-banner-that-attracts-attention-sizes-layo
      Loesch   https://georg-loesch.de/blog/roll-up-gestalten/
      marcon.  https://www.marconomy.de/7-tipps-zur-gestaltung-von-roll-ups-a-646379/
      b&b      https://www.bold-and-bright.de/schriftgroessenrechner.html (DIN 1450 formula, second-hand)
      WCAG     https://www.w3.org/TR/WCAG21/#contrast-minimum
      Denso    https://www.qrcode.com/en/howto/code.html
      Flyeral. https://www.flyeralarm.com/blog/wp-content/uploads/Flyeralarm_Checkliste_Druckdatenerstellung-1.pdf
      PullUp   https://pullupstand.com/blogs/blog/pop-up-banner-design-guide-canva-photoshop-tips
      Dubai    https://printerydubai.com/effective-roll-up-banner-design/
      designen https://www.designenlassen.de (roll-up guide; cited by the research, not re-read)
"""

from __future__ import annotations

from dataclasses import dataclass

from asset_engine.printing.sheet import MM_PER_INCH
from asset_engine.rollups.compose import Drawn
from asset_engine.rollups.config import RollupConfig

ERROR = "error"
WARNING = "warning"

## The floor band hidden by tables, counters, brochure stands and people at a fair: nothing that
## must be read goes below it (J-A-B: dead zone 0-500 mm; Loesch: lower 50-70 cm covered).
DEAD_ZONE_MM = 500
## Where the logo is looked for: the top band (J-A-B identity band 1700-2000 mm).
LOGO_FLOOR_MM = 1700
## A code is scanned comfortably between hip and chest height (J-A-B action band from 500 mm;
## PullUp: 1-1.5 m). The centre should land in this range.
QR_CENTER_MM = (600, 1500)
## One glance, one message: 3-7 words of headline, under ~35 in all (J-A-B; Loesch).
HEADLINE_MAX_WORDS = 7
TOTAL_MAX_WORDS = 35
## Headline letters 1 cm high per metre of viewing distance (Loesch, marconomy: "1 cm Schrifthöhe
## pro Meter Leseabstand"), measured on the capitals.
CAP_MM_PER_METRE = 10
## Running text: x-height = reading distance / 250 (DIN 1450 comfort formula, via b&b).
X_HEIGHT_DIVISOR = 250
## A code is read from about ten times its width; 80-100 mm for people walking by (J-A-B). That is
## the code's own modules: the card's margin is its quiet zone (Denso: four modules) and does not
## count.
QR_MIN_MM = 80
## Contrast of type against its ground (WCAG 2.1 AA: 4.5:1, 3:1 for large text). A headline on a
## roll-up is large by any measure; the smaller roles are held to the stricter figure.
CONTRAST_HEADLINE = 3.0
CONTRAST_TEXT = 4.5
## At most two typeface families (Dubai; designen; marcon.).
MAX_FAMILIES = 2
## Pictures below this print visibly soft even at a few metres; large format asks 100-150 dpi
## (Flyeralarm), and roll-up shops accept down to about 72-100.
MIN_EFFECTIVE_DPI = 75

TEXT_TYPES = ("text", "list")


@dataclass(frozen=True)
class Finding:
    level: str
    element: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():<7} {self.element}: {self.message}"


def _mm(config: RollupConfig, px: float) -> float:
    return px * MM_PER_INCH / config.sheet.dpi


def _above_floor(config: RollupConfig, row: int) -> float:
    """A pixel row as millimetres above the floor of the visible area."""
    return _mm(config, config.floor_y(0) - row)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """The WCAG contrast ratio of two colours."""
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def check(config: RollupConfig, drawn: list[Drawn]) -> list[Finding]:
    """Every finding for one composed roll-up, errors first."""
    findings: list[Finding] = []
    safe = config.sheet.safe_box

    def add(level: str, element: Drawn | str, message: str) -> None:
        findings.append(Finding(level, element if isinstance(element, str) else element.id, message))

    for item in drawn:
        if item.box is None:
            continue
        left, top, right, bottom = item.box
        if left < safe[0] or top < safe[1] or right > safe[2] or bottom > safe[3]:
            add(ERROR, item, f"leaves the safe area of the visible banner ({item.box} vs {safe})")
        if (item.type in TEXT_TYPES or item.type == "qr") and _above_floor(config, bottom) < DEAD_ZONE_MM:
            add(
                ERROR,
                item,
                f"reaches down to {_above_floor(config, bottom):.0f} mm above the floor; below {DEAD_ZONE_MM} mm "
                f"a table or a visitor hides it",
            )

    words = 0
    families = set()
    for item in drawn:
        if item.type not in TEXT_TYPES:
            continue
        families.add(item.family)
        count = len(item.text.split())
        if item.role != "url":
            words += count
        cap_mm = _mm(config, item.cap_height_px)
        x_mm = _mm(config, item.x_height_px)
        if item.role == "headline":
            if count > HEADLINE_MAX_WORDS:
                add(ERROR, item, f"{count} words; a headline read in passing carries at most {HEADLINE_MAX_WORDS}")
            need = config.viewing_distance_m * CAP_MM_PER_METRE
            if cap_mm < need:
                add(ERROR, item, f"capitals {cap_mm:.0f} mm high; read from {config.viewing_distance_m} m they need {need:.0f} mm")
        else:
            need = config.reading_distance_m * 1000 / X_HEIGHT_DIVISOR
            if x_mm < need:
                add(ERROR, item, f"x-height {x_mm:.1f} mm; read from {config.reading_distance_m} m it needs {need:.1f} mm")
        floor = CONTRAST_HEADLINE if item.role == "headline" else CONTRAST_TEXT
        ## Against whichever end of the ground is nearer the type's own colour: the worst place a
        ## letter can sit, not the average one.
        ratio = min(contrast(item.color, extreme) for extreme in item.ground)
        if ratio < floor:
            add(
                ERROR,
                item,
                f"contrast {ratio:.1f}:1 against the lightest/darkest of its ground {item.ground}; it needs {floor}:1",
            )
    if words > TOTAL_MAX_WORDS:
        add(ERROR, "copy", f"{words} words in all (the address not counted); a roll-up read in passing carries {TOTAL_MAX_WORDS}")
    if len(families) > MAX_FAMILIES:
        add(WARNING, "fonts", f"{len(families)} typeface families {sorted(families)}; two read as one design")

    for item in drawn:
        if item.type == "qr":
            width_mm = _mm(config, item.code_px)
            if width_mm < QR_MIN_MM:
                add(ERROR, item, f"the code is {width_mm:.0f} mm wide without its margin; read at a stand it needs {QR_MIN_MM} mm")
            center = _above_floor(config, (item.box[1] + item.box[3]) / 2)
            if not QR_CENTER_MM[0] <= center <= QR_CENTER_MM[1]:
                add(WARNING, item, f"centre {center:.0f} mm above the floor; it scans best between {QR_CENTER_MM[0]} and {QR_CENTER_MM[1]} mm")
        if item.role == "logo" and _above_floor(config, item.box[3]) < LOGO_FLOOR_MM:
            add(WARNING, item, f"stands {_above_floor(config, item.box[3]):.0f} mm above the floor; the logo is looked for above {LOGO_FLOOR_MM} mm")
        if item.raster is not None and item.raster.effective_dpi < MIN_EFFECTIVE_DPI:
            raster = item.raster
            needed = tuple(round(side * MIN_EFFECTIVE_DPI / raster.effective_dpi) for side in raster.source_px)
            add(
                WARNING,
                item,
                f"{raster.path.name} prints at {raster.effective_dpi:.0f} dpi ({raster.source_px[0]}x{raster.source_px[1]} px "
                f"over {raster.placed_px[0]}x{raster.placed_px[1]} px); {MIN_EFFECTIVE_DPI} dpi needs about "
                f"{needed[0]}x{needed[1]} px",
            )
    return sorted(findings, key=lambda finding: finding.level != ERROR)


def errors(findings: list[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.level == ERROR]


__all__ = ["ERROR", "WARNING", "Finding", "check", "contrast", "errors"]
