"""The App Store record an app publishes to: the app, its version, its app info, its settings.

Layer: asset engine, store publishing.
Rules:
  - The version is found, never guessed. Without a version string it is the one in preparation; a
    plan with none in preparation compares against the live one, which is what App Store Connect
    copies into a new version. With a version string, a version in preparation that carries another
    string is refused, unless the app has never been live: a new record starts with "1.0" while the
    builds carry the app's CalVer, so the first release may be renamed (``ensure_version``).
  - A version in review is never renamed or written to: only an editable state takes edits.
  - The record's settings (territories, primary language, categories, copyright, release type,
    content rights) are the app's decisions and live in ``appstore/store.json`` → ``apple``.
"""

from __future__ import annotations

from dataclasses import dataclass

from asset_engine.store.connect.client import AppStoreConnectClient, AppStoreConnectError
from asset_engine.store.listing import read_json
from asset_engine.store.report import Report


API_V2 = "https://api.appstoreconnect.apple.com/v2"

## A version Apple lets us edit: in preparation, or sent back.
EDITABLE_VERSION_STATES = frozenset(
    {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED", "INVALID_BINARY"}
)
LIVE_VERSION_STATES = frozenset({"READY_FOR_DISTRIBUTION", "READY_FOR_SALE"})
## A version that was on the store and is replaced or withdrawn.
ENDED_VERSION_STATES = frozenset({"REPLACED_WITH_NEW_VERSION", "REMOVED_FROM_SALE", "DEVELOPER_REMOVED_FROM_SALE"})
## An app-info record that is on the store and no longer takes edits.
LIVE_APP_INFO_STATES = frozenset({"READY_FOR_DISTRIBUTION", "READY_FOR_SALE"})


def ref(kind: str, ident: str) -> dict:
    return {"data": {"type": kind, "id": ident}}


def version_state(version: dict) -> str:
    attributes = version["attributes"]
    return attributes.get("appVersionState") or attributes.get("appStoreState") or ""


def resolve_app(client: AppStoreConnectClient, bundle_id: str) -> dict:
    """The App Store Connect app row with ``bundle_id``."""
    rows = list(client.list_all("apps", **{"filter[bundleId]": bundle_id}))
    if len(rows) != 1:
        raise AppStoreConnectError(f"expected one App Store Connect app with bundle id {bundle_id}, got {len(rows)}")
    return rows[0]


@dataclass(frozen=True)
class AppleSettings:
    """``store.json`` → ``apple``: where and how the app is sold."""

    ## ISO alpha-3 territories, or ``None`` for every territory Apple has.
    territories: tuple[str, ...] | None
    base_territory: str
    primary_locale: str
    content_rights: str
    copyright: str | None
    primary_category: str | None
    secondary_category: str | None
    release_type: str
    available_in_new_territories: bool
    ## Remove every store language the app does not publish (a German-only app keeps no English).
    remove_other_languages: bool


def apple_settings() -> AppleSettings:
    raw = read_json("store.json")["apple"]
    territories = raw.get("territories", "ALL")
    return AppleSettings(
        territories=None if territories == "ALL" else tuple(territories),
        base_territory=raw["baseTerritory"],
        primary_locale=raw["primaryLocale"],
        content_rights=raw["contentRights"],
        copyright=raw.get("copyright"),
        primary_category=raw.get("primaryCategory"),
        secondary_category=raw.get("secondaryCategory"),
        release_type=raw.get("releaseType", "MANUAL"),
        available_in_new_territories=bool(raw.get("availableInNewTerritories", False)),
        remove_other_languages=bool(raw.get("removeOtherLanguages", False)),
    )


def ios_versions(client: AppStoreConnectClient, app_id: str) -> list[dict]:
    return list(client.list_all(f"apps/{app_id}/appStoreVersions", **{"filter[platform]": "IOS", "limit": 50}))


def live_version(client: AppStoreConnectClient, app_id: str) -> dict | None:
    return next((row for row in ios_versions(client, app_id) if version_state(row) in LIVE_VERSION_STATES), None)


def current_version(client: AppStoreConnectClient, app_id: str) -> dict | None:
    """The newest version that is not ended: in preparation, in review or pending, else live."""
    versions = ios_versions(client, app_id)
    for wanted in (EDITABLE_VERSION_STATES, None, LIVE_VERSION_STATES):
        for row in versions:
            state = version_state(row)
            if wanted is None:
                if state not in EDITABLE_VERSION_STATES | LIVE_VERSION_STATES | ENDED_VERSION_STATES:
                    return row
            elif state in wanted:
                return row
    return None


def editable_version(client: AppStoreConnectClient, app_id: str) -> dict | None:
    return next((row for row in ios_versions(client, app_id) if version_state(row) in EDITABLE_VERSION_STATES), None)


def ensure_version(
    client: AppStoreConnectClient,
    app_id: str,
    version_string: str | None,
    report: Report,
    *,
    release_type: str = "MANUAL",
) -> tuple[dict, bool]:
    """The version a listing run works on, and whether it is editable.

    A plan never creates or renames: it reports what ``execute`` would do and returns the version to
    compare against (the one in preparation, else the live one).
    """
    versions = ios_versions(client, app_id)
    editable = next((row for row in versions if version_state(row) in EDITABLE_VERSION_STATES), None)
    live = next((row for row in versions if version_state(row) in LIVE_VERSION_STATES), None)
    if editable is not None:
        current = editable["attributes"]["versionString"]
        if not version_string or version_string == current:
            report.ok(f"version {current} ({version_state(editable)})")
            return editable, True
        if live is None and version_state(editable) == "PREPARE_FOR_SUBMISSION":
            report.change(f"version: rename the first release {current} -> {version_string}")
            if report.execute:
                client.patch(
                    f"appStoreVersions/{editable['id']}",
                    {"data": {"type": "appStoreVersions", "id": editable["id"], "attributes": {"versionString": version_string}}},
                )
                editable = client.get(f"appStoreVersions/{editable['id']}")["data"]
            return editable, True
        raise AppStoreConnectError(
            f"{current} is already in preparation ({version_state(editable)}); pass --version {current} or "
            f"none, not {version_string}"
        )
    in_flight = next(
        (row for row in versions if version_state(row) not in EDITABLE_VERSION_STATES | LIVE_VERSION_STATES | ENDED_VERSION_STATES),
        None,
    )
    if in_flight is not None and (not version_string or version_string == in_flight["attributes"]["versionString"]):
        ## Waiting for review, in review, pending release: nothing takes edits, a plan reads it.
        report.note(
            f"{in_flight['attributes']['versionString']} is {version_state(in_flight)}: comparing against it, "
            f"nothing can be written until it is released or rejected"
        )
        return in_flight, False
    if not version_string:
        if live is None:
            raise AppStoreConnectError("no iOS version in preparation and none live — pass --version X.Y.Z")
        report.note(f"no version in preparation: comparing against the live {live['attributes']['versionString']}")
        return live, False
    report.change(f"version: create {version_string} (App Store Connect copies the live listing into it)")
    if not report.execute:
        if live is None:
            raise AppStoreConnectError(f"no {version_string} and nothing live to compare against")
        return live, False
    created = client.post(
        "appStoreVersions",
        {
            "data": {
                "type": "appStoreVersions",
                "attributes": {"platform": "IOS", "versionString": version_string, "releaseType": release_type},
                "relationships": {"app": ref("apps", app_id)},
            }
        },
    )["data"]
    return created, True


def app_info(client: AppStoreConnectClient, app_id: str, *, editable_only: bool) -> dict:
    """The app-info record that takes edits (there are two once the app is live), or for a plan the
    live one when none is editable."""
    rows = list(client.list_all(f"apps/{app_id}/appInfos", include="primaryCategory,secondaryCategory"))
    editable = [
        row for row in rows
        if (row["attributes"].get("state") or row["attributes"].get("appStoreState")) not in LIVE_APP_INFO_STATES
    ]
    if editable:
        return editable[0]
    if editable_only or not rows:
        raise AppStoreConnectError("no editable app info: the record has no version in preparation")
    return rows[0]


__all__ = [
    "API_V2",
    "EDITABLE_VERSION_STATES",
    "LIVE_VERSION_STATES",
    "AppleSettings",
    "app_info",
    "apple_settings",
    "current_version",
    "editable_version",
    "ensure_version",
    "ios_versions",
    "live_version",
    "ref",
    "resolve_app",
    "version_state",
]
