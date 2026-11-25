"""Place the App Store creative assets (product page header, search results image) on a version.

Layer: asset engine, store upload.
Rules:
  - The app supplies the client (its key), its production bundle id and the locales to place, as
    ``{frame language: Apple locale}`` — which locales are ready is app knowledge (a listing whose
    copy is incomplete is not uploaded, so its localization may not exist).
  - The input is what the store CLI's ``creatives`` command rendered for the main set, one file per
    locale and per placement in the app's ``creatives.json``. Nothing is re-rendered here, so what
    goes to Apple is exactly what is on disk.
  - **The placements ride the version.** A placement on an App Store version localization is reviewed
    with that version (developer.apple.com/documentation/appstoreconnectapi/placing-assets-on-your-
    app-store-surfaces, read 2026-11-17), so nothing here creates a ``reviewSubmission``. Writing is
    refused on a version that is not editable.
  - **Compared, never re-sent blindly.** Every image is filed under a reference name that carries its
    file's checksum. A placement whose image already carries the rendered file's checksum stays, so
    the step runs on every listing upload and sends only what changed. An image a stalled run left
    unplaced in the library is found by that name and placed instead of uploaded again.
  - All or nothing on disk: a locale missing one of its files raises before anything is sent. No
    rendered creatives at all is a release built without them, and the step leaves Apple's placements
    as they are.
  - What Apple accepts per placement (category, placement group, file types, sizes) is read from the
    asset library's reference data on every run and checked before the first write, not restated.
  - A replaced image is deleted only when nothing places it any more and Apple has not approved it.
    An approved image may still be what the live version shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from pathlib import Path

from PIL import Image

from asset_engine.store.config import load_config
from asset_engine.store.connect.assets import (
    LIBRARY_READY_STATES,
    LibraryUpload,
    checksum,
    upload_library_images,
)
from asset_engine.store.connect.app import (
    EDITABLE_VERSION_STATES,
    LIVE_VERSION_STATES,
    resolve_app,
    version_state,
)
from asset_engine.store.connect.client import AppStoreConnectClient, AppStoreConnectError
from asset_engine.store.creatives import load_creatives_config


logger = logging.getLogger(__name__)

## Apple's placement type for each placement an app's ``creatives.json`` may lay out. Each app sweeps
## its own layout against this map (`test_every_creative_placement_has_an_app_store_placement_type`).
PLACEMENT_TYPES: dict[str, str] = {
    "header": "PRODUCT_PAGE_HEADER_ASSET",
    "search-results": "APP_STORE_SEARCH_RESULTS_ASSET",
}

## The reference data's name for the main listing's versions, whose policies say which placement
## groups a version localization takes.
APP_STORE_VERSIONS_FEATURE = "APP_STORE_VERSIONS"



class CreativesIncomplete(RuntimeError):
    """The rendered creatives on disk are partial or do not match what Apple accepts. Nothing is sent."""


@dataclass(frozen=True)
class PlacementSpec:
    """What Apple accepts for one placement type on an App Store version, from the reference data."""

    placement_type: str
    category: str
    group: str
    ## Per accepted image spec: the file extensions and the (min_w, max_w, min_h, max_h) it allows.
    specs: tuple[tuple[frozenset[str], tuple[int, int, int, int]], ...]

    def refusal(self, path: Path) -> str | None:
        """Why Apple would refuse ``path`` for this placement, or ``None``."""
        with Image.open(path) as image:
            width, height = image.size
        suffix = path.suffix.lower()
        for extensions, (min_w, max_w, min_h, max_h) in self.specs:
            if suffix in extensions and min_w <= width <= max_w and min_h <= height <= max_h:
                return None
        accepted = "; ".join(
            f"{'/'.join(sorted(extensions))} {min_w}-{max_w}x{min_h}-{max_h}"
            for extensions, (min_w, max_w, min_h, max_h) in self.specs
        )
        return f"{path.name} ({suffix} {width}x{height}) fits no spec Apple accepts for {self.placement_type}: {accepted}"


@dataclass
class CreativesReport:
    version: str = ""
    lines: list[str] = field(default_factory=list)
    sent: int = 0
    unchanged: int = 0
    ## Placements a plan would add or replace.
    planned: int = 0

    def add(self, line: str) -> None:
        self.lines.append(line)
        print(line)


## -- the files on disk --------------------------------------------------------------------------


def rendered_creatives(locales: Mapping[str, str]) -> dict[str, dict[str, Path]]:
    """``{apple_locale: {placement: path}}`` for every placement ``creatives.json`` lays out, for each
    ``{frame language: apple_locale}`` in ``locales``.

    Empty when nothing is rendered. Raises ``CreativesIncomplete`` when only some files are, and when
    a placement has no Apple type, before any file is looked at.
    """
    creatives = load_creatives_config()
    untyped = [name for name in creatives.placements if name not in PLACEMENT_TYPES]
    if untyped:
        raise CreativesIncomplete(f"no App Store placement type for {', '.join(untyped)} — add it to PLACEMENT_TYPES")
    config = load_config()
    found: dict[str, dict[str, Path]] = {}
    missing: list[Path] = []
    for language, locale in locales.items():
        frames = config.frames_dir(language)
        paths = {
            name: frames / creatives.file_name(name, language=language, set_name=config.out_subdir)
            for name in creatives.placements
        }
        missing.extend(path for path in paths.values() if not path.is_file())
        found[locale] = paths
    if len(missing) == sum(len(paths) for paths in found.values()):
        return {}
    if missing:
        names = "\n  ".join(str(path) for path in missing)
        raise CreativesIncomplete(
            f"{len(missing)} creative(s) not rendered — run the store CLI's `creatives` command "
            f"first:\n  {names}"
        )
    return found


def reference_name(placement: str, locale: str, path: Path) -> str:
    """The name an image is filed under: what tells this render from any other on a later run."""
    return f"creative {placement} {locale} md5:{checksum(path.read_bytes())}"


## -- what Apple accepts --------------------------------------------------------------------------


def placement_specs(client: AppStoreConnectClient) -> dict[str, PlacementSpec]:
    """Each of our placement types as the asset library's reference data states it."""
    rows = client.get("appAssetLibraryRefData")["data"]
    if len(rows) != 1:
        raise AppStoreConnectError(f"expected one appAssetLibraryRefData row, got {len(rows)}")
    attributes = rows[0]["attributes"]
    types = {row["placementTypeId"]: row for row in attributes["placementTypes"]}
    images = {row["specId"]: row for row in attributes["imageSpecs"]}
    feature = next(
        (row for row in attributes["features"] if row["featureId"] == APP_STORE_VERSIONS_FEATURE), None
    )
    if feature is None:
        raise AppStoreConnectError(f"the reference data names no {APP_STORE_VERSIONS_FEATURE} feature")
    policies = {row["placementType"]: row for row in feature["placementPolicies"]}

    specs: dict[str, PlacementSpec] = {}
    for placement_type in PLACEMENT_TYPES.values():
        if placement_type not in types or placement_type not in policies:
            raise AppStoreConnectError(f"App Store versions take no {placement_type} placement any more")
        categories = types[placement_type]["acceptsAssetCategories"]
        groups = [group for limit in policies[placement_type]["groupLimits"] for group in limit["groupIds"]]
        if len(categories) != 1 or len(groups) != 1:
            raise AppStoreConnectError(
                f"{placement_type}: expected one asset category and one placement group, Apple offers "
                f"categories {categories} and groups {groups} — choose here which one the creatives use"
            )
        spec_ids = [
            spec_id
            for mapping in types[placement_type]["specMappings"]
            if mapping["placementGroupId"] == groups[0]
            for spec_id in mapping["specs"]
            if spec_id in images
        ]
        specs[placement_type] = PlacementSpec(
            placement_type=placement_type,
            category=categories[0],
            group=groups[0],
            specs=tuple(
                (
                    frozenset(extension.lower() for extension in images[spec_id]["fileExtensions"]),
                    (
                        images[spec_id]["dimensions"]["minWidth"],
                        images[spec_id]["dimensions"]["maxWidth"],
                        images[spec_id]["dimensions"]["minHeight"],
                        images[spec_id]["dimensions"]["maxHeight"],
                    ),
                )
                for spec_id in spec_ids
            ),
        )
    return specs


