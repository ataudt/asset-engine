"""CLI for app icons.

Usage (``<prog>`` is the project's entry, e.g. ``python -m marketing.logos``):
    <prog> generate "a fox curled into a circle" -n 4      # drafts -> <default_out_dir>/drafts/<time>/
    <prog> generate "thicker lines, warmer orange" --from <draft.png>
    <prog> build                                           # master -> every target path in the logo config
    <prog> build --out ~/Desktop/icons --guides

``generate`` asks the app's image model and writes only drafts; copy the one you want to the
``master`` path of the logo config and commit it. ``build`` derives every target from that master,
deterministically. ``--out`` writes ``<target>.png`` there instead of to the app's paths;
``--guides`` also writes ``guides.png``: each target with its safe circle and viewport drawn in, and
as the platforms' masks show it.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

from asset_engine.logos.config import LogoConfig, LogoConfigError, Target, load_config
from asset_engine.logos.derive import SUPERSAMPLE, Motif, cut_out, render
from asset_engine.logos.generate import LogoGenerationError, generate_drafts, new_drafts_dir
from asset_engine.printing.report import Run, clear_outputs
from asset_engine.project import get_project

THUMB = 256
MARGIN = 24
LABEL = 28
## The monochrome layer is tinted by the launcher; the guides show it in one such tint.
THEMED = ((211, 227, 253), (4, 30, 73))


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(get_project().root))
    except ValueError:
        return str(path)


def _cmd_generate(args: argparse.Namespace) -> int:
    config = load_config()
    reference = Path(args.reference).expanduser() if args.reference else None
    if reference is not None and not reference.is_file():
        print(f"FAIL no draft at {reference}")
        return 1
    out_dir = new_drafts_dir(config, Path(args.out).expanduser() if args.out else None)
    print(f"asking {config.model} ({config.location}) for {args.count} draft(s) ...", flush=True)
    drafts = generate_drafts(config, args.description, count=args.count, out_dir=out_dir, reference=reference)
    run = Run()
    for draft in drafts:
        label = f"draft-{draft.index:02d}"
        if draft.error:
            run.fail(label, draft.error)
        else:
            print(f"ok   {label}: {_relative(out_dir / draft.path)}")
    print(f"     prompt and outcomes: {_relative(out_dir / 'run.json')}")
    print(f"     to use one: copy it to {_relative(config.master)} and run build")
    return run.summary()


def _mask(shape: str, size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(mask)
    if shape == "circle":
        draw.ellipse((0, 0, big - 1, big - 1), fill=255)
    elif shape == "rounded-30":
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=round(0.30 * big), fill=255)
    else:
        ## Superellipse |x|^5 + |y|^5 = 1, close to the iOS and Pixel squircles.
        half = big / 2
        points = []
        for step in range(720):
            t = 2 * math.pi * step / 720
            c, s = math.cos(t), math.sin(t)
            points.append((
                half + half * math.copysign(abs(c) ** 0.4, c),
                half + half * math.copysign(abs(s) ** 0.4, s),
            ))
        draw.polygon(points, fill=255)
    return mask.resize((size, size), Image.BOX)


def _checker(size: int) -> Image.Image:
    board = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(board)
    cell = 16
    for y in range(0, size, cell):
        for x in range((y // cell % 2) * cell, size, 2 * cell):
            draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(222, 222, 222))
    return board


def _as_shown(image: Image.Image, target: Target, config: LogoConfig) -> Image.Image:
    """What a device draws before its mask: a transparent layer over its background or tint."""
    if target.layer == "monochrome":
        ground = Image.new("RGBA", image.size, (*THEMED[0], 255))
        ink = Image.new("RGBA", image.size, (*THEMED[1], 0))
        ink.putalpha(image.getchannel("A"))
        ground.alpha_composite(ink)
        return ground.convert("RGB")
    if image.mode == "RGBA":
        ground = Image.new("RGBA", image.size, (*config.background, 255))
        ground.alpha_composite(image)
        return ground.convert("RGB")
    return image.convert("RGB")


def guides(rendered: dict[Target, Image.Image], config: LogoConfig) -> Image.Image:
    """One row per target: the file on a checkerboard with its safe circle (cyan) and viewport
    (magenta) drawn in, then the viewport under each of the platform's masks."""
    rows = []
    for target, image in rendered.items():
        raw = _checker(THUMB)
        thumb = image.convert("RGBA").resize((THUMB, THUMB), Image.LANCZOS)
        raw.paste(thumb, (0, 0), thumb)
        draw = ImageDraw.Draw(raw)
        r = target.safe_circle * THUMB / 2
        draw.ellipse((THUMB / 2 - r, THUMB / 2 - r, THUMB / 2 + r, THUMB / 2 + r), outline=(0, 200, 230), width=2)
        v = target.viewport * THUMB / 2
        if target.viewport < 1:
            draw.rectangle((THUMB / 2 - v, THUMB / 2 - v, THUMB / 2 + v, THUMB / 2 + v), outline=(230, 0, 200), width=2)
        shown = _as_shown(image, target, config)
        crop = (1 - target.viewport) / 2 * shown.width
        view = shown.crop((round(crop), round(crop), round(shown.width - crop), round(shown.width - crop)))
        view = view.resize((THUMB, THUMB), Image.LANCZOS)
        panels = [raw]
        for shape in target.masks:
            panel = Image.new("RGB", (THUMB, THUMB), (255, 255, 255))
            panel.paste(view, (0, 0), _mask(shape, THUMB))
            panels.append(panel)
        rows.append((f"{target.name}  ({target.size} px, {target.layer}; masks: {', '.join(target.masks) or 'none'})", panels))
    width = MARGIN + max(len(panels) for _, panels in rows) * (THUMB + MARGIN)
    height = MARGIN + len(rows) * (LABEL + THUMB + MARGIN)
    sheet = Image.new("RGB", (width, height), (246, 246, 246))
    draw = ImageDraw.Draw(sheet)
    y = MARGIN
    for caption, panels in rows:
        draw.text((MARGIN, y + 8), caption, fill=(40, 40, 40))
        y += LABEL
        for column, panel in enumerate(panels):
            sheet.paste(panel, (MARGIN + column * (THUMB + MARGIN), y))
        y += THUMB + MARGIN
    return sheet


