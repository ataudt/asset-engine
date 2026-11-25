"""App Store custom product pages: promotional text, ticked keywords and screenshots, as drafts.

Layer: asset engine, store publishing. Moved here from NutriSpy's
``marketing/app_store_connect/custom_product_pages.py``; the pages themselves (which slugs exist, how
their copy layers onto the main listing) stay the app's (``PageRegistry``, ``PublishFiles.listing_reader``).
Rules:
  - The input is ``listing.page_bundle``: what a page may differ in from the main listing, assembled
    from the app's listing source in memory.
  - **The main listing is out of reach.** The client is built to refuse every request naming the main
    listing's version or app info (``MAIN_LISTING_RESOURCES``): a custom page has CPP-scoped resources
    for everything it may change, so a request naming one of those is a bug that would overwrite the
    general-audience listing.
  - **Draft only.** Promotional text goes live without review, screenshots do not; this never creates
    a ``reviewSubmission``. Submitting is a human press in the Console, where the page can be seen.
  - Idempotent, and resumable: the page is found by its recorded id or its name, the editable version
    reused, localizations patched, and in each screenshot set only the frames that are missing,
    changed or never finished processing are sent (``listing.replace_screenshots``).
  - Search keywords are a set per localization *of a version*: a draft opened behind a live page
    starts with none ticked, so the page's words are synced onto the draft on every run — added and
    removed until the two agree, one word per request, as Apple refuses a word the app's *approved*
    keyword field does not hold yet. ``keywords_only`` does just that.
  - A page whose version is with App Review is refused, not edited around. A live page gets a new
    draft version; the approved one keeps serving until the draft is approved in turn.
  - Page ids and their ``ppid`` URLs are recorded in ``appstore/custom_product_pages.json``: Apple
    keeps the name of a *deleted* page reserved, so once the id is recorded the name stops mattering.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from urllib.parse import parse_qs, urlparse

import requests

from asset_engine.store.connect.app import resolve_app
from asset_engine.store.connect.client import AppStoreConnectClient, AppStoreConnectError
from asset_engine.store.connect.listing import replace_screenshots
from asset_engine.store.listing import PageBundle, appstore_dir


logger = logging.getLogger(__name__)

## Resources of the app's *live product page*, refused by a custom-page client. Stems, not full type
## names: the same resource appears as a path segment (`appStoreVersions`), a relationship key
## (`appStoreVersionLocalization`, singular) and a payload `type`.
MAIN_LISTING_RESOURCES = ("appStoreVersion", "appInfo")
## Apple's limit per app.
MAX_PAGES_PER_APP = 35
EDITABLE_STATES = frozenset({"PREPARE_FOR_SUBMISSION", "REJECTED"})
IN_REVIEW_STATES = frozenset({"READY_FOR_REVIEW", "WAITING_FOR_REVIEW", "IN_REVIEW", "ACCEPTED"})
PAGE_MAP_NAME = "custom_product_pages.json"


class PageLocked(RuntimeError):
    """A version of the page is with App Review, so no draft can be edited or opened."""


class PageMissing(RuntimeError):
    """``keywords_only`` asked for a page that does not exist; it never creates one."""


def custom_page_client(key_getter: Callable[[], dict]) -> AppStoreConnectClient:
    return AppStoreConnectClient(key_getter=key_getter, forbidden_resources=MAIN_LISTING_RESOURCES)


@dataclass
class PageReport:
    """What a run did, or would do, for one page."""

    page: str
    execute: bool
    page_id: str | None = None
    page_name: str | None = None
    page_url: str | None = None
    actions: list[str] = field(default_factory=list)
    changes: int = 0
    ## Locales whose keyword edit Apple refused; the run fails the page when this is non-empty.
    refused: list[str] = field(default_factory=list)

    def add(self, action: str, *, change: bool = False) -> None:
        self.changes += int(change)
        self.actions.append(action)
        print(f"  {'plan  ' if change and not self.execute else ('set   ' if change else 'ok    ')}  {action}", flush=True)

    @property
    def ppid(self) -> str | None:
        if not self.page_url:
            return None
        values = parse_qs(urlparse(self.page_url).query).get("ppid")
        return values[0] if values else None


## -- the page ------------------------------------------------------------------------------------


def page_map_path():
    return appstore_dir() / PAGE_MAP_NAME


def recorded_page_id(page: str) -> str | None:
    path = page_map_path()
    if not path.is_file():
        return None
    return (json.loads(path.read_text(encoding="utf-8")).get(page) or {}).get("pageId")


def find_page(client: AppStoreConnectClient, app_id: str, name: str, page: str) -> dict | None:
    pages = list(client.list_all(f"apps/{app_id}/appCustomProductPages", limit=200))
    known = recorded_page_id(page)
    for row in pages:
        if row["id"] == known or (row.get("attributes") or {}).get("name") == name:
            return row
    if len(pages) >= MAX_PAGES_PER_APP:
        raise AppStoreConnectError(
            f"the app already has {len(pages)} custom product pages, Apple's limit is {MAX_PAGES_PER_APP} — "
            f"retire one before adding {name!r}"
        )
    return None


def _create_page(client: AppStoreConnectClient, app_id: str, name: str, seed: PageBundle) -> dict:
    """Create the page, its first version and one localization — in a single request.

    The documented schema marks ``appCustomProductPageVersions`` optional, yet a create without it is
    rejected (``409 ENTITY_ERROR.RELATIONSHIP.REQUIRED``), and that error demands
    ``appCustomProductPageLocalizations``, which is no relationship of the page at all. What Apple
    wants is JSON:API's ``included``. ``seed`` must be the app's **primary locale**: a create seeded
    with any other is rejected with the same error (NutriSpy, Sep 2026).
    """
    return client.post(
        "appCustomProductPages",
        {
            "data": {
                "type": "appCustomProductPages",
                "attributes": {"name": name},
                "relationships": {
                    "app": {"data": {"type": "apps", "id": app_id}},
                    "appCustomProductPageVersions": {
                        "data": [{"type": "appCustomProductPageVersions", "id": "${new-version}"}]
                    },
                },
            },
            "included": [
                {
                    "type": "appCustomProductPageVersions",
                    "id": "${new-version}",
                    "relationships": {
                        "appCustomProductPageLocalizations": {
                            "data": [{"type": "appCustomProductPageLocalizations", "id": "${new-localization}"}]
                        }
                    },
                },
                {
                    "type": "appCustomProductPageLocalizations",
                    "id": "${new-localization}",
                    "attributes": {"locale": seed.tag, "promotionalText": seed.promo},
                },
            ],
        },
    )["data"]


def _versions(client: AppStoreConnectClient, page_id: str) -> list[dict]:
    return list(client.list_all(f"appCustomProductPages/{page_id}/appCustomProductPageVersions", limit=200))


def _state(version: dict) -> str | None:
    return (version.get("attributes") or {}).get("state")


def _editable_version(client: AppStoreConnectClient, page_id: str, report: PageReport) -> dict:
    versions = _versions(client, page_id)
    for version in versions:
        if _state(version) in EDITABLE_STATES:
            return version
    in_review = sorted({_state(v) for v in versions} & IN_REVIEW_STATES)
    if in_review:
        raise PageLocked(f"a version of this page is in {', '.join(in_review)} — wait for App Review to finish")
    report.add("open a new version behind the live one" if versions else "create the page's first version", change=True)
    try:
        return client.post(
            "appCustomProductPageVersions",
            {
                "data": {
                    "type": "appCustomProductPageVersions",
                    "relationships": {"appCustomProductPage": {"data": {"type": "appCustomProductPages", "id": page_id}}},
                }
            },
        )["data"]
    except requests.Timeout:
        ## Opening a version behind a live one copies every localization and its screenshots
        ## server-side, which outlasts the read timeout while still succeeding (NutriSpy, Oct 2026).
        ## A POST is not retried, so read back.
        for version in _versions(client, page_id):
            if _state(version) in EDITABLE_STATES:
                return version
        raise


def _localizations(client: AppStoreConnectClient, version_id: str) -> dict[str, dict]:
    return {
        (row.get("attributes") or {}).get("locale"): row
        for row in client.list_all(f"appCustomProductPageVersions/{version_id}/appCustomProductPageLocalizations", limit=200)
    }


def _localization(client: AppStoreConnectClient, version_id: str, item: PageBundle, report: PageReport) -> dict:
    row = _localizations(client, version_id).get(item.tag)
    if row is None:
        report.add(f"[{item.tag}] create localization with its promotional text", change=True)
        return client.post(
            "appCustomProductPageLocalizations",
            {
                "data": {
                    "type": "appCustomProductPageLocalizations",
                    "attributes": {"locale": item.tag, "promotionalText": item.promo},
                    "relationships": {
                        "appCustomProductPageVersion": {"data": {"type": "appCustomProductPageVersions", "id": version_id}}
                    },
                }
            },
        )["data"]
    if ((row.get("attributes") or {}).get("promotionalText") or "").strip() == item.promo:
        report.add(f"[{item.tag}] promotional text")
        return row
    report.add(f"[{item.tag}] promotional text updated ({len(item.promo)} chars)", change=True)
    return client.patch(
        f"appCustomProductPageLocalizations/{row['id']}",
        {"data": {"type": "appCustomProductPageLocalizations", "id": row["id"], "attributes": {"promotionalText": item.promo}}},
    )["data"]


## -- keywords --------------------------------------------------------------------------------------


def _keywords_path(localization_id: str) -> str:
    return f"appCustomProductPageLocalizations/{localization_id}/relationships/searchKeywords"


def _linkage(words: list[str]) -> dict:
    return {"data": [{"type": "appKeywords", "id": word} for word in words]}


def _sync_keywords(
    client: AppStoreConnectClient, item: PageBundle, report: PageReport, *, localization_id: str | None, execute: bool
) -> None:
    """Make the localization's ticked words equal the page's; ``localization_id`` is None for one that
    does not exist yet (or a version still to be opened), which starts with nothing ticked."""
    ticked = {row["id"] for row in client.list_all(_keywords_path(localization_id), limit=200)} if localization_id else set()
    add = [word for word in item.keywords if word not in ticked]
    drop = sorted(ticked - set(item.keywords))
    if not add and not drop:
        report.add(f"[{item.tag}] keywords {','.join(item.keywords) or 'none'}")
        return
    change = " ".join([*(f"+{word}" for word in add), *(f"-{word}" for word in drop)])
    report.add(f"[{item.tag}] keywords {change}", change=True)
    if not execute:
        return
    if drop:
        client.delete(_keywords_path(localization_id), _linkage(drop))
    refused: list[str] = []
    for word in add:
        try:
            client.post(_keywords_path(localization_id), _linkage([word]))
        except AppStoreConnectError:
            logger.warning("[%s] keyword %r refused", item.tag, word, exc_info=True)
            refused.append(word)
    if refused:
        report.refused.append(item.tag)
        report.add(f"[{item.tag}] keywords REFUSED: {','.join(refused)} (not in the approved keyword field yet)")


## -- screenshots ---------------------------------------------------------------------------------


def _screenshot_set(client: AppStoreConnectClient, localization_id: str, display_type: str) -> dict:
    for row in client.list_all(f"appCustomProductPageLocalizations/{localization_id}/appScreenshotSets", limit=50):
        if (row.get("attributes") or {}).get("screenshotDisplayType") == display_type:
            return row
    return client.post(
        "appScreenshotSets",
        {
            "data": {
                "type": "appScreenshotSets",
                "attributes": {"screenshotDisplayType": display_type},
                "relationships": {
                    "appCustomProductPageLocalization": {"data": {"type": "appCustomProductPageLocalizations", "id": localization_id}}
                },
            }
        },
    )["data"]


## -- the run ---------------------------------------------------------------------------------------


def _plan_existing(client: AppStoreConnectClient, page_id: str, bundles: list[PageBundle], report: PageReport) -> None:
    """The read-only half: compared against the draft if there is one, else the live version."""
    versions = _versions(client, page_id)
    states = {_state(v) for v in versions}
    editable = [v for v in versions if _state(v) in EDITABLE_STATES]
    in_review = sorted(states & IN_REVIEW_STATES)
    if not editable and in_review:
        report.add(f"REFUSED: a version is in {', '.join(in_review)} — with App Review, not editable")
        return
    if not versions:
        report.add("create the page's first version", change=True)
    elif not editable:
        report.add("open a new version behind the live one", change=True)
    baseline = editable[0] if editable else (versions[0] if versions else None)
    live = _localizations(client, baseline["id"]) if baseline else {}
    from asset_engine.store.connect.assets import checksum, is_complete

    for item in bundles:
        row = live.get(item.tag)
        if row is None:
            report.add(f"[{item.tag}] create localization, promotional text ({len(item.promo)} chars)", change=True)
        elif ((row.get("attributes") or {}).get("promotionalText") or "").strip() == item.promo:
            report.add(f"[{item.tag}] promotional text")
        else:
            report.add(f"[{item.tag}] promotional text would change", change=True)
        _sync_keywords(client, item, report, localization_id=row["id"] if (row is not None and editable) else None, execute=False)
        sets = (
            {
                (s.get("attributes") or {}).get("screenshotDisplayType"): s
                for s in client.list_all(f"appCustomProductPageLocalizations/{row['id']}/appScreenshotSets", limit=50)
            }
            if row is not None
            else {}
        )
        for display_type, frames in item.screenshots.items():
            current = []
            if display_type in sets:
                current = [
                    (shot.get("attributes") or {}).get("sourceFileChecksum")
                    for shot in client.list_all(f"appScreenshotSets/{sets[display_type]['id']}/appScreenshots", limit=50)
                    if is_complete(shot)
                ]
            wanted = [checksum(frame.read_bytes()) for frame in frames]
            if current == wanted:
                report.add(f"[{item.tag}] {display_type}: {len(wanted)} screenshot(s) identical")
            else:
                changed = sum(1 for index, digest in enumerate(wanted) if index >= len(current) or current[index] != digest)
                report.add(
                    f"[{item.tag}] {display_type}: {len(current)} -> {len(wanted)} screenshot(s), {changed} new or changed",
                    change=True,
                )


def write_page_map(report: PageReport, tags: list[str]) -> None:
    """Record the page id and its ppid URL, merged into what the file already has (the locales a
    union: a run may cover one locale, and the page does not lose the others)."""
    if not report.page_id:
        return
    path = page_map_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    previous = (data.get(report.page) or {}).get("locales") or []
    data[report.page] = {
        "pageId": report.page_id,
        "name": report.page_name,
        "url": report.page_url,
        "ppid": report.ppid,
        "locales": sorted(set(previous) | set(tags)),
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    path.write_text(json.dumps(dict(sorted(data.items())), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def publish_page(
    client: AppStoreConnectClient,
    *,
    bundle_id: str,
    page: str,
    bundles: list[PageBundle],
    execute: bool,
    page_name: str | None = None,
    keywords_only: bool = False,
) -> PageReport:
    """Plan (by default) or push one custom page. Leaves the page in draft.

    ``page_name`` overrides the Console name (default: the slug), needed exactly when Apple still
    holds the name from a deleted page; it is recorded, so it is passed once.
    """
    report = PageReport(page=page, execute=execute)
    print(f"custom product page {page}", flush=True)
    app = resolve_app(client, bundle_id)
    app_id = app["id"]
    primary = (app.get("attributes") or {}).get("primaryLocale")
    by_tag = {item.tag: item for item in bundles}
    name = page_name or page
    row = find_page(client, app_id, name, page)
    if row is None and keywords_only:
        raise PageMissing(f"no custom product page {name!r} yet — the full run creates it")
    if row is not None:
        report.page_id = row["id"]
        report.page_name = (row.get("attributes") or {}).get("name") or name
        report.page_url = (row.get("attributes") or {}).get("url")
    if keywords_only:
        versions = _versions(client, report.page_id)
        editable = [v for v in versions if _state(v) in EDITABLE_STATES]
        version = _editable_version(client, report.page_id, report) if execute else (editable[0] if editable else None)
        rows = _localizations(client, version["id"]) if version else {}
        for item in bundles:
            if item.tag not in rows and version is not None:
                report.add(f"[{item.tag}] no localization on this page — skipped (run the full upload)")
                continue
            _sync_keywords(client, item, report, localization_id=(rows.get(item.tag) or {}).get("id"), execute=execute)
        return report
    if row is None:
        report.add(f"create custom product page {name!r} (seeded with {primary})", change=True)
        if primary not in by_tag:
            raise PageMissing(
                f"a new page must be created with the app's primary locale ({primary}); this run covers "
                f"{', '.join(by_tag)} — add it to --language"
            )
        if not execute:
            for item in bundles:
                report.add(f"[{item.tag}] promotional text, keywords {','.join(item.keywords) or 'none'}, "
                           f"{sum(len(f) for f in item.screenshots.values())} screenshot(s)", change=True)
            return report
        try:
            row = _create_page(client, app_id, name, by_tag[primary])
        except AppStoreConnectError as error:
            if "DUPLICATE" not in str(error):
                raise
            raise AppStoreConnectError(
                f"App Store Connect already holds the page name {name!r} (a deleted page keeps its name "
                f"reserved) — pass --page-name with another one; the id is recorded afterwards"
            ) from error
        report.page_id = row["id"]
        report.page_name = (row.get("attributes") or {}).get("name") or name
    if not execute:
        _plan_existing(client, report.page_id, bundles, report)
        return report
    version = _editable_version(client, report.page_id, report)
    for item in bundles:
        localization = _localization(client, version["id"], item, report)
        _sync_keywords(client, item, report, localization_id=localization["id"], execute=True)
        for display_type, frames in item.screenshots.items():
            screenshot_set = _screenshot_set(client, localization["id"], display_type)
            uploaded, kept = replace_screenshots(client, screenshot_set["id"], frames)
            report.add(f"[{item.tag}] {display_type}: {uploaded} uploaded, {kept} kept", change=bool(uploaded))
    fresh = client.get(f"appCustomProductPages/{report.page_id}")["data"]
    report.page_url = (fresh.get("attributes") or {}).get("url") or report.page_url
    write_page_map(report, sorted(by_tag))
    return report


__all__ = [
    "IN_REVIEW_STATES",
    "MAIN_LISTING_RESOURCES",
    "MAX_PAGES_PER_APP",
    "PageLocked",
    "PageMissing",
    "PageReport",
    "custom_page_client",
    "publish_page",
    "write_page_map",
]
