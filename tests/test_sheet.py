from __future__ import annotations

from asset_engine.rollups.config import printer_sheet


def test_the_wirmachendruck_rollup_file_is_the_size_its_data_sheet_asks_for():
    """WIRmachenDRUCK's Premium Roll-Up 85x200 data sheet: Datenformat 856 x 2186 mm with 3 mm
    Beschnitt, of which 10 mm at the top vanish under the rail and 170 mm at the bottom in the
    cassette, leaving 850 x 2000 mm visible. At 150 dpi (25.4 mm to the inch) the file is
    5055 x 12909 px and the visible area starts 13 mm = 77 px below its top edge.

    Fails if the profile in printers.json or the per-edge arithmetic drifts — either way the shop
    would trim or roll away part of the artwork.
    """
    sheet = printer_sheet("wirmachendruck-premium-85x200", 150)

    assert sheet.canvas == (5055, 12909)
    left, top, right, bottom = sheet.visible_mm
    assert (right - left, bottom - top) == (850, 2000)
    assert sheet.visible_box[1] == 77


def test_the_flyeralarm_rollup_file_is_the_size_its_data_sheet_asks_for():
    """Flyeralarm Roll-Up Classic 85x200: Datenformat 870 x 2270 mm (10 mm Beschnitt), 10 mm hidden
    under the rail and 250 mm in the cassette, so 1990 mm are seen — the sheet's 2000 mm include the
    rail strip. At 150 dpi that is 5138 x 13406 px."""
    sheet = printer_sheet("flyeralarm-classic-85x200", 150)

    assert sheet.canvas == (5138, 13406)
    left, top, right, bottom = sheet.visible_mm
    assert (right - left, bottom - top) == (850, 1990)