def _cmd_build(args: argparse.Namespace) -> int:
    config = load_config()
    out_dir = Path(args.out).expanduser() if args.out else None
    paths = {target: ((out_dir / f"{target.name}.png",) if out_dir else where) for target, where in config.outputs.items()}
    guides_path = (out_dir or config.default_out_dir) / "guides.png"
    run = Run()
    if not config.master.is_file():
        run.fail("master", f"no master at {_relative(config.master)} — run generate and copy a draft there",
                 clear_outputs([*(path for where in paths.values() for path in where), guides_path]))
        return run.summary()
    with Image.open(config.master) as master:
        motif, findings = cut_out(master, config.key_color)
    print(f"     master: {_relative(config.master)} (motif {motif.image.width}x{motif.image.height})")
    rendered = {}
    for target, where in paths.items():
        image = render(motif, target, background=config.background, fill=config.fill_for(target))
        for path in where:
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path, format="PNG", optimize=True)
            print(f"ok   {target.name}: {_relative(path)} ({image.width}x{image.height}, {image.mode})")
        rendered[target] = image
    if args.guides:
        guides_path.parent.mkdir(parents=True, exist_ok=True)
        guides(rendered, config).save(guides_path, format="PNG")
        print(f"     guides: {_relative(guides_path)}")
    run.warn("master", findings)
    return run.summary()


def build_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Draw app-icon drafts and build the icon set.")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="ask the image model for drafts; writes only drafts")
    generate.add_argument("description", help="what the icon shows, or with --from, what to change")
    generate.add_argument("-n", "--count", type=int, default=4, help="how many drafts (default 4)")
    generate.add_argument("--from", dest="reference", help="a draft to revise instead of starting anew")
    generate.add_argument("--out", help="directory under which drafts/<time>/ is made (default: default_out_dir)")
    build = commands.add_parser("build", help="derive every target from the master, always")
    build.add_argument("--out", help="write <target>.png here instead of to the logo config's paths")
    build.add_argument("--guides", action="store_true", help="also write guides.png with the safe circles and masks")
    generate.set_defaults(func=_cmd_generate)
    build.set_defaults(func=_cmd_build)
    return parser


def main(argv: list[str] | None = None, *, prog: str) -> int:
    args = build_parser(prog=prog).parse_args(argv)
    try:
        return args.func(args)
    except (LogoConfigError, LogoGenerationError) as error:
        print(f"FAIL {error}")
        return 1


__all__ = ["build_parser", "guides", "main"]
