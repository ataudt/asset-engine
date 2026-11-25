"""The App Store listing: version and app-info localizations, screenshots, review details, categories.

Layer: asset engine, store publishing. Replaces fastlane ``deliver``.
Rules:
  - Read, compare, plan; with ``execute`` write only the fields that differ, on an editable version.
    A plan with no version in preparation compares against the live one (``app.ensure_version``).
  - A store language the repo does not publish yet is created only when asked (``add_languages``):
    a wrong tag would otherwise publish the listing in a new store language instead of failing.
  - A field the repo leaves empty is left as it is on the store: no keywords means the keyword field
    is untouched, no What's New block means the release notes are (a first version has none at all).
  - Screenshots are compared by Apple's ``sourceFileChecksum`` (MD5), in order, per display type. An
    identical set is not touched; otherwise frames already uploaded are kept, the rest replaced, and
    the set reordered: upload order does not set display order, and a caption is welded to its slot.
    Only the display types the app renders are touched; any other set stays exactly as it is.
  - Review contact and notes come from ``appstore/review_information.json`` (notes as a list of
    paragraphs); copyright, categories and release type from ``store.json`` → ``apple``.
"""

from __future__ import annotations

import json
from pathlib import Path

from asset_engine.store.connect.app import (
    app_info,
    apple_settings,
    ensure_version,
    live_version,
    ref,
    resolve_app,
)
from asset_engine.store.connect.assets import checksum, is_complete, upload_screenshots
from asset_engine.store.connect.client import AppStoreConnectClient, AppStoreConnectError
from asset_engine.store.listing import AppleListing, appstore_dir
from asset_engine.store.report import Report


## Version localization: API attribute -> AppleListing field.
VERSION_FIELDS = (
    ("description", "description"),
    ("keywords", "keywords"),
    ("promotionalText", "promotional_text"),
    ("supportUrl", "support_url"),
    ("marketingUrl", "marketing_url"),
    ("whatsNew", "whats_new"),
)
## App info localization.
INFO_FIELDS = (("name", "name"), ("subtitle", "subtitle"), ("privacyPolicyUrl", "privacy_url"))
## appstore/review_information.json key -> App Store review detail attribute.
REVIEW_FIELDS = (
    ("first_name", "contactFirstName"),
    ("last_name", "contactLastName"),
    ("phone_number", "contactPhone"),
    ("email_address", "contactEmail"),
    ("notes", "notes"),
)


def describe(old: str | None, new: str) -> str:
    """A change, short enough for one line: both values when short, else lengths and where they part."""
    old = old or ""
    if len(old) <= 70 and len(new) <= 70:
        return f"{old!r} -> {new!r}"
    at = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
    start = max(0, at - 20)
    return f"{len(old)} -> {len(new)} chars, from …{new[start:at + 40]!r}…".replace("\n", "⏎")


def _norm(value: str | None) -> str:
    return (value or "").strip()


def sync_fields(
    current: dict, wanted: dict[str, str | None], *, label: str, report: Report
) -> dict[str, str]:
    """The fields of ``wanted`` (``None`` = leave alone) whose value differs from ``current``."""
    differing: dict[str, str] = {}
    for key, value in wanted.items():
        if value is None:
            continue
        if _norm(current.get(key)) == _norm(value):
            continue
        report.change(f"{label} {key}: {describe(current.get(key), value)}")
        differing[key] = value
    return differing


## -- localizations ------------------------------------------------------------------------------


def _localizations(client: AppStoreConnectClient, path: str) -> dict[str, dict]:
    return {row["attributes"]["locale"]: row for row in client.list_all(path, limit=50)}


def sync_version_localization(
    client: AppStoreConnectClient,
    version_id: str,
    current: dict | None,
    listing: AppleListing,
    *,
    add_languages: bool,
    report: Report,
) -> dict | None:
    """Bring one version localization to ``listing``; returns it (``None`` in a plan that would create it)."""
    wanted = {api: getattr(listing, attr) for api, attr in VERSION_FIELDS}
    label = f"[{listing.tag}]"
    if current is None:
        if not add_languages:
            raise AppStoreConnectError(
                f"{listing.tag} is not a language of this version; a wrong tag would publish a new store "
                f"language — fix the app's apple_locales, or pass --add-languages if that is the intent"
            )
        report.change(f"{label} add the store language {listing.tag}")
        if not report.execute:
            return None
        return client.post(
            "appStoreVersionLocalizations",
            {
                "data": {
                    "type": "appStoreVersionLocalizations",
                    "attributes": {"locale": listing.tag, **{k: v for k, v in wanted.items() if v is not None}},
                    "relationships": {"appStoreVersion": ref("appStoreVersions", version_id)},
                }
            },
        )["data"]
    differing = sync_fields(current["attributes"], wanted, label=label, report=report)
    if not differing:
        report.ok(f"{label} description, keywords, promotional text, URLs, What's New")
    elif report.execute:
        client.patch(
            f"appStoreVersionLocalizations/{current['id']}",
            {"data": {"type": "appStoreVersionLocalizations", "id": current["id"], "attributes": differing}},
        )
    return current


