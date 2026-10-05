"""CLI for generating localized App Store screenshots.

Layer: asset engine.
Imports: asset_engine.store.*; the installed project answers locales and pages.
Rules:
  - ``--language`` accepts repeats and comma-separated values.
  - ``--device`` likewise, and it defaults to *every* device in config.json. The stores want both
    sets, so producing only the phone one has to be asked for rather than being what a bare
    ``build`` happens to do.
  - The tree is ``<default_out_dir>/<lang>/<set>/`` (NutriSpy: ``appstore/screenshots/``), locale
    first — the shape every other locale tree in an app repo should have.
  - ``--design`` is a single name and defaults to none: a bare ``build`` renders the look the
    stores get, into ``appstore/screenshots/<lang>/default/``. A design renders into
    ``appstore/screenshots/<lang>/<design>/``, so an experiment cannot be picked up by the
    metadata builder or fastlane by accident — which is also why a design named after a store page
    is refused: they share that level.
  - ``--variant`` names the store pages to render, repeatable and comma-separated, and ``default``
    — the app's own set — is one of them. Without it every page is rendered, ``default`` first: a
    bare ``build`` that produced the app's set alone is how a new page went unrendered until someone
    asked for it by name. A custom product page renders only the frames it owns
    (``variant_slots``) into ``appstore/screenshots/<lang>/<slug>/``, and it
    defaults ``--language`` to the locales that actually carry that page's copy, as
    an app's metadata builder does. A page and a design together are refused.
  - An app adds commands of its own through ``build_parser(extend=...)``; NutriSpy's caption
    aggregates are one (``aggregate.py``).
  - Nothing is capped silently: captions with no slot in config.json are reported on every run, and
    a page's frame that resolved its capture from the default set's map says so (``default:<code>``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from PIL import ImageDraw, Image

from asset_engine.store.captions import (
    CaptionError,
    read_all_captions,
    read_captions,
    read_sublines,
    target_languages,
)
from asset_engine.store.compose import (
    Mockup,
    RawCaptureError,
    compose_mockup,
    resolve_capture,
)
from asset_engine.store.feature_graphic import (
    OUTPUT_NAME as FEATURE_GRAPHIC_NAME,
    FeatureGraphicError,
    compose_feature_graphic,
    load_feature_config,
    read_headlines,
)
from asset_engine.store.config import (
    ScreenshotConfig,
    Slot,
    design_names,
    device_names,
    load_config,
)
from asset_engine.store.output import save_frame
from asset_engine.project import get_project
from asset_engine.store.render import layout_headline, render_slot

EPILOG = """\
examples:
  {prog} build                      # every page, default first
  {prog} build --language de,en
  {prog} build --device phone
  {prog} build --language de --device phone --force
  {prog} build --variant default
  {prog} build --variant <page>,<page> --force
  {prog} build --variant <page> --language de,en --dry-run
  {prog} compose --language de
  {prog} report
  {prog} feature-graphic --language en,de
