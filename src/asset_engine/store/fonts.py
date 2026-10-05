"""Headline font resolution with script-aware fallback.

Layer: asset engine.
Rules:
  - Selection is coverage-driven, not a hardcoded locale -> font table: the first face in the
    project's ``font_priority`` whose cmap covers every codepoint of the caption wins. Adding a
    locale needs no code change; adding a script needs only a font file.
  - Never render tofu. If no bundled face covers the text, raise with the missing codepoints.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont
from fontTools.ttLib import TTFont

from asset_engine.project import get_project

## Codepoints that are layout, not content — they are stripped before rendering, so a font need
## not cover them to be eligible.
IGNORED_CODEPOINTS = frozenset({0x20, 0x0A})


class FontCoverageError(RuntimeError):
    """No bundled font covers every codepoint of a caption."""


@lru_cache(maxsize=8)
def _cmap(path: Path) -> frozenset[int]:
    if not path.is_file():
        raise FileNotFoundError(f"Bundled font missing: {path}")
    return frozenset(TTFont(str(path)).getBestCmap())


def missing_codepoints(text: str, path: Path) -> list[str]:
    """Characters of ``text`` that ``path`` cannot render, in first-seen order."""
    cmap = _cmap(path)
    seen: dict[str, None] = {}
    for char in text:
        if ord(char) in IGNORED_CODEPOINTS or ord(char) in cmap:
            continue
        seen.setdefault(char, None)
    return list(seen)


def font_priority() -> tuple[Path, ...]:
    """The project's brand faces, highest priority first — what the store assets are set in."""
    return get_project().font_priority


def font_path_for_text(text: str, *, label: str = "", priority: tuple[Path, ...] | None = None) -> Path:
    """First font in ``priority`` that fully covers ``text``.

    ``priority`` is the project's ``font_priority`` for the store assets. The print flyer puts its
    own faces in front of that list, so it keeps the same fallback for a script its faces do not
    cover.
    """
    gaps: dict[Path, list[str]] = {}
    for path in priority or font_priority():
        missing = missing_codepoints(text, path)
        if not missing:
            return path
        gaps[path] = missing

    detail = "; ".join(
        f"{path.name} missing {''.join(chars)!r} ({', '.join(f'U+{ord(c):04X}' for c in chars)})"
        for path, chars in gaps.items()
    )
    where = f" [{label}]" if label else ""
    raise FontCoverageError(f"No bundled font covers{where} {text!r} — {detail}")


@lru_cache(maxsize=256)
def _truetype(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def font_for_text(
    text: str,
    size: int,
    *,
    label: str = "",
    priority: tuple[Path, ...] | None = None,
) -> ImageFont.FreeTypeFont:
    """Load the highest-priority bundled font that can render ``text`` at ``size``."""
    return _truetype(font_path_for_text(text, label=label, priority=priority), size)


__all__ = [
    "FontCoverageError",
    "font_for_text",
    "font_priority",
    "font_path_for_text",
    "missing_codepoints",
]
