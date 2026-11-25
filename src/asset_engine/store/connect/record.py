"""The App Store record beyond the listing: content rights, age rating, availability, price, build,
store languages.

Layer: asset engine, store publishing. Moved here from Wunschkalender's
``marketing/app_store_connect/record.py``.
Rules:
  - Each setting is read first and written only when it differs, and only with ``execute``; without
    it the run prints the plan. A re-run after a failure therefore resumes where it stopped.
  - The values are the app's decisions, in ``appstore/store.json`` → ``apple`` (territories, base
    territory, primary language, content rights) and ``appstore/age_rating.json`` (the declaration in
    API field names). The app is free: the subscriptions sell, the app does not.
  - A price already set to something other than free is reported, never overwritten: that would be
    a business decision taken in App Store Connect. So is "available in new territories" when the
    file says otherwise.
  - Store languages: the primary is ``primaryLocale``. A localization in a language the app does not
    publish (not in ``PublishFiles.apple_locales``) is removed only where ``store.json`` says
    ``removeOtherLanguages`` — a new record starts in English, and a German-only app keeps none.
    Everywhere else it is reported and left: NutriSpy keeps a Hindi app-info localization by hand.
"""

from __future__ import annotations

from decimal import Decimal
import json

from asset_engine.store.connect.app import (
    API_V2,
    AppleSettings,
    app_info,
    apple_settings,
    ensure_version,
    ref,
    resolve_app,
)
from asset_engine.store.connect.client import AppStoreConnectClient, AppStoreConnectError
from asset_engine.store.listing import appstore_dir, store_locales
from asset_engine.store.report import Report


def desired_age_rating() -> dict | None:
    """``appstore/age_rating.json`` without its comment, or ``None`` for an app that keeps none."""
    path = appstore_dir() / "age_rating.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return {key: value for key, value in data.items() if not key.startswith("_")}


def ensure_content_rights(client: AppStoreConnectClient, app: dict, settings: AppleSettings, report: Report) -> None:
    current = app["attributes"].get("contentRightsDeclaration")
    if current == settings.content_rights:
        report.ok(f"content rights: {current}")
        return
    report.change(f"content rights: {current} -> {settings.content_rights}")
    if report.execute:
        client.patch(
            f"apps/{app['id']}",
            {"data": {"type": "apps", "id": app["id"], "attributes": {"contentRightsDeclaration": settings.content_rights}}},
        )


def ensure_age_rating(client: AppStoreConnectClient, info: dict, report: Report) -> None:
    wanted = desired_age_rating()
    if wanted is None:
        report.note("age rating: no appstore/age_rating.json — left as it is")
        return
    declaration = client.get(f"appInfos/{info['id']}/ageRatingDeclaration")["data"]
    current = declaration["attributes"]
    differing = {key: value for key, value in wanted.items() if current.get(key) != value}
    if not differing:
        report.ok("age rating: every answer as in appstore/age_rating.json")
        return
    report.change(
        f"age rating: {len(differing)} answer(s) "
        + ", ".join(f"{key} {current.get(key)} -> {value}" for key, value in sorted(differing.items()))
    )
    if report.execute:
        client.patch(
            f"ageRatingDeclarations/{declaration['id']}",
            {"data": {"type": "ageRatingDeclarations", "id": declaration["id"], "attributes": differing}},
        )


def all_territories(client: AppStoreConnectClient) -> list[str]:
    return [row["id"] for row in client.list_all("territories", limit=200)]


