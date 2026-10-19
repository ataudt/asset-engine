"""CLI for profile banners.

Usage (``<prog>`` is the project's entry, e.g. ``python -m marketing.banners``):
    <prog> report                          # compose every banner, print where key elements land
    <prog> build                           # ... and write each as <id>.png; findings are printed loudly
    <prog> build --banner linkedin-profile --guides --out ~/Desktop

``--guides`` also writes ``<id>_guides.png``: the banner with the platform's zones drawn in, then
the banner as a desktop profile shows it and as a phone crops it, each with the profile photo over it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from asset_engine.banners.compose import ComposedBanner, compose_banner
from asset_engine.banners.config import Banner, BannerConfig, BannerConfigError, Photo, load_config
from asset_engine.banners.rules import check
from asset_engine.printing.copy import CopyError
from asset_engine.printing.report import Run, clear_outputs
from asset_engine.project import get_project
from asset_engine.store.fonts import FontCoverageError

## The guides sheet is drawn at the platform's own pixels; the photo hangs below the banner, as it
## does on a profile, into a strip this tall.
OVERHANG = 160
MARGIN = 24
LABEL = 28


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(get_project().root))
    except ValueError:
        return str(path)


def _split(values: list[str] | None, every: list[str]) -> list[str]:
    if not values:
        return every
    return [name for value in values for name in value.split(",") if name]


def _with_photo(view: Image.Image, photo: Photo, offset: tuple[int, int]) -> Image.Image:
    """``view`` over a strip for the overhang, with the round profile photo drawn where it sits."""
    panel = Image.new("RGB", (view.width, view.height + OVERHANG), (243, 242, 239))
    panel.paste(view, (0, 0))
    draw = ImageDraw.Draw(panel)
    cx, cy = photo.center[0] - offset[0], photo.center[1] - offset[1]
    r = photo.diameter // 2
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(176, 176, 176), outline=(255, 255, 255), width=4)
    return panel


def guides(banner: Banner, composed: ComposedBanner) -> Image.Image:
    """The banner at the platform's pixels three times: zones and element boxes drawn in, as a desktop
    profile shows it, and as the narrowest phone crop shows it."""
    platform = banner.platform
    flat = composed.image.resize(platform.canvas, Image.LANCZOS)

    zoned = flat.convert("RGBA")
    overlay = Image.new("RGBA", zoned.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for zone in platform.zones:
        if zone.kind == "keep-out":
            draw.rectangle(zone.box, fill=(255, 40, 40, 70), outline=(255, 40, 40, 255), width=2)
            draw.text((zone.box[0] + 6, zone.box[3] - 16), zone.name, fill=(255, 255, 255, 255))
        else:
            draw.rectangle(zone.box, outline=(0, 230, 255, 255), width=3)
            draw.text((zone.box[0] + 6, zone.box[1] + 4), zone.name, fill=(0, 230, 255, 255))
    for item in composed.drawn:
        if item.role == "key" and item.box is not None:
            draw.rectangle(item.box, outline=(255, 0, 255, 255), width=2)
    zoned.alpha_composite(overlay)

    view = platform.zone(platform.mobile_view).box
    panels = [
        ("zones: red = covered by the photo, cyan = survives the narrowest phone crop, magenta = key elements", zoned.convert("RGB")),
        ("desktop profile", _with_photo(flat, platform.photos["desktop"], (0, 0))),
        (f"phone, narrowest crop ({view[2] - view[0]} x {view[3] - view[1]})", _with_photo(flat.crop(view), platform.photos["mobile"], view[:2])),
    ]
    width = platform.canvas[0] + 2 * MARGIN
    height = sum(LABEL + panel.height + MARGIN for _, panel in panels) + MARGIN
    sheet = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    y = MARGIN
    for caption, panel in panels:
        draw.text((MARGIN, y + 6), caption, fill=(40, 40, 40))
        y += LABEL
        sheet.paste(panel, (MARGIN, y))
        y += panel.height + MARGIN
    return sheet


def _compose(config: BannerConfig, name: str):
    banner = config.banner(name)
    composed = compose_banner(config, banner)
    return banner, composed, check(banner, composed.drawn)


def _cmd_report(args: argparse.Namespace) -> int:
    config = load_config()
    for name in _split(args.banner, list(config.banners)):
        banner, _, findings = _compose(config, name)
        width, height = banner.size
        print(f"{name}  ({banner.platform.name}, {banner.language}, {width}x{height} px)")
        for finding in findings:
            print(f"  {finding}")
        if not findings:
            print("  ok")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    config = load_config()
    out_dir = Path(args.out).expanduser() if args.out else config.default_out_dir
    run = Run()
    for name in _split(args.banner, list(config.banners)):
        image_path, guides_path = out_dir / f"{name}.png", out_dir / f"{name}_guides.png"
        try:
            banner, composed, findings = _compose(config, name)
        except (BannerConfigError, CopyError, FontCoverageError, FileNotFoundError) as error:
            run.fail(name, str(error), clear_outputs([image_path, guides_path]))
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        composed.image.save(image_path, format="PNG", optimize=True)
        print(f"ok   {name}: {_relative(image_path)} ({composed.image.width}x{composed.image.height})")
        if args.guides:
            guides(banner, composed).save(guides_path, format="PNG")
            print(f"     guides: {_relative(guides_path)}")
        run.warn(name, findings)
    return run.summary()


def build_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Build profile banners.")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="compose and print where key elements land, write nothing")
    build = commands.add_parser("build", help="render each banner as PNG, always")
    for sub in (report, build):
        sub.add_argument("--banner", action="append", help="banner id; repeatable, comma-separated")
    build.add_argument("--out", help="output directory (default: default_out_dir in the banner config)")
    build.add_argument("--guides", action="store_true", help="also write <id>_guides.png with the zones and the desktop and phone views")
    report.set_defaults(func=_cmd_report)
    build.set_defaults(func=_cmd_build)
    return parser


def main(argv: list[str] | None = None, *, prog: str) -> int:
    args = build_parser(prog=prog).parse_args(argv)
    try:
        return args.func(args)
    except (BannerConfigError, CopyError) as error:
        print(f"FAIL {error}")
        return 1


__all__ = ["build_parser", "guides", "main"]