"""

def _split_csv(values: list[str] | None) -> list[str]:
    """Flatten repeated and comma-separated ``--language`` values."""
    out: list[str] = []
    for value in values or []:
        out.extend(part for part in (p.strip() for p in value.split(",")) if part)
    return out


def _devices(args: argparse.Namespace) -> list[str]:
    """The devices to render, defaulting to all of them. Unknown names fail before any work."""
    known = device_names()
    requested = _split_csv(getattr(args, "device", None)) or list(known)
    unknown = [name for name in requested if name not in known]
    if unknown:
        raise SystemExit(f"unknown device(s): {', '.join(unknown)} — config.json has {', '.join(known)}")
    return requested


def _design(args: argparse.Namespace) -> str | None:
    """The design to render, or ``None`` for the look the stores get. Unknown names fail early."""
    name = getattr(args, "design", None)
    if name is not None and name not in design_names():
        raise SystemExit(
            f"unknown design: {name} — config.json has {', '.join(design_names()) or 'none'}"
        )
    return name


def _variants(args: argparse.Namespace) -> list[str]:
    """The store pages to render; without ``--variant``, all of them, ``default`` first.

    Validated against the configured slugs, not against ``variants.json``: a page that re-aims the
    billboard in words alone has no artwork entry and is still a page. ``PageRegistry.check_slug``
    is also what refuses a base other pages resolve through and that is never a page of its own
    (NutriSpy's ``symptom``). It may refuse the default too, so that name is accepted here.
    A design is another look of the app's own set and is refused together with a page, so with
    ``--design`` and no ``--variant`` the answer is ``default`` alone.
    """
    pages = get_project().pages
    names = _split_csv(getattr(args, "variant", None))
    if not names:
        if getattr(args, "design", None) is not None:
            return [pages.default()]
        return [pages.default(), *pages.slugs()]
    variants: list[str] = []
    for name in names:
        if name != pages.default():
            try:
                name = pages.check_slug(name)
            except ValueError as error:
                raise SystemExit(str(error)) from error
        if name not in variants:
            variants.append(name)
    return variants


def _languages(args: argparse.Namespace, variant: str) -> list[str]:
    """The locales to render. With a page and no ``--language``, the ones that carry its copy.

    The default an app's metadata builder should take too, and for the same reason: a page is
    translated where its copy exists, and a hardcoded pair is how eight pages once shipped in two
    languages because nobody had asked for more.
    """
    pages = get_project().pages
    codes = _split_csv(args.language)
    if not codes and pages.is_custom(variant):
        codes = pages.locales(variant)
        if not codes:
            raise SystemExit(f"no locale carries copy for variant {variant!r}")
    return target_languages(codes)


MOCKUPS_ROOT_NAME = "_mockups"


def _out_dir(args: argparse.Namespace, config: ScreenshotConfig, language: str) -> Path:
    """Where this set's frames for one locale are written: ``<root>/<locale>/<set>``.

    Per locale rather than a root the caller joins the locale onto, because the locale is the *outer*
    level: a root computed once outside the language loop cannot express that. ``--out`` repeats the
    renderer's own shape under a different root, so a tree built there reads like the real one.
    """
    if args.out:
        return Path(args.out).resolve() / language / config.out_subdir
    return config.frames_dir(language)


def _mockup_dir(args: argparse.Namespace, config: ScreenshotConfig, language: str) -> Path:
    """Where the text-free mockups for one locale go.

    Under one ``_mockups`` root rather than inside the set's own folder: the name is the whole
    containment — no builder and no fastlane lane walks a path with it in — and putting it at the
    locale's own level instead would place it where a page or a design goes, in the namespace
    ``load_config`` already polices for collisions.
    """
    if args.out:
        return Path(args.out).resolve() / language / config.out_subdir
    return config.default_out_dir / MOCKUPS_ROOT_NAME / language / config.out_subdir


def _log(
    message: str,
    *,
    language: str | None = None,
    device: str | None = None,
    stderr: bool = False,
) -> None:
    tag = "/".join(part for part in (str(language) if language else None, device) if part)
    prefix = f"[{tag}] " if tag else ""
    print(f"{prefix}{message}", file=sys.stderr if stderr else sys.stdout)


def _report_unmapped(config: ScreenshotConfig, caption_count: int) -> None:
    unmapped = sorted(set(range(1, caption_count + 1)) - config.mapped_captions)
    if unmapped:
        _log(
            f"note: caption(s) {', '.join(str(n) for n in unmapped)} have no slot in "
            f"config.json and are not rendered",
            stderr=True,
        )


def _mockup_for(
    config: ScreenshotConfig,
    lang: str,
    slot: Slot,
) -> tuple["Mockup | None", str]:
    """The text-free canvas for one slot, plus a short note on where it came from.

    The note is ``composed`` for this locale's own capture, ``composed:<code>`` for one borrowed
    along the fallback chain, so a frame silently showing another language's UI is reported, and
    ``missing`` (with no image) when not even the fallbacks have one.
    """
    if slot.phone is None:
        ## A frame without a phone shows no capture, so there is none to borrow or to miss.
        return compose_mockup(config, str(lang), slot), "composed"
    resolved = resolve_capture(config, str(lang), slot)
    if resolved is None:
        return None, "missing"
    source = resolved[1]
    if config.is_custom_page and config.variant_capture_stem(source, slot) is None:
        ## The page named no stem for the language that answered, so this frame is the default
        ## set's screen under the page's headline. Legitimate — a page re-aims the frames it has a
        ## reason to — but it has to be said, because the alternative was a default-artwork frame
        ## landing in a page's tree with nothing printed at all.
        return compose_mockup(config, str(lang), slot), f"{get_project().pages.default()}:{source}"
    note = "composed" if source == str(lang) else f"composed:{source}"
    return compose_mockup(config, str(lang), slot), note


def _report_source(source: str, lang: str, slot: Slot, device: str) -> int:
    """Say where a slot's artwork came from when it is not this locale's own capture.

    Returns what to add to the problem count. Only "nothing at all" counts: a borrowed frame still
    ships, and borrowing is how a locale gets a set before every screen is shot.
    """
    if source == "missing":
        _log(
            f"slot {slot.caption}: no raw capture, not even in the fallbacks",
            language=lang,
            device=device,
            stderr=True,
        )
        return 1
    if source.startswith(f"{get_project().pages.default()}:"):
        _log(
            f"slot {slot.caption}: the page names no capture of its own — showing the default set's "
            f"{source.split(':', 1)[1]} screen under the page's headline",
            language=lang,
            device=device,
            stderr=True,
        )
        return 0
    if source.startswith("composed:"):
        _log(
            f"slot {slot.caption}: no {lang} capture — using the {source.split(':', 1)[1]} one, "
            f"so its in-app text is not {lang}",
            language=lang,
            device=device,
            stderr=True,
        )
    return 0


def _compose_device(args: argparse.Namespace, device: str, variant: str) -> int:
    """Write one page's text-free mockups for one device; returns the problem count."""
    config = load_config(device, _design(args), variant)
    languages = _languages(args, variant)
    problems = 0
    for lang in languages:
        out_dir = _mockup_dir(args, config, str(lang))
        out_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for slot in config.slots:
            try:
                mockup = compose_mockup(config, str(lang), slot)
            except RawCaptureError as error:
                problems += 1
                _log(str(error), language=lang, device=device, stderr=True)
                continue
            save_frame(mockup.image, out_dir / slot.template)
            written += 1
        _log(f"wrote {written} mockup(s) -> {out_dir}", language=lang, device=device)
    return problems


def _cmd_compose(args: argparse.Namespace) -> int:
    """Write the text-free mockups only — for reviewing artwork without the captions on top."""
    variants = _variants(args)
    problems = sum(
        _compose_device(args, device, variant) for variant in variants for device in _devices(args)
    )
    return 1 if problems else 0


def _build_device(args: argparse.Namespace, device: str, variant: str) -> int:
    """Render one page's set on one device for every requested locale; returns the problem count."""
    config = load_config(device, _design(args), variant)
    languages = _languages(args, variant)
    captions_by_language = read_all_captions(languages, config)

    ## A frame's subline is optional per locale, so a locale that lacks one the hand-written source
    ## has is named: its frame ships without, which is right until it is translated and wrong after.
    source_sublines = set(read_sublines(get_project().source_locale, config))

    problems = 0
    for lang in languages:
        captions = captions_by_language[lang]
        sublines = read_sublines(lang, config)
        missing = sorted(source_sublines - set(sublines))
        if missing:
            _log(
                f"no subline for caption(s) {', '.join(map(str, missing))} — rendered without; "
                f"translate them for this locale",
                language=lang,
                device=device,
                stderr=True,
            )
        out_dir = _out_dir(args, config, str(lang))
        if not args.dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        for slot in config.slots:
            caption = captions[slot.caption - 1]
            dest = out_dir / slot.template
            if dest.exists() and not args.force and not args.dry_run:
                _log(f"skip (exists, use --force): {dest.name}", language=lang, device=device)
                continue

            label = f"{lang}/{device} slot {slot.caption}"
            if args.dry_run:
                ## Lay out against a throwaway canvas so --dry-run still validates fit and fonts.
                draw = ImageDraw.Draw(Image.new("RGB", config.canvas))
                layout = layout_headline(
                    draw, caption, config.text, label=label, keep_lines=slot.keep_lines
                )
                image = None
                try:
                    _, source = _mockup_for(config, lang, slot)
                except RawCaptureError as error:
                    problems += 1
                    _log(str(error), language=lang, device=device, stderr=True)
                    continue
                problems += _report_source(source, lang, slot, device)
            else:
                try:
                    mockup, source = _mockup_for(config, lang, slot)
                except RawCaptureError as error:
                    problems += 1
                    _log(str(error), language=lang, device=device, stderr=True)
                    continue
                problems += _report_source(source, lang, slot, device)
                if mockup is None:
                    continue
                image, layout = render_slot(
                    mockup.image,
                    caption,
                    config,
                    band_bottom=mockup.phone_top,
                    subline=sublines.get(slot.caption),
                    label=label,
                    keep_lines=slot.keep_lines,
                )

            if layout.overflows:
                problems += 1
                _log(
                    f"OVERFLOW slot {slot.caption}: {len(layout.lines)} lines at {layout.size}px "
                    f"({layout.width}x{layout.height}px) — trim the caption",
                    language=lang,
                    device=device,
                    stderr=True,
                )
            if image is not None:
                ## Format and encoder settings live in ``output.save_frame`` — every writer and
                ## every downstream glob has to agree on the extension.
                save_frame(image, dest)
                written += 1

        _log(
            f"{'would write' if args.dry_run else 'wrote'} {written} file(s) -> {out_dir}",
            language=lang,
            device=device,
        )
    return problems


def _cmd_build(args: argparse.Namespace) -> int:
    ## Validated before anything is printed, so an unknown page fails as a line and not
    ## half-way through a table.
    variants = _variants(args)
    ## Without the variant on purpose: a page's config holds only the frames it owns, so every
    ## frame it inherits would read here as a caption with no artwork.
    base = load_config(design=_design(args))
    _report_unmapped(base, len(read_captions(get_project().source_locale, base)))
    problems = 0
    for variant in variants:
        _log(f"== {variant}")
        problems += sum(_build_device(args, device, variant) for device in _devices(args))
    if problems:
        _log(f"{problems} slot(s) had a problem — see the lines above", stderr=True)
    return 1 if problems else 0


def _report_device(args: argparse.Namespace, device: str, variant: str) -> int:
    config = load_config(device, _design(args), variant)
    languages = _languages(args, variant)
    captions_by_language = read_all_captions(languages, config)

    draw = ImageDraw.Draw(Image.new("RGB", config.canvas))
    problems = 0
    for lang in languages:
        captions = captions_by_language[lang]
        for slot in config.slots:
            caption = captions[slot.caption - 1]
            ## No mockup is composed here, so the layout uses the tightest band (worst case).
            layout = layout_headline(
                draw,
                caption,
                config.text,
                label=f"{lang} slot {slot.caption}",
                keep_lines=slot.keep_lines,
            )
            if layout.overflows:
                status, problems = "OVERFLOW", problems + 1
            elif layout.shrunk:
                status = "shrunk"
            else:
                status = "ok"
            print(
                f"{variant:<13}{device:<7}{str(lang):<5}{slot.caption:<6}{len(layout.lines):<7}{layout.size:<7}"
                f"{layout.width:<8}{layout.ink_top:<7}{layout.ink_bottom:<8}{status}"
            )
    return problems


def _cmd_report(args: argparse.Namespace) -> int:
    variants = _variants(args)
    ## Without the variant on purpose: a page's config holds only the frames it owns, so every
    ## frame it inherits would read here as a caption with no artwork.
    base = load_config(design=_design(args))
    _report_unmapped(base, len(read_captions(get_project().source_locale, base)))
    print(f"{'page':<13}{'dev':<7}{'lang':<5}{'slot':<6}{'lines':<7}{'size':<7}{'width':<8}{'top':<7}{'bottom':<8}status")
    problems = sum(
        _report_device(args, device, variant) for variant in variants for device in _devices(args)
    )
    if problems:
        _log(f"{problems} caption(s) do not fit", stderr=True)
    return 1 if problems else 0


def _cmd_feature_graphic(args: argparse.Namespace) -> int:
    """Render the 1024x500 Play feature graphic for the requested locales."""
    config = load_config()
    languages = target_languages(_split_csv(args.language))

    problems = 0
    for lang in languages:
        try:
            headlines = read_headlines(lang, config)
        except (CaptionError, json.JSONDecodeError) as error:
            problems += 1
            _log(str(error), language=lang, stderr=True)
            continue

        try:
            image = compose_feature_graphic(
                headlines,
                language=str(lang),
                phone_language=args.phone_language or load_feature_config().capture_language,
                screenshots=config,
                label=str(lang),
            )
        except (FeatureGraphicError, RawCaptureError) as error:
            problems += 1
            _log(str(error), language=lang, stderr=True)
            continue

        out_dir = _out_dir(args, config, str(lang))
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / FEATURE_GRAPHIC_NAME
        save_frame(image, dest)
        _log(f"wrote {dest}", language=lang)

    if problems:
        _log(f"{problems} locale(s) did not render — see above", stderr=True)
    return 1 if problems else 0


def _add_language_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--language",
        action="append",
        metavar="CODE",
        help="Locale code; repeatable and comma-separated (e.g. --language de,en). Default: all.",
    )