## -- the version ---------------------------------------------------------------------------------


def find_version(
    client: AppStoreConnectClient, app_id: str, version_string: str | None, *, execute: bool = False
) -> dict:
    """The iOS version named ``version_string``; without one, the version in preparation, or for a
    plan with none in preparation, the live one."""
    versions = list(
        client.list_all(f"apps/{app_id}/appStoreVersions", **{"filter[platform]": "IOS", "limit": 50})
    )
    if version_string:
        match = [row for row in versions if row["attributes"]["versionString"] == version_string]
        if not match:
            raise AppStoreConnectError(f"App Store Connect has no iOS version {version_string}")
        return match[0]
    editable = [row for row in versions if version_state(row) in EDITABLE_VERSION_STATES]
    if editable:
        return editable[0]
    live = [row for row in versions if version_state(row) in LIVE_VERSION_STATES]
    if execute or not live:
        raise AppStoreConnectError("no iOS version in preparation — creatives are placed on one")
    return live[0]


def _localizations(client: AppStoreConnectClient, version_id: str) -> dict[str, str]:
    """``{apple_locale: appStoreVersionLocalizations id}``."""
    rows = client.list_all(f"appStoreVersions/{version_id}/appStoreVersionLocalizations", limit=50)
    return {row["attributes"]["locale"]: row["id"] for row in rows}


