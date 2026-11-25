"""The App Store Connect API: everything an app publishes on the App Store but its binary.

Layer: asset engine, store publishing. Needs the ``store`` extra (PyJWT, requests, google-auth).

``client`` is the authenticated HTTP; ``app`` finds the record, its version and its settings;
``listing`` writes the localizations, screenshots and review details (in place of fastlane's
``deliver``); ``record`` the rest of the record; ``subscriptions`` the products and prices;
``creatives`` places the creative assets through the App Asset Library; ``assets`` is the upload
under all of them, and custom product pages build on it. The engine holds no key: the app hands it
over as ``PublishFiles.apple_key``. The binary is EAS's (``eas submit``).
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
