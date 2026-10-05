"""A printed product's text: one hand-written JSON file per language.

Layer: asset engine.
Rules:
  - The files present are the languages there are. Nothing lists them, and no translation script
    owns them: print copy is short, written for one run, and checked on paper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CopyError(RuntimeError):
    """A language has no copy file."""


def read_copy(copy_dir: Path, language: str, *, product: str) -> dict[str, Any]:
    """The text of ``product`` in ``language`` from ``copy_dir/<language>.json``."""
    path = copy_dir / f"{language}.json"
    if not path.is_file():
        have = sorted(p.stem for p in copy_dir.glob("*.json"))
        raise CopyError(f"no {product} copy for {language!r}: {path} is missing (there is {have})")
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["CopyError", "read_copy"]
