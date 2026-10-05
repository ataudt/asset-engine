"""CLI for the print flyers.

Usage (``<prog>`` is the app's entry, NutriSpy's ``python -m marketing.flyers``):
    <prog> build                       # every campaign in config.json
    <prog> build --campaign flyer-de --design green-wall
    <prog> build --out ~/Dropbox/...   # write somewhere else
    <prog> links                       # (re)write <site_dir>/<path>/<campaign>/{web,app}/
    <prog> report                      # links and their targets, renders nothing

Order of work for a print run: add the campaign to config.json, `links`, deploy the website,
check both addresses live, `build`, scan the codes, then send the PDF to the printer.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from asset_engine.project import get_project
from asset_engine.flyers.compose import compose_flyer, save_pdf
from asset_engine.flyers.config import Campaign, FlyerConfig, FlyerConfigError, load_config
from asset_engine.printing.copy import CopyError
from asset_engine.printing.links import LinkError, campaign_links, render_page, write_pages


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(get_project().root))
    except ValueError:
        return str(path)


def _selected(config: FlyerConfig, names: list[str] | None) -> list[Campaign]:
    if not names:
        return list(config.campaigns)
    return [config.campaign(name) for value in names for name in value.split(",") if name]


def _stale_pages(config: FlyerConfig, campaign: Campaign) -> list[str]:
    """Forwarding pages that are missing or no longer say what ``config.json`` says."""
    return [
        _relative(link.page)
        for link in campaign_links(config, campaign).values()
        if not link.page.is_file()
        or link.page.read_text(encoding="utf-8") != render_page(config, link, campaign.language)
    ]


def _designs(config: FlyerConfig, names: list[str] | None) -> list[str]:
    if not names:
        return list(config.designs)
    return [name for value in names for name in value.split(",") if name]


def _cmd_build(args: argparse.Namespace) -> int:
    config = load_config()
    out_root = Path(args.out).expanduser() if args.out else config.default_out_dir
    problems = 0
    for campaign in _selected(config, args.campaign):
        for design in _designs(config, args.design):
            label = f"{campaign.name}/{design}"
            try:
                pages = compose_flyer(config.for_design(design), campaign)
            except (FlyerConfigError, LinkError, CopyError) as error:
                print(f"FAIL {label}: {error}")
                problems += 1
                continue

            ## One folder per design, and the design in the file name too: the PDF is what gets
            ## sent to a printer, and there it has to say which of the looks it is.
            out_dir = out_root / campaign.name / design
            pdf = out_dir / f"Flyer - {campaign.language.upper()} - {design}.pdf"
            save_pdf(pages, pdf, config.page)
            ## The previews are cut to the trim size: they show the flyer as it comes back from
            ## the printer, while the PDF carries the bleed the printer asks for.
            for number, composed in enumerate(pages, start=1):
                composed.image.crop(config.page.trim_box).save(out_dir / f"page-{number}.png", format="PNG")
            print(f"ok   {label}: {_relative(pdf)}")

        stale = _stale_pages(config, campaign)
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
        for device, target in link.by_device.items():
            print(f"      {device:>7}: {target}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    config = load_config()
    for campaign in _selected(config, args.campaign):
        print(f"{campaign.name} ({campaign.language})")
        stale = set(_stale_pages(config, campaign))
        for link in campaign_links(config, campaign).values():
            state = "STALE" if _relative(link.page) in stale else "ok   "
            print(f"  {state} {link.url}")
            print(f"        -> {link.target}")
            for device, target in link.by_device.items():
                print(f"        {device:>7}: {target}")
    return 0


def build_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Build the A6 print flyers and the forwarding pages behind their QR codes.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="render the flyer PDF and page previews")
    build.add_argument("--campaign", action="append", help="campaign name; repeatable, comma-separated")
    build.add_argument("--design", action="append", help="design name; repeatable, comma-separated")
    build.add_argument("--out", help="output root (default: default_out_dir in the flyer config)")
    build.set_defaults(func=_cmd_build)

    links = commands.add_parser("links", help="write the forwarding pages under the site's <path>/")
    links.set_defaults(func=_cmd_links)

    report = commands.add_parser("report", help="list every QR address and its target")
    report.add_argument("--campaign", action="append", help="campaign name; repeatable, comma-separated")
    report.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None, *, prog: str) -> int:
    args = build_parser(prog=prog).parse_args(argv)
    try:
        return args.func(args)
    except (FlyerConfigError, LinkError, CopyError) as error:
        print(f"FAIL {error}")
        return 1

