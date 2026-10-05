"""QR codes for print, drawn with Pillow from a ``segno`` module matrix.

Layer: asset engine.
Rules:
  - A module is a whole number of pixels. A code scaled to an arbitrary width gets modules of
    uneven size, which is what makes a printed code hard to read; the code is therefore drawn at
    the largest whole module that fits its card and centred, and the card takes up the slack.
  - Plain square modules, nothing in the middle. A logo in the code spends error correction on
    decoration, and these are scanned off paper at arm's length.
"""

from __future__ import annotations

import segno
from PIL import Image, ImageDraw

## Level M restores about 15% of a damaged code, which covers a crease or a scuff; the short
## addresses in use fit a 29-module code at this level.
ERROR_LEVEL = "m"


class QrError(RuntimeError):
    """The code cannot be drawn legibly on the card it was given."""


def qr_matrix(data: str) -> tuple[tuple[bool, ...], ...]:
    """The code for ``data`` as rows of modules, ``True`` for dark, without a quiet zone."""
    code = segno.make(data, error=ERROR_LEVEL, micro=False)
    return tuple(tuple(bool(module) for module in row) for row in code.matrix)


def module_size(modules: int, card: int, quiet_modules: int) -> int:
    """Pixels per module for a code of ``modules`` on a ``card`` pixels wide."""
    return card // (modules + 2 * quiet_modules)


def render_qr_card(
    data: str,
    size: int,
    *,
    quiet_modules: int,
    corner_radius: int,
    dark: tuple[int, int, int],
) -> Image.Image:
    """A white rounded card ``size`` pixels square with the code for ``data`` centred on it."""
    matrix = qr_matrix(data)
    modules = len(matrix)
    module = module_size(modules, size, quiet_modules)
    if module < 1:
        raise QrError(f"a {modules}-module code does not fit a {size}px card")

    card = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=corner_radius, fill=(255, 255, 255, 255))

    origin = (size - modules * module) // 2
    for row_index, row in enumerate(matrix):
        for column_index, is_dark in enumerate(row):
            if not is_dark:
                continue
            left = origin + column_index * module
            top = origin + row_index * module
            draw.rectangle((left, top, left + module - 1, top + module - 1), fill=(*dark, 255))
    return card


__all__ = ["ERROR_LEVEL", "QrError", "module_size", "qr_matrix", "render_qr_card"]
