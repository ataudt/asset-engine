"""Write composed pages as a print PDF.

Layer: asset engine.
Rules:
  - RGB raster pages at the sheet's resolution, the page size being the whole data format (trim
    plus bleed). The print shops this is used with convert RGB to CMYK themselves; a CMYK/PDF-X
    path (ICC output intent, TrimBox/BleedBox) is a later addition, not a silent assumption.
  - A PDF is a function of its pages. Pillow stamps ``/CreationDate`` and ``/ModDate`` with the
    wall clock, so two builds of the same flyer differed in six bytes and a hash could not tell
    "nothing changed" from "the flyer changed"; both dates are fixed here.
"""

from __future__ import annotations

from pathlib import Path
import time

from PIL import Image

## The date every PDF carries. Any constant would do; a real one keeps viewers that show it sane.
## A ``struct_time`` because that is the type Pillow's PDF writer serializes as a PDF date.
FIXED_DATE = time.strptime("2026-01-01", "%Y-%m-%d")


def save_pdf(images: list[Image.Image], path: Path, *, dpi: int) -> None:
    """Write ``images`` as one PDF, one page each, at ``dpi``."""
    first, *rest = images
    path.parent.mkdir(parents=True, exist_ok=True)
    first.save(
        path,
        format="PDF",
        save_all=True,
        append_images=rest,
        resolution=float(dpi),
        quality=95,
        creationDate=FIXED_DATE,
        modDate=FIXED_DATE,
    )


__all__ = ["FIXED_DATE", "save_pdf"]
