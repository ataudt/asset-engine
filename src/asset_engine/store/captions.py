"""Read the screenshot headline lines out of ``appstore/locales/<code>/screenshots.json``.

Layer: asset engine.
Rules:
  - The per-locale JSON files are the source of truth. An aggregate an app generates from them
    (NutriSpy's ``appstore/promotional/screenshots.md``) is never read back.
  - The locale set is the project's ``locales``, not a filesystem glob — the app's translation
    step, which also owns these files, works from the same list.

``sublines`` is the optional small line under a headline, an object keyed by caption number; see
``read_sublines``.

Every layer is a directory, ``appstore/locales/<code>/<slug>/screenshots.json``, the app's own set
under ``default``. A custom product page overrides the frames it owns, resolved through the same
fallback chain as its listing copy. In every layer ``captions`` is an **object keyed by frame
number**, not an array: a page states the one or two frames it re-aims, and in ``default`` the key
is what lets an app's aligner (NutriSpy's ``align_locales.py appstore``) see one new frame as one
missing caption — to the aligner an array is a single value, present in every locale, so a ninth
frame would never be translated and a changed one could only be re-sent all at once. ``default`` must be numbered 1..N without gaps.

A caption is a **list of lines**, and each line is a hard line break in the rendered headline. It
used to be one markdown string with ``·`` / ``—`` acting as break markers that this module split
back out with a regex; storing the lines directly means the layout is data, a translator cannot
break it by dropping a marker, and the aligner can carry it between locales.
"""

from __future__ import annotations

import json
from pathlib import Path

from asset_engine.store.config import (
    ScreenshotConfig,
    load_config,
    variant_slot_numbers,
)
from asset_engine.project import get_project


class CaptionError(RuntimeError):
    """A locale file is missing, malformed, or out of step with the other locales."""


def promotional_path(
    language: str,
    config: ScreenshotConfig | None = None,
    slug: str | None = None,
) -> Path:
    """One layer's headlines for one locale. ``slug`` defaults to the app's own set.

    One formula for every layer, the default listing included — which is what the flattening of
    ``appstore/locales/`` bought: the layer is a directory name, so there is no second path shape
    for the set that happens to be the app's own.

    The default is ``default`` and deliberately **not** ``config.variant``: the callers below read
    the full caption set and then lay a page's frame-keyed overrides on top, so a page's
    config must still send them to the app's own file. A page's own layers are named explicitly,
    by ``_variant_overrides``.
    """
    config = config or load_config()
    return config.promotional_dir / f"{language}" / (slug or get_project().pages.default()) / "screenshots.json"


def _variant_overrides(
    language: str,
    config: ScreenshotConfig,
    key: str,
) -> dict[int, list[str]]:
    """``captions`` or ``sublines`` for one page, its chain applied, keyed by caption number.

    The default's file is read separately, by the caller, because it alone must number every frame
    1..N, while a page may only state frames it owns. So this walks ``PageRegistry.overlay_layers``,
    which is the chain without its foot. For
    the default set that list is empty and the walk is a no-op, which is why no layer is tested for
    here. Every absent layer is an empty overlay, the base included: a page may carry sublines and
    no captions, and ``symptom`` may carry nothing while a page that chains through it carries its
    own.
    """
    owned = set(variant_slot_numbers())
    merged: dict[int, list[str]] = {}
    for layer in get_project().pages.overlay_layers(config.variant):
        path = promotional_path(language, config, layer)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CaptionError(f"[{language}/{layer}] invalid JSON in {path}: {exc}") from exc
        block = data.get(key) or {}
        if not isinstance(block, dict):
            raise CaptionError(
                f"[{language}/{layer}] {key!r} in {path} must be an object keyed by caption "
                f"number — a page states the frames it re-aims, not all of them"
            )
        for caption, lines in block.items():
            try:
                number = int(caption)
            except (TypeError, ValueError):
                raise CaptionError(
                    f"[{language}/{layer}] {key!r} in {path} is keyed by {caption!r}, "
                    f"not a caption number"
                ) from None
            if number not in owned:
                raise CaptionError(
                    f"[{language}/{layer}] {key} {number} in {path}: a page owns frames "
                    f"{sorted(owned)} and inherits the rest, so this copy would never be rendered "
                    f"while the page shipped the main set's frame {number}"
                )
            if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
                raise CaptionError(
                    f"[{language}/{layer}] {key} {number} in {path} must be a list of strings"
                )
            stripped = [line.strip() for line in lines if line.strip()]
            if not stripped:
                raise CaptionError(f"[{language}/{layer}] {key} {number} in {path} is empty")
            merged[number] = stripped
    return merged


