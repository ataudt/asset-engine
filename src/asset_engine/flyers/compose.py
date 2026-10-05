"""Compose the two pages of the print flyer.

Layer: asset engine.
Rules:
  - The pieces are ``printing.elements``, shared with every printed product; this module only
    resolves the flyer's millimetres (from the top-left of the trimmed page) into their pixels. The
    bezel, the drop shadow and the rotation come from ``store.compose``.
  - The faces are the flyer's own (``fonts`` in ``config.json``), picked by coverage ahead of the
    store's list, so a script they lack still falls back to a face that has it.
  - Type that does not fit stops the build. A flyer with a clipped line is a reprint.
  - Every element that must survive the cut reports its box, so the safe margin can be checked
    against what was drawn and not against what ``config.json`` intended.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from asset_engine.flyers.config import Campaign, FlyerConfig, FlyerConfigError, Page, read_copy
from asset_engine.printing.elements import (
    Box,
    StackLayout,
    background,
    draw_runs,
    draw_stack,
    paste_by_height,
    paste_placed,
    paste_qr,
)
from asset_engine.printing.elements import layout_stack as layout_stack_in_band
from asset_engine.printing.links import campaign_links
from asset_engine.printing.pdf import save_pdf as save_print_pdf
from asset_engine.project import get_project
from asset_engine.store.compose import center_of, find_capture, frame_capture, paste_rotated, raw_path, shadow_for
from asset_engine.store.config import FrameSpec, Placement, ScreenshotConfig, ShadowSpec, TextStyle
from asset_engine.store.config import load_config as load_screenshot_config
from asset_engine.store.fonts import font_for_text
from asset_engine.store.render import layout_headline


class FlyerOverflowError(FlyerConfigError):
    """The copy does not fit its band even at the smallest size."""


@dataclass(frozen=True)
class ComposedPage:
    """One page on the bled canvas, and where its cut-sensitive elements ended up."""

    image: Image.Image
    boxes: dict[str, Box]


def layout_stack(
    draw: ImageDraw.ImageDraw,
    paragraphs: list[list[str]],
    spec: dict[str, Any],
    config: FlyerConfig,
    *,
    label: str = "",
) -> StackLayout:
    """Set every paragraph of ``spec``'s band at the one size at which the whole stack fits.

    The opening ``lead_paragraphs`` are the headline and set in its face; the rest is running text.
    """
    page = config.page
    band_top, band_bottom = (page.at(edge) for edge in spec["band_mm"])
    return layout_stack_in_band(
        draw,
        paragraphs,
        [config.fonts["headline" if index < spec["lead_paragraphs"] else "body"] for index in range(len(paragraphs))],
        band_top=band_top,
        band_bottom=band_bottom,
        max_width=page.px(spec["max_width_mm"]),
        base_size=page.px(spec["base_size_mm"]),
        min_size=page.px(spec["min_size_mm"]),
        step=max(1, page.px(spec["shrink_step_mm"])),
        line_spacing=spec["line_spacing"],
        paragraph_gap=spec["paragraph_gap"],
        color=tuple(spec["color"]),  # type: ignore[arg-type]
        label=label,
    )


def _background(config: FlyerConfig, spec: dict[str, Any], *, veil: float = 0.0) -> Image.Image:
    """The page's ground on the bled canvas: a plain colour, or a plate cut to fill it."""
    return background(config.page.canvas, spec, get_project().root, veil=veil)


def _draw_wordmark(draw: ImageDraw.ImageDraw, config: FlyerConfig) -> Box:
    """The brand name in its two colours, centred on the page."""
    page, spec = config.page, config.wordmark
    text = "".join(part["text"] for part in spec["parts"])
    font = font_for_text(text, page.px(spec["size_mm"]), label="wordmark", priority=config.fonts["wordmark"])
    return draw_runs(draw, spec["parts"], font, center_x=page.at(page.trim_mm[0] / 2), top=page.at(spec["top_mm"]))


def _paste_qr(
    canvas: Image.Image,
    config: FlyerConfig,
    url: str,
    *,
    center_x_mm: float,
    top_mm: float,
    size_mm: float,
) -> Box:
    page = config.page
    return paste_qr(
        canvas,
        url,
        center_x=page.at(center_x_mm),
        top=page.at(top_mm),
        size=page.px(size_mm),
        quiet_modules=config.qr["quiet_modules"],
        corner_radius=page.px(config.qr["corner_radius_mm"]),
        dark=tuple(config.qr["dark"]),  # type: ignore[arg-type]
    )


