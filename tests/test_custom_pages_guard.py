"""The custom-page client must not be able to write the app's own product page.

Moved from NutriSpy's ``sanipy/tests/test_appstore_listing.py`` with the code. The client holds a
team key that can edit the live listing, and it exists for custom product pages, which may differ in
screenshots, previews and promotional text only — each with a CPP-scoped resource. A localization id
pasted into the wrong call would replace the general-audience listing with an audience-specific one,
silently and in every locale it names.
"""

from __future__ import annotations

import pytest

from asset_engine.store.connect import MainListingRefused
from asset_engine.store.connect.custom_pages import custom_page_client


def test_the_custom_page_client_refuses_the_apps_main_listing() -> None:
    client = custom_page_client(lambda: {})
    with pytest.raises(MainListingRefused):
        client._guard("appStoreVersionLocalizations/12345")
    with pytest.raises(MainListingRefused):
        client._guard(
            "appScreenshotSets",
            {"data": {"relationships": {"appStoreVersionLocalization": {"data": {"id": "1"}}}}},
        )