def read_captions(
    language: str,
    config: ScreenshotConfig | None = None,
) -> list[list[str]]:
    """Screenshot headlines for one locale, in frame order (caption 1 first).

    Each caption is its list of rendered lines. The list is always the **whole** set, so a caller
    may index it by ``slot.caption - 1`` whatever it was asked for.

    For an Apple custom product page the frames it owns carry its own headlines and every other
    frame keeps the default listing's, which is what the page ships for those anyway.
    """
    path = promotional_path(language, config)
    if not path.is_file():
        raise CaptionError(f"[{language}] missing screenshots file: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaptionError(f"[{language}] invalid JSON in {path}: {exc}") from exc

    captions = data.get("captions")
    if not isinstance(captions, dict) or not captions:
        raise CaptionError(f"[{language}] no 'captions' object keyed by frame number in {path}")

    by_number: dict[int, list[str]] = {}
    for key, caption in captions.items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            raise CaptionError(
                f"[{language}] 'captions' in {path} is keyed by {key!r}, not a frame number"
            ) from None
        ## A bare string would silently render as one line; that is a malformed file, not a
        ## shorthand, because the whole point of the list is that the line break is explicit.
        if not isinstance(caption, list) or not all(isinstance(line, str) for line in caption):
            raise CaptionError(
                f"[{language}] caption {number} in {path} must be a list of strings, "
                f"got {type(caption).__name__}"
            )
        lines = [line.strip() for line in caption if line.strip()]
        if not lines:
            raise CaptionError(f"[{language}] caption {number} in {path} is empty")
        by_number[number] = lines

    ## A gap is a frame the aligner has not translated yet (or one a hand edit dropped). Named here,
    ## it reads as "frame 5 missing in fr" instead of surfacing later as a count mismatch.
    gaps = sorted(set(range(1, max(by_number) + 1)) - set(by_number))
    if gaps:
        raise CaptionError(
            f"[{language}] captions {gaps} missing in {path} — frames are numbered 1..N without "
            f"gaps; a translated locale is missing frames the source locale has"
        )
    normalized = [by_number[number] for number in range(1, len(by_number) + 1)]

    if config is not None and config.is_custom_page:
        for number, lines in _variant_overrides(language, config, "captions").items():
            if number > len(normalized):
                raise CaptionError(
                    f"[{language}/{config.variant}] caption {number} has no frame in {path}, "
                    f"which carries {len(normalized)}"
                )
            normalized[number - 1] = lines
    return normalized


def read_sublines(
    language: str,
    config: ScreenshotConfig | None = None,
) -> dict[int, list[str]]:
    """The small lines under the headlines of one locale, keyed by caption number.

    Optional, per caption and per locale: ``sublines`` is an object and not an array, so a frame
    without one is simply absent, and a locale the aligner has not reached yet renders its frames
    without rather than failing the build — the headline is what a frame cannot do without.
    """
    path = promotional_path(language, config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptionError(f"[{language}] cannot read {path}: {exc}") from exc

    sublines = data.get("sublines") or {}
    if not isinstance(sublines, dict):
        raise CaptionError(f"[{language}] 'sublines' in {path} must be an object keyed by caption number")
    normalized: dict[int, list[str]] = {}
    for key, lines in sublines.items():
        if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
            raise CaptionError(f"[{language}] subline {key} in {path} must be a list of strings")
        normalized[int(key)] = [line.strip() for line in lines if line.strip()]
    if config is not None and config.is_custom_page:
        ## The same chain as the headlines, so ``cli._build_device``'s "which locales are missing
        ## the English subline" report counts a page's own sublines rather than reporting all
        ## sixteen locales as missing one the page just added.
        normalized.update(_variant_overrides(language, config, "sublines"))
    return normalized


def target_languages(codes: list[str] | None = None) -> list[str]:
    """Resolve ``--language`` codes against the project's locales; all of them when unset."""
    locales = get_project().locales
    if not codes:
        return list(locales)

    by_code = {str(lang): lang for lang in locales}
    resolved: list[str] = []
    for code in codes:
        lang = by_code.get(code.strip().lower())
        if lang is None:
            raise SystemExit(f"Unknown or unsupported locale: {code!r}")
        if lang not in resolved:
            resolved.append(lang)
    return resolved


def read_all_captions(
    languages: list[str] | None = None,
    config: ScreenshotConfig | None = None,
) -> dict[str, list[list[str]]]:
    """Captions for every requested locale, asserting they all carry the same number."""
    languages = languages or list(get_project().locales)
    by_language = {lang: read_captions(lang, config) for lang in languages}

    counts = {lang: len(caps) for lang, caps in by_language.items()}
    if len(set(counts.values())) > 1:
        summary = ", ".join(
            f"{lang}={n}" for lang, n in sorted(counts.items(), key=lambda kv: str(kv[0]))
        )
        raise CaptionError(
            f"Locales disagree on screenshot caption count — every locale must have the same "
            f"number of captions: {summary}"
        )
    return by_language


def caption_text(caption: list[str]) -> str:
    """One-line form, for aggregates and logs. The renderer uses the lines directly."""
    return " · ".join(caption)


__all__ = [
    "CaptionError",
    "caption_text",
    "promotional_path",
    "read_all_captions",
    "read_captions",
    "read_sublines",
    "target_languages",
]