def _add_device_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        action="append",
        metavar="NAME",
        help=(
            f"Device set from config.json ({', '.join(device_names())}); repeatable and "
            f"comma-separated. Default: all of them — the stores want every set."
        ),
    )


def _add_variant_arg(parser: argparse.ArgumentParser) -> None:
    default = get_project().pages.default()
    pages = ", ".join([default, *get_project().pages.slugs()])
    parser.add_argument(
        "--variant",
        action="append",
        metavar="SLUG",
        help=(
            f"The store page(s) to render ({pages}); repeatable and comma-separated. "
            f"{default} is the app's own set, every frame; any other is an Apple "
            f"custom product page and renders only the frames it owns, into a tree of its own. "
            f"Default: all of them."
        ),
    )


def _add_design_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--design",
        metavar="NAME",
        help=(
            f"Render another look from config.json ({', '.join(design_names()) or 'none defined'}) "
            f"into a tree of its own. Default: the look the stores get."
        ),
    )


def build_parser(
    *,
    prog: str,
    extend: Callable[[argparse._SubParsersAction], None] | None = None,
    examples: tuple[str, ...] = (),
) -> argparse.ArgumentParser:
    """The store-set CLI. ``extend`` adds an app's own subcommands to it, ``examples`` their lines
    in the epilog (``{prog}`` is filled in)."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Generate localized App Store screenshots from the promotional caption bullets. "
            "build, compose and report take --variant to pick the store page; see `build --help`."
        ),
        epilog=EPILOG.format(prog=prog) + "".join(f"  {line.format(prog=prog)}\n" for line in examples),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Render screenshots for the requested locales.")
    _add_language_arg(build)
    _add_device_arg(build)
    _add_design_arg(build)
    _add_variant_arg(build)
    build.add_argument("--out", metavar="DIR", help="Output root (default: config default_out_dir).")
    build.add_argument("--force", action="store_true", help="Overwrite existing output files.")
    build.add_argument("--dry-run", action="store_true", help="Check fit, fonts and captures; write nothing.")
    build.set_defaults(func=_cmd_build)

    compose = sub.add_parser("compose", help="Write the text-free mockups only, for artwork review.")
    _add_language_arg(compose)
    _add_device_arg(compose)
    _add_design_arg(compose)
    _add_variant_arg(compose)
    compose.add_argument(
        "--out",
        metavar="DIR",
        help=f"Output root (default: the {MOCKUPS_ROOT_NAME} root beside the rendered sets).",
    )
    compose.set_defaults(func=_cmd_compose)

    report = sub.add_parser("report", help="Print the fit table without rendering anything.")
    _add_language_arg(report)
    _add_device_arg(report)
    _add_design_arg(report)
    _add_variant_arg(report)
    report.set_defaults(func=_cmd_report)

    feature = sub.add_parser(
        "feature-graphic",
        help="Render the 1024x500 Google Play feature graphic per locale.",
    )
    _add_language_arg(feature)
    feature.add_argument("--out", metavar="DIR", help="Output root (default: config default_out_dir).")
    feature.add_argument(
        "--phone-language",
        metavar="CODE",
        help="Locale whose raw captures fill the device screens (default: capture_language in "
             "feature_graphic.json — at ~120px wide the in-app text is illegible, so only the "
             "headlines are localized).",
    )
    feature.set_defaults(func=_cmd_feature_graphic)

    if extend is not None:
        extend(sub)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    prog: str,
    extend: Callable[[argparse._SubParsersAction], None] | None = None,
    examples: tuple[str, ...] = (),
) -> int:
    args = build_parser(prog=prog, extend=extend, examples=examples).parse_args(argv)
    return args.func(args)


__all__ = ["EPILOG", "build_parser", "main"]