def ensure_availability(client: AppStoreConnectClient, app_id: str, settings: AppleSettings, report: Report) -> None:
    """Available where ``store.json`` says (every territory for ``"ALL"``), and in new ones only if it says so."""
    territories = all_territories(client)
    wanted = set(settings.territories) if settings.territories is not None else set(territories)
    label = ", ".join(settings.territories) if settings.territories is not None else "every territory"
    availability = client.get_or_none(f"apps/{app_id}/appAvailabilityV2")
    if availability is None:
        report.change(f"availability: none -> {label}")
        if not report.execute:
            return
        missing = wanted - set(territories)
        if missing:
            raise AppStoreConnectError(f"App Store Connect knows no territory {', '.join(sorted(missing))}")
        included = [
            {
                "type": "territoryAvailabilities",
                "id": f"${{{code}}}",
                "attributes": {"available": code in wanted},
                "relationships": {"territory": ref("territories", code)},
            }
            for code in territories
        ]
        client.request(
            "POST",
            f"{API_V2}/appAvailabilities",
            payload={
                "data": {
                    "type": "appAvailabilities",
                    "attributes": {"availableInNewTerritories": settings.available_in_new_territories},
                    "relationships": {
                        "app": ref("apps", app_id),
                        "territoryAvailabilities": {
                            "data": [{"type": "territoryAvailabilities", "id": row["id"]} for row in included]
                        },
                    },
                },
                "included": included,
            },
        )
        return

    availability_id = availability["data"]["id"]
    if bool(availability["data"]["attributes"].get("availableInNewTerritories")) != settings.available_in_new_territories:
        report.note(
            f"availability: 'available in new territories' is "
            f"{availability['data']['attributes'].get('availableInNewTerritories')}, store.json says "
            f"{settings.available_in_new_territories}; App Store Connect changes it only in Preise und "
            f"Verfügbarkeit -> Verfügbarkeit"
        )
    rows = list(
        client.list_all(
            f"{API_V2}/appAvailabilities/{availability_id}/territoryAvailabilities", include="territory", limit=200
        )
    )
    wrong = [
        row for row in rows
        if bool(row["attributes"].get("available")) != (row["relationships"]["territory"]["data"]["id"] in wanted)
    ]
    if not wrong:
        report.ok(f"availability: {label}")
        return
    for row in wrong:
        code = row["relationships"]["territory"]["data"]["id"]
        available = code in wanted
        report.change(f"availability: {code} {'on' if available else 'off'}")
        if report.execute:
            client.patch(
                f"territoryAvailabilities/{row['id']}",
                {"data": {"type": "territoryAvailabilities", "id": row["id"], "attributes": {"available": available}}},
            )


def ensure_free(client: AppStoreConnectClient, app_id: str, settings: AppleSettings, report: Report) -> None:
    """The app is free: a price schedule based in the base territory at the zero price point."""
    ## Apple answers the schedule itself for every app (its id is the app's); one never set has no
    ## manual prices, and asking for them is the 404.
    schedule = client.get(f"apps/{app_id}/appPriceSchedule")
    prices = client.get_or_none(
        f"appPriceSchedules/{schedule['data']['id']}/manualPrices", include="appPricePoint", limit=50
    )
    if prices is not None and prices.get("data"):
        points = [row for row in prices.get("included", []) if row["type"] == "appPricePoints"]
        customer = {Decimal(row["attributes"]["customerPrice"]) for row in points}
        if customer == {Decimal(0)}:
            report.ok("price: free")
        else:
            report.note(f"price: set to {sorted(customer)}, not free — left as it is, change it in App Store Connect")
        return

    report.change(f"price: none -> free (base territory {settings.base_territory})")
    if not report.execute:
        return
    free = [
        row for row in client.list_all(
            f"apps/{app_id}/appPricePoints", **{"filter[territory]": settings.base_territory, "limit": 200}
        )
        if Decimal(row["attributes"]["customerPrice"]) == 0
    ]
    if not free:
        raise AppStoreConnectError(f"no free price point for {settings.base_territory}")
    client.post(
        "appPriceSchedules",
        {
            "data": {
                "type": "appPriceSchedules",
                "relationships": {
                    "app": ref("apps", app_id),
                    "baseTerritory": ref("territories", settings.base_territory),
                    "manualPrices": {"data": [{"type": "appPrices", "id": "${free}"}]},
                },
            },
            "included": [
                {
                    "type": "appPrices",
                    "id": "${free}",
                    "attributes": {"startDate": None},
                    "relationships": {"appPricePoint": ref("appPricePoints", free[0]["id"])},
                }
            ],
        },
    )


def ensure_build(client: AppStoreConnectClient, app_id: str, version: dict, build_version: str, report: Report) -> None:
    """The version carries the newest processed build of ``build_version`` (the binary's version)."""
    builds = client.get(
        "builds",
        **{
            "filter[app]": app_id,
            "filter[preReleaseVersion.version]": build_version,
            "filter[processingState]": "VALID",
            "sort": "-uploadedDate",
            "limit": 1,
        },
    )["data"]
    attached = (client.get(f"appStoreVersions/{version['id']}/build").get("data") or {}).get("id")
    if not builds:
        report.note(f"build: no processed build of {build_version} on App Store Connect yet")
        return
    build = builds[0]
    label = f"{build_version} ({build['attributes']['version']})"
    if attached == build["id"]:
        report.ok(f"build: {label}")
        return
    report.change(f"build: {'another build' if attached else 'none'} -> {label}")
    if report.execute:
        client.patch(f"appStoreVersions/{version['id']}/relationships/build", ref("builds", build["id"]))


