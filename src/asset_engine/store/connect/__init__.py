"""The App Store Connect API, for the store work ``deliver`` cannot do.

Layer: asset engine, store upload. Needs the ``store`` extra (PyJWT, requests).

fastlane covers an app's main listing. What it does not model is the **App Asset Library**, through
which the creative assets (product page header, search results image) are placed on a version's
localizations (``creatives``), nor custom product pages, which an app builds on ``client`` and
``assets``. The engine holds no key: the app builds the client with its own ``key_getter``.
"""

from asset_engine.store.connect.client import (
    API_ROOT,
    NETWORK_ERRORS,
    AppStoreConnectClient,
    AppStoreConnectError,
    MainListingRefused,
)


__all__ = [
    "API_ROOT",
    "NETWORK_ERRORS",
    "AppStoreConnectClient",
    "AppStoreConnectError",
    "MainListingRefused",
]
