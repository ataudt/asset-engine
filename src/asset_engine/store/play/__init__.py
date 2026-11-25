"""The Google Play Developer API, for everything an app publishes on Play but its binary.

Layer: asset engine, store publishing. Needs the ``store`` extra (google-auth, requests).

``client`` is the authenticated HTTP, bound to one package; ``listing`` writes the store listing
(text, images, What's New) through one edit; ``subscriptions`` the products, base plans and prices.
The engine holds no key: the app hands the service account over as ``PublishFiles.play_key``. The
binary is EAS's (``eas submit``), and the very first one of a new app is uploaded by hand, as Play's
API refuses a package it has never seen.
"""

from asset_engine.store.play.client import NETWORK_ERRORS, PlayApiError, PlayClient


__all__ = ["NETWORK_ERRORS", "PlayApiError", "PlayClient"]
