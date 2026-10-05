from __future__ import annotations

from asset_engine.flyers.config import Page


def test_an_a6_sheet_with_bleed_is_the_size_print_shops_ask_for():
    """A6 is 105 x 148 mm; with 3 mm bleed on every side a printer wants 111 x 154 mm, which at
    300 dpi (25.4 mm to the inch) is 1311 x 1819 px. The trim edge then sits 3 mm = 35 px in.

    Fails if the millimetre-to-pixel conversion drifts — a rounding change, a wrong inch, or the
    bleed counted once instead of twice — any of which ships a PDF the printer rejects or trims
    into the artwork.
    """
    page = Page.of(trim_mm=(105, 148), bleed_mm=3, safe_mm=2, dpi=300)

    assert page.canvas == (1311, 1819)
    assert page.trim_box == (35, 35, 1276, 1783)