def _paste_badge(
    canvas: Image.Image,
    config: FlyerConfig,
    path: Path,
    *,
    center_x_mm: float,
    top_mm: float,
    height_mm: float,
) -> Box:
    if not path.is_file():
        raise FlyerConfigError(f"no store badge at {path} — every flyer language needs its own pair")
    page = config.page
    return paste_by_height(
        canvas, path, center_x=page.at(center_x_mm), top=page.at(top_mm), height=page.px(height_mm)
    )


def compose_text_page(config: FlyerConfig, campaign: Campaign) -> ComposedPage:
    """The back: wordmark, the copy, and the code that leads to the website."""
    page, spec = config.page, config.text_page
    copy = read_copy(campaign.language)["text_page"]
    links = campaign_links(config, campaign)

    canvas = _background(config, spec["background"], veil=spec["veil"])
    draw = ImageDraw.Draw(canvas)
    boxes: dict[str, Box] = {"wordmark": _draw_wordmark(draw, config)}

    stack = layout_stack(draw, copy["paragraphs"], spec["text"], config, label=campaign.name)
    if stack.overflows:
        raise FlyerOverflowError(
            f"[{campaign.name}] the copy is {stack.width}x{stack.height}px at the smallest size "
            f"({stack.size}px) and does not fit its band — trim copy/{campaign.language}.json"
        )
    boxes["text"] = draw_stack(
        draw, stack, center_x=page.at(page.trim_mm[0] / 2), color=tuple(spec["text"]["color"])  # type: ignore[arg-type]
    )

    qr = spec["qr"]
    boxes["qr"] = _paste_qr(
        canvas,
        config,
        links[qr["link"]].url,
        center_x_mm=qr["center_x_mm"],
        top_mm=qr["top_mm"],
        size_mm=qr["size_mm"],
    )

    label = spec["label"]
    label_font = font_for_text(
        copy["qr_label"], page.px(label["size_mm"]), label=campaign.name, priority=config.fonts["headline"]
    )
    label_at = (page.at(qr["center_x_mm"]), page.at(label["top_mm"]))
    draw.text(label_at, copy["qr_label"], font=label_font, fill=tuple(label["color"]), anchor="ma")
    boxes["label"] = draw.textbbox(label_at, copy["qr_label"], font=label_font, anchor="ma")

    return ComposedPage(image=canvas.convert("RGB"), boxes=boxes)


def _capture_path(config: FlyerConfig, screenshots: ScreenshotConfig, language: str) -> Path:
    """The raw capture the phone shows, in ``language`` or borrowed from the fallback language."""
    spec = config.image_page["phone"]
    capture = spec["capture"]
    if isinstance(capture, int):
        slots = [slot for slot in screenshots.slots if slot.caption == capture]
        if not slots:
            raise FlyerConfigError(
                f"the flyer wants capture {capture}, which the store set's config.json "
                f"does not map to a slot"
            )
        return raw_path(screenshots, language, slots[0])

    for candidate in dict.fromkeys((language, spec["capture_fallback"])):
        stem = capture.get(candidate)
        found = find_capture(screenshots.raw_dir / candidate, stem) if stem else None
        if found is not None:
            return found
    raise FlyerConfigError(
        f"no capture for the flyer's phone in {language!r} or {spec['capture_fallback']!r} "
        f"under {screenshots.raw_dir}"
    )


def _phone_layers(
    config: FlyerConfig,
    screenshots: ScreenshotConfig,
    language: str,
) -> tuple[Image.Image, Image.Image, Placement, tuple[int, int]]:
    """The framed capture, its shadow, where it goes and how far the shadow is offset."""
    page, spec = config.page, config.image_page["phone"]
    width = page.px(spec["width_mm"])
    framed = frame_capture(
        screenshots,
        _capture_path(config, screenshots, language),
        phone_width=width,
        frame=FrameSpec(stroke=page.px(spec["frame_stroke_mm"]), color=screenshots.frame.color),
    )
    ## Colour and opacity are the store's; offset and blur are lengths, so they are the flyer's.
    shadow = ShadowSpec(
        offset=(page.px(spec["shadow"]["offset_mm"][0]), page.px(spec["shadow"]["offset_mm"][1])),
        blur=page.px(spec["shadow"]["blur_mm"]),
        opacity=screenshots.shadow.opacity,
        color=screenshots.shadow.color,
    )
    placement = Placement(
        left=page.at(spec["left_mm"]), top=page.at(spec["top_mm"]), width=width, rotation=spec["rotation"]
    )
    return framed, shadow_for(framed, shadow), placement, shadow.offset