def _placed(client: AppStoreConnectClient, localization_id: str, placement_type: str) -> list[tuple[str, dict]]:
    """``[(placement id, its image's attributes)]`` of one type on one localization."""
    page = client.get(
        f"appStoreVersionLocalizations/{localization_id}/placements",
        **{
            "filter[placementType]": placement_type,
            "include": "image",
            "fields[appAssetLibraryImages]": "referenceName,state",
        },
    )
    images = {row["id"]: row.get("attributes") or {} for row in page.get("included") or []}
    placed = []
    for row in page.get("data") or []:
        linkage = ((row.get("relationships") or {}).get("image") or {}).get("data") or {}
        placed.append((row["id"], {"id": linkage.get("id"), **images.get(linkage.get("id"), {})}))
    return placed


def _library_image(client: AppStoreConnectClient, library_id: str, name: str) -> str | None:
    """An image already filed under ``name`` and processed, so a run can place it without sending."""
    for row in client.list_all(f"appAssetLibraries/{library_id}/images", **{"filter[referenceName]": name}):
        attributes = row.get("attributes") or {}
        if attributes.get("referenceName") == name and attributes.get("state") in LIBRARY_READY_STATES:
            return row["id"]
    return None


def _retire(client: AppStoreConnectClient, image: dict) -> None:
    """Delete a replaced image once nothing places it and Apple has not approved it."""
    image_id = image.get("id")
    if not image_id or image.get("state") != "PREPARE_FOR_SUBMISSION":
        return
    if client.get(f"appAssetLibraryImages/{image_id}/relationships/placements").get("data"):
        return
    client.delete(f"appAssetLibraryImages/{image_id}")


## -- the run -------------------------------------------------------------------------------------