def ensure_locales(
    client: AppStoreConnectClient, app: dict, info: dict, version: dict, settings: AppleSettings, report: Report
) -> None:
    """The primary language is ``primaryLocale``; languages the app does not publish to Apple are removed.

    The primary localization cannot be deleted, and the switch needs the new primary's localization to
    exist, which the listing run creates. So on a first release this follows the listing.
    """
    primary_wanted = settings.primary_locale
    published = set(store_locales("apple").values())
    version_locs = {
        row["attributes"]["locale"]: row["id"]
        for row in client.list_all(f"appStoreVersions/{version['id']}/appStoreVersionLocalizations", limit=50)
    }
    info_locs = {
        row["attributes"]["locale"]: row["id"]
        for row in client.list_all(f"appInfos/{info['id']}/appInfoLocalizations", limit=50)
    }
    primary = app["attributes"]["primaryLocale"]
    if primary != primary_wanted:
        if primary_wanted not in version_locs or primary_wanted not in info_locs:
            report.change(
                f"primary language: {primary} -> {primary_wanted}, once the listing run has added {primary_wanted}"
            )
            return
        report.change(f"primary language: {primary} -> {primary_wanted}")
        if report.execute:
            try:
                client.patch(
                    f"apps/{app['id']}",
                    {"data": {"type": "apps", "id": app["id"], "attributes": {"primaryLocale": primary_wanted}}},
                )
            except AppStoreConnectError:
                report.note(
                    "Apple refused the primary-language switch; set it in App Store Connect -> "
                    "App-Informationen -> Primärsprache, then re-run to remove the other languages"
                )
                raise
    else:
        report.ok(f"primary language: {primary}")

    extra_version = sorted(locale for locale in version_locs if locale not in published)
    extra_info = sorted(locale for locale in info_locs if locale not in published)
    if not extra_version and not extra_info:
        report.ok("store languages: only those the app publishes")
        return
    if not settings.remove_other_languages:
        report.note(
            f"store languages the repo does not publish: {', '.join(sorted(set(extra_version) | set(extra_info)))} "
            f"— left as they are (store.json: removeOtherLanguages)"
        )
        return
    for locale in extra_version:
        report.change(f"remove {locale} (version localization)")
        if report.execute:
            client.delete(f"appStoreVersionLocalizations/{version_locs[locale]}")
    if report.execute and extra_version:
        ## Apple drops a language's app-info localization together with its last version
        ## localization, so what is left is read again rather than deleted from the first list.
        info_locs = {
            row["attributes"]["locale"]: row["id"]
            for row in client.list_all(f"appInfos/{info['id']}/appInfoLocalizations", limit=50)
        }
        extra_info = sorted(locale for locale in info_locs if locale not in published)
    for locale in extra_info:
        report.change(f"remove {locale} (app info localization)")
        if report.execute:
            client.delete(f"appInfoLocalizations/{info_locs[locale]}")


def apply_record(
    client: AppStoreConnectClient,
    *,
    bundle_id: str,
    version_string: str | None = None,
    build_version: str | None = None,
    execute: bool = False,
) -> Report:
    """Plan (by default) or apply every setting above to the iOS record.

    ``build_version`` is the binary's version to attach, by default the version's own string. They
    differ in the plan of a first release, while the version is still App Store Connect's "1.0".
    """
    settings = apple_settings()
    report = Report(execute=execute)
    report.section(f"App Store record of {bundle_id}")
    app = resolve_app(client, bundle_id)
    version, editable = ensure_version(client, app["id"], version_string, report, release_type=settings.release_type)
    if execute and not editable:
        raise AppStoreConnectError("no version in preparation — the record is written with a version in preparation")
    info = app_info(client, app["id"], editable_only=execute)

    ensure_content_rights(client, app, settings, report)
    ensure_age_rating(client, info, report)
    ensure_availability(client, app["id"], settings, report)
    ensure_free(client, app["id"], settings, report)
    ensure_build(
        client, app["id"], version, build_version or version_string or version["attributes"]["versionString"], report
    )
    ensure_locales(client, app, info, version, settings, report)
    print(report.summary())
    return report


__all__ = ["apply_record", "desired_age_rating"]