def sync_info_localization(
    client: AppStoreConnectClient, info_id: str, current: dict | None, listing: AppleListing, report: Report
) -> None:
    wanted = {api: getattr(listing, attr) for api, attr in INFO_FIELDS}
    label = f"[{listing.tag}]"
    if current is None:
        report.change(f"{label} app info: name, subtitle, privacy URL")
        if report.execute:
            client.post(
                "appInfoLocalizations",
                {
                    "data": {
                        "type": "appInfoLocalizations",
                        "attributes": {"locale": listing.tag, **wanted},
                        "relationships": {"appInfo": ref("appInfos", info_id)},
                    }
                },
            )
        return
    differing = sync_fields(current["attributes"], wanted, label=label, report=report)
    if not differing:
        report.ok(f"{label} name, subtitle, privacy URL")
    elif report.execute:
        client.patch(
            f"appInfoLocalizations/{current['id']}",
            {"data": {"type": "appInfoLocalizations", "id": current["id"], "attributes": differing}},
        )


## -- screenshots ----------------------------------------------------------------------------------


def _screenshot_sets(client: AppStoreConnectClient, localization_id: str) -> dict[str, dict]:
    return {
        row["attributes"]["screenshotDisplayType"]: row
        for row in client.list_all(f"appStoreVersionLocalizations/{localization_id}/appScreenshotSets", limit=50)
    }


def _create_set(client: AppStoreConnectClient, localization_id: str, display_type: str) -> dict:
    return client.post(
        "appScreenshotSets",
        {
            "data": {
                "type": "appScreenshotSets",
                "attributes": {"screenshotDisplayType": display_type},
                "relationships": {
                    "appStoreVersionLocalization": ref("appStoreVersionLocalizations", localization_id)
                },
            }
        },
    )["data"]


def replace_screenshots(client: AppStoreConnectClient, set_id: str, paths: list[Path]) -> tuple[int, int]:
    """Make ``set_id`` hold ``paths`` in order; returns (uploaded, kept).

    A frame already in the set stays when it is complete and byte-identical (Apple keeps the MD5 the
    upload committed); everything else goes — a stale render, a duplicate, and a frame Apple left
    unprocessed (NutriSpy, Nov 2026: three runs in a row ended on one).
    """
    wanted = {checksum(path.read_bytes()): path for path in paths}
    kept: dict[str, str] = {}
    for existing in client.list_all(f"appScreenshotSets/{set_id}/appScreenshots", limit=50):
        digest = (existing.get("attributes") or {}).get("sourceFileChecksum")
        if digest in wanted and digest not in kept and is_complete(existing):
            kept[digest] = existing["id"]
        else:
            client.delete(f"appScreenshots/{existing['id']}")
    missing = [path for digest, path in wanted.items() if digest not in kept]
    uploaded = dict(zip((checksum(p.read_bytes()) for p in missing), upload_screenshots(client, set_id, missing)))
    ordered = [kept.get(digest) or uploaded[digest] for digest in wanted]
    client.patch(
        f"appScreenshotSets/{set_id}/relationships/appScreenshots",
        {"data": [{"type": "appScreenshots", "id": shot_id} for shot_id in ordered]},
    )
    return len(uploaded), len(kept)


def sync_screenshots(
    client: AppStoreConnectClient, localization: dict | None, listing: AppleListing, report: Report
) -> None:
    sets = _screenshot_sets(client, localization["id"]) if localization else {}
    for display_type, paths in listing.screenshots.items():
        label = f"[{listing.tag}] {display_type}"
        current_set = sets.get(display_type)
        current: list[str] = []
        if current_set is not None:
            current = [
                (row.get("attributes") or {}).get("sourceFileChecksum") or ""
                for row in client.list_all(f"appScreenshotSets/{current_set['id']}/appScreenshots", limit=50)
                if is_complete(row)
            ]
        wanted = [checksum(path.read_bytes()) for path in paths]
        if current == wanted:
            report.ok(f"{label}: {len(wanted)} screenshot(s) identical")
            continue
        changed = sum(1 for digest in wanted if digest not in current)
        report.change(f"{label}: {len(current)} -> {len(wanted)} screenshot(s), {changed} new or changed")
        if not report.execute or localization is None:
            continue
        set_id = current_set["id"] if current_set else _create_set(client, localization["id"], display_type)["id"]
        uploaded, kept = replace_screenshots(client, set_id, paths)
        report.note(f"{label}: {uploaded} uploaded, {kept} kept")


## -- version-level ----------------------------------------------------------------------------------


def review_information() -> dict[str, str] | None:
    path = appstore_dir() / "review_information.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for key, api in REVIEW_FIELDS:
        value = data.get(key)
        if isinstance(value, list):
            value = "\n\n".join(paragraph.strip() for paragraph in value)
        if value:
            out[api] = value.strip()
    return out