def upload_creatives(
    *,
    client: AppStoreConnectClient,
    bundle_id: str,
    locales: Mapping[str, str],
    version_string: str | None = None,
    execute: bool = False,
) -> CreativesReport:
    """Plan (by default) or place the rendered creatives on an iOS version's localizations.

    ``locales`` is ``{frame language: Apple locale}``; ``client`` must be allowed to write the main
    listing's version localizations, which is where the placements live.
    """
    report = CreativesReport()
    rendered = rendered_creatives(locales)
    if not rendered:
        report.add("creatives: none rendered — App Store Connect keeps what is placed")
        return report

    specs = placement_specs(client)
    refusals = [
        refusal
        for paths in rendered.values()
        for name, path in paths.items()
        if (refusal := specs[PLACEMENT_TYPES[name]].refusal(path))
    ]
    if refusals:
        raise CreativesIncomplete("Apple would refuse:\n  " + "\n  ".join(refusals))

    app_id = resolve_app(client, bundle_id)["id"]
    version = find_version(client, app_id, version_string, execute=execute)
    state = version_state(version)
    report.version = version["attributes"]["versionString"]
    if execute and state not in EDITABLE_VERSION_STATES:
        raise AppStoreConnectError(f"{report.version} is {state}, not editable — nothing placed")
    report.add(f"creatives on {report.version} ({state}){'' if execute else ' — plan only'}")
    localizations = _localizations(client, version["id"])
    library_id = client.get(f"apps/{app_id}/assetLibrary")["data"]["id"]

    ## Plan every locale first, so nothing is sent for a run that would stop half-way on a read.
    work: list[tuple[str, str, Path, str, list[tuple[str, dict]]]] = []
    for locale, paths in rendered.items():
        localization_id = localizations.get(locale)
        if localization_id is None:
            report.add(f"[{locale}] not a language of {report.version} — skipped")
            continue
        for name, path in paths.items():
            name_for_render = reference_name(name, locale, path)
            placed = _placed(client, localization_id, PLACEMENT_TYPES[name])
            if any(image.get("referenceName") == name_for_render for _, image in placed):
                report.unchanged += 1
                report.add(f"[{locale}] {name}: same")
                continue
            report.add(f"[{locale}] {name}: {'replace' if placed else 'add'}")
            report.planned += 1
            work.append((localization_id, name, path, name_for_render, placed))
    if not execute or not work:
        return report

    ## Send what the library does not already hold, all at once, then place one by one.
    reusable = {item[3]: _library_image(client, library_id, item[3]) for item in work}
    uploads = [
        LibraryUpload(path=path, category=specs[PLACEMENT_TYPES[name]].category, reference_name=filed)
        for _, name, path, filed, _ in work
        if reusable[filed] is None
    ]
    sent = upload_library_images(client, library_id, uploads) if uploads else {}
    for localization_id, name, path, filed, placed in work:
        image_id = reusable[filed] or sent[path]
        spec = specs[PLACEMENT_TYPES[name]]
        ## The group takes one asset, so the old placement goes before the new one is made.
        for placement_id, _ in placed:
            client.delete(f"appAssetLibraryPlacements/{placement_id}")
        client.post(
            "appAssetLibraryPlacements",
            {
                "data": {
                    "type": "appAssetLibraryPlacements",
                    "attributes": {"placementType": spec.placement_type, "placementGroup": spec.group},
                    "relationships": {
                        "image": {"data": {"type": "appAssetLibraryImages", "id": image_id}},
                        "appStoreVersionLocalization": {
                            "data": {"type": "appStoreVersionLocalizations", "id": localization_id}
                        },
                    },
                }
            },
        )
        for _, image in placed:
            _retire(client, image)
        report.sent += 1
    report.add(f"creatives: {report.sent} placed, {report.unchanged} unchanged on {report.version}")
    return report


__all__ = [
    "PLACEMENT_TYPES",
    "CreativesIncomplete",
    "CreativesReport",
    "placement_specs",
    "rendered_creatives",
    "find_version",
    "upload_creatives",
]
