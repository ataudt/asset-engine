"""Screenshot and asset-library upload: reserve, send, commit, wait.

Layer: asset engine, store upload.

An App Store screenshot is not a file POST. Apple hands back a set of ``uploadOperations`` — a
pre-signed destination per byte range — and the asset only exists once it has been committed with
the source file's MD5 and Apple has finished processing it. The last step is the one worth having
code for: an image Apple rejects (wrong dimensions, alpha channel) goes to ``FAILED`` *after* a
perfectly successful upload, so skipping the wait means reporting a page as pushed that is not.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import time

from asset_engine.store.connect.client import AppStoreConnectClient, AppStoreConnectError


logger = logging.getLogger(__name__)

## Apple processes a frame in seconds to about three minutes — or never: now and then a committed
## upload stays in UPLOAD_COMPLETE for good (11 h and counting on 2026-11-13), at random, on either
## device, while the same bytes sent again complete in minutes. It is Apple's processing, not the
## upload: web Console uploads stall the same way (developer.apple.com/forums/thread/848464). So a
## frame still unprocessed after STUCK_AFTER_SECONDS is deleted and sent again, up to MAX_SEND_ROUNDS.
DELIVERY_POLL_SECONDS = 3
STUCK_AFTER_SECONDS = 300
MAX_SEND_ROUNDS = 3


def checksum(blob: bytes) -> str:
    """The ``sourceFileChecksum`` Apple keeps for an uploaded asset: what tells a frame already in a
    set from a stale render of the same name."""
    ## Apple specifies MD5 for sourceFileChecksum. Not a security decision, and not ours to make.
    return hashlib.md5(blob, usedforsecurity=False).hexdigest()


def _delivery_error(screenshot: dict) -> str | None:
    state = (screenshot.get("attributes") or {}).get("assetDeliveryState") or {}
    if state.get("state") != "FAILED":
        return None
    warnings = state.get("warnings") or []
    errors = state.get("errors") or []
    return "; ".join(str(item) for item in (errors + warnings)) or "Apple rejected the asset"


def is_complete(screenshot: dict) -> bool:
    """Apple has finished processing the asset; ``UPLOAD_COMPLETE`` is not that yet."""
    state = (screenshot.get("attributes") or {}).get("assetDeliveryState") or {}
    return state.get("state") == "COMPLETE"


def upload_screenshots(client: AppStoreConnectClient, set_id: str, paths: list[Path]) -> list[str]:
    """Upload PNGs into ``set_id`` and return the new ``appScreenshots`` ids, in ``paths`` order.

    Every frame of a round is sent before any is waited on, so Apple processes the set in parallel.
    A frame Apple left unprocessed goes again in the next round, under a new id.
    """
    ids: dict[Path, str] = {}
    pending = list(paths)
    for round_number in range(1, MAX_SEND_ROUNDS + 1):
        sent = {_send_screenshot(client, set_id, path): path for path in pending}
        ids.update({path: screenshot_id for screenshot_id, path in sent.items()})
        stuck = _await_delivery(client, sent)
        if not stuck:
            return [ids[path] for path in paths]
        pending = [sent[screenshot_id] for screenshot_id in stuck]
        names = ", ".join(path.name for path in pending)
        if round_number == MAX_SEND_ROUNDS:
            break
        logger.warning("%s unprocessed after %ss, sending again", names, STUCK_AFTER_SECONDS)
        for screenshot_id in stuck:
            client.delete(f"appScreenshots/{screenshot_id}")
    raise AppStoreConnectError(
        f"{names} unprocessed after {MAX_SEND_ROUNDS} sends — Apple's processing stalls; a re-run "
        f"keeps every frame that completed and sends only these"
    )


def _send_screenshot(client: AppStoreConnectClient, set_id: str, path: Path) -> str:
    """Reserve, send and commit one PNG; return its ``appScreenshots`` id. Processing is not awaited."""
    blob = path.read_bytes()
    reserved = client.post(
        "appScreenshots",
        {
            "data": {
                "type": "appScreenshots",
                "attributes": {"fileName": path.name, "fileSize": len(blob)},
                "relationships": {
                    "appScreenshotSet": {"data": {"type": "appScreenshotSets", "id": set_id}}
                },
            }
        },
    )["data"]

    for operation in reserved["attributes"].get("uploadOperations") or []:
        offset = operation["offset"]
        client.upload_asset_chunk(
            operation["method"],
            operation["url"],
            {header["name"]: header["value"] for header in operation.get("requestHeaders") or []},
            blob[offset : offset + operation["length"]],
        )

    screenshot_id = reserved["id"]
    client.patch(
        f"appScreenshots/{screenshot_id}",
        {
            "data": {
                "type": "appScreenshots",
                "id": screenshot_id,
                "attributes": {"uploaded": True, "sourceFileChecksum": checksum(blob)},
            }
        },
    )
    return screenshot_id


def _await_delivery(client: AppStoreConnectClient, sent: dict[str, Path]) -> list[str]:
    """Wait for ``sent`` to finish processing; return the ids still unprocessed at the deadline.

    A rejected frame raises: Apple refused the image itself, and the same bytes would fail again.
    """
    deadline = time.monotonic() + STUCK_AFTER_SECONDS
    pending = dict(sent)
    while True:
        for screenshot_id, path in list(pending.items()):
            screenshot = client.get(f"appScreenshots/{screenshot_id}")["data"]
            failure = _delivery_error(screenshot)
            if failure:
                raise AppStoreConnectError(f"{path.name} was rejected by App Store Connect: {failure}")
            if is_complete(screenshot):
                del pending[screenshot_id]
        if not pending or time.monotonic() > deadline:
            return list(pending)
        time.sleep(DELIVERY_POLL_SECONDS)


## -- the App Asset Library ---------------------------------------------------------------------
##
## The successor to the set-based screenshot resources (developer.apple.com/documentation/
## appstoreconnectapi/uploading-and-managing-image-assets, read 2026-11-17). Same reserve / send /
## commit shape, three differences: the commit takes `uploaded` alone (no checksum — Apple checks the
## bytes against the declared `fileSize`), the state is a plain `state` attribute, and a processed
## image is `PREPARE_FOR_SUBMISSION`, ready to place, not `COMPLETE`.

LIBRARY_READY_STATES = frozenset({"PREPARE_FOR_SUBMISSION", "APPROVED"})


@dataclass(frozen=True)
class LibraryUpload:
    """One image for the asset library: the file, Apple's asset category, and the name it is filed
    under — which is how a later run recognises it (see ``creatives``)."""

    path: Path
    category: str
    reference_name: str


def upload_library_images(
    client: AppStoreConnectClient, library_id: str, uploads: list[LibraryUpload]
) -> dict[Path, str]:
    """Send ``uploads`` into the app's asset library; return each path's ``appAssetLibraryImages`` id
    once Apple has processed it far enough to be placed.

    The same rounds as ``upload_screenshots``: every image of a round is sent before any is waited
    on, and one Apple left unprocessed after ``STUCK_AFTER_SECONDS`` is deleted and sent again.
    """
    ids: dict[Path, str] = {}
    pending = list(uploads)
    for round_number in range(1, MAX_SEND_ROUNDS + 1):
        sent = {_send_library_image(client, library_id, upload): upload.path for upload in pending}
        ids.update({path: image_id for image_id, path in sent.items()})
        stuck = _await_library_images(client, sent)
        if not stuck:
            return ids
        stuck_paths = {sent[image_id] for image_id in stuck}
        pending = [upload for upload in pending if upload.path in stuck_paths]
        names = ", ".join(upload.path.name for upload in pending)
        if round_number == MAX_SEND_ROUNDS:
            break
        logger.warning("%s unprocessed after %ss, sending again", names, STUCK_AFTER_SECONDS)
        for image_id in stuck:
            client.delete(f"appAssetLibraryImages/{image_id}")
    raise AppStoreConnectError(
        f"{names} unprocessed after {MAX_SEND_ROUNDS} sends — Apple's processing stalls; a re-run "
        f"finds every image that did complete by its reference name and sends only these"
    )


def _send_library_image(client: AppStoreConnectClient, library_id: str, upload: LibraryUpload) -> str:
    """Reserve, send and commit one image; return its id. Processing is not awaited."""
    blob = upload.path.read_bytes()
    reserved = client.post(
        "appAssetLibraryImages",
        {
            "data": {
                "type": "appAssetLibraryImages",
                "attributes": {
                    "fileName": upload.path.name,
                    "fileSize": len(blob),
                    "category": upload.category,
                    "referenceName": upload.reference_name,
                },
                "relationships": {
                    "assetLibrary": {"data": {"type": "appAssetLibraries", "id": library_id}}
                },
            }
        },
    )["data"]
    for operation in reserved["attributes"].get("uploadOperations") or []:
        offset = operation["offset"]
        client.upload_asset_chunk(
            operation["method"],
            operation["url"],
            {header["name"]: header["value"] for header in operation.get("requestHeaders") or []},
            blob[offset : offset + operation["length"]],
        )
    image_id = reserved["id"]
    client.patch(
        f"appAssetLibraryImages/{image_id}",
        {"data": {"type": "appAssetLibraryImages", "id": image_id, "attributes": {"uploaded": True}}},
    )
    return image_id


def _await_library_images(client: AppStoreConnectClient, sent: dict[str, Path]) -> list[str]:
    """Wait for ``sent`` to become placeable; return the ids still unprocessed at the deadline.

    A rejected image raises: Apple refused the file itself, and the same bytes would fail again.
    """
    deadline = time.monotonic() + STUCK_AFTER_SECONDS
    pending = dict(sent)
    while True:
        for image_id, path in list(pending.items()):
            attributes = client.get(f"appAssetLibraryImages/{image_id}")["data"]["attributes"]
            state = attributes.get("state")
            if state == "FAILED":
                raise AppStoreConnectError(
                    f"{path.name} was rejected by App Store Connect: {attributes.get('stateDetails') or 'no details'}"
                )
            if state in LIBRARY_READY_STATES:
                del pending[image_id]
        if not pending or time.monotonic() > deadline:
            return list(pending)
        time.sleep(DELIVERY_POLL_SECONDS)


__all__ = [
    "LIBRARY_READY_STATES",
    "LibraryUpload",
    "checksum",
    "is_complete",
    "upload_library_images",
    "upload_screenshots",
]