def sync_review_detail(client: AppStoreConnectClient, version: dict, report: Report) -> None:
    wanted = review_information()
    if wanted is None:
        return
    detail = client.get_or_none(f"appStoreVersions/{version['id']}/appStoreReviewDetail")
    data = (detail or {}).get("data")
    if not data:
        report.change("review information: create (contact and notes from review_information.json)")
        if report.execute:
            client.post(
                "appStoreReviewDetails",
                {
                    "data": {
                        "type": "appStoreReviewDetails",
                        "attributes": {**wanted, "demoAccountRequired": False},
                        "relationships": {"appStoreVersion": ref("appStoreVersions", version["id"])},
                    }
                },
            )
        return
    differing = sync_fields(data["attributes"], wanted, label="review information", report=report)
    if not differing:
        report.ok("review information")
    elif report.execute:
        client.patch(
            f"appStoreReviewDetails/{data['id']}",
            {"data": {"type": "appStoreReviewDetails", "id": data["id"], "attributes": differing}},
        )


def sync_version_attributes(client: AppStoreConnectClient, version: dict, report: Report) -> None:
    settings = apple_settings()
    wanted = {"copyright": settings.copyright, "releaseType": settings.release_type}
    differing = sync_fields(version["attributes"], wanted, label="version", report=report)
    if not differing:
        report.ok("copyright, release type")
    elif report.execute:
        client.patch(
            f"appStoreVersions/{version['id']}",
            {"data": {"type": "appStoreVersions", "id": version["id"], "attributes": differing}},
        )


def sync_categories(client: AppStoreConnectClient, info: dict, report: Report) -> None:
    settings = apple_settings()
    relationships = info.get("relationships") or {}
    changes: dict[str, dict] = {}
    for key, wanted in (("primaryCategory", settings.primary_category), ("secondaryCategory", settings.secondary_category)):
        if wanted is None:
            continue
        current = ((relationships.get(key) or {}).get("data") or {}).get("id")
        if current == wanted:
            continue
        report.change(f"{key}: {current} -> {wanted}")
        changes[key] = ref("appCategories", wanted)
    if not changes:
        report.ok("categories")
    elif report.execute:
        client.patch(
            f"appInfos/{info['id']}",
            {"data": {"type": "appInfos", "id": info["id"], "relationships": changes}},
        )


## -- the runs ---------------------------------------------------------------------------------------


def publish_listing(
    client: AppStoreConnectClient,
    *,
    bundle_id: str,
    listings: list[AppleListing],
    version_string: str | None,
    execute: bool,
    add_languages: bool = False,
) -> tuple[Report, str]:
    """Plan (by default) or write the App Store listing; returns the report and the version string."""
    report = Report(execute=execute)
    report.section(f"App Store listing of {bundle_id}")
    app = resolve_app(client, bundle_id)
    version, editable = ensure_version(
        client, app["id"], version_string, report, release_type=apple_settings().release_type
    )
    if execute and not editable:
        raise AppStoreConnectError("no version in preparation to write to — pass --version X.Y.Z to create one")
    info = app_info(client, app["id"], editable_only=execute)
    version_locs = _localizations(client, f"appStoreVersions/{version['id']}/appStoreVersionLocalizations")
    info_locs = _localizations(client, f"appInfos/{info['id']}/appInfoLocalizations")

    sync_version_attributes(client, version, report)
    sync_categories(client, info, report)
    for listing in listings:
        localization = sync_version_localization(
            client, version["id"], version_locs.get(listing.tag), listing, add_languages=add_languages, report=report
        )
        sync_info_localization(client, info["id"], info_locs.get(listing.tag), listing, report)
        if listing.screenshots:
            sync_screenshots(client, localization, listing, report)
    sync_review_detail(client, version, report)
    print(report.summary())
    return report, version["attributes"]["versionString"]


def publish_promotional_text(
    client: AppStoreConnectClient, *, bundle_id: str, texts: dict[str, str], execute: bool
) -> Report:
    """Plan or write the promotional text on the **live** version, the one field it takes without a build."""
    report = Report(execute=execute)
    report.section(f"App Store promotional text of {bundle_id} (live version)")
    app = resolve_app(client, bundle_id)
    version = live_version(client, app["id"])
    if version is None:
        raise AppStoreConnectError("no live version — the promotional text rides the listing instead")
    locs = _localizations(client, f"appStoreVersions/{version['id']}/appStoreVersionLocalizations")
    for tag, text in texts.items():
        current = locs.get(tag)
        if current is None:
            report.note(f"[{tag}] not a language of the live version — skipped")
            continue
        differing = sync_fields(current["attributes"], {"promotionalText": text}, label=f"[{tag}]", report=report)
        if not differing:
            report.ok(f"[{tag}] promotional text")
        elif execute:
            client.patch(
                f"appStoreVersionLocalizations/{current['id']}",
                {"data": {"type": "appStoreVersionLocalizations", "id": current["id"], "attributes": differing}},
            )
    print(report.summary())
    return report


__all__ = ["describe", "publish_listing", "publish_promotional_text", "replace_screenshots", "sync_fields"]