def compose_image_page(
    config: FlyerConfig,
    campaign: Campaign,
    screenshots: ScreenshotConfig | None = None,
) -> ComposedPage:
    """The front: wordmark, tagline, the app on a phone with Nutrius, and the code to the stores."""
    screenshots = screenshots or load_screenshot_config()
    page, spec = config.page, config.image_page
    copy = read_copy(campaign.language)["image_page"]
    links = campaign_links(config, campaign)

    canvas = _background(config, spec["background"], veil=spec.get("veil", 0.0))
    draw = ImageDraw.Draw(canvas)
    boxes: dict[str, Box] = {"wordmark": _draw_wordmark(draw, config)}

    tagline = spec["tagline"]
    band_top, band_bottom = (page.at(edge) for edge in tagline["band_mm"])
    style = TextStyle(
        band_top=band_top,
        band_clearance=0,
        fallback_band_bottom=band_bottom,
        max_width=page.px(tagline["max_width_mm"]),
        color=tuple(tagline["color"]),  # type: ignore[arg-type]
        base_size=page.px(tagline["base_size_mm"]),
        min_size=page.px(tagline["min_size_mm"]),
        shrink_step=max(1, page.px(tagline["shrink_step_mm"])),
        line_spacing=tagline["line_spacing"],
        max_lines=tagline["max_lines"],
    )
    layout = layout_headline(draw, copy["tagline"], style, label=campaign.name, fonts=config.fonts["headline"])
    if layout.overflows:
        raise FlyerOverflowError(
            f"[{campaign.name}] the tagline needs {len(layout.lines)} lines at {layout.size}px "
            f"and does not fit — trim copy/{campaign.language}.json"
        )
    center_x = page.at(page.trim_mm[0] / 2)
    for index, line in enumerate(layout.lines):
        draw.text(
            (center_x, layout.origin_y + index * layout.line_height),
            line,
            font=layout.font,
            fill=style.color,
            anchor="ma",
        )
    boxes["tagline"] = (
        center_x - layout.width // 2,
        layout.ink_top,
        center_x + (layout.width + 1) // 2,
        layout.ink_bottom,
    )

    framed, shadow, placement, offset = _phone_layers(config, screenshots, campaign.language)
    center = center_of(placement, framed.size)
    paste_rotated(canvas, shadow, (center[0] + offset[0], center[1] + offset[1]), placement.rotation)
    paste_rotated(canvas, framed, center, placement.rotation)

    ## Nutrius after the phone: he stands in front of its edge, as in store screenshot 1.
    mascot = spec["mascot"]
    paste_placed(
        canvas,
        get_project().root / mascot["asset"],
        Placement(
            left=page.at(mascot["left_mm"]),
            top=page.at(mascot["top_mm"]),
            width=page.px(mascot["width_mm"]),
            rotation=mascot["rotation"],
        ),
    )

    ## The store column last: one code for both stores, the two badges stacked above it.
    store = spec["store"]
    for badge in store["badges"]:
        boxes[f"badge:{Path(badge['badge']).stem}"] = _paste_badge(
            canvas,
            config,
            config.badges_dir / campaign.language / badge["badge"],
            center_x_mm=store["center_x_mm"],
            top_mm=badge["top_mm"],
            height_mm=badge["height_mm"],
        )
    boxes[f"qr:{store['link']}"] = _paste_qr(
        canvas,
        config,
        links[store["link"]].url,
        center_x_mm=store["center_x_mm"],
        top_mm=store["qr_top_mm"],
        size_mm=store["qr_size_mm"],
    )

    return ComposedPage(image=canvas.convert("RGB"), boxes=boxes)


def compose_flyer(config: FlyerConfig, campaign: Campaign) -> list[ComposedPage]:
    """Both pages of one campaign's flyer, in print order: the front first, then the back."""
    return [compose_image_page(config, campaign), compose_text_page(config, campaign)]


def save_pdf(pages: list[ComposedPage], path: Path, page: Page) -> None:
    """Write the pages as one PDF whose page size is the bled canvas at the page's resolution."""
    save_print_pdf([composed.image for composed in pages], path, dpi=page.dpi)


__all__ = [
    "Box",
    "ComposedPage",
    "FlyerOverflowError",
    "StackLayout",
    "compose_flyer",
    "compose_image_page",
    "compose_text_page",
    "layout_stack",
    "save_pdf",
]
