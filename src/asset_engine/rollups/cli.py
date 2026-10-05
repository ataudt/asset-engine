"""CLI for roll-ups.

Usage (``<prog>`` is the app's entry, e.g. FieldFix's ``python -m marketing.rollups``):
    <prog> report                       # compose every campaign x design, print the rule findings
    <prog> build                        # ... and write PDF, preview and guides where no rule fails
    <prog> build --draft                # write preview and guides even when a rule fails, no PDF
    <prog> build --campaign rollup-messe-de --design blue --out ~/Desktop
    <prog> links                        # (re)write the forwarding pages behind the QR codes

Order of work for a print run: add the campaign, `links`, deploy the website, scan the code from
the guides preview, `build`, look at the preview at full size, then upload the PDF to the shop.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from asset_engine.printing.copy import CopyError
from asset_engine.printing.links import LinkError, campaign_links, render_page, write_pages
from asset_engine.printing.pdf import save_pdf
from asset_engine.project import get_project
from asset_engine.rollups.compose import ComposedRollup, compose_rollup
from asset_engine.rollups.config import RollupConfig, RollupConfigError, load_config
from asset_engine.rollups.rules import DEAD_ZONE_MM, check, errors

## The previews are a quarter of the print resolution: enough to judge the layout on a screen.
PREVIEW_SCALE = 4
## The eye-level band drawn on the guides: J-A-B's hook band, where the headline goes.
EYE_BAND_MM = (1300, 1700)


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(get_project().root))
    except ValueError:
        return str(path)


def _split(values: list[str] | None, every: list[str]) -> list[str]:
    if not values:
        return every
    return [name for value in values for name in value.split(",") if name]


def _scaled(image: Image.Image) -> Image.Image:
    return image.resize((image.width // PREVIEW_SCALE, image.height // PREVIEW_SCALE), Image.LANCZOS)


def guides(config: RollupConfig, composed: ComposedRollup) -> Image.Image:
    """The whole data format, reduced, with what the eye at the stand never sees marked on it."""
    sheet = config.sheet
    image = composed.image.convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    stroke = max(2, sheet.px(2))
    ## Bleed and the hidden strips: cut off or rolled away.
    trim = sheet.trim_box
    visible = sheet.visible_box
    for box in ((0, 0, width, trim[1]), (0, trim[3], width, height), (0, 0, trim[0], height), (trim[2], 0, width, height)):
        draw.rectangle(box, fill=(255, 0, 0, 110))
    draw.rectangle((trim[0], trim[1], trim[2], visible[1]), fill=(120, 120, 120, 150))
    draw.rectangle((trim[0], visible[3], trim[2], trim[3]), fill=(120, 120, 120, 150))
    ## The band a table hides, and the eye-level band.
    draw.rectangle((trim[0], config.floor_y(DEAD_ZONE_MM), trim[2], visible[3]), fill=(255, 140, 0, 70))
    for mm in EYE_BAND_MM:
        draw.line((trim[0], config.floor_y(mm), trim[2], config.floor_y(mm)), fill=(0, 200, 90, 255), width=stroke)
    draw.rectangle(sheet.safe_box, outline=(0, 220, 255, 255), width=stroke)
    for item in composed.drawn:
        if item.box is not None:
            draw.rectangle(item.box, outline=(255, 0, 255, 255), width=stroke)
    image.alpha_composite(overlay)
    return _scaled(image.convert("RGB"))


def _compose(config: RollupConfig, campaign_name: str, design: str):
    designed = config.for_design(design)
    composed = compose_rollup(designed, config.campaign(campaign_name))
    return designed, composed, check(designed, composed.drawn)


def _cmd_report(args: argparse.Namespace) -> int:
    config = load_config()
    failed = 0
    for campaign in _split(args.campaign, [c.name for c in config.campaigns]):
        for design in _split(args.design, list(config.designs)):
            designed, _, findings = _compose(config, campaign, design)
            print(f"{campaign}/{design}  ({designed.printer}, {designed.sheet.dpi} dpi, canvas {designed.sheet.canvas[0]}x{designed.sheet.canvas[1]} px)")
            for finding in findings:
                print(f"  {finding}")
            if not findings:
                print("  ok")
            failed += bool(errors(findings))
    return 1 if failed else 0


def _cmd_build(args: argparse.Namespace) -> int:
    config = load_config()
    out_root = Path(args.out).expanduser() if args.out else config.default_out_dir
    problems = 0
    for campaign_name in _split(args.campaign, [c.name for c in config.campaigns]):
        campaign = config.campaign(campaign_name)
        for design in _split(args.design, list(config.designs)):
            label = f"{campaign_name}/{design}"
            designed, composed, findings = _compose(config, campaign_name, design)
            for finding in findings:
                print(f"     {finding}")
            out_dir = out_root / campaign_name / design
            out_dir.mkdir(parents=True, exist_ok=True)
            failing = errors(findings)
            if not failing or args.draft:
                visible = composed.image.crop(designed.sheet.visible_box)
                _scaled(visible).save(out_dir / "preview.png", format="PNG")
                guides(designed, composed).save(out_dir / "guides.png", format="PNG")
            if failing:
                print(f"FAIL {label}: {len(failing)} rule(s) fail — no PDF" + ("; draft previews written" if args.draft else ""))
                problems += 1
                continue
            pdf = out_dir / f"Rollup - {campaign.language.upper()} - {design}.pdf"
            save_pdf([composed.image], pdf, dpi=designed.sheet.dpi)
            print(f"ok   {label}: {_relative(pdf)}")

        stale = [
            _relative(link.page)
            for link in campaign_links(config, campaign).values()
            if not link.page.is_file() or link.page.read_text(encoding="utf-8") != render_page(config, link, campaign.language)
        ]
        if stale:
            print("     the QR codes point at pages that are missing or out of date:")
            for path in stale:
                print(f"       {path}")
            print(f"     run `{config.links.command} links` and deploy the website before printing")
    return 1 if problems else 0


def _cmd_links(args: argparse.Namespace) -> int:
    config = load_config()
    for link, changed in write_pages(config):
        print(f"{'wrote' if changed else 'same '} {_relative(link.page)}")
        print(f"      {link.url} -> {link.target}")
    return 0


def build_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Build trade-fair roll-ups and the pages behind their QR codes.")
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="compose and print the rule findings, write nothing")
    build = commands.add_parser("build", help="render the print PDF, a preview and a guides image")
    for sub in (report, build):
        sub.add_argument("--campaign", action="append", help="campaign name; repeatable, comma-separated")
        sub.add_argument("--design", action="append", help="design name; repeatable, comma-separated")
    build.add_argument("--out", help="output root (default: default_out_dir in the roll-up config)")
    build.add_argument("--draft", action="store_true", help="write preview and guides even when a rule fails")
    report.set_defaults(func=_cmd_report)
    build.set_defaults(func=_cmd_build)

    links = commands.add_parser("links", help="write the forwarding pages under the site's <path>/")
    links.set_defaults(func=_cmd_links)
    return parser


def main(argv: list[str] | None = None, *, prog: str) -> int:
    args = build_parser(prog=prog).parse_args(argv)
    try:
        return args.func(args)
    except (RollupConfigError, LinkError, CopyError) as error:
        print(f"FAIL {error}")
        return 1


__all__ = ["build_parser", "guides", "main"]
